# dsh-endnote

EndNote 文献库集成插件:把论文加进 EndNote(自动附带开放获取 PDF)、给已有文献补/换附件、
管理 group、查重清理、刷新检索索引,以及单独下载开放获取 PDF。

一个装好即用的 DSH 插件:2 个 skill + 7 个原生工具,全部共用同一套 Python 实现。

**English** | [中文说明见下](#中文说明)

---

## English

### What it does

An [DSH](https://github.com/deepseek-ai/deepseek-harness) plugin that turns a
local EndNote 21 library into something an agent can actually operate:

- **Add a paper** from a DOI, PMID, title or URL — and attach its open-access PDF.
- **Attach a PDF** to a reference that is already in the library.
- **Manage groups** — list them with their members, create a custom group, add or
  remove records.
- **Find and remove duplicates.**
- **Refresh the search index** so new records are findable.
- **Download an open-access PDF** on its own, falling back to the full text when
  no PDF is obtainable.

Everything works **while EndNote is open**. See [Design notes](#design-notes) for
why that is not obvious.

### Requirements

- Windows (the EndNote automation is Windows-specific)
- EndNote 21 (20 may work; the discovery logic is version-agnostic)
- Python 3.10+ on `PATH`, or point `pythonExe` at one
- DSH with the `web` profile
- Optional: [`endnote-mcp`](https://github.com/gokmengokhan/endnote-mcp) if you
  also want read-only search tools inside conversations. This plugin works
  without it; the two complement each other.

### Install

From the npm registry:

```powershell
dsh plugin --profile web add dsh-endnote
```

Or from a local checkout (development):

```powershell
dsh plugin --profile web add link:<absolute path to this directory>
```

Then **restart DSH** — bundle lists are not hot-reloaded.

> **Switching between the two?** Remove first. `dsh plugin` forwards to pnpm, so
> `add dsh-endnote` while a `link:` of the same name is present is a no-op —
> pnpm sees the dependency key already satisfied and skips resolution (verified:
> it printed "resolution step is skipped" and left the link in place). So to go
> from a checkout to the published version:
>
> ```powershell
> dsh plugin --profile web remove dsh-endnote
> dsh plugin --profile web add dsh-endnote
> ```

Uninstall:

```powershell
dsh plugin --profile web remove dsh-endnote
```

### Configuration

Every field is optional. With none set, the plugin **discovers your library**
(see `scripts/endnote_paths.py`):

1. `DSH_ENDNOTE_LIBRARY` environment variable
2. `$DSH_HOME/endnote.json` — `{"library": "...", "stagingDir": "..."}`
3. The existing `endnote-mcp` config (`%APPDATA%\endnote-mcp\config.yaml`), whose
   XML/PDF paths identify the live library — so a machine already using EndNote
   MCP needs **no configuration at all**
4. A bounded scan of `Documents`, `Desktop`, `Downloads`, `OneDrive/Documents`
5. A conventional fallback, reported honestly rather than guessed at

To set values explicitly, edit the plugin's `cordis.patch.yml`:

```yaml
- insert:
    - id: dsh-endnote
      name: dsh-endnote
      config:
        library: 'D:\Research\My Library.enl'   # empty -> auto-detect
        stagingDir: 'D:\Research\pdf-inbox'     # empty -> <DSH_HOME>/endnote-staging
        endnoteExe: 'C:\Program Files (x86)\EndNote 21\EndNote.EXE'
        pythonExe: 'C:\Python312\python.exe'
        toolTimeoutMs: 300000
        registerTools: true
```

| Field | Default | Meaning |
|---|---|---|
| `library` | auto-detect | Path to the EndNote `.enl` |
| `stagingDir` | `<DSH_HOME>/endnote-staging` | Where downloaded PDFs and generated `.enw` files go |
| `endnoteExe` | auto-detect | `EndNote.EXE`, used to import `.enw` files |
| `pythonExe` | the running interpreter | Interpreter for the bundled scripts |
| `toolTimeoutMs` | `300000` | Per-tool-call timeout (PDF downloads are slow) |
| `registerTools` | `true` | `false` registers only the skills |

These are passed to Python as `DSH_ENDNOTE_*` variables, so **no library path is
hardcoded** — discovery is centralised in `scripts/endnote_paths.py`. The one
literal that remains is EndNote's own conventional install location
(`C:\Program Files (x86)\EndNote 21\EndNote.EXE`), used only as a last-resort
fallback and reported as `default (not found)` when it is used.

Environment variables you may want to set yourself:

| Variable | Default | Meaning |
|---|---|---|
| `DSH_ENDNOTE_LIBRARY` | (unset) | Overrides `library`; checked first during discovery |
| `DSH_ENDNOTE_PROXY` | (unset) | HTTP(S) proxy for metadata and PDF requests. Unset means direct. **Setting it relaxes TLS verification for the proxied connection only** — an intercepting proxy presents its own CA, but ordinary direct requests stay fully verified |
| `DSH_ENDNOTE_CONTACT` | `endnote-pdf@example.org` | Contact address sent to Unpaywall, which asks callers to identify themselves. **Set this to your own address if you want to be a good API citizen**; the placeholder is RFC 2606-reserved and some services reject implausible addresses |

There is **no `pip install` step**: the bundled scripts use only the Python
standard library (`sqlite3`, `ssl`, `urllib`, `ctypes`). `endnote-mcp` is optional
and only needed to make records searchable.

### Tools

| Tool | Purpose |
|---|---|
| `endnote_add` | Add a paper from a DOI/PMID/title/URL, attaching its OA PDF. Can also file it into a group and reindex in the same call |
| `endnote_attach` | Attach, list or remove a PDF on an existing reference |
| `endnote_groups` | List groups and members; create a group, add/remove records |
| `endnote_dedupe` | Find duplicates and trash them (reversible) |
| `endnote_status` | Health check: install, library sources, index sync, APIs |
| `paper_pdf` | Download an OA PDF, or its full text |
| `endnote_refresh` | Re-export the library and rebuild the search index |

**Adding a paper files it into a group automatically.** By default `endnote_add`
chooses the best-matching **existing custom** group and files the record there:

```powershell
python scripts/endnote_add.py 10.1038/s41586-020-2649-2 --refresh
```

The choice is made from the paper's own title/abstract/keywords, compared against
the **titles and keywords of each custom group's existing members** — those
members are evidence of what a group is for, whereas a group *name* alone is not
("Chapter drafts" vs a paper on phage encapsulation is genuinely ambiguous). Only
custom groups are considered, because a derived group's membership is computed by
EndNote and cannot be written.

**It declines when the evidence is thin.** If no group matches convincingly, or
two groups score within 10% of each other, the paper is still added but *not*
filed, and the reason is printed. A paper silently filed into the wrong group is
worse than one not filed: you will not notice until you go looking for it.

```powershell
--group "Exact name"   # force a group (a typo refuses rather than creating one)
--group none           # or --no-group: do not file at all
--group-create         # with an explicit name, create the group if missing
```

This follows the same reasoning as the combined call: tool round trips cost far
more than the work inside them — measured at ~26 s of wall clock per call against
~2 s of real work per step — so the follow-up steps run inside `endnote_add`.
That single call imports the paper, waits for EndNote to apply the (asynchronous)
import, files the record, and rebuilds the search index. `--wait SECONDS` bounds
the wait for the import (default 30), after which the tool reports what did not
happen instead of hanging.

Skills: **`endnote-add`** (library workflows and their traps) and **`paper-pdf`**
(publisher-by-publisher PDF retrieval strategy).

### Design notes

Things that were established by experiment, not assumption — each shaped the code:

**EndNote holds an exclusive OS lock on `.enl` while it runs.** Not `mode=ro`,
not even `immutable=1`; polling showed it never releases. So all reads go through
`<lib>.Data/sdb/sdb.eni`, an unlocked working copy with the same tables. That is
what makes index refresh possible without closing EndNote.

**`%>` (the `.enw` "Link to PDF" field) only attaches a file when it holds a
local path.** A URL there creates a dead link and no attachment. So the importer
downloads first and points `%>` at the file — and omits the field entirely when
no PDF was obtained, rather than leaving a broken link.

**A minimized EndNote silently ignores the import.** The `.enl` stayed
byte-identical until the window was foregrounded. The plugin restores and focuses
the window itself.

**Writing `refs` needs two SQL shims.** EndNote defines an `AFTER UPDATE`
trigger calling `EN_MAKE_SORT_KEY()` and writing `refs_ord` (custom collation).
The dedupe tool registers a placeholder collation and **replays** the existing
sort key rather than reimplementing EndNote's algorithm; a lookup miss raises
instead of writing a corrupt key.

**Group membership is little-endian.** `members` is a 4-byte prefix plus
little-endian uint32 record ids. The big-endian reading yields plausible-looking
but impossible ids; little-endian yields real ones, stable across every backup
tested. `endnote_groups.py --probe` prints both so the assumption stays checkable.

**Only custom groups are editable.** `spec` XML carries a `<rules>` element:
`TYPE;3` is a manual group; `TYPE;6` is an online-search group; others are smart
groups. Derived membership is refused rather than overwritten.

**Index refresh must be `--full`.** The incremental `index` command upserts but
never prunes, so deleted records stay searchable forever.

### PDF retrieval: publishers differ

Measured on the author's machine:

| Works | Blocked (403 to non-browser clients) |
|---|---|
| arxiv.org, link.springer.com, nature.com, frontiersin.org, bmcmicrobiol.biomedcentral.com, institutional repositories | mdpi.com, europepmc.org, pubs.acs.org, pubs.rsc.org |

Repository mirrors rescue blocked papers (an ACS article 403s at the publisher
but downloads from an institutional repository). When every PDF route fails,
Europe PMC's `fullTextXML` still works, so the file is replaced by full text.

### Verification

Run from the repository root:

```powershell
python tools/audit_publication.py       # no personal paths in shipped files
python tools/audit_bibliography.py      # no library DOI / group name / surname in examples
python tools/verify_package.py          # asks npm what the tarball would contain
python tools/test_publication.py        # the plugin works when extracted, as a consumer gets it
python tools/test_tls_posture.py        # direct requests stay TLS-verified with a proxy set
python tools/test_discovery.py          # library auto-discovery
node   tools/test_group_selection.mjs   # automatic group filing, all forms
node   tools/validate_patch.mjs         # patch YAML + config plumbing
node   tools/smoke_test.mjs             # plugin logic against a fake host context
node   tools/e2e_test.mjs               # really executes the tools (read-only)
python tools/check_skills.py skills     # bundled skills are well formed
```

Some checks need a readable EndNote library, and say so — they report SKIP with a
reason rather than a failure, because an unreadable library is not a code
regression. `test_group_selection.mjs` exits 2 when it skipped anything, so that
cannot be mistaken for a clean pass; pass `--allow-skip` to accept it.

The full list, with what each one is for, is in
[CONTRIBUTING.md](CONTRIBUTING.md#before-every-pull-request).

### License

MIT — see [LICENSE](LICENSE).

### AI usage disclosure

Substantial portions of this plugin were written by an AI coding agent, working
under human direction and review. Read [AI-USAGE.md](AI-USAGE.md) before relying
on it for anything destructive: it states exactly which parts are machine-written,
what was verified experimentally versus assumed, and where the author's confidence
is lower.

---

## 中文说明

### 这是什么

把本地 EndNote 21 文献库变成 agent 可操作的 DSH 插件:

- **加入文献** —— 给 DOI / PMID / 标题 / URL,自动附上开放获取 PDF
- **补附件** —— 给库里已有的文献贴 PDF
- **管理 group** —— 列出 group 及成员、新建自定义 group、加入 / 移出
- **查重清理** —— 找重复记录并移入回收站(可恢复)
- **刷新索引** —— 让新记录能被检索到
- **单独下载 OA PDF** —— 取不到 PDF 时回退全文

**全程无需关闭 EndNote。**

### 安装

从 npm 仓库安装：

```powershell
dsh plugin --profile web add dsh-endnote
```

或从本地目录安装（开发用）：

```powershell
dsh plugin --profile web add link:<本目录绝对路径>
# 然后重启 DSH（bundle 列表不热更新）
```

> **两种方式互切时先卸载。** `dsh plugin` 是 pnpm 的透传，所以同名 `link:`
> 还在的情况下执行 `add dsh-endnote` 是空操作——pnpm 认为依赖键已满足，直接
> 跳过解析（实测输出 "resolution step is skipped"，链接原样保留）。从本地
> 版切到发布版：
>
> ```powershell
> dsh plugin --profile web remove dsh-endnote
> dsh plugin --profile web add dsh-endnote
> ```

卸载：

```powershell
dsh plugin --profile web remove dsh-endnote
```

### 配置

全部字段可留空。留空时插件会**自动发现**文献库(依次尝试环境变量、
`$DSH_HOME/endnote.json`、已有的 endnote-mcp 配置、常见目录扫描)。
已用过 endnote-mcp 的机器**无需任何配置**。

具体字段见上方英文表格;值通过 `DSH_ENDNOTE_*` 环境变量传给脚本,
**脚本内零硬编码路径**。

### 两点必须知道的限制

- **EndNote 运行期间独占锁定 `.enl`**,所以读取走未加锁的工作副本
  `<lib>.Data\sdb\sdb.eni`。这是"刷新索引不必关闭 EndNote"的原因。
- **只有自定义 group 可编辑**。在线检索组与智能组的成员由 EndNote 计算,
  插件会拒绝写入而不是写一个 EndNote 会忽略的成员表。

### AI 使用声明

本插件相当大一部分代码由 AI 编程 agent 在人类指导下撰写与审阅。
用于破坏性操作前请先读 [AI-USAGE.md](AI-USAGE.md):其中说明了哪些部分是
机器生成、哪些结论经过实测、哪些属于假设,以及作者信心较低的环节。

### 许可

MIT,见 [LICENSE](LICENSE)。
