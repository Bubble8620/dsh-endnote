#!/usr/bin/env python3
"""Re-export the EndNote library and rebuild the endnote-mcp search index.

Why this is its own script
--------------------------
The refresh logic used to live only in the plugin's JavaScript entry point, so
`endnote_add --refresh` (Python) would have had to reimplement it — the exact
duplication that caused an earlier bug, where two PDF fetchers drifted and one
silently failed where the other succeeded. One implementation, called by both.

Stages
------
1. Resolve paths through endnote_paths, so a misconfigured library fails loudly.
2. Regenerate the XML export that the indexer reads, when the exporter is present
   beside the library. The exporter is specific to how the user produced their
   export, so its absence is not fatal — we still re-index whatever XML exists.
3. Rebuild the index with `--full`.
4. Report index-vs-library sync so a caller can assert searchability.

Why `--full` by default
-----------------------
Plain `index` upserts but NEVER prunes, so a deleted or trashed reference stays
searchable forever (verified: five trashed records were still returned until
`--full` ran). `--incremental` is available for the case where nothing was
removed and the extra safety is not wanted.

Usage:
    python endnote_refresh.py
    python endnote_refresh.py --incremental
    python endnote_refresh.py --quiet
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

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path



def _run(cmd: list[str] | str, *, shell: bool = False, timeout: int = 300) -> tuple[bool, str]:
    """Run a command, returning (ok, combined-output) rather than raising."""
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, f"not found: {cmd if isinstance(cmd, str) else cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = "\n".join(s for s in (r.stdout, r.stderr) if s and s.strip()).strip()
    return r.returncode == 0, out


def refresh(*, incremental: bool = False, quiet: bool = False) -> int:
    """Re-export and re-index. Returns a process exit code."""
    def say(*parts: object) -> None:
        if not quiet:
            print(*parts)

    say("== resolved paths ==")
    say(ep.describe())

    # ---- 2. XML export -------------------------------------------------
    exporter = ep.LIBRARY.parent / "enl_to_endnote_xml.py"
    if ep.LIBRARY.is_file() and exporter.is_file():
        ok, out = _run([sys.executable, str(exporter), str(ep.LIBRARY), str(ep.XML)])
        say("", "== export ==")
        say(out or "(no output)")
        if not ok:
            say("  ! export failed; indexing the existing XML instead")
    else:
        reason = ("library not found" if not ep.LIBRARY.is_file()
                  else f"no exporter at {exporter}")
        say("", "== export ==", f"skipped ({reason})")

    # ---- 3. rebuild the index -----------------------------------------
    #
    # The index location must be pinned explicitly. `endnote-mcp index` reads its
    # OWN default config when given nothing, so a refresh would rewrite an index
    # wherever that config points — even if this tool was told (via
    # DSH_ENDNOTE_MCP_DB) to use somewhere else. That is a real surprise for
    # anyone operating on a copy, which is what a test or a restore does.
    #
    # `endnote-mcp index` accepts `--config PATH` (a config.yaml), and that file
    # carries `db_path`. So a small temporary config is written to state both the
    # XML to read and the database to write, making the refresh independent of
    # whatever happens to be configured globally.
    say("", "== reindex ==")
    if shutil.which("endnote-mcp") is None:
        # Optional dependency: the library still works without search tools.
        say("endnote-mcp is not installed — records will not be searchable.")
        say('  install with:  pip install "endnote-mcp"')
        index_ok = False
    else:
        args = ["endnote-mcp", "index"]
        cfg_path = None
        if ep.MCP_DB:
            cfg_path = ep.STAGING / "_refresh_config.yaml"
            try:
                ep.STAGING.mkdir(parents=True, exist_ok=True)
                cfg_path.write_text(
                    "# Written by endnote_refresh.py so the index location is\n"
                    "# explicit rather than inherited from the global config.\n"
                    f"endnote_xml: {ep.XML}\n"
                    f"pdf_dir: {ep.PDFS}\n"
                    f"db_path: {ep.MCP_DB}\n",
                    encoding="utf-8")
                args += ["--config", str(cfg_path)]
            except OSError as exc:
                say(f"  ! could not write a config to pin the index location "
                    f"({type(exc).__name__}: {exc}); using the global config")
                cfg_path = None
        if not incremental:
            # `--full` is the default on purpose: plain `index` upserts but never
            # prunes, so a trashed reference stays searchable forever (verified:
            # five trashed records were still returned until --full ran).
            args.append("--full")
        index_ok, out = _run(args, timeout=600)
        say(f"$ {' '.join(args)}")
        say(out or "(no output)")
        if cfg_path is not None:
            try:
                cfg_path.unlink()
            except OSError:
                pass
        if not index_ok:
            say("  ! indexing failed")

    # ---- 4. sync report -----------------------------------------------
    say("", "== verify ==")
    doctor = Path(__file__).resolve().parent / "endnote_doctor.py"
    if doctor.is_file():
        _ok, out = _run([sys.executable, str(doctor)], timeout=120)
        # Keep only the index/sync lines; the full doctor report is noise here.
        keep = [l for l in out.splitlines()
                if any(k in l.lower() for k in
                       ("index", "in sync", "stale", "records", "attachment"))]
        say("\n".join(keep[:14]) if keep else out[-800:])
    else:
        say("(endnote_doctor.py not found; skipping the sync check)")

    return 0 if index_ok or shutil.which("endnote-mcp") is None else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-export the library and rebuild the index.")
    ap.add_argument("--incremental", action="store_true",
                    help="add new records only; NEVER removes deleted ones")
    ap.add_argument("--quiet", action="store_true", help="summarise only")
    args = ap.parse_args(argv)
    return refresh(incremental=args.incremental, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
