#!/usr/bin/env python3
"""Read and edit EndNote groups (custom groups: add / remove / create).

Storage model (decoded from the live library, see --probe)
----------------------------------------------------------
Groups live in one table in the library's working copy:

    groups(group_id INTEGER PRIMARY KEY, recs_stamp INTEGER, spec BLOB, members BLOB)

* `spec` is XML: uuid, <name>, created/modified stamps, and a <rules> element.
  The rule tells the group TYPE apart, which decides whether membership is ours
  to edit:
      TYPE;3  custom/manual group -> members is an explicit id list (EDITABLE)
      TYPE;6  online-search group -> derived from a database connection (NOT ours)
  A rule-based smart group is likewise derived and never written.
* `members` is a 4-byte constant prefix `00 00 00 02` followed by
  LITTLE-ENDIAN uint32 record ids. A single 0 word means "no members".
  (Big-endian was tried first and produced implausible ids like 50331648; the
  little-endian reading yields real ids in the library's range, and the same
  decode is stable across every backup on disk.)

Why ENDIANNESS was worth proving: getting it wrong silently corrupts every
group's membership. It was disambiguated by decoding all five groups and
checking which interpretation yields ids that actually exist in `refs`.

Usage
-----
    python endnote_groups.py --list
    python endnote_groups.py --show "Reading list"
    python endnote_groups.py --create "Reading list"
    python endnote_groups.py --add "Reading list" --refs 10,11,12
    python endnote_groups.py --remove "Reading list" --refs 11
    python endnote_groups.py --create "X" --refs 2,4       # create + fill
    python endnote_groups.py --probe                        # dump raw storage
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
import struct
import sys
import time
import uuid as uuidlib
from pathlib import Path


MEMBER_PREFIX = b"\x00\x00\x00\x02"
CUSTOM_RULE = "TYPE;3"


# ------------------------------------------------------------------ storage

def connect(mode: str = "ro") -> sqlite3.Connection:
    ep.require_library()
    c = sqlite3.connect(f"file:{ep.SDB.as_posix()}?mode={mode}", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


class MemberDecodeError(Exception):
    """The members blob is not in the format this tool understands."""


class AmbiguousGroupError(Exception):
    """A partial name matched more than one group."""


def decode_members(blob, *, strict: bool = False) -> list[int]:
    """Little-endian uint32 ids after the constant 4-byte prefix.

    `strict=True` raises MemberDecodeError instead of returning [] for a blob that
    does not match the documented format. **Any caller that WRITES membership must
    use strict mode**, because a silent [] is indistinguishable from "this group is
    empty" — and `encode_members` would then rewrite the blob from that empty list,
    destroying every member. Verified: a 7-byte blob on a group whose real members
    were [5,12,16,21,27] decoded as empty, and an ordinary `--add --refs 2`
    printed "0 -> 1 member(s)" and permanently lost all five, exiting 0.

    Read paths keep the lenient behaviour, so a group we cannot parse is still
    listed and reported rather than crashing the whole listing.
    """
    if blob is None:
        return []
    if isinstance(blob, str):
        blob = blob.encode("latin-1", "replace")

    def bad(reason: str) -> list[int]:
        if strict:
            raise MemberDecodeError(
                f"{reason} (got {len(blob)} bytes: {blob[:12].hex()}). "
                f"Refusing to rewrite this group: the real members cannot be "
                f"recovered from a blob in an unknown format, and writing would "
                f"silently replace them."
            )
        return []

    if len(blob) < 4:
        return bad("members blob is shorter than its 4-byte prefix")
    # The prefix is a version/constant marker. Validating it is the difference
    # between "I parsed this" and "I guessed".
    if blob[:4] != MEMBER_PREFIX:
        return bad(f"unexpected members prefix {blob[:4].hex()}, "
                   f"expected {MEMBER_PREFIX.hex()}")
    if (len(blob) - 4) % 4 != 0:
        return bad("members payload is not a whole number of 4-byte ids")
    ids = list(struct.unpack(f"<{(len(blob) - 4) // 4}I", blob[4:]))
    return [i for i in ids if i != 0]        # a lone 0 word means "empty"


def encode_members(ids: list[int]) -> bytes:
    """Inverse of decode_members. Empty -> prefix + a single 0 word."""
    ordered = sorted({int(i) for i in ids})
    if not ordered:
        ordered = [0]
    return MEMBER_PREFIX + struct.pack(f"<{len(ordered)}I", *ordered)


def xml_escape(text: str) -> str:
    """Escape a value for inclusion in the group spec XML.

    The name is interpolated into XML, so an unescaped `&` or `<` produces a spec
    EndNote cannot parse — verified: `--create "R&D <v2>"` made a group that was
    then unfindable, and the refs were silently not added, with exit 0. Escaping
    also matters for the round trip: `parse_spec` reads `<name>...</name>` back, so
    an unescaped value would not survive a write/read cycle.
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&apos;"))


def xml_unescape(text: str) -> str:
    """Inverse of xml_escape, for values read back out of the spec.

    `&amp;` is replaced LAST so a literal `&amp;lt;` in the original does not
    become `<`.
    """
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&apos;", "'").replace("&amp;", "&"))


def parse_spec(blob) -> dict:
    """Pull the fields we care about out of the group XML.

    The name is unescaped, so a group created with `&` or `<` in it round-trips to
    the same string the user typed. Without this, `--show "R&D"` would fail even
    though creation had succeeded.
    """
    if blob is None:
        return {}
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
    name = re.search(r"<name>([^<]*)</name>", text)
    gid = re.search(r"<id>([^<]+)</id>", text)
    rules = re.findall(r"<rule>([^<]*)</rule>", text)
    created = re.search(r"<created[^>]*>(\d+)</created>", text)
    return {
        "name": xml_unescape(name.group(1)) if name else "",
        "uuid": gid.group(1) if gid else "",
        "rules": rules,
        "created": created.group(1) if created else "",
        "raw": text,
    }


def group_kind(rules: list[str]) -> str:
    if not rules:
        return "custom"
    if any(r.startswith("TYPE;3") for r in rules):
        return "custom"
    if any(r.startswith("TYPE;6") for r in rules):
        return "online-search"
    return "smart"


def _article(word: str) -> str:
    """'a' or 'an' for a kind name, so messages read 'an online-search group'."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def load_groups(c: sqlite3.Connection) -> list[dict]:
    """Every group, with membership decoded leniently (for listing/reporting).

    `members_ok` records whether the blob parsed cleanly. A caller that intends to
    WRITE membership must check it (or call write_members, which enforces it), so
    an unparseable blob can never be silently reinterpreted as an empty group.
    """
    out = []
    for r in c.execute("SELECT group_id, recs_stamp, spec, members FROM groups ORDER BY group_id"):
        spec = parse_spec(r["spec"])
        try:
            ids = decode_members(r["members"], strict=True)
            ok = True
        except MemberDecodeError:
            ids, ok = [], False
        out.append({
            "group_id": r["group_id"],
            "recs_stamp": r["recs_stamp"],
            "name": spec.get("name", ""),
            "uuid": spec.get("uuid", ""),
            "rules": spec.get("rules", []),
            "created": spec.get("created", ""),
            "kind": group_kind(spec.get("rules", [])),
            "ids": ids,
            "members_ok": ok,
            "spec_raw": spec.get("raw", ""),
        })
    return out


def find_group(c: sqlite3.Connection, name: str, *, fuzzy: bool = True) -> dict | None:
    """Find a group by exact name, falling back to a unique substring match.

    The substring fallback exists because a user may type part of a long name, but
    it is only safe when it identifies exactly ONE group. An earlier version
    returned the first substring hit, so `--add Phag` silently edited "Phage
    delivery" — the caller asked for something that did not exist and the tool
    modified a different group instead, with no warning.

    Write paths call this with `fuzzy=False` so an approximate name can never
    cause an edit; read/display paths may keep the convenience.
    """
    needle = name.strip().lower()
    groups = load_groups(c)
    for g in groups:
        if g["name"].lower() == needle:
            return g
    if not fuzzy:
        return None
    matches = [g for g in groups if needle and needle in g["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Ambiguous: refuse rather than pick one arbitrarily.
        names = ", ".join(f'"{g["name"]}"' for g in matches[:5])
        raise AmbiguousGroupError(
            f"{name!r} matches {len(matches)} groups ({names}). "
            f"Use an exact name."
        )
    return None


#: Words that carry no topic information. Without this, "delivery" or "systems"
#: would match almost any group name and produce confident nonsense.
_STOP = {
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "with", "by",
    "from", "at", "as", "is", "are", "be", "using", "use", "used", "based",
    "new", "review", "study", "analysis", "system", "systems", "method",
    "methods", "approach", "approaches", "effect", "effects", "role", "roles",
    "via", "into", "their", "its", "this", "that", "these", "those",
}


def _topic_words(text: str) -> set[str]:
    """Lowercase content words, long enough to be meaningful."""
    return {w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", (text or "").lower())
            if w not in _STOP}


def suggest_group(c: sqlite3.Connection, *, title: str = "", abstract: str = "",
                  keywords: list[str] | None = None, journal: str = "",
                  author: str = "") -> dict | None:
    """Pick the custom group whose existing members best match this paper.

    Why this is evidence-based rather than name-based: a group NAME alone is a
    weak signal ("Chapter drafts" vs a paper about phage encapsulation — is that
    a match?). But the members EndNote already holds are ground truth: if a
    group's existing records read like this paper, that group is where the user
    has been putting such papers. So the score is built from the paper's own
    text overlapping the TITLES/KEYWORDS of each group's members.

    Only CUSTOM groups are considered — membership of a derived group (online
    search, smart rule) is computed by EndNote and cannot be written, so
    suggesting one would produce a group edit that always fails.

    Returns {"group": <dict>, "score": float, "reason": str, "runner_up": ...}
    or None when nothing clears the bar. Refusing is deliberate: silently filing
    a paper into the wrong group is worse than not filing it, because the user
    will not notice until they go looking for it.
    """
    text = " ".join(filter(None, [title, abstract, journal, " ".join(keywords or [])]))
    words = _topic_words(text)
    if not words:
        return None

    groups = [g for g in load_groups(c) if g["kind"] == "custom"]
    if not groups:
        return None

    # Member metadata, fetched once for every id we might score.
    ids = sorted({i for g in groups for i in g["ids"]})
    member_text: dict[int, str] = {}
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start:chunk_start + 400]
        if not chunk:
            continue
        q = ("SELECT id, COALESCE(title,'') || ' ' || COALESCE(keywords,'') "
             f"FROM refs WHERE id IN ({','.join('?' * len(chunk))})")
        for rid, blob in c.execute(q, chunk):
            member_text[int(rid)] = (blob or "").lower()

    scored = []
    for g in groups:
        members = [member_text.get(int(i), "") for i in g["ids"]]
        members = [m for m in members if m]
        if not members:
            # An empty group is a legitimate target (the user made it for this),
            # but it cannot be scored, so it is offered as a weak fallback only.
            scored.append((0.0, g, 0, 0))
            continue
        # Score = best single-member overlap, not the average: one strongly
        # on-topic neighbour is far more informative than many unrelated ones,
        # and averaging would bury a new topic in a broad group.
        best_hits, best_size = 0, 1
        for m in members:
            mw = _topic_words(m)
            hits = len(words & mw)
            if hits > best_hits:
                best_hits, best_size = hits, max(1, len(mw))
        # Normalise by the paper's own vocabulary so the score is a fraction of
        # "how much of this paper is already represented in that group".
        score = best_hits / max(1, min(len(words), best_size))
        scored.append((score, g, best_hits, len(members)))

    scored.sort(key=lambda t: (-t[0], -t[2], -t[3], t[1]["group_id"]))
    top_score, top_group, top_hits, top_members = scored[0]
    runner = scored[1] if len(scored) > 1 else None

    # Require real evidence.
    if top_hits < 2 or top_score < 0.20:
        return None

    # A near-tie is reported, not guessed. Two groups can genuinely both fit —
    # especially when a paper is already a member of one of them, which makes both
    # score identically. Naming the candidates is far more useful than silence:
    # the caller can pick, or ask which was meant.
    if runner and runner[0] >= top_score * 0.9 and runner[1]["group_id"] != top_group["group_id"]:
        return {
            "group": None,
            "score": top_score,
            "hits": top_hits,
            "ambiguous": [
                {"name": top_group["name"], "score": top_score},
                {"name": runner[1]["name"], "score": runner[0]},
            ],
            "reason": (f"two groups fit about equally: \"{top_group['name']}\" "
                       f"({top_score:.2f}) and \"{runner[1]['name']}\" ({runner[0]:.2f})"),
        }

    reason = (f"{top_hits} of this paper's topic words already appear in "
              f"\"{top_group['name']}\" ({top_members} member(s))")
    return {
        "group": top_group,
        "score": top_score,
        "hits": top_hits,
        "reason": reason,
        "runner_up": ({"name": runner[1]["name"], "score": runner[0]} if runner else None),
    }


def ref_titles(c: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out = {}
    for r in c.execute(
            f"SELECT id, year, author, title, trash_state FROM refs WHERE id IN ({marks})", ids):
        out[r["id"]] = (r["year"] or "----", (r["author"] or "").split("\r")[0][:28],
                        (r["title"] or "")[:64], r["trash_state"])
    return out


# ------------------------------------------------------------------ display

def cmd_list(c: sqlite3.Connection, verbose: bool) -> int:
    groups = load_groups(c)
    custom = [g for g in groups if g["kind"] == "custom"]
    other = [g for g in groups if g["kind"] != "custom"]
    print(f"{len(groups)} group(s): {len(custom)} custom, {len(other)} other\n")

    for g in custom:
        titles = ref_titles(c, g["ids"])
        live = [i for i in g["ids"] if i in titles and not titles[i][3]]
        print(f"  [custom] \"{g['name']}\"  ({len(g['ids'])} member(s), "
              f"{len(live)} active)  group_id={g['group_id']}")
        if verbose:
            for i in g["ids"]:
                t = titles.get(i)
                if t:
                    flag = " [TRASH]" if t[3] else ""
                    print(f"      #{i:<4}{flag} {t[0]}  {t[1]:28} {t[2]}")
                else:
                    print(f"      #{i:<4} (record no longer in the library)")
    if other:
        print("\n  -- not editable (derived from a search or connection) --")
        for g in other:
            print(f"  [{g['kind']}] \"{g['name']}\"  group_id={g['group_id']}  "
                  f"rules={g['rules']}")
    return 0


def cmd_show(c: sqlite3.Connection, name: str) -> int:
    g = find_group(c, name)
    if not g:
        print(f"! no group matching {name!r}", file=sys.stderr)
        return 1
    titles = ref_titles(c, g["ids"])
    live = [i for i in g["ids"] if i in titles and not titles[i][3]]
    print(f"group    : \"{g['name']}\"")
    print(f"type     : {g['kind']}  (rules={g['rules']})")
    print(f"group_id : {g['group_id']}   uuid={g['uuid']}")
    print(f"members  : {len(g['ids'])} ({len(live)} active)")
    if g["kind"] != "custom":
        print("\n  ! this group is derived from a search/connection; membership is")
        print("    computed by EndNote and cannot be edited by this tool.")
    print()
    for i in g["ids"]:
        t = titles.get(i)
        if t:
            flag = " [TRASH]" if t[3] else ""
            print(f"  #{i:<4}{flag} {t[0]}  {t[1]:28} {t[2]}")
        else:
            print(f"  #{i:<4} (record no longer in the library)")
    if not g["ids"]:
        print("  (empty)")
    return 0


# ------------------------------------------------------------------ writes

def write_members(c: sqlite3.Connection, g: dict, ids: list[int],
                  *, dry: bool = False) -> int:
    # Refuse to overwrite a group whose current membership could not be decoded.
    # `g["ids"]` would be [] in that case, so writing `ids` would silently replace
    # the real members with only what this call passes — verified data loss, and
    # the reason decode_members has a strict mode at all. Checked here, in the one
    # function that writes, so no caller can forget it.
    if not g.get("members_ok", True):
        print(f"! refusing to modify \"{g.get('name', '?')}\": its membership blob "
              f"is not in a format this tool understands.", file=sys.stderr)
        print(f"  Rewriting it would replace the real members with only the ones "
              f"passed here, so nothing was changed.", file=sys.stderr)
        print(f"  Inspect the group with:  endnote_groups.py --probe", file=sys.stderr)
        return 1
    if len(g.get("ids", [])) != len(set(g.get("ids", []))):
        print(f"! refusing to modify \"{g.get('name', '?')}\": its decoded membership "
              f"contains duplicates, which means the decode is wrong.", file=sys.stderr)
        return 1

    blob = encode_members(ids)
    print(f"  \"{g['name']}\": {len(g['ids'])} -> {len(set(ids))} member(s)")
    if dry:
        print("  (dry run — nothing written)")
        return 0
    try:
        c.execute("BEGIN IMMEDIATE")
        # recs_stamp appears to be a change marker; bump it so EndNote notices.
        c.execute("UPDATE groups SET members=?, recs_stamp=? WHERE group_id=?",
                  (blob, int(time.time()) & 0xFFFFFFFF, g["group_id"]))
        c.commit()
    except Exception as exc:
        c.rollback()
        print(f"! failed to update the group: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    after = decode_members(blob)
    print(f"  written; group now holds {len(after)} member(s): {after}")
    return 0


def create_group(c: sqlite3.Connection, name: str, *, dry: bool = False) -> int:
    if not name.strip():
        print("! a group name is required", file=sys.stderr)
        return 1
    groups = load_groups(c)
    if any(g["name"].lower() == name.strip().lower() for g in groups):
        print(f"! a group named \"{name}\" already exists", file=sys.stderr)
        return 1
    new_id = (max((g["group_id"] for g in groups), default=0) + 1)
    now = int(time.time())
    uid = str(uuidlib.uuid4()).upper()
    spec = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<group version="1">'
        f'<ids><id>{uid}</id><name>{xml_escape(name)}</name></ids>'
        f'<times><created format="UTC">{now}</created>'
        f'<modified format="UTC">{now}</modified></times>'
        f'<rules><rule>{CUSTOM_RULE}</rule></rules>'
        '</group>'
    )
    print(f"{'would create' if dry else 'creating'} group \"{name}\"")
    print(f"  group_id : {new_id}")
    print(f"  uuid     : {uid}")
    print(f"  rule     : {CUSTOM_RULE} (custom/manual)")
    if dry:
        print("  (dry run — nothing written)")
        return 0
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO groups (group_id, recs_stamp, spec, members) VALUES (?,?,?,?)",
                  (new_id, now & 0xFFFFFFFF, spec.encode("utf-8"), encode_members([])))
        c.commit()
    except Exception as exc:
        c.rollback()
        print(f"! failed to create the group: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"  created as group_id={new_id}")
    return 0


def parse_refs(text: str) -> list[int]:
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        if part.strip().isdigit():
            out.append(int(part))
    return out


def validate_refs(c: sqlite3.Connection, ids: list[int]) -> list[int]:
    """Keep only ids that exist. Report the ones that do not."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    known = {r[0] for r in c.execute(f"SELECT id FROM refs WHERE id IN ({marks})", ids)}
    missing = [i for i in ids if i not in known]
    if missing:
        print(f"  ! ignoring unknown record(s): {missing}")
    return [i for i in ids if i in known]


# ------------------------------------------------------------------ probe

def cmd_probe(c: sqlite3.Connection) -> int:
    print("=== raw group storage (for verifying the decode) ===")
    for r in c.execute("SELECT group_id, recs_stamp, spec, members FROM groups ORDER BY group_id"):
        spec = parse_spec(r["spec"])
        blob = r["members"]
        if isinstance(blob, str):
            blob = blob.encode("latin-1", "replace")
        rest = blob[4:]
        be = list(struct.unpack(f">{len(rest)//4}I", rest)) if rest else []
        print(f"\n  group_id={r['group_id']} recs_stamp={r['recs_stamp']}")
        print(f"    name      : {spec.get('name')!r}")
        print(f"    rules     : {spec.get('rules')}")
        print(f"    members hx: {blob.hex()}")
        print(f"    prefix    : {blob[:4].hex()} ({'expected' if blob[:4] == MEMBER_PREFIX else 'UNEXPECTED'})")
        print(f"    LE decode : {decode_members(blob)}   <- used")
        print(f"    BE decode : {be}   <- rejected (implausible ids)")
    print("\n=== active records, for cross-checking decoded ids ===")
    live = [r[0] for r in c.execute("SELECT id FROM refs WHERE COALESCE(trash_state,0)=0 ORDER BY id")]
    print(f"  {live}")
    return 0


# ------------------------------------------------------------------ driver

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read and edit EndNote groups.")
    ap.add_argument("--list", action="store_true", help="list groups (with members)")
    ap.add_argument("--show", metavar="NAME", help="show one group and its members")
    ap.add_argument("--create", metavar="NAME", help="create a new custom group")
    ap.add_argument("--add", metavar="NAME", help="add records to a group")
    ap.add_argument("--remove", metavar="NAME", help="remove records from a group")
    ap.add_argument("--refs", metavar="IDS", help="comma-separated record numbers, e.g. 2,4,5")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--probe", action="store_true", help="dump raw storage and the decode")
    args = ap.parse_args(argv)

    if args.probe:
        c = connect()
        rc = cmd_probe(c)
        c.close()
        return rc

    if args.list:
        c = connect()
        rc = cmd_list(c, verbose=True)
        c.close()
        return rc

    if args.show:
        c = connect()
        rc = cmd_show(c, args.show)
        c.close()
        return rc

    # ---- mutating paths ----
    c = connect("rw")
    try:
        if args.create:
            rc = create_group(c, args.create, dry=args.dry_run)
            if rc != 0:
                return rc
            if args.refs:
                g = find_group(c, args.create, fuzzy=False)
                if g:
                    rc = write_members(c, g, validate_refs(c, parse_refs(args.refs)),
                                       dry=args.dry_run)
            return rc

        if args.add or args.remove:
            name = args.add or args.remove
            # fuzzy=False: an approximate name must never cause an edit.
            g = find_group(c, name, fuzzy=False)
            if not g:
                print(f"! no group matching {name!r}. Use --list to see them.", file=sys.stderr)
                return 1
            if g["kind"] != "custom":
                print(f"! \"{g['name']}\" is {_article(g['kind'])} {g['kind']} group — its membership is computed",
                      file=sys.stderr)
                print("  by EndNote from a search/connection, so it cannot be edited here.",
                      file=sys.stderr)
                return 1
            if not args.refs:
                print("! --refs is required with --add/--remove", file=sys.stderr)
                return 2
            wanted = validate_refs(c, parse_refs(args.refs))
            current = set(g["ids"])
            if args.add:
                new = sorted(current | set(wanted))
            else:
                new = sorted(current - set(wanted))
            print(f"group    : \"{g['name']}\" (group_id={g['group_id']})")
            print(f"  current: {sorted(current)}")
            print(f"  change : {'add' if args.add else 'remove'} {wanted}")
            return write_members(c, g, new, dry=args.dry_run)

        ap.print_help()
        return 2
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
