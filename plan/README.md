# crit → open-source coding agent: staged plan

Goal: turn crit into an open-source coding agent that beats opencode where results can be
measured. With the model held fixed, it should give more reliable tool calls, verified
changes and a real sandbox. On top of that it adds bring-your-own tools, file-defined
agents and an indexing engine that scales to AOSP.

Evidence base: [`research/literature-review.md`](research/literature-review.md) (a copy of the
literature review; the research notes behind it are in the main checkout under
`research_notes/Coding agent harness literature/`).
Every stage below says which findings it acts on.

## Principles (apply to every stage)

1. **Measure before and after.** Stage 0 builds the eval harness. No later stage counts as
   done until its eval targets are met on the suite, with the model held fixed (Qwen3.6-27B
   on llama.cpp and vLLM).
2. **Keep the layers.** models → tools → engine → apps → routing (`docs/architecture.md`).
   Put each new module in `tests/test_layers.py` `LAYERS`. No new crossings.
3. **Keep the tools boundary narrow.** Every tool (built-in, manifest, MCP, plugin) goes
   through `ToolRegistry`: spec, permission, execute. A later "own tool layer" can replace
   the registry without changing the engine.
4. **Stay local-first and private by default.** Nothing leaves the machine unless config
   allows it. Redaction, egress policy and secret-file gating apply on every path, including
   new ones.
5. **Web chat is a removable module.** In Stage 1 the web-chat backend moves into its own
   package (`critique_bot/webchat/`), found by core crit only through the provider registry,
   with Playwright as an optional extra. Core crit must work, and its tests pass, with that
   folder deleted. Everything new is designed for model APIs (local servers now, cloud APIs
   in Stage 11); nothing new may depend on a browser or on parsing a web page.
6. **Many local models, not one.** Model families (Qwen, DeepSeek, GLM, Mistral/Devstral,
   gpt-oss, Llama, Gemma, Kimi, MiniMax, …) and servers (llama.cpp, vLLM, SGLang, Ollama,
   LM Studio, TGI, MLX) differ through profiles and presets, never through code paths in the
   engine. Qwen3.6-27B is the reference model for targets, not the only one supported.
7. **Keep the cache prefix stable.** Within a session, never reorder or change the system
   prompt or the tool list. Changes go in as appended messages.
8. **One writer.** Sub-agents read, or write only in their own worktree. No writer swarms.

## Stages

| # | Stage | Main result | Depends on |
|---|---|---|---|
| 0 | [Baseline and evals](00-baseline-and-evals.md) | `crit eval` with pass@1/pass^3/tokens/errors; crit vs opencode baseline | — |
| 1 | [Model protocol](01-model-protocol.md) | Web chat isolated as a removable module; structured turns, native tool calls, fallback parser; profiles for many local model families and servers | 0 |
| 2 | [Tool layer hardening](02-tool-hardening.md) | Edit match ladder, parse/lint guard, output caps, shell defaults | 1 |
| 3 | [Sessions and context](03-sessions-and-context.md) | Event log, resume/fork, cache-aware prompts, compaction driven by context pressure | 1 |
| 4 | [Sandbox and safety](04-sandbox-and-safety.md) | OS sandbox, egress proxy, taint tracking, audit log | 3 |
| 5 | [Engine core and server](05-engine-and-server.md) | `agent.py` split, Engine API, HTTP/SSE server, clients on it | 1, 3 |
| 6 | [Extensibility](06-extensibility.md) | Manifest tools, MCP client, file-defined agents, commands, hooks | 4, 5 |
| 7 | [Verified changes](07-verified-changes.md) | repro → fix → test → review flow, plan triggers, impact list | 2, 6 |
| 8 | [Indexing engine](08-indexing-engine.md) | Lexical + symbol graph + git tools, incremental, per-module | 2 |
| 8b | [Project memory](08b-project-memory.md) | Expert-like "cached mental model": structure cards, change patterns, episodes, approved conventions, per-task recall, KV primer | 3, 7, 8 |
| 9 | [Scale and domain packs](09-scale-and-domain.md) | Best-of-N, AOSP pack, gated debugger, cascades, plugins, AOSP bench | 7, 8 |
| 10 | [Open-source release](10-open-source-release.md) | Clean public repo, docs, CI, governance | runs alongside 5 onward |
| 11 | [Cloud providers (future)](11-cloud-providers.md) | Cloud APIs as adapters behind the Stage 1 interface, local-first rules intact | 1, 4; after M2 |

```
0 ──► 1 ──► 2 ──────────────► 7 ──► 9
      │     └──► 8 ───────────────────┘
      │          └──► 8b ◄── 3, 7
      └──► 3 ──► 4 ──► 6 ──► 7
            └──► 5 ──┘
10 (runs alongside from 5)        11 (after M2, needs 1 + 4)
```

## Milestones

| Milestone | Stages | Headline target |
|---|---|---|
| **M1: Reliable local alpha** | 0–4 | Same model, same server: crit pass@1 ≥ opencode + 5 points on the crit suite; malformed-call rate < 1%; sandbox on by default on Linux |
| **M2: Public beta** | 5–7, 10 | Repo public; headless server; BYO tools + MCP + agents; verified-change flow on by default for bug fixes, with precision ≥ 1.5× the unverified run |
| **M3: 1.x differentiation** | 8, 8b, 9 | AOSP pack and public AOSP task suite; localization acc@5 ≥ 80% on it; project memory cuts turns-to-first-correct-file by 40%; best-of-N opt-in |
| **M4: Cloud (future)** | 11 | Cloud adapters pass the provider contract tests; crit-vs-opencode numbers published for cloud models too |

## How to read a stage file

Each file has the same sections: **Goal**, **Why** (evidence), **Scope / Out of scope**,
**Design**, **Work items** (ordered, with the files touched), **Tests**, **Completion
targets** (checkboxes; a stage is done when every box is ticked), **Risks**.

Numbers in the completion targets are **provisional until Stage 0 records the baseline**.
After that, update them in place and note the date.

## Working rules

- Work continues on `feat/aaos-scale` (one-branch rule). If a stage becomes too large for a
  single branch, say so before splitting.
- Every stage ends with: full test suite green (scratch venv, `PYTHONPATH=src`), eval run
  results committed under `plan/results/<stage>/`, and docs updated (`docs/architecture.md`
  and any user docs that change).
- Decisions that change the plan go into [`decisions.md`](decisions.md), dated, with the reason.
