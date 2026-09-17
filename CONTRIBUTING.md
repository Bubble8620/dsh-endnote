# Contributing

Thanks for considering a contribution. This plugin writes to a user's EndNote
library outside any supported API, so correctness matters more than features.
Please read the [AI-USAGE.md](AI-USAGE.md) risk section before changing anything
that writes.

## Ground rules

1. **Never commit user data.** No `.enl`, `.Data/`, `.eni`, `library.db`, `.enw`
   or PDFs. `.gitignore` covers these; do not override it.
2. **No hardcoded personal paths.** Run the audit before opening a PR:
   ```powershell
   python tools/audit_publication.py   # must PASS
   python tools/verify_package.py      # must PASS
   ```
   Absolute paths belong in configuration or `scripts/endnote_paths.py` defaults,
   never inline. **Watch the escaping trap:** a path appears as `C:\<dir>\<file>`
   in prose but `"C:\\<dir>\\<file>"` in source, so a naive regex matches only one
   of the two forms and reports a false PASS. That bug actually shipped here once;
   `tools/audit_publication.py` now tolerates both forms and its comment explains
   why.
3. **Prefer refusing over guessing.** If a write's safety is unclear (an
   unrecognised group type, an unexpected schema), raise or skip and say so.
   Silent partial success is the worst outcome.
4. **Test by running, not by reasoning.** Several early bugs survived careful
   reading and died immediately under execution.
5. **Do not add author-identifying content to examples.** Use invented DOIs and
   generic group names; a shipped example that happens to be the author's own
   paper, library entry or group name discloses their research area. Run
   `python tools/audit_bibliography.py` — it cross-checks embedded DOIs against
   the local library and flags any overlap.

## Development setup

```powershell
# install the plugin from your checkout
dsh plugin --profile web add link:<absolute path to this directory>
# restart DSH afterwards — bundle lists are not hot-reloaded
```

Requirements: Windows, EndNote 21, Python 3.10+, and a library you can restore
from a backup.

## Before every pull request

```powershell
python tools/audit_publication.py     # no author-identifying paths in shipped files
python tools/audit_publication.py --strict   # also checks maintainer scripts
python tools/audit_bibliography.py    # no real library DOI/topic in examples
python tools/verify_package.py        # the npm tarball ships no .pyc or user data
python tools/test_publication.py      # works when extracted as a stranger receives it
python tools/test_discovery.py        # library auto-discovery regression tests
node   tools/test_group_selection.mjs # auto group selection: all forms + decline path
node   tools/validate_patch.mjs       # patch YAML + config plumbing
node   tools/smoke_test.mjs           # assertions, no real host needed
node   tools/e2e_test.mjs             # really runs the tools (read-only ops)
python tools/check_skills.py          # vendored skills are valid
```

**Run the audits AFTER any build step, not before.** If you re-vendor
(`vendor_scripts.py`, `vendor_skills.py`), the generated copies are what ship, so an
audit run beforehand validates a tree that no longer matches the package. That
happened for real here: a DOI was scrubbed from a master, the audit went green, and
re-vendoring restored it. Order: **build → audit → commit.**

`e2e_test.mjs` and `smoke_test.mjs` assume a reachable EndNote library; with none
present, expect the tool wrappers to report that clearly rather than pass.
`audit_bibliography.py` needs the local library to compare against and will say so
if it cannot read one. That check is also a **moving target** — a DOI that is safe
today becomes a finding the moment that paper is added to the library, so re-run it
after any library change.

## Editing the sources vs the vendored copies

`scripts/` and `skills/` are **generated**. Their master copies live in the
author's workspace (`tools/` next to the plugin, and `$DSH_HOME/skills`). After
changing a master, re-sync and re-test:

```powershell
python tools/normalize_bootstrap.py   # keep the path bootstrap canonical
python tools/vendor_scripts.py        # re-copy scripts, strip author paths
python tools/vendor_skills.py         # re-copy skills, rewrite paths
```

This two-step flow exists because the plugin must ship a self-contained copy
while the master sources reference the author's layout. If you are contributing
only to the plugin, editing `scripts/` directly is fine — just do not also carry
a divergent master.

**Skill precedence caution:** the plugin registers skills at runtime rank 250,
which outranks the user skills directory at rank 400. The plugin's `endnote-add`
and `paper-pdf` therefore **shadow** any same-named skill in `$DSH_HOME/skills`.
Editing the user copy has no effect while the plugin is installed.

## Code style

- Python: standard library only for the shipped scripts (no new runtime
  dependencies), type hints, `from __future__ import annotations`, and a module
  docstring explaining *why* the file exists rather than what it contains.
- JavaScript: ESM, no build step. `lib/index.js` must load with only optional
  `@deepseek-ai/*` peers available — the profile's `node_modules` may not contain
  them, so keep the defensive imports.
- Comments should record **non-obvious constraints and measured facts** (why a
  field needs a local path, why little-endian). Do not narrate obvious code.

## Reporting bugs

Include:

- your EndNote version, and whether the library was open or closed
- the exact command or tool call, and its full output
- whether the problem is reproducible after `endnote_refresh`

For anything involving data loss, please state whether you had a backup, and
open an issue before attempting a fix.

## Scope

Welcome: additional EndNote field coverage, more publisher routes, better
diagnostics, tests that do not require a live library.

Out of scope: writing bibliographic fields (the sort-key replay deliberately
refuses), Mac/Linux support without someone able to test it, and any change that
writes to a library without a dry-run or a reversible path.
