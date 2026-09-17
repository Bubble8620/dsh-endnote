---
name: paper-pdf
description: "Find and download the open-access PDF of a paper from a DOI, PMID, arXiv id, or title, and extract its full text when no PDF is obtainable. Knows which publishers block automated downloads and which mirrors to fall back on."
whenToUse: "Use when the user wants the PDF of a paper, asks to download/获取/下载 a paper or its 全文/PDF, needs a paper's full text for reading or extraction, or when the EndNote importer could not fetch a PDF automatically. Also use before attaching a PDF to an EndNote reference, to obtain the file in the first place."
---

<!--
Vendored into the `dsh-endnote` plugin by tools/vendor_skills.py.
Do not edit this copy directly: edit the source skill, then re-run the vendorer.

Paths below are placeholders:
  <plugin>/scripts   the bundled tool scripts (the plugin sets
                     DSH_ENDNOTE_SCRIPTS to this directory)
  <staging dir>      where downloads are stored (the `stagingDir` config)
  <library.enl>      your EndNote library (the `library` config)

When the plugin is installed, PREFER ITS NATIVE TOOLS over running a script by
path — they are the same code, already wired to your configuration. Reach for the
script form only when the tools are unavailable.
-->


# Getting a paper's open-access PDF

## One command

```powershell
python <plugin>/scripts\paper_pdf.py 10.1038/nature12373
python <plugin>/scripts\paper_pdf.py 10.1038/nature12373 --out C:\path\dir
python <plugin>/scripts\paper_pdf.py "bacteriophage encapsulation review" --out .
python <plugin>/scripts\paper_pdf.py 10.3390/v13061131 --text      # full text when no PDF
```

It prints the resolved paper, tries every mirror, and reports which route won.

| Flag | Effect |
|---|---|
| `--out DIR` | where to save (default `<staging dir>`) |
| `--text` | **skip the PDF download**, go straight to the full text (Europe PMC XML → markdown). The full text is saved *automatically* when no PDF can be obtained, so this flag is for when you want the text and don't want to spend time on PDF routes |
| `--list` | enumerate every candidate URL and its result, downloading nothing |
| `--json` | machine-readable result |

## The core insight: publishers differ sharply

Measured on this machine — this is the whole reason a fallback chain is needed:

| Route | Result |
|---|---|
| **arxiv.org** | PDF ✅ (most reliable of all) |
| **link.springer.com** | PDF ✅ |
| **nature.com** | PDF ✅ (sometimes serves HTML on a `.pdf` path) |
| **frontiersin.org** | PDF ✅ |
| **bmcmicrobiol.biomedcentral.com** | PDF ✅ |
| **institutional repositories**  | PDF ✅ |
| mdpi.com | **403** ❌ |
| europepmc.org (`?pdf=render`) | **403** ❌ |
| pubs.acs.org | **403** ❌ |
| pubs.rsc.org | **403** ❌ |
| pmc.ncbi.nlm.nih.gov/.../pdf/ | HTML, not a PDF ⚠️ |

A single "grab the OA link and download it" step fails roughly half the time,
because the most common OA hosts (MDPI, Europe PMC) are exactly the ones that
block non-browser clients. Never conclude "this paper has no PDF" from one 403 —
**try the other routes**.

## The fallback order that matters

1. **arXiv** — if the DOI has a preprint, this is the most reliable PDF anywhere.
   Query `export.arxiv.org/api/query?search_query=all:"<doi>"` and use
   `arxiv.org/pdf/<id>`. Check this *early* for physics/CS/math papers.
2. **OpenAlex** `locations[].pdf_url` — often lists a **repository copy** alongside
   the blocked publisher link. This is what rescues blocked papers: ACS
   `10.1021/acssynbio.2c00465` 403s at the publisher but downloads from
   an institutional repository. Prefer hosts not in the 403 table.
3. **Europe PMC** `fullTextUrlList` — deceptively useful: it usually lists the
   **publisher's working PDF** (e.g. `link.springer.com/...pdf`), not just its own
   blocked `?pdf=render` link. This is how the Springer and BMC PDFs were found.
4. **Unpaywall** `oa_locations[].url_for_pdf` — needs a plausible `?email=`;
   a dummy address returns **422 Unprocessable Entity** (easy to misread as
   "no OA copy"). It also has **no data at all** for some OA DOIs that OpenAlex
   knows about — never rely on it alone.
5. **Semantic Scholar** `openAccessPdf.url`.
6. **Europe PMC `fullTextXML`** — the safety net: even when every PDF host blocks
   the request, the *text* is often retrievable. Use `--text`. Recovered 52,289
   characters of usable full text for a paper whose PDF was fully blocked.

## The 403s are real — don't burn time on workarounds

Cloudflare bot detection on MDPI/ACS/RSC/EPMC rejects:

- browser User-Agent strings ❌
- Referer headers ❌
- a configured HTTP proxy (`DSH_ENDNOTE_PROXY`; unset by default) ❌

**Headless Chrome was tested and does not help** — `--headless=new` with
`--download-directory` downloaded nothing usable and hung. Do not retry that
avenue.

The productive move when a PDF 403s is either a different host (arXiv/repository)
or `--text`.

## `?pdf=render` on Europe PMC

A useful trap to know: `https://europepmc.org/articles/<PMCID>?pdf=render` looks
like the canonical OA PDF URL and appears in Europe PMC's own metadata, but it
**403s for any non-browser client**. Meanwhile
`https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML` **works**.
Same content, different door — reach for the XML when the PDF is refused.

## Verifying a download actually worked

Publishers often return an HTML paywall page with HTTP 200. **Always check the
magic bytes**:

```python
if not blob.startswith(b"%PDF"):
    # it's a web page, not a PDF
```

A "successful" HTTP 200 whose body starts `<!DOC` is a failure. The script does
this check; if you fetch by hand, do it too.

## Then attach it to EndNote

Once you have the file:

```powershell
python <plugin>/scripts\endnote_attach.py --list
python <plugin>/scripts\endnote_attach.py 10 <staging dir>\paper.pdf
```

For a **new** record, `endnote_add.py` already does download-and-attach in one
step (its `%>` field needs a **local path**, not a URL — a URL attaches nothing).

## Troubleshooting

| Symptom | Meaning |
|---|---|
| `not a PDF (starts b'<!DOC')` | Publisher served a paywall/landing page. Try another route. |
| `403` on every route | Genuinely blocked. Use `--text`, or ask the user for the PDF. |
| `PAYWALLED — no open-access copy found` | No free copy exists anywhere. Not a bug: suggest institutional access or ask the user for the file. |
| Unpaywall `422` | The `email=` looks fake. Use a plausible address. |
| arXiv `not found` | No preprint for that DOI. Normal for biology/medicine. |
| Only `--text` works | Fine for reading/searching; tell the user there is no downloadable PDF. |

## Reference

- `<staging dir>\_survey.txt` — the measured route survey, with
  per-paper results and the corrections it forced.
- `<staging dir>\_survey_raw.txt` — raw per-route log.
- Regenerate either with `python <plugin>/scripts\paper_pdf_survey.py`.

The EndNote side of PDF handling — `%>` semantics, the `sdb.eni` write path,
dedupe rules — is documented in the `endnote-add` skill and
`the plugin README`.
