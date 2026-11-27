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
             agent_shell, agent_edit (match ladder, atomic writes), agent_patch (unified/V4A patches),
             edit_guard (parse check and lint after each write), tool_output (result shaping),
             env_snapshot (first-turn environment note), agent_rules, agent_build, code_index,
             workspace, project_home,
             file_catalog (cached file lists, repo trees), big_file (windowed reads),
             log_tools (the log tool), soong (Android.bp module lookup, lunch targets)
1  models    model_api (interface and backend registry),
             local_model (OpenAI-compatible server: llama.cpp, LM Studio, vLLM),
             webchat/ (optional: the web page in Edge; browser, chat_client, provider, setup_ui)
0  core      config, log, patch (input limits), redaction, output, roles
```

A module imports only from its own layer and the layers below. `tests/test_layers.py`
checks every import, lazy ones included, and fails on a new crossing. Add a new module to
its `LAYERS` table.

## Interfaces

| Layer | Interface | What goes through it |
| --- | --- | --- |
| models | `model_api` | `ChatProvider` / `ChatSession` (`send`, optional `new_chat`, `stop_generation`, `save_failure`), `open_provider`, the errors (`ModelError`, `BackendError`, `ChatError`, ...), completion constants, `model_label`, `context_tokens` |
| tools | `tool_api` | `ToolRegistry`: one `ToolSpec` per tool (description, JSON Schema arguments, read-only flag), `permission`, `execute`; `use_registry` installs another registry. `shape_output` (every built-in result: head and tail within the budget, the full text under `.crit/cache/out/`), `environment_snapshot` (the first-turn environment note). Also the shared types (`TaskState`, `ToolContext`, `Permission`) and the workspace services the engine drives (shell sessions, edit checkpoints, rules, index, `.crit` state). |
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
