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
             agent_ui, agent_acp, welcome, setup_wizard   (terminal and IDE front ends)
             audit_cmd, sandbox_doctor                    (crit audit, crit doctor sandbox)
3  engine    agent (the loop), agent_helpers (helper conversations), ui_api, openai_format
             sessions_cmd                                 (crit sessions / resume / fork)
3  engine    agent (the loop), agent_helpers (helper conversations), ui_api, openai_format,
             session_log (event log, projection, resume/fork), compaction (context pressure),
             prompt_build (byte-stable prefix, memory files, change notes)
2  tools     tool_api (interface), agent_tools (implementation),
             agent_shell, agent_edit, agent_rules, agent_build, code_index, workspace, project_home,
             file_catalog (cached file lists, repo trees), big_file (windowed reads),
             log_tools (the log tool), soong (Android.bp module lookup, lunch targets),
             sandbox/ (OS sandbox for commands: linux bwrap, macos sandbox-exec, windows),
             egress_proxy (the only network path out of the sandbox), taint (untrusted content)
             log_tools (the log tool), soong (Android.bp module lookup, lunch targets)
             extensions/ (manifest tools, MCP client, hooks, agent and command files, trust store)
1  models    model_api (interface and backend registry),
             local_model (OpenAI-compatible server: llama.cpp, LM Studio, vLLM),
             tool_parse (fallback tool-call parser), profiles (model profiles, server presets),
             webchat/ (optional: the web page in Edge; browser, chat_client, provider, setup_ui)
0  core      config, log, audit (audit.jsonl), patch (input limits), redaction, output, roles
```

A module imports only from its own layer and the layers below. `tests/test_layers.py`
checks every import, lazy ones included, and fails on a new crossing. Add a new module to
its `LAYERS` table.

## Interfaces

| Layer | Interface | What goes through it |
| --- | --- | --- |
| models | `model_api` | `ChatProvider` / `ChatSession` (`send`, optional `new_chat`, `stop_generation`, `save_failure`), `open_provider`, the errors (`ModelError`, `BackendError`, `ChatError`, ...), completion constants, `model_label`, `context_tokens`, `model_profile` / `tool_mode` / `native_tools_ok` (the model profile and native vs prompted tool calls), `server_info` / `probe_tool_turn` / `record_probe` (for `crit doctor model`) |
| tools | `tool_api` | `ToolRegistry`: one `ToolSpec` per tool (description, JSON Schema arguments, read-only flag), `permission`, `execute`; `use_registry` installs another registry. Also the shared types (`TaskState`, `ToolContext`, `Permission`) and the workspace services the engine drives (shell sessions, edit checkpoints, rules, index, `.crit` state). |
| tools → OS | `sandbox.run` | Every command the agent runs: `ShellSession` passes its `sandbox.Policy` (set by `agent_shell.use_sandbox`) to `sandbox.run`, which wraps the argv for the platform and returns a `Popen`. The engine sees only `Permission.contained` (auto mode runs contained commands unasked) and `taint.check` around its permission decision. |
| engine → UI | `ui_api` | `NullUI` lists what a front end implements: output (`note`, `tool_start`, `tool_done`, ...), questions (`approve`, `ask_question`, `review_plan`, `read_message`), and UI-side changes (`take_shell_change`, `take_role_change`). `agent.use_ui(...)` installs one. |
| engine → apps | `agent.run_agent_loop`, `agent.run_read_only_task`, `AnswerContract` | An app gives a task, and for a read-only task the contract its final answer must meet (the review supplies "has the review JSON"). |

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
API session's message list; resume and fork use it, and `LocalModelSession.replace_history`
installs it. A model API session (`_context_window` in the loop settings and a session with
`replace_history`) gets a `compaction.Compactor` at turn boundaries instead of the web
chat's size/turn/time rotation. `prompt_build` owns the prefix (`seed_message`), memory
files, the first turn's `SESSION` block and mid-session change notes.

What Stage 5 finishes: the loop still keeps the live history in the session object
(`LocalModelSession.messages`, or the web page) and logs it; the projection is used to
rebuild it on resume and checked against it in tests. Making `history_from_events` the
only source (the engine owns the message list and the session becomes a stateless
request function), and ACP `session/load`, move with the `agent.py` split.

## Testing a layer on its own

- **models:** a backend against `model_api` types; above it, any object with `send` is a
  session (`tests/test_review_agent.py` and `tests/test_tool_api.py` use scripted ones).
- **tools:** `tests/test_tool_api.py` checks the specs against the implementation and
  runs the engine on a fake `ToolRegistry`.
- **engine:** `run_agent_loop` with a scripted session, `NullUI`, and real or fake tools.
- **front ends:** install the UI (`agent.use_ui(agent_ui)`) and drive the loop, as
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
(`agent_helpers`), with its allowlist enforced through `ToolRegistry.only` and a git
worktree for an agent that writes.
- **ACP.** `crit --acp` already runs crit as an Agent Client Protocol agent
  ([acp.md](acp.md)); `agent_acp` is one more front end behind `ui_api`.
