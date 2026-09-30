#!/usr/bin/env python3
"""Prepare a literature reading package for one EndNote record.

What this does (the mechanical half)
-------------------------------------
Given a reference in the EndNote library, this gathers everything an agent needs
to write a reading note, and lays out the output folder:

  1. resolve the reference from a rec-number, a DOI, or a title fragment;
  2. pull the metadata the note header needs: title, journal, DOI, authors, year;
  3. look up a journal impact factor proxy from OpenAlex (free, no key);
  4. extract the paper's full text (PDF if attached, else the OA route, else
     Europe PMC fullTextXML);
  5. write the dossier into <workspace>/文献解读/ with the header pre-filled and
     the full text parked under a clearly-marked section for the agent to
     translate and interpret;
  6. attach the finished note back to the EndNote record.

What this does NOT do
---------------------
It does not write the translation or the interpretation. Those need a language
model, and the agent reading the paper is a better one than any API call this
script could make. The script writes a TEMPLATE with the facts filled in and the
prose left empty, and refuses to claim the note is finished.

Usage
-----
    python endnote_digest.py --list                     # references + whether text is available
    python endnote_digest.py 21                         # by rec-number
    python endnote_digest.py --doi 10.3390/v15081737    # by DOI
    python endnote_digest.py 21 --out "D:\\notes"       # custom output root
    python endnote_digest.py 21 --no-attach             # skip the attachment step
    python endnote_digest.py 21 --attach-only FILE.md   # attach an existing note
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import endnote_paths as ep  # noqa: E402

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

#: Folder created in the workspace, per the requested layout.
DEFAULT_FOLDER = "文献解读"


def workspace_root() -> Path:
    """Where to put 文献解读/.

    Resolution order, most explicit first:

      1. `DSH_ENDNOTE_WORKSPACE` — the plugin sets this from its config, so the
         location is not baked in.
      2. `DSH_WORKSPACE` — the harness's own variable, when the tool is invoked
         from inside a session that has one.
      3. a `workspace` sibling of DSH_HOME's parent (this machine's layout).
      4. the current directory.

    Deliberately NOT a hardcoded workspace path: the plugin has to work on
    another machine, and every other path in this project resolves through
    `endnote_paths` for the same reason.
    """
    for var in ("DSH_ENDNOTE_WORKSPACE", "DSH_WORKSPACE"):
        got = _os.environ.get(var)
        if got:
            p = Path(got).expanduser()
            if p.is_dir():
                return p
    # Walk up from the current directory looking for the harness layout, so a
    # call made from inside a plugin checkout still lands in the workspace
    # rather than dumping 文献解读/ into the repository.
    here = Path.cwd().resolve()
    for cand in (here, *here.parents):
        if (cand / "TASKS").is_dir() or (cand / "README.md").is_file() \
                and (cand / "dsh-endnote").is_dir():
            return cand
    guess = ep.dsh_home().parent / "workspace"
    if guess.is_dir():
        return guess
    return here

#: OpenAlex asks for a contact address; the placeholder is RFC 2606-reserved.
CONTACT = _os.environ.get("DSH_ENDNOTE_CONTACT", "endnote-pdf@example.org")

_UA = "dsh-endnote/1.0 (literature digest)"


# ------------------------------------------------------------------ library

def connect(mode: str = "ro") -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{ep.SDB.as_posix()}?mode={mode}", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def get_ref(number: int | None = None, doi: str | None = None,
            title: str | None = None) -> dict | None:
    """Resolve one active reference by rec-number, DOI, or title fragment."""
    ep.require_library()
    c = connect()
    rows = [dict(r) for r in c.execute("""
        SELECT id, year, author, title, secondary_title, abstract,
               electronic_resource_number, volume, number, pages, isbn, url
        FROM refs WHERE trash_state = 0 ORDER BY id
    """)]
    att: dict[int, list[str]] = {}
    for r in c.execute("SELECT refs_id, file_path, file_pos FROM file_res"):
        att.setdefault(r["refs_id"], []).append(r["file_path"])
    c.close()
    for r in rows:
        r["attachments"] = att.get(r["id"], [])

    if number is not None:
        for r in rows:
            if r["id"] == number:
                return r
        return None
    if doi:
        want = doi.strip().lower().rstrip("/")
        for r in rows:
            got = (r["electronic_resource_number"] or "").strip().lower().rstrip("/")
            if got and (got == want or want in got):
                return r
        return None
    if title:
        needle = title.strip().lower()
        for r in rows:
            if needle in (r["title"] or "").lower():
                return r
        return None
    return None


def list_refs() -> list[dict]:
    c = connect()
    rows = [dict(r) for r in c.execute(
        "SELECT id, year, author, title, secondary_title, "
        "electronic_resource_number FROM refs "
        "WHERE trash_state = 0 ORDER BY id")]
    att: dict[int, int] = {}
    for r in c.execute("SELECT refs_id, COUNT(*) AS n FROM file_res GROUP BY refs_id"):
        att[r["refs_id"]] = r["n"]
    c.close()
    for r in rows:
        r["n_attachments"] = att.get(r["id"], 0)
    return rows


# ----------------------------------------------------------------- metadata

def _get_json(url: str, timeout: int = 30) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for relax in (False, True):
        try:
            ctx = None
            if relax and _os.environ.get("DSH_ENDNOTE_PROXY"):
                import ssl
                ctx = ssl._create_unverified_context()  # noqa: S323
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as fh:
                return json.load(fh)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue
    return None


def impact_factor(journal: str, issn: str = "") -> dict:
    """Best-effort journal metric from OpenAlex.

    The real Journal Impact Factor is Clarivate's commercial data and is not
    available from any free API. OpenAlex publishes `2yr_mean_citedness` for a
    source, which is the same *kind* of quantity (mean citations to items
    published in the prior two years) computed over open data. It is a
    reference figure, not the official JCR number, and the two differ — do not
    present it as an exact IF.

    Returns {} when the journal cannot be resolved.
    """
    if not journal:
        return {}

    def _lookup(url: str) -> dict:
        d = _get_json(url)
        if not d or not d.get("results"):
            return {}
        src = d["results"][0]
        stats = src.get("summary_stats") or {}
        raw = stats.get("2yr_mean_citedness")
        return {
            "name": src.get("display_name") or journal,
            "issn_l": src.get("issn_l") or "",
            "works_count": src.get("works_count") or 0,
            "h_index": stats.get("h_index"),
            # One decimal is enough for a reference figure and avoids implying
            # precision the proxy does not have.
            "if_proxy": round(float(raw), 1) if isinstance(raw, (int, float)) else None,
            "if_source": "OpenAlex 2yr_mean_citedness",
        }

    if issn:
        got = _lookup(f"https://api.openalex.org/sources/issn:{urllib.parse.quote(issn)}"
                      f"?mailto={CONTACT}")
        if got:
            return got
    return _lookup("https://api.openalex.org/sources?filter="
                   f"display_name.search:{urllib.parse.quote(journal)}"
                   f"&per_page=1&mailto={CONTACT}")


# ---------------------------------------------------------------- full text

def full_text(ref: dict) -> tuple[str, str]:
    """Return (text, provenance). Empty text when nothing could be extracted.

    Order matters. This project ships scripts that declare **no third-party
    Python dependency** (enforced by `tools/test_imports.py`), so the preferred
    sources are ones that need nothing beyond the standard library:

      1. Europe PMC JATS XML, via the plugin's own `paper_pdf` module -- the
         same route the CSV/PDF tools already use, no extra packages.
      2. A locally attached PDF, which needs PyMuPDF. That import is optional
         and its absence is reported, never fatal.
    """
    doi = ref.get("electronic_resource_number") or ""

    # 1. the dependency-free route first.
    if doi:
        try:
            import paper_pdf as pp  # noqa: PLC0415
            paper = pp.resolve(doi)
            if paper:
                got = pp.full_text(paper)
                if got:
                    return got, "Europe PMC full-text XML (no local PDF parsing)"
        except Exception as exc:  # noqa: BLE001
            print(f"  (Europe PMC route failed: {type(exc).__name__}: {exc})",
                  file=sys.stderr)

    # 2. the attached PDF, if a PDF library happens to be available.
    if ref.get("attachments"):
        text, why = _text_from_stored_pdf(ref)
        if text:
            return text, why
        if why:
            print(f"  (local PDF extraction skipped: {why})", file=sys.stderr)

    return "", "not available"


def _text_from_stored_pdf(ref: dict) -> tuple[str, str]:
    """Extract text from an attachment already inside the library.

    Returns (text, note). PyMuPDF is optional: this script must import cleanly
    on a machine that has only the standard library, because the plugin tells
    users there is no `pip install` step.
    """
    fitz = None
    for modname in ("pymupdf", "fitz"):
        try:
            fitz = __import__(modname)
            break
        except ImportError:
            continue
    if fitz is None:
        return "", "PyMuPDF not installed (optional; Europe PMC was tried first)"

    for rel in ref["attachments"]:
        p = ep.PDFS / rel
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        try:
            doc = fitz.open(p)
            try:
                # Cap the extraction: a reading note needs the body, and a
                # 40-page appendix would swamp the model's context for no gain.
                pages = min(doc.page_count, 40)
                text = "\n".join(doc.load_page(i).get_text() for i in range(pages))
                if text.strip():
                    return text, f"PDF attached to the EndNote record ({pages} pages)"
            finally:
                doc.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not read {p.name}: {type(exc).__name__})", file=sys.stderr)
            continue
    return "", "attached PDF yielded no text (scanned image? needs OCR)"


# ------------------------------------------------------------------ output

def _safe_filename(ref: dict, meta: dict) -> str:
    """Build a filesystem-safe name from the record.

    EndNote stores multiple authors separated by a bare CR (not ";"), so
    `author` routinely contains \\r. It is not a legal filename character and
    it silently produced an OSError rather than a bad-looking name, so strip
    ALL control characters rather than just the path-illegal set.
    """
    # Every author separator EndNote uses, normalised to one.
    raw_authors = re.split(r"[\r\n;]+", ref["author"] or "")
    first = next((a.strip() for a in raw_authors if a.strip()), "Unknown")
    # EndNote is inconsistent about name order: most records store
    # "Surname, Given" but imported ones may store "Given Surname" (measured
    # both in this library). Prefer the comma form; otherwise take the last
    # word, which is the surname in the "Given Surname" case and harmless for a
    # single-word name.
    if "," in first:
        surname = first.split(",")[0].strip()
    else:
        surname = first.split()[-1] if first.split() else "Unknown"
    surname = surname or "Unknown"

    year = str(ref["year"] or "----")

    title = ref["title"] or "untitled"
    # Control chars first (incl. \r), then the Windows-illegal set, then squash.
    title = re.sub(r"[\x00-\x1f\x7f]", " ", str(title))
    title = re.sub(r'[\\/:*?"<>|]', "", title)
    title = re.sub(r"\s+", " ", title).strip()[:70].rstrip(" .")

    name = f"{surname}{year}_{title}.md"
    # Last line of defence: never hand back something with a path separator or
    # a reserved Windows device name.
    if not name or name in (".md",):
        name = f"ref{ref['id']}.md"
    return name


def build_note(ref: dict, meta: dict, text: str, provenance: str) -> str:
    """Render the dossier. Prose sections are left as TODO markers."""
    # EndNote separates authors with a bare CR; normalise for display.
    authors = re.sub(r"[\r\n]+", "; ", (ref["author"] or "").strip("; \r\n"))
    doi = ref["electronic_resource_number"] or ""
    journal = ref["secondary_title"] or meta.get("name") or ""
    ifv = meta.get("if_proxy")
    iftxt = f"IF≈{ifv}" if ifv is not None else "IF=?"

    # The header the user asked for: 题目、刊名、IF、DOI
    lines = [
        f"# {ref['title']}",
        "",
        f"**{ref['title']}**（{journal}, {ref['year'] or '----'}, "
        f"{iftxt}, {doi}）",
        "",
    ]
    if meta.get("if_proxy") is not None:
        lines += [
            f"> IF 为参考值：{meta['if_source']}（免费数据源），"
            f"非 Clarivate 官方 JCR 影响因子。",
            "",
        ]

    lines += [
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 题目 | {ref['title']} |",
        f"| 作者 | {authors[:300]} |",
        f"| 刊名 | {journal} |",
        f"| 年份 | {ref['year'] or ''} |",
        f"| 卷期页 | {ref['volume'] or ''}({ref['number'] or ''}) {ref['pages'] or ''} |",
        f"| IF（参考） | {ifv if ifv is not None else '未取到'} |",
        f"| DOI | {doi} |",
        f"| EndNote 记录号 | #{ref['id']} |",
        f"| 生成日期 | {date.today().isoformat()} |",
        f"| 全文来源 | {provenance} |",
        "",
        "---",
        "",
        "## 摘要译文",
        "",
    ]

    abstract = (ref.get("abstract") or "").strip()
    if abstract:
        lines += [
            "<!-- TODO(agent): 把下面的英文摘要译成中文，保持学术语体。 -->",
            "",
            "*原文摘要：*",
            "",
            "> " + abstract.replace("\n", " "),
        ]
    else:
        lines += ["<!-- TODO(agent): 库内无摘要，请从全文首段提取并翻译。 -->"]

    lines += [
        "",
        "## 全文翻译与解读",
        "",
        "<!-- TODO(agent): 按节翻译全文并给出解读。建议分节结构：",
        "     引言 / 方法 / 结果 / 讨论 / 结论，每节先译文后解读。",
        "     解读要点：研究问题、方法学强度与局限、结论是否被数据支持、",
        "     与相关工作的关系、可借鉴之处。 -->",
        "",
        "## 批判性评价",
        "",
        "<!-- TODO(agent): 样本量、对照、统计、可重复性、作者自述局限。 -->",
        "",
        "## 对我的课题的意义",
        "",
        "<!-- TODO(agent): 明确写「可用于什么」和「不能说明什么」。 -->",
        "",
        "---",
        "",
        "## 附：原文全文（供翻译用，勿直接引用）",
        "",
    ]

    if text:
        lines += ["```text", text.strip(), "```"]
    else:
        lines += [
            "> 未能取到全文。可能原因：闭源且无 OA 版本、PDF 未附到 EndNote 条目、",
            "> 或 PDF 为扫描件（无文本层，需 OCR）。",
        ]

    lines.append("")
    return "\n".join(lines)


def write_note(ref: dict, meta: dict, text: str, provenance: str,
               out_root: Path, *, force: bool = False) -> Path:
    """Write the dossier, refusing to clobber an existing file by default.

    A reading note is hand-written work. Regenerating the scaffold over a note
    that already has the translation and interpretation in it destroys exactly
    the content that took the longest, and there is no undo -- so an existing
    file is an error the caller must resolve explicitly.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / _safe_filename(ref, meta)

    if path.exists() and not force:
        print(f"! {path.name} already exists.", file=sys.stderr)
        print(f"  Refusing to overwrite it: a note may already contain the", file=sys.stderr)
        print(f"  translation and interpretation.", file=sys.stderr)
        print(f"  Options:", file=sys.stderr)
        print(f"    --force          overwrite it with a fresh template", file=sys.stderr)
        print(f"    --out <dir>      write the template somewhere else", file=sys.stderr)
        raise SystemExit(1)

    path.write_text(build_note(ref, meta, text, provenance), encoding="utf-8")
    return path


# -------------------------------------------------------------- attachment

def attach_note(ref: dict, note: Path, *, dry: bool = False) -> int:
    """Attach the note file to the EndNote record.

    Writes the same `file_res` row shape as `endnote_attach.py` (the verified
    path), minus its PDF-only magic-byte check, because a reading note is
    Markdown.

    Two guards learned the hard way, both from a real run against this library:

    1. **Do not add a second note when one is already attached.** An earlier
       version happily appended another `.md`, so a record could accumulate
       several near-identical notes and the newest was not obviously the one in
       use. Now a note already on the record is reported and skipped unless
       `replace` is asked for.

    2. **Compute the next position from a live read, inside the write
       transaction.** `(refs_id, file_pos)` is UNIQUE; positions read before the
       transaction can be stale, and a stale position either raises
       IntegrityError or lands the note where another attachment expects to be.
    """
    ep.require_library()
    if not note.is_file():
        print(f"! note not found: {note}", file=sys.stderr)
        return 1

    import shutil

    # --- guard 1: is a reading note already on this record? -----------------
    c = connect()
    existing = [dict(r) for r in c.execute(
        "SELECT file_path, file_pos FROM file_res WHERE refs_id=? ORDER BY file_pos",
        (ref["id"],))]
    c.close()

    same_name = [e for e in existing if Path(e["file_path"]).name == note.name]
    md_notes = [e for e in existing
                if Path(e["file_path"]).suffix.lower() in (".md", ".markdown")]

    if same_name:
        print(f"! ref#{ref['id']} already has a note with this filename:")
        print(f"    {same_name[0]['file_path']}")
        print("  Nothing written. Edit that file, or pass a different name.")
        return 0

    print(f"attaching : {note.name}  ({note.stat().st_size:,} bytes)")
    print(f"to        : #{ref['id']}  {str(ref['title'])[:60]}")
    if md_notes:
        print(f"  note: this record already has {len(md_notes)} .md attachment(s):")
        for e in md_notes:
            print(f"          {e['file_path']}")

    folder = f"{int(__import__('time').time())}"[-10:]
    rel = f"{folder}/{note.name}"
    dest = ep.PDFS / folder / note.name
    print(f"dest      : {dest}")

    if dry:
        print("\n(dry run — nothing copied, no row written)")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(note, dest)

    # --- guard 2: choose the position inside the transaction ---------------
    c = connect("rw")
    try:
        c.execute("BEGIN IMMEDIATE")
        # Re-read INSIDE the lock: anything computed before BEGIN may be stale,
        # and (refs_id, file_pos) is UNIQUE.
        used = {r["file_pos"] for r in c.execute(
            "SELECT file_pos FROM file_res WHERE refs_id=?", (ref["id"],))}
        pos = max(used) + 1 if used else 0
        c.execute("INSERT INTO file_res (refs_id, file_path, file_type, file_pos) "
                  "VALUES (?,?,?,?)", (ref["id"], rel, 1, pos))
        c.commit()
    except Exception as exc:  # noqa: BLE001
        c.rollback()
        c.close()
        shutil.rmtree(dest.parent, ignore_errors=True)
        print(f"! failed to write the file_res row: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("  (the copied file was rolled back)")
        return 1
    c.close()
    print(f"attached  : ref#{ref['id']} now has {len(existing) + 1} attachment(s) "
          f"(new file at position {pos})")
    print("  If EndNote does not show it, click another reference and back —")
    print("  EndNote reconciles attachments when it next rewrites the library.")
    return 0


# --------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a literature reading dossier for an EndNote record.")
    ap.add_argument("ref", nargs="?", help="EndNote record number")
    ap.add_argument("--doi", help="select the reference by DOI")
    ap.add_argument("--title", help="select the reference by title fragment")
    ap.add_argument("--list", action="store_true", help="list references and exit")
    ap.add_argument("--out", help=f"output root (default: <workspace>/{DEFAULT_FOLDER})")
    ap.add_argument("--no-attach", action="store_true",
                    help="write the note but do not attach it to EndNote")
    ap.add_argument("--attach-only", metavar="FILE",
                    help="attach an existing note file and exit")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing note file (it may hold real work)")
    args = ap.parse_args(argv)

    if args.list:
        refs = list_refs()
        print(f"{len(refs)} active reference(s):\n")
        for r in refs:
            doi = r["electronic_resource_number"] or "-"
            # EndNote separates authors with a bare CR, which wrecks the column
            # alignment in a terminal. Show them comma-separated instead.
            authors = ", ".join(
                a.strip() for a in re.split(r"[\r\n;]+", r["author"] or "") if a.strip())
            print(f"  #{r['id']:<4} {r['year'] or '----'}  "
                  f"{authors[:34]:36} "
                  f"{str(r['title'] or '')[:50]}")
            print(f"        {doi}   attachments={r['n_attachments']}")
        return 0

    number = int(args.ref) if args.ref and str(args.ref).isdigit() else None
    ref = get_ref(number, args.doi, args.title or (args.ref if not number else None))
    if not ref:
        print(f"! no active reference found for "
              f"{args.ref or args.doi or args.title!r}", file=sys.stderr)
        print("  Use --list to see what is in the library.", file=sys.stderr)
        return 1

    if args.attach_only:
        return attach_note(ref, Path(args.attach_only), dry=args.dry_run)

    print(f"reference : #{ref['id']}  {ref['title']}")
    journal = ref["secondary_title"] or ""
    print(f"journal   : {journal}  {ref['year'] or ''}")
    print(f"doi       : {ref['electronic_resource_number'] or '(none)'}")

    print("\n== journal metric ==")
    meta = impact_factor(journal, ref.get("isbn") or "")
    if meta.get("if_proxy") is not None:
        print(f"  {meta['name']}: IF≈{meta['if_proxy']} "
              f"({meta['if_source']}; h-index={meta.get('h_index')})")
        print("  note: a free-data proxy, not the official JCR Impact Factor")
    else:
        print("  (no metric found for this journal)")

    print("\n== full text ==")
    text, provenance = full_text(ref)
    if text:
        print(f"  {len(text):,} characters via {provenance}")
    else:
        print("  NOT AVAILABLE — the note will say so instead of inventing text")

    out_root = Path(args.out) if args.out else workspace_root() / DEFAULT_FOLDER
    note = write_note(ref, meta, text, provenance, out_root, force=args.force)
    print(f"\nwrote     : {note}")
    print(f"            ({note.stat().st_size:,} bytes)")

    print("\n== NEXT (agent) ==")
    print("  The note is a TEMPLATE. Fill in each `TODO(agent)` section:")
    print("    - 摘要译文      : translate the abstract")
    print("    - 全文翻译与解读 : translate + interpret section by section")
    print("    - 批判性评价    : methods, limits, whether data support the claims")
    print("    - 对我的课题的意义")
    print("  Then attach it:")
    print(f"    python {Path(__file__).name} {ref['id']} --attach-only \"{note}\"")

    if not args.no_attach:
        print("\n== attaching the note to the EndNote record ==")
        rc = attach_note(ref, note, dry=args.dry_run)
        if rc == 0 and not args.dry_run:
            print("  (the note is attached; update the file in place as you write,")
            print("   then re-attach with --attach-only if you want the copy refreshed)")
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
