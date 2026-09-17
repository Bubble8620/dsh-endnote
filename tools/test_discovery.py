#!/usr/bin/env python3
"""Regression tests for endnote_paths discovery.

Covers two bugs an audit found:
  M2  _from_mcp_config derived `<dir>.enl` from a pdf_dir instead of
      `<dir>/<lib>.enl`, breaking the advertised zero-config path.
  m2  reading config.yaml without BOM tolerance missed the first key.

These run against a TEMP APPDATA, so the machine's real config is untouched.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

fails = 0


def check(label: str, got, want) -> None:
    global fails
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"         got : {got}")
        print(f"         want: {want}")
        fails += 1


def with_config(body: str, *, bom: bool = False):
    """Reload endnote_paths with a temp APPDATA holding the given config.yaml."""
    tmp = Path(tempfile.mkdtemp(prefix="ep-test-"))
    try:
        appdata = tmp / "Roaming"
        cfgdir = appdata / "endnote-mcp"
        cfgdir.mkdir(parents=True)
        enc = "utf-8-sig" if bom else "utf-8"
        (cfgdir / "config.yaml").write_text(body, encoding=enc)

        old_appdata = os.environ.get("APPDATA")
        old_lib = os.environ.pop("DSH_ENDNOTE_LIBRARY", None)
        old_home = os.environ.get("DSH_HOME")
        # Keep the settings-file and scan steps from finding the real machine.
        os.environ["APPDATA"] = str(appdata)
        os.environ["DSH_HOME"] = str(tmp / "dsh-home")
        try:
            import endnote_paths
            importlib.reload(endnote_paths)
            result = endnote_paths._from_mcp_config()
        finally:
            if old_appdata is not None:
                os.environ["APPDATA"] = old_appdata
            else:
                os.environ.pop("APPDATA", None)
            if old_lib is not None:
                os.environ["DSH_ENDNOTE_LIBRARY"] = old_lib
            if old_home is not None:
                os.environ["DSH_HOME"] = old_home
            else:
                os.environ.pop("DSH_HOME", None)
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


print("=== M2: pdf_dir must yield <dir>/<lib>.enl, not <dir>.enl ===")
check(
    "pdf_dir -> full library name preserved",
    with_config("pdf_dir: D:\\Refs\\My EndNote Library.Data\\PDF\n"),
    Path("D:/Refs/My EndNote Library.enl"),
)
check(
    "pdf_dir with a different stem",
    with_config("pdf_dir: C:\\Work\\PhageRefs.Data\\PDF\n"),
    Path("C:/Work/PhageRefs.enl"),
)
check(
    "pdf_dir using forward slashes",
    with_config("pdf_dir: /home/u/Refs/Lib.Data/PDF\n"),
    Path("/home/u/Refs/Lib.enl"),
)

print("\n=== endnote_xml still derives correctly ===")
check(
    "xml -> sibling .enl",
    with_config("endnote_xml: D:\\Refs\\My EndNote Library.xml\n"),
    Path("D:/Refs/My EndNote Library.enl"),
)

print("\n=== m2: a UTF-8 BOM must not hide the first key ===")
check(
    "BOM'd pdf_dir on line 1",
    with_config("pdf_dir: D:\\Refs\\My EndNote Library.Data\\PDF\n", bom=True),
    Path("D:/Refs/My EndNote Library.enl"),
)
check(
    "BOM'd endnote_xml on line 1",
    with_config("endnote_xml: D:\\Refs\\My EndNote Library.xml\n", bom=True),
    Path("D:/Refs/My EndNote Library.enl"),
)

print("\n=== unrelated config yields nothing (no false positive) ===")
check("no relevant keys", with_config("max_pdf_pages: 30\n"), None)
check("empty pdf_dir", with_config("pdf_dir: ''\n"), None)

# Restore the module to the real machine state for any later import.
# (nothing to restore: APPDATA was already put back by with_config)
importlib.reload(sys.modules["endnote_paths"])

print(f"\n{'ALL PASSED' if fails == 0 else str(fails) + ' CHECK(S) FAILED'}")
sys.exit(0 if fails == 0 else 1)
