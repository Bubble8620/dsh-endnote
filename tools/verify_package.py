#!/usr/bin/env python3
"""Verify what a published npm tarball would actually contain.

This asks npm itself (`npm pack --dry-run --json`) rather than reimplementing its
selection rules. That distinction matters: an earlier version approximated the
choice as `files` ∩ `.npmignore`, which is *wrong* — when `files` is set, npm
ignores `.npmignore` for those paths instead of intersecting with it. So the old
version reported PASS for a set it had computed itself, and would have kept
passing even if `files` were changed to ship the whole `scripts/` directory
(which is exactly how `__pycache__/*.pyc` once leaked, baking absolute build
paths into the tarball).

`npm pack` is the authority; this parse is just a way to ask it.

Usage:  python tools/verify_package.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Patterns that must never appear in a published tarball.
#:
#: Note the survey pattern: `_survey` alone would match the legitimate script
#: `paper_pdf_survey.py`. It is anchored to the OUTPUT files the survey writes
#: (`_survey.txt`, `_survey_raw.txt`) so a real filename is not a false failure —
#: a check that cries wolf gets ignored, which defeats its purpose.
MUST_NOT_SHIP = [
    r"__pycache__", r"\.pyc$", r"\.pyo$",
    r"\.enl$", r"\.enlx$", r"\.eni$", r"\.enw$", r"\.pdf$",
    r"library\.db", r"\.Data/", r"backups", r"oa-inbox", r"endnote-staging",
    r"endnote\.json$", r"(^|/)_survey[^/]*\.(txt|log|json)$",
    r"\.log$", r"\.bak$", r"Thumbs\.db$", r"\.DS_Store$",
]
#: Directories that must not be published. Checked against the real file list, so
#: this can actually fire — an earlier version tested membership in a list the
#: candidates were drawn from, making the check vacuous.
MAINTAINER_ONLY = ("tools/",)


def packed_files() -> tuple[list[str], str]:
    """Ask npm what it would pack. Returns (files, how)."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        try:
            r = subprocess.run([npm, "pack", "--dry-run", "--json"],
                               capture_output=True, text=True, cwd=str(ROOT),
                               timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if isinstance(data, list) and data:
                    files = data[0].get("files", [])
                    return sorted(f["path"] for f in files), "npm pack --dry-run"
                files = data.get("files", []) if isinstance(data, dict) else []
                return sorted(f["path"] for f in files), "npm pack --dry-run"
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass
    # Fallback: approximate, and SAY SO, because the approximation is the bug this
    # tool exists to avoid.
    return _approximate(), "APPROXIMATED (npm unavailable — treat as advisory)"


def _approximate() -> list[str]:
    """Rough `files` expansion. Only used when npm cannot be run."""
    import fnmatch
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    chosen: list[Path] = []
    for entry in pkg.get("files", []):
        if any(ch in entry for ch in "*?["):
            chosen.extend(sorted(ROOT.glob(entry)))
        else:
            p = ROOT / entry
            if p.is_file():
                chosen.append(p)
            elif p.is_dir():
                chosen.extend(sorted(x for x in p.rglob("*") if x.is_file()))
    out = []
    for p in chosen:
        rel = p.relative_to(ROOT).as_posix()
        # npm applies its own default ignores even with `files` set; mirror the
        # ones that matter here.
        if any(fnmatch.fnmatch(seg, "__pycache__") or seg.endswith(".pyc")
               for seg in rel.split("/")):
            continue
        out.append(rel)
    return sorted(set(out))


def main() -> int:
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    files, how = packed_files()
    print(f"package.json files: {pkg.get('files')}")
    print(f"source of truth   : {how}")
    if "APPROXIMATED" in how:
        print("  ! npm could not be run, so the selection was computed locally.")
        print("    That approximation is NOT authoritative — see this file's docstring.")
    print(f"\nwould publish {len(files)} file(s):")
    for f in files:
        print(f"  {f}")

    failures: list[tuple[str, str]] = []
    for f in files:
        for pat in MUST_NOT_SHIP:
            if re.search(pat, f):
                failures.append((f, pat))
        if any(f.startswith(d) for d in MAINTAINER_ONLY):
            failures.append((f, "maintainer-only directory"))

    # A sanity floor: an empty or absurdly small set means the parse failed rather
    # than that the package is clean.
    if len(files) < 5:
        failures.append((f"<only {len(files)} files>", "suspiciously small file set"))

    print()
    if failures:
        print("FAIL — these must not be published:")
        for f, why in failures:
            print(f"  {f}   (matched {why})")
        return 1

    print("PASS — no build artefacts, maintainer scripts, or user data.")
    if "APPROXIMATED" in how:
        print("       (but the file list was approximated; re-run where npm works)")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
