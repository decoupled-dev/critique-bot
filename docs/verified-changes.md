# Verified changes

A bug fix that crit reports as done should come with evidence: the problem was reproduced,
the fix makes the repro pass, the tests that touch the change still pass, and a reviewer
that did not write the change read it. That is the **verified flow** (plan Stage 7). It is
on by default for bug fixes; features and refactors get the test and review steps; questions
and build, git or log work run as before.

## What you see

While it runs, the terminal (and an API client, as `notice` events) shows the steps:

```
Verified flow (bugfix): reproduce, fix, test, review
Running the repro (unmodified code): python -m pytest -q .crit/repro/test_negative_price.py
Repro accepted: python -m pytest -q .crit/repro/test_negative_price.py
...
Running tests/test_shop.py (before): python -m unittest discover -s tests -p test_shop.py
Running tests/test_shop.py (after): ...
Verified flow: review
```

and at the end a checklist:

```
repro: ✓ failed before, passes after (python -m pytest -q .crit/repro/test_negative_price.py)
tests: ✓ failing 0 → 0 (2 target(s))
review: ✓ safe, 0 blocking, 1 other finding(s)
impact: 1 unchecked: check 3 call site(s) of `price()` in: cart.py:12, api.py:40, ...
caveat: ...
```

`crit run --json` ends with a final object that carries the same evidence:

```json
{"type": "final", "code": "COMPLETED", "changed": ["shop.py"],
 "evidence": {"repro": "passes", "failing_before": 0, "failing_after": 0,
              "review": {"risk": "safe", "blocking": 0, "findings": 1}, "checklist": ["..."], "...": "..."}}
```

The `task_finished` event has it as `evidence` too.

## The steps

**Classify.** Keyword rules sort the task into `bugfix`, `feature`, `refactor`, `question` or
`ops`. When the rules cannot tell and the model is behind an API, one short question to a
fresh session decides; otherwise an unclear task runs plain.

**REPRODUCE** (bug fixes only). The model states the failure it expects
(`EXPECTED: ValueError: price must be positive`) and writes a minimal repro, either a test in
the project's test tree or a script under `.crit/repro/`; in this step it cannot change any
other file. It replies with `REPRO: <command>`. crit runs the command on the unmodified
code and accepts it only when it **fails for the stated reason**: a non-zero exit, the
expected error type in the output, and most of the expected message's words. An
`ImportError` when a `ValueError` was expected is "the wrong reason" (the repro is broken,
not the code). Two tries; after that the repro is marked **untrusted** and the flow goes on
without it. Generated fail-to-pass tests succeed only a quarter to a third of the time in
the studies, so this gate is soft on purpose.

**FIX.** Normal editing. When the model says COMPLETED, crit runs the repro; it must pass
now. If it still fails, the output goes back as a repair round, at most 3 per attempt. After
that the attempt is rejected: its diff and the reason are kept, and the next attempt is
told "do not repeat these" (two attempts in all). If the first attempt fails its checks
twice, crit suggests plan mode (in auto mode it switches, and the plan goes ahead without
a review).

**TEST.** crit selects the tests that touch the change:

- the project's test command (`check_command`, or the `Test command:` line in `.crit/AGENT.md`);
- test files named after the changed files (`test_cart.py`, `cart_test.go`, `CartTest.kt`, `cart.spec.ts`, ...);
- with a project index (Stage 8), the test files that import the changed files.

It runs them twice: **before** (with the pre-task copies of the changed files swapped in for
a moment) and **after**. A test that passed before and fails after is a regression and goes
back to FIX as a repair round. crit runs Python test files (pytest when the project uses it,
else unittest) and Go packages itself; for other languages the project's test command is
the check, and the test files it found are listed as a caveat.

**REVIEW.** The `verifier` agent (`critique_bot/agents/verifier.md`, read-only) runs in a
fresh chat. It sees the task, the final diff and the evidence (repro and tests, before and
after), not the author's reasoning, and answers in the review mode's JSON contract
(`risk`, `comments` with `severity`). `blocker`, `must-fix` and `security` findings send the
change back to FIX once; the rest are shown. Without a second chat (a backend that cannot
open one) the review is skipped with a caveat.

**DONE.** The checklist above; then the usual finish (the project check, the diff).

## Stop rules

- **No progress:** in FIX, 12 model turns (`flow_no_progress_turns`) without a change in the
  failing-test count stop the task (`STOPPED`) with what was tried. The count comes from any
  test run, the model's or crit's; until a first count is seen, the token budget is the limit.
- **Token budgets per state** (estimated as characters / 4): REPRODUCE 40000, FIX 400000,
  REVIEW 80000. Past REPRODUCE's budget the repro becomes untrusted; past FIX's the task
  stops; past REVIEW's the review is skipped.

## The impact list

After an edit changes the signature of a function, method or class (the definition line
before and after, from the symbol extractor: tree-sitter when installed, else the regex
fallback), crit finds its call sites (the index's `who_calls` / `refs`, else a
word-boundary search in files of the same language family) and adds a todo:
"check 3 call site(s) of `price()` in: cart.py:12, api.py:40, ...". An item is checked when
its todo is completed or every file it names was read or edited afterwards; the DONE
checklist lists the rest.

## Plan triggers

When the flow is on, a task starts in plan mode (auto mode) or crit asks first (other
modes) when it looks big: more than 5 files to change (the paths it names plus the files
that use the identifiers it names, from the index or `rg`), or a migration or refactor
across modules. The third trigger is the failed first attempt above.

## Settings

| Key | What it does |
| --- | --- |
| `flow` | `auto` (crit's default): verify bug fixes, test and review features and refactors. `verified`: the whole flow for every change task. `plain`: never. `--flow` sets it for one run; an agent file's `flow:` wins over the setting. |
| `flow_no_progress_turns` | The no-progress stop. Default 12. |
| `flow_budgets` | `{"reproduce": 40000, "fix": 400000, "review": 80000}` (estimated tokens). An agent file's `budget:` takes the same keys. |
| `plan_triggers` | `false` turns the plan triggers off; `true` turns them on for plain tasks too. |
| `impact_list` | `true` keeps the impact list on for plain tasks too. |
| `verifier_model` | The reviewer's model (default: the session's). The `verifier` agent file's `model:` does the same. |
| `check_command` | The project's test command (else `Test command:` in `.crit/AGENT.md`). |

Over `crit serve`, the flow runs the same way: its steps are `notice` events, its records are
`flow_state`, `repro_result`, `test_run` and `review_result` events
([sessions.md](sessions.md)), and approvals of the commands it runs are `request` events.
The web chat speaks the same text protocol, so the flow works there too; its reviewer opens
a second tab.

## Measuring it

`scripts/eval/verified_eval.py run` runs the suite's bugfix tasks with `"flow": "verified"`
and `"plain"` and reports precision (claimed fixes that pass the hidden tests), pass@1,
tokens per solved task and false claims; `reviewer` runs the verifier prompt on the
seeded-defect fixtures in `tests/verifier_fixtures/`. Results go to `plan/results/07/`.
