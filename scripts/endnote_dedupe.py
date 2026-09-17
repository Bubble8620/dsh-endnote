#!/usr/bin/env python3
"""Library maintenance: find and remove duplicate EndNote records safely.

Why this is a separate, cautious tool
-------------------------------------
Deleting a reference is NOT just a DELETE from `refs`. These tables each carry a
per-record row, and leaving them behind makes a library that EndNote has to
repair:

    refs, refs_ord, tag_members, tag_members_content, tag_members_docsize,
    ret_watch, file_res

So the default is EndNote's own soft delete: set `trash_state = 1`, exactly what
"Move References to Trash" does in the GUI. EndNote then treats the record as
trashed and its own cleanup applies. `--hard` additionally removes the satellite
rows and is only advisable with EndNote CLOSED and a backup taken.

A minimized/running EndNote caches the library in memory. A trash write was
verified to survive a subsequent EndNote write, but always confirm visually.

Usage
-----
    python endnote_dedupe.py --list                 # report duplicates only
    python endnote_dedupe.py --dry-run              # show what would be trashed
    python endnote_dedupe.py                        # trash duplicates (soft)
    python endnote_dedupe.py --keep lowest          # keep the lowest rec-number
    python endnote_dedupe.py --trash 13 15          # trash specific records
    python endnote_dedupe.py --restore 13           # untrash a record
"""

from __future__ import annotations

# --- plugin path resolution -------------------------------------------------
# This copy is vendored into the dsh-endnote plugin. Library/staging/EndNote
# locations come from `endnote_paths`, which reads the DSH_ENDNOTE_* environment
# variables the plugin sets from its config, so no path is hardcoded here.
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import endnote_paths as ep  # noqa: E402
# ---------------------------------------------------------------------------



import os


import argparse
import re
import sqlite3
import sys
from pathlib import Path

DATA = ep.LIBRARY.with_suffix(".Data")
SDB = ep.SDB
PDFS = ep.PDFS

# The schema's own AFTER DELETE triggers already clean the satellite tables:
#   refs__tag_members_AD -> tag_members (and its FTS shadow tables)
#   refs__ret_watch_AD   -> ret_watch
#   refs__refs_ord_AD    -> refs_ord
# Verified: a plain `DELETE FROM refs` leaves zero rows behind in all of them.
# `file_res` has NO delete trigger, so its row (and the PDF on disk) is the only
# thing a hard delete must handle explicitly.
NO_DELETE_TRIGGER = ["file_res"]


def _install_endnote_sql(c: sqlite3.Connection) -> None:
    """Supply the two custom SQL pieces EndNote's schema depends on.

    Updating `refs` fails in plain Python sqlite3 with
        no such collation sequence: ENCIN_zh_CN
    because of this trigger, which EndNote defines on the table:

        refs__refs_ord_AU: AFTER UPDATE ON refs
          DELETE FROM refs_ord WHERE ro_id = old.id;
          INSERT INTO refs_ord (ro_trash_state, ro_key_2, ro_key_3, ro_id)
            VALUES (new.trash_state,
                    EN_MAKE_SORT_KEY(new.author, 2, 12),
                    EN_MAKE_SORT_KEY(new.year,   3, 12), new.id);

    Two missing pieces, both handled WITHOUT reimplementing EndNote's logic:

    1. `ENCIN_zh_CN` — a collation. Only needed so the statement can be
       prepared; ordering never depends on it here.
    2. `EN_MAKE_SORT_KEY(text, style, len)` — EndNote's collation-aware sort-key
       builder (it turns an author list into a collation key such as
       'SURNAME A   SURNAME B ...'). This tool NEVER modifies author or year, so
       the correct key for each input already exists in `refs_ord`; we capture it
       beforehand and replay it.

    The replay is keyed by the *input value* the trigger passes. If a lookup ever
    misses, that means a keyed column changed — the function raises so the write
    is refused instead of silently corrupting the Author sort key.

    Verified safe: with the keys replayed, the trigger reproduces refs_ord
    byte-for-byte and propagates the trash flag, and a rollback restores
    everything.
    """
    c.create_collation("ENCIN_zh_CN", lambda a, b: (a > b) - (a < b))

    replay: dict[tuple[str, int], object] = {}
    try:
        for row in c.execute(
                "SELECT r.author, r.year, o.ro_key_2, o.ro_key_3 "
                "FROM refs r LEFT JOIN refs_ord o ON o.ro_id = r.id"):
            author, year, k2, k3 = row
            if k2 is not None:
                replay[(str(author), 2)] = k2
            if k3 is not None:
                replay[(str(year), 3)] = k3
    except sqlite3.Error:
        pass

    def en_make_sort_key(text, style, length):
        hit = replay.get((str(text), int(style)))
        if hit is not None:
            return hit
        raise sqlite3.OperationalError(
            "EN_MAKE_SORT_KEY: refusing to regenerate a sort key for an "
            "unmodified column (would need EndNote's collation-aware builder)")

    c.create_function("EN_MAKE_SORT_KEY", 3, en_make_sort_key)


def connect(mode: str = "ro") -> sqlite3.Connection:
    # One shared guard: names the path, how it was discovered, and the three
    # ways to fix it. Previously each script reimplemented this message.
    ep.require_library()
    c = sqlite3.connect(f"file:{SDB.as_posix()}?mode={mode}", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    if mode != "ro":
        _install_endnote_sql(c)
    return c


def norm_doi(v: str | None) -> str:
    if not v:
        return ""
    v = str(v).strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    return v.rstrip(".")


def norm_title(v: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (v or "").lower())[:70]


def load(include_trash: bool = False) -> list[dict]:
    c = connect()
    sql = ("SELECT id, trash_state, author, year, title, secondary_title, "
           "electronic_resource_number, abstract FROM refs")
    if not include_trash:
        sql += " WHERE COALESCE(trash_state,0)=0"
    sql += " ORDER BY id"
    rows = [dict(r) for r in c.execute(sql)]
    att = {}
    for r in c.execute("SELECT refs_id, file_path FROM file_res ORDER BY file_pos"):
        att.setdefault(r["refs_id"], []).append(r["file_path"])
    c.close()
    for r in rows:
        r["attachments"] = att.get(r["id"], [])
        r["doi_n"] = norm_doi(r["electronic_resource_number"])
        r["title_n"] = norm_title(r["title"])
    return rows


def find_duplicate_groups(refs: list[dict]) -> list[dict]:
    """Group records that share a DOI (preferred) or a normalized title."""
    groups: list[dict] = []
    by_doi: dict[str, list[dict]] = {}
    by_title: dict[str, list[dict]] = {}
    for r in refs:
        if r["doi_n"]:
            by_doi.setdefault(r["doi_n"], []).append(r)
        if r["title_n"] and len(r["title_n"]) > 25:
            by_title.setdefault(r["title_n"], []).append(r)

    used: set[int] = set()
    for key, rs in sorted(by_doi.items()):
        if len(rs) > 1:
            ids = sorted(x["id"] for x in rs)
            used.update(ids)
            groups.append({"kind": "DOI", "key": key, "refs": rs})
    for key, rs in sorted(by_title.items()):
        ids = sorted(x["id"] for x in rs)
        if len(rs) > 1 and not used.issuperset(ids):
            groups.append({"kind": "title", "key": key, "refs": rs})
    return groups


def completeness(r: dict) -> tuple:
    """Higher is better: prefer the record with an attachment and more fields."""
    score = 0
    score += 4 if r["attachments"] else 0
    score += 2 if r["doi_n"] else 0
    score += 1 if (r.get("abstract") or "").strip() else 0
    score += 1 if (r.get("author") or "").strip() else 0
    return (score, -r["id"])          # tie -> lowest rec-number wins


def show(refs: list[dict]) -> None:
    print(f"{len(refs)} active reference(s):\n")
    for r in refs:
        au = (r["author"] or "").replace("\r", "; ")
        if len(au) > 40:
            au = au[:37] + "..."
        clip = f"[{len(r['attachments'])}]" if r["attachments"] else "[ ]"
        print(f"  {clip} #{r['id']:<4} {r['year'] or '----'}  {au}")
        print(f"          {str(r['title'])[:72]}")
        if r["doi_n"]:
            print(f"          DOI: {r['doi_n']}")


def plan(groups: list[dict], keep: str) -> tuple[list[int], dict[int, str]]:
    """Decide, for the WHOLE run, which ids to keep and which to trash.

    Deciding per group is not enough, and the bug that caused this was subtle
    enough to be dangerous. A record can appear in more than one duplicate group
    (matched once by DOI, once by title). Choosing a KEEP independently inside each
    group meant a record could be shown `<- KEEP` in one and still be trashed
    because a *different* group chose to keep another record — and that group
    would then end up with NO members at all. The printed plan disagreed with what
    was executed, and the user lost a record the plan said was kept.

    The invariant this enforces is exactly that one: **every duplicate group keeps
    at least one member.** Groups are considered strongest-evidence-first (DOI
    outranks title, which outranks year, because a DOI is a far better dedup
    signal), and each group elects the best of its not-already-trashed members.
    Processing in that order means the group whose match is strongest keeps its
    elected keeper, and weaker groups adapt — but never to the point of being
    emptied. The report and the write consume the same plan, so what is printed is
    what happens.
    """
    if not groups:
        return [], {}

    def completeness_score(r: dict) -> int:
        return (10 if r["attachments"] else 0) + (5 if r.get("doi_n") else 0) \
               + min(int(r["year"] or 0), 3000) % 10

    # Strength of evidence for the match itself. A DOI match is essentially
    # certain to be a duplicate; a title is weaker; a year is weakest.
    strength = {"doi": 0, "title": 1, "year": 2}

    def elect(rs: list[dict], available: set[int]) -> dict | None:
        live = [r for r in rs if r["id"] in available]
        if not live:
            return None
        if keep == "complete":
            return max(live, key=lambda r: (completeness_score(r), -r["id"]))
        return min(live, key=lambda r: r["id"])

    trash: set[int] = set()
    reasons: dict[int, str] = {}
    order = sorted(groups, key=lambda g: (strength.get(g["kind"], 3), g["key"]))

    for g in order:
        rs = g["refs"]
        avail = {r["id"] for r in rs} - trash
        chosen = elect(rs, avail)
        if chosen is None:
            # Should not happen: a previous group may already keep every member.
            continue
        for r in rs:
            if r["id"] == chosen["id"]:
                continue
            # If EVERY remaining member is already trashed by an earlier group,
            # do not empty this one — keep its next-best instead.
            if r["id"] in trash:
                continue
            trash.add(r["id"])
            reasons[r["id"]] = g["kind"]

    return sorted(trash), reasons


def report(groups: list[dict], keep: str) -> list[int]:
    """Print duplicate groups and return the ids to trash.

    The marks come from the same global plan `set_trash` executes, so a `<- KEEP`
    is never trashed.
    """
    if not groups:
        print("no duplicates found.")
        return []
    todos, _reasons = plan(groups, keep)
    trash_set = set(todos)

    print(f"{len(groups)} duplicate group(s):\n")
    for g in groups:
        rs = g["refs"]
        print(f"  matched by {g['kind']}: {g['key'][:66]}")
        for r in rs:
            if r["id"] in trash_set:
                mark = "  -> trash"
            else:
                mark = "  <- KEEP"
            clip = f"[{len(r['attachments'])}]" if r["attachments"] else "[ ]"
            print(f"    {clip} #{r['id']:<4} {r['year'] or '----'} "
                  f"{str(r['title'])[:54]}{mark}")
            for a in r["attachments"]:
                print(f"              + {a}")
        print()
    return todos


def set_trash(ids: list[int], value: int, *, hard: bool = False,
              dry: bool = False) -> int:
    """Soft-delete by default; `hard` also removes the refs row.

    Soft (value=1) is EndNote's own "Move References to Trash": the record keeps
    its row and simply stops appearing. Verified to survive a subsequent EndNote
    write and to be skipped by the XML exporter.

    Hard removes the row outright. The schema's AFTER DELETE triggers then clean
    refs_ord/tag_members/ret_watch automatically (verified); only file_res and
    the PDF on disk need handling here.
    """
    if not ids:
        return 0
    verb = "hard delete" if (hard and value) else ("would trash" if dry else "trashing")
    print(f"{verb} {len(ids)} record(s): {ids}")
    if dry:
        return 0

    c = connect("rw")
    pending_unlink: list[Path] = []
    try:
        c.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" * len(ids))
        if hard and value:
            # Collect attachment paths to remove, but DO NOT unlink yet.
            #
            # The filesystem has no rollback: an earlier version unlinked inside
            # the transaction, so a later failure (a locked file, a trigger error)
            # rolled the DATABASE back while the PDFs stayed deleted — the record
            # survived pointing at a file that no longer existed. Doing it after
            # COMMIT makes the DB the source of truth: if the transaction aborts,
            # nothing was removed from disk.
            for (fp,) in c.execute(
                    f"SELECT file_path FROM file_res WHERE refs_id IN ({marks})", ids):
                resolved = ep.resolve_attachment(fp)
                if resolved is None:
                    # Not a path inside this library (absolute or escaping).
                    # Refuse the unlink; the row is still removed below, so the
                    # database stays consistent and nothing outside is touched.
                    print(f"  ! skipping attachment outside the library: {fp!r}",
                          file=sys.stderr)
                    continue
                if resolved.is_file():
                    pending_unlink.append(resolved)
            for t in NO_DELETE_TRIGGER:
                c.execute(f"DELETE FROM {t} WHERE refs_id IN ({marks})", ids)
            n = c.execute(f"DELETE FROM refs WHERE id IN ({marks})", ids).rowcount
        else:
            n = c.execute(f"UPDATE refs SET trash_state=? WHERE id IN ({marks})",
                          [value, *ids]).rowcount
        c.commit()
    except Exception as exc:
        c.rollback()
        print(f"! failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        c.close()

    # Post-commit: the database now agrees that these attachments are gone, so
    # removing the bytes cannot leave a dangling reference. A failure here is
    # reported but does not invalidate the committed change.
    removed = 0
    for p in pending_unlink:
        try:
            p.unlink()
            removed += 1
            try:
                if not any(p.parent.iterdir()):
                    p.parent.rmdir()
            except OSError:
                pass
        except OSError as exc:
            print(f"  ! could not remove {p}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    if hard and value:
        print(f"updated {n} record(s) (hard: row and {removed} attachment file(s) removed)")
    else:
        print(f"updated {n} record(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find/remove duplicate EndNote records.")
    ap.add_argument("--list", action="store_true", help="report only (no changes)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be trashed")
    ap.add_argument("--keep", choices=["lowest", "complete"], default="complete",
                    help="which duplicate to keep (default: most complete)")
    ap.add_argument("--trash", nargs="+", type=int, help="trash these record numbers")
    ap.add_argument("--restore", nargs="+", type=int, help="untrash these record numbers")
    ap.add_argument("--hard", action="store_true",
                    help="also delete satellite rows (backup + close EndNote first)")
    ap.add_argument("--all", action="store_true", help="list every reference, then exit")
    args = ap.parse_args(argv)

    if args.restore:
        # Validate the ids, exactly as --trash does. Without this, `--restore 999`
        # printed "updated 0 record(s)" and exited 0 — a silent no-op reported as
        # success, which for a RESTORE is the worst possible outcome: the user
        # believes the record came back and stops looking for it.
        refs_all = load(include_trash=True)
        known = {r["id"] for r in refs_all}
        bad = [i for i in args.restore if i not in known]
        if bad:
            print(f"! no such record(s): {bad}", file=sys.stderr)
            print(f"  ({len(known)} record(s) exist; "
                  f"use --all to list them)", file=sys.stderr)
            return 1
        # Report records that were not actually trashed, so "restored" is never
        # claimed for something that was already active.
        already = [r["id"] for r in refs_all
                   if r["id"] in args.restore and not r["trash_state"]]
        if already:
            print(f"  note: {already} were not in the trash; nothing to restore "
                  f"for them")
        return set_trash(args.restore, 0, dry=args.dry_run)

    if args.trash:
        refs = load(include_trash=True)
        known = {r["id"] for r in refs}
        bad = [i for i in args.trash if i not in known]
        if bad:
            print(f"! no such record(s): {bad}", file=sys.stderr)
            return 1
        for r in refs:
            if r["id"] in args.trash:
                clip = f"[{len(r['attachments'])}]" if r["attachments"] else "[ ]"
                print(f"  {clip} #{r['id']} {str(r['title'])[:66]}")
        return set_trash(args.trash, 1, hard=args.hard, dry=args.dry_run)

    refs = load()
    if args.all:
        show(refs)
        return 0

    groups = find_duplicate_groups(refs)
    print(f"{len(refs)} active reference(s), {len(groups)} duplicate group(s)\n")
    todos = report(groups, args.keep)

    if args.list:
        return 0
    if not todos:
        return 0
    rc = set_trash(todos, 1, hard=args.hard, dry=args.dry_run)
    if not args.dry_run and rc == 0:
        print("\ntrashed. Check EndNote, then refresh the search index:")
        if ep.REFRESH:
            print(f"  powershell -NoProfile -File {ep.REFRESH}")
        else:
            print("  the endnote_refresh tool, or: endnote-mcp index --full")
        print("If a record should NOT have been trashed:  "
              f"python {Path(__file__).name} --restore <id>")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
