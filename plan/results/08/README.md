# Stage 8 results (2026-11-27)

Measured with `scripts/eval/bench_index.py` on the dev machine. Raw numbers are in the
`bench-*.json` files next to this one; each `bench-*.md` is the readable version.

## Index build, size, incremental update, query latency

| tree | parser | files | full build | db / source | 10 files changed | no-op refresh | query p95 (worst tool) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| crit (this repo, project mode) | regex | 779 | 3.8 s | 41 MB / 4.5 MB (920%) | 0.21 s | 0.03 s | locate 138 ms |
| crit (this repo, project mode) | tree-sitter | 779 | 4.0 s | 41 MB / 4.5 MB (919%) | 0.17 s | 0.03 s | locate 143 ms |
| AOSP `packages/services/Car` (module group) | regex | 9,859 | 14.2 s | 170 MB / 58 MB (291%) | 1.19 s | 0.21 s | repo_map 869 ms, locate 739 ms |
| AOSP `packages/services/Car` (module group) | tree-sitter | 9,859 | 18.0 s | 171 MB / 58 MB (292%) | 2.48 s | 0.54 s | repo_map 824 ms, locate 656 ms |

Every other tool (find_symbol, symbol_refs, who_calls, callees, imports_of, imported_by,
module_of, cochanged, git_history) is under 110 ms p95 on Car; find_strings 144-183 ms.

Notes:
- AOSP was read-only: the database went to the scratch folder; only `packages/services/Car`
  (one git project) was indexed, as a module group (no trigram text, no refs table), the way the
  tools build it on first use. The checkout is shallow, so its history has 1 commit.
- Project mode keeps the 1.0.x trigram text index and identifier refs (about 60% of the crit
  database); the new layers (edges, strings) add about 11 MB there.
- **Size target missed** (plan: db <= 10% of source on frameworks/base). On Car the call edges
  (241k rows, about 120 MB with their indexes) dominate. Next step: intern paths (integer file
  ids for `edges.src_path` / `strings.path`) and store call edges per file as one packed row
  with a name -> file index for who_calls; estimated 3-4x smaller. Not done in this round.
- frameworks/base was not indexed (out of bounds for this run), so the 15-min / 3 s / 300 ms
  targets are open; Car extrapolates to about 3-4 min for a frameworks/base-size module
  (~100k files) for L0-L3, but repo_map and locate need work to stay under 300 ms / 2 s there.

## locate without a model (10 localization tasks x 3 variants)

| variant / retrievers | file acc@1 | file acc@5 | function acc@5 |
| --- | --- | --- | --- |
| original, fused | 0.4 | 1.0 | 0.4 |
| original, names only | 0.3 | 0.7 | 0.3 |
| obfuscated names, fused | 0.4 | 1.0 | 0.3 |
| obfuscated names, names only | 0.1 | 0.5 | 0.1 |
| obfuscated + no comments, fused | 0.5 | 0.8 | 0.3 |
| obfuscated + no comments, names only | 0.1 | 0.5 | 0.1 |

This is `locate` alone, no model in the loop, on small fixtures (4-7 files each), so file
acc@5 is easy; the drop from original to obfuscated is the informative part: fused retrievers
lose 0 points (comments kept) and 20 points (comments stripped) at acc@5, names-only loses 20.
The agent-level metrics (acc@5 with the model, search turns, tokens) need model runs
(`crit eval --tag localization`), not done here.

## Strings and coverage

- find_strings: for 18/18 log lines quoted in the localization tasks (originals and obfuscated
  copies) the top hit is the literal that prints the message (format strings, Kotlin templates
  and numbers that differ included). The log-crash eval tasks carry only logs, no source, so
  they cannot be used for this target.
- coverage_locate on the suite's 3 Python bug-fix tasks (hidden failing check, plus the
  project's own tests as passing runs when present): the function the reference fix changes is
  in the top 5 for 2 of 3 (ranks 2, 6, 1). The miss is a shared-state bug where the faulty
  helper runs in passing tests too.
