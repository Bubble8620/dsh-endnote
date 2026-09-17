#!/usr/bin/env python3
"""Decode the tag -> field mapping out of EndNote's own binary import filter.

The `.enw` tagged format is defined by `EndNote Import.enf`, a binary filter.
Rather than trust folklore, extract each reference type's ordered (tag, field-id)
pairs from that file and resolve the ids to names via RefTypeTableEN9.xml.
"""

from __future__ import annotations

# --- plugin path resolution -------------------------------------------------
# This copy is vendored into the dsh-endnote plugin. Library/staging/EndNote
# locations come from `endnote_paths`, which reads the DSH_ENDNOTE_* environment
# variables the plugin sets from its config, so no path is hardcoded here.
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import endnote_paths as ep  # noqa: E402
# ---------------------------------------------------------------------------

import re
import sys
from pathlib import Path

ENF = ep.IMPORT_FILTER
REFTYPE = ep.REFTYPE_TABLE


def ref_type_field_names() -> dict[int, tuple[str, dict[int, str]]]:
    text = REFTYPE.read_text(encoding="utf-8", errors="replace")
    out: dict[int, tuple[str, dict[int, str]]] = {}
    for m in re.finditer(r'<RefType id="(\d+)" name="([^"]*)"[^>]*>(.*?)</RefType>', text, re.S):
        rid, rname, body = int(m.group(1)), m.group(2), m.group(3)
        fmap = {int(f.group(1)): f.group(2).strip()
                for f in re.finditer(r'<Field id="(\d+)"[^>]*>([^<]*)</Field>', body)}
        out[rid] = (rname, fmap)
    return out


def main() -> int:
    raw = ENF.read_bytes()
    print(f"filter: {ENF.name}  {len(raw)} bytes")

    # Reference-type blocks are delimited by backtick-quoted names.
    names = [(m.start(), m.group(1)) for m in
             re.finditer(rb'`([^`\x00]{1,40})`', raw)]
    print(f"backtick-quoted names found: {len(names)}")
    for off, nm in names[:12]:
        print(f"  @{off:6} {nm.decode('cp1252', 'replace')}")

    # Pick the Journal Article block and read the tag/uint16 pairs inside it.
    target = None
    for i, (off, nm) in enumerate(names):
        if nm.strip() == b"Journal Article":
            end = names[i + 1][0] if i + 1 < len(names) else len(raw)
            target = (off, end)
            break
    if target is None:
        print("Journal Article block not found")
        return 1

    off, end = target
    seg = raw[off:end]
    print(f"\n=== Journal Article block: @{off}..{end} ({len(seg)} bytes) ===")

    pairs = []
    for m in re.finditer(rb'%([\x20-\x7e])', seg):
        tag = chr(m.group(1)[0])
        after = seg[m.end():m.end() + 8]
        # Layout after the tag char is `00 00 00 02 00 XX`: a 4-byte marker
        # then the field id as a big-endian uint16 at offset 4.
        fid = int.from_bytes(after[4:6], "big") if len(after) >= 6 else None
        pairs.append((tag, fid, after[:6].hex()))

    types = ref_type_field_names()
    _, fmap = types.get(0, ("Journal Article", {}))
    print(f"\n{'tag':4} {'id':>4}  {'field name':32} raw")
    for tag, fid, rawhex in pairs:
        name = fmap.get(fid, "?") if fid is not None else "?"
        print(f"%{tag:3} {str(fid):>4}  {name:32} {rawhex}")

    print("\n=== resolved map for Journal Article (tag -> EndNote field) ===")
    for tag, fid, _ in pairs:
        if fid is not None and fid in fmap:
            print(f"  %{tag} -> {fmap[fid]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
