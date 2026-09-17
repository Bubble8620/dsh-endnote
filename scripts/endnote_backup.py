#!/usr/bin/env python3
"""SAFETY BACKUP before any write experiment.

Copies .enl + the sdb sidecars to a timestamped folder so an experiment can
always be undone. Also records row counts for before/after comparison.
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


import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ENL = ep.LIBRARY
DATA = ENL.with_suffix(".Data")
#: Backups go under the harness home, not into any author-specific folder.
DEST = Path(os.environ.get("DSH_ENDNOTE_BACKUPS") or (ep.dsh_home() / "endnote-backups"))


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = DEST / stamp
    out.mkdir(parents=True, exist_ok=True)

    copied = []
    skipped = []
    targets = [ENL, ep.SDB,
               ENL.with_suffix(".Data") / "sdb" / "pdb.eni"]
    for src in targets:
        if not src.is_file():
            skipped.append((src.name, "missing"))
            continue
        dst = out / src.name
        try:
            # .enl is exclusively locked while EndNote runs; plain copy can fail.
            # read_bytes() goes through a different path and often still works.
            with open(src, "rb") as fh:
                dst.write_bytes(fh.read())
            copied.append((src.name, dst.stat().st_size))
        except OSError as exc:
            # Last resort: SQLite's own backup API for the DB files.
            try:
                import sqlite3 as s3
                src_conn = s3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=10)
                dst_conn = s3.connect(str(dst))
                with dst_conn:
                    src_conn.backup(dst_conn)
                src_conn.close()
                dst_conn.close()
                copied.append((src.name, dst.stat().st_size))
            except Exception as exc2:
                skipped.append((src.name, f"{type(exc).__name__}/{type(exc2).__name__}"))

    print(f"backup -> {out}")
    for name, size in copied:
        print(f"  copied  {name:28} {size:>10,} bytes")
    for name, why in skipped:
        print(f"  SKIPPED {name:28} ({why})")

    if not copied:
        # Distinguish "the library is locked by EndNote" from "there is no
        # library". The original message blamed EndNote unconditionally, which
        # disguised an absent library as normal operation — and the script still
        # exited 0, so `endnote_backup.py && ...` left the caller believing a
        # backup existed.
        library_present = ep.LIBRARY.is_file() or ep.SDB.is_file()
        if not library_present:
            print("\n  ! NO LIBRARY FOUND — nothing was backed up.")
            print(f"    looked for: {ep.LIBRARY}")
            print(f"    found via : {ep.LIBRARY_SOURCE}")
            print("    Point the plugin at your library: set the `library` config,")
            print("    DSH_ENDNOTE_LIBRARY, or write $DSH_HOME/endnote.json.")
            return 1
        print("\n  ! could not copy any file. If EndNote holds the library open that")
        print("    is expected for the .enl; close EndNote and re-run for a full copy.")
        return 1

    if not any(n == ENL.name for n, _ in copied):
        print("\n  note: the .enl itself was not copied (EndNote holds it open).")
        print("        sdb.eni is the live state, so the important part is captured;")
        print("        close EndNote and re-run for a complete backup.")

    # current counts, for comparison after an experiment
    sdb = ep.SDB
    try:
        c = sqlite3.connect(f"file:{sdb.as_posix()}?mode=ro", uri=True)
        print(f"\ncurrent state (via sdb.eni):")
        print(f"  refs      : {c.execute('SELECT COUNT(*) FROM refs').fetchone()[0]}")
        print(f"  file_res  : {c.execute('SELECT COUNT(*) FROM file_res').fetchone()[0]}")
        print(f"  max id    : {c.execute('SELECT MAX(id) FROM refs').fetchone()[0]}")
        c.close()
    except Exception as exc:
        print(f"\ncould not read sdb.eni: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
