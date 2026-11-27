# Architecture

crit is a harness in layers. Each layer can change, and be tested, without the ones above
it, because the layers above use it only through its interface. Code in the routing layer
wires the concrete pieces together.

```
5  routing   cli, agent_cli, __main__
             parse arguments, load config, open a backend, install a UI, call an app
4  apps      review_agent, review_session, review_comments   (review)
             queue, worker, gitlab, gitlab_post               (CI)
             eval_harness, doctor                             (measuring, checking the model server)
             agent_ui, agent_acp, welcome, setup_wizard       (terminal and IDE front ends)
             server, api_client                               (crit serve; crit attach, crit run)
             audit_cmd, sandbox_doctor, sessions_cmd, extensions_cmd, config_cmd, installer
3  engine    engine/ (the loop and the Engine API, below), ui_api (the UI interface),
             session_log (event log, projection, resume/fork), compaction (context pressure),
             prompt_build (byte-stable prefix, memory files, change notes), openai_format,
             agent (compatibility names for older callers; apps may not import it)
2  tools     tool_api (interface), agent_tools (implementation),
             agent_shell, agent_edit (match ladder, atomic writes), agent_patch (unified/V4A patches),
             edit_guard (parse check and lint after each write), tool_output (result shaping),
             env_snapshot (first-turn environment note), agent_rules, agent_build, code_index,
             workspace, project_home, file_catalog (cached file lists, repo trees), big_file
             (windowed reads), log_tools (the log tool), soong (Android.bp module lookup),
             sandbox/ (OS sandbox for commands: linux bwrap, macos sandbox-exec, windows),
             egress_proxy (the only network path out of the sandbox), taint (untrusted content),
             extensions/ (manifest tools, MCP client, hooks, agent and command files, trust store)
             indexing/ (the code index: index.db layers, the index tools; docs/indexing.md)
             packs/ (domain packs, loaded through extensions: aosp triage/adb/aosp_build; docs/packs/aosp.md)
1  models    model_api (interface and backend registry),
             local_model (OpenAI-compatible server: llama.cpp, LM Studio, vLLM),
             tool_parse (fallback tool-call parser), profiles (model profiles, server presets),
             webchat/ (optional: the web page in Edge; browser, chat_client, provider, setup_ui)
0  core      config, log, audit (audit.jsonl), patch (input limits), redaction, output, roles
```

A module imports only from its own layer and the layers below. `tests/test_layers.py`
checks every import, lazy ones included, and fails on a new crossing; a package counts as
one module. Add a new module to its `LAYERS` table.

## Interfaces

| Layer | Interface | What goes through it |
| --- | --- | --- |
| models | `model_api` | `ChatProvider` / `ChatSession` (`send`, optional `new_chat`, `stop_generation`, `save_failure`, and for a model API session `history` / `replace_history`), `open_provider`, the errors (`ModelError`, `BackendError`, `ChatError`, ...), completion constants, `model_label`, `context_tokens`, `model_profile` / `tool_mode` / `native_tools_ok` (the model profile and native vs prompted tool calls), `server_info` / `probe_tool_turn` / `record_probe` (for `crit doctor model`) |
| tools | `tool_api` | `ToolRegistry`: one `ToolSpec` per tool (description, JSON Schema arguments, read-only flag), `permission`, `execute`; `use_registry` installs one process-wide, and `ToolContext.registry` gives a session its own. `shape_output` (every built-in result: head and tail within the budget, the full text under `.crit/cache/out/`), `environment_snapshot` (the first-turn environment note). Also the shared types (`TaskState`, `ToolContext`, `Permission`) and the workspace services the engine drives (shell sessions, edit checkpoints, rules, index, `.crit` state). |
| tools → OS | `sandbox.run` | Every command the agent runs: `ShellSession` passes its `sandbox.Policy` (set by `agent_shell.use_sandbox`) to `sandbox.run`, which wraps the argv for the platform and returns a `Popen`. The engine sees only `Permission.contained` (auto mode runs contained commands unasked) and `taint.check` around its permission decision. |
| engine → UI | `ui_api` | `NullUI` lists what a front end implements: output (`note`, `tool_start`, `tool_done`, ...), questions (`approve`, `ask_question`, `review_plan`, `read_message`), and UI-side changes (`take_shell_change`, `take_role_change`, `take_session_change`). `engine.use_ui(...)` installs one for the process; the Engine gives each session thread its own (`ui.use_thread_ui`). |
| engine → apps | `engine` (the package's `__init__`) | `Engine` / `SessionHandle` (below), `drive_ui`, `run_agent`, `run_read_only_task` + `AnswerContract` (an app gives a task, and for a read-only task the contract its final answer must meet), `run_agent_loop`, `use_ui` / `current_ui`, and the small pieces front ends show (`ToolCall`, `activity`, `hide_tool_markup`, `READ_ONLY_TOOLS`). Apps may also use `ui_api` and `session_log`; every other engine module is private to the engine and the routing layer. |
| apps → network | `server` (`crit serve`) | The Engine API over HTTP and server-sent events, specified in [`api/openapi.yaml`](api/openapi.yaml); see [server.md](server.md). |

## The engine package

`critique_bot/engine/` was `agent.py` (4,700 lines) until Stage 5. No module in it is over
800 lines.

| Module | What it holds |
| --- | --- |
| `api.py` | `Engine` (create, open, fork and list one project's sessions), `SessionHandle` (`send`, `events(since_seq)`, `answer`, `cancel`, `set_mode`, `undo`, `close`), `EventUI`, `drive_ui` |
| `runs.py` | `run_agent`: one whole session (open the backend, set the session up, run the loop, save the transcript) |
| `loop.py` | `run_agent_loop`: tasks in, one task run each; the first message (task, repo map, skills, notes, first-turn context); task-end bookkeeping |
| `task.py` | `_TaskRun`: the turn loop of one task (send, read the reply, decide), with three mixins: |
| `dispatch.py` | running a reply's tool calls: permission, approval, rules, taint, parallel reads, repeat and loop detection |
| `modes.py` | ask / edits / auto / plan, plan review, mode change notes, plan triggers |
| `stop_rules.py` | when a task stops: finish checks, the project check, refusals, budgets; the flow's no-progress watch and per-state token budgets |
| `task_class.py` | Stage 7: bugfix / feature / refactor / question / ops by rules, one model question when unclear; the flow for a class |
| `flows/` | Stage 7: the verified flow (`verified.py`: REPRODUCE, FIX, TEST, REVIEW, DONE as sub-goal messages and permission profiles on the one writer), its model-free checks (`checks.py`: repro verdicts, failing-test counts, test selection), the reviewer (`review.py`: the `verifier` agent in a fresh chat, the review JSON contract) and the per-task start (`start.py`: which flow, the impact list, plan triggers) |
| `impact.py` | Stage 7: signature changes after an edit and their call sites, as todos |
| `contracts.py` | `AnswerContract`, `run_read_only_task` (the review's read-only session) |
| `delegate.py` | helper chats and file-defined agents (`delegate`), one pool per session |
| `chat.py` | `_Chat`: the conversation, retries, size and health; **the engine's message list** for a model API session |
| `context.py` | the Stage 3 hooks: logged tool calls, compaction at turn boundaries, resume, session switch, meta |
| `prompts.py` | `prompts/agent*.txt` sections, tool formats, the task message, skills |
| `parse.py`, `json_repair.py`, `replies.py` | reading a reply: tool calls (tags or Responses items), lenient JSON, finish codes, refusals, questions, plans |
| `setup.py` | the tool format and set (apply_patch only for patch models), extensions, the shell, the role, profile values as loop settings |
| `ui.py`, `runtime.py` | what a running session is connected to: its UI (process-wide or the session thread's own), its native tool list and extensions |
| `text.py` | small helpers |

`session_log`, `compaction` and `prompt_build` stay sibling modules in the engine layer
(Stage 3), because `crit sessions` (an app) reads `session_log`, and the other two are
self-contained; the engine package imports them. `agent.py` keeps the names older callers
and tests import (90 lines); apps cannot import it.

### The Engine API

```python
engine = Engine(config, home)                      # one project, one model configuration
session = engine.create_session(mode="ask")        # the session log exists at once: session.id
session.send("fix the failing test")               # starts the task on the session's thread
for event in session.events(since_seq=0):          # replays the log, then follows it live
    if event["type"] == "request":                 # an approval, a question, a plan review
        session.answer(event["id"], {"decision": "yes"})
    if event["type"] == "task_finished":
        break
engine.open_session(session.id)                    # resume (after a crash too)
engine.fork_session(session.id, at_seq=12)
```

A `SessionHandle` runs `run_agent` on a worker thread whose UI is an `EventUI`. Every UI
call becomes a session-log event (`notice`, `assistant_text`; tool calls are already
`tool_started` / `tool_result`), and `approve`, `ask_question` and `review_plan` become a
`request` event with an id that blocks the task until `answer(request_id, reply)`; only the
first answer counts (`RequestClosed` after it). Because events are the log's, with its
`seq`, a client that reconnects asks for `events(since_seq=N)` and misses nothing, and a
session whose process died is resumed by `open_session` from the same log.

`drive_ui(events, front, answer=..., send=...)` is the other direction: it shows a session
on any front end with `NullUI`'s methods and answers the session's requests through it.
`crit attach` uses it over HTTP; an in-process front end uses it on a `SessionHandle`.

### Who uses what

| Client | How it reaches the engine |
| --- | --- |
| `crit` in a terminal (TUI) | `engine.run_agent` in process, with the terminal installed as the UI (`engine.use_ui(agent_ui)`); the same run a `SessionHandle` drives |
| `crit serve` + `crit attach` | `Engine` / `SessionHandle` behind HTTP; `drive_ui` over the event stream |
| `crit run [--agent] [--json]` | `Engine.create_session(unattended=True)`, events printed as JSON lines (or `--url`: a server's agent run) |
| `POST /v1/agents/{name}/runs` | a headless session with the file-defined agent as the role |
| ACP (`crit --acp`) | `run_agent` with `agent_acp` as the UI; `session/load` resumes a logged session (the ACP session id is the log id) |
| `crit review` (agent review) | `engine.run_read_only_task` with `review_agent.CONTRACT` |
| queue / worker | unchanged one-shot reviews (`review_session`); CI can instead submit to a `crit serve` (`crit run --url ... --agent code-reviewer`) |

## The web chat is a removable module

`critique_bot/webchat/` holds everything for driving a chat page in Edge. Nothing outside
it imports it (`tests/test_layers.py` checks, and that nothing outside imports Playwright).
Importing the package registers the `browser` backend with `model_api.register_backend`,
plus the hooks the command line uses (sign-in, `crit setup`, blocked-site reports), which
it reaches through `model_api.backend_hooks("browser")`. Playwright is the `[webchat]`
extra. With the folder deleted, crit works with local models and a `backend: browser`
config gets one message saying how to install it; `scripts/check_webchat_removable.py`
runs the core test suite on such a copy (CI does too). Web-chat tests inside core test
modules carry `@needs_webchat` (`tests/webchat_support.py`).

Defaults are safe and headless: with no UI installed the engine uses `NullUI`, which
approves nothing and asks nothing, and with no registry installed it uses the built-in
tools.

## Sessions and context (Stage 3)

Every session is an append-only event log in `.crit/sessions/<id>/` (`session_log`, see
[sessions.md](sessions.md)). The loop records through a few hooks: `_Chat.send` writes
`model_request` / `assistant_turn` (with the `wire` messages a model API session added),
`_LoggedHooks` wraps a task run's hooks for `tool_started` / `tool_result`, `_approved`
writes `permission_decision`, and the task loop writes `user_message`, `checkpoint`,
`mode_changed`, `role_changed` and `task_finished`. `history_from_events` rebuilds a model
API session's message list; resume and fork use it, and the engine installs it
(`_Chat.set_history`). A model API session (`_context_window` in the loop settings and a session with
`replace_history`) gets a `compaction.Compactor` at turn boundaries instead of the web
chat's size/turn/time rotation. `prompt_build` owns the prefix (`seed_message`), memory
files, the first turn's `SESSION` block and mid-session change notes.

Stage 5 made the engine the owner of a model API session's conversation: `_Chat.messages`
is installed in the session (`replace_history`) before every request and grows by what the
exchange appended; compaction, resume and a new chat replace it through the engine. The
web chat's conversation stays on its page (there is no list to own); resume gives it a
summary instead.

## Testing a layer on its own

- **models:** a backend against `model_api` types; above it, any object with `send` is a
  session (`tests/test_review_agent.py` and `tests/test_tool_api.py` use scripted ones).
- **tools:** `tests/test_tool_api.py` checks the specs against the implementation and
  runs the engine on a fake `ToolRegistry`.
- **engine:** `run_agent_loop` with a scripted session, `NullUI`, and real or fake tools;
  the Engine API with a fake provider (`tests/test_engine.py`).
- **server:** `tests/test_server.py` against `docs/api/openapi.yaml`, plus a killed server.
- **front ends:** install the UI (`engine.use_ui(agent_ui)`) and drive the loop, as
  `tests/test_agent_ui.py` does.

## Where standard protocols plug in

- **OpenAI Responses format.** The default (`"tool_format": "openai"`): the model itself speaks it,
  on the web page and with a local model alike:
  the first message carries the registry's tools as a Responses `tools` array, replies are
  `{"output": [...]}` with `function_call` and `message` items, and results go back as
  `function_call_output` items (`openai_format.py`; the format-specific prompt sections are in
  `prompts/agent_openai.txt`). Not built yet: a backend that sends `tools` through a native API
  (the local backend already talks to `/v1/chat/completions`) and returns structured calls.
- **MCP.** `ToolSpec.to_mcp()` gives `tools/list` entries, so crit's tools can be served
  over MCP (not built yet). MCP tools from other servers (`mcp.json`, and the `mcpServers`
  an ACP client offers) are a client in `extensions/mcp_client.py`, merged into the session's
  `ToolRegistry`.

## Extensions

`extensions/` (a package, part of the tools layer's interface) reads tools, MCP servers,
agents, slash commands and hooks from `~/.crit` and the project's `.crit`
([extending.md](extending.md)). `extensions.load_session` runs once at session start, and
its `registry(base)` returns one `ToolRegistry` built with `tool_api.merge`: an extension
tool's permission and execution go through the same registry calls as a built-in's, and
the `pre_tool`/`post_tool` hooks wrap that registry. The engine sees no special cases. It
gets one registry, plus `run_hook` for the hooks that sit in the loop (`user_prompt`,
`task_finished`, `pre_compact`). Every extension process starts in `extensions.runner.run_argv`,
which the sandbox replaces. File-defined agents are profiles: `/agent` (alias `/role`)
seeds the main chat with one, and `delegate(agent=...)` runs one in a helper chat
(`engine/delegate.py`), with its allowlist enforced through `ToolRegistry.only` and a git
worktree for an agent that writes.

Domain packs (`packs/`, also tools layer) are built-in bundles that `load_session` adds when the
settings name them: their manifest tools (which may name Python handlers in the pack) and agents
load like the user's own, so the engine still sees one registry.
- **ACP.** `crit --acp` already runs crit as an Agent Client Protocol agent
  ([acp.md](acp.md)); `agent_acp` is one more front end behind `ui_api`.
- **ACP.** `crit --acp` runs crit as an Agent Client Protocol agent ([acp.md](acp.md));
  `agent_acp` is one more front end behind `ui_api`, and `session/load` resumes a logged session.

## Writes, edits and tool results (Stage 2)

- **Match ladder** (`agent_edit.replace_span`): exact → line endings / trailing whitespace →
  indentation (re-indented, tabs vs spaces) → markdown punctuation → fuzzy (≥ 92% and ≥ 5 points
  ahead of any other window). Several hits are refused with their line numbers; a miss shows the
  closest candidate and the first line that differs. `apply_patch` (`agent_patch`) finds each
  hunk through the same ladder and ignores line numbers; no `git apply`.
- **Write safety**: reads and writes record a digest of the file; an edit to a file changed on
  disk since then is refused (`stale_edit_refusals`), and an edit to a file never read applies
  only on an exact match. Batches and multi-file patches are all or nothing; writes go through
  a temp file in the same folder, `fsync`, then `rename`, keeping encoding, BOM, line endings,
  the final-newline state and permission bits.
- **Guard** (`edit_guard`): a parse check (tree-sitter with the `index` extra, else the
  language's cheap front end) and a time-bounded lint that reports only new issues; setting
  `edit_guard` is `flag` (default), `reject` or `off`, and `lint` overrides the linter table.
- **Results**: `tool_output.shape_output` keeps every result within `tool_output_cap_tokens`
  (else the engine's per-result size, at most 10K tokens), saving the full text; empty results
  say what happened. The task state counts failed edits per file, and after `edit_fail_limit`
  (default 2) the result suggests a whole-file write (or smaller edits over
  `whole_file_max_lines`, default 800).
