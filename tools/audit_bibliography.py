#!/usr/bin/env python3
"""Find content from the author's real EndNote library that leaked into the plugin.

The path/username audit cannot see bibliographic content, so this covers the three
classes that a path check is structurally blind to — each of which has actually
shipped here at least once:

  1. **DOIs** that are also records in the author's library.
  2. **Author surnames** from those records, appearing anywhere in the package.
     A sort key quoting a real author list shipped in a comment once, and no check
     could see it.
  3. **Group names** — the author's real custom groups, used as examples. One
     shipped in the npm tarball via a bundled skill.

All three reveal what the author reads or how they file it. The check is
deliberately against the LIVE library, because it is a moving target: a DOI that
was safe when written becomes a finding the moment that paper is added.

Read-only; it never writes to the library.

Usage:
    python tools/audit_bibliography.py
    python tools/audit_bibliography.py --quiet     # exit code only
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import endnote_paths as ep  # noqa: E402

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\)\]`\"'<>]+")
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
#: This file necessarily quotes the terms it searches for.
SKIP_FILES = {"audit_bibliography.py"}

#: Surnames that are common English words or ubiquitous in scholarly prose. Flagging
#: them would bury the real findings in noise, and noise gets ignored.
SURNAME_STOP = {
    "wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou",
    "lee", "kim", "park", "singh", "kumar", "smith", "jones", "brown", "white",
    "young", "green", "hall", "wood", "hill", "lake", "long", "short", "best",
    "new", "main", "read", "post", "gross", "klein", "lang", "mark", "march",
    "may", "june", "july", "fall", "winter", "summer", "spring", "north",
    "south", "east", "west", "black", "gray", "rose", "field", "ford", "fox",
}


def library_dois() -> set[str]:
    """DOIs of the author's own records, via the unlocked working copy."""
    out: set[str] = set()
    if not ep.SDB.is_file():
        return out
    try:
        c = sqlite3.connect(f"file:{ep.SDB.as_posix()}?mode=ro", uri=True)
        for (ern,) in c.execute(
                "SELECT electronic_resource_number FROM refs "
                "WHERE COALESCE(trash_state,0)=0"):
            if not ern:
                continue
            m = DOI_RE.search(str(ern))
            if m:
                out.add(m.group(0).lower().rstrip("."))
        c.close()
    except sqlite3.Error:
        pass
    return out


def library_facts() -> dict:
    """Everything about the author's library that must not appear in the package.

    Returns DOIs, custom group names, and surnames from active records. Reading all
    three together — rather than DOIs alone — is the point: an earlier version
    checked only DOIs and therefore reported a clean pass while a real author list
    and a real group name sat in shipped files.
    """
    facts: dict = {"dois": set(), "groups": set(), "surnames": set(), "titles": []}
    if not ep.SDB.is_file():
        return facts
    try:
        c = sqlite3.connect(f"file:{ep.SDB.as_posix()}?mode=ro", uri=True)
        for (ern,) in c.execute(
                "SELECT electronic_resource_number FROM refs "
                "WHERE COALESCE(trash_state,0)=0"):
            if not ern:
                continue
            m = DOI_RE.search(str(ern))
            if m:
                facts["dois"].add(m.group(0).lower().rstrip("."))

        # Custom group names only: a derived group's name is EndNote's, not the
        # author's, so it discloses nothing about them.
        try:
            for (spec,) in c.execute("SELECT spec FROM groups"):
                raw = spec.decode("utf-8", "replace") if isinstance(spec, bytes) else str(spec)
                if "TYPE;3" not in raw:
                    continue
                nm = re.search(r"<name>([^<]+)</name>", raw)
                if nm and nm.group(1).strip():
                    facts["groups"].add(nm.group(1).strip())
        except sqlite3.Error:
            pass

        # Surnames: the last token of each author, from the record's author field.
        for (auth,) in c.execute(
                "SELECT author FROM refs WHERE COALESCE(trash_state,0)=0"):
            if not auth:
                continue
            for person in re.split(r"[\r\n;]+", str(auth)):
                person = person.strip()
                if not person:
                    continue
                # EndNote stores "Surname, First M." or "First M. Surname".
                surname = (person.split(",")[0] if "," in person
                           else (person.split()[-1] if person.split() else ""))
                surname = re.sub(r"[^A-Za-z'\-]", "", surname)
                if len(surname) >= 5 and surname.lower() not in SURNAME_STOP:
                    facts["surnames"].add(surname)
        c.close()
    except sqlite3.Error:
        pass
    return facts


def _edges() -> list[tuple[str, int, str]]:
    """Every (file, line number, line text) in the scanned tree."""
    out: list[tuple[str, int, str]] = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_FILES or p.suffix.lower() not in {
                ".md", ".py", ".mjs", ".js", ".yml", ".yaml", ".json", ".txt"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(p.relative_to(ROOT))
        for i, line in enumerate(text.splitlines(), 1):
            out.append((rel, i, line))
    return out


def scan_groups(edges: list[tuple[str, int, str]]) -> dict[str, list]:
    """Real custom group names appearing in the tree, with locations.

    A group name is only evidence when used AS a name — quoted, or in a command.
    Matching the bare words flagged the ordinary English phrase "the new group"
    in a docstring, which is a false positive; a check that cries wolf gets
    ignored, which is how a real finding is missed.
    """
    facts = library_facts()
    hits: dict[str, list] = {}
    for name in sorted(facts["groups"], key=len, reverse=True):
        if len(name) < 3:
            continue
        # Quoted ("X" / 'X' / `X`), or a word-boundary match with capitals intact
        # in a command-like context.
        pats = [
            re.compile(rf"""["'`]{re.escape(name)}["'`]""", re.IGNORECASE),
            re.compile(rf"--(?:group|show|add|remove)\s+[\"']?{re.escape(name)}[\"']?",
                       re.IGNORECASE),
        ]
        for rel, ln, line in edges:
            if any(p.search(line) for p in pats):
                hits.setdefault(name, []).append((rel, ln, line.strip()))
    return hits


def scan_surnames(edges: list[tuple[str, int, str]],
                  library_dois: set[str]) -> dict[str, list]:
    """Surnames from the library appearing in the tree, with locations."""
    facts = library_facts()
    hits: dict[str, list] = {}
    for name in sorted(facts["surnames"]):
        pat = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        for rel, ln, line in edges:
            if pat.search(line):
                hits.setdefault(name, []).append((rel, ln, line.strip()))
    return hits


def main() -> int:
    facts = library_facts()
    mine = facts["dois"]
    print(f"library: {len(mine)} DOI(s), {len(facts['groups'])} custom group(s), "
          f"{len(facts['surnames'])} surname(s)")
    print()

    edges = _edges()
    found: dict[str, list[tuple[str, int, str]]] = {}
    for rel, i, line in edges:
        for m in DOI_RE.finditer(line):
            doi = m.group(0).lower().rstrip(".")
            found.setdefault(doi, []).append((rel, i, line.strip()))

    print(f"DOIs referenced anywhere in the plugin: {len(found)}\n")

    # The interesting set: DOIs that are BOTH in the plugin and in the author's
    # library. These reveal research interest rather than merely illustrating code.
    overlap = sorted(set(found) & mine)
    print("=" * 72)
    if overlap:
        print(f"ATTENTION — {len(overlap)} DOI(s) are in BOTH the plugin and the "
              f"author's library:")
        print("=" * 72)
        for doi in overlap:
            print(f"\n  {doi}")
            for name, ln, text in found[doi]:
                short = text if len(text) <= 88 else text[:85] + "..."
                print(f"    {name}:{ln}")
                print(f"      {short}")
        print("\n  Assessment: a DOI that also appears in the author's library")
        print("  indicates their research area. Severity is LOW when it is used as")
        print("  an illustrative example (e.g. 'python paper_pdf.py <doi>'), and")
        print("  higher if the record's title/authors also appear. Decide whether")
        print("  to substitute an unrelated, well-known DOI.")
        print("\n  CAVEAT — this check is a MOVING TARGET: it compares against the")
        print("  library AS IT IS NOW. A DOI that was safe when written becomes a")
        print("  finding the moment that paper is added to the library. So re-run")
        print("  this AFTER any library change, and after re-vendoring skills")
        print("  (the vendored copies are what actually ship).")
    else:
        print("none — no plugin DOI appears in the author's library")

    others = sorted(set(found) - mine)
    print("\n" + "-" * 72)
    print(f"DOIs NOT in the library ({len(others)}) — illustrative examples:")
    for doi in others:
        where = ", ".join(f"{n}:{ln}" for n, ln, _ in found[doi][:3])
        print(f"  {doi:38} {where}")

    # ---- 2. group names -------------------------------------------------
    # The author's real custom group names are a disclosure too, and this was the
    # class no checker could see: one of them shipped inside a bundled skill.
    group_hits = scan_groups(edges)
    print("\n" + "=" * 72)
    if group_hits:
        print(f"ATTENTION — {len(group_hits)} real group name(s) appear in the plugin:")
        print("=" * 72)
        total = 0
        for name, where in sorted(group_hits.items()):
            print(f"\n  {name!r}")
            for f, ln, _ in where[:6]:
                print(f"    {f}:{ln}")
            if len(where) > 6:
                print(f"    ... and {len(where) - 6} more")
            total += len(where)
        print(f"\n  These are groups the author actually has in their EndNote library.")
        print("  Using one as an example discloses their filing habits and, through")
        print("  the group name, often their research area. Substitute a neutral")
        print("  name (the shipped examples use 'Reading list' / 'Chapter drafts').")
        print(f"  NOTE: skills/*/SKILL.md ships in the npm tarball, so a hit there")
        print("  reaches every consumer, not just people reading the repository.")
    else:
        print("none — no real group name appears in the plugin")

    # ---- 3. author surnames --------------------------------------------
    # A quoted sort key once reproduced a real record's author list verbatim.
    name_hits = scan_surnames(edges, mine)
    print("\n" + "=" * 72)
    if name_hits:
        print(f"ATTENTION — {len(name_hits)} surname(s) from the author's library "
              f"appear in the plugin:")
        print("=" * 72)
        for name, where in sorted(name_hits.items()):
            print(f"\n  {name}")
            for f, ln, text in where[:4]:
                short = text if len(text) <= 88 else text[:85] + "..."
                print(f"    {f}:{ln}")
                print(f"      {short}")
        print("\n  A surname from a paper the author stores is weak evidence of")
        print("  research area; several together, or a contiguous author sequence,")
        print("  is strong. Check whether the occurrence quotes a real record.")
    else:
        print("none — no library surname appears in the plugin")

    return 1 if (overlap or group_hits or name_hits) else 0


if __name__ == "__main__":
    raise SystemExit(main())
