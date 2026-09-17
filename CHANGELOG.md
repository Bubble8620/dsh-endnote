# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed — the unused GitHub Actions publish workflow

`.github/workflows/publish.yml` was written for OIDC trusted publishing while
the npm 2FA block was being worked around. The package was ultimately published
with a local token, so the workflow never ran a single time (verified: 0 runs)
and nothing referenced it — no run, no tag, no doc.

It was never in the npm tarball either: `files` excludes `.github`, and the
published `dsh-endnote@1.0.0` tarball contains 20 files with no `.github` in
them. So this only removes dead CI configuration from the repository.

Worth keeping in mind if the account's 2FA ever blocks a token-based publish
again: the OIDC route needs this file back, plus a matching Trusted Publisher
entry on npmjs.com (user, repo, workflow filename, and `npm publish` as an
allowed action).

### Added — published to npm, with a registry install path

`dsh-endnote@1.0.0` is on the npm registry, so the README now leads with the
registry install (`dsh plugin --profile web add dsh-endnote`) and keeps the
local-checkout `link:` form as the development route.

Documented alongside it, because it costs a confusing few minutes otherwise:
**switching between the two needs a `remove` first.** `dsh plugin` forwards to
pnpm, and `add dsh-endnote` while a `link:` of the same name is present is a
silent no-op — pnpm sees the dependency key already satisfied and prints
"resolution step is skipped", leaving the link untouched (verified). Neither
command fails, so without the note the install simply appears not to work.

### Security — three verified data-loss paths closed

An independent adversarial audit wrote to copies of a real library and proved
three ways this plugin could destroy user data. All three are fixed and covered
by `tools/test_data_loss_guards.py`:

- **A group whose membership blob it could not parse was silently emptied.** An
  ordinary `--add` decoded a malformed blob as `[]` and rewrote it, permanently
  losing every member. `decode_members` now validates the format (including its
  prefix) and the write path **refuses** any group it cannot fully decode, instead
  of guessing.
- **A hard delete unlinked PDFs before commit.** The filesystem has no rollback,
  so a mid-transaction failure left files deleted while the database rolled back —
  records pointing at files that no longer existed. File deletion now happens
  **after** commit, so the database is the source of truth.
- **An attachment stored as an absolute or `..`-escaping path could be unlinked
  outside the library.** `PDFS / r"C:\…\file"` rebases the base; an unchecked
  delete could remove a file with no relation to the library. A shared
  `resolve_attachment()` now returns `None` for anything outside the PDF tree, and
  both delete paths skip it.

### Fixed — from six independent audits

- **`test_publication.py` disagreed with `audit_publication.py` about the same
  tree**, in both directions. It reported the package's own declared
  `noreply` address as a leak (a false positive that fires whenever the local
  account name is a prefix of the public handle), while missing institutional
  hosts entirely because it never read `audit_publication.personal.txt`. It now
  **redacts only the declared public handle** before matching — so a real address
  sharing that line is still caught, which a whole-line skip would have hidden —
  and consults the same git-ignored pattern file as the audit. Verified against
  three negative controls: a foreign email, an author path, and an institutional
  domain are each still reported.
- **`--trash` / `--restore` rejected the comma-separated spelling.** The sibling
  tools in this suite (`endnote_groups --refs`, `endnote_add --group`) all take a
  comma list, so `--trash 28,29` is the form reached for first; it died on
  argparse's bare `invalid int value: '28,29'`, which reads as a broken tool
  rather than a formatting difference. Both `--trash 28 29` and `--trash 28,29`
  (and any mixing) now work. Ids are still validated **before** any write, so
  `--trash 28,abc` changes nothing and names the offending token.
- **`--restore` reported itself as "trashing".** The log verb was chosen from the
  mechanism, not the direction, so an untrash printed `trashing 2 record(s)` —
  in the one line a user reads to confirm what happened to their library. The
  verb now follows the direction: `trashing` / `restoring` / `would restore`.

- **`paper_pdf_survey.py` crashed on import** with `NameError: os` — an edit added
  `os.environ.get` without `import os`, and the vendoring step made it look
  internally consistent. It also ran its whole network survey at import time, so
  even `--help` hung. Now wrapped in `main()`; every shipped script is import-
  checked by `tools/test_imports.py`.
- **`paper_pdf --text` was a no-op.** The flag was accepted and never read; the
  full-text fallback ran unconditionally. It now means what it says: skip the PDF
  download and go straight to the text. The automatic fallback is unchanged.
- **`endnote_refresh` ignored `DSH_ENDNOTE_MCP_DB`** and rewrote whatever index
  the global endnote-mcp config pointed at. It now writes a temporary config and
  passes `--config`, so the refresh and the health check describe the same index.
  Verified: pointing it at a temp index leaves the real one untouched.
- **Duplicate plan said `<- KEEP` and trashed the record anyway.** A record in two
  duplicate groups (DOI + title) could be elected keeper in one and trashed via
  the other, emptying a group entirely. `plan()` now enforces "every duplicate
  group keeps at least one member", strongest-evidence-first, and the report uses
  the same plan the write executes.
- **`--restore 999` exited 0 as a silent no-op.** It now validates ids and reports
  records that were not actually trashed.
- **Group names were never XML-escaped**, so `--create "R&D <v2>"` made an
  unreadable group and silently didn't add the refs. Escaped on write and
  unescaped on read, so names round-trip.
- **A partial group name silently edited a different group.** A short substring
  argument matched an existing custom group by name. Write paths now require an
  exact name; a unique substring is accepted for reads, and an ambiguous one is
  an error.
- **Attaching a second file to a record that had one removed raised
  IntegrityError** — `file_pos = len(attachments)` collided with the unique index.
  Now `max(file_pos) + 1`.
- **`verify_package.py` used a hand-rolled approximation of npm's selection** and
  could not detect a bad `files` entry; it now calls `npm pack --dry-run` and is
  no longer vacuous (proven: reverting `files` to the old value fails the build).
- **`test_group_selection.mjs` could not tell "no library" from a regression** —
  both collapsed to a failure naming the wrong thing. Unreadable-library checks
  now report SKIP with a reason, and a run that skips anything exits 2 unless
  `--allow-skip`.
- **The publication audit's derived workspace pattern matched only the
  Python-source form**, missing the Markdown form it exists to catch — `Path.parts`
  yields the drive with a trailing separator, which `re.escape` doubled. Fixed and
  canary-verified.
- **`check_skills.py` validated the user's skills, not the vendored ones** the
  command documented. Defaults to the shipped copies now.

### Changed

- `endnote_enf_tags.py` and `paper_pdf_survey.py` are now import-safe (no work at
  import time).
- The publication audit now also detects real **group names** and **author
  surnames** from the library, the two classes it was structurally blind to.
- `.npmignore` no longer claims to be a "second line of defence" for `files`
  (verified empirically that it is not; `files` is the control that matters).
- README documents `DSH_ENDNOTE_PROXY` and `DSH_ENDNOTE_CONTACT`, and notes there
  is no `pip install` step (stdlib only).

### Added — adding a paper files it into a group automatically

`endnote_add` now picks a group for you unless told otherwise. The match is
**evidence-based, not name-based**: the paper's title/abstract/keywords are
compared against the titles and keywords of each **custom** group's existing
members, because those members are ground truth about what a group holds, while a
group name alone is ambiguous ("Chapter drafts" vs a paper on phage encapsulation).
Derived groups (online search, smart rule) are never candidates — EndNote computes
their membership and it cannot be written.

**It declines when the evidence is thin.** No convincing match, or two groups
within 10% of each other, means the paper is added but *not* filed, with the
reason printed. Silently filing into the wrong group is worse than not filing:
the user will not notice until they cannot find the paper.

| Form | Effect |
|---|---|
| `--group` omitted | auto-select the best-matching custom group (new default) |
| `--group "Name"` | file into that group; a typo refuses, changing nothing |
| `--group auto` | the default, stated explicitly |
| `--group none` / `--no-group` | do not file at all |
| `--group-create` | with an explicit name, create the group if missing |

Auto-selection runs **after** metadata is resolved (it needs the title) and
**before** the import, so an unwanted choice costs nothing to correct and never
leaves a half-finished state. Covered by `tools/test_group_selection.mjs`, which
asserts all five forms, the decline path, and that a typo changes nothing.

### Changed — fewer round trips for the common workflow

Adding a paper and filing it into a group took five separate tool calls (`add`,
`groups --list`, `groups --add`, `refresh`, `status`). Each round trip costs far
more than the work inside it — measured at **~26 s of wall clock per call against
~2 s of actual work per step** — so the work is now combined:

- `endnote_add --group "NAME"` files the new record into a group in the same call.
- `endnote_add --group-create` allows that to create a missing group. Off by
  default, so a mistyped name **refuses** instead of silently creating a group.
- `endnote_add --refresh` re-exports and rebuilds the index in the same call.
- `endnote_add --wait SECONDS` bounds how long to wait for EndNote to apply it.

A full "add + group + refresh" now completes in **one call (~31 s)** rather than
five round trips.

Because EndNote's import is **asynchronous**, `--group`/`--refresh` poll the
unlocked `sdb.eni` working copy until the new record appears, then report its
record number. The wait is bounded (default 30 s) and **fails loudly** rather than
hanging, because a minimized or busy EndNote may never apply the import. Without
those flags nothing is polled and behaviour is unchanged.

### Changed — backup and dry-run are scoped, not routine

Measured cost: `--dry-run` **0.0 s**, `backup` **1.6 s**. They were never the
bottleneck, so they are not being cut for speed — they are scoped to where they
earn their place. The skill now says to skip both for a routine DOI/PMID add, use
`--dry-run` when the identifier is a **title** (to confirm the resolved paper is
the intended one), and take a `backup` before destructive work (a `--hard`
delete, a direct `refs` write, or the first run against a new library).

### Added

- `scripts/endnote_refresh.py` — the export/index/sync logic in one place. It
  previously existed only in the plugin's JavaScript entry point, so
  `endnote_add --refresh` would have had to reimplement it. Both callers now share
  one implementation; the JS `endnote_refresh` tool delegates to it, removing
  ~80 lines of duplicated logic.
- `endnote_add --group / --group-create / --refresh / --refresh-incremental / --wait`.

### Fixed

- Refusing a non-custom group said "is a online-search group"; now "an".
- A survey DOI became a library record after the library changed, so a shipped
  example disclosed one. Replaced it, and `audit_bibliography.py` now warns that
  the check is a moving target: re-run it after any library change **and after
  re-vendoring skills**, because the vendored copies are what actually ship.

## [1.0.0] - 2026-09-17

First public release. Packaged from scripts and skills that had been built and
tested against a live EndNote 21 library, then hardened by two independent
adversarial audits (see [AI-USAGE.md](AI-USAGE.md) for what those found).

### Security and privacy fixes from the pre-release audit

These were found *after* the plugin's own audit tool reported PASS; that tool was
itself defective.

- **The publication audit could not see Python source.** Its path pattern was a
  regex matching one backslash, while Python source contains two — so it passed
  while real author paths shipped in `endnote_dedupe.py` and `paper_pdf.py`. The
  patterns are now separator- and escaping-tolerant, with a comment explaining the
  trap, plus new detections for email addresses and institutional-repository hints.
- **A real contact address** was removed from `paper_pdf_survey.py`; the Unpaywall
  contact is now `DSH_ENDNOTE_CONTACT` with an `@example.org`
  default.
- **Three DOIs that were also records in the author's own library** were replaced
  with unrelated open-access papers from the same publishers, and the comment
  advertising that the list was "weighted to the user's field" was removed.
- **The author's real custom group names**, used as examples, were replaced with
  neutral ones. An earlier entry claimed this had already been done; it had not —
  the real names were still present and one of them ships in the npm tarball via
  `skills/endnote-add/SKILL.md`. The claim is corrected here rather than left
  standing, because a changelog that asserts a privacy fix that did not happen is
  worse than one that says nothing.
- **An author sort key from a real library record** was quoted in a comment in
  `scripts/endnote_dedupe.py`; the comment now describes the key's shape without
  reproducing a real author list.
- **`scripts/__pycache__/*.pyc` shipped in the npm tarball**, embedding absolute
  build paths. `files` is now `scripts/*.py`; `tools/verify_package.py` fails the
  build if a `.pyc` or user data would be published. (`.npmignore` is kept as
  defence in depth, but note it does NOT restrict `files` — when `files` is set,
  `.npmignore` replaces npm's default ignore list rather than intersecting with
  it. The `files` allowlist is the control that matters.)
- **TLS verification is no longer disabled unconditionally.** It is relaxed only
  when a proxy is actually configured, instead of accepting a man-in-the-middle on
  every request for users who have none.
- **The hardcoded local proxy** (`127.0.0.1:7897`, the author's setup) is now an
  optional `DSH_ENDNOTE_PROXY`, unset by default, and the proxy leg is skipped
  entirely when unset.

### Fixed

- **`endnote_refresh` did nothing on a fresh install.** It depended on an unshipped
  `refresh-endnote-index.ps1` and, with an empty config, resolved a bare relative
  filename. It now regenerates the XML export itself when the exporter is present
  and always runs `endnote-mcp index --full`, reporting clearly when endnote-mcp is
  absent.
- **Library auto-discovery derived the wrong path** from a `pdf_dir` in the
  endnote-mcp config: `<dir>/My EndNote Library.Data/PDF` yielded `<dir>.enl`,
  discarding the library name. This broke the advertised zero-configuration path.
  Covered by `tools/test_discovery.py`.
- **`endnote_attach --list` gave a raw sqlite traceback** when no library existed,
  because the guard lived only inside `attach()` while every read path bypassed it.
- **`endnote_backup.py` exited 0 after copying nothing** and blamed EndNote even
  when the real problem was an absent library — so a scripted
  `endnote_backup.py && …` believed a backup existed.
- **`require_library()` was dead code**; four scripts had each reimplemented its
  message. All now call the shared helper.
- **Duplicated `import os` lines** from the bootstrap normalizer, which is now
  idempotent and alias-safe.
- Config reading now tolerates a UTF-8 BOM (PowerShell 5.1 and Notepad write one),
  which previously hid the first key.
- Python interpreter is probed at startup (`execPath`, `python`, `python3`, `py`)
  rather than assuming a bare `python`, which is often the Microsoft Store stub.

### Added

- **Plugin scaffold** — DSH bundle (`cordis.patch.yml`), entry point
  (`lib/index.js`), and packaging for `scripts/` and `skills/`.
- **7 native tools**
  - `endnote_add` — add a paper from a DOI, PMID, title or URL, downloading and
    attaching its open-access PDF by default
  - `endnote_attach` — attach, list or remove a PDF on an existing reference
  - `endnote_groups` — list groups and members; create a custom group, add or
    remove records
  - `endnote_dedupe` — find duplicate records and trash them (reversible)
  - `endnote_status` — health check across install, library sources, index sync
    and metadata APIs
  - `paper_pdf` — download an open-access PDF, falling back to full text
  - `endnote_refresh` — re-export the library and rebuild the search index
- **2 skills** — `endnote-add` (library workflows and their traps) and
  `paper-pdf` (per-publisher PDF retrieval strategy).
- **Centralised, portable path resolution** (`scripts/endnote_paths.py`) with
  library discovery: environment variable, `$DSH_HOME/endnote.json`, the existing
  `endnote-mcp` config, then a bounded scan. No path is hardcoded in any shipped
  script, so the package is installable on another machine unchanged.
- **Test and maintenance harnesses** — isolated smoke test (38 assertions), a
  real-execution test over every tool, patch-file validation, a publication audit
  for personal paths, and vendoring scripts to re-sync the sources.

### Verified behaviours

Established by experiment; several overturned an initial assumption. See
[AI-USAGE.md](AI-USAGE.md) for method and for what remains unverified.

- EndNote holds an exclusive OS lock on `.enl` while running, so all reads go
  through the unlocked working copy `<lib>.Data/sdb/sdb.eni` — which is what makes
  index refresh possible without closing EndNote.
- The `.enw` `%>` field attaches a file **only** when it holds a local path; a URL
  produces a dead link and no attachment.
- A minimized EndNote silently ignores an import; the plugin restores and focuses
  the window itself.
- Writing `refs` requires two SQL shims (a collation and `EN_MAKE_SORT_KEY`),
  supplied by replaying the existing sort key rather than reimplementing
  EndNote's algorithm.
- Group membership is little-endian; only custom groups (`TYPE;3`) are editable.
- `endnote-mcp index --full` is required to prune deleted records — the
  incremental form never removes them.

### Known limitations

- Windows only; EndNote 21 only.
- Group `recs_stamp` semantics are not established.
- Only custom groups can be edited; online-search and smart groups are refused.
- Bibliographic field editing is unsupported (the sort-key replay raises rather
  than risk a corrupt key).
- PDF retrieval is host-dependent: arXiv, Springer, Nature, Frontiers, BMC and
  institutional repositories work; MDPI, Europe PMC, ACS and RSC return 403 to
  non-browser clients.

[Unreleased]: https://github.com/Bubble8620/dsh-endnote/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Bubble8620/dsh-endnote/releases/tag/v1.0.0

<!--
URLs added later) with the real GitHub account, and set the repository URL in
package.json if you publish to npm.
-->

