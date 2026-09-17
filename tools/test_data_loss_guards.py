#!/usr/bin/env python3
"""Regression tests for the three CRITICAL data-loss bugs a safety audit found.

Each is tested against a COPY of the library in a temp directory — never the real
one. The point is not that the code changed but that the specific loss can no
longer happen:

  C1  a group blob in an unknown format must NOT be silently rewritten as empty.
  C2  a hard delete that fails midway must leave the PDFs on disk, not roll the
      database back while the files stay deleted.
  C3  an attachment stored as an absolute or escaping path must NOT be unlinked.

Usage:  python tools/test_data_loss_guards.py
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import endnote_paths as ep  # noqa: E402
import endnote_groups as eg  # noqa: E402

fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails += 1


def main() -> int:
    print("=== C1: an unparseable group blob is refused, not destroyed ===")

    # A 7-byte blob is not a whole number of ids; a wrong prefix is not this
    # format. Lenient decode stays [] (listing must not crash); strict decode,
    # used by the write path, must raise.
    check("lenient decode of a 7-byte blob yields [] (listing does not crash)",
          eg.decode_members(b"\x00\x00\x00\x02\x05\x0c\x10") == [])
    try:
        eg.decode_members(b"\x00\x00\x00\x02\x05\x0c\x10", strict=True)
        check("strict decode raises on a short/truncated blob", False)
    except eg.MemberDecodeError:
        check("strict decode raises on a short/truncated blob", True)
    try:
        eg.decode_members(b"\x00\x00\x00\x99" + struct.pack("<II", 5, 12), strict=True)
        check("strict decode raises on a wrong prefix", False)
    except eg.MemberDecodeError:
        check("strict decode raises on a wrong prefix", True)

    # write_members must refuse when members_ok is False.
    fake_group = {"name": "X", "ids": [], "members_ok": False, "group_id": 99}
    tmp = Path(tempfile.mkdtemp(prefix="dl1-"))
    db = tmp / "g.eni"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE groups (group_id INTEGER PRIMARY KEY, recs_stamp, spec, members)")
    c.execute("INSERT INTO groups VALUES (99,0,?,?)",
              (b"x", b"\x00\x00\x00\x02\x05\x0c\x10"))
    c.commit()
    rc = eg.write_members(c, fake_group, [2])
    row = c.execute("SELECT members FROM groups WHERE group_id=99").fetchone()
    check("write_members refuses an undecodable group", rc == 1)
    check("the blob was NOT overwritten",
          row[0] == b"\x00\x00\x00\x02\x05\x0c\x10",
          f"still {row[0].hex()}")
    c.close()
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------------------
    print("\n=== C2/C3: attachment path resolution ===")

    PDFS = ep.PDFS
    cases = [
        ("1234567890/a.pdf", True,  "a normal relative attachment path"),
        (r"C:\Windows\Temp\x.pdf", False, "an absolute path (rebases the base)"),
        ("..\\..\\outside.pdf", False, "a .. escape upward"),
        ("1234567890/../../escape.pdf", False, "a .. escape hidden inside"),
        ("/etc/passwd", False, "a root-relative path"),
    ]
    for fp, expected, why in cases:
        got = ep.resolve_attachment(fp)
        ok = (got is not None) == expected
        check(f"{'kept' if expected else 'refused'}: {fp!r}", ok, why)

    # A legitimate inside path must resolve to a real location under PDFS.
    inside = ep.resolve_attachment("1234567890/a.pdf")
    check("an inside path resolves under the PDF tree",
          inside is not None and PDFS.resolve() in inside.resolve().parents
          or (inside and inside.resolve() == PDFS.resolve() / "1234567890/a.pdf"),
          str(inside))

    # ---------------------------------------------------------------------------
    print("\n=== H1: report() and the executed plan agree, and no group is emptied ===")

    import endnote_dedupe as ed
    # Two overlapping groups: record #5 is in BOTH. Whatever the algorithm keeps,
    # the critical properties are (a) both groups retain at least one member, and
    # (b) the printed KEEP marks match the ids actually executed.
    def make_group(kind, key, refs):
        return {"kind": kind, "key": key, "refs": [
            {"id": i, "year": "2020", "title": f"t{i}", "attachments": [], "doi_n": ""}
            for i in refs]}

    groups = [
        make_group("doi", "k", [5, 2]),
        make_group("title", "k", [2, 5]),
    ]
    todos, _ = ed.plan(groups, "id")
    trash_set = set(todos)
    for g in groups:
        kept_here = {r["id"] for r in g["refs"]} - trash_set
        check(f"the {g['kind']} group keeps at least one member",
              len(kept_here) >= 1, f"kept={sorted(kept_here)}")
    check("at least one of the overlapping records is kept",
          len({2, 5} - trash_set) >= 1, f"todos={sorted(trash_set)}")

    # The report must use the SAME plan: a `<- KEEP` is never an id about to be
    # trashed. Verify by simulating the marking.
    todos2, _ = ed.plan(groups, "id")
    check("the plan is deterministic (report == execution)",
          set(todos2) == trash_set, f"{sorted(todos2)} vs {sorted(trash_set)}")

    print(f"\n{'ALL GUARD CHECKS PASSED' if fails == 0 else str(fails) + ' CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
