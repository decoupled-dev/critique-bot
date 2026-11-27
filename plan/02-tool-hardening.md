# Stage 2: Tool layer hardening

## Goal
Make the built-in tools work well with a 27B model: an edit tool that applies when it
should and refuses clearly when it shouldn't, a parse/lint guard after every edit, tool
output sized to the context budget, and shell defaults that keep the model from waiting.

## Why
- SWE-agent ablations on Lite: no edit command 10.3% vs 18.0%; edits without linting 15.0%;
  a full-file viewer 12.7% vs a 100-line window; iterative paged search 12.0%, worse than no
  search at 15.7%.
- Aider: removing flexible patching raised edit errors 9x. Qwen3-32B produces well-formed
  diffs 83.6% of the time, against 99.6% for whole-file output. Hence a whole-file fallback.
- Anthropic: a str_replace editor that accepts exactly one match and returns errors that
  explain how to fix the call. A concise/detailed output switch gives about 3x fewer tokens.
- Codex: shell takes a `workdir` argument; output truncated to about 10K tokens, keeping the
  head and tail. Factory: short default timeouts and a background-process registry helped.
- opencode: LSP is off by default (it drifts, uses memory, and slows work). Prefer fast
  linters on changed files.

## Scope
`edit_file`, `write_files`, `apply_patch`, `read_files`, `search_code`, `run_command`,
`command_*`, and tool output shaping in general. Plus the tool descriptions themselves.

Out of scope: new tools (Stage 6/8), LSP servers (an opt-in in Stage 8).

## Design

### Edit match ladder (`agent_edit.py`)
`edit_file(path, old, new, replace_all=false)`:
1. Exact match: exactly one hit → apply. More than one hit and `replace_all` is false →
   refuse, listing the line numbers of each hit and asking for more context.
2. Normalized line endings and trailing whitespace.
3. Indentation-agnostic: compare with each line's leading whitespace stripped, then re-indent
   `new` by the indentation difference at the hit.
4. Fuzzy: best window by line-level similarity ≥ 0.92 (difflib), and only when it is unique by
   a margin of ≥ 0.05 over the runner-up. Apply, and say so in the result
   (`"matched approximately at lines 120-134; check the diff"`).
5. Otherwise refuse with the **closest candidate** (≤ 15 lines, with line numbers) and the
   specific difference (`"line 3 differs: expected '    return x', file has '\treturn x'"`).

Every result includes a unified diff of what changed (capped), and the new line range.

### Write safety and batching
- **Stale-read protection** (as in Claude Code): crit records the hash of each file when the
  model reads it. An edit to a file that changed on disk since then (by the user, a
  formatter, a build) is refused with "file changed since you read it; re-read lines X–Y". An
  edit to a file never read in this task is refused unless it is a new file. Whole-file
  writes over an unread existing file already need `overwrite: true`; keep that.
- **Batched edits:** `edit_file(path, edits=[...])` (exists) applies all edits to one file
  **atomically**: either all match or none is applied, and the error names the edit that
  failed. Edits to different files go as parallel tool calls in one turn (Stage 1).
- **Atomic writes:** write to a temp file in the same folder, `fsync`, then `rename` over the
  original. Keep the file's encoding (UTF-8 with or without BOM, Latin-1), line endings
  (LF/CRLF, detected per file), trailing newline state and permission bits. An undo
  checkpoint is taken before the first write in each turn (exists).

### Edit formats per model (profile `edit_format`)
| Format | Tool | Used for |
|---|---|---|
| `str_replace` (default) | `edit_file` with the match ladder, also accepted under the `str_replace_editor` / `edit` names | every family unless the profile says otherwise |
| `whole_file` | `write_files` | fallback after repeated edit failures; new files; small files |
| `patch` | `apply_patch` | only for models trained on a patch format (gpt-oss / V4A-style). Hidden from all other models |

`apply_patch` stops shelling out to `git apply`. It gets its own parser for unified diffs and
V4A-style patches that **ignores line numbers**, finds each hunk by its context lines through
the same match ladder as `edit_file`, and applies all hunks atomically. It is never offered as
the default for Qwen or the other families above.

### Fallback to whole-file writes
The engine counts failed edits per file. After the profile's `edit_fail_limit` (default 2)
failures on one file, the next tool result tells the model: "use write_files with the full
new content for this file". For files > `whole_file_max_lines` (default 800), it suggests
smaller edits instead.

### Parse and lint guard (`agent_rules.py` + new `edit_guard.py`, tools layer)
After every write or edit:
1. **Parse check** with tree-sitter (if the `index` extra is installed) or the language's
   compiler front end (`python -c compile`, `javac -proc:none` only when fast). New
   syntax errors in the changed region → the edit is **kept but flagged**, by default. Profile
   or setting `edit_guard: "reject"` rolls it back instead (SWE-agent-style).
2. **Fast lint** on the changed files only, time-bounded (default 5 s). Use a per-language
   table (ruff / pyflakes, ktlint, eslint, `go vet`, shellcheck, clang-tidy when
   compile_commands exists), set in `.crit/settings.json` → `lint`. Report **only issues new
   compared with the pre-edit file** (diff the two lint runs).
3. The result is appended to the tool result: `"lint: 1 new error: foo.py:42 F821 undefined name 'bar'"`.

### Output shaping (`tool_api.py` + `agent_tools.py`)
- Every tool result passes through `shape_output(result, budget)`. The budget is
  `profile.tool_output_cap_tokens` (falls back to 10% of the effective context, capped at
  10K). When over budget, keep the head and tail, and write the full output to
  `.crit/cache/out/<id>.txt` with its path in the result (recoverable compression,
  Manus-style).
- `read_files`: default window of 100 lines when the file is > 300 lines and no range was
  given; result header shows `lines 1-100 of 2,431`.
- `search_code`: summarized results (files and hit counts first, then the top N snippets),
  capped. Never paged. This already exists in part (rg streaming); align it with the cap.
  Accept `patterns: [...]` (several patterns in one call, grouped results), so the model's
  first search covers its variants in one turn. Stage 8 adds kinds and the index-backed path.
- Empty output is never empty: `"(no output; exit 0)"`, `"no matches for 'X' in 12,345 files"`.
- `response_format: "concise" | "detailed"` on read/search/log tools, concise by default.

### Shell defaults (`agent_shell.py`, `agent_tools.py`)
- `run_command(command, workdir?, timeout_s?, background?)`: the default timeout is from the
  profile (60 s). A longer one must be asked for explicitly (up to 30 min). On timeout the
  process keeps running in the background, and the result gives its id (convert, don't kill).
- A background-process registry is already present (`command_output`/`kill_command`).
  Ensure all of these processes are listed in the environment note and cleaned up at task end.
- An environment snapshot at session start: OS, shell, cwd, git branch/status summary, the
  languages detected, the test command (from AGENT.md `Test command:`), tools on PATH. It is
  given as the *first user turn* (Stage 3), not in the system prompt.

### Tool descriptions
Rewrite every `ToolSpec.description` against one checklist: what it's for, when *not* to
use it, argument examples, output shape, limits. Record before/after eval results. Keep the
names the models were trained on (`edit_file` can stay, but accept the `str_replace_editor`
aliases already present).

## Work items
1. `agent_edit.py`: ladder steps 2–5, diff in results, failure counters exposed to the engine.
2. Engine: per-file failure counting and the whole-file hint.
2a. Stale-read protection (read-hash tracking per task, refusal message with the range to re-read).
2b. Atomic batched edits; atomic writes keeping encoding, line endings, trailing newline and mode.
2c. `apply_patch` rewrite: own unified-diff/V4A parser, context-based hunk location through the
    match ladder, atomic apply; no `git apply`. Offered only when the profile's
    `edit_format` is `patch`.
2d. Edit metrics emitted per call: format, success/failure step, output tokens, seconds
    (consumed by `crit eval`, Stage 0).
3. `edit_guard.py`: parse check + lint diffing; `lint` setting; language table.
4. `shape_output` in `tool_api.py`; apply it to every built-in tool; `.crit/cache/out/` with
   cleanup (keep the last 200 files).
5. `read_files` windowing, `search_code` caps and summaries, empty-output messages,
   `response_format`.
6. Shell: profile timeout, convert-to-background on timeout, environment snapshot builder.
7. Description rewrite pass; `tests/test_tool_api.py` checks every spec has an example and
   a stated limit.
8. Eval run; record in `plan/results/02/`.

## Tests
- Edit ladder: fixtures for each step, including ambiguous matches, tabs vs spaces, CRLF, a
  near-miss that must refuse, and a near-miss that must apply.
- Guard: a Python edit that introduces a syntax error → flagged/rejected per setting; a lint
  error that already existed → not reported.
- shape_output: head/tail split, file written, path in result, budget respected for each
  profile.
- Timeout converts to background and the process is still listed.
- Stale read: a file modified on disk after the read → the edit is refused; after a re-read it applies.
- Batched edits: one failing edit in a batch of 5 → the file is unchanged.
- Atomic writes: CRLF, BOM, no trailing newline, an executable bit and a Latin-1 file all
  round-trip unchanged except for the edited text; a crash between write and rename leaves
  the original intact.
- `apply_patch`: patches with wrong line numbers, shifted context and CRLF apply correctly;
  ambiguous hunks are refused.

## Completion targets
- [ ] Edit failure rate on the suite ≤ 50% of the Stage 1 rate
- [ ] No turn where the model received > profile cap tokens from one tool result
- [ ] Every tool result is non-empty and says what happened
- [ ] Guard catches 100% of the syntax-error fixtures; < 5% false "new error" reports on the
      lint fixtures
- [ ] pass@1 ≥ Stage 1 + 2 points, or tokens per solved task −15% at equal pass@1
- [ ] Tool descriptions pass the checklist test
- [ ] Edit success rate ≥ 95% of edit calls for Qwen3.6-27B on the suite (counting
      ladder-recovered edits as successes, and refusals of truly ambiguous edits as correct)
- [ ] Median output tokens per successful edit ≤ 400; whole-file fallbacks ≤ 10% of edits
- [ ] 0 edits applied to stale content (stale-read tests and eval transcripts)
- [ ] `apply_patch` no longer calls `git`; per-profile format selection tested

## Risks
- **Fuzzy edits applied in the wrong place:** require uniqueness by a margin, show the
  diff, and keep `/undo` checkpoints (already there).
- **Slow linters on big repos:** hard 5 s budget, changed files only; skip with a note when
  the budget is exceeded.
