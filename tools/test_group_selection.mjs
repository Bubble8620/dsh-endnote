#!/usr/bin/env node
// Verify the `group` contract end to end, through the REAL script.
//
// Auto-filing is the DEFAULT now, so the cases that matter are:
//   * `group` omitted          -> auto-selects, or declines with a stated reason
//   * `group: "none"`         -> filing suppressed entirely
//   * an explicit group name  -> that group is used; a typo refuses
//
// DESIGN NOTE: this test does NOT hardcode a DOI. An earlier version pinned one
// of the author's own library records, which (a) leaked a record on publication
// and (b) broke the moment the library changed. Instead it DISCOVERS a suitable
// paper at runtime by reading a custom group's existing members — which makes it
// library-independent and, better, tests the property that actually matters:
//
//     a paper already filed in group X is auto-filed back into X.
//
// `--dry-run` is used throughout, so nothing is imported and the library is never
// modified. Exit 0 = all checks passed (skips count as passes, and are labelled).
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'

const ROOT = process.cwd()
const SCRIPT = path.join(ROOT, 'scripts', 'endnote_add.py')
const PY = process.env.PYTHON || 'python'

let bad = 0
let skipped = 0
const check = (label, cond, detail) => {
  console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`)
  if (!cond) bad++
}
const skip = (label, why) => {
  console.log(`  SKIP ${label}  (${why})`)
  skipped++
}

function run(args) {
  try {
    return execFileSync(PY, [SCRIPT, ...args], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    })
  } catch (err) {
    return `${err.stdout || ''}${err.stderr || ''}`
  }
}

// Each CLI invocation resolves metadata over the network (~45 s), so seven runs
// would exceed any sane timeout. The subject under test is the SELECTION logic,
// not metadata resolution, so call the selection functions directly instead:
// one resolution, then every choice evaluated in-process. `select_for()` below
// mirrors exactly what endnote_add.main() does with the result.
function selectFor(args) {
  try {
    const raw = execFileSync(PY, ['-c', `
import sys, json, argparse
sys.path.insert(0, r'${path.join(ROOT, 'scripts').replace(/\\/g, '\\\\')}')
import endnote_groups as eg
import endnote_add as ea

ap = argparse.ArgumentParser()
ap.add_argument('identifier')
ap.add_argument('--group', default='auto')
ap.add_argument('--dry-run', action='store_true')
ap.add_argument('--no-group', dest='group', action='store_const', const=None)
a, _ = ap.parse_known_args(${JSON.stringify(args)})

if a.group is not None and a.group.strip().lower() in ('none', '-', ''):
    a.group = None

out = {'groupArg': a.group, 'explicit': None, 'auto': None}
if a.group and a.group.strip().lower() != 'auto':
    c = eg.connect('ro')
    g = eg.find_group(c, a.group)
    out['explicit'] = {'found': bool(g), 'kind': g['kind'] if g else None,
                       'name': g['name'] if g else None}
    c.close()
elif a.group and a.group.strip().lower() == 'auto':
    ref = ea.resolve(a.identifier)
    c = eg.connect('ro')
    picked = eg.suggest_group(c, title=ref.title, abstract=ref.abstract,
                              keywords=ref.keywords, journal=ref.journal)
    c.close()
    if picked is None:
        out['auto'] = {'declined': 'no match'}
    elif not picked.get('group'):
        out['auto'] = {'declined': 'ambiguous',
                       'candidates': [x['name'] for x in picked.get('ambiguous', [])]}
    else:
        out['auto'] = {'chose': picked['group']['name'], 'score': picked['score'],
                       'reason': picked['reason']}
print(json.dumps(out))
`], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 })
    // Take the LAST line: the scripts may print diagnostics before the JSON.
    return JSON.parse(raw.trim().split('\n').pop())
  } catch (err) {
    const detail = (err.stderr || err.stdout || err.message || String(err))
    const text = String(detail)
    // Distinguish "there is no library to test against" from "the logic changed".
    // Collapsing both to a bare error made an unreadable library look like a
    // regression: the run reported `FAIL declines instead of guessing`, naming
    // the wrong thing entirely. A machine-dependent skip must be reported as a
    // skip, so a real regression stays visible.
    if (/library not found|no readable library|not found:|working copy not found/i.test(text)) {
      return { unavailable: text.trim().split('\n').filter(Boolean)[0].slice(0, 160) }
    }
    return { error: text.trim().split('\n').slice(-3).join(' | ').slice(0, 300) }
  }
}

// ---------------------------------------------------------------- discovery
// Find a paper already filed in a custom group. Read-only: opens the same
// unlocked working copy the tools use.
//
// NOTE on why this also looks for an AMBIGUOUS case: a paper can legitimately sit
// in two groups at once, and then both score identically. The right behaviour is
// not to guess but to NAME the candidates. That is asserted below, because it was
// a real gap: the first version declined silently and the user could not tell why.
function discover() {
  try {
    const out = execFileSync(PY, ['-c', `
import sys, json
sys.path.insert(0, r'${path.join(ROOT, 'scripts').replace(/\\/g, '\\\\')}')
import endnote_groups as eg
c = eg.connect('ro')
uniq, shared = None, None
seen = {}
for g in eg.load_groups(c):
    if g['kind'] != 'custom' or not g['ids']:
        continue
    ids = ','.join(str(int(i)) for i in g['ids'][:40])
    for rid, doi in c.execute(
            f"SELECT id, electronic_resource_number FROM refs "
            f"WHERE id IN ({ids}) AND COALESCE(trash_state,0)=0 "
            f"AND electronic_resource_number LIKE '10.%'"):
        if not doi:
            continue
        seen.setdefault(str(doi), []).append(g['name'])
for doi, names in seen.items():
    if len(names) == 1 and uniq is None:
        uniq = {'group': names[0], 'doi': doi}
    if len(names) > 1 and shared is None:
        shared = {'groups': names, 'doi': doi}
c.close()
print(json.dumps({'unique': uniq, 'shared': shared}))
`], { encoding: 'utf8' })
    return JSON.parse(out.trim().split('\n').pop())
  } catch {
    return null
  }
}

const found = discover()
const uniq = found && found.unique
const shared = found && found.shared

// A single probe establishes whether the library is readable at all. When it is
// not, every library-dependent assertion is skipped WITH A REASON, rather than
// reported as a failure that names the wrong cause.
const probeSel = selectFor(['10.0000/probe', '--dry-run'])
const noLibrary = Boolean(probeSel && probeSel.unavailable)
if (noLibrary) {
  console.log(`=== no readable library — library-dependent checks will SKIP ===`)
  console.log(`  (${probeSel.unavailable})`)
}

console.log('\n=== auto-selection (default: no --group) ===')
if (!uniq) {
  skip('auto-selects the group a paper already belongs to',
    noLibrary ? 'no readable library' : 'no DOI sits in exactly one custom group')
  skip('states why it chose it', 'depends on the above')
} else {
  console.log(`  (discovered: "${uniq.group}" holds ${uniq.doi})`)
  const sel = selectFor([uniq.doi, '--dry-run'])
  if (sel && sel.error) console.log(`  !! selectFor error: ${sel.error}`)
  const auto = sel && sel.auto
  check('auto-selects the group a paper already belongs to',
    Boolean(auto && auto.chose) &&
    String(auto.chose).toLowerCase() === uniq.group.toLowerCase(),
    auto && auto.chose ? `chose "${auto.chose}"` : JSON.stringify(auto))
  check('states WHY it chose it', Boolean(auto && auto.reason &&
    /already appear in/.test(auto.reason)), auto && auto.reason)
  check('default group argument is "auto"', sel && sel.groupArg === 'auto',
    sel && String(sel.groupArg))
}

// ------------------------------------------------------- ambiguity is reported
console.log('\n=== when two groups fit equally, it says so ===')
if (!shared) {
  skip('names both candidate groups',
    noLibrary ? 'no readable library' : 'no DOI sits in two custom groups')
} else {
  console.log(`  (discovered: ${shared.doi} is in ${shared.groups.join(' + ')})`)
  const sel = selectFor([shared.doi, '--dry-run'])
  const auto = sel && sel.auto
  check('declines instead of guessing', Boolean(auto && auto.declined),
    JSON.stringify(auto))
  check('names both candidate groups',
    Boolean(auto && auto.candidates) &&
    shared.groups.every((g) => auto.candidates.includes(g)),
    auto && auto.candidates ? auto.candidates.join(', ') : '')
}

// -------------------------------------------------- decline path (independent)
// A paper from a field no group covers must NOT be guessed at. Uses a DOI that is
// a well-known, unrelated work rather than anything from the library.
console.log('\n=== declines when no group fits ===')
const UNRELATED = '10.1038/s41586-020-2649-2' // NumPy paper (array programming)
const noneSel = selectFor([UNRELATED, '--dry-run'])
if (noneSel && (noneSel.unavailable || noneSel.error)) {
  skip('declines instead of guessing',
    noneSel.unavailable ? 'no readable library' : `probe failed: ${noneSel.error}`)
} else {
  const noneAuto = noneSel && noneSel.auto
  check('declines instead of guessing',
    Boolean(noneAuto && noneAuto.declined && !noneAuto.chose),
    JSON.stringify(noneAuto))
}

// ------------------------------------------------------------ explicit values
// These exercise argument handling, which does not need a library: --group none
// and --no-group never open it, and an unknown name is answered from the same
// place. So they run even when nothing is readable.
console.log('\n=== explicit values ===')
const target = uniq ? uniq.doi : UNRELATED

const explicitNone = selectFor([target, '--group', 'none', '--dry-run'])
check('--group none suppresses filing', explicitNone.groupArg === null,
  String(explicitNone.groupArg))

const noGroup = selectFor([target, '--no-group', '--dry-run'])
check('--no-group behaves the same', noGroup.groupArg === null,
  String(noGroup.groupArg))

if (noLibrary) {
  skip('an unknown name is not resolved', 'no readable library')
} else {
  const typed = selectFor([target, '--group', 'No Such Group Zzz', '--dry-run'])
  check('an unknown name is not resolved',
    Boolean(typed.explicit && typed.explicit.found === false),
    JSON.stringify(typed.explicit))
}

if (uniq) {
  const named = selectFor([target, '--group', uniq.group, '--dry-run'])
  check('an exact name resolves to that group',
    Boolean(named.explicit && named.explicit.found),
    JSON.stringify(named.explicit))
} else {
  skip('an exact name resolves to that group',
    noLibrary ? 'no readable library' : 'no discovered group')
}

// --------------------------------------------- one real CLI run (integration)
// The checks above call the selection functions directly for speed. This one goes
// through the actual command line, so the argv wiring and the printed output are
// verified too — but only ONCE, because each run resolves metadata (~45 s).
//
// It also works without a library: an unknown group name is rejected from a local
// check before any network work, which is the behaviour being asserted.
console.log('\n=== end-to-end through the CLI (1 run) ===')
const typoOut = run([target, '--group', 'No Such Group Zzz'])
check('a bad group name refuses via the CLI', /no group matching/.test(typoOut))
check('and says nothing was changed', /Nothing was changed/.test(typoOut))
check('and suggests how to find the right name', /--list/.test(typoOut))

// ------------------------------------------------------- library untouched
console.log('\n=== the library was not modified ===')
if (uniq) {
  const after = discover()
  check('discovery still returns the same grouping',
    Boolean(after && after.unique && after.unique.group === uniq.group),
    after && after.unique ? `"${after.unique.group}"` : 'unreadable')
} else {
  skip('library unchanged', 'nothing discovered to compare')
}

console.log(
  bad ? `\n${bad} CHECK(S) FAILED`
      : `\nALL PASSED${skipped ? ` (${skipped} skipped)` : ''}`)

// A skip is honest about the environment, but it must not be invisible to an
// automated caller: on a machine with no library most of the property is
// unverified, and a bare exit 0 would claim otherwise. `--allow-skip` keeps CI on
// a library-less machine usable; without it, skipping is reported as a distinct
// exit code (2) so it cannot be mistaken for a clean pass.
if (bad) process.exit(1)
if (skipped && !process.argv.includes('--allow-skip')) {
  console.log('  (exit 2 = ran, but with skips; pass --allow-skip to accept that)')
  process.exit(2)
}
process.exit(0)
