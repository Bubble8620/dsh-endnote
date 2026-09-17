#!/usr/bin/env python3
"""Find and download a paper's open-access PDF from a DOI, PMID, arXiv id, or title.

Design is driven by a measured survey (see `--list` and
`oa-inbox/_survey.txt`), not by assumption. The findings that shape it:

  * Publisher hosts differ sharply. Verified working: link.springer.com,
    nature.com, frontiersin.org, arxiv.org, bmcmicrobiol.biomedcentral.com,
    and institutional repositories.
    Verified 403 for non-browser clients: mdpi.com, europepmc.org,
    pubs.acs.org, pubs.rsc.org, pubs.acs.org.
  * Therefore: gather MANY candidate URLs from several metadata services,
    then try each and keep the first that returns real %PDF bytes.
  * Repository mirrors rescue papers whose publisher blocks: an ACS paper
    403s at pubs.acs.org but downloads fine from an institutional repository.
  * Europe PMC's fullTextUrlList often lists the PUBLISHER's working PDF
    (e.g. Springer), alongside its own blocked ?pdf=render link.
  * `pmc.ncbi.nlm.nih.gov/articles/<PMCID>/pdf/` returns HTML, not a PDF —
    it is a landing page, so it is not a PDF route.
  * Europe PMC fullTextXML is retrievable even when every PDF host refuses,
    so `--text` is a real fallback rather than a dead end.

Usage
-----
    python paper_pdf.py 10.1038/nature12373
    python paper_pdf.py 10.3390/v13061131 --text
    python paper_pdf.py "graph neural network survey" --out .
    python paper_pdf.py 10.1038/nature12373 --list
    python paper_pdf.py 10.1038/nature12373 --json
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
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Optional HTTP(S) proxy. Empty by default: most machines need none, and
# assuming one leaks the author setup and can mis-route requests.
PROXY = os.environ.get("DSH_ENDNOTE_PROXY", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Unpaywall rejects an obviously fake address with HTTP 422, so a plausible one is
# required. Override it with DSH_ENDNOTE_CONTACT to identify your own client.
EMAIL = os.environ.get("DSH_ENDNOTE_CONTACT", "endnote-pdf@example.org")
DEFAULT_OUT = ep.STAGING
TIMEOUT = 40

# Hosts measured to reject non-browser clients. Tried last, not skipped: the
# block is per-request and a different path on the same host can still work.
BLOCKED_HOSTS = {
    "mdpi.com", "www.mdpi.com",
    "europepmc.org",
    "pubs.acs.org", "pubs.rsc.org",
    "www.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov",
    "doi.org", "www.tandfonline.com", "onlinelibrary.wiley.com",
}


# ------------------------------------------------------------------ plumbing

def _ctx(relax: bool = False) -> ssl.SSLContext:
    """TLS context. Verification is relaxed ONLY for the proxy leg.

    Why `relax` is a per-request argument rather than a check on PROXY: an
    intercepting proxy presents its own CA, which the system trust store does not
    know, so the proxied connection needs a relaxed context. But the DIRECT
    connection does not — and an earlier version relaxed based on `if PROXY:` at
    context-build time, which meant that merely *setting* DSH_ENDNOTE_PROXY
    silently disabled verification for every request in the process, including the
    direct ones and the downloaded PDF bytes. Gating on the leg being attempted
    keeps the relaxation where it is justified and nowhere else.
    """
    c = ssl.create_default_context()
    if relax:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def _attempts() -> list[bool]:
    """Transports to try: direct, plus the proxy only when one is configured."""
    return [False, True] if PROXY else [False]


def _open(url: str, *, accept: str, prox: bool, timeout: int, limit: int | None):
    # `relax=prox`: the direct leg keeps full verification even when a proxy is
    # configured, so an unused proxy setting cannot weaken ordinary requests.
    handlers = [urllib.request.HTTPSHandler(context=_ctx(relax=prox))]
    handlers.append(urllib.request.ProxyHandler(
        {"http": PROXY, "https": PROXY} if prox else {}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept,
        "Accept-Encoding": "identity", "Connection": "close"})
    with opener.open(req, timeout=timeout) as r:
        data = r.read() if limit is None else r.read(limit)
        return r.status, r.headers.get("Content-Type", ""), data


def get_json(url: str) -> dict | None:
    for prox in _attempts():
        try:
            _st, _ct, body = _open(url, accept="application/json",
                                   prox=prox, timeout=TIMEOUT, limit=None)
            return json.loads(body)
        except Exception:
            continue
    return None


def get_text(url: str) -> str | None:
    for prox in _attempts():
        try:
            _st, _ct, body = _open(url, accept="application/xml,text/xml,*/*",
                                   prox=prox, timeout=TIMEOUT, limit=None)
            return body.decode("utf-8", "replace")
        except Exception:
            continue
    return None


def download(url: str, dest: Path) -> tuple[bool, str]:
    """Try to save real PDF bytes. Returns (ok, note).

    Verifies the %PDF magic: publishers commonly answer 200 with an HTML
    paywall page, which must NOT be recorded as success.
    """
    last = ""
    for prox in _attempts():
        try:
            _st, ctype, blob = _open(url, accept="application/pdf,*/*",
                                     prox=prox, timeout=120, limit=None)
            if blob[:4] != b"%PDF":
                return False, ("not-pdf: png?" if blob[:4] == b"\x89PNG"
                               else "not-pdf(html)" if b"<" in blob[:64]
                               else f"not-pdf({blob[:4]!r})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            return True, f"{len(blob):,} bytes"
        except urllib.error.HTTPError as exc:
            last = f"HTTP{exc.code}"
            continue
        except Exception as exc:                      # noqa: BLE001
            last = type(exc).__name__
    return False, last or "failed"


# ------------------------------------------------------------------ metadata

@dataclass
class Paper:
    doi: str = ""
    title: str = ""
    year: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    pmcid: str = ""
    arxiv: str = ""
    candidates: list[tuple[str, str]] = field(default_factory=list)  # (url, source)
    xml_url: str = ""

    def add(self, url: str | None, source: str) -> None:
        if not url:
            return
        if any(u == url for u, _ in self.candidates):
            return
        self.candidates.append((url, source))

    def ordered(self) -> list[tuple[str, str]]:
        """Working hosts first, known-blocked hosts last."""
        def rank(item):
            host = urllib.parse.urlparse(item[0]).netloc.lower()
            blocked = any(host == b or host.endswith("." + b) or b in host
                          for b in BLOCKED_HOSTS)
            return (1 if blocked else 0)
        return sorted(self.candidates, key=rank)


def _norm_doi(text: str) -> str | None:
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+", text or "")
    return m.group(0).rstrip(".,;)") if m else None


def resolve(identifier: str) -> Paper | None:
    ident = identifier.strip()
    p = Paper()

    doi = _norm_doi(ident)
    if not doi:
        m = re.match(r"^(?:pmid:?|pmc:?)?\s*(PMC\d+|\d{6,9})$", ident, re.I)
        if m:
            token = m.group(1)
            q = (f"PMCID:{token}" if token.upper().startswith("PMC")
                 else f"EXT_ID:{token}")
            doi = _epmc_doi(q)
        if not doi:
            doi = _doi_from_text(ident)
    if not doi:
        return None
    p.doi = doi

    _from_crossref(p)
    _from_epmc(p)
    _from_openalex(p)
    _from_unpaywall(p)
    _from_arxiv(p)
    _from_semanticscholar(p)
    # Return the Paper whenever the identifier resolved to a real DOI. Do NOT
    # collapse "identified the paper but it is paywalled" into "could not
    # resolve" — those need different messages, and the caller decides.
    return p


def _epmc_search(query: str, *, field: str = "") -> dict | None:
    """One Europe PMC result for a query, or None."""
    q = f'{field}:"{query}"' if field else query
    d = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
                 + urllib.parse.urlencode({"query": q, "format": "json",
                                           "resultType": "core", "pageSize": "1"}))
    res = ((d or {}).get("resultList") or {}).get("result") or []
    return res[0] if res else None


def _epmc_doi(query: str) -> str | None:
    r = _epmc_search(query)
    return (r.get("doi") or "").lower() or None if r else None


def _doi_from_text(ident: str) -> str | None:
    """Resolve a title or free-text phrase to a DOI.

    TITLE:"..." needs a near-exact title, so a loose phrase like
    "a loose phrase like this" finds nothing that way. Fall back to
    an unfielded relevance search, which is what a human would expect.
    """
    for field in ("TITLE", ""):
        r = _epmc_search(ident, field=field)
        if r and r.get("doi"):
            return str(r["doi"]).lower()
    # Last resort: Crossref bibliographic query.
    d = get_json("https://api.crossref.org/works?"
                 + urllib.parse.urlencode({"query.bibliographic": ident,
                                           "rows": "1", "select": "DOI,title"}))
    items = (((d or {}).get("message") or {}).get("items") or [])
    if items and items[0].get("DOI"):
        return str(items[0]["DOI"]).lower()
    return None


def _from_crossref(p: Paper) -> None:
    d = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(p.doi)}")
    m = (d or {}).get("message")
    if not m:
        return
    p.title = p.title or _clean((m.get("title") or [""])[0])
    p.journal = p.journal or _clean((m.get("container-title") or [""])[0])
    if not p.year:
        for k in ("published-print", "published-online", "issued", "created"):
            parts = (m.get(k) or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0]:
                p.year = str(parts[0][0])
                break
    if not p.authors:
        for a in (m.get("author") or [])[:6]:
            nm = " ".join(x for x in (a.get("given", ""), a.get("family", "")) if x)
            if nm or a.get("name"):
                p.authors.append(nm or a["name"])


def _clean(t: str | None) -> str:
    if not t:
        return ""
    t = str(t)
    for _ in range(2):
        t = (t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
              .replace("&quot;", '"').replace("&#39;", "'"))
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _from_epmc(p: Paper) -> None:
    d = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
                 + urllib.parse.urlencode({"query": f'DOI:"{p.doi}"', "format": "json",
                                           "resultType": "core", "pageSize": "1"}))
    res = ((d or {}).get("resultList") or {}).get("result") or []
    if not res:
        return
    r = res[0]
    p.pmcid = p.pmcid or str(r.get("pmcid") or "")
    p.title = p.title or _clean(r.get("title"))
    p.journal = p.journal or _clean(((r.get("journalInfo") or {}).get("journal") or {}).get("title"))
    p.year = p.year or str(r.get("pubYear") or "")
    # fullTextUrlList frequently carries the PUBLISHER's working PDF.
    for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if u.get("documentStyle") == "pdf" and u.get("url"):
            p.add(u["url"], f"epmc/{urllib.parse.urlparse(u['url']).netloc}")
    if p.pmcid:
        p.add(f"https://europepmc.org/articles/{p.pmcid}?pdf=render", "epmc-render")
        p.xml_url = (f"https://www.ebi.ac.uk/europepmc/webservices/rest/"
                     f"{p.pmcid}/fullTextXML")


def _from_openalex(p: Paper) -> None:
    d = get_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(p.doi)}")
    if not d:
        return
    p.title = p.title or _clean(d.get("title"))
    p.year = p.year or str(d.get("publication_year") or "")
    best = d.get("best_oa_location") or {}
    for k in ("url_for_pdf", "pdf_url"):
        if best.get(k):
            p.add(best[k], "openalex/best")
    for loc in (d.get("locations") or []):
        u = loc.get("pdf_url")
        if u:
            host = urllib.parse.urlparse(u).netloc
            p.add(u, f"openalex/{host}")


def _from_unpaywall(p: Paper) -> None:
    d = get_json(f"https://api.unpaywall.org/v2/{p.doi}?email={EMAIL}")
    if not d:
        return
    for loc in (d.get("oa_locations") or []):
        u = loc.get("url_for_pdf")
        if u:
            host = urllib.parse.urlparse(u).netloc
            kind = loc.get("host_type") or "?"
            p.add(u, f"unpaywall/{kind}")


def _from_arxiv(p: Paper) -> None:
    if p.arxiv:
        p.add(f"https://arxiv.org/pdf/{p.arxiv}", "arxiv")
        return
    xml = get_text("https://export.arxiv.org/api/query?"
                   + urllib.parse.urlencode({"search_query": f'all:"{p.doi}"',
                                             "max_results": "1"}))
    if not xml:
        return
    m = re.search(r"<id>http://arxiv\.org/abs/([^<]+)</id>", xml)
    if m:
        p.arxiv = m.group(1).strip()
        p.add(f"https://arxiv.org/pdf/{p.arxiv}", "arxiv")


def _from_semanticscholar(p: Paper) -> None:
    d = get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:"
                 + urllib.parse.quote(p.doi) + "?fields=openAccessPdf,externalIds,title")
    if not d:
        return
    p.title = p.title or _clean(d.get("title"))
    ids = d.get("externalIds") or {}
    if ids.get("ArXiv") and not p.arxiv:
        p.arxiv = str(ids["ArXiv"])
        p.add(f"https://arxiv.org/pdf/{p.arxiv}", "arxiv/s2")
    u = (d.get("openAccessPdf") or {}).get("url")
    if u:
        p.add(u, "semanticscholar")


def full_text(p: Paper) -> str | None:
    """Europe PMC JATS XML -> readable markdown-ish text."""
    if not p.xml_url:
        return None
    xml = get_text(p.xml_url)
    if not xml:
        return None
    body = re.search(r"<body[^>]*>(.*?)</body>", xml, re.S)
    src = body.group(1) if body else xml
    src = re.sub(r"<(title|p|sec|abstract)[^>]*>", "\n\n", src)
    src = re.sub(r"</(title|p|sec)>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&#x2212;", "-")):
        src = src.replace(a, b)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", src)).strip()


# ------------------------------------------------------------------ driver

def safe_name(p: Paper) -> str:
    who = (p.authors[0].split()[-1] if p.authors else "paper")
    raw = f"{who}_{p.year}_{p.title[:70]}".strip("_")
    return (re.sub(r"[^\w.\- ]+", "_", raw).strip() or "paper")[:120] + ".pdf"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download a paper's open-access PDF.")
    ap.add_argument("identifier", help="DOI, PMID/PMCID, arXiv id, or title")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    ap.add_argument("--text", action="store_true",
                    help="skip the PDF download and save the full text instead "
                         "(Europe PMC XML). Without this flag the full text is "
                         "still saved AUTOMATICALLY when no PDF can be obtained")
    ap.add_argument("--list", action="store_true",
                    help="show every candidate URL and its result; download nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args(argv)

    outdir = Path(args.out)
    p = resolve(args.identifier)
    if p is None or not p.doi:
        print(f"! could not resolve: {args.identifier}", file=sys.stderr)
        return 2

    if not args.json:
        print(f"DOI     : {p.doi}")
        print(f"title   : {p.title or '(unknown)'}")
        who = "; ".join(p.authors[:4]) or "(unknown)"
        print(f"authors : {who}" + (f" (+{len(p.authors)-4})" if len(p.authors) > 4 else ""))
        print(f"journal : {p.journal or '?'} {p.year}")
        if p.pmcid:
            print(f"PMCID   : {p.pmcid}")
        if p.arxiv:
            print(f"arXiv   : {p.arxiv}")

    # --list: report each route's outcome without saving anything.
    if args.list:
        print(f"\n{len(p.candidates)} candidate URL(s), best host first:\n")
        for url, source in p.ordered():
            host = urllib.parse.urlparse(url).netloc
            blocked = any(b in host for b in BLOCKED_HOSTS)
            print(f"  [{'blocked' if blocked else '  try  '}] {source:26} {url[:74]}")
        print(f"\nfull text available: {bool(p.xml_url)}"
              + (f"  ({p.xml_url})" if p.xml_url else ""))
        return 0

    # Try every candidate; keep the first real PDF.
    dest = outdir / safe_name(p)
    result = {"doi": p.doi, "title": p.title, "pdf": None, "text": None,
              "attempts": []}

    # `--text` means "full text is what I want, do not spend time on PDFs".
    # Previously the flag was accepted and never read: the full-text fallback below
    # is unconditional, so `--text` changed nothing at all. Rather than leave a
    # documented switch inert, it now has the effect its name implies — skip the
    # download attempts and go straight to the text. The automatic fallback is
    # unchanged, so omitting the flag still yields text when no PDF exists.
    if not args.text:
        for url, source in p.ordered():
            ok, note = download(url, dest)
            result["attempts"].append({"source": source, "url": url,
                                       "ok": ok, "note": note})
            if not args.json:
                print(f"  {source:26} {'OK' if ok else '--':3} {note:18} "
                      f"{urllib.parse.urlparse(url).netloc}")
            if ok:
                result["pdf"] = str(dest)
                break
    elif not args.json:
        print("  --text: skipping PDF download, going straight to full text")

    if result["pdf"]:
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nsaved PDF -> {dest}")
        return 0

    # No PDF: fall back to full text.
    if p.xml_url:
        txt = full_text(p)
        if txt:
            tdest = dest.with_suffix(".md")
            # Create the directory here too. The PDF path does this before writing,
            # but the full-text path did not — so `--out <new-dir>` raised a raw
            # FileNotFoundError at exactly the moment the PDF route had already
            # failed, which is the common case for a blocked publisher.
            try:
                tdest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"! cannot create {tdest.parent}: {exc}", file=sys.stderr)
                return 1
            tdest.write_text(f"# {p.title}\n\nDOI: {p.doi}\n\n{txt}\n", encoding="utf-8")
            result["text"] = str(tdest)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(f"\nno PDF obtainable (every host refused).")
                print(f"full text saved -> {tdest}  ({len(txt):,} chars)")
            return 0

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif not p.candidates:
        print("\n! this paper appears to be PAYWALLED — no open-access copy found.")
        print("  No free PDF or full text exists at any indexed location.")
        print("  Options: the user's institutional access, interlibrary loan, or")
        print("  the author's own copy. You can also attach a PDF they supply")
        print("  with the endnote_attach tool (or endnote_attach.py --list).")
    else:
        print("\n! no PDF and no full text obtained.")
        print(f"  Tried {len(p.candidates)} host(s); all refused (bot-blocking).")
        print("  Fetch it in a browser (see the paper-pdf skill), or ask the user.")
        for url, _source in p.candidates[:4]:
            print(f"    {url}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
