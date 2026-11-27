# Stage 7: Verified changes (repro → fix → test → review)

## Goal
Make bug-fix tasks come back verified by default: reproduce the problem, fix it, run the
right tests, and have an independent reviewer that sees the test evidence. Add adaptive
plan-mode triggers and an impact list after signature changes.

## Why
- SWT-Bench: keeping only patches that make a self-generated failing test pass **doubles
  SWE-agent's precision**. But fail-to-pass test generation succeeds only 23.6% (TDD-Bench
  Verified) to 32.9% (Issue2Test) of the time, so the gate must be **soft**.
- Self-repair (Olausson): gains are modest once compute is counted; external feedback is the
  real lever. Cap repair at 2–3 rounds; prefer diverse attempts. RepairAgent: failed runs
  burn about 15x the tokens of successful ones, so stop when there is no progress.
- R2E-Gym: test-based and judge-based verifiers each plateau at about 42–43%; a hybrid
  reached 51% for an open 32B agent whose pass@1 was 34.4%. Judge-only review rewards style.
  The reviewer must see test results, and ideally runs on a different model.
- ChatRepair: remembering rejected plausible patches makes alternatives truly different.
- ADaPT: decompose only when the executor fails (+28–33%). CodePlan: dependency and impact
  analysis after edits, 5 of 6 migrations against 0. Codex: up-front plans caused early
  stopping on older models, so trigger plans adaptively.
- AgentCoder: an independent test writer that can't see the author's reasoning.

## Scope
Task classification; the verified flow; rejected-patch memory; the no-progress stop;
reviewer integration with the existing review mode; plan-mode triggers; the impact list (grep
v0, graph-backed after Stage 8).

Out of scope: best-of-N attempts (Stage 9), a debugger (Stage 9).

## Design

### Task classes (`engine/task_class.py`)
A cheap classifier (rules first, one short model call only when unclear): `bugfix`,
`feature`, `refactor`, `question`, `ops` (build, git, logs). The verified flow is the default
for `bugfix`; `feature` and `refactor` run the test + review steps without repro;
`question` and `ops` skip the flow. Override with `--flow verified|plain`, or set it in an
agent file (`flow:`).

### The flow (`engine/flows/verified.py`)
The flow is a state machine on top of the normal loop. Each state is a sub-goal message and
a permission profile; it is not a separate agent, so the one writer keeps full context.

```
REPRODUCE  → write a minimal repro (test or script) under .crit/repro/ or the test tree;
             run it on the unmodified code; it must fail, *for the reason the issue states*
             (the model states the expected failure first; the harness compares error type/message).
             Up to k=2 tries. Fail → mark repro "untrusted" and continue (soft gate).
FIX        → normal editing. Each attempt ends with: run repro + targeted tests.
             ≤ 3 repair rounds per attempt. A rejected patch is stored (diff + why it was
             rejected) and shown in the next attempt as "do not repeat these".
TEST       → test selection: tests named in AGENT.md `Test command:`, tests touching changed
             files (path heuristics now, the Stage 8 import graph later), the repro.
             Regression: tests that passed before must still pass (run before/after on the
             selected set).
REVIEW     → reviewer sub-agent (agent file `verifier`, read-only, fresh context): sees the
             task, the final diff, the repro + test outputs (before/after), not the author's
             reasoning. It returns the review JSON (reuse the code-review mode's contract:
             findings with severity). Profile setting: reviewer model (default = same, or a
             configured stronger/different one).
             Blocking findings → back to FIX (once). Non-blocking → shown to the user.
DONE       → summary: what was wrong, the fix, the evidence (repro failing → passing, tests),
             reviewer findings, and any untrusted-repro caveat.
```

Stop rules (`stop_rules.py`):
- **No progress:** N model turns (default 12) with no change in failing-test count → stop
  FIX and report what was tried.
- **Token budget per state** from the agent/profile; leaving it → move on or stop with an
  explanation.
- Everything recorded as events (`flow_state`, `repro_result`, `test_run`, `review_result`).

### Plan mode triggers (`engine/modes.py`)
- Suggest plan mode (ask the user; in auto mode, enter it) when any of these hold: estimated
  files to change > 5 (from a quick search), the task mentions migration/refactor across
  modules, or the first FIX attempt failed twice.
- The plan is a todo list with file-level steps; plan mode is a read-only profile + the plan
  file + approval exit (exists; just enforce it through permissions as in Stage 5).

### Impact list (`engine/impact.py`)
After an edit changes a function/method/class signature (detected with tree-sitter by
comparing before and after), find callers/implementers (v0: `rg` on the name with
word boundaries and language filters; v1: the Stage 8 graph). Push them onto the todo list:
"check 7 call sites of `foo()` in: …". The DONE summary lists any that remain unchecked.

### Evidence in the UI
The TUI and server events show a small checklist: repro ✓/✗/untrusted, tests before→after,
reviewer verdict. `crit run --json` includes it in the final object.

## Work items
1. Task classifier + `--flow` + agent-file `flow:` key.
2. Flow state machine with per-state sub-goal prompts (in `prompts/flows/verified.txt`).
3. Repro step: expected-failure statement, run-and-compare, soft gate.
4. Test selection + before/after regression runs (reusing `agent_rules` check commands).
5. Rejected-patch memory.
6. Stop rules: no-progress, budgets.
7. `verifier` agent file; reviewer gets evidence; review JSON reused from the review mode;
   the reviewer-model setting.
8. Plan triggers; impact list v0.
9. Eval: suite bugfix subset with `--flow verified` vs `plain`, 3 runs; measure precision
   (correct fixes / fixes claimed), pass@1, tokens. Record in `plan/results/07/`.

## Tests
- Scripted sessions for each state transition, including untrusted repro, blocking review
  → back to FIX once, and the no-progress stop.
- Repro "fails for the wrong reason" fixture (an ImportError when the issue is a wrong
  value) → not accepted as the repro.
- Regression: a fix that breaks a previously passing test → caught at TEST.
- Impact: a signature change in Python/Java/Kotlin fixtures → call sites listed.

## Completion targets
- [ ] Bugfix subset: **precision** (claimed fixes that pass hidden tests) ≥ 1.5× plain mode
- [ ] Bugfix subset: pass@1 ≥ plain mode (verification must not cost solves)
- [ ] Tokens per solved bugfix ≤ 1.6× plain mode
- [ ] 0 tasks where crit says "fixed" while its own repro or selected tests fail (by
  construction the flow reaches DONE only after the repro passes and no selected test
  regressed, and the scripted tests cover both; `verified_eval.py` counts `false_claims`
  on the model runs)
- [ ] Reviewer catches ≥ 50% of the seeded-defect fixtures (diffs with a planted bug)
- [x] Plan triggers fire on ≥ 80% of the multi-module tasks and on ≤ 10% of the single-file ones
  (2026-11-27: 10 of 10 and 0 of 10 on the task lists in `tests/test_verified_flow.py`,
  `PlanTriggerTest`; recheck on the suite's multi-module tasks with the model runs)

## Risks
- **Slow test suites (AOSP):** test selection with a time budget; `atest` targets per module
  in the AOSP pack (Stage 9).
- **The reviewer rubber-stamps:** seeded-defect fixtures measure it; allow a different
  model for the reviewer.

## Status (2026-11-27)

Done (branch `wip/s7-verified`; every item below has scripted-session or unit tests in
`tests/test_verified_flow.py` and `tests/test_verified_eval.py`, no model needed):
- **Task classes** (item 1): `engine/task_class.py` (started per task by `engine/flows/start.py`), keyword rules first, then one short
  question to a fresh session (model API backends) only when the rules are unclear; the
  classifier is pluggable (`_task_classifier` in the loop settings). `--flow
  verified|plain|auto` on `crit` and `crit run`, `flow:` in agent files (validated), the
  `flow` setting; precedence flag > agent > setting > default (`auto` in crit's sessions,
  plain for a bare `run_agent_loop`).
- **The flow** (items 2, 3, 5): `engine/flows/verified.py` on the one writer: sub-goal
  messages from `prompts/flows/verified.txt`, the REPRODUCE permission profile (only the
  test tree and `.crit/repro/` can be written), the expected-failure statement and the
  run-and-compare verdict (`engine/flows/checks.py`; an ImportError for an expected
  ValueError is "the wrong reason"), k=2 then untrusted, ≤3 repair rounds per attempt, 2
  attempts, rejected patches (diff + why) shown to the next attempt. Hooks in `task.py`
  (turn counting, limits, REPRODUCE replies), `dispatch.py` (profile, impact, progress) and
  `stop_rules._finish` (a COMPLETED claim runs the checks).
- **Test selection + regression** (item 4): the check command, tests named after the
  changed files, the index's `imported_by` through `code_nav`; before (pre-task copies
  swapped in) and after; Python and Go run per file/package, other languages through the
  check command (their test files are a caveat).
- **Stop rules** (item 6): `ProgressWatch` (12 turns without a change in the failing-test
  count) and `StateBudgets` (estimated tokens per state) in `stop_rules.py`.
- **Reviewer** (item 7): `agents/verifier.md` (read-only), `engine/flows/review.py`: a fresh chat from the run's
  `reviewer_factory` (`verifier_model` / the agent's `model:`), task + diff + evidence only,
  the review JSON contract (`risk`, `comments[].severity`), blocking → FIX once.
- **Plan triggers, impact list** (item 8): `modes.plan_trigger` / `estimate_files` /
  `Modes._suggest_plan` (auto mode enters plan mode and approves the plan itself; other
  modes ask); `engine/impact.py` (signature lines before/after from `code_index` symbols,
  call sites from `code_nav` or a word search, todos, unchecked ones in the evidence).
- **Evidence**: checklist lines in the TUI and as `notice` events; `flow_state`,
  `repro_result`, `test_run`, `review_result` events; `evidence` on `task_finished`; the
  final object of `crit run --json`; `flow` in the run stats for `crit eval`.
- **Eval support** (item 9, without the runs): `scripts/eval/verified_eval.py` (`run`,
  `reviewer`, `report`), 8 seeded-defect fixtures in `tests/verifier_fixtures/`.

Left:
- The model runs: `verified_eval.py run --runs 3` and `reviewer` on the eval machines, into
  `plan/results/07/`; then the first five completion targets.
- Test selection for JVM / JS / C++ test files (Gradle, npm, ctest targets per file) and
  AOSP `atest` targets (Stage 9); a time budget for slow suites.
- `--flow` over `crit run --url` / `POST /v1/sessions` (set `flow` in the server project's
  settings meanwhile).
- The impact list's "checked" rule is a heuristic (the todo completed, or every file
  read or edited after); tree-sitter signatures span one line (the definition line).
