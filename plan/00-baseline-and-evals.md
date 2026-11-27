# Stage 0: Baseline and evals

## Goal
Make `crit eval` the instrument that decides every later stage. Record a baseline for
today's crit and for opencode on the same model and server, so every later change shows up
as a measured difference.

## Why
- The harness moves results as much as the model does: 8–15 points on Terminal-Bench with
  the model held fixed (Factory 58.8% vs Claude Code 43.2%), and 3–8 points from SWE-agent
  ACI ablations. That difference is what we compete on.
- Anthropic's eval guidance: 20–50 tasks from real failures, code-based grading first, both
  pass@k and pass^k, clean environment per trial, and audit low scores (a CORE-Bench grader
  fix took one score from 42% to 95%).
- SWE-bench Verified is contaminated (OpenAI stopped reporting it in Feb 2026). Use our own
  tasks and SWE-bench Pro / Terminal-Bench 2.0 subsets, not Verified headlines.

## Scope
- Promote `scripts/eval/run_eval.py` to a `crit eval` command (apps layer), keeping the
  task.json format.
- Metrics, isolation, multiple runs, comparison reports.
- Runner adapters: crit (this checkout or installed), opencode (`opencode run`), and
  optionally Terminus 2 for Terminal-Bench tasks.
- Grow the suite from 5 to at least 30 tasks.

Out of scope: hosted leaderboards, LLM-judge grading (added in Stage 7, for review quality only).

## Design

### Metrics recorded per trial (results.jsonl)
| Field | Meaning |
|---|---|
| `pass` | `check` exits 0 and/or `answer` regex matches |
| `wall_s`, `turns`, `tool_calls{name:n}` | as today |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | from server usage (llama.cpp `timings`, vLLM `usage.prompt_tokens_details`) |
| `tool_errors` | tool results with `ok: false` |
| `malformed_calls` | replies with tool markup that did not parse (or failed schema) |
| `edit_failures` | edit/apply_patch calls rejected (no match, ambiguous, parse guard) |
| `edits{format: {calls, ok, ladder_step, out_tokens, seconds}}` | per edit format: success rate, which match step succeeded, output tokens and seconds per edit |
| `stale_edit_refusals` | edits refused because the file changed since it was read |
| `repeat_breaks` | identical-call breaker fired |
| `compactions` | count and tokens before/after |
| `exit_reason` | finished / gave up / budget / timeout / crash |

Summaries per task and per suite: pass@1 (mean over runs), pass@3 (any of 3), **pass^3**
(all of 3), median tokens, median wall time, error rates. Comparing two result sets gives a
per-task diff table and an overall difference with a bootstrap 90% interval. With at most
50 tasks, report the interval so noise doesn't read as progress.

### Isolation
Each trial gets a fresh git worktree or temp copy of the fixture, a fresh `CRIT_HOME`, and
no leftover `.crit/`. Read-only `$AOSP` workspaces are mounted read-only. In Stage 4, use
the sandbox to enforce this. The audit item: confirm no git history from earlier trials is
visible (Anthropic found leakage between trials in one internal eval).

### Suite layout
`tests/eval_tasks/<task>/task.json` gains `category` (bugfix, feature, refactor, logs,
build, aosp, git, question), `difficulty`, and `source` (real failure, synthetic, SWE-bench
Pro id, Terminal-Bench id).

Target mix (≥ 30):
- 8 bug fixes in Python/Java/Kotlin/C++ with hidden tests
- 4 features with tests
- 3 multi-file refactors
- 4 log/crash triage (logcat, tombstone, ANR, build log)
- 3 build-break fixes (Soong, Gradle, CMake)
- 3 git tasks (rebase conflict, bisect, cherry-pick)
- 3 AOSP navigation questions with regex answers
- 2 "should refuse or ask" tasks, for the safety behavior

Plus optional external slices: 20 SWE-bench Pro public tasks, 20 Terminal-Bench 2.0 tasks.

### opencode adapter
Run `opencode run "<prompt>"` in the same worktree with a config that points at the same
OpenAI-compatible server and model. Record its exported session (`opencode export`) for
token counts. Pin the opencode version in `results/meta.json`.

## Work items
1. `src/critique_bot/eval.py` (apps): move the logic from `scripts/eval/run_eval.py`; keep
   the script as a thin wrapper. Add `crit eval [--suite DIR] [--only] [--tag] [--runs N]
   [--runner crit|opencode|terminus] [--out DIR]` in `cli.py`.
2. Instrument the engine to emit counters (malformed calls, edit failures, breaker,
   compactions, usage) to the transcript as structured lines. This is temporary until
   Stage 3's event log replaces it.
3. Usage capture in `local_model.py`: parse `usage` and `timings` from the final stream
   chunk and put them in `last_detail`.
4. `crit eval compare A B` prints the markdown diff table and the bootstrap interval.
5. Runner adapters: `eval_runners/crit.py`, `eval_runners/opencode.py` (subprocess, config
   templating), `eval_runners/terminus.py` (optional).
6. Write 25+ new tasks. Prefer real failures from your own work (AOSP build breaks, logcat
   crashes you've hit). Each needs a reference solution that passes `check`, kept outside
   the fixture.
7. Grader audit: run each task's reference solution through `check` (must pass), and run an
   untouched fixture through `check` (must fail). Fail CI on either.
8. Write `plan/results/00/` with: `meta.json` (crit commit, opencode version, model,
   server, flags, GPU), the results.jsonl files, and `baseline.md`.

## Tests
- Unit: metric aggregation (pass@k, pass^k, bootstrap) on fixed inputs.
- The grader audit (item 7) runs in CI with no model needed.
- Smoke: `crit eval --only py-fix-test --runs 1` against a scripted fake model session.

## Completion targets
Progress: [`results/00/README.md`](results/00/README.md).

- [x] `crit eval` runs the suite with `--runs 3` and writes results.jsonl + summary.md
- [x] ≥ 30 tasks; every task passes the grader audit (31 tasks; 30 audited here, 1 needs `$AOSP`)
- [ ] Baseline recorded: crit (current text protocol) and opencode, same Qwen3.6-27B
      quant, same llama.cpp build, same context size. Repeat on vLLM if available
- [ ] Each metric in the table above is populated for crit; tokens/turns for opencode
- [ ] `baseline.md` lists the 5 most common failure causes for crit, read off the transcripts
- [ ] Targets in Stages 1–4 updated from the baseline (dated note in `decisions.md`)

## Risks
- **Variance on a single GPU:** 3 runs × 30 tasks with a 27B model may take a long time. Keep
  a `--quick` tag of 10 tasks for development and run the full suite at stage exits.
- **opencode configuration fairness:** document every opencode setting and let opencode use
  its recommended tools and prompts for that model.
