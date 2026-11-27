# Measuring crit: `crit eval`

`crit eval` runs a coding agent on a suite of real tasks and records how it did. It is
the instrument behind every change to crit's harness: hold the model and server fixed,
change one thing, and compare.

```bash
crit eval list                                   # the tasks, and which can run on this machine
crit eval audit                                  # every grader fails untouched and passes with its reference
crit eval --config ~/.crit/qwen.json --runs 3    # run the suite (crit eval run ... is the same)
crit eval --tag quick                            # the 10-task subset for quick checks while developing
crit eval compare .crit/eval/A .crit/eval/B      # what changed between two runs
crit eval --tag aosp --packs aosp                # the AOSP tasks with the AOSP pack on (docs/packs/aosp.md)
```

From a checkout, `python3 scripts/eval/run_eval.py` is the same command.

## What it records

Each trial gets a fresh temporary git workspace and, for crit, a fresh `CRIT_HOME`, so no
earlier trial can leak into it. A task's hidden check files are copied in only after the
agent finishes, so the agent never sees them.

`<out>/results.jsonl` has one line per trial:

| Field | Meaning |
|---|---|
| `result` | `pass`, `fail`, `skipped` (a required program or `$VAR` is missing) or `error` (setup failed) |
| `seconds`, `exit`, `why` | wall time, the agent's exit code, why it failed |
| `model_requests`, `prompt_tokens`, `completion_tokens`, `cached_tokens` | from the model server's usage reports (`cached_tokens`: vLLM `prompt_tokens_details`, llama.cpp `timings.cache_n`) |
| `tool_calls` | calls per tool |
| `tool_errors`, `malformed_calls`, `edit_failures` | tool results with `ok: false`; tool markup that did not parse; failed edits |
| `edits` | per edit tool: calls, ok, seconds, estimated output tokens |
| `repeat_breaks`, `compactions`, `stale_edit_refusals` | loop refusals; moves to a new conversation; edits refused because the file changed since it was read |
| `exit_reason` | the task's last status (COMPLETED, FAILED, ...) |

`summary.md` reports the suite: pass@1, pass@k and **pass^k** (every one of k trials
passes, which measures consistency), median seconds, turns and tokens, completion tokens
per solved task, cache hit ratio, malformed calls and tool errors per turn, edit success
rate, and exit reasons. `meta.json` holds the crit version and commit, the runner, the
config file, the GPU and `--note` (use it for the server, quant and flags).

`crit eval compare A B` prints a per-task table and the pass@1 difference over the tasks
both runs share, with a paired bootstrap 90% interval. With a suite of 30–50 tasks, a
difference whose interval includes 0 is noise.

## Comparing with opencode

```bash
crit eval --runner opencode --model local/qwen3.6-27b --runs 3 --note "llama.cpp b6xxx, Q4_K_M, 32K"
```

Point opencode's own config (`opencode.json`) at the same OpenAI-compatible server and
model crit uses. `--opencode` changes the command template (`{prompt}`, `{model_flag}`);
the default is `opencode run --format json {model_flag} {prompt}`. Token counts come from
opencode's JSON events when it prints them. Record the opencode version (it goes into
`meta.json`) and use its recommended settings for the model, so the comparison is fair.

## Writing a task

```
tests/eval_tasks/<name>/
  task.json
  files/      the repository the agent sees
  hidden/     check files, copied in only for grading
  solution/   the reference fix, overlaid on the workspace (or solution.sh / solution.patch)
```

```json
{
  "prompt": "The unit tests in this project fail. Find the bug and fix it.",
  "fixture": "files",
  "setup": "python3 gen_log.py",
  "hidden": "hidden",
  "reference": "solution",
  "check": "python3 -m unittest -q hidden_tests",
  "requires": ["python3"],
  "timeout": 900,
  "tags": ["python", "quick"],
  "category": "bugfix",
  "difficulty": "medium",
  "source": "synthetic"
}
```

Answer tasks use `"answer"` (a regex the final reply must match; lookaheads make "all of
these" checks) and `"reference_answer"`. `"workspace": "$AOSP"` runs on an existing tree
instead of a fixture.

`crit eval audit` must pass for every task: the check fails on the untouched workspace
and passes with the reference, and `reference_answer` matches `answer`. CI runs the audit
on Linux. Prefer real failures you have hit over synthetic ones, keep checks offline and
under a minute, and don't let a check pass when tests are deleted.
