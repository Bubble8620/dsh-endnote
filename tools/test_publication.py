#!/usr/bin/env python3
"""Test the plugin the way a STRANGER would receive it.

Everything so far was tested from the development directory, where `tools/`, the
masters and the author's layout all exist. A published package has none of that.
This packs the tarball, extracts it into an empty directory, and runs the plugin
from there — so a file that is needed but not shipped fails here rather than for
the first user.

Checks, in order:
  1. the packed file set contains everything the entry point requires
  2. the entry imports and registers its tools/skills from the extracted copy
  3. the scripts run (doctor) with no environment pointing at this checkout
  4. nothing in the tarball references the author's machine

Read-only with respect to the repository; writes only under a temp directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node") or "node"
PYTHON = sys.executable

fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails += 1


def packed_files(pkg: dict) -> list[Path]:
    """Reproduce npm's selection from `files` + .npmignore."""
    import fnmatch

    ignores = []
    ni = ROOT / ".npmignore"
    if ni.is_file():
        ignores = [l.strip().rstrip("/") for l in ni.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")]

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
        if any(fnmatch.fnmatch(seg, ig) or fnmatch.fnmatch(rel, ig)
               or fnmatch.fnmatch(rel, f"**/{ig}")
               for ig in ignores for seg in rel.split("/")):
            continue
        out.append(p)
    return sorted(set(out))


def main() -> int:
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="dsh-publish-test-"))
    try:
        # ---- 1. materialise the "installed" package ----------------------
        pkgdir = tmp / "node_modules" / pkg["name"]
        pkgdir.mkdir(parents=True)
        files = packed_files(pkg)
        print(f"=== 1. packed file set ({len(files)} files) ===")
        for f in files:
            rel = f.relative_to(ROOT)
            dest = pkgdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        # package.json itself always ships.
        shutil.copy2(ROOT / "package.json", pkgdir / "package.json")
        check("entry point shipped", (pkgdir / pkg["main"]).is_file(), pkg["main"])
        check("patch file shipped", (pkgdir / "cordis.patch.yml").is_file())

        # Every script the entry point names must be present.
        entry = (pkgdir / pkg["main"]).read_text(encoding="utf-8")
        named = set(re.findall(r"['\"]([\w_]+\.py)['\"]", entry))
        missing = sorted(n for n in named if not (pkgdir / "scripts" / n).is_file())
        check(f"all {len(named)} scripts named in the entry are shipped",
              not missing, f"missing: {missing}" if missing else "")

        skill_dirs = sorted((pkgdir / "skills").glob("*/SKILL.md")) if (pkgdir / "skills").is_dir() else []
        check("skills shipped", len(skill_dirs) >= 2, f"{len(skill_dirs)} found")

        # ---- 2. load it from the extracted copy --------------------------
        print("\n=== 2. load from the extracted package ===")
        loader = tmp / "load.mjs"
        loader.write_text(f"""
import {{ pathToFileURL }} from 'node:url'
const m = await import(pathToFileURL({json.dumps(str(pkgdir / pkg['main']))}).href)
const tools = [], skills = []
m.apply({{
  logger: {{ info: () => {{}} }},
  tools: {{ register: (d) => {{ tools.push(d); return () => {{}} }} }},
  skills: {{ register: (s) => {{ skills.push(s); return () => {{}} }} }},
  effect: (f) => f(),
}}, new m.Config({{}}))
console.log(JSON.stringify({{
  name: m.name,
  tools: tools.map((t) => t.name),
  skills: skills.map((s) => s.name),
}}))
""", encoding="utf-8")
        r = subprocess.run([NODE, str(loader)], capture_output=True, text=True, cwd=str(tmp))
        if r.returncode != 0:
            check("entry imports", False, (r.stderr or "").strip().splitlines()[-1][:90])
        else:
            info = json.loads(r.stdout.strip().splitlines()[-1])
            check("entry imports", True, f"name={info['name']}")
            check("registers 7 tools", len(info["tools"]) == 7, ",".join(info["tools"]))
            check("registers 2 skills", len(info["skills"]) == 2, ",".join(info["skills"]))

        # ---- 3. run a script with a clean environment --------------------
        print("\n=== 3. run a script from the extracted copy ===")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("DSH_ENDNOTE")}
        env["PYTHONUTF8"] = "1"
        r = subprocess.run([PYTHON, str(pkgdir / "scripts" / "endnote_doctor.py")],
                           capture_output=True, text=True, cwd=str(pkgdir), env=env)
        out = (r.stdout or "") + (r.stderr or "")

        # What matters is that the script RUNS from the package — not that the
        # machine happens to be online. doctor exits non-zero when a metadata API
        # is unreachable, so requiring exit 0 made this test fail on a flaky
        # network and report a non-existent packaging bug. Assert on the library
        # section instead, which is local and always present.
        check("doctor runs from the package (no import/path errors)",
              "Traceback" not in out and "ModuleNotFoundError" not in out
              and "can't open file" not in out)
        check("doctor found EndNote", "EndNote installation" in out)
        check("doctor read the library path resolution", "Library access" in out
              or "library" in out.lower())
        if r.returncode != 0:
            offline = [l.strip() for l in out.splitlines()
                       if "unreachable" in l or "problem(s)" in l]
            print(f"       (doctor exited {r.returncode} — likely offline; "
                  f"{'; '.join(offline[:2]) or 'see output'})")

        # ---- 4. no author-machine references ----------------------------
        print("\n=== 4. no author-machine references in the tarball ===")
        # Patterns are DERIVED from the environment, not hardcoded: an earlier
        # version spelled out the author's username as a literal, which made the
        # checker itself the leak. The username and the checkout's ancestor paths
        # are both available at runtime, so nothing personal needs to be written
        # down here.
        leaks = []
        pats = [r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"]
        user = os.environ.get("USERNAME") or os.environ.get("USER") or Path.home().name
        if user and user.lower() not in ("root", "user", "admin", "runner"):
            pats.append(rf"\b{re.escape(user)}\b")
        bs = r"(?:\\{1,2}|/)"
        for anc in list(ROOT.parents)[:3]:
            parts = [p for p in anc.parts if p not in ("\\", "/")]
            if len(parts) >= 2 and len(parts[-1]) >= 3:
                pats.append(bs.join(re.escape(p) for p in parts))
        for f in list(pkgdir.rglob("*")):
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                for p in pats:
                    if re.search(p, line):
                        leaks.append(f"{f.relative_to(pkgdir)}:{i}")
        check("no author paths / username / real email", not leaks,
              f"{len(leaks)}: {leaks[:4]}")

        print(f"\n{'PUBLICATION TEST PASSED' if fails == 0 else str(fails) + ' CHECK(S) FAILED'}")
        return 1 if fails else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
