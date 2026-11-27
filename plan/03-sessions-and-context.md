# Stage 3: Sessions and context

## Goal
Store every session as an append-only event log, and rebuild the conversation from that
log. On top of it: list, resume and fork; prompts ordered for cache hits; and compaction
driven by measured context pressure instead of turns and time.

## Why
- OpenHands V1 is event-sourced with negligible overhead and fewer system failures. The same
  log gives resume, fork, audit and eval replay.
- Context rot: every model tested by Chroma (Qwen3-32B included) degrades as input grows.
  NoLiMa: 10 of 12 "128K" models fall to half their score by 32K. Budget by effective context.
- The Complexity Trap (SWE-agent, Verified): masking old observations (keep the last 10)
  halves cost and matches LLM summarization. Summaries hide failure signals (+15% longer
  runs). For Qwen3-32B in non-thinking mode, masking cost 2 points, so use the hybrid: mask
  routinely, summarize rarely.
- Cache: Manus calls KV-cache hit rate the most important metric (100:1 input:output). Codex
  keeps tools, sandbox message and cwd byte-stable, appending any change. vLLM matches
  prefixes from the start in 16-token blocks. llama.cpp chooses a slot by similarity, so pin
  `id_slot`.
- Claude Code compaction keeps decisions, open bugs and implementation details, then
  re-attaches the 5 most recently read files.
- AGENTS.md study (ICLR 2026): LLM-written files hurt, human-written ones help only a
  little, and both add > 20% cost. So load human files hierarchically, keep them short, and
  never auto-generate them.

## Scope
Event log and projections; session commands; prompt assembly; compaction policy; memory
files (AGENTS.md support next to `.crit/AGENT.md`); cache metrics.

Out of scope: a sessions UI beyond the TUI (Stage 5 server exposes it), memory proposals
(Stage 9).

## Design

### Event log (`session_log.py`, engine layer; storage in `.crit/sessions/<id>/`)
```
events.jsonl   one JSON object per line, never rewritten
meta.json      id, title, created, parent (fork), model, profile, cwd, crit version
blobs/<sha>    large payloads (file contents, long tool outputs) by hash
```
Event types (v1):
`session_started`, `user_message`, `system_prompt` (hash + blob), `tools_declared`
(hash + blob), `model_request` (message-count, prompt hash, profile), `assistant_turn`
(text, tool_calls, reasoning ref, usage, parse_notes), `permission_decision`, `tool_started`,
`tool_result` (ok, summary, blob ref), `checkpoint` (undo id), `mode_changed`,
`role_changed`, `compaction` (strategy, range masked or summarized, tokens before/after,
summary blob), `task_finished` (exit_reason), `error`.

Every event carries `seq`, `ts` and `turn`. Writes are fsync'd at turn boundaries. Secrets
are redacted on write, using the same redaction as egress.

**Projection:** `history_from_events(events) -> list[Message]` is the *only* way the engine
builds history. Masking and summaries are events too, so the projection applies them, and
the original content stays recoverable in blobs.

### Commands
- `crit sessions` (list: id, title, when, task status, tokens), `crit resume [id|--last]`,
  `crit fork <id> [--at seq]`, `crit sessions show <id>` (a readable transcript), `crit
  sessions export <id> [--redacted]`.
- TUI: `/resume`, `/fork`, `/sessions`.
- Retention: keep 30 days or 200 sessions per project (setting); `crit sessions prune`.

### Prompt assembly (`prompt_build.py`, engine)
Fixed order, byte-stable within a session:
1. System prompt (role/agent text + engine rules), with no dates, git status or cwd in it
2. Tools, sorted by name, built-ins first (Stage 1)
3. Memory files: `~/.crit/AGENT.md` (user), then `AGENTS.md` / `.crit/AGENT.md` from the repo
   root down to the cwd, with deeper files overriding. Cap 32 KiB total; over the cap → a
   warning, not a silent cut
4. Repo map, if any (Stage 8), refreshed **only at compaction**
5. First user turn: environment snapshot (Stage 2) + date + git status + the task
6. Append-only turns

Changes mid-session (mode switch, role switch, new permission) are **appended** as a short
system-style user message, never edits to items 1–4. A role switch that needs a different
system prompt starts a new segment (compaction event) instead.

### Compaction policy (`compaction.py`, engine)
Budget `B = profile.effective_context` (or the server's reported window, if smaller).
Measure `used` from the last turn's `usage.prompt` + estimated pending.

| Trigger | Action |
|---|---|
| `used > 0.70·B` | **Mask**: replace tool results older than the last 10 with `[elided: read_files src/x.py lines 1-100 → blob abc123]`. One batch per trigger, never every turn |
| `used > 0.85·B` after masking, or before a known large step | **Summarize** older turns into a structured block (goal, decisions, files changed, open problems, failing tests, next steps), keep the last 3 turns verbatim, re-attach the 5 most recently read files (fresh read, capped) |
| Thinking-off profile (`masking_penalty: true`) | Prefer summarize over mask (Complexity Trap's Qwen3-32B finding) |

Today's turn- and time-based triggers (`compact_after_turns`, `compact_after_minutes`)
were for the web chat: they move into the web-chat provider (Stage 1), which can ask the
engine to start a new conversation. For model APIs only context pressure triggers compaction.

The todo list is re-stated at the end of every compaction summary, and after every
`todo` call, as the last message (recitation).

Compaction happens only at turn boundaries, at most once per N turns (default 5), and
each event records tokens before and after, plus the cache hit on the next request.

### Cache metrics
From usage: `cached / prompt` per request; logged in `model_request` events; summarized by
`crit eval` and shown in `/status`. llama.cpp: `cache_prompt: true`, and a stable `id_slot`
per session (slot = hash(session id) mod server slots, read from `/props`).

## Work items
1. `session_log.py`: writer, reader, blob store, redaction on write, projection.
2. Engine writes all events; history comes only from the projection. Remove ad-hoc
   transcript writing (keep a human-readable `transcript.md` *rendered* from events).
3. `prompt_build.py` with the fixed order; move dynamic state into the first user turn;
   append-only change notes.
4. Memory files: hierarchical AGENTS.md + `.crit/AGENT.md` loading; the cap; docs that say
   "keep it short and human-written".
5. `compaction.py`: masking, structured summary (one helper conversation), re-attach,
   recitation; profile flags; turn/time triggers only in the web-chat provider.
6. Session commands in `cli.py` and the TUI; retention and prune.
7. Cache metrics plumbing; `/status` shows the hit rate.
8. Eval with long tasks (add 3 tasks needing > 60 turns) and record results in `plan/results/03/`.

## Tests
- Projection: event fixtures → exact message lists; masking and summary events round-trip;
  original content recoverable from blobs.
- Crash safety: kill the process mid-turn → `crit resume` continues from the last complete turn.
- Fork at seq N gives identical history up to N.
- Prompt stability: across 20 turns with mode/role/permission changes, the bytes of items
  1–4 stay identical (hash test).
- Redaction: a secret in a tool output never appears in events.jsonl or blobs.

## Completion targets
- [ ] Every session is resumable after a hard kill; fork works at any seq
- [ ] Prompt-prefix hash is stable across the session in 100% of eval runs (except at
      recorded compaction events)
- [ ] Cache hit ratio (cached/prompt tokens) ≥ 80% median on llama.cpp and vLLM over the suite
- [ ] Long-task subset: pass@1 ≥ Stage 2 result, with ≥ 30% fewer prompt tokens per solved task
- [ ] No task fails because the context window overflowed
- [ ] `crit sessions`, `resume`, `fork`, `show`, `export --redacted` documented and tested

## Risks
- **Log size on AOSP sessions:** blobs are deduplicated by hash, and large tool outputs are
  already capped and stored once.
- **Masking hides something the model needs:** the elision text says how to get it back
  (path + range), and the blob stays available to a `recall` read.
