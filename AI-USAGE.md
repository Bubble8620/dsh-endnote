# AI usage disclosure

This plugin was written with substantial AI assistance. This document states
plainly what that means, what was verified, and where confidence is lower — so
you can judge the risk yourself rather than take a blanket assurance.

## How it was produced

An AI coding agent (DeepSeek Harness, model `deepseek-v4.1-flash`) wrote the
code, ran the experiments, and drafted this documentation, working under
continuous human direction and review. The human specified the objectives,
supplied the EndNote installation and library used for testing, ran the GUI
confirmations that a program cannot perform, and decided what to ship.

Concretely:

| Part | Author |
|---|---|
| All Python scripts under `scripts/` | AI-written |
| `lib/index.js` (plugin entry, tool/skill registration) | AI-written |
| `tools/*.mjs` test and validation harnesses | AI-written |
| `tools/vendor_*.py` maintainer scripts | AI-written |
| `README.md`, `AI-USAGE.md`, `CHANGELOG.md`, commit messages | AI-drafted, human-reviewed |
| `LICENSE` | Standard MIT text, selected by the human |
| The skill texts (`skills/*/SKILL.md`) | AI-drafted from its own run notes |
| Design decisions, priorities, scope | Human |
| GUI verification (does the group/attachment actually appear?) | **Human** |

## What was verified experimentally

These are not assumptions. Each was established by running an experiment, and
several contradicted the AI's first guess. The measurements are reproducible with
the scripts in `tools/`.

| Claim | How it was established |
|---|---|
| EndNote holds an **exclusive OS lock on `.enl`** while running; `mode=ro` and `immutable=1` both fail | Direct connection attempts; then polling for 75 s showed a single state with no read window |
| **`<lib>.Data/sdb/sdb.eni` is an unlocked working copy** with the same `refs`/`file_res` tables | Opened it read-only while EndNote was running, and confirmed the tables and the columns the exporter reads are all present |
| The **`.enw` `%>` field attaches a file only when it holds a local path** | Imported with a URL (no `file_res` row appeared) vs an absolute local path (EndNote copied the PDF and registered it) |
| A **minimized EndNote silently ignores the import** | `.enl` stayed byte-identical until the window was foregrounded; after focusing, the record appeared |
| **`refs` writes fail without two SQL shims** (`ENCIN_zh_CN` collation, `EN_MAKE_SORT_KEY`) | `UPDATE refs` raised `no such collation sequence: ENCIN_zh_CN`; the trigger DDL was read from `sqlite_master` |
| Replaying the existing sort key **reproduces `refs_ord` byte-for-byte** | Performed the update inside a transaction, compared keys, rolled back |
| **Group membership is little-endian** | Big-endian produced ids like `50331648` that cannot exist; little-endian produced real ids, identical across three library backups |
| Only **custom groups** (`TYPE;3`) are editable; `TYPE;6` is an online-search group | Read every group's `spec` XML and cross-checked the member ids against `refs` |
| **Created groups survive an EndNote write** | Created a group, then forced an import to make EndNote write the library, then re-read |
| **`index --full` is required to prune**; incremental `index` never removes | Trashed records, ran incremental index (still searchable), then `--full` (removed) |
| Publisher PDF hosts differ sharply (403 vs works) | A survey of 10 papers × 6 routes; results in the skill's reference section |

## What is **not** verified — treat as assumption

Be more careful here. These are inferences or untested paths.

1. **Group `recs_stamp` semantics are unknown.** The tool bumps it to the current
   time because it looks like a change marker. Everything worked in testing, but
   whether EndNote requires it, ignores it, or uses it for caching is **not
   established**. If group edits ever appear stale in the GUI, this is the first
   suspect.

2. **The `TYPE;3` / `TYPE;6` rule meanings are inferred**, not documented. They
   were deduced from the group names (`TYPE;6` belongs to the four online-search
   groups) and from which groups held member lists. Some group type may exist
   that this tool misclassifies. The tool errs toward refusing edits, but an
   unrecognised-but-editable type would be blocked.

3. **The `tag_members` FTS table was never used.** It exists and EndNote maintains
   it, but its role (possibly legacy tagging) is not understood. Nothing writes to
   it. If EndNote keeps group state there in some configuration, this tool would
   miss it.

4. **Only EndNote 21 was tested.** The discovery logic is version-agnostic and
   probes `EndNote*` directories, but EndNote 20 was never run. Internal schema
   differences between versions are plausible.

5. **Mac and Linux are unsupported and untested.** `sdb.eni` layout, the
   `.enw` association and window focusing are all Windows-specific.

6. **Field-level ordering is not validated.** `EN_MAKE_SORT_KEY` is replayed
   rather than recomputed, so a record whose author or year is edited by *this*
   tool would be refused (it raises) rather than mis-sorted. That is deliberate
   but means these tools cannot edit bibliographic fields at all.

7. **`recs_stamp`-style caches and GUI refresh timing are not characterised.**
   Changes are usually visible immediately; the tool suggests switching records
   if a paperclip does not appear. That is advice, not a measurement.

8. **Single-machine testing.** Every measurement in this document comes from one
   Windows 11 machine with one EndNote 21 install. The library held 6 records when
   the core behaviour was tested and has since grown to 12 (9 active); nothing has
   been re-measured at the larger size. There is no CI matrix and no second user.
   Behaviours listed as "verified" are verified *there*; a different EndNote build
   or a library with thousands of records is untested.

## A self-audit that failed, and what it cost

Worth recording because it is the clearest example of this project's failure mode.

The plugin shipped with `tools/audit_publication.py`, whose job was to find
author-identifying paths before publication. It printed **PASS**. It was wrong.

Its pattern was a raw string naming the author's path with a single backslash on
each side, which as a *regex* matches that **one**-backslash text. But in Python
source such a path is written with **two** backslashes per separator. So the
pattern matched Markdown and never matched Python, and two real author paths sat
in shipped `.py` files while the audit reported clean. The same audit
also had no email pattern and no notion of bibliographic content, so it missed a
real contact address and three DOIs that were also records in the author's own
library.

It was caught only by running **two independent adversarial audits** against the
tree rather than trusting the shipped checker. The lesson, and the reason the test
harnesses are committed: *a passing check proves nothing unless the check itself
has been attacked.* `tools/verify_package.py`, `tools/test_discovery.py` and
`tools/audit_bibliography.py` now exist specifically because the original audit
was silently blind in three separate ways.

That episode also produced the tiered design now in `audit_publication.py`:
BLOCKING (author-identifying) versus CONVENTIONAL (`C:\Program Files (x86)\EndNote 21`
is the same on every machine). The split exists so the report stays short enough
to read — a checker that cries wolf gets ignored, which is how a real leak slips
through.

## Risk statement

**These tools write to your EndNote library directly**, outside any API Clarivate
provides. EndNote publishes no write interface (`EndNote.Application` does not
exist; `EndNote21.AddinServer` exposes no usable methods), so there is no
supported path — this plugin works around that.

Therefore:

- **Back up before destructive operations.** `endnote_backup.py` copies the
  working copy and PDF index; for a complete backup, close EndNote and copy the
  library yourself.
- **Prefer the soft delete.** `endnote_dedupe` defaults to `trash_state = 1`
  (EndNote's own "Move References to Trash"), which is reversible with
  `--restore`. `--hard` removes rows and is only advisable with EndNote closed
  and a backup taken.
- **Group edits and attachment writes were tested on a small library (6 records at the
  time; 12 records / 9 active now).**
  Behaviour on a library with thousands of records, or one with smart groups,
  is untested.
- **The author is not liable** for library corruption or data loss — see LICENSE
  (MIT, no warranty).

## Honest quality assessment

- **Well tested where it counts:** 38 isolated assertions, a real-execution test
  over every tool, patch-file validation, and a publication audit. The
  behaviours listed as "verified" above are solid, and several were discovered
  precisely because an earlier AI guess was wrong and the experiment overturned it.
- **Weakest areas:** group `recs_stamp` semantics, unusual group types, and any
  EndNote installation other than this one. Also note the whole codebase shares
  one author's single-machine testing — there is no CI matrix, no other user has
  run it, and the tests exercise this library rather than synthetic fixtures.
- **AI-specific failure modes seen and guarded against:** the agent twice
  inserted a code block in a position Python rejected (`from __future__` must come
  first), and once wrote a self-test that asserted on text the tool never prints —
  a passing-looking suite that reported a working tool as broken. Both were
  caught by running things rather than reasoning about them, which is why the
  test harnesses are committed.

If you find a case where a "verified" item in the table does not hold on your
machine, that is a bug report worth filing — the measurement scripts are included
so the claim can be re-checked rather than trusted.
