#!/usr/bin/env python3
"""Every shipped script must at least IMPORT.

A script that raises on import is broken for every caller, but nothing caught it:
`scripts/paper_pdf_survey.py` shipped with a NameError because an edit added
`os.environ.get(...)` without the matching `import os`. The bundling step rewrote
the imports into `import os as _os`, so the file looked internally consistent while
being unable to load at all.

This runs each shipped script in a subprocess with a deliberately incomplete
environment and checks that the failure — if any — is the script's OWN argument
handling, never a NameError/ImportError/SyntaxError. `--help` is used so no script
does real work; scripts without argparse are invoked with `--help` too and are
expected to fail cleanly rather than crash.

Also checks the plugin entry point, since a JS syntax error would be equally fatal
to a consumer.

Usage:  python tools/test_imports.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable

#: Failures that mean "this file cannot be loaded", as opposed to "this file wants
#: different arguments".
FATAL = re.compile(
    r"NameError|ImportError|ModuleNotFoundError|SyntaxError|IndentationError|"
    r"AttributeError: module|UnboundLocalError"
)

fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails += 1


def main() -> int:
    scripts = sorted(SCRIPTS.glob("*.py"))
    print(f"=== {len(scripts)} shipped script(s): can each be imported? ===")

    env = {k: v for k, v in os.environ.items() if not k.startswith("DSH_ENDNOTE")}
    env["PYTHONUTF8"] = "1"
    # Deliberately unset the library so a script cannot depend on this machine.
    env.pop("DSH_ENDNOTE_LIBRARY", None)

    for p in scripts:
        # IMPORT the module rather than run it. Running with --help assumes every
        # script parses arguments; paper_pdf_survey.py has none and would start a
        # multi-minute network sweep. Importing is also the stronger check: it
        # catches a NameError or a missing import without doing any work, and it
        # proves the module can be loaded for inspection at all — which is exactly
        # what failed when paper_pdf_survey.py ran its survey at import time.
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('probe', r'{p}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            # Register BEFORE exec: dataclasses looks the module up in sys.modules
            # to resolve annotations, so a module using @dataclass fails to load
            # otherwise — an artefact of the loading technique, not of the script.
            "sys.modules['probe'] = mod\n"
            "spec.loader.exec_module(mod)\n"
            "print('IMPORTED-OK')\n"
        )
        try:
            r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                               cwd=str(SCRIPTS), env=env, timeout=45)
            out = (r.stdout or "") + (r.stderr or "")
            bad = FATAL.search(out)
            ok = ("IMPORTED-OK" in out) and not bad
            check(f"{p.name} imports cleanly", ok,
                  f"-> {bad.group(0)}" if bad else ("" if ok else "did not import"))
        except subprocess.TimeoutExpired:
            # A module that does its work at import time cannot be loaded safely by
            # any caller; treat it as a failure rather than a slow pass.
            check(f"{p.name} imports cleanly", False,
                  "timed out — it runs work at import time")

    # A syntax/import error in the entry point is equally fatal for a consumer.
    print("\n=== plugin entry point ===")
    node = "node"
    r = subprocess.run([node, "--check", str(ROOT / "lib" / "index.js")],
                       capture_output=True, text=True, timeout=60)
    check("lib/index.js parses", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[-1][:80] if r.returncode else "")

    r = subprocess.run([node, "-e", (
        "const p=require('path');import(p+'').catch(()=>{});"
    )], capture_output=True, text=True, timeout=30)
    check("node itself is runnable", r.returncode == 0)

    # Every non-stdlib import must be intentional: the package promises no
    # third-party Python dependency, so a stray one is both a bug and a lie.
    print("\n=== shipped scripts declare no third-party Python dependency ===")
    import ast
    stdlib = set(sys.stdlib_module_names)
    local = {q.stem for q in SCRIPTS.glob("*.py")}
    external: dict[str, list[str]] = {}
    for p in scripts:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top not in stdlib and top not in local:
                    external.setdefault(top, []).append(p.name)
    check("no third-party imports", not external, str(external) if external else "")

    print(f"\n{'ALL IMPORT CHECKS PASSED' if fails == 0 else str(fails) + ' CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
