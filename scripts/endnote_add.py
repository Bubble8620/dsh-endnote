#!/usr/bin/env python3
"""Add a paper to the EndNote library by DOI, PMID, title, or URL.

Why this exists
---------------
EndNote 21 keeps its library in a SQLite `.enl` that it holds with an exclusive
lock, and it publishes no write API (the only registered COM object,
`EndNote21.AddinServer`, exposes no usable methods). So nothing can insert a
record behind EndNote's back. What DOES work is EndNote's own importer: the
registry associates `.enw` files with `EndNote.EXE "%1"`, so handing EndNote a
tagged `.enw` file makes it import the record using its own code — no schema
risk, no lock fight.

Pipeline
--------
    identifier -> metadata (Crossref / Europe PMC / OpenAlex)
               -> .enw tagged file (tags taken from EndNote's own filter)
               -> hand to EndNote.EXE
               -> (optional) fetch the open-access PDF

The tag -> field mapping is not guessed: `--tags` prints it as decoded from
EndNote's binary filter `Filters/EndNote Import.enf`, cross-referenced with
`XML Support/RefTypeTableEN9.xml`.

Usage
-----
    python endnote_add.py 10.1038/nature12373
    python endnote_add.py "Nanometre-scale thermometry in a living cell"
    python endnote_add.py 10.1038/nature12373 --pdf
    python endnote_add.py 10.1038/nature12373 --dry-run
    python endnote_add.py --verify 10.1038/nature12373
    python endnote_add.py --list
    python endnote_add.py --tags

Two things about EndNote that the code works around
---------------------------------------------------
* **A minimized EndNote silently ignores the import.** Verified: the .enl stayed
  byte-identical until EndNote was brought to the foreground. So after launching
  we explicitly restore + focus its window, and `--verify` exists to confirm the
  record really landed.
* **The .enl is exclusively locked while EndNote runs** (mode=ro and even
  immutable=1 both fail). Reads therefore fall back to the unlocked mirror
  `<lib>.Data\\sdb\\sdb.eni`, which carries the same `refs`/`file_res` tables.
  That is what lets `--verify` work, and what makes
  `refresh-endnote-index.ps1` usable without closing EndNote.
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



import os


import argparse
import json
import re
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- constants

UA = "endnote-add/1.0 (mailto:endnote-add@localhost)"
CONTACT = "endnote-add@localhost"
TIMEOUT = 30

STAGING = ep.STAGING
ENW_DIR = ep.ENW_DIR
ENDNOTE_EXE = ep.ENDNOTE_EXE

# Tagged-format codes, decoded from EndNote's own "EndNote Import.enf".
TAGS = {
    "author": "%A",
    "title": "%T",
    "journal": "%J",
    "year": "%D",
    "volume": "%V",
    "issue": "%N",
    "pages": "%P",
    "doi": "%R",          # Electronic Resource Number
    "abstract": "%X",
    "keywords": "%K",
    "url": "%U",
    "issn": "%@",
    "label": "%F",
    "notes": "%Z",
    "research_notes": "%<",
    "link_pdf": "%>",     # Link to PDF
    "ref_type": "%0",
}

# Optional HTTP(S) proxy. Empty by default: most machines need none, and
# assuming one leaks the author's setup and can mis-route requests.
PROXY = os.environ.get("DSH_ENDNOTE_PROXY", "")


# ---------------------------------------------------------------- transport

def _ssl_ctx(relax: bool = False) -> ssl.SSLContext:
    """TLS context. Verification is relaxed ONLY for a proxied connection.

    An intercepting proxy terminates TLS with its own CA, which the system trust
    store does not know — that was the original reason verification was disabled.
    But an earlier version decided this from `if PROXY:` when building the
    context, so merely *setting* DSH_ENDNOTE_PROXY disabled verification for every
    request in the process, including the direct ones. Making the relaxation a
    property of the leg being attempted keeps it where it is justified.
    """
    ctx = ssl.create_default_context()
    if relax:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _proxy_attempts(use_proxy: bool | None) -> list[bool]:
    """Which transports to try. The proxy leg exists only when one is configured.

    PROXY defaults to empty, so a normal machine makes a single direct request
    instead of pointlessly trying a port that nothing is listening on.
    """
    if use_proxy is not None:
        return [use_proxy]
    return [False, True] if PROXY else [False]


def http_json(url: str, *, use_proxy: bool | None = None) -> dict | None:
    """GET a URL and parse JSON. Direct first, then the configured proxy."""
    last_err: Exception | None = None
    for prox in _proxy_attempts(use_proxy):
        # `relax=prox`: the direct leg stays fully verified even with a proxy set.
        handlers = [urllib.request.HTTPSHandler(context=_ssl_ctx(relax=prox))]
        if prox:
            handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))  # ignore env proxies
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except Exception as exc:      # noqa: BLE001 - fall through to next transport
            last_err = exc
    print(f"  ! request failed: {url}\n    {type(last_err).__name__}: {last_err}",
          file=sys.stderr)
    return None


def http_download(url: str, dest: Path, *, quiet: bool = False) -> bool:
    """Download a PDF. Returns True when a real PDF landed on disk.

    Sends browser-like headers because several publishers reject the default
    urllib agent outright. NOTE: some hosts (MDPI, ACS, RSC, europepmc.org)
    answer 403 to any non-browser client regardless of headers — verified
    against every variant tried — so callers should try several candidate URLs
    (see fetch_pdf) rather than expecting one to work. `quiet` suppresses the
    per-attempt diagnostics so a multi-candidate run stays readable.
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last: Exception | None = None
    for prox in _proxy_attempts(None):
        # Download leg: same rule — only the proxied attempt relaxes verification.
        handlers = [urllib.request.HTTPSHandler(context=_ssl_ctx(relax=prox))]
        if prox:
            handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=120) as r:
                blob = r.read()
            if not blob.startswith(b"%PDF"):
                if not quiet:
                    print(f"  ! not a PDF (starts {blob[:5]!r}) — publisher served a web page",
                          file=sys.stderr)
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            return True
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 403:
                continue  # blocked; the proxy may present a different origin
        except Exception as exc:      # noqa: BLE001
            last = exc
    if not quiet:
        print(f"  ! download failed: {type(last).__name__}: {last}", file=sys.stderr)
    return False


def pdf_candidates(ref: Ref) -> list[str]:
    """Ordered candidate URLs to try for an open-access PDF.

    DELEGATES TO paper_pdf WHERE POSSIBLE. There used to be a second, weaker
    implementation here that only tried four URLs and knew nothing about
    repository mirrors, so `endnote_add.py` silently failed to attach PDFs that
    `paper_pdf.py` could fetch. Verified: ACS 10.1021/acssynbio.2c00465 gave
    "every source declined" here while paper_pdf.py downloaded 2.9 MB from
    an institutional repository. One implementation now serves both.
    """
    urls = _paper_pdf_candidates(ref)
    if urls:
        return urls
    # Fallback if paper_pdf is unavailable for any reason.
    out: list[str] = []
    if ref.oa_pdf:
        out.append(ref.oa_pdf)
    if ref.pmcid:
        out.append(f"https://europepmc.org/articles/{ref.pmcid}?pdf=render")
    if ref.doi:
        out.append(f"https://doi.org/{ref.doi}")
    seen: set[str] = set()
    return [u for u in out if u and not (u in seen or seen.add(u))]


def _paper_pdf_candidates(ref: Ref) -> list[str]:
    """Ask paper_pdf for its ranked candidates, working hosts first.

    paper_pdf owns the route knowledge (arXiv, repository mirrors via
    Unpaywall/OpenAlex, Semantic Scholar), so endnote_add reuses it rather than
    duplicating a stale copy of that logic.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import paper_pdf as pp  # noqa: PLC0415
    except Exception:
        return []

    paper = pp.Paper(
        doi=ref.doi, title=ref.title, year=ref.year, authors=list(ref.authors),
        journal=ref.journal, pmcid=ref.pmcid,
    )
    # oa_pdf first: it is the publisher's version of record when it works.
    if ref.oa_pdf:
        paper.add(ref.oa_pdf, "epmc/publisher")
    if ref.pmcid:
        paper.xml_url = (f"https://www.ebi.ac.uk/europepmc/webservices/rest/"
                         f"{ref.pmcid}/fullTextXML")
    # Enrich from the services paper_pdf already knows how to query.
    try:
        pp._from_openalex(paper)
        pp._from_unpaywall(paper)
        pp._from_arxiv(paper)
        pp._from_semanticscholar(paper)
    except Exception:
        pass
    return [u for u, _src in paper.ordered()]



def _has_oa_evidence(ref: Ref) -> bool:
    """Does any service indicate an open-access copy exists?

    Used only for messaging: a paper with no OA evidence at all is paywalled,
    which is a different situation (and different advice) from every known OA
    mirror refusing us. Note `pdf_candidates` always appends a bare doi.org URL
    as a last resort, so its length is NOT evidence of an OA copy.
    """
    if ref.oa_pdf or ref.pmcid:
        return True
    return bool(_paper_pdf_candidates(ref))


def _try_fulltext(ref: Ref) -> Path | None:
    """Save the paper's full text when no PDF can be downloaded.

    Delegates to paper_pdf, which reads Europe PMC's fullTextXML — reachable even
    when europepmc.org's PDF endpoint 403s. Returns the .md path, or None when the
    paper has no PMC full text. Never feeds %> (that field needs a PDF).
    """
    if not ref.pmcid:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import paper_pdf as pp  # noqa: PLC0415
        paper = pp.Paper(doi=ref.doi, title=ref.title, year=ref.year,
                         authors=list(ref.authors), journal=ref.journal,
                         pmcid=ref.pmcid)
        paper.xml_url = (f"https://www.ebi.ac.uk/europepmc/webservices/rest/"
                         f"{ref.pmcid}/fullTextXML")
        txt = pp.full_text(paper)
        if not txt:
            return None
        safe = re.sub(r"[^\w.\- ]+", "_",
                      f"{ref.authors[0].split()[-1] if ref.authors else 'paper'}"
                      f"_{ref.year}_{ref.title[:70]}").strip() or "paper"
        dest = STAGING / (safe[:120] + ".md")
        dest.write_text(f"# {ref.title}\n\nDOI: {ref.doi}\n\n{txt}\n", encoding="utf-8")
        return dest
    except Exception:
        return None


def fetch_pdf(ref: Ref, dest: Path) -> Path | None:
    """Try each candidate until real PDF bytes land. Returns the path or None."""
    cands = pdf_candidates(ref)
    for i, url in enumerate(cands, 1):
        host = urllib.parse.urlparse(url).netloc
        note = f"   [{i}/{len(cands)}] {host}"
        if http_download(url, dest, quiet=True):
            print(f"{note} -> OK ({dest.stat().st_size:,} bytes)")
            return dest
        print(f"{note} -> no")
    return None


# ---------------------------------------------------------------- metadata

@dataclass
class Ref:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    url: str = ""
    issn: str = ""
    ref_type: str = "Journal Article"
    oa_pdf: str = ""
    pmcid: str = ""
    source: str = ""

    def missing(self) -> list[str]:
        out = []
        if not self.title:
            out.append("title")
        if not self.authors:
            out.append("authors")
        if not self.year:
            out.append("year")
        return out


def _clean(text: str | None) -> str:
    """Strip markup from a metadata value.

    Order matters: entities must be unescaped BEFORE tag stripping. Sources like
    Europe PMC return double-escaped markup (`&lt;i&gt;Campylobacter&lt;/i&gt;`),
    so stripping tags first would leave a literal `<i>` in the title.
    """
    if not text:
        return ""
    text = str(text)
    for _ in range(2):  # handle double-escaped markup
        text = (text.replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&#39;", "'")
                    .replace("&apos;", "'").replace("&nbsp;", " ")
                    .replace("&amp;", "&"))
    text = re.sub(r"<[^>]+>", " ", text)
    # Any residual entity (e.g. numeric) is not worth keeping as markup.
    text = re.sub(r"&[a-zA-Z#][a-zA-Z0-9]{1,8};", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def from_crossref(doi: str) -> Ref | None:
    d = http_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    if not d or "message" not in d:
        return None
    m = d["message"]

    authors = []
    for a in m.get("author", []) or []:
        given, family = a.get("given", ""), a.get("family", "")
        name = " ".join(x for x in (given, family) if x).strip()
        if not name and a.get("name"):
            name = a["name"]
        if name:
            authors.append(name)

    year = ""
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (m.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break

    ctype = (m.get("type") or "").lower()
    ref_type = {
        "journal-article": "Journal Article",
        "book-chapter": "Book Section",
        "book": "Book",
        "proceedings-article": "Conference Paper",
        "posted-content": "Journal Article",
        "report": "Report",
        "dissertation": "Thesis",
    }.get(ctype, "Journal Article")

    return Ref(
        title=_clean((m.get("title") or [""])[0]),
        authors=authors,
        journal=_clean((m.get("container-title") or [""])[0]),
        year=year,
        volume=str(m.get("volume") or ""),
        issue=str(m.get("issue") or ""),
        pages=str(m.get("page") or ""),
        doi=(m.get("DOI") or doi).lower(),
        abstract=_clean(m.get("abstract")),
        keywords=[_clean(k) for k in (m.get("subject") or []) if _clean(k)],
        url=str(m.get("URL") or f"https://doi.org/{doi}"),
        issn=str((m.get("ISSN") or [""])[0]),
        ref_type=ref_type,
        source="Crossref",
    )


def from_europepmc(query: str) -> Ref | None:
    """Works for a PMID, a PMC id, or a free-text title query."""
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode({"query": query, "format": "json",
                                     "resultType": "core", "pageSize": "1"}))
    d = http_json(url)
    if not d:
        return None
    results = ((d.get("resultList") or {}).get("result") or [])
    if not results:
        return None
    r = results[0]

    ji = r.get("journalInfo") or {}
    jr = ji.get("journal") or {}
    authors = []
    for a in ((r.get("authorList") or {}).get("author") or []):
        nm = a.get("fullName") or " ".join(
            x for x in (a.get("firstName", ""), a.get("lastName", "")) if x).strip()
        if nm:
            authors.append(nm)

    doi = (r.get("doi") or "").lower()
    pmcid = str(r.get("pmcid") or "")
    # Europe PMC exposes a render endpoint whenever it holds the full text.
    oa_pdf = ""
    if pmcid:
        oa_pdf = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if u.get("documentStyle") == "pdf" and u.get("url"):
            oa_pdf = u["url"]
            break

    return Ref(
        title=_clean(r.get("title")),
        authors=authors,
        journal=_clean(jr.get("title")),
        year=str(r.get("pubYear") or ""),
        volume=str(ji.get("volume") or ""),
        issue=str(ji.get("issue") or ""),
        pages=_clean(r.get("pageInfo")),
        doi=doi,
        abstract=_clean(r.get("abstractText")),
        keywords=[_clean(k) for k in (r.get("keywordList") or {}).get("keyword", []) if _clean(k)],
        url=f"https://doi.org/{doi}" if doi else (r.get("fullTextUrlList") or {}).get("fullTextUrl", [{}])[0].get("url", ""),
        issn=str(jr.get("issn") or ""),
        oa_pdf=oa_pdf,
        pmcid=pmcid,
        source="Europe PMC",
    )


def fill_from_openalex(ref: Ref) -> None:
    """Fill gaps and pick up an OA PDF link."""
    if ref.doi:
        d = http_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(ref.doi)}")
    else:
        d = http_json("https://api.openalex.org/works?"
                      + urllib.parse.urlencode({"search": ref.title, "per-page": "1"}))
        d = ((d or {}).get("results") or [None])[0]
    if not d:
        return

    if not ref.abstract and d.get("abstract_inverted_index"):
        pos: dict[int, str] = {}
        for word, idxs in d["abstract_inverted_index"].items():
            for i in idxs:
                pos[i] = word
        ref.abstract = " ".join(pos[i] for i in sorted(pos))[:4000]

    if not ref.authors:
        ref.authors = [a["author"]["display_name"]
                       for a in (d.get("authorships") or []) if a.get("author", {}).get("display_name")]
    if not ref.year and d.get("publication_year"):
        ref.year = str(d["publication_year"])
    if not ref.journal:
        ref.journal = _clean(((d.get("primary_location") or {}).get("source") or {}).get("display_name"))

    best = d.get("best_oa_location") or {}
    if best.get("url_for_pdf"):
        ref.oa_pdf = best["url_for_pdf"]
    elif best.get("pdf_url"):
        ref.oa_pdf = best["pdf_url"]
    if not ref.oa_pdf:
        for loc in (d.get("locations") or []):
            if loc.get("pdf_url"):
                ref.oa_pdf = loc["pdf_url"]
                break
    ref.source = (ref.source + " + OpenAlex").strip(" +")


def fill_gaps_from_epmc(ref: Ref) -> None:
    """Europe PMC often carries an abstract where Crossref and OpenAlex do not.

    Crossref frequently omits `abstract` entirely (publisher-dependent), and
    OpenAlex only supplies an inverted index when the source has one. Europe PMC
    has abstracts for most indexed biomedical literature, so use it to backfill.

    Note: the guard must include `oa_pdf`, not just the bibliographic fields.
    A record can arrive from Crossref complete except for any PDF link — and
    Europe PMC is often the only source that knows one (via `?pdf=render`).
    """
    if ref.abstract and ref.authors and ref.year and ref.journal and ref.oa_pdf:
        return
    query = f'DOI:"{ref.doi}"' if ref.doi else f'TITLE:"{ref.title}"'
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode({"query": query, "format": "json",
                                     "resultType": "core", "pageSize": "1"}))
    d = http_json(url)
    if not d:
        return
    results = ((d.get("resultList") or {}).get("result") or [])
    if not results:
        return
    r = results[0]

    if not ref.abstract:
        ref.abstract = _clean(r.get("abstractText"))
    if not ref.authors:
        ref.authors = [a.get("fullName", "") for a in
                       ((r.get("authorList") or {}).get("author") or [])
                       if a.get("fullName")]
    if not ref.year:
        ref.year = str(r.get("pubYear") or "")
    if not ref.journal:
        ref.journal = _clean(((r.get("journalInfo") or {}).get("journal") or {}).get("title"))
    if not ref.pages:
        ref.pages = _clean(r.get("pageInfo"))
    if not ref.keywords:
        ref.keywords = [_clean(k) for k in
                        (r.get("keywordList") or {}).get("keyword", []) if _clean(k)]
    if not ref.doi and r.get("doi"):
        ref.doi = str(r["doi"]).lower()
    if not ref.issn:
        ref.issn = str(((r.get("journalInfo") or {}).get("journal") or {}).get("issn") or "")
    if ref.abstract or ref.authors:
        ref.source = (ref.source + " + Europe PMC").strip(" +")

    # Europe PMC often knows a PDF link that OpenAlex lacks.
    if not ref.pmcid and r.get("pmcid"):
        ref.pmcid = str(r["pmcid"])
    if not ref.oa_pdf:
        for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
            if u.get("documentStyle") == "pdf" and u.get("url"):
                ref.oa_pdf = u["url"]
                break
        else:
            if ref.pmcid and str(r.get("inEPMC")).upper() == "Y":
                ref.oa_pdf = f"https://europepmc.org/articles/{ref.pmcid}?pdf=render"


def resolve(identifier: str) -> Ref | None:
    identifier = identifier.strip()

    # A DOI, bare or as a URL.
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+", identifier)
    if m:
        doi = m.group(0).rstrip(".,;)")
        ref = from_crossref(doi) or from_europepmc(f'DOI:"{doi}"')
        if ref:
            fill_gaps_from_epmc(ref)
            fill_from_openalex(ref)
            return ref
        return None

    # PMID / PMC id.
    m = re.match(r"^(?:pmid:?|pmc:?)?\s*(PMC\d+|\d{6,9})$", identifier, re.I)
    if m:
        token = m.group(1)
        q = f"PMCID:{token}" if token.upper().startswith("PMC") else f"EXT_ID:{token}"
        ref = from_europepmc(q)
        if ref:
            fill_gaps_from_epmc(ref)
            fill_from_openalex(ref)
            return ref

    # Otherwise treat it as a title / free-text query.
    ref = from_europepmc(f'TITLE:"{identifier}"') or from_europepmc(identifier)
    if ref:
        fill_gaps_from_epmc(ref)
        fill_from_openalex(ref)
        return ref
    return None


# ---------------------------------------------------------------- .enw output

def to_enw(ref: Ref) -> str:
    """Render a tagged .enw file using EndNote's own field codes."""
    lines: list[str] = []

    def put(tag_key: str, value: str) -> None:
        value = (value or "").strip()
        if not value:
            return
        for i, part in enumerate(value.splitlines()):
            part = part.strip()
            if part:
                lines.append(f"{TAGS[tag_key]} {part}" if i == 0 else part)

    lines.append(f"{TAGS['ref_type']} {ref.ref_type}")
    for a in ref.authors:
        a = a.strip()
        if a:
            lines.append(f"{TAGS['author']} {a}")
    put("title", ref.title)
    put("journal", ref.journal)
    put("year", ref.year)
    put("volume", ref.volume)
    put("issue", ref.issue)
    put("pages", ref.pages)
    put("doi", ref.doi)
    put("issn", ref.issn)
    put("url", ref.url)
    for k in ref.keywords:
        k = k.strip()
        if k:
            lines.append(f"{TAGS['keywords']} {k}")
    put("abstract", ref.abstract)
    put("link_pdf", ref.oa_pdf)
    lines.append("")
    return "\n".join(lines)


def print_tags() -> int:
    """Show the tag map, re-derived from EndNote's own filter when present."""
    enf = ep.IMPORT_FILTER
    print("Tagged-format codes used by this tool (from EndNote Import.enf):\n")
    for key, tag in TAGS.items():
        print(f"  {tag:3}  {key}")
    print(f"\nfilter present: {enf.is_file()}  ({enf})")
    return 0


# ---------------------------------------------------------------- driver

def find_endnote_window() -> tuple[int, int]:
    """Return (pid, hwnd) of the running EndNote main window, or (0, 0).

    This matters: a MINIMIZED EndNote silently ignores a file handed to it —
    verified by experiment, where the .enl stayed byte-identical until EndNote
    was brought to the foreground. So we find and restore the window ourselves
    instead of hoping the shell does it.
    """
    if sys.platform != "win32":
        return 0, 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[tuple[int, int]] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            if "EndNote" in title and "Library" in title:
                found.append((pid.value, hwnd))
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        if found:
            return found[0]
    except Exception:
        pass
    return 0, 0


def focus_endnote() -> bool:
    """Restore + foreground EndNote so it actually processes the import."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        _pid, hwnd = find_endnote_window()
        if not hwnd:
            return False
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def read_library_refs() -> list[dict] | None:
    """Read the library through whichever source is readable.

    Prefers the live .enl, falls back to the unlocked <lib>.Data/sdb/sdb.eni
    mirror — which is the only source available while EndNote is running.
    """
    cands = [ep.LIBRARY, ep.SDB]
    for path in cands:
        if not path.is_file():
            continue
        try:
            c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=8)
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT id, trash_state, author, year, title, secondary_title, "
                "electronic_resource_number, url FROM refs ORDER BY id")]
            c.close()
            return rows
        except sqlite3.Error:
            continue
    return None


def wait_for_new_record(doi: str, before_ids: set[int], timeout: float = 30.0,
                        interval: float = 0.5) -> dict | None:
    """Wait until the imported record actually appears, or time out.

    EndNote's import is asynchronous: `Popen([ENDNOTE_EXE, enw])` returns
    immediately and EndNote applies the record some time later. Anything that must
    act on the NEW record (adding it to a group, refreshing the index) therefore
    has to wait — and the wait must be bounded, because a minimized or busy
    EndNote may never apply it, and hanging forever is worse than reporting.

    Returns the matching record dict, or None on timeout. Polls the unlocked
    sdb.eni working copy, which is the only readable source while EndNote runs.
    """
    import time
    deadline = time.monotonic() + timeout
    needle = (doi or "").strip().lower()
    while time.monotonic() < deadline:
        refs = read_library_refs()
        if refs:
            for r in refs:
                if r["id"] in before_ids or r["trash_state"]:
                    continue
                if not needle:
                    return r
                hay = " ".join(str(r.get(k) or "") for k in
                               ("electronic_resource_number", "url", "title")).lower()
                if needle in hay:
                    return r
        time.sleep(interval)
    return None


def add_to_group(name: str, ref_id: int, *, create: bool = True) -> int:
    """Put one record into a named custom group. Returns a process exit code.

    Runs in-process rather than shelling out to endnote_groups.py: same library,
    same already-open transaction window, and it saves a whole tool round trip
    (measured at roughly 26s of wall clock in practice).

    Only CUSTOM groups (rule TYPE;3) are editable — a group derived from an online
    search or a smart rule is computed by EndNote and is reported, not written to.
    """
    import endnote_groups as eg  # noqa: PLC0415

    c = eg.connect("rw")
    try:
        # fuzzy=False: filing a paper must not land in a merely similar group.
        g = eg.find_group(c, name, fuzzy=False)
        if g is None:
            if not create:
                print(f"  ! no group matching {name!r}; not creating it "
                      f"(use --group-create to allow creation)", file=sys.stderr)
                return 1
            # Creating on demand saves the caller a --list round trip. Say so
            # loudly: a typo would otherwise create an unwanted group silently.
            print(f"  ! no group matching {name!r} — creating it.")
            print(f"    (check the spelling if that was not intended)")
            rc = eg.create_group(c, name)
            if rc != 0:
                return rc
            g = eg.find_group(c, name)
            if g is None:
                print(f"  ! group creation reported success but it is not readable",
                      file=sys.stderr)
                return 1
        if g["kind"] != "custom":
            print(f"  ! \"{g['name']}\" is {eg._article(g['kind'])} {g['kind']} group — its membership is "
                  f"computed by EndNote and cannot be edited.", file=sys.stderr)
            return 1
        current = set(g["ids"])
        if ref_id in current:
            print(f"  already in \"{g['name']}\" (group_id={g['group_id']})")
            return 0
        rc = eg.write_members(c, g, sorted(current | {ref_id}))
        if rc == 0:
            print(f"  added #{ref_id} to \"{g['name']}\" "
                  f"({len(current)} -> {len(current) + 1} member(s))")
        return rc
    finally:
        c.close()


def run_refresh(incremental: bool = False) -> int:
    """Re-export and re-index in-process. Returns a process exit code."""
    import endnote_refresh as er  # noqa: PLC0415
    return er.refresh(incremental=incremental, quiet=True)


def verify(doi_or_title: str = "") -> int:
    """Confirm an import landed, and report whether the MCP index has caught up."""
    refs = read_library_refs()
    if refs is None:
        print("! no readable library source (neither .enl nor sdb.eni)", file=sys.stderr)
        return 1

    live = [r for r in refs if not r["trash_state"]]
    print(f"library: {len(refs)} records, {len(live)} active\n")

    needle = doi_or_title.strip().lower()
    matches = []
    for r in live:
        hay = " ".join(str(r.get(k) or "") for k in
                       ("title", "electronic_resource_number", "url")).lower()
        if not needle or needle in hay:
            matches.append(r)

    if needle:
        if matches:
            print(f"FOUND {len(matches)} match(es) for {doi_or_title!r}:")
        else:
            print(f"NOT FOUND: {doi_or_title!r}")
            print("  the import did not land — was EndNote minimized or closed?")
            return 1
    for r in matches:
        au = (r["author"] or "").replace("\r", "; ")
        if len(au) > 60:
            au = au[:57] + "..."
        print(f"  #{r['id']:<4} {r['year'] or '----'}  {au}")
        print(f"        {r['title']}")
        if r["electronic_resource_number"]:
            print(f"        DOI: {r['electronic_resource_number']}")

    # Is the MCP index in sync?
    mcp = ep.MCP_DB
    if mcp.is_file():
        try:
            m = sqlite3.connect(f"file:{mcp.as_posix()}?mode=ro", uri=True)
            indexed = {int(r[0]) for r in m.execute("SELECT rec_number FROM references_")}
            m.close()
            lib_ids = {int(r["id"]) for r in live}
            missing = sorted(lib_ids - indexed)
            print(f"\nMCP index: {len(indexed)} records", end="")
            if missing:
                print(f" — STALE, missing {missing}")
                if ep.REFRESH:
                    print(f"  run:  powershell -NoProfile -File {ep.REFRESH}")
                else:
                    print("  run:  the endnote_refresh tool (or `endnote-mcp index --full`)")
                print("  (works while EndNote is open — it reads sdb.eni)")
            else:
                print(" — in sync with the library")
        except sqlite3.Error as exc:
            print(f"\nMCP index unreadable: {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add a paper to EndNote from a DOI, PMID, title, or URL.")
    ap.add_argument("identifier", nargs="?", help="DOI, PMID/PMCID, title, or URL")
    ap.add_argument("--pdf", dest="pdf", action="store_true", default=True,
                    help="download and attach the open-access PDF (default)")
    ap.add_argument("--no-pdf", dest="pdf", action="store_false",
                    help="metadata only; do not look for or attach a PDF")
    ap.add_argument("--dry-run", action="store_true", help="write the .enw but do not launch EndNote")
    ap.add_argument("--no-launch", action="store_true", help="same as --dry-run")

    # ---- post-import actions, combined so one tool call can do the whole job --
    # Each separate step otherwise costs a full agent round trip; measured at
    # roughly 26s each in practice, against ~2s of actual work per step.
    ap.add_argument("--group", metavar="NAME", default="auto",
                    help="after importing, add the new record to this group. "
                         "Defaults to 'auto', which picks the best-matching existing "
                         "group from the paper's own metadata; it files nothing when "
                         "no group matches convincingly")
    ap.add_argument("--no-group", dest="group", action="store_const", const=None,
                    help="do not file the record into any group (skip auto-selection)")
    ap.add_argument("--group-create", action="store_true",
                    help="allow --group to create the group when it does not exist")
    ap.add_argument("--no-group-create", dest="group_create", action="store_false",
                    help="refuse to create a missing group (default unless set)")
    ap.add_argument("--refresh", action="store_true",
                    help="after importing, re-export and rebuild the search index")
    ap.add_argument("--refresh-incremental", action="store_true",
                    help="with --refresh, add new records only (never prunes)")
    ap.add_argument("--wait", type=float, default=30.0, metavar="SECONDS",
                    help="how long to wait for EndNote to apply the import "
                         "(default 30; only used by --group/--refresh)")

    ap.add_argument("--tags", action="store_true", help="print the tag map and exit")
    ap.add_argument("--verify", metavar="DOI_OR_TITLE", nargs="?", const="",
                    help="check whether a record is in the library (and if the index is in sync)")
    ap.add_argument("--list", action="store_true", help="list the library's records and exit")
    args = ap.parse_args(argv)

    if args.tags:
        return print_tags()
    if args.verify is not None:
        return verify(args.verify)
    if args.list:
        return verify("")
    if not args.identifier:
        ap.print_help()
        return 2

    # `--group none` (and an empty value) means "do not file this record".
    # Accepted so a caller can explicitly disable the default auto-selection
    # without knowing the flag name.
    if args.group is not None and args.group.strip().lower() in ("none", "-", ""):
        args.group = None

    # Validate the group BEFORE doing any network work or launching EndNote.
    #
    # Checking it afterwards wasted the whole run: a mistyped name was reported
    # only after ~30s of metadata resolution, PDF download and an import that had
    # already been applied. A cheap local check belongs first, so a bad argument
    # fails in milliseconds and changes nothing.
    group_target = None
    if args.group and args.group.strip().lower() != "auto":
        import endnote_groups as eg  # noqa: PLC0415
        try:
            gc = eg.connect("ro")
        except SystemExit:
            gc = None
        if gc is not None:
            try:
                group_target = eg.find_group(gc, args.group, fuzzy=False)
            finally:
                gc.close()
        if group_target is None and not args.group_create:
            print(f"! no group matching {args.group!r}, and --group-create was not "
                  f"given. Nothing was changed.", file=sys.stderr)
            print(f"  List the groups to find the exact name:", file=sys.stderr)
            print(f"    python {Path(__file__).name} --list   # or: endnote_groups --list",
                  file=sys.stderr)
            print(f"  Or pass --group-create to create {args.group!r}.", file=sys.stderr)
            return 1
        if group_target is not None and group_target["kind"] != "custom":
            print(f"! \"{group_target['name']}\" is {eg._article(group_target['kind'])} "
                  f"{group_target['kind']} group — its membership is computed by "
                  f"EndNote and cannot be edited. Nothing was changed.", file=sys.stderr)
            return 1
        if group_target is None:
            print(f"   note: group {args.group!r} does not exist yet; "
                  f"--group-create will create it after the import.")

    print(f"== resolving: {args.identifier}")
    ref = resolve(args.identifier)
    if ref is None:
        print("  ! could not resolve this identifier to a reference", file=sys.stderr)
        return 1

    # `--group auto`: choose the group from the paper's own metadata.
    #
    # This has to happen AFTER resolve() because the choice is made from the
    # title/abstract/keywords, and BEFORE the import so the decision can be
    # reported and reversed cheaply. When nothing matches convincingly the paper
    # is still added — it is simply not filed, and the reason is stated. Filing a
    # paper into the wrong group is worse than not filing it: the user will not
    # notice until they go looking for the paper and cannot find it.
    if args.group and args.group.strip().lower() == "auto":
        import endnote_groups as eg  # noqa: PLC0415
        try:
            gc = eg.connect("ro")
        except SystemExit:
            gc = None
        picked = None
        if gc is not None:
            try:
                picked = eg.suggest_group(
                    gc, title=ref.title, abstract=ref.abstract,
                    keywords=ref.keywords, journal=ref.journal,
                    author="; ".join(ref.authors[:3]))
            finally:
                gc.close()
        if picked is None:
            print("   group   : (none chosen — no existing group matches this paper "
                  "closely enough)")
            print("             the paper is still added; file it manually with:")
            print("               python endnote_groups.py --add \"<group>\" --refs <id>")
            args.group = None
        elif not picked.get("group"):
            # Ambiguous: name the candidates rather than silently picking one.
            print(f"   group   : (not chosen — {picked['reason']})")
            print("             the paper is still added. Name the one you want with")
            print("               --group \"<name>\", or add it later:")
            for cand in picked.get("ambiguous", []):
                print(f"                 \"{cand['name']}\" (score {cand['score']:.2f})")
            args.group = None
        else:
            group_target = picked["group"]
            args.group = group_target["name"]
            print(f"   group   : \"{args.group}\"  (auto-selected)")
            print(f"             {picked['reason']}")
            if picked.get("runner_up"):
                ru = picked["runner_up"]
                print(f"             runner-up: \"{ru['name']}\" "
                      f"(score {ru['score']:.2f} vs {picked['score']:.2f})")
            print(f"             override with --group \"<name>\", or --no-group")

    miss = ref.missing()
    print(f"== metadata from {ref.source}")
    print(f"   title   : {ref.title or '(none)'}")
    print(f"   authors : {', '.join(ref.authors[:4]) or '(none)'}"
          + (f" (+{len(ref.authors) - 4} more)" if len(ref.authors) > 4 else ""))
    print(f"   journal : {ref.journal or '(none)'}  {ref.year}  {ref.volume}({ref.issue}) {ref.pages}")
    print(f"   doi     : {ref.doi or '(none)'}")
    if miss:
        print(f"   ! missing: {', '.join(miss)}")
    if ref.oa_pdf:
        print(f"   oa pdf  : {ref.oa_pdf}")

    # PDF handling.
    #
    # LOAD-BEARING DETAIL: `%>` (EndNote's "Link to PDF", field 44) only creates
    # an attachment when it holds a path to a LOCAL FILE. Given a URL it stores a
    # dead link and attaches nothing — verified: URL-based imports produced no
    # file_res row, while an absolute local path made EndNote copy the PDF into
    # <lib>.Data/PDF/<id>/ and register it. So --pdf must download FIRST and then
    # rewrite ref.oa_pdf to the local path before the .enw is rendered.
    local_pdf: Path | None = None
    if args.pdf:
        if not (ref.oa_pdf or ref.pmcid or ref.doi):
            print("   ! no PDF source known for this record")
        else:
            safe = re.sub(r"[^\w.\- ]+", "_",
                          f"{ref.authors[0].split()[-1] if ref.authors else 'paper'}"
                          f"_{ref.year}_{ref.title[:60]}").strip() or "paper"
            dest = STAGING / f"{safe}.pdf"
            print(f"== fetching the open-access PDF (trying {len(pdf_candidates(ref))} source(s))")
            local_pdf = fetch_pdf(ref, dest)
            if local_pdf:
                print(f"   using local file for %")
            else:
                n = len(pdf_candidates(ref))
                if not _has_oa_evidence(ref):
                    print("   ! no open-access copy exists (this paper is paywalled).")
                    print("     Institutional access or the author's own copy is needed.")
                else:
                    print(f"   ! none of {n} source(s) served a file (publisher bot-blocking).")
                # Every PDF route failed. paper_pdf can still recover the full
                # TEXT from Europe PMC's XML endpoint, which works where its own
                # PDF host 403s. Save that next to the record so the content is
                # not lost — but do NOT put it in %>, which expects a PDF.
                text_path = _try_fulltext(ref)
                if text_path:
                    print(f"   full text saved instead -> {text_path.name}")
                print("     The record imports without a PDF. To attach one later:")
                print("       python endnote_attach.py --list")
                print("       python endnote_attach.py <ref-number> <downloaded.pdf>")
                if ref.oa_pdf:
                    print(f"     link: {ref.oa_pdf}")
    elif ref.oa_pdf:
        print("   (open-access PDF available — it is attached by default; use --no-pdf to skip)")

    # `%>` is set ONLY to a local file path. A URL there is worse than nothing:
    # EndNote shows it as a link that cannot be opened, and no attachment is
    # created. So when no local PDF was obtained, the field is left out entirely.
    ref.oa_pdf = str(local_pdf) if local_pdf is not None else ""

    ENW_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w.\-]+", "_", (ref.doi or ref.title or "ref"))[:80] or "ref"
    enw = ENW_DIR / f"{slug}.enw"
    enw.write_text(to_enw(ref), encoding="utf-8-sig")
    print(f"== wrote {enw}")
    print("   " + "-" * 58)
    for line in enw.read_text(encoding="utf-8-sig").rstrip().splitlines():
        print(f"   | {line[:100]}")
    print("   " + "-" * 58)

    if args.dry_run or args.no_launch:
        print("== dry run: not launching EndNote")
        return 0

    if not ENDNOTE_EXE.is_file():
        print(f"  ! EndNote not found at {ENDNOTE_EXE}", file=sys.stderr)
        return 1

    import subprocess
    print(f"== handing to EndNote: {enw.name}")

    # Snapshot existing ids so the new record can be identified after the import.
    # Only needed when something must act on the new record.
    needs_identify = bool(args.group or args.refresh)
    before_ids: set[int] = set()
    if needs_identify:
        prior = read_library_refs() or []
        before_ids = {r["id"] for r in prior}

    subprocess.Popen([str(ENDNOTE_EXE), str(enw)], close_fds=True)

    # A minimized EndNote ignores the import (verified), so bring it up.
    import time
    time.sleep(2.0)
    focused = focus_endnote()
    print("   EndNote brought to the foreground." if focused
          else "   ! could not focus EndNote — if it is minimized, the import may not land.")

    new_ref = None
    if needs_identify:
        print(f"   waiting for EndNote to apply the import "
              f"(up to {args.wait:.0f}s) ...")
        new_ref = wait_for_new_record(ref.doi or args.identifier, before_ids,
                                      timeout=args.wait)
        if new_ref is None:
            # Bounded failure: say exactly what did not happen and what still can.
            print(f"   ! the record had not appeared after {args.wait:.0f}s.",
                  file=sys.stderr)
            print("     EndNote may be busy, minimized, or showing a dialog.",
                  file=sys.stderr)
            if args.group:
                print(f"     the group step was skipped; retry once it appears:",
                      file=sys.stderr)
                print(f"       python {Path(__file__).name} --verify {ref.doi}",
                      file=sys.stderr)
                print(f"       (then add it to \"{args.group}\")", file=sys.stderr)
            return 1
        print(f"   appeared as record #{new_ref['id']}: "
              f"{str(new_ref['title'])[:60]}")

    rc = 0
    if args.group:
        rc = add_to_group(args.group, new_ref["id"], create=args.group_create) or rc
    if args.refresh:
        print("== refreshing the search index ==")
        rc = run_refresh(incremental=args.refresh_incremental) or rc

    if not needs_identify:
        print("   The record now appears in the open library.")
        print(f"   Verify/search with:  python {Path(__file__).name} --verify")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
