# Decisions

Dated record of choices that change the plan. Newest first.

| Date | Decision | Why | Stage |
|---|---|---|---|
| 2026-11-27 | Finding code: minimize search turns (multi-pattern search, counts first, index-backed AOSP search, parallel calls); handle bad names by fusing name-independent signals (string literals/log tags, runtime coverage and stack traces, commit history, plain-language cards, structure, tests, optional embeddings) in `locate`; an obfuscated-names eval set; optional small-model search helper (9.8) | On a local model, round trips cost more than scanning; experts find badly named code by behavior and evidence, not names | 2, 8, 8b, 9 |
| 2026-11-27 | **Revised:** keep the web-chat backend, but as a removable module (`critique_bot/webchat/`, optional `webchat` extra), not removed in Stage 1. Core crit must work with the folder deleted (test + CI job) | User direction: do not remove it for now. Isolation gives the same design benefits (one API-shaped engine) without dropping existing users | 1, 3, 5, 10 |
| 2026-11-27 | Writes: find-and-replace with the match ladder is the main path; whole-file only as a fallback; `apply_patch` only for patch-trained models, with its own parser (no `git apply`); stale-read protection, atomic batched edits and atomic writes; n-gram speculative decoding in server presets, kept only if measured faster | Write time is output tokens, not disk I/O; exact unique matches fail loudly; fuzzy matching cuts edit errors 9x (Aider); a whole-file rewrite on a local 27B model can take minutes | 0, 1, 2 |
| 2026-11-27 | New Stage 8b, Project memory: structure cards, change patterns, episodes, approved conventions, per-task recall as a "hunch", KV primer; replaces 9.7 | User idea: give the agent an expert developer's cached mental model of the project instead of re-reading code every session. Designed against the AGENTS.md study's failure modes (generic, unverified, always injected, stale) | 8b, 9 |
| 2026-11-27 | (Superseded for the web chat by the revised row above.) Drop the web-chat backend completely (Stage 1, first work item); support many local model families and servers via profiles; cloud providers become a future Stage 11 behind the same `Provider` interface | User direction. Removes ~6K lines and the Playwright dependency, and leaves one model path (APIs) to design for | 1, 3, 5, 9, 10, 11 |
| 2026-11-27 | Plan created: 11 stages, 3 milestones; protocol and sandbox first, verification second, scale third | Literature review (`research/literature-review.md`): harness deltas on a fixed model are the measurable edge; native tool calls, edit guards, cache-aware context and a sandbox are the cheapest large wins | all |

## Open decisions
- Stage 1: keep `starlette`/`uvicorn` out of the base install? (decide in Stage 5)
- Stage 4: reuse Anthropic `sandbox-runtime` or write our own bwrap/seatbelt layer
- Stage 6: own MCP client vs the official `mcp` SDK as an extra
- Stage 10: project name; publish history or a fresh one; DCO vs CLA
