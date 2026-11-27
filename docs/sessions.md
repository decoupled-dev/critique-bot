# Sessions, resume, and context

Every `crit` session is written to an append-only event log. The log lets you list past
sessions, continue one after crit was closed or killed, copy one at any point (fork), read
it back, and share it with secrets removed. For a model API backend (a local server) the
same log drives compaction: when the context window fills up, old tool results are
replaced by pointers into the log, and if that is not enough, older turns are summarized.

## Where sessions live

```
.crit/sessions/<id>/
  events.jsonl   one JSON object per line, appended, never rewritten
  meta.json      id, title, created, updated, parent (fork), model, backend, cwd, status, turns, tokens
  blobs/<sha256> long strings (tool outputs, file contents, message lists), stored once by hash
  agent.json     the transcript of the last run in this session (what crit eval reads)
  agent.md       the same, readable
```

The id is the start time plus four hex digits, for example `20261127T101500Z-1a2b`. Commands
take any unique start of an id, or its last four characters, or `last`.

`.crit/sessions/` is in the project's `.gitignore` and readable only by you. Every string is
**redacted before it is written**, with the same redaction as a message to the model (secrets,
personal data, and your `redact_terms`), so a key printed by a command never lands in the log
or a blob. `"session_log": false` in `.crit/settings.json` turns the log off for new sessions.

### Events

Each event has `seq` (1, 2, ...), `ts` (UTC), `turn` (model replies so far) and `type`:

| type | what it records |
| --- | --- |
| `session_started` / `session_resumed` | model, backend, cwd, crit version; how a resume rebuilt the conversation |
| `system_prompt`, `tools_declared` | the prefix and the native tool list, with their hashes |
| `user_message` | a task as typed, with the mode and role |
| `model_request` | message count, prefix hash and whether the prefix is unchanged, tools hash, and for the web chat the text sent |
| `assistant_turn` | the reply, its tool calls, reasoning, usage (prompt, completion, cached tokens), parse notes, and `wire`: the messages the exchange added to a model API session |
| `permission_decision` | an approval asked or applied: kind, what, allowed, reason, mode |
| `tool_started`, `tool_result` | tool, call id, arguments, ok, a one-line summary, the full result |
| `checkpoint` | the undo checkpoint of a task |
| `mode_changed`, `role_changed` | a switch mid-session (old, new) |
| `compaction` | `new_chat`, `mask` or `summarize`, with tokens before and after |
| `memory_recall` | the project-memory hunch added to a task: its likely files with confidences, the source ids, its tokens and budget ([memory.md](memory.md)) |
| `task_finished` | the finish code (COMPLETED, FAILED, INTERRUPTED, ...) and the files changed |
| `error` | a reply that failed (it is retried) |

Writes are flushed at once and fsync'd at every turn boundary, so a hard kill loses at most
the turn in progress. A half-written last line is skipped when the log is read.

## Commands

| Command | What it does |
| --- | --- |
| `crit sessions` | List the project's sessions, newest first: id, time, status, turns, tokens, title. A session whose process is gone without finishing shows as `killed`. |
| `crit sessions show <id>` | A readable transcript rendered from the events (`--full` includes long outputs). |
| `crit sessions export <id>` | The whole session as one JSON document, blobs inlined. `--redacted` runs the redaction again and replaces the workspace path with `<workspace>` and your home folder with `~`, for sharing. `-o file` writes it to a file. |
| `crit sessions prune` | Remove sessions older than 30 days, and all but the newest 200 (`sessions_keep_days`, `sessions_keep` in settings; `--days`, `--keep`, `--dry-run`). A running session is never removed. |
| `crit resume [<id>\|--last] [task] [options]` | Continue a session (no id: the newest). Everything after the id is a normal crit command line, so `crit resume --last --yes "now add tests"` works. |
| `crit fork <id> [--at SEQ] [task] [options]` | Copy a session up to event `SEQ` (all of it without `--at`) into a new session and continue the copy. The original is not changed. |

In the terminal UI: `/sessions` lists them, `/resume <id>` makes your next message continue
that session, and `/fork [<id>] [--at SEQ]` copies one (the current session by default) and
continues the copy. `/status` shows the current session's id.

### How a resume rebuilds the conversation

- **Model API (local server):** the message list is rebuilt from the log (the
  `assistant_turn` deltas, with every mask and summary applied) and handed back to the
  session, up to the last complete turn. The instructions are not sent again. If crit was
  killed while tools ran, those calls get a "not run" answer and the next message says so.
- **Web chat:** a page conversation cannot be rebuilt, so a new chat starts with the
  instructions and a summary of the log (earlier tasks, the open task, files changed, the
  last command, the model's last words), the same way a long chat moves to a new one.

A fork at `SEQ` rebuilds exactly the history the original had at that event.

## Prompt order and the prompt cache

A local server reuses the work it did on the start of the conversation (llama.cpp and vLLM
match the prompt from the first token). crit keeps that start, the **prefix**, byte for byte
the same for the whole session:

1. the shell, the instructions and the role text (no date, no git status);
2. the tools: in the request with native tool calling, sorted by name; in the first message
   for the text formats;
3. `ENVIRONMENT`: facts that do not change in a session (OS, shell, workspace, sites, helpers);
4. the memory files (below).

What changes goes after it: the first task starts with a `SESSION` block (date and git
status), and a mode switch, a role switch (model API sessions) or a shell switch is
appended to the next message as a one-line note (`MODE CHANGED: ...`). Each `model_request`
event records the prefix hash; a test runs 20 turns with mode and role changes and checks
that the first message, the tools, and every earlier message are unchanged.

With llama.cpp crit also pins the session to one **slot**: `id_slot` is a stable hash of
the session id modulo the server's slots (`GET /props` `total_slots`), with
`cache_prompt: true`. Other servers get neither field. `"slot_pinning": false` turns it off;
a number sets the slot count without asking the server.

`/status` shows the cache hit ratio: the share of prompt tokens the server took from its
cache, as reported in usage (llama.cpp `timings.cache_n`, vLLM and others
`prompt_tokens_details.cached_tokens`).

## Memory files

crit reads these files and puts them in the prefix, least specific first, so a deeper file
overrides an earlier one:

1. `~/.crit/AGENT.md`: your notes for every project;
2. from the repository root down to the workspace, in each folder: `AGENTS.md`, then
   `.crit/AGENT.md` (only the text below `<!-- notes start -->`).

Together they may hold 32 KiB. Over that, crit cuts the least specific files first, says so in
the terminal and the log, and tells the model which file was cut. Nothing is cut silently.

**Keep them short and written by people.** A study of AGENTS.md files (ICLR 2026) found that
generated files make agents worse, and that even good human-written ones help only a
little while adding more than 20% to the cost of every request. Write the build and test
commands, the rules a newcomer would break, and nothing the code already says. crit never
writes or updates these files itself.

## Compaction (model API backends)

The budget is the context window: `local.num_ctx`, or what the server reports. Pressure is
the last request's prompt and reply tokens, from the server's usage, plus an estimate of the
message about to go. At a turn boundary, at most once every 5 replies:

| Pressure | What happens |
| --- | --- |
| over 70% | **Mask**: tool results older than the last 10 are replaced by a pointer such as `[elided: read_files src/x.py lines 1-100 -> blob 3f2a..., 4210 chars. Run the call again if you need it]`. The original stays in `blobs/`. |
| over 85%, or still over it after masking | **Summarize**: one extra request on a second session that holds the same messages (so the server reuses the cached prefix) writes GOAL, DECISIONS, FILES CHANGED, OPEN PROBLEMS, FAILING TESTS and NEXT STEPS. The first message is kept and the summary appended to it, the last 3 turns stay verbatim, the 5 most recently read files are attached fresh (secret files never), and the todo list is recited at the end. Without a second session the summary is built from the task state. |

Each compaction is a `compaction` event with the tokens before and after; the rebuilt
history applies it, so a resume continues from the compacted conversation. Settings:
`compaction` (`false` turns it off), `compaction_mask_at`, `compaction_summarize_at`,
`compaction_keep_results`, `compaction_keep_turns`, `compaction_every_turns`, and
`compaction_prefer_summary` (for a model that loses more from masking than from a summary;
`masking_penalty` in a model profile means the same).

The web chat keeps its own rule: a new conversation after a size, a number of messages, or
a time (`compact_after_chars`, `compact_after_turns`, `compact_after_minutes`), with a
handoff note.
