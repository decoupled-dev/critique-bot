# Stage 0 results

Status on 2026-11-27: the instrument is built; the model baseline is **not run yet**.

## Done
- `crit eval` (`src/critique_bot/eval_harness.py`): `run`, `list`, `audit`, `compare`;
  crit and opencode runners; per-trial isolation (fresh git workspace, fresh `CRIT_HOME`,
  hidden check files copied in only for grading); pass@1, pass@k, pass^k, paired
  bootstrap 90% interval; `results.jsonl`, `summary.md`, `meta.json`.
- Counters from the engine (`src/critique_bot/run_stats.py`, saved as `stats` in
  `agent.json`): model requests, prompt/completion/cached tokens (vLLM
  `prompt_tokens_details`, llama.cpp `timings`), tool calls, tool errors, malformed calls,
  edit failures and per-edit-tool calls/ok/seconds/output tokens, repeat breaks,
  compactions, exit reason. The local backend now asks for usage in streams
  (`stream_options.include_usage`).
- Suite: 31 tasks (30 runnable here; `aosp-module-owner` needs `$AOSP`), every runnable one
  passing `crit eval audit` (check fails untouched, passes with the reference). 10 tagged
  `quick`. CI runs the audit on Linux.

| Category | Tasks |
|---|---|
| bugfix | 9 (Python ×3, Java ×2, C++ ×2, Go, Rust) |
| feature | 4 (Python ×2, Java, JavaScript) |
| refactor | 2 (Python, Go) |
| logs | 5 (logcat crash, ANR, tombstone, SELinux + .te fix, deep Java stack) |
| build | 4 (CMake, Soong/javac, Go interface, build-log fix) |
| git | 4 (rebase, cherry-pick conflict, bisect, history scrub) |
| safety | 2 (prompt injection, ambiguous delete) |
| question | 1 (AOSP module owner, needs `$AOSP`) |

## Not done (needs a model server)
- [ ] Baseline: crit (current text protocol) and opencode on the same model, server, quant
      and context size, `--runs 3`
- [ ] The 5 most common crit failure causes, read from the transcripts
- [ ] Stage 1–4 targets updated from the baseline

The GPU on the development machine is an RTX 3060 Laptop GPU (crit eval's `meta.json`),
too small for Qwen3.6-27B. The baseline needs a larger GPU or a LAN server, or a smaller
reference model for day-to-day runs (decide in `plan/decisions.md`).

## Notes for task authors
- `javac` on PATH here is Java 8 (`java` is 25): fixtures compile under Java 8.
- Go tasks run offline (`GOTOOLCHAIN=local GOPROXY=off`).
- `git-bisect-test` does not grade "leave the repo as you found it", because an untouched
  repo would pass that check and the audit requires every check to fail untouched.

## How to run the baseline
```bash
crit eval --config ~/.crit/qwen.json --runs 3 --out plan/results/00/crit \
  --note "server=llama.cpp bNNNN, Qwen3.6-27B Q4_K_M, ctx 32768, KV q8_0"
crit eval --runner opencode --model local/qwen3.6-27b --runs 3 --out plan/results/00/opencode --note "..."
crit eval compare plan/results/00/opencode plan/results/00/crit --out plan/results/00/baseline.md
```
