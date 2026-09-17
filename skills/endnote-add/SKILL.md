---
name: endnote-add
description: "Add a paper to the user's EndNote library from a DOI, PMID, title, or URL — typically right after finding it via web search — and attach its open-access PDF. Resolves metadata from Crossref/Europe PMC/OpenAlex, writes a tagged .enw using EndNote's own field codes, hands it to EndNote, verifies the record landed, and can attach a PDF the user supplies to an existing reference."
whenToUse: "Use when the user wants a paper added to their EndNote library — e.g. they say '加入我的endnote库', 'add this to my EndNote', 'import this paper', or after you find an article they express interest in. Also use when they want a PDF attached to an existing EndNote reference, or hand you a PDF to attach. Works from a DOI, a PMID/PMCID, a title, a journal URL, or a local PDF file."
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


# Adding a paper (and its PDF) to the user's EndNote library

This is the user's **personal** library. Always say what you are about to add and
report what actually landed. Never claim success without verifying.

## Add a paper — the PDF is attached automatically

```powershell
python <plugin>/scripts\endnote_add.py <identifier>
```

The PDF is **on by default**. The tool resolves metadata, finds the
open-access PDF, and hands EndNote a `.enw` whose `%>` field points at the
**downloaded local file** — which is what makes EndNote actually attach it.

> **Do not hand-roll PDF retrieval.** Downloading goes through
> `tools\paper_pdf.py`, which owns the route knowledge (arXiv, repository
> mirrors, Europe PMC XML). It USED to be duplicated here with a weaker
> 4-URL list, which silently failed on papers the downloader could fetch
> (ACS `10.1021/acssynbio.2c00465`: "every source declined" vs 2.9 MB from a
> repository mirror). Both tools now share one implementation — if a PDF cannot
> be obtained, use the **`paper-pdf`** skill rather than writing new fetch code.

### Filing into a group is automatic by default

`endnote_add` picks a group **for you** unless told otherwise. Leave `--group`
unset and it chooses the best-matching **existing custom** group, then files the
record there:

```powershell
# auto-selects a group, adds the record, and shows why it chose that group
python <plugin>/scripts\endnote_add.py 10.1038/s41586-020-2649-2
```

How the choice is made — and why it is not a name match: the paper's own
title/abstract/keywords are compared against the **titles and keywords of each
custom group's existing members**. Existing members are ground truth about what a
group is for; a group *name* alone is not ("Chapter drafts" — does a paper on
phage encapsulation belong there? unclear). Only **custom** groups are considered,
because a derived group's membership is computed by EndNote and cannot be written.

**It refuses when the evidence is thin.** Two situations produce no filing, and
both print the reason:

- **No group fits** — nothing scores high enough. The paper is still added.
- **Two groups fit about equally** — a real case, not a bug: a paper can already
  sit in two groups, so both score identically. The tool **names both candidates**
  instead of guessing. Ask the user which one, or pick from the list.

A paper silently filed into the *wrong* group is worse than one not filed, because
you will not notice until you go looking for it and cannot find it. **Read the
output**: if it says "not chosen" or "none chosen", say so and offer to file it
once the user says where.

Always report which group was used, and say so if the tool declined to pick one.

| Form | Effect |
|---|---|
| `--group` omitted | **auto-select** the best-matching custom group |
| `--group "Exact name"` | file into that group; a mistyped name **refuses** (nothing is changed) |
| `--group auto` | same as omitting it, stated explicitly |
| `--group none` / `--no-group` | do not file at all |
| `--group-create` | with an explicit name, create the group if it is missing |

Auto-selection runs **after** metadata is resolved (it needs the title) and
**before** the import, so an unwanted choice is cheap to correct and never leaves
a half-finished state.

### Do the whole job in ONE call

Adding a paper and filing it in a group used to take five separate tool calls
(`add`, `groups --list`, `groups --add`, `refresh`, `status`). Each round trip
costs far more than the work inside it — measured: **~26 s of wall clock per call
against ~2 s of actual work per step**. So `endnote_add` performs the follow-up
steps itself, in one process:

```powershell
# import + auto-file into the best-matching group + make it searchable
python <plugin>/scripts\endnote_add.py 10.1038/s41586-020-2649-2 --refresh
```

| Flag | Effect |
|---|---|
| `--refresh` | after importing, re-export the library and rebuild the index |
| `--refresh-incremental` | with `--refresh`, add new records only (**never prunes**) |
| `--wait SECONDS` | how long to wait for EndNote to apply the import (default 30) |
| `--no-pdf` | metadata only; skip the PDF entirely |
| `--dry-run` | build and show the `.enw` **without** launching EndNote |
| `--verify <doi>` | confirm a record is in the library + whether the index is in sync |
| `--list` | list the library's records |
| `--tags` | print the `.enw` tag map |

Use the separate `endnote_groups` / `endnote_refresh` tools only to inspect an
intermediate state (e.g. `--list` to see every group) or for a standalone refresh
after a cleanup.

### The import is asynchronous — hence `--wait`

`EndNote.EXE <file>.enw` returns immediately; EndNote applies the record some time
later. Anything that must act on the *new* record (filing it, indexing it) has to
wait for it to appear. The tool polls the unlocked `sdb.eni` working copy for up
to `--wait` seconds (default 30), then **gives up with an explanation** rather
than hanging — a minimized or busy EndNote may never apply the import.

Because auto-filing is the default, this wait happens on a normal add **whenever a
group is chosen**. If no group matches (see above), nothing is polled and the tool
returns as soon as EndNote accepts the file.

### When to bother with `--dry-run` and `backup`

Both are cheap (measured: **0.0 s** for `--dry-run`, **1.6 s** for a backup), so
the reason to skip them is not speed — it is not doing pointless steps. Scope them
to where they earn their place:

| Situation | `--dry-run` | `backup` |
|---|---|---|
| DOI or PMID, routine add | skip — the identifier is unambiguous | skip |
| **Title** as the identifier | **use it** — confirms you resolved the paper the user meant, not a similarly-titled one | skip |
| First run on a new library or install | skip | **run it** |
| `--hard` delete, or any direct `refs` write | skip | **run it** |
| Bulk / repeated adds | skip | skip |

`backup` copies `sdb.eni` + `pdb.eni`; it cannot copy the `.enl` while EndNote
holds it open. It is the only thing guarding an irreversible mistake, so keep it
for the destructive cases above — not for every add.

When no PDF can be downloaded it saves the **full text** instead (Europe PMC
`fullTextXML`, which works even where the PDF host 403s) and prints the link for
a manual download. It never writes a URL into `%>`, because that attaches nothing.

`<identifier>` may be a DOI, DOI URL, PMID/PMCID, title, or journal URL.

> **Load-bearing detail:** `%>` must hold a **local file path**. Given a URL,
> EndNote stores a dead link and attaches nothing. If the PDF cannot be
> downloaded, the tool omits `%>` rather than leaving a broken link.

## Attach a PDF to an EXISTING reference

Use this when the user supplies a PDF, or when a paper imported without one
(its publisher blocked automated download).

```powershell
python <plugin>/scripts\endnote_attach.py --list                 # find the reference number
python <plugin>/scripts\endnote_attach.py 10 C:\path\to\paper.pdf   # attach
python <plugin>/scripts\endnote_attach.py --doi 10.3390/v13061131 paper.pdf
python <plugin>/scripts\endnote_attach.py 10 C:\path\to\paper.pdf --dry-run
python <plugin>/scripts\endnote_attach.py --remove 10 paper.pdf  # detach
```

There is **no built-in batch mode**: attach one file per reference, and always
`--list` first to get the right number.

**Why this needs its own mechanism:** attaching via EndNote's importer is
impossible — re-importing a DOI creates a **duplicate record** (verified: two
records ended up sharing one DOI). Instead this writes a `file_res` row into the
library's unlocked working copy `sdb.eni` and copies the PDF into
`<lib>.Data/PDF/<folder>/`. Verified end-to-end: the row survives a subsequent
EndNote write, and EndNote's GUI shows the attachment.

## Groups — reading and editing

```powershell
python <plugin>/scripts\endnote_groups.py --list
python <plugin>/scripts\endnote_groups.py --show "Reading list"     # one group
python <plugin>/scripts\endnote_groups.py --create "Reading list"
python <plugin>/scripts\endnote_groups.py --create "X" --refs 10,11,12   # create + fill
python <plugin>/scripts\endnote_groups.py --add "X" --refs 11,6
python <plugin>/scripts\endnote_groups.py --remove "X" --refs 11
```

Add `--dry-run` to any write to preview it. Names match exactly first, then by
substring, so `--show "reading"` finds `"Reading list"`.

### Only CUSTOM groups are editable

Each group's `spec` XML carries a `<rules>` element that identifies its type:

| Rule | Kind | Editable? |
|---|---|---|
| `TYPE;3` | **custom / manual** — an explicit member list | **yes** |
| `TYPE;6` | online-search group (PubMed, Web of Science, …) | no — derived |
| other | smart group computed from a rule | no — derived |

The tool reports non-custom groups as *"derived from a search or connection"* and
**refuses to edit them** rather than writing a member list EndNote would ignore.
Always `--list` first: if the user names a group that turns out to be a search
group, say so instead of forcing it.

### How membership is stored (why the endianness matters)

One table holds everything:

```
groups(group_id INTEGER PRIMARY KEY, recs_stamp INTEGER, spec BLOB, members BLOB)
```

`members` is the 4-byte constant `00 00 00 02` followed by **little-endian
uint32** record ids; a single `0` word means empty. Verify with `--probe`, which
prints both decodings side by side.

> A big-endian reading was tried first and produced plausible-*looking* but
> impossible ids (e.g. `50331648`). Little-endian yields real ids in the
> library's range, and the same decode is stable across every backup on disk.
> Getting this wrong would silently corrupt membership, so `--probe` keeps both
> interpretations visible.

Verified: a created group and its membership **survive a subsequent EndNote
write** (tested by forcing an import), so this is a durable edit, not a cache.

### Trashed members

A group can list a record that is in the trash or has been deleted. `--list`
shows those as `(record no longer in the library)` and they are counted
separately from active members — mention this rather than reporting a wrong count.

## Cleaning up duplicates

Duplicate records are the normal consequence of importing — **importing a paper
already in the library creates a NEW record** (verified), so re-importing is
never a way to "refresh" an entry.

```powershell
python <plugin>/scripts\endnote_dedupe.py --list      # report duplicates, change nothing
python <plugin>/scripts\endnote_dedupe.py --dry-run   # show what would be trashed
python <plugin>/scripts\endnote_dedupe.py             # trash duplicates (soft delete)
python <plugin>/scripts\endnote_dedupe.py --trash 13 15       # specific records
python <plugin>/scripts\endnote_dedupe.py --trash 13,15       # comma form also works
python <plugin>/scripts\endnote_dedupe.py --restore 13        # undo a trash
```

`--trash` and `--restore` accept either spelling (`13 15` or `13,15`, any
mixing of spaces and commas). The record numbers are validated **before** any
write, so `--trash 28,abc` changes nothing and names the bad token.

It groups by **DOI** first, then by normalized title, and keeps the **most
complete** copy (one with an attachment and more metadata) by default; use
`--keep lowest` to keep the lowest record number instead.

Always run `--list` first and show the user the groups before trashing anything.
After trashing, refresh the index so the removed records stop being searchable.

### Writing to the library: the two shims it needs

Anything that **updates `refs`** fails in plain Python sqlite3 with
`no such collation sequence: ENCIN_zh_CN`, because EndNote defines an AFTER
UPDATE trigger:

```sql
refs__refs_ord_AU: AFTER UPDATE ON refs
  DELETE FROM refs_ord WHERE ro_id = old.id;
  INSERT INTO refs_ord (ro_trash_state, ro_key_2, ro_key_3, ro_id)
    VALUES (new.trash_state, EN_MAKE_SORT_KEY(new.author, 2, 12),
            EN_MAKE_SORT_KEY(new.year,   3, 12), new.id);
```

`endnote_dedupe.py` supplies both missing pieces **without reimplementing
EndNote's logic**: it registers a dummy `ENCIN_zh_CN` collation, and replays the
existing `refs_ord` sort key (keyed by the input value the trigger passes). Since
this tool never modifies `author`/`year`, the correct key already exists; if a
lookup ever misses, the function **raises** rather than writing a corrupted key.
Verified: the trigger reproduces `refs_ord` byte-for-byte.

**Hard deletes need less than you'd think.** The schema's own AFTER DELETE
triggers clean `refs_ord`, `tag_members` (and its FTS shadow tables) and
`ret_watch`; only `file_res` and the PDF on disk need manual handling. Verified
that a plain `DELETE FROM refs` leaves zero orphans.

### Prefer the soft delete

Default is `trash_state = 1` — EndNote's own "Move References to Trash". Verified
to survive a subsequent EndNote write and to be skipped by the XML exporter. Use
`--hard` only with EndNote closed and a backup taken.

## Index refresh — and why it must be `--full`

```powershell
powershell -NoProfile -File <refresh script>
```

**This works while EndNote is open** (it reads `sdb.eni`).

The script defaults to `endnote-mcp index --full` on purpose: the plain `index`
command is **incremental and never prunes**, so a deleted or trashed reference
stays searchable forever. Verified: after trashing 5 records the index still
returned them; `--full` cleared them. Pass `-Incremental` only if you know you
have added nothing and removed nothing.

`endnote_doctor.py` now reports this directly — if it prints
`still indexed but gone from the library`, run the refresh.

## Then refresh the index, or nothing is searchable

```powershell
powershell -NoProfile -File <refresh script>
```

**This works while EndNote is open** — never tell the user to close EndNote. It
reads `sdb.eni`, printing `Read from: sdb.eni (EndNote open — unlocked working copy)`.

Finish by searching with `mcp__endnote__search_library` to prove the paper is
findable (and, when a PDF is attached, that full text matches), then give the
user the record number (`[16]`).

## How it works, and the EndNote quirks it handles

```
DOI/title → Crossref + Europe PMC + OpenAlex → .enw → EndNote imports → index refresh
```

Field codes are **not guessed**: decoded from EndNote's own filter
`Filters\EndNote Import.enf` (`%A` author, `%T` title, `%J` journal, `%D` year,
`%R` DOI, `%X` abstract, `%>` link-to-PDF, …).

**Quirk 1 — a minimized EndNote silently ignores the import.** Verified: the
`.enl` stayed byte-identical until EndNote was foregrounded. The tool restores and
focuses the window itself, printing `EndNote brought to the foreground.` If that
line is missing, ask the user to click EndNote.

**Quirk 2 — `.enl` is exclusively locked while EndNote runs.** Not even `mode=ro`
or `immutable=1` can read it. Reads use the unlocked `sdb.eni` mirror.

**Quirk 3 — PDF downloads are host-dependent.** Measured on this machine:

| Works | Blocked (HTTP 403 to any non-browser client) |
|---|---|
| link.springer.com, nature.com, frontiersin.org, arxiv.org | mdpi.com, europepmc.org, pubs.acs.org, pubs.rsc.org |

The tool tries several candidate URLs and keeps the first that returns real PDF
bytes. When all fail, the record still imports and the tool prints the link — tell
the user to fetch it in a browser and offer to attach it with `endnote_attach.py`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `could not resolve this identifier` | Bad DOI, or title absent from Crossref/Europe PMC. Get a DOI. |
| `! missing: title/authors/year` | Source has thin metadata. Try the DOI. |
| `every source declined` | Publisher blocks automation. Record imported without PDF; fetch in browser, then attach. |
| `! ref#N already has this PDF` | Dedupe working (EndNote renames files, so it compares normalized names + sizes). |
| `NOT FOUND` after import | EndNote closed/minimized, or no library open. Check EndNote, then retry. |
| `MCP index: … STALE` | Expected before a refresh. Run the refresh script. |

## Health check

```powershell
python <plugin>/scripts\endnote_doctor.py --refs
```

Before a **destructive** operation — a `--hard` delete, a direct `refs` write, or
the first run against a new library — back it up. Not for routine adds; see the
scoping table above.

```powershell
python <plugin>/scripts\endnote_backup.py
```

It exits non-zero when nothing was copied, so a scripted
`endnote_backup.py && …` cannot mistake an absent library for a successful backup.
