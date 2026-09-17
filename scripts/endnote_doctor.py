#!/usr/bin/env python3
"""Health check for the EndNote <-> DSH integration.

Verifies every link in the chain and reports which one is broken:

  1. EndNote install + import filter (the .enw tag map's source of truth)
  2. library file access  — .enl (often locked) vs sdb.eni (should be readable)
  3. what the library contains, read via whichever source is available
  4. the endnote-mcp index vs the library (are they in sync?)
  5. metadata APIs reachable (Crossref / Europe PMC / OpenAlex)
  6. the .enw staging area for endnote_add.py

Usage:
    python endnote_doctor.py
    python endnote_doctor.py --refs      # also list the records
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
import sqlite3
import sys
from pathlib import Path

# All paths come from endnote_paths, which discovers the library instead of
# assuming an author-specific location. The plugin vendors both files together.

ENL = ep.LIBRARY
XML = ep.XML
SDB = ep.SDB
EXE = ep.ENDNOTE_EXE
FILTER = ep.IMPORT_FILTER
REFTYPE = ep.REFTYPE_TABLE
MCP_DB = ep.MCP_DB
STAGING = ep.STAGING


def open_ro(path: Path) -> sqlite3.Connection | None:
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=8)
        c.execute("SELECT COUNT(*) FROM refs").fetchone()
        return c
    except sqlite3.Error:
        return None


def section(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refs", action="store_true", help="list the records")
    args = ap.parse_args()

    problems: list[str] = []

    # 1 ── installation
    section(1, "EndNote installation")
    print(f"    exe          : {'OK  ' if EXE.is_file() else 'MISS'} {EXE}")
    print(f"    import filter: {'OK  ' if FILTER.is_file() else 'MISS'} {FILTER.name}")
    print(f"    ref types    : {'OK  ' if REFTYPE.is_file() else 'MISS'} {REFTYPE.name}")
    if not (EXE.is_file() and FILTER.is_file()):
        problems.append("EndNote install incomplete — cannot import")

    # 2 ── library access
    section(2, "Library access")
    enl_conn = open_ro(ENL)
    print(f"    .enl         : {'READABLE' if enl_conn else 'LOCKED (normal while EndNote runs)'}")
    sdb_conn = open_ro(SDB) if SDB.is_file() else None
    print(f"    sdb.eni      : {'READABLE' if sdb_conn else 'MISSING/UNREADABLE'}  {SDB}")
    if not enl_conn and not sdb_conn:
        problems.append("no readable library source — cannot refresh the index")

    conn = enl_conn or sdb_conn
    if conn:
        conn.row_factory = sqlite3.Row

    # 3 ── library contents
    section(3, "Library contents")
    lib_ids: set[int] = set()
    if conn:
        rows = conn.execute(
            "SELECT id, trash_state, year, author, title, secondary_title, "
            "volume, number, pages FROM refs ORDER BY id").fetchall()
        live = [r for r in rows if not r["trash_state"]]
        lib_ids = {int(r["id"]) for r in live}
        print(f"    records      : {len(rows)} total, {len(live)} active, "
              f"{len(rows) - len(live)} in trash")
        try:
            nres = conn.execute("SELECT COUNT(*) FROM file_res WHERE file_type=1").fetchone()[0]
            print(f"    attachments  : {nres} PDF(s)")
        except sqlite3.Error:
            pass
        if args.refs:
            print()
            for r in rows:
                flag = " [TRASH]" if r["trash_state"] else ""
                au = (r["author"] or "").replace("\r", "; ")
                if len(au) > 52:
                    au = au[:49] + "..."
                print(f"      #{r['id']:<4}{flag} {r['year'] or '----'}  {au}")
                print(f"             {r['title'] or '(no title)'}")
    else:
        print("    (unavailable — no readable source)")

    # 4 ── index sync
    section(4, "endnote-mcp index vs library")
    indexed_ids: set[int] = set()
    if MCP_DB.is_file():
        try:
            m = sqlite3.connect(f"file:{MCP_DB.as_posix()}?mode=ro", uri=True)
            indexed_ids = {int(r[0]) for r in m.execute("SELECT rec_number FROM references_")}
            nidx = len(indexed_ids)
            m.close()
        except sqlite3.Error as exc:
            nidx = f"ERR {exc}"
        print(f"    indexed      : {nidx}")
        if isinstance(nidx, int) and lib_ids:
            missing = lib_ids - indexed_ids      # in library, not searchable yet
            stale = indexed_ids - lib_ids        # deleted/trashed but still searchable
            if missing:
                print(f"    ! not indexed: {sorted(missing)}  -> run the refresh")
                problems.append(f"index is stale for record(s) {sorted(missing)}")
            if stale:
                # The incremental `index` command upserts but never prunes, so a
                # deleted or trashed reference stays searchable forever. Only
                # `index --full` removes it; the refresh helper now defaults to it.
                print(f"    ! still indexed but gone from the library: {sorted(stale)}")
                print("      (incremental indexing never prunes — refresh uses --full)")
                problems.append(f"index holds {len(stale)} deleted record(s): {sorted(stale)}")
            if not missing and not stale:
                print("    in sync      : OK")
        print(f"    xml export   : {'present' if XML.is_file() else 'MISSING'}  "
              f"({XML.stat().st_size:,} bytes)" if XML.is_file() else "    xml export   : MISSING")
    else:
        print(f"    index db     : MISSING {MCP_DB}")
        problems.append("endnote-mcp index missing")

    # 5 ── metadata APIs
    section(5, "Metadata APIs")
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import endnote_add as ea
        for name, url, key in [
            ("Crossref", "https://api.crossref.org/works/10.1038/nature12373",
             lambda d: d["message"]["title"][0][:40]),
            ("Europe PMC", "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
             "query=DOI:%2210.1038/nature12373%22&format=json&resultType=core",
             lambda d: f"hits={d['hitCount']}"),
            ("OpenAlex", "https://api.openalex.org/works/doi:10.1038/nature12373",
             lambda d: d["title"][:40]),
        ]:
            d = ea.http_json(url)
            if d:
                try:
                    print(f"    {name:12} OK   {key(d)}")
                except Exception:
                    print(f"    {name:12} OK")
            else:
                print(f"    {name:12} FAIL")
                problems.append(f"{name} unreachable")
    except Exception as exc:
        print(f"    could not load endnote_add: {exc}")

    # 6 ── staging
    section(6, "Import staging")
    enw_dir = STAGING / "_enw"
    print(f"    staging dir  : {'OK  ' if STAGING.is_dir() else 'MISS'} {STAGING}")
    if enw_dir.is_dir():
        enws = sorted(enw_dir.glob("*.enw"))
        print(f"    .enw files   : {len(enws)}")
        for e in enws[-5:]:
            print(f"      {e.name}")
    print("    note         : EndNote must be FOREGROUNDED for an import to land;")
    print("                   a minimized EndNote silently ignores the file.")

    # summary
    print("\n" + "=" * 62)
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
    else:
        print("All checks passed.")
    print("=" * 62)

    for c in (enl_conn, sdb_conn):
        if c:
            c.close()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
