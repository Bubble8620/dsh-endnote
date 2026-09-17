#!/usr/bin/env python3
"""Vendor the two skill files into the plugin, rewriting tool paths.

The skills reference the tool scripts in their examples. Inside the plugin those
scripts live in `<package>/scripts`, so the examples must point there — otherwise
the model would follow instructions to a path that only exists on the author's
machine, and the published bundle would leak that path.

The rewrite target is relative to the *plugin*, but the skill text needs
something a model can actually execute. It uses `<plugin>/scripts/...` style
placeholders that the plugin entry point resolves, plus a note explaining that
`DSH_ENDNOTE_SCRIPTS` is set for the session.

Usage:
    python dsh-endnote/tools/vendor_skills.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Author's master copies. Override with DSH_ENDNOTE_SKILL_SRC.
SRC_ROOT = Path(os.environ.get("DSH_ENDNOTE_SKILL_SRC") or (Path.home() / ".dsh" / "skills"))
DEST = Path(__file__).resolve().parent.parent / "skills"

SKILLS = ["endnote-add", "paper-pdf"]

#: How the vendored skill should refer to the scripts. The plugin sets
#: DSH_ENDNOTE_SCRIPTS for the child processes, and the model can always call the
#: native tools instead, so the examples stay machine independent.
PLACEHOLDER = "<plugin>/scripts"

#: Rewrite the author's absolute paths to portable placeholders. Order matters:
#: the more specific prefixes must run before the generic fallbacks.
REWRITES: list[tuple[str, str]] = [
    (r"C:\\1\\workspace\\dsh-endnote\\scripts", PLACEHOLDER),
    (r"C:\\1\\workspace\\tools", PLACEHOLDER),
    (r"C:\\1\\Endnote\\refresh-endnote-index\.ps1", "<refresh script>"),
    (r"C:\\1\\Endnote\\My EndNote Library\.enl", "<library.enl>"),
    (r"C:\\1\\Endnote", "<EndNote dir>"),
    (r"C:\\1\\workspace\\oa-inbox", "<staging dir>"),
    (r"C:\\1\\workspace\\ENDNOTE-DSH-SETUP\.md", "the plugin README"),
    (r"C:\\1\\workspace", "<workspace>"),
    (r"C:\\path\\paper\.pdf", r"C:\path\to\paper.pdf"),
]

#: A note prepended to each vendored skill, so provenance and the placeholder
#: convention are obvious to anyone reading the published file.
BANNER = """<!--
Vendored into the `dsh-endnote` plugin by tools/vendor_skills.py.
Do not edit this copy directly: edit the source skill, then re-run the vendorer.

Paths below are placeholders:
  <plugin>/scripts   the bundled tool scripts (the plugin sets
                     DSH_ENDNOTE_SCRIPTS to this directory)
  <staging dir>      where downloads are stored (the `stagingDir` config)
  <library.enl>      your EndNote library (the `library` config)

When the plugin is installed, PREFER ITS NATIVE TOOLS over running a script by
path — they are the same code, already wired to your configuration. Reach for the
script form only when the tools are unavailable.
-->

"""


def main() -> int:
    if not SRC_ROOT.is_dir():
        print(f"! skill source dir not found: {SRC_ROOT}", file=sys.stderr)
        print("  set DSH_ENDNOTE_SKILL_SRC to the directory holding the skills",
              file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    for dir_name in SKILLS:
        src = SRC_ROOT / dir_name / "SKILL.md"
        if not src.is_file():
            print(f"  SKIP {dir_name} (missing {src})")
            continue
        text = src.read_text(encoding="utf-8")

        # Insert the banner AFTER the frontmatter block so it stays valid YAML.
        m = re.match(r"^(---\r?\n.*?\r?\n---\r?\n)", text, re.S)
        if m:
            text = text[:m.end()] + "\n" + BANNER + text[m.end():]
        else:
            text = BANNER + text

        n = 0
        for pattern, repl in REWRITES:
            # Use a lambda: the replacement is a path, and re.subn treats
            # backslash-digit sequences in a plain string as group references.
            text, k = re.subn(pattern, lambda _m, r=repl: r, text)
            n += k

        out = DEST / dir_name
        out.mkdir(parents=True, exist_ok=True)
        (out / "SKILL.md").write_text(text, encoding="utf-8")
        print(f"  {dir_name:14} {n} rewrite(s) -> {out / 'SKILL.md'}")

    print(f"\nvendored skills -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

