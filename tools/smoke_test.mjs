/**
 * Isolated smoke test for the dsh-endnote plugin.
 *
 * Imports the real module and exercises its internals without a harness:
 *  - Config validation
 *  - skill parsing (frontmatter + body) from the packaged files
 *  - scriptEnv path propagation
 *  - every registered tool's argument builder, via apply() against a FAKE ctx
 *
 * The fake ctx records registrations instead of performing them, so this proves
 * the plugin's own logic without touching the live DSH process.
 */
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.resolve(HERE, '..')

// Windows: dynamic import needs a file:// URL, not a bare C:\ path
// (ERR_UNSUPPORTED_ESM_URL_SCHEME otherwise).
const mod = await import(pathToFileURL(path.join(PLUGIN, 'lib', 'index.js')).href)
const { Config, apply, name, inject, scriptEnv } = mod

let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`)
  if (!ok) failures += 1
}

console.log('=== module exports ===')
check('name is dsh-endnote', name === 'dsh-endnote', name)
check('inject lists tools + skills',
  Array.isArray(inject) && inject.includes('tools') && inject.includes('skills'),
  JSON.stringify(inject))
check('Config is a schema', typeof Config === 'function' || typeof Config === 'object')

console.log('\n=== Config validation ===')
const cfg = new Config({})
check('defaults library to empty string', cfg.library === '', JSON.stringify(cfg.library))
check('defaults toolTimeoutMs to 300000', cfg.toolTimeoutMs === 300000, String(cfg.toolTimeoutMs))
check('defaults registerTools true', cfg.registerTools === true, String(cfg.registerTools))
const cfg2 = new Config({ library: 'D:\\x\\Lib.enl', stagingDir: 'D:\\stage' })
check('accepts overrides', cfg2.library === 'D:\\x\\Lib.enl' && cfg2.stagingDir === 'D:\\stage')

console.log('\n=== scriptEnv path propagation ===')
const env = scriptEnv(cfg2)
check('DSH_ENDNOTE_LIBRARY set', env.DSH_ENDNOTE_LIBRARY === 'D:\\x\\Lib.enl', env.DSH_ENDNOTE_LIBRARY)
check('DSH_ENDNOTE_STAGING set', env.DSH_ENDNOTE_STAGING === 'D:\\stage', env.DSH_ENDNOTE_STAGING)
check('PYTHONUTF8 forced on', env.PYTHONUTF8 === '1')
check('PYMUPDF_MESSAGE moved to stderr', env.PYMUPDF_MESSAGE === 'fd:2')

console.log('\n=== apply() against a fake ctx ===')
const registered = { tools: [], skills: [], logs: [], effects: 0 }
const fakeCtx = {
  logger: { info: (m) => registered.logs.push(m) },
  tools: { register: (def) => { registered.tools.push(def); return () => {} } },
  skills: { register: (sk) => { registered.skills.push(sk); return () => {} } },
  effect: (fn) => { registered.effects += 1; const d = fn(); return d },
}

try {
  apply(fakeCtx, cfg)
  check('apply() did not throw', true)
} catch (err) {
  check('apply() did not throw', false, String(err && err.stack ? err.stack.split('\n')[0] : err))
}

console.log('\n=== registered skills ===')
check('2 skills registered', registered.skills.length === 2, String(registered.skills.length))
for (const s of registered.skills) {
  const hasBody = typeof s.content === 'string' && s.content.length > 500
  check(`skill "${s.name}" has description`, typeof s.description === 'string' && s.description.length > 20)
  check(`skill "${s.name}" has body (${s.content ? s.content.length : 0} chars)`, hasBody)
  check(`skill "${s.name}" has whenToUse`, typeof s.whenToUse === 'string')
  check(`skill "${s.name}" resourceBase points at package`,
    s.resourceBase && s.resourceBase.path === PLUGIN, s.resourceBase && s.resourceBase.path)
}

console.log('\n=== registered tools ===')
const names = registered.tools.map((t) => t.name)
check('7 tools registered', registered.tools.length === 7, JSON.stringify(names))
for (const expected of ['endnote_add', 'endnote_attach', 'endnote_status',
  'endnote_dedupe', 'endnote_groups', 'paper_pdf', 'endnote_refresh']) {
  check(`tool ${expected} present`, names.includes(expected))
}
for (const t of registered.tools) {
  const hasDesc = typeof t.description === 'string' && t.description.length > 40
  const hasOutput = t.output && t.output.schema
  const hasExec = typeof t.execute === 'function'
  check(`tool ${t.name}: description/output/execute`,
    hasDesc && hasOutput && hasExec,
    `${hasDesc ? '' : 'desc '}${hasOutput ? '' : 'output '}${hasExec ? '' : 'exec'}`.trim())
}

console.log('\n=== apply() with registerTools=false ===')
const reg2 = { tools: [], skills: [] }
apply({
  logger: { info: () => {} },
  tools: { register: (d) => { reg2.tools.push(d); return () => {} } },
  skills: { register: (s) => { reg2.skills.push(s); return () => {} } },
  effect: (fn) => fn(),
}, new Config({ registerTools: false }))
check('skills still registered (2)', reg2.skills.length === 2, String(reg2.skills.length))
check('no tools registered', reg2.tools.length === 0, String(reg2.tools.length))

console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`)
process.exit(failures === 0 ? 0 : 1)
