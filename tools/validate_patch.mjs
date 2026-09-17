/**
 * Validate dsh-endnote's cordis.patch.yml the way the loader will read it.
 *
 * A malformed patch layer breaks the whole profile at startup, so this parses the
 * file and asserts the shape the Loader expects: a top-level array of entries with
 * an `insert` list, each carrying `id`, `name` and optional `config`.
 *
 * js-yaml is present in the profile's node_modules (pulled in by dsh-mobile).
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.resolve(HERE, '..')
const PATCH = path.join(PLUGIN, 'cordis.patch.yml')

let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`)
  if (!ok) failures += 1
}

// Resolve js-yaml from wherever it actually exists. The harness checkout carries
// one; the profile does not.
const YAML_BASES = [
  path.join(PLUGIN, 'node_modules'),
  path.join(process.env.USERPROFILE || '', '.dsh', 'profiles', 'web', 'node_modules'),
  path.join(process.env.USERPROFILE || '', '.dsh', 'profiles', 'web',
    '.dsh-module-fallback', 'node_modules'),
  path.join(process.env.LOCALAPPDATA || '',
    'npm-cache', '_npx', 'c8633a242642d858', 'node_modules'),
]
let yaml
for (const base of YAML_BASES) {
  for (const entry of ['index.js', 'dist/js-yaml.mjs', 'dist/js-yaml.js']) {
    try {
      yaml = await import(pathToFileURL(path.join(base, 'js-yaml', entry)).href)
      if (yaml && (yaml.load || (yaml.default && yaml.default.load))) {
        yaml = yaml.load ? yaml : yaml.default
        break
      }
      yaml = undefined
    } catch { /* try next candidate */ }
  }
  if (yaml) break
}
if (!yaml) {
  try { const m = await import('js-yaml'); yaml = m.load ? m : m.default } catch { /* ignore */ }
}
if (!yaml) {
  console.log('  SKIP js-yaml unavailable; doing a structural text check instead')
  const text = fs.readFileSync(PATCH, 'utf8')
  check('starts with a YAML list entry', /^\s*-\s/m.test(text))
  check('declares - insert:', /-\s*insert:/.test(text))
  check('declares id: dsh-endnote', /id:\s*dsh-endnote/.test(text))
  check('declares name: dsh-endnote', /name:\s*dsh-endnote/.test(text))
  process.exit(failures === 0 ? 0 : 1)
}

console.log('=== parsing cordis.patch.yml ===')
const doc = yaml.load(fs.readFileSync(PATCH, 'utf8'))
check('parses to an array', Array.isArray(doc), Array.isArray(doc) ? `${doc.length} entr(ies)` : typeof doc)

const entry = Array.isArray(doc) ? doc.find((e) => e && e.insert) : undefined
check('has an entry with `insert`', Boolean(entry))
if (entry) {
  const rows = entry.insert
  check('insert is a list', Array.isArray(rows), Array.isArray(rows) ? `${rows.length} row(s)` : typeof rows)
  const row = Array.isArray(rows) ? rows[0] : undefined
  check('row has id: dsh-endnote', row && row.id === 'dsh-endnote', row && row.id)
  check('row has name: dsh-endnote', row && row.name === 'dsh-endnote', row && row.name)
  const cfg = row && row.config
  check('row has a config object', cfg && typeof cfg === 'object')
  if (cfg) {
    // Defaults are intentionally EMPTY strings so the Python side auto-discovers
    // the library. An earlier version of this check asserted that the config
    // carried a real path, which made a portable shipped default look like a
    // failure. What matters is the shape and that it reaches the scripts.
    check('config.library is a string', typeof cfg.library === 'string',
      JSON.stringify(cfg.library))
    check('config.stagingDir is a string', typeof cfg.stagingDir === 'string',
      JSON.stringify(cfg.stagingDir))
    check('config.toolTimeoutMs is a number', typeof cfg.toolTimeoutMs === 'number', String(cfg.toolTimeoutMs))
    check('config.registerTools is a boolean', typeof cfg.registerTools === 'boolean', String(cfg.registerTools))
    const mod = await import(pathToFileURL(path.join(PLUGIN, 'lib', 'index.js')).href)
    const validated = new mod.Config(cfg)
    check('config validates against the plugin Config',
      validated.toolTimeoutMs === cfg.toolTimeoutMs,
      `timeout=${validated.toolTimeoutMs}`)
    // The plumbing that matters: a NON-empty value must reach the child process
    // as DSH_ENDNOTE_LIBRARY. Tested with a synthetic value so the assertion does
    // not depend on what the shipped defaults happen to be.
    const probe = new mod.Config({ library: 'X:\\probe\\Lib.enl', stagingDir: 'X:\\probe\\stage' })
    const env = mod.scriptEnv(probe)
    check('config values reach the scripts as DSH_ENDNOTE_*',
      env.DSH_ENDNOTE_LIBRARY === 'X:\\probe\\Lib.enl'
      && env.DSH_ENDNOTE_STAGING === 'X:\\probe\\stage',
      env.DSH_ENDNOTE_LIBRARY)
    check('plugin exposes DSH_ENDNOTE_SCRIPTS',
      typeof env.DSH_ENDNOTE_SCRIPTS === 'string' && env.DSH_ENDNOTE_SCRIPTS.includes('scripts'),
      env.DSH_ENDNOTE_SCRIPTS)
  }
}

console.log('\n=== package.json dsh.bundle.patch points at it ===')
const pkg = JSON.parse(fs.readFileSync(path.join(PLUGIN, 'package.json'), 'utf8'))
check('dsh.bundle.patch === ./cordis.patch.yml',
  pkg.dsh && pkg.dsh.bundle && pkg.dsh.bundle.patch === './cordis.patch.yml',
  pkg.dsh && pkg.dsh.bundle && pkg.dsh.bundle.patch)
check('main === lib/index.js', pkg.main === 'lib/index.js', pkg.main)
check('files ships scripts and skills', Array.isArray(pkg.files)
  && pkg.files.some((f) => f.startsWith('scripts'))
  && pkg.files.includes('skills'), JSON.stringify(pkg.files))
// `scripts` was widened to `scripts/*.py` on purpose: shipping the whole
// directory also shipped scripts/__pycache__/*.pyc, which embed the build
// machine's absolute paths. `.npmignore` is the second line of defence.
check('files does not ship the whole scripts dir (no .pyc)',
  Array.isArray(pkg.files) && !pkg.files.includes('scripts'),
  JSON.stringify(pkg.files))

console.log(`\n${failures === 0 ? 'PATCH FILE VALID' : failures + ' CHECK(S) FAILED'}`)
process.exit(failures === 0 ? 0 : 1)
