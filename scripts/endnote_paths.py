#!/usr/bin/env python3
"""Single source of truth for every path the EndNote tooling touches.

Design goal: **no personal path is hardcoded anywhere in this package.** The
library, staging folder, EndNote install and search index are all discovered, so
the plugin works on someone else's machine without editing any script.

Resolution order for the library
--------------------------------
1. `DSH_ENDNOTE_LIBRARY` environment variable (the plugin sets this from its
   `library` config field).
2. A machine-local settings file, `$DSH_HOME/endnote.json` — created by the user,
   never shipped, and the natural place for per-machine paths.
3. The existing `endnote-mcp` config (`%APPDATA%/endnote-mcp/config.yaml`), which
   already records the XML export and PDF directory for the *live* library. The
   `.enl` is derived from it. This is how an already-configured machine is picked
   up with no new configuration at all.
4. A bounded scan of the usual EndNote locations (Documents, Desktop, Downloads,
   OneDrive Documents) for `*.enl`.
5. A conventional fallback path that probably does not exist — reported honestly
   by `endnote_doctor.py` and by `require_library()` rather than guessed at.

Overrides (all optional):
    DSH_ENDNOTE_LIBRARY   path to the .enl library
    DSH_ENDNOTE_STAGING   folder for downloaded PDFs / generated .enw files
    DSH_ENDNOTE_EXE       path to EndNote.EXE
    DSH_ENDNOTE_PYTHON    Python interpreter used by the plugin's tool wrappers
    DSH_ENDNOTE_MCP_DB    endnote-mcp search index (library.db)
    DSH_ENDNOTE_REFRESH   refresh-endnote-index.ps1
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# --------------------------------------------------------------- helpers

def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def dsh_home() -> Path:
    """The harness config root, where machine-local files may live."""
    v = _env("DSH_HOME")
    if v:
        return Path(v).expanduser()
    return Path.home() / ".dsh"


def appdata() -> Path:
    v = _env("APPDATA")
    if v:
        return Path(v)
    return Path.home() / "AppData" / "Roaming"


# --------------------------------------------------------------- library

def _from_settings_file() -> Path | None:
    """$DSH_HOME/endnote.json — {"library": "...", "stagingDir": "..."}."""
    f = dsh_home() / "endnote.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lib = (data.get("library") or "").strip()
    return Path(lib).expanduser() if lib else None


def _from_mcp_config() -> Path | None:
    """Derive the .enl from endnote-mcp's config.yaml (xml path or pdf dir).

    This is the path that makes an already-configured machine work with NO
    configuration, so a mistake here silently points the whole toolchain at a
    nonexistent library.

    The pdf_dir case is subtle. For `<dir>/My EndNote Library.Data/PDF` the
    library is `<dir>/My EndNote Library.enl`. An earlier version used
    `p.parent.parent.with_suffix(".enl")`, which yields `<dir>.enl` — it discards
    the library's own name, because `p.parent.parent` is the *containing folder*,
    not the library stem. The correct move is to strip the `.Data` suffix from the
    directory name and append `.enl`.
    """
    for cand in (appdata() / "endnote-mcp" / "config.yaml",
                 Path.home() / ".config" / "endnote-mcp" / "config.yaml"):
        if not cand.is_file():
            continue
        try:
            # utf-8-sig: PowerShell 5.1 and Notepad write a BOM, which would
            # otherwise make the first key fail to match.
            text = cand.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for key in ("endnote_xml", "pdf_dir"):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(f"{key}:"):
                    raw = line.split(":", 1)[1].strip().strip("'\"")
                    if not raw:
                        continue
                    p = Path(raw)
                    if key == "pdf_dir":
                        # Expect <parent>/<lib>.Data/PDF
                        data_dir = p.parent if p.name.lower() == "pdf" else None
                        if data_dir is not None and data_dir.name.endswith(".Data"):
                            stem = data_dir.name[: -len(".Data")]
                            return data_dir.parent / f"{stem}.enl"
                    if key == "endnote_xml" and p.suffix.lower() == ".xml":
                        return p.with_suffix(".enl")
    return None


def _scan_common_locations() -> Path | None:
    """Bounded search of the places EndNote libraries normally live."""
    home = Path.home()
    roots = [
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
        home / "OneDrive" / "Documents",
        home / "OneDrive" / "文档",
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            # Non-recursive first: a library at the top of Documents is the norm.
            found.extend(root.glob("*.enl"))
            # One level down, which covers "Documents/EndNote/My Library.enl".
            for sub in root.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    try:
                        found.extend(sub.glob("*.enl"))
                    except OSError:
                        continue
        except OSError:
            continue
        if found:
            break
    if not found:
        return None
    # Prefer a library whose .Data sibling exists (i.e. a real, non-empty one).
    found.sort(key=lambda p: (not p.with_suffix(".Data").is_dir(), len(str(p))))
    return found[0]


def discover_library() -> tuple[Path, str]:
    """Return (path, how-it-was-found) without requiring the file to exist."""
    v = _env("DSH_ENDNOTE_LIBRARY")
    if v:
        return Path(v).expanduser(), "DSH_ENDNOTE_LIBRARY"
    p = _from_settings_file()
    if p:
        return p, "settings file"
    p = _from_mcp_config()
    if p:
        return p, "endnote-mcp config"
    p = _scan_common_locations()
    if p:
        return p, "auto-detected"
    # Nothing found. Return a conventional path that almost certainly does not
    # exist, so callers fail with a clear message (see require_library) instead of
    # silently operating on the wrong file. The name is EndNote's own default for
    # a new library, not a real user's data.
    return Path.home() / "Documents" / "My EndNote Library.enl", "default (not found)"


LIBRARY, LIBRARY_SOURCE = discover_library()

#: EndNote's unlocked working copy — readable while EndNote holds the .enl lock.
SDB = LIBRARY.with_suffix(".Data") / "sdb" / "sdb.eni"
#: PDF working copy index (full-text cache), also unlocked.
PDB = LIBRARY.with_suffix(".Data") / "sdb" / "pdb.eni"
#: Where EndNote stores attachments: <lib>.Data/PDF/<10-digit folder>/<file>.pdf
PDFS = LIBRARY.with_suffix(".Data") / "PDF"
#: XML export consumed by endnote-mcp.
XML = LIBRARY.with_suffix(".xml")


def require_library() -> Path:
    """The library path, or a clear, actionable error.

    Tools that cannot do anything without a library call this so the user gets one
    readable message instead of an obscure sqlite error.
    """
    if LIBRARY.is_file() or SDB.is_file():
        return LIBRARY
    raise SystemExit(
        "! EndNote library not found.\n"
        f"  looked for: {LIBRARY}  (via {LIBRARY_SOURCE})\n"
        "  Fix by any one of:\n"
        "    - set the plugin's `library` config to your .enl path\n"
        "    - set the DSH_ENDNOTE_LIBRARY environment variable\n"
        f'    - or write {{"library": "<path>"}} to {dsh_home() / "endnote.json"}\n'
        "  (see README, section Configuration)"
    )


# --------------------------------------------------------------- tooling

def staging() -> Path:
    """Folder for downloaded PDFs and generated .enw files.

    Defaults under the harness home rather than any workspace, so a fresh install
    writes somewhere it owns. Created on demand by callers.
    """
    v = _env("DSH_ENDNOTE_STAGING")
    if v:
        return Path(v).expanduser()
    f = dsh_home() / "endnote.json"
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            s = (data.get("stagingDir") or "").strip()
            if s:
                return Path(s).expanduser()
        except (OSError, ValueError):
            pass
    return dsh_home() / "endnote-staging"


STAGING = staging()
#: Generated tagged-import files live here (kept apart from downloaded PDFs).
ENW_DIR = STAGING / "_enw"


def endnote_exe() -> Path:
    """EndNote.EXE, used to hand EndNote a .enw for import.

    Only the conventional install locations are probed; the version is not
    hardcoded to one release.
    """
    v = _env("DSH_ENDNOTE_EXE")
    if v:
        return Path(v)
    roots = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
             Path(os.environ.get("ProgramFiles", r"C:\Program Files"))]
    for root in roots:
        if not root.is_dir():
            continue
        for vendor in sorted(root.glob("EndNote*")):
            exe = vendor / "EndNote.EXE"
            if exe.is_file():
                return exe
    return Path(r"C:\Program Files (x86)\EndNote 21\EndNote.EXE")


ENDNOTE_EXE = endnote_exe()


def python_exe() -> str:
    """Interpreter for the plugin's tool wrappers. Defaults to this one."""
    return _env("DSH_ENDNOTE_PYTHON") or sys.executable


PYTHON = python_exe()


def mcp_db() -> Path:
    """endnote-mcp's SQLite search index."""
    v = _env("DSH_ENDNOTE_MCP_DB")
    if v:
        return Path(v).expanduser()
    return appdata() / "endnote-mcp" / "library.db"


MCP_DB = mcp_db()


def refresh_script() -> Path | None:
    """refresh-endnote-index.ps1, if one ships beside the library.

    Returns None when absent, because the plugin can rebuild the index itself;
    this helper only exists for installs that already have one.
    """
    v = _env("DSH_ENDNOTE_REFRESH")
    if v:
        return Path(v)
    cand = LIBRARY.parent / "refresh-endnote-index.ps1"
    return cand if cand.is_file() else None


REFRESH = refresh_script()


def install_dir() -> Path:
    """EndNote's install root, for the import filter and reference-type table."""
    # ENDNOTE_EXE always has a parent, whether or not the file exists; the
    # fallback already returns a conventional path from endnote_exe().
    return ENDNOTE_EXE.parent


INSTALL = install_dir()
#: Binary filter that defines the .enw tagged format (source of the tag map).
IMPORT_FILTER = INSTALL / "Filters" / "EndNote Import.enf"
#: Field-id -> name table, used to cross-check the tag map.
REFTYPE_TABLE = INSTALL / "XML Support" / "RefTypeTableEN9.xml"


def have_sqlite_source() -> bool:
    """Whether any readable copy of the library exists right now."""
    return LIBRARY.is_file() or SDB.is_file()


def resolve_attachment(file_path: str) -> Path | None:
    """Resolve a `file_res.file_path` to a real path INSIDE the library, or None.

    Two hazards this exists to stop, both verified against a real library:

    1. **Absolute paths replace the base.** EndNote supports linked attachments
       whose `file_path` is absolute. `PDFS / r"C:\\Windows\\Temp\\x.pdf"` yields
       `C:\\Windows\\Temp\\x.pdf` — the base is discarded — so a delete would
       unlink a file that has nothing to do with the library.

    2. **`..` escapes upward.** `PDFS / "..\\..\\thing"` normalises outside the
       PDF tree while still *looking* like a relative path.

    Returning None tells the caller to skip the filesystem operation. The database
    row is still handled, so the record and the file listing stay consistent; only
    the unlink outside the library is refused.

    Containment is checked on the RESOLVED path, so symlinks and `..` are both
    caught.
    """
    if not file_path:
        return None
    raw = str(file_path).strip().replace("\\", os.sep).replace("/", os.sep)

    # An absolute path, or one with a drive/UNC component, is never a library
    # relative path — refuse rather than let pathlib silently rebase it.
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":") or raw.startswith(os.sep * 2):
        return None

    try:
        root = PDFS.resolve()
        candidate = (PDFS / raw).resolve()
    except (OSError, RuntimeError):
        return None

    # Must be the PDF root itself or strictly beneath it.
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def describe() -> str:
    """Human-readable summary, for tool output and doctor runs."""
    lines = [
        f"library   : {LIBRARY}",
        f"  found via: {LIBRARY_SOURCE}",
        f"  .enl     : {'present' if LIBRARY.is_file() else 'MISSING'}",
        f"  sdb.eni  : {'present' if SDB.is_file() else 'MISSING'}",
        f"PDF dir   : {PDFS}",
        f"staging   : {STAGING}",
        f"EndNote   : {ENDNOTE_EXE}",
        f"python    : {PYTHON}",
        f"mcp index : {MCP_DB}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
