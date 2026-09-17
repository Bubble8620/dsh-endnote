#!/usr/bin/env python3
"""Audit the plugin for anything that must not be published.

Checks for absolute personal paths, usernames, and machine-specific EndNote
locations — all of which break portability AND leak private detail if pushed to a
public repository.

The BANNED patterns below deliberately contain the author's identifiers: this
file exists to find them, so it is excluded from its own scan.

Usage:
    python dsh-endnote/tools/audit_publication.py
    python dsh-endnote/tools/audit_publication.py --strict   # also scan tools/
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: BLOCKING — these identify the author's machine, workspace or library. They
#: must never appear in a shipped file: they both leak private detail and make the
#: package non-portable on another machine.
#:
#: CRITICAL — why these patterns look the way they do:
#:
#: A path appears in two textual forms. In Markdown/prose it is written once
#: (`C:\1\workspace`); in Python/JS *source* it is written with doubled
#: backslashes (`"C:\\1\\workspace"`). A pattern of `r"C:\\1\\"` is a REGEX that
#: matches only the single-backslash form, so an earlier version of this audit
#: reported PASS while real author paths sat in shipped .py files. That actually
#: happened: it missed scripts/endnote_dedupe.py and scripts/paper_pdf.py.
#:
#: So every path pattern must accept one OR two backslashes (`\\{1,2}`) and both
#: separators.
_BS = r"(?:\\{1,2}|/)"


def _path(*parts: str) -> str:
    """Build a path regex tolerant of escaped and unescaped separators.

    Each component has trailing separators stripped first. On Windows
    `Path.parts` yields `'C:\\\\'` for the drive — drive AND separator — and
    passing that through `re.escape` produced a pattern demanding two literal
    backslashes. The derived workspace check therefore matched the Python-source
    form but silently missed the Markdown form it exists to catch: a documented
    tolerance that was not actually delivered.
    """
    clean = [p.rstrip("\\/") for p in parts]
    return _BS.join(re.escape(p) for p in clean if p)


def _derived_patterns() -> list[tuple[str, str]]:
    """Sensitive strings discovered from the RUNNING environment.

    Why derived rather than hardcoded: this file used to contain the author's
    actual username and workspace path as literal strings, so the audit tool
    itself was the leak — and because it skips its own filename, it could never
    detect that. Anything the check needs is already available at runtime:
    the username from the environment, the home directory, and this file's own
    ancestors. Deriving them keeps the tool portable to any machine (it will flag
    the *current* user's details, which is the correct behaviour) while shipping
    no personal data of its own.
    """
    out: list[tuple[str, str]] = []

    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        try:
            user = Path.home().name
        except (RuntimeError, OSError):
            user = ""
    if user and user.lower() not in ("root", "user", "admin", "runner"):
        out.append((rf"\b{re.escape(user)}\b", "current username"))
        out.append((_path("C:", "Users", user), "current user profile path"))

    # The checkout's own ancestor directories, e.g. C:\proj\plugin -> C:\proj.
    #
    # `Path.parts` gives the drive as 'C:\\' (drive AND separator) on Windows, so
    # the backslash is stripped to 'C:' and re-joined with the tolerant separator.
    # Every non-empty component is kept — an earlier filter dropped single-character
    # names, which silently lost the `1` in `C:\1\workspace` and produced a pattern
    # matching `C:\workspace`. The specificity guard is the length of the LAST
    # component, not of each one.
    for anc in list(ROOT.parents)[:3]:
        parts = [p.rstrip("\\/") for p in anc.parts if p.rstrip("\\/")]
        if len(parts) >= 2 and len(parts[-1]) >= 3:
            out.append((_path(*parts), "workspace path"))

    return out


def _personal_patterns() -> list[tuple[str, str]]:
    """Author-specific patterns read from a git-ignored local file.

    Some things this check must recognise are themselves the disclosure — an
    institutional repository host that identifies the author, for instance. If the
    literal lived in the committed code it would sit in the published repository,
    exactly the leak it exists to catch. So it lives in
    `tools/audit_publication.personal.txt`, one regex per line, which is
    git-ignored and ships nothing. The file is read at runtime when present; on a
    clean clone it is absent and these checks simply do not apply.

    This is the same principle as `_derived_patterns` (recognise without
    recording) applied to things the environment cannot supply.
    """
    f = ROOT / "tools" / "audit_publication.personal.txt"
    if not f.is_file():
        return []
    out: list[tuple[str, str]] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append((line, "personal identifier (from a git-ignored file)"))
    return out


BLOCKING = _derived_patterns() + _personal_patterns() + [
    # Any real email other than a documented placeholder domain OR a service
    # noreply address that is meant to be public. `users.noreply.github.com` is
    # GitHub's privacy-preserving address and is *designed* to be published; a
    # check that flags it produces a false positive every release.
    (r"[A-Za-z0-9._%+-]+@(?!(?:example\.(?:com|org|net)|users\.noreply\.github\.com|noreply)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     "email address"),
]

#: CONVENTIONAL — paths that are the same on every Windows machine, or that name
#: EndNote's own defaults. Reported for review but NOT failures.
#:
#: `My EndNote Library.enl` is the name EndNote itself proposes for a new library,
#: and `C:\Program Files (x86)\EndNote 21` is the standard install location. A
#: detector that flagged those would be noise — and noise gets ignored, which is
#: how a real leak slips through. They are listed separately on purpose.
CONVENTIONAL = [
    (_path("C:", "Program Files (x86)", "EndNote") + r" \d+", "standard EndNote install path"),
    (_path("C:", "Program Files", "EndNote") + r" \d+", "standard EndNote install path"),
    # Only the real default location counts. Matching the bare filename flagged
    # invented test fixtures like "D:/Refs/My EndNote Library.enl", which is a
    # false positive — and false positives train people to ignore the report.
    (r"Documents[\\/]My EndNote Library\.enl", "EndNote's default library location"),
    (r"127\.0\.0\.1:\d+", "local proxy (documented, overridable)"),
]

SKIP_DIRS = {"__pycache__", ".git", "node_modules"}

#: This file contains the patterns by design, as does the self-test that
#: demonstrates the escaping trap.
SELF = {"audit_publication.py"}


def iter_files(include_tools: bool) -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SELF:
            continue
        # tools/ holds maintainer scripts: off by default, since some legitimately
        # describe the author's layout in comments and default values.
        if not include_tools and "tools" in p.relative_to(ROOT).parts:
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit for publishable-clean content.")
    ap.add_argument("--strict", action="store_true",
                    help="also scan tools/ (maintainer scripts)")
    args = ap.parse_args()

    files = iter_files(args.strict)
    blocking: dict[str, list[tuple[int, str, str]]] = {}
    conventional: dict[str, list[tuple[int, str, str]]] = {}
    n_block = n_conv = 0

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pattern, label in BLOCKING:
                if re.search(pattern, line):
                    blocking.setdefault(str(f.relative_to(ROOT)), []).append(
                        (i, label, line.strip()))
                    n_block += 1
            for pattern, label in CONVENTIONAL:
                if re.search(pattern, line):
                    conventional.setdefault(str(f.relative_to(ROOT)), []).append(
                        (i, label, line.strip()))
                    n_conv += 1

    scope = "all files including tools/" if args.strict else "shipped files"
    print(f"scanned {len(files)} file(s)  [{scope}]\n")

    def dump(title: str, table: dict, count: int, note: str) -> None:
        if not count:
            return
        print(f"{title} ({count})\n{note}\n")
        for name, hits in table.items():
            print(f"  {name}  ({len(hits)})")
            for ln, label, text in hits[:8]:
                short = text if len(text) <= 92 else text[:89] + "..."
                print(f"      L{ln:<4} [{label}] {short}")
            if len(hits) > 8:
                print(f"      ... and {len(hits) - 8} more")
        print()

    dump("BLOCKING — author-identifying", blocking, n_block,
         "  These must be fixed before publishing.")
    dump("CONVENTIONAL — same on every machine (review, not a failure)",
         conventional, n_conv,
         "  EndNote's own default library location and its standard install path.")

    if not args.strict and n_block:
        print(f"FAIL — {n_block} blocking occurrence(s).")
        return 1

    if args.strict and n_block:
        # In --strict the maintainer scripts are included, and three of them
        # legitimately contain the author's layout: their whole job is to REWRITE
        # those paths when vendoring into the plugin. They are excluded from the
        # published tarball (.npmignore lists tools/), so their contents cannot
        # leak. Report them as expected rather than as a failure, so the strict
        # run stays a useful signal instead of permanent noise.
        offenders = set(blocking)
        # Paths are reported with the platform separator, so normalise before
        # comparing — assuming "/" made every maintainer script look unexpected.
        maintainer_only = {"tools/vendor_scripts.py", "tools/vendor_skills.py",
                           "tools/normalize_bootstrap.py"}
        norm = {k.replace("\\", "/"): k for k in offenders}
        unexpected = {norm[k] for k in (set(norm) - maintainer_only)}
        print("NOTE — the only blocking hits are in maintainer-only scripts that are")
        print("       excluded from the published package by .npmignore:")
        for name in sorted(offenders):
            key = name.replace("\\", "/")
            print(f"         {name}{'  (expected)' if key in maintainer_only else '  <-- UNEXPECTED'}")
        if unexpected:
            print(f"\nFAIL — {len(unexpected)} unexpected file(s): {sorted(unexpected)}")
            return 1
        print("\nPASS — shipped files are clean; remaining hits are maintainer-only.")
        return 0

    print("PASS — no author-identifying paths or identifiers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
