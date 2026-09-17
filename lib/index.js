/**
 * dsh-endnote — EndNote library integration for the DeepSeek Harness.
 *
 * What it provides
 * ----------------
 * 2 skills and 7 native tools, all backed by ONE set of Python scripts:
 *
 *   skills : endnote-add, paper-pdf          (prose guidance, model-invocable)
 *   tools  : endnote_add, endnote_attach, endnote_groups, endnote_dedupe,
 *            endnote_status, paper_pdf, endnote_refresh
 *
 * Skills carry the judgement (which route to try, what to verify, how to phrase
 * results); tools carry the mechanics. Both call the same vendored scripts, so
 * there is no second implementation to drift — the exact failure this plugin was
 * built to eliminate (endnote_add used to carry its own weaker PDF fetcher and
 * silently failed on papers paper_pdf could download).
 *
 * Why tools shell out to Python instead of reimplementing in JS
 * -------------------------------------------------------------
 * The EndNote work is inherently Python: sqlite3 with custom collation shims for
 * the `refs` triggers, ctypes for the Win32 window calls, and the HTTP clients.
 * Everything used is the standard library — there is no third-party Python
 * dependency to install. Reimplementing it in Node would duplicate ~2000 lines
 * and re-introduce the drift. Piped child_process exec is verified working here.
 *
 * @module dsh-endnote
 */

import { execFile, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const run = promisify(execFile)

/**
 * `defineTool` is loaded defensively.
 *
 * It is a thin validator/wrapper (it converts the parameter spec to JSON Schema,
 * validates args, and forwards to `execute`), and the @deepseek-ai packages are
 * OPTIONAL peers: a `link:` install resolves the real path, so the profile's
 * node_modules is never on the resolution chain and the import fails. When the
 * real one is unavailable we fall back to an equivalent local implementation,
 * so the plugin works either way.
 *
 * The schema conversion is the part the fallback cannot skip. The author writes
 * a bare map of properties, where `required: true` on a property is an
 * author-only marker that the real `defineTool` lifts into the schema's root
 * `required` array (`parameterSchemaSpecToJsonSchema`). Passing the raw map
 * through ships the provider a parameter schema with no `type: 'object'`, and
 * every request that advertises these tools fails before the model runs:
 *   400 Invalid schema for function 'endnote_add':
 *       schema must be a JSON Schema of 'type: "object"', got 'type: null'
 */
/** Author-only annotation keys, forwarded onto the compiled node. */
const PROPERTY_ANNOTATIONS = ['description', 'title', 'default', 'examples']

/** Compile one property node, in the order the real compiler emits it. */
function compilePropertyNode(author) {
  const compiled = {}
  if (author.type !== undefined) compiled.type = author.type
  for (const key of PROPERTY_ANNOTATIONS) {
    if (Object.hasOwn(author, key)) compiled[key] = author[key]
  }
  for (const [key, value] of Object.entries(author)) {
    if (key === 'type' || PROPERTY_ANNOTATIONS.includes(key)) continue
    if (key === 'properties') {
      const nested = compilePropertyMap(value)
      compiled.properties = nested.properties
      if (nested.required !== undefined) compiled.required = nested.required
      continue
    }
    compiled[key] = value
  }
  return compiled
}

/**
 * Compile an author-facing property map to raw JSON Schema.
 * `required: true` is the only author-only key the plugin uses: it belongs in
 * the schema's root `required` array, not inside the property, and dropping it
 * there loses the marker; forgetting to re-emit `type` on the property loses
 * the type. Both are silent — the harness validator only complains about a
 * property that keeps `enum` without a type, and the provider 400s on the root.
 */
function compilePropertyMap(spec = {}) {
  const properties = {}
  const required = []
  for (const [key, node] of Object.entries(spec)) {
    if (node === null || typeof node !== 'object') {
      throw new TypeError(`dsh-endnote: parameter "${key}" must be a schema object`)
    }
    const { required: isRequired, ...author } = node
    properties[key] = compilePropertyNode(author)
    if (isRequired === true) required.push(key)
  }
  return { properties, ...(required.length > 0 ? { required } : {}) }
}

let defineTool
try {
  ({ defineTool } = await import('@deepseek-ai/dsh-tools'))
} catch {
  defineTool = (options) => ({
    name: options.name,
    description: options.description,
    parameters: { type: 'object', ...compilePropertyMap(options.parameters) },
    output: options.output,
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
    execute: options.execute,
  })
}

/** Same story for the config schema helper. */
let z
try {
  const mod = await import('@deepseek-ai/schemastery')
  z = mod.default ?? mod
} catch {
  z = undefined
}

/** Stable Loader identity. */
export const name = 'dsh-endnote'

/** Package root: lib/index.js -> package root. Keeps the bundle relocatable. */
const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SCRIPTS = path.join(PACKAGE_ROOT, 'scripts')
const SKILLS = path.join(PACKAGE_ROOT, 'skills')

/** Services used: tools to register, skills to contribute to the catalog. */
export const inject = ['tools', 'skills']

/** Validated plugin configuration. */
export const Config = z
  ? z.object({
      library: z.string().default(''),
      stagingDir: z.string().default(''),
      endnoteExe: z.string().default(''),
      pythonExe: z.string().default(''),
      toolTimeoutMs: z.number().default(300000),
      registerTools: z.boolean().default(true),
    })
  : /** Fallback applying the same defaults when schemastery is unavailable.
     *
     * It must satisfy BOTH contracts, because they are not the same one:
     *   - cordis never calls `Config` as a constructor. It validates through
     *     the Standard Schema interface, i.e. `Config['~standard']
     *     .validate(raw)` (cordis `resolveConfig`). A plain class has no
     *     `~standard`, so the loader threw "Cannot read properties of
     *     undefined (reading 'validate')" and took the whole plugin tree down.
     *   - the local tests (tools/smoke_test.mjs, tools/validate_patch.mjs)
     *     exercise `new Config(cfg)` to check the defaults.
     * A class with a static `~standard` is callable both ways. This is what
     * keeps the plugin loadable when the optional peer is missing, which is
     * the case for a `link:` install: Node resolves the real path, so the
     * profile's node_modules is not on the resolution chain. */
    class Config {
      constructor(input = {}) {
        Object.assign(this, Config['~standard'].validate(input).value)
      }

      static '~standard' = {
        version: 1,
        vendor: 'dsh-endnote',
        validate(input = {}) {
          return {
            value: {
              library: input.library ?? '',
              stagingDir: input.stagingDir ?? '',
              endnoteExe: input.endnoteExe ?? '',
              pythonExe: input.pythonExe ?? '',
              toolTimeoutMs: input.toolTimeoutMs ?? 300000,
              registerTools: input.registerTools ?? true,
            },
          }
        },
      }
    }

// ---------------------------------------------------------------- helpers

/** Read a packaged skill's frontmatter and body. */
function readSkill(dirName) {
  const file = path.join(SKILLS, dirName, 'SKILL.md')
  const raw = fs.readFileSync(file, 'utf8')
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/.exec(raw)
  if (!m) throw new Error(`dsh-endnote: ${dirName}/SKILL.md has no YAML frontmatter`)
  const block = m[1]
  const body = m[2]
  const field = (key) => {
    const mm = new RegExp(`^${key}:\\s*(.+)$`, 'm').exec(block)
    if (!mm) return undefined
    let v = mm[1].trim()
    if (v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1)
    return v
  }
  const name = field('name')
  const description = field('description')
  if (!name || !description) {
    throw new Error(`dsh-endnote: ${dirName}/SKILL.md needs name and description`)
  }
  return { name, description, whenToUse: field('whenToUse'), content: body }
}

/**
 * Build the environment the scripts read for path resolution.
 *
 * This is the seam that makes the plugin configurable: the Python side has zero
 * hardcoded paths, so pointing `library` at another install retargets the whole
 * toolchain.
 */
function scriptEnv(config) {
  const env = { ...process.env }
  if (config.library) env.DSH_ENDNOTE_LIBRARY = config.library
  if (config.stagingDir) env.DSH_ENDNOTE_STAGING = config.stagingDir
  if (config.endnoteExe) env.DSH_ENDNOTE_EXE = config.endnoteExe
  if (config.pythonExe) env.DSH_ENDNOTE_PYTHON = config.pythonExe
  // Expose where the bundled scripts live, so skill instructions and anyone
  // debugging a run can locate them without knowing the install path.
  env.DSH_ENDNOTE_SCRIPTS = SCRIPTS
  // Keep non-ASCII paper titles intact through the child process pipe.
  env.PYTHONUTF8 = '1'
  env.PYTHONIOENCODING = 'utf-8'
  // Defensive only: a tool result is parsed as text, so any library that prints an
  // import-time warning to stdout would corrupt it. The bundled scripts use only
  // the standard library, so nothing here needs this — it guards against a user's
  // environment supplying an extra package that a script might import.
  env.PYMUPDF_MESSAGE = 'fd:2'
  return env
}

/**
 * Resolve which interpreter runs the scripts.
 *
 * Order: explicit config, then DSH_ENDNOTE_PYTHON, then whatever is running DSH
 * (which is a real interpreter and almost always has Python available beside it),
 * then the common launcher names.
 *
 * Probing matters because a bare `python` is often the Microsoft Store stub on
 * Windows, and some machines only expose the `py` launcher — in both cases every
 * tool would fail with a confusing error. The first candidate that actually
 * answers `-c pass` wins; if none do, the last candidate is returned so the
 * failure message still names something concrete.
 */
let cachedPython
function pythonFor(config) {
  if (config.pythonExe) return config.pythonExe
  if (process.env.DSH_ENDNOTE_PYTHON) return process.env.DSH_ENDNOTE_PYTHON
  if (cachedPython) return cachedPython

  const candidates = [process.execPath, 'python', 'python3', 'py']
  for (const exe of candidates) {
    try {
      const res = spawnSync(exe, ['-c', 'pass'], { timeout: 8000, windowsHide: true })
      if (res.status === 0) {
        cachedPython = exe
        return exe
      }
    } catch { /* try the next candidate */ }
  }
  return 'python'
}

/**
 * Run one vendored script and return its combined output.
 *
 * Scripts routinely exit non-zero to mean "the answer is no" (no duplicate found,
 * no open-access copy). That is a normal result, not a tool error, so the exit
 * code is reported in the text rather than thrown — the model needs to read the
 * message either way.
 */
async function runScript(config, scriptName, args, signal, timeoutMs) {
  const script = path.join(SCRIPTS, scriptName)
  if (!fs.existsSync(script)) {
    throw new Error(`dsh-endnote: script missing: ${script}`)
  }
  try {
    const { stdout, stderr } = await run(pythonFor(config), [script, ...args], {
      env: scriptEnv(config),
      timeout: timeoutMs ?? config.toolTimeoutMs,
      maxBuffer: 16 * 1024 * 1024,
      windowsHide: true,
      signal,
    })
    return [stdout, stderr].filter((s) => s && s.trim()).join('\n').trim()
  } catch (err) {
    // A non-zero exit still carries useful output in stdout/stderr.
    const out = [err.stdout, err.stderr].filter((s) => s && s.trim()).join('\n').trim()
    if (out) return `${out}\n[exit code: ${err.code ?? 'unknown'}]`
    if (err.killed || err.signal) throw new Error(`dsh-endnote: ${scriptName} was interrupted`)
    throw new Error(`dsh-endnote: ${scriptName} failed: ${err.message}`)
  }
}

/** Declare a text-output tool that forwards one script invocation. */
function scriptTool({ toolName, description, parameters, script, buildArgs, config }) {
  return defineTool({
    name: toolName,
    description,
    parameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      const argv = buildArgs(args)
      return await runScript(config, script, argv, exec.signal)
    },
  })
}

// ---------------------------------------------------------------- plugin

/**
 * Register the skills and (optionally) the native tools.
 * @param ctx - agent-scoped services; `tools` and `skills` are injected.
 * @param config - validated plugin configuration.
 */
export function apply(ctx, config) {
  const disposers = []

  // ---- skills: always registered, regardless of registerTools ----
  for (const dirName of ['endnote-add', 'paper-pdf']) {
    const skill = readSkill(dirName)
    disposers.push(ctx.skills.register({
      name: skill.name,
      description: skill.description,
      ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
      content: skill.content,
      // Point relative references at the packaged scripts so the model can find
      // them even when the workspace has no copy.
      resourceBase: { kind: 'directory', path: PACKAGE_ROOT },
      metadata: { plugin: 'dsh-endnote', scripts: SCRIPTS },
    }))
  }

  if (!config.registerTools) {
    ctx.logger?.info?.('dsh-endnote: skills registered, native tools disabled by config')
    ctx.effect(() => () => {
      for (const d of disposers) { try { d() } catch { /* teardown best effort */ } }
    })
    return
  }

  // ---- tools: thin wrappers over the same scripts the skills invoke ----
  const tools = [
    scriptTool({
      config,
      toolName: 'endnote_add',
      script: 'endnote_add.py',
      description:
        'Add a paper to the user\'s EndNote library from a DOI, PMID, title, or journal URL, ' +
        'downloading and attaching its open-access PDF by default. Resolves metadata from ' +
        'Crossref/Europe PMC/OpenAlex, writes a tagged .enw using EndNote\'s own field codes, ' +
        'and hands it to EndNote. ' +
        'This one call can also file the paper into a group and make it searchable — pass ' +
        '`group` and/or `refresh` instead of calling endnote_groups and endnote_refresh ' +
        'separately, because each extra tool call costs far more than the work it does. ' +
        'When `group` or `refresh` is used the tool waits for EndNote to actually apply the ' +
        'import (it is asynchronous) and reports the new record number.',
      parameters: {
        identifier: {
          type: 'string',
          required: true,
          description: 'DOI, DOI URL, PMID/PMCID, article title, or journal URL.',
        },
        group: {
          type: 'string',
          description:
            'Which group to file the record into. Leave UNSET for the default, which ' +
            'is automatic: the best-matching existing group is chosen from the paper\'s ' +
            'own title/abstract/keywords, based on how similar it is to that group\'s ' +
            'existing members. If no group matches convincingly the paper is still ' +
            'added but not filed, and the reason is reported — so check the output and ' +
            'file it manually if a group was wanted. Pass an exact name to force a ' +
            'group, or "none" to skip filing entirely.',
        },
        groupCreate: {
          type: 'boolean',
          description:
            'Allow `group` to create the group when no such group exists. Off by default so ' +
            'a typo does not silently create one.',
        },
        refresh: {
          type: 'boolean',
          description:
            'After importing, re-export the library and rebuild the search index so the paper ' +
            'becomes findable. Prefer this over a separate endnote_refresh call.',
        },
        waitSeconds: {
          type: 'number',
          description:
            'How long to wait for EndNote to apply the import, in seconds (default 30). Only ' +
            'matters when `group` or `refresh` is set.',
        },
        noPdf: {
          type: 'boolean',
          description: 'Metadata only; do not look for or attach a PDF.',
        },
        dryRun: {
          type: 'boolean',
          description:
            'Resolve and show the .enw without launching EndNote. Worth using when the ' +
            'identifier is a TITLE, to confirm the resolved paper is the intended one.',
        },
      },
      buildArgs: (a) => [
        a.identifier,
        ...(a.noPdf ? ['--no-pdf'] : []),
        ...(a.dryRun ? ['--dry-run'] : []),
        // Unset means "auto" (the script's default). "none" and "" explicitly
        // disable filing; any other value is taken as a group name.
        ...(a.group === undefined || a.group === null ? []
          : (String(a.group).toLowerCase() === 'none' || a.group === ''
            ? ['--no-group'] : ['--group', a.group])),
        ...(a.groupCreate ? ['--group-create'] : []),
        ...(a.refresh ? ['--refresh'] : []),
        ...(typeof a.waitSeconds === 'number' ? ['--wait', String(a.waitSeconds)] : []),
      ],
    }),
    scriptTool({
      config,
      toolName: 'endnote_attach',
      script: 'endnote_attach.py',
      description:
        'Attach a PDF file to an EXISTING EndNote reference, or list references with their ' +
        'attachments. Use this when the user supplies a PDF, or when a paper imported ' +
        'without one because its publisher blocked automated download. ' +
        'Re-importing a paper does NOT attach to the existing record — it creates a duplicate — ' +
        'which is why this separate route exists.',
      parameters: {
        ref: {
          type: 'string',
          description: 'Reference number (rec-number) from endnote_status, e.g. "16".',
        },
        pdf: {
          type: 'string',
          description: 'Absolute path to the PDF to attach. Omit when listing.',
        },
        list: {
          type: 'boolean',
          description: 'List all references and their attachments, then exit.',
        },
        remove: {
          type: 'string',
          description: 'Reference number to detach the given pdf from.',
        },
      },
      buildArgs: (a) => {
        if (a.list) return ['--list']
        if (a.remove) {
          return a.pdf ? ['--remove', String(a.remove), a.pdf] : ['--remove', String(a.remove), String(a.ref ?? '')]
        }
        return a.pdf ? [String(a.ref ?? ''), a.pdf] : ['--list']
      },
    }),
    scriptTool({
      config,
      toolName: 'endnote_status',
      script: 'endnote_doctor.py',
      description:
        'Health check for the EndNote integration: verifies the install, both library sources ' +
        '(.enl and the unlocked sdb.eni), reports contents, whether the endnote-mcp search ' +
        'index is in sync, and whether the metadata APIs are reachable. Run this first ' +
        'whenever something about EndNote seems wrong.',
      parameters: {
        refs: {
          type: 'boolean',
          description: 'Also list every record in the library.',
        },
      },
      buildArgs: (a) => (a.refs ? ['--refs'] : []),
    }),
    scriptTool({
      config,
      toolName: 'endnote_dedupe',
      script: 'endnote_dedupe.py',
      description:
        'Find and remove duplicate EndNote records. Groups by DOI first then normalized title, ' +
        'keeping the most complete copy. Defaults to a report; pass apply=true to trash the ' +
        'duplicates (EndNote\'s own soft delete, reversible with restore). ' +
        'Show the user the groups before applying.',
      parameters: {
        apply: {
          type: 'boolean',
          description: 'Actually trash the duplicates. Omit to only report them.',
        },
        keep: {
          type: 'string',
          enum: ['complete', 'lowest'],
          description: 'Which copy to keep: the most complete (default) or the lowest record number.',
        },
        trash: {
          type: 'string',
          description: 'Comma-separated record numbers to trash instead of auto-detecting.',
        },
        restore: {
          type: 'string',
          description: 'Comma-separated record numbers to untrash.',
        },
      },
      buildArgs: (a) => {
        const out = []
        if (a.restore) out.push('--restore', ...String(a.restore).split(',').map((s) => s.trim()))
        else if (a.trash) out.push('--trash', ...String(a.trash).split(',').map((s) => s.trim()))
        else if (!a.apply) out.push('--list')
        if (a.keep) out.push('--keep', a.keep)
        return out
      },
    }),
    scriptTool({
      config,
      toolName: 'paper_pdf',
      script: 'paper_pdf.py',
      description:
        'Download the open-access PDF of a paper from a DOI, PMID, arXiv id, or title. ' +
        'Tries arXiv, OpenAlex, Unpaywall, Semantic Scholar and Europe PMC, preferring hosts ' +
        'that do not block automated requests, and verifies the response really is a PDF. ' +
        'When no PDF exists it can save the full text instead. ' +
        'Use this to obtain a file, then endnote_attach to put it on a reference.',
      parameters: {
        identifier: {
          type: 'string',
          required: true,
          description: 'DOI, PMID/PMCID, arXiv id, or article title.',
        },
        out: {
          type: 'string',
          description: 'Output directory. Defaults to the configured staging folder.',
        },
        text: {
          type: 'boolean',
          description: 'If no PDF is obtainable, save the full text instead.',
        },
        list: {
          type: 'boolean',
          description: 'List every candidate source without downloading.',
        },
      },
      buildArgs: (a) => [
        a.identifier,
        ...(a.out ? ['--out', a.out] : []),
        ...(a.text ? ['--text'] : []),
        ...(a.list ? ['--list'] : []),
      ],
    }),
    scriptTool({
      config,
      toolName: 'endnote_groups',
      script: 'endnote_groups.py',
      description:
        'Read and edit EndNote groups. Lists every group with its members, and can create a ' +
        'custom group, add records to one, or remove records from one. ' +
        'Only CUSTOM groups (rule TYPE;3) can be edited: groups derived from an online search ' +
        'or a smart rule are computed by EndNote and are reported as not editable. ' +
        'Call with list=true first to see the groups and the record numbers they contain.',
      parameters: {
        list: {
          type: 'boolean',
          description: 'List all groups and their members.',
        },
        show: {
          type: 'string',
          description: 'Show one group by name (substring match) and its members.',
        },
        create: {
          type: 'string',
          description: 'Name of a new custom group to create.',
        },
        add: {
          type: 'string',
          description: 'Name of an existing custom group to add records to.',
        },
        remove: {
          type: 'string',
          description: 'Name of an existing custom group to remove records from.',
        },
        refs: {
          type: 'string',
          description:
            'Comma-separated record numbers, e.g. "2,5,16". Required with add/remove; ' +
            'optional with create to populate the new group in one step.',
        },
        dryRun: {
          type: 'boolean',
          description: 'Show what would change without writing.',
        },
      },
      buildArgs: (a) => {
        const out = []
        if (a.list) out.push('--list')
        if (a.show) out.push('--show', a.show)
        if (a.create) out.push('--create', a.create)
        if (a.add) out.push('--add', a.add)
        if (a.remove) out.push('--remove', a.remove)
        if (a.refs) out.push('--refs', String(a.refs))
        if (a.dryRun) out.push('--dry-run')
        return out.length ? out : ['--list']
      },
    }),
  ]

  // endnote_refresh delegates to scripts/endnote_refresh.py — the same module
  // `endnote_add --refresh` uses — so the export/index/sync logic exists once.
  tools.push(defineTool({
    name: 'endnote_refresh',
    description:
      'Re-export the EndNote library and rebuild the endnote-mcp search index so newly added ' +
      'or edited references become searchable. Works with EndNote OPEN (it reads the unlocked ' +
      'sdb.eni working copy), and reads EndNote\'s full text extracts. ' +
      'Run this after endnote_add, and after any cleanup.',
    parameters: {
      incremental: {
        type: 'boolean',
        description:
          'Add new records only. Slower to reason about and NEVER removes deleted ones — ' +
          'leave unset unless you know nothing was removed.',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      // Delegates to scripts/endnote_refresh.py, which both this tool and
      // `endnote_add --refresh` call. The logic used to be duplicated here in
      // JavaScript, which is exactly the drift this plugin was built to remove:
      // two implementations of one job, only one of which gets fixed.
      const argv = [path.join(SCRIPTS, 'endnote_refresh.py')]
      if (args.incremental) argv.push('--incremental')
      return await runScript(config, 'endnote_refresh.py', argv.slice(1), exec.signal)
    },
  }))

  for (const definition of tools) {
    disposers.push(ctx.tools.register(definition))
  }

  ctx.logger?.info?.(
    `dsh-endnote: registered ${tools.length} tools and 2 skills (library=${config.library || 'default'})`,
  )

  ctx.effect(() => () => {
    for (const d of disposers) {
      try { d() } catch { /* teardown best effort */ }
    }
  })
}

export { scriptEnv, runScript }
