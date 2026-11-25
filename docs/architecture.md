# Architecture

crit is a harness in layers. Each layer can change, and be tested, without the ones above
it, because the layers above use it only through its interface. Code in the routing layer
wires the concrete pieces together.

```
5  routing   cli, agent_cli, __main__
             parse arguments, load config, open a backend, install a UI, call an app
4  apps      review_agent, review_session, review_comments   (review)
             queue, worker, gitlab, gitlab_post               (CI)
             agent_ui, agent_acp, welcome, setup_wizard   (terminal and IDE front ends)
3  engine    agent (the loop), agent_helpers (helper conversations), ui_api, openai_format
2  tools     tool_api (interface), agent_tools (implementation),
             agent_shell, agent_edit, agent_rules, agent_build, code_index, workspace, bot_home
1  models    model_api (interface), provider + browser + chat_client (web page in Edge),
             local_model (OpenAI-compatible server: llama.cpp, LM Studio, vLLM), setup_ui
0  core      config, log, patch (input limits), redaction, output, roles
```

A module imports only from its own layer and the layers below. `tests/test_layers.py`
checks every import, lazy ones included, and fails on a new crossing. Add a new module to
its `LAYERS` table.

## Interfaces

| Layer | Interface | What goes through it |
| --- | --- | --- |
| models | `model_api` | `ChatProvider` / `ChatSession` (`send`, optional `new_chat`, `stop_generation`, `save_failure`), `open_provider`, the errors (`ModelError`, `BackendError`, `ChatError`, ...), completion constants, `model_label`, `context_tokens` |
| tools | `tool_api` | `ToolRegistry`: one `ToolSpec` per tool (description, JSON Schema arguments, read-only flag), `permission`, `execute`; `use_registry` installs another registry. Also the shared types (`TaskState`, `ToolContext`, `Permission`) and the workspace services the engine drives (shell sessions, edit checkpoints, rules, index, `.bot` state). |
| engine → UI | `ui_api` | `NullUI` lists what a front end implements: output (`note`, `tool_start`, `tool_done`, ...), questions (`approve`, `ask_question`, `review_plan`, `read_message`), and UI-side changes (`take_shell_change`, `take_role_change`). `agent.use_ui(...)` installs one. |
| engine → apps | `agent.run_agent_loop`, `agent.run_read_only_task`, `AnswerContract` | An app gives a task, and for a read-only task the contract its final answer must meet (the review supplies "has the review JSON"). |

Defaults are safe and headless: with no UI installed the engine uses `NullUI`, which
approves nothing and asks nothing, and with no registry installed it uses the built-in
tools.

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
  over MCP, and MCP tools from other servers (for example the `mcpServers` an ACP client
  offers) can be added as a `ToolRegistry`. Not built yet.
- **ACP.** `crit --acp` already runs crit as an Agent Client Protocol agent
  ([acp.md](acp.md)); `agent_acp` is one more front end behind `ui_api`.
