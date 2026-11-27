# Stage 9: Scale and domain packs

## Goal
The features that make crit distinct once the base is solid: best-of-N attempts with hybrid
selection, the AOSP/device domain pack with a public task suite, a gated batch debugger,
budgets with an architect/editor cascade, precise cross-references and shareable indexes,
optional embeddings, and Python plugins. (Memory moved to Stage 8b.)

Each item is independent, so pick them in the order evals and users ask for. Each has its own
completion targets.

## 9.1 Best-of-N attempts with hybrid selection
**Why:** CodeMonkeys: 69.8% of issues had a correct candidate, random pick 45.8%, voting 57.4%.
R2E-Gym hybrid verifier: 51% from a 34.4% pass@1 32B agent. SWE-Gym verifier: +11.4 points.
**Design:** `--attempts N` (opt-in; agent-file `attempts:`). N worktrees, each running the
Stage 7 flow with varied temperature and seed; then selection: (1) tests (repro + selected),
(2) behavioral clustering (run generated tests across candidates; group by identical
results), (3) a judge tie-break with the reviewer agent. A cost meter shows tokens and time
before starting (estimated from earlier runs). On vLLM, run attempts concurrently; on a
single-slot llama.cpp, run them one after another and say so.
**Targets:**
- [ ] Bugfix subset with N=4: pass@1 ≥ Stage 7 + 8 points
- [ ] The selector picks a passing candidate in ≥ 80% of the tasks where one exists

## 9.2 AOSP / device domain pack
**Why:** Holmes: removing the log agent cut root-cause accuracy from 65.7% to 52.1%. Blueprint
First: a deterministic crash → stack → suspect-commit pipeline with a refusal guard. LLM-enhanced
crash reports: Top-1 localization from 10.6% to about 40%. No AOSP agent benchmark exists.
**Design:** pack = `packs/aosp/` with agent files (aosp-debugger, aosp-build-fixer,
selinux-helper), manifest tools (adb logcat/shell/push/pull/install, `dumpsys`, `bugreport`
parse, `atest`, `m <module>`, `lunch`, tombstone symbolization via `stack`/`addr2line`,
`audit2allow`), triage pipelines (crash → stack → owning module → suspect commits with
`git_history`; refuse a fix draft when no candidate is found), Soong-aware test selection,
and an `aosp` sandbox preset. Device-in-the-loop: flash/push/run with a confirmation before
anything that changes a device.
**AOSP task suite:** 40+ tasks (crashes, ANRs, SELinux denials, build breaks, HAL bugs, CarService
behaviors), each with a check. Published as its own repo with a leaderboard of harnesses on
fixed models: crit's own benchmark for the domain.
**Targets:**
- [ ] 40-task AOSP suite public, with graders audited (Stage 0 rules)
- [ ] crit + pack ≥ crit without pack + 15 points pass@1 on the suite; ≥ opencode + 20

## 9.3 Gated batch debugger (DAP / gdb / pdb)
**Why:** debug-gym: pdb helped Claude 3.7 (37.2% → 48.4%, and 52.1% when it was offered
only after 5 attempts); it hurt GPT-4o on easy tasks and did nothing for Llama-70B. No
gdb-agent evidence exists.
**Design:** a `debug_probe(target, breakpoints[], expressions[], max_hits)` batch tool:
runs the program or test under the debugger (DAP for Python/Java/Kotlin/C++, gdb/lldb
directly), stops at each breakpoint, records the stack and the requested expressions/locals,
and returns one capped report. No interactive stepping. **Offered only after k failed FIX
attempts** (default 2) or in an agent that opts in. Includes a short usage playbook in its description.
**Targets:**
- [ ] On a 10-task "needs runtime state" subset: pass@1 ≥ without debugger + 5 points
- [ ] No regression on the rest of the suite (gating works)

## 9.4 Budgets and architect/editor cascade
**Why:** token use explains 80% of performance variance (Anthropic); failed runs burn 15x;
Aider architect/editor improved every pair tested. Automatic escalation between models has
no direct study, and switching models breaks the cache.
**Design:** per-task and per-agent budgets (tokens and time; money once Stage 11 adds
cloud), shown live; `architect` agent setting: a planning model writes the change plan,
and the editor model applies it; escalation only at plan time or after a failed attempt,
never mid-turn. Before Stage 11 the pair is two local models (for example a large MoE on a
LAN server as architect, Qwen3.6-27B as editor). After Stage 11 the architect can be a cloud
model, given redacted context only and subject to taint rules.
**Targets:**
- [ ] Cascade (large local or LAN architect + 27B editor) ≥ 27B alone + 8 points on the hard subset
- [ ] After Stage 11: cloud architect + local editor ≥ local-only + 10 points, with ≥ 70%
      of tokens processed locally
- [ ] A budget overrun is impossible (hard stop tested)

## 9.5 Precise cross-references, shareable indexes, embeddings
**Why:** CodePlan-style impact analysis benefits from precise refs; full LSP is impractical
at AOSP scale; Cursor's +12.5% applies to grep + semantic together; no study covers shared
indexes, but building AOSP indexes live is not feasible.
**Design:**
- SCIP ingestion (scip-java, scip-clang from compile_commands, scip-python) into the L3
  tables, marked "precise"; Soong can emit compile_commands (`SOONG_GEN_COMPDB=1`).
- Shareable index bundles: `crit index export/import`, pinned to a repo manifest
  revision (`repo manifest -r` hash), signed (minisign/sigstore), verified on import, with
  local deltas laid over them. CI builds them nightly for the team's AOSP branch.
- Optional local embeddings (a small code embedding model served by the same local server,
  or sentence-transformers as an extra), chunked by symbol, used only inside `locate` and a
  `semantic_search` tool. Off by default.
**Targets:**
- [ ] AOSP import of a prebuilt bundle ≤ 5 min; verified signature; local edits reflected
- [ ] Localization acc@5 with SCIP + embeddings ≥ Stage 8 + 5 points on the AOSP set

## 9.6 Python plugins
**Why:** parity with opencode; no outcome evidence; in-process code bypasses the sandbox.
**Design:** entry-point plugins (`crit.plugins`) that can register tools, hooks, providers
and commands through the same registries. Disabled until the user enables each one; their
tools still run via the registry (permissions, audit), but in-process code is trusted code,
which the docs say plainly.
**Targets:**
- [ ] One example plugin (a provider + a tool) in the docs; plugin load is opt-in and listed in `/status`

## 9.7 (moved)
Memory proposals became the "conventions" layer (M4) of
[Stage 8b: Project memory](08b-project-memory.md), alongside structure memory, change
patterns, episodes and per-task recall.

## 9.8 Fast search helper (small model)
**Why:** on one GPU, search turns on the 27B model dominate localization time. A small model
generates several times faster. Cognition reports fast parallel search from an RL-trained
search model (SWE-grep), which is a vendor claim with no open replication.
**Design:** an agent file `searcher` (read-only tools: `search_code`, `find_symbol`,
`symbol_refs`, `locate`, `read_files` in windows; max 6 turns; up to 8 parallel calls per turn)
on a small model of the same family (a 4B-class Qwen) served next to the main model (a
second llama.cpp/vLLM instance, or a second model in the same server). It returns only
`file:line` candidates with a reason each (≤ 500 tokens). The main agent calls it via
`delegate`. The profile setting `search_helper: off | auto | on` decides; `auto` uses it only
when the main model's measured tokens/s is below a threshold.
**Targets:**
- [ ] On the localization set: time to the first correct file −40% against the main model
      searching alone, with acc@5 within 5 points
- [ ] No pass@1 drop on the full suite with `search_helper: auto`

## Risks (stage-wide)
- **Scope creep:** each item ships alone with its own eval; drop any whose targets aren't met.
- **Device safety:** anything that flashes or wipes a device requires explicit confirmation,
  even in auto mode.
