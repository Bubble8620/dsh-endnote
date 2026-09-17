#!/usr/bin/env python3
"""Attach a PDF to an EXISTING EndNote reference.

Why this needs its own mechanism
--------------------------------
Attaching via EndNote's importer is ruled out: re-importing a DOI creates a
DUPLICATE record (verified - refs #12/#13 share a DOI after one re-import).

The route that works: write a `file_res` row into the library's unlocked
working copy `<lib>.Data/sdb/sdb.eni` and drop the PDF into `<lib>.Data/PDF/`.
Verified end-to-end:
  * sdb.eni is WRITABLE while EndNote runs (.enl is not)
  * the row SURVIVES a subsequent EndNote write (forced an import, row intact)
  * EndNote's GUI then shows the attachment (user-confirmed)

Usage
-----
    python endnote_attach.py --list
    python endnote_attach.py 10 C:\\path\\to\\paper.pdf
    python endnote_attach.py --doi 10.3390/v13061131 paper.pdf
    python endnote_attach.py 10 paper.pdf --dry-run
    python endnote_attach.py --remove 10 paper.pdf
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
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DATA = ep.LIBRARY.with_suffix(".Data")
SDB = ep.SDB
PDFS = ep.PDFS

FILE_TYPE_PDF = 1


# --------------------------------------------------------------- library io

def connect(mode: str = "ro") -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{SDB.as_posix()}?mode={mode}", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def list_refs() -> list[dict]:
    c = connect()
    rows = [dict(r) for r in c.execute("""
        SELECT id, trash_state, year, author, title, secondary_title,
               electronic_resource_number
        FROM refs ORDER BY id
    """)]
    att = {}
    for r in c.execute("SELECT refs_id, file_path, file_pos FROM file_res"):
        att.setdefault(r["refs_id"], []).append(r["file_path"])
    c.close()
    for r in rows:
        r["attachments"] = att.get(r["id"], [])
    return rows


def find_ref(number: int | None, doi: str | None) -> dict | None:
    refs = list_refs()
    if number is not None:
        for r in refs:
            if r["id"] == number:
                return r
        return None
    if doi:
        needle = doi.strip().lower()
        for r in refs:
            ern = str(r.get("electronic_resource_number") or "").lower()
            if needle in ern or ern.endswith(needle):
                return r
    return None


def existing_folders() -> set[str]:
    return {d.name for d in PDFS.iterdir() if d.is_dir()} if PDFS.is_dir() else set()


def new_folder_name() -> str:
    """A fresh 10-digit folder name in the style EndNote itself uses.

    EndNote's own folder names are random 10-digit numbers. The scheme is not
    derivable (checked against md5/sha1 of the filename, path, and refs_id), so
    we just mint a unique one. Collisions are avoided against the live set.
    """
    import secrets
    taken = existing_folders()
    for _ in range(64):
        cand = "".join(str(secrets.randbelow(10)) for _ in range(10))
        if cand not in taken and not (PDFS / cand).exists():
            return cand
    raise RuntimeError("could not mint a unique attachment folder name")


def _norm(name: str) -> str:
    """Normalize a filename for comparison.

    EndNote RENAMES files while importing (it truncates long names), so an
    exact-name check misses the attachment it just made. Compare on a squashed
    key instead: lowercase, non-alphanumerics dropped.
    """
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def next_file_pos(ref_id: int) -> int:
    """The next free `file_res.file_pos` for a reference: max(pos) + 1.

    There is a UNIQUE index on (refs_id, file_pos). Using `len(attachments)` as
    the position broke the moment a record had an attachment REMOVED — two files,
    delete one, len() is 1, but position 1 is still taken — so the next attach
    raised IntegrityError and the record could never gain a second file again.
    MAX+1 cannot collide, whatever the gaps.
    """
    c = connect("ro")
    try:
        r = c.execute("SELECT MAX(file_pos) FROM file_res WHERE refs_id=?",
                      (ref_id,)).fetchone()
    finally:
        c.close()
    return (int(r[0]) + 1) if r and r[0] is not None else 0


def find_existing(ref: dict, pdf: Path) -> str | None:
    """Return the existing attachment that already IS this file, if any.

    Matches on normalized name, and also on size when the name differs — a
    renamed duplicate is still a duplicate.
    """
    want = _norm(pdf.name)
    try:
        size = pdf.stat().st_size
    except OSError:
        size = -1
    for a in ref["attachments"]:
        got = Path(a)
        if _norm(got.name) == want:
            return a
        # Same stem (EndNote truncates) and same byte size => same document.
        if size > 0:
            full = PDFS / a
            try:
                if full.is_file() and full.stat().st_size == size:
                    stem_a = _norm(got.stem)[:40]
                    stem_b = _norm(pdf.stem)[:40]
                    if stem_a and stem_b and (stem_a.startswith(stem_b[:20])
                                              or stem_b.startswith(stem_a[:20])):
                        return a
            except OSError:
                pass
    return None


# ------------------------------------------------------------------ actions

def attach(ref: dict, pdf: Path, *, dry: bool = False) -> int:
    ep.require_library()
    if not pdf.is_file():
        print(f"! PDF not found: {pdf}", file=sys.stderr)
        return 1
    if pdf.suffix.lower() != ".pdf":
        print(f"! not a .pdf file: {pdf.name}", file=sys.stderr)
        return 1

    head = pdf.open("rb").read(5)
    if head[:4] != b"%PDF":
        print(f"! {pdf.name} does not look like a PDF (starts {head!r})", file=sys.stderr)
        return 1

    # Refuse to attach a file this record already has (EndNote renames on
    # import, so compare normalized names and byte sizes — see find_existing).
    already = find_existing(ref, pdf)
    if already:
        print(f"! ref#{ref['id']} already has this PDF:")
        print(f"    {already}")
        print("  nothing to do. (use --remove to detach it first)")
        return 0

    folder = new_folder_name()
    rel = f"{folder}/{pdf.name}"
    dest = PDFS / folder / pdf.name

    print(f"reference : #{ref['id']}  {str(ref['title'])[:66]}")
    print(f"current   : {len(ref['attachments'])} attachment(s)")
    for a in ref["attachments"]:
        print(f"              {a}")
    print(f"adding    : {pdf.name}  ({pdf.stat().st_size:,} bytes)")
    print(f"dest      : {dest}")

    if dry:
        print("\n(dry run — nothing copied, no row written)")
        return 0

    # 1. copy the file into the library's attachment tree
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, dest)

    # 2. write the file_res row
    #
    # `file_pos` must not collide: there is a UNIQUE index on
    # (refs_id, file_pos). Using `len(ref["attachments"])` broke as soon as a
    # record had an attachment REMOVED — two files, delete one, len() is 1, but
    # position 1 is still taken — so the next attach raised IntegrityError and the
    # record could never receive a second file again. MAX+1 is collision-free
    # regardless of gaps.
    pos = next_file_pos(ref["id"])
    c = connect("rw")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO file_res (refs_id, file_path, file_type, file_pos) VALUES (?,?,?,?)",
            (ref["id"], rel, FILE_TYPE_PDF, pos))
        c.commit()
    except Exception as exc:
        c.rollback()
        c.close()
        shutil.rmtree(dest.parent, ignore_errors=True)
        print(f"! failed to write the file_res row: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("  (the copied file was rolled back)")
        return 1
    finally:
        try:
            c.close()
        except Exception:
            pass

    print(f"\nattached. EndNote may need a moment; if the paperclip does not appear,")
    print("click another reference and back, or restart EndNote.")

    verify = find_ref(ref["id"], None)
    if verify and rel in verify["attachments"]:
        print(f"verified: ref#{ref['id']} now has {len(verify['attachments'])} attachment(s)")
    return 0


def remove(ref: dict, name: str, *, dry: bool = False) -> int:
    match = [a for a in ref["attachments"] if Path(a).name.lower() == name.lower()]
    if not match:
        print(f"! ref#{ref['id']} has no attachment named {name}")
        return 1
    rel = match[0]
    # Containment-checked: a linked attachment may store an ABSOLUTE path, and
    # `PDFS / <absolute>` rebases to that path — so an unchecked unlink could
    # delete a file with no relation to the library. resolve_attachment() returns
    # None for anything outside the PDF tree, including `..` escapes.
    path = ep.resolve_attachment(rel)
    print(f"would remove: ref#{ref['id']}  {rel}")
    if path is None:
        print(f"  ! {rel!r} is not inside the library PDF tree; the record entry "
              f"will be removed but no file will be deleted.")
    if dry:
        print("(dry run — nothing changed)")
        return 0

    c = connect("rw")
    # BEGIN IMMEDIATE covers the row deletion only; `c` is closed in `finally`
    # below, so no transaction is left open on an error path.
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM file_res WHERE refs_id=? AND file_path=?",
                  (ref["id"], rel))
        c.commit()
    except Exception as exc:
        c.rollback()
        print(f"! delete failed: {exc}", file=sys.stderr)
        return 1
    finally:
        c.close()

    # Filesystem work after the commit, so a failure here cannot leave the
    # database rolled back while the file is already gone.
    if path is not None and path.is_file():
        parent = path.parent
        try:
            path.unlink()
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        except OSError as exc:
            print(f"  ! row removed but the file could not be deleted: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"removed {rel}")
    return 0


def show_list() -> int:
    refs = [r for r in list_refs() if not r["trash_state"]]
    print(f"{len(refs)} active reference(s):\n")
    for r in refs:
        au = (r["author"] or "").replace("\r", "; ")
        if len(au) > 44:
            au = au[:41] + "..."
        n = len(r["attachments"])
        paperclip = f"[{n}]" if n else "[ ]"
        print(f"  {paperclip} #{r['id']:<4} {r['year'] or '----'}  {au}")
        print(f"          {str(r['title'])[:76]}")
        for a in r["attachments"]:
            print(f"          + {a}")
    print("\n[tally] = has attachments")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Attach a PDF to an existing EndNote reference.")
    ap.add_argument("ref", nargs="?", help="reference number (rec-number), e.g. 10")
    ap.add_argument("pdf", nargs="?", help="path to the PDF file")
    ap.add_argument("--doi", help="select the reference by DOI instead of number")
    ap.add_argument("--list", action="store_true", help="list references and attachments")
    ap.add_argument("--remove", metavar="REF", help="remove an attachment (with pdf name)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    args = ap.parse_args(argv)

    # Guard at the single dispatch point, not only inside attach(). Every
    # read path (--list, --remove, a bare ref/doi) funnels through
    # show_list() -> find_ref() -> list_refs() -> connect(), which never
    # passed through attach()'s guard -- so the most likely first command
    # produced a raw sqlite traceback instead of this message.
    ep.require_library()

    if args.list:
        return show_list()

    if args.remove:
        # NOTE: the attachment name may arrive as the positional `pdf`, because
        # argparse has already consumed one positional for `ref`. Accept both.
        target = args.pdf or args.ref
        name_arg = None
        if str(args.remove).isdigit():
            ref = find_ref(int(args.remove), None)
        else:
            ref = find_ref(None, args.remove)
        if not ref:
            print(f"! no reference found for {args.remove}", file=sys.stderr)
            return 1
        if not target:
            print("! give the attachment filename to remove", file=sys.stderr)
            return 2
        # If `ref` was consumed by the path, the remaining token is the name.
        name_arg = Path(target).name
        return remove(ref, name_arg, dry=args.dry_run)

    if not args.pdf:
        ap.print_help()
        return 2

    number = int(args.ref) if args.ref and str(args.ref).isdigit() else None
    ref = find_ref(number, args.doi or (args.ref if not number else None))
    if not ref:
        print(f"! no reference found for {args.ref or args.doi}", file=sys.stderr)
        print("\nUse --list to see the available references.")
        return 1

    return attach(ref, Path(args.pdf), dry=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
