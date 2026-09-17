#!/usr/bin/env python3
"""Insert the canonical endnote_paths bootstrap into workspace tool sources.

The plugin's vendorer replaces this block with its own, so the two must agree on
the exact text. Doing it with a script rather than by hand keeps all seven
scripts identical and avoids the "block landed above __future__" class of bug,
which already broke every script once.

Maintainer script; source location overridable for a different checkout.

Usage:
    python dsh-endnote/tools/normalize_bootstrap.py [--check]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
PLUGIN_DIR = _HERE.parent.parent
SRC = Path(os.environ.get("DSH_ENDNOTE_TOOL_SRC") or (PLUGIN_DIR.parent / "tools"))

#: Sources that must carry the bootstrap, in plugin order.
TARGETS = [
    "endnote_add.py",
    "endnote_attach.py",
    "endnote_backup.py",
    "endnote_doctor.py",
    "endnote_dedupe.py",
    "endnote_groups.py",
    "paper_pdf.py",
]

MARK = "import endnote_paths as ep"

#: The canonical block. It uses `_os`/`_sys` aliases so it cannot collide with a
#: file's own `import os` / `import sys`, which is what produced duplicated
#: imports when the block used the plain names.
BLOCK = (
    "# Path resolution lives in endnote_paths (no machine-specific defaults).\n"
    "import os as _os\n"
    "import sys as _sys\n"
    "\n"
    '_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))\n'
    '_sys.path.insert(0, r"C:\\1\\workspace\\dsh-endnote\\scripts")\n'
    "import endnote_paths as ep  # noqa: E402\n"
)

#: Lines belonging to a previously-inserted bootstrap, in any historical form.
#: Matched line-wise so order does not matter.
STRIP_LINES = [
    r'^\s*_?sys\.path\.insert\(0,\s*r?"C:\\1\\workspace\\dsh-endnote\\scripts"\)\s*$',
    r'^\s*_?sys\.path\.insert\(0,\s*str\(Path\(r"C:\\1\\workspace\\dsh-endnote\\scripts"\)\)\)\s*$',
    r"^\s*_?sys\.path\.insert\(0,\s*_?os\.path\.dirname\(_?os\.path\.abspath\(__file__\)\)\)\s*$",
    r"^\s*import endnote_paths as ep(\s*#.*)?$",
    r"^\s*import os as _os\s*$",
    r"^\s*import sys as _sys\s*$",
    r"^\s*#\s*Path resolution (lives|is centralised) in endnote_paths.*$",
]


def canonical(text: str) -> bool:
    return MARK in text and "import os as _os" in text


def apply(text: str) -> tuple[str, bool]:
    # Idempotence: if the canonical block is already present, do nothing. Testing
    # this FIRST matters — an earlier version stripped the block, then saw no
    # marker and re-added it, so --check reported "needs update" forever.
    if canonical(text):
        return text, False

    kept = [ln for ln in text.splitlines(keepends=True)
            if not any(re.match(p, ln.rstrip("\n")) for p in STRIP_LINES)]
    text = "".join(kept)

    # Anchor after the last `from __future__` line (those must come first), else
    # after the module docstring, else at the very top.
    futures = list(re.finditer(r"^from __future__ import [^\n]*\n", text, re.M))
    if futures:
        head = futures[-1].end()
    else:
        m = re.match(r'^(?:#![^\n]*\n)?("""(?:.|\n)*?"""\n)', text)
        head = m.end() if m else 0

    return text[:head] + "\n" + BLOCK + text[head:], True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report without writing")
    args = ap.parse_args()

    stale = 0
    for name in TARGETS:
        p = SRC / name
        if not p.is_file():
            print(f"  SKIP     {name} (missing)")
            stale += 1
            continue
        text = p.read_text(encoding="utf-8")
        new, changed = apply(text)
        if args.check:
            print(f"  {name:24} {'needs update' if changed else 'ok'}")
            stale += 1 if changed else 0
            continue
        if changed:
            p.write_text(new, encoding="utf-8")
            print(f"  {name:24} updated")
        else:
            print(f"  {name:24} already canonical")

    if args.check:
        print(f"\n{stale} file(s) need normalizing")
        return 1 if stale else 0
    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
