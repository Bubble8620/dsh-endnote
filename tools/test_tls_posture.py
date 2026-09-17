#!/usr/bin/env python3
"""Prove the direct connection verifies TLS even when a proxy is configured.

An audit found the shipped code relaxed verification based on `if PROXY:` at
context-build time, so merely SETTING DSH_ENDNOTE_PROXY disabled verification for
every request in the process — including the direct leg and the downloaded PDF
bytes. The relaxation is only justified for a proxied connection, whose
intercepting CA the system trust store does not know.

This asserts the property directly by inspecting the context each leg receives,
rather than by reasoning about the source.

Usage:  python tools/test_tls_posture.py
"""

from __future__ import annotations

import importlib
import os
import ssl
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails += 1


#: Probe run in a SUBPROCESS for each PROXY setting, so module-level constants
#: are read with the environment already in place.
PROBE = r"""
import json, sys, ssl
sys.path.insert(0, sys.argv[1])
which = sys.argv[2]
mod = __import__(which)
out = {}
if which == "paper_pdf":
    out["direct"] = {"verify": mod._ctx(relax=False).verify_mode == ssl.CERT_REQUIRED,
                     "check_hostname": mod._ctx(relax=False).check_hostname}
    out["proxied"] = {"verify": mod._ctx(relax=True).verify_mode == ssl.CERT_REQUIRED,
                      "check_hostname": mod._ctx(relax=True).check_hostname}
    out["proxy_setting"] = mod.PROXY
elif which == "endnote_add":
    out["direct"] = {"verify": mod._ssl_ctx(relax=False).verify_mode == ssl.CERT_REQUIRED,
                     "check_hostname": mod._ssl_ctx(relax=False).check_hostname}
    out["proxied"] = {"verify": mod._ssl_ctx(relax=True).verify_mode == ssl.CERT_REQUIRED,
                      "check_hostname": mod._ssl_ctx(relax=True).check_hostname}
    out["proxy_setting"] = mod.PROXY
print(json.dumps(out))
"""

import json  # noqa: E402


def probe(module: str, proxy: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DSH_ENDNOTE")}
    env["PYTHONUTF8"] = "1"
    if proxy:
        env["DSH_ENDNOTE_PROXY"] = proxy
    r = subprocess.run([sys.executable, "-c", PROBE, str(SCRIPTS), module],
                       capture_output=True, text=True, env=env, cwd=str(SCRIPTS))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    for module in ("paper_pdf", "endnote_add"):
        print(f"=== {module} ===")

        # 1. No proxy configured: everything verifies (the normal machine).
        none = probe(module, "")
        check("no proxy: direct leg verifies",
              none["direct"]["verify"] and none["direct"]["check_hostname"])

        # 2. A proxy IS configured. The direct leg must still verify — this is the
        #    regression the audit found: it used to be unverified here.
        #    A deliberately arbitrary address: the test only needs PROXY to be
        #    non-empty, and using a realistic-looking port would have embedded the
        #    author's own setup in a file that ships with the repository.
        test_proxy = "http://127.0.0.1:9"
        with_proxy = probe(module, test_proxy)
        check("proxy set: PROXY is actually read",
              with_proxy["proxy_setting"] == test_proxy,
              with_proxy["proxy_setting"])
        check("proxy set: DIRECT leg STILL verifies  <- the regression",
              with_proxy["direct"]["verify"] and with_proxy["direct"]["check_hostname"])
        check("proxy set: only the PROXIED leg relaxes",
              not with_proxy["proxied"]["verify"]
              and not with_proxy["proxied"]["check_hostname"])
        print()

    # 3. The relaxed context must not be the default anywhere in shipped code.
    print("=== shipped scripts: no PROXY-conditional relaxation remains ===")
    bad = []
    for p in sorted(SCRIPTS.glob("*.py")):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            # A context built from a bare `if PROXY:` check is the old bug shape.
            if line.strip().startswith("if PROXY:") and "relax" not in line:
                # Confirm it guards a TLS relaxation within the next few lines.
                window = "\n".join(text.splitlines()[i - 1:i + 3])
                if "CERT_NONE" in window or "check_hostname" in window:
                    bad.append(f"{p.name}:{i}")
    check("no `if PROXY:` guarding CERT_NONE", not bad, ", ".join(bad))

    print(f"\n{'TLS POSTURE OK' if fails == 0 else str(fails) + ' CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
