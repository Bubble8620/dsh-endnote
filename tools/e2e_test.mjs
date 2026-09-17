/**
 * End-to-end test: call each registered tool's execute() and confirm it runs the
 * real Python scripts and returns real output.
 *
 * ── WHAT THIS DOES AND DOES NOT TOUCH ────────────────────────────────────────
 * The library is NOT modified: nothing here adds, trashes, or re-groups a
 * reference, and no tool writes to the EndNote database.
 *
 * It is NOT entirely side-effect-free, and the earlier comment claiming otherwise
 * was wrong. `endnote_refresh` rebuilds the endnote-mcp SEARCH INDEX — a derived,
 * rebuildable cache, not the library. That still counts as a write, so:
 *
 *   * it is enabled only when DSH_ENDNOTE_MCP_DB is set, which points the index
 *     somewhere disposable;
 *   * otherwise it is skipped with a notice, so a developer running this on their
 *     own machine does not silently rewrite their real index.
 *
 * The refresh's index location is pinned by `endnote_refresh.py` itself (it writes
 * a temporary config and passes `--config`), so this honours the variable rather
 * than reaching whatever the global endnote-mcp config points at.
 */
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.resolve(HERE, '..')
const mod = await import(pathToFileURL(path.join(PLUGIN, 'lib', 'index.js')).href)
const INDEX_IS_DISPOSABLE = Boolean(process.env.DSH_ENDNOTE_MCP_DB)

const registered = []
const ctx = {
  logger: { info: () => {} },
  tools: { register: (d) => { registered.push(d); return () => {} } },
  skills: { register: () => () => {} },
  effect: (fn) => fn(),
}

// Configuration is deliberately left empty so the plugin's own discovery runs
// (env var, $DSH_HOME/endnote.json, endnote-mcp config, then a scan). Hardcoding
// a library here would both break on other machines and bake the author's path
// into a published file.
const config = new mod.Config({
  toolTimeoutMs: 240000,
})
mod.apply(ctx, config)

const byName = Object.fromEntries(registered.map((t) => [t.name, t]))
const fakeExec = { signal: undefined }

let failures = 0
async function exercise(toolName, args, expect) {
  const tool = byName[toolName]
  if (!tool) {
    console.log(`  FAIL ${toolName}: not registered`)
    failures++
    return ''
  }
  const started = Date.now()
  try {
    const out = String(await tool.execute(args, fakeExec))
    const ms = Date.now() - started
    const ok = expect(out)
    console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${toolName} (${ms}ms, ${out.length} chars)`)
    if (!ok) {
      failures++
      console.log('       --- output head ---')
      console.log(out.split('\n').slice(0, 12).map((l) => '       ' + l).join('\n'))
    } else {
      console.log('       ' + out.split('\n').filter((l) => l.trim())[0].slice(0, 100))
    }
    // Returned so a later step can inspect it — e.g. discovering a real group name
    // instead of hardcoding one.
    return out
  } catch (err) {
    console.log(`  FAIL ${toolName}: threw ${err && err.message}`)
    failures++
    return ''
  }
}

console.log('=== endnote_status --refs ===')
// NOTE: assert on strings the doctor actually prints. An earlier version of this
// check required "library:" with a colon, which the report never emits (it says
// "Library access"), so a working tool was reported as failing.
await exercise('endnote_status', { refs: true },
  (o) => /library access/i.test(o) && /records\s*:/i.test(o) && /in sync|stale/i.test(o))

console.log('\n=== endnote_dedupe (report only) ===')
await exercise('endnote_dedupe', {},
  (o) => /duplicate/i.test(o))

console.log('\n=== paper_pdf --list (no download) ===')
await exercise('paper_pdf', { identifier: '10.3390/v13061131', list: true },
  (o) => /candidate|DOI|blocked|try/i.test(o))

console.log('\n=== endnote_attach --list ===')
await exercise('endnote_attach', { list: true },
  (o) => /active reference|attachment/i.test(o))

console.log('\n=== endnote_groups (list) ===')
await exercise('endnote_groups', { list: true },
  (o) => /group\(s\)|custom|not editable/i.test(o))

console.log('\n=== endnote_groups (show one) ===')
// Discover a group name at runtime rather than hardcoding one. A hardcoded name
// both fails on a machine that lacks it and discloses the author's own group
// naming in a committed file. This asks the library and uses whatever it has.
const listed = await exercise('endnote_groups', { list: true },
  (o) => /group\(s\)|custom|not editable/i.test(o))
const groupName = (String(listed).match(/\[custom\]\s+"([^"]+)"/) ||
                   String(listed).match(/"([^"]+)"\s+\(/))?.[1]
if (groupName) {
  await exercise('endnote_groups', { show: groupName },
    (o) => /group\s*:|members\s*:/i.test(o))
} else {
  console.log('SKIP endnote_groups (show one) — no custom group present')
}

console.log('\n=== endnote_refresh (rebuilds a derived index) ===')
if (INDEX_IS_DISPOSABLE) {
  await exercise('endnote_refresh', {},
    (o) => /resolved paths|refresh|verify|reindex/i.test(o))
} else {
  // Skipped by default: this is the one case that writes anything, and it writes
  // to a search index. Point DSH_ENDNOTE_MCP_DB at a throwaway path to include it.
  console.log('  SKIP endnote_refresh — set DSH_ENDNOTE_MCP_DB to a disposable path ' +
              'to run it (it rewrites the search index, not the library)')
}

console.log(`\n${failures === 0 ? 'ALL TOOLS EXECUTED' : failures + ' TOOL(S) FAILED'}`)
process.exit(failures === 0 ? 0 : 1)
