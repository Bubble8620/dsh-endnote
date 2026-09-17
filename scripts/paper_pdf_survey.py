#!/usr/bin/env python3
"""Survey every viable route to an open-access PDF, across real papers.

Goal: replace assumptions with a measured table. For each DOI, try each route
and record whether real PDF bytes came back.

Routes under test
  1. Unpaywall oa_locations (needs a plausible contact email)
  2. OpenAlex locations[].pdf_url
  3. Europe PMC fullTextUrlList
  4. Semantic Scholar openAccessPdf
  5. arXiv (direct id, and via OpenAlex locations)
  6. Europe PMC REST fullTextXML -> is the TEXT retrievable even when the PDF 403s?
  7. bioRxiv / medRxiv
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

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROXY = os.environ.get("DSH_ENDNOTE_PROXY", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
EMAIL = os.environ.get("DSH_ENDNOTE_CONTACT", "endnote-pdf@example.org")

# A spread of publishers. Kept deliberately generic: an earlier version said
# "weighted to the user's field" and listed real papers from the author's own
# library, which disclosed their research area.
#
# NOTE: this list is a MOVING TARGET. A DOI that is safe when chosen can later
# become a library record (one did, after the library changed). Re-run
# tools/audit_bibliography.py after any change to the library.
DOIS = [
    "10.1007/s00134-012-2769-8",     # Springer  (works)
    "10.1038/s41586-020-2649-2",     # Nature    (works)
    "10.3389/fmicb.2020.01395",      # Frontiers (works)
    "10.3390/v13061131",             # MDPI      (403)
    "10.1039/d2ma00980c",            # RSC       (403)
    "10.1021/acssynbio.2c00465",     # ACS       (403)
    "10.1128/spectrum.00422-22",     # ASM/EPMC  (403)
    "10.1016/j.apsb.2025.05.037",    # Elsevier  (?)
    "10.1038/nature12373",           # Nature + arXiv preprint
    "10.1186/s12915-021-01021-4",    # BMC       (?)
]


def ctx(relax: bool = False):
    """TLS context; relaxed only for a PROXIED connection (see paper_pdf).

    Gating on the leg rather than on `if PROXY` matters: otherwise setting
    DSH_ENDNOTE_PROXY would disable verification for the direct requests too.
    """
    c = ssl.create_default_context()
    if relax:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def req(url, *, accept="application/json", prox=False, timeout=45, limit=None):
    hs = [urllib.request.HTTPSHandler(context=ctx(relax=prox))]
    hs.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY} if prox else {}))
    op = urllib.request.build_opener(*hs)
    r = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept, "Accept-Encoding": "identity",
        "Connection": "close"})
    with op.open(r, timeout=timeout) as resp:
        # NOTE: resp.read(None) or resp.read(0) both return b"" — must branch,
        # otherwise every JSON call silently yields an empty body.
        data = resp.read() if limit is None else resp.read(limit)
        return resp.status, resp.headers.get("Content-Type", ""), data


def get_json(url):
    try:
        _st, _ct, body = req(url)
        return json.loads(body)
    except Exception:
        return None


def try_pdf(url) -> str:
    """Return 'PDF', 'HTML', '403', or an error tag."""
    for prox in (False, True):
        try:
            st, ct, body = req(url, accept="application/pdf,*/*", prox=prox, limit=8)
            if body[:4] == b"%PDF":
                return "PDF"
            return "HTML" if "html" in ct.lower() else f"OTHER({ct[:18]})"
        except urllib.error.HTTPError as e:
            if e.code == 403:
                last = "403"
                continue
            return f"HTTP{e.code}"
        except Exception as e:
            last = type(e).__name__
    return last


def main() -> int:
    """Run the survey once and print the table."""
    print(f"{'DOI':32} {'route':30} {'result':12} url")
    print("=" * 118)
    xml_ok = []
    for doi in DOIS:
        print(f"\n### {doi}")

        # 1. Unpaywall
        up = get_json(f"https://api.unpaywall.org/v2/{doi}?email={EMAIL}")
        if up:
            locs = up.get("oa_locations") or []
            pdfs = [l.get("url_for_pdf") for l in locs if l.get("url_for_pdf")]
            if pdfs:
                for u in pdfs[:2]:
                    print(f"{'':32} {'unpaywall':30} {try_pdf(u):12} {u[:60]}")
            else:
                print(f"{'':32} {'unpaywall':30} {'no link':12} is_oa={up.get('is_oa')}")
        else:
            print(f"{'':32} {'unpaywall':30} {'no data':12}")

        # 2. OpenAlex
        oa = get_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}")
        if oa:
            cands = []
            best = oa.get("best_oa_location") or {}
            for k in ("url_for_pdf", "pdf_url"):
                if best.get(k):
                    cands.append(best[k])
            for l in (oa.get("locations") or []):
                if l.get("pdf_url") and l["pdf_url"] not in cands:
                    cands.append(l["pdf_url"])
            for u in cands[:3]:
                host = urllib.parse.urlparse(u).netloc
                print(f"{'':32} {'openalex:' + host[:20]:30} {try_pdf(u):12} {u[:60]}")
            if not cands:
                print(f"{'':32} {'openalex':30} {'no link':12} is_oa={(oa.get('open_access') or {}).get('is_oa')}")

        # 3/6. Europe PMC: PDF list + fullTextXML
        epmc = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
                        + urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json",
                                                  "resultType": "core", "pageSize": "1"}))
        pmcid = None
        if epmc and epmc.get("resultList", {}).get("result"):
            r = epmc["resultList"]["result"][0]
            pmcid = r.get("pmcid")
            for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
                if u.get("documentStyle") == "pdf" and u.get("url"):
                    print(f"{'':32} {'epmc:pdf':30} {try_pdf(u['url']):12} {u['url'][:60]}")
            if pmcid:
                xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                try:
                    st, ct, body = req(xml_url, accept="application/xml", limit=400)
                    ok = body.startswith(b"<?xml") or b"<article" in body
                    print(f"{'':32} {'epmc:fullTextXML':30} {'XML' if ok else ct[:10]:12} {pmcid}")
                    if ok:
                        xml_ok.append((doi, pmcid))
                except Exception as e:
                    print(f"{'':32} {'epmc:fullTextXML':30} {type(e).__name__:12} {pmcid}")

        # 4. Semantic Scholar
        s2 = get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:"
                      + urllib.parse.quote(doi) + "?fields=openAccessPdf,externalIds")
        if s2:
            p = (s2.get("openAccessPdf") or {}).get("url")
            if p:
                print(f"{'':32} {'semanticscholar':30} {try_pdf(p):12} {p[:60]}")
            else:
                print(f"{'':32} {'semanticscholar':30} {'no link':12} ids={s2.get('externalIds')}")

        # 5. arXiv
        arx = get_json("https://export.arxiv.org/api/query?"
                       + urllib.parse.urlencode({"search_query": f"doi:{doi}", "max_results": "1"}))
        if arx:
            pass  # arXiv returns Atom XML; handled crudely below
        try:
            st, ct, body = req("https://export.arxiv.org/api/query?"
                               + urllib.parse.urlencode({"search_query": f'all:"{doi}"',
                                                         "max_results": "1"}),
                               accept="application/atom+xml", limit=4000)
            m = re.search(rb"<id>(http://arxiv\.org/abs/[^<]+)</id>", body)
            if m:
                aid = m.group(1).decode().rsplit("/", 1)[-1]
                u = f"https://arxiv.org/pdf/{aid}"
                print(f"{'':32} {'arxiv':30} {try_pdf(u):12} {u}")
            else:
                print(f"{'':32} {'arxiv':30} {'not found':12}")
        except Exception as e:
            print(f"{'':32} {'arxiv':30} {type(e).__name__:12}")

    print("\n" + "=" * 118)
    print(f"papers with a retrievable Europe PMC fullTextXML: {len(xml_ok)}/{len(DOIS)}")
    for doi, pmc in xml_ok:
        print(f"   {doi:34} {pmc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
