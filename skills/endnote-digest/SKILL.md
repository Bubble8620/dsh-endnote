---
name: endnote-digest
description: "解读/翻译/详读 EndNote 库里的一篇文献 —— 生成中文解读稿（题目、刊名、IF、DOI、摘要译文、全文翻译与解读），存入工作区 文献解读/ 文件夹，并把成品挂回 EndNote 条目。Generate a Chinese reading note for an EndNote reference, saved to 文献解读/ and attached to the record. Use when asked to 解读、翻译、详读、精读、文献笔记, or to write a literature note for a record named by number, DOI, or title."
whenToUse: "用户指向 EndNote 文献库中的某篇论文并要求解读/翻译/详读时；尤其是用记录号、DOI 或标题指代，而不是给 PDF 路径时。"
---

# Reading a paper out of the EndNote library

`endnote_digest.py` does the **mechanical** half and hands you the facts plus a
template. **You write the translation and the interpretation** — that is the
whole division of labour, and the script refuses to pretend otherwise.

## The flow

```powershell
# 1. find the record
python scripts/endnote_digest.py --list

# 2. scaffold: metadata + IF + full text + the note file
python scripts/endnote_digest.py 21

# 3. READ the note, fill in every TODO(agent) section with the real work

# 4. attach the finished note to the EndNote record
python scripts/endnote_digest.py 21 --attach-only "<path to the note the script printed>"
```

Step 2 already attaches the template, so the record never ends up with a note
that does not exist on disk. Re-attach after editing if you want the copy inside
the library refreshed.

## What the script produces

The header matches the format that was asked for:

```
**题目**（刊名, 年份, IF≈4.3, 10.3390/v15081737）
```

then a metadata table, then these sections with `TODO(agent)` markers:

| Section | What you put there |
|---|---|
| 摘要译文 | Translate the abstract. Academic register, not literal word order. |
| 全文翻译与解读 | Translate section by section (引言/方法/结果/讨论/结论), each followed by interpretation. |
| 批判性评价 | Sample size, controls, statistics, reproducibility, the authors' own stated limits. |
| 对我的课题的意义 | What it can and **cannot** support. Both halves matter. |
| 附：原文全文 | The extracted text, fenced. Reference material — do not quote it as your own prose. |

## Things that will bite you

**The IF is a proxy, not the real Impact Factor.** The script reports OpenAlex
`2yr_mean_citedness`, because the JCR Impact Factor is Clarivate's commercial
data and no free API carries it. The two numbers differ (measured: Frontiers in
Microbiology gives 5.50 here vs 5.8 in the user's own citation of it). One
decimal is deliberate — more would imply precision the proxy does not have. The
note says so in a blockquote; keep that when you write the file.

**Authors are separated by a bare CR, not a semicolon.** EndNote stores
`"Li, Guannan\rWu, Meihong\r..."`. This leaked a `\r` into a generated filename
once and surfaced as `OSError: [Errno 22] Invalid argument` rather than anything
resembling a filename problem. The script strips all control characters now —
if you construct a filename yourself, do the same.

**Name order is inconsistent across records.** Most are `Surname, Given`, but
imported ones can be `Given Surname` (both measured in this library). The script
prefers the comma form and otherwise takes the last word.

**It will not overwrite an existing note.** A note is hand-written work and there
is no undo, so a second run on the same paper exits 1 and makes you choose
`--force` or `--out <dir>`. Do not reach for `--force` reflexively.

**A duplicate filename is refused at attach time** for the same reason: the
record may already carry a note, and silently adding a second near-identical one
makes it unclear which is current.

**Full text can be missing, and the note says so rather than inventing content.**
Causes: paywalled with no OA copy, no PDF attached to the record, or a scanned
PDF with no text layer (needs OCR). Report which of these it is; do not
paraphrase from the abstract as though you had read the paper.

**Do not run `--attach-only` with the positional `ref` also present.** Passing
both a record number and a file is fine, but note that any command carrying a
DOI/identifier positional will **import** before doing anything else. To only
inspect, pass the number alone. (This cost a duplicate record in this library
once: `endnote_add.py <doi> --verify <doi>` imports first, because the
positional identifier is still present.)

## Attachment mechanics, since they surprise people

Notes are attached by writing a `file_res` row and copying the file into
`<lib>.Data/PDF/<folder>/`, exactly as `endnote_attach.py` does for PDFs — that
tool is PDF-only by design, so non-PDFs go through the same writer minus the
`%PDF` magic check.

Verified: **EndNote accepts and keeps a `.md` attachment.** A Markdown note on
record #5 survived in a live EndNote 21 session alongside the paper's PDF.
EndNote reconciles attachments when it next rewrites the library, so if the
paperclip does not appear immediately, click another reference and back.

`(refs_id, file_pos)` is UNIQUE. The position is therefore chosen **inside** the
write transaction — a position computed before `BEGIN IMMEDIATE` can be stale
and will either raise `IntegrityError` or land the note where another
attachment expects to be.
