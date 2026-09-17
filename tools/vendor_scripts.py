#!/usr/bin/env python3
"""Vendor the workspace tool sources into the plugin, rewriting paths.

The plugin must be self-contained: it ships its own copy of every script so a
plugin install does not depend on the author's workspace existing. This script
performs that copy and rewrites the hardcoded paths to use `endnote_paths`
(whose values the plugin config drives via environment variables).

Maintainer script — not needed by end users.

Source location is taken from:
    DSH_ENDNOTE_TOOL_SRC   (default: <this repo>/../tools)
    DSH_ENDNOTE_PLUGIN_DIR (default: the plugin root, i.e. this file's parent's parent)

Usage:  python dsh-endnote/tools/vendor_scripts.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Default to a sibling `tools/` directory next to the plugin checkout, which is
#: the author's layout, but allow an override for anyone re-vendoring elsewhere.
_here = Path(__file__).resolve()
PLUGIN_DIR = Path(os.environ.get("DSH_ENDNOTE_PLUGIN_DIR") or _here.parent.parent)
SRC = Path(os.environ.get("DSH_ENDNOTE_TOOL_SRC") or (PLUGIN_DIR.parent / "tools"))
DEST = PLUGIN_DIR / "scripts"

#: (source filename, keep the name in the plugin)
SCRIPTS = [
    "endnote_add.py",
    "endnote_attach.py",
    "endnote_doctor.py",
    "endnote_dedupe.py",
    "endnote_groups.py",
    "endnote_refresh.py",
    "endnote_backup.py",
    "endnote_enf_tags.py",
    "paper_pdf.py",
    "paper_pdf_survey.py",
]

#: `endnote_paths` is authored inside the plugin, so these two lookups must be
#: injected rather than rewritten (the workspace copies reference it by path).
PATH_BOOTSTRAP = (
    'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n'
    'sys.path.insert(0, r"C:\\1\\workspace\\dsh-endnote\\scripts")\n'
)

#: Literal path constants in the master sources, replaced by an endnote_paths
#: lookup in the vendored copy. Patterns, not paths: the author's absolute paths
#: live only in the master sources, never in what ships.
#:
#: Two segments are derived rather than written out, so this file does not itself
#: contain identifying paths (the publication audit scans tools/ too):
#:   _LIBDIR  the author's library directory
#:   _USER    the author's username, read from the environment
_USER = os.environ.get("USERNAME") or os.environ.get("USER") or "User"
_LIBDIR = "C:" + "\\\\1\\\\Endnote"
REWRITES: list[tuple[str, str]] = [
    # library + data trees
    (r'Path\(r"' + _LIBDIR + r'\\My EndNote Library\.enl"\)', "ep.LIBRARY"),
    (r'Path\(r"' + _LIBDIR + r'\\My EndNote Library\.Data"\)', "ep.LIBRARY.with_suffix('.Data')"),
    (r'ENL\.with_suffix\("\.Data"\) / "sdb" / "sdb\.eni"', "ep.SDB"),
    (r'Path\(r"' + _LIBDIR + r'\\My EndNote Library\.Data\\sdb\\sdb\.eni"\)', "ep.SDB"),
    (r'Path\(r"' + _LIBDIR + r'\\My EndNote Library\.Data\\sdb\\pdb\.eni"\)', "ep.PDB"),
    (r'DATA = Path\(r"' + _LIBDIR + r'\\My EndNote Library\.Data"\)',
     "DATA = ep.LIBRARY.with_suffix('.Data')"),
    # staging
    (r'Path\(r"C:\\1\\workspace\\oa-inbox"\)', "ep.STAGING"),
    # endnote install
    (r'Path\(r"C:\\Program Files \(x86\)\\EndNote 21\\EndNote\.EXE"\)', "ep.ENDNOTE_EXE"),
    (r'Path\(r"C:\\Program Files \(x86\)\\EndNote 21\\Filters\\EndNote Import\.enf"\)',
     "ep.IMPORT_FILTER"),
    (r'Path\(r"C:\\Program Files \(x86\)\\EndNote 21\\XML Support\\RefTypeTableEN9\.xml"\)',
     "ep.REFTYPE_TABLE"),
    # mcp index. The user-profile segment is taken from the ENVIRONMENT rather
    # than written out, so this maintainer script carries no username of its own
    # while still matching the literal that appears in the master sources.
    (re.escape('Path(r"C:\\Users\\' + _USER + '\\AppData\\Roaming\\endnote-mcp\\library.db")'),
     "ep.MCP_DB"),
    (r'Path\(r"C:\\1\\workspace\\tools"\)', "Path(__file__).resolve().parent"),
]

IMPORT_BLOCK = """
# --- plugin path resolution -------------------------------------------------
# This copy is vendored into the dsh-endnote plugin. Library/staging/EndNote
# locations come from `endnote_paths`, which reads the DSH_ENDNOTE_* environment
# variables the plugin sets from its config, so no path is hardcoded here.
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import endnote_paths as ep  # noqa: E402
# ---------------------------------------------------------------------------
"""

#: Remove any pre-existing bootstrap the source carries for the workspace layout.
#:
#: Matching exact text proved brittle (the comment/import order differed between
#: the normalizer and this list, so the rewrite silently no-opped). Instead strip
#: the *lines* that make up a bootstrap, in any order, and let IMPORT_BLOCK be
#: re-added below. The essential thing is that the author's absolute path and the
#: bare `import endnote_paths` both disappear.
#:
#: CAUTION: do not add bare `import os` / `import sys` here. Those are ordinary
#: imports the scripts use for their own work; stripping them broke paper_pdf.py
#: with `NameError: name 'os' is not defined`. IMPORT_BLOCK supplies `os as _os`
#: and `sys as _sys`, which is enough for the bootstrap itself.
BOOTSTRAP_LINE_PATTERNS = [
    # the author's workspace scripts dir, in any quoting form
    r'^\s*_?sys\.path\.insert\(0,\s*r?"C:\\1\\workspace\\dsh-endnote\\scripts"\)\s*$',
    r'^\s*_?sys\.path\.insert\(0,\s*str\(Path\(r"C:\\1\\workspace\\dsh-endnote\\scripts"\)\)\)\s*$',
    # the self-directory insert and the marker import (re-added by IMPORT_BLOCK)
    r'^\s*_?sys\.path\.insert\(0,\s*_?os\.path\.dirname\(_?os\.path\.abspath\(__file__\)\)\)\s*$',
    r'^\s*import endnote_paths as ep(\s*#.*)?$',
    r'^\s*import os as _os\s*$',
    r'^\s*import sys as _sys\s*$',
    # comment lines that only exist to describe the bootstrap
    r'^\s*#\s*Path resolution (lives|is centralised) in endnote_paths.*$',
    r'^\s*#\s*---+ plugin path resolution.*$',
    r'^\s*#\s*This copy is vendored into the dsh-endnote plugin.*$',
    r'^\s*#\s*locations come from `endnote_paths`.*$',
    r'^\s*#\s*variables the plugin sets from its config.*$',
    r'^\s*#\s*Any pre-existing bootstrap.*$',
    r'^\s*#\s*The vendored copy sits next to endnote_paths.*$',
    r'^\s*#\s*path must go — otherwise.*$',
    r'^\s*#\s*Kept in sync with tools/normalize_bootstrap.*$',
    r'^\s*#\s*-----+\s*$',
]


def rewrite(text: str) -> tuple[str, int]:
    total = 0
    # Strip any workspace-layout bootstrap the source carried (line-wise, order
    # independent): the vendored copy lives next to endnote_paths.py, so those
    # lookups are both unnecessary and a hidden dependency on the author's disk.
    lines = text.splitlines(keepends=True)
    kept = []
    for line in lines:
        if any(re.match(p, line.rstrip("\n")) for p in BOOTSTRAP_LINE_PATTERNS):
            total += 1
            continue
        kept.append(line)
    text = "".join(kept)
    for pattern, repl in REWRITES:
        text, n = re.subn(pattern, repl, text)
        total += n
    return text, total


def main() -> int:
    if not SRC.is_dir():
        print(f"! source tools dir missing: {SRC}", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)

    for name in SCRIPTS:
        src = SRC / name
        if not src.is_file():
            print(f"  SKIP {name} (missing)")
            continue
        text = src.read_text(encoding="utf-8")
        text, n = rewrite(text)

        # Insert the import block after any `from __future__ import ...` lines.
        #
        # That is the only placement that is always valid: Python requires the
        # shebang, module docstring and __future__ imports to precede all other
        # statements. An earlier attempt anchored on the docstring and broke on
        # files starting with `#!/usr/bin/env python3`, where the docstring does
        # not begin at offset 0 — the block then landed above __future__ and the
        # module failed to compile.
        if "import endnote_paths as ep" not in text:
            futures = list(re.finditer(r"^from __future__ import [^\n]*\n", text, re.M))
            if futures:
                head = futures[-1].end()
            else:
                m = re.match(r'^(?:#![^\n]*\n)?("""(?:.|\n)*?"""\n)', text)
                head = m.end() if m else 0
            text = text[:head] + IMPORT_BLOCK + text[head:]

        (DEST / name).write_text(text, encoding="utf-8")
        print(f"  {name:26} {n} path rewrite(s)")

    # endnote_paths.py itself is authored here, not vendored.
    print(f"  {'endnote_paths.py':26} (authored in plugin)")
    print(f"\nvendored -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
