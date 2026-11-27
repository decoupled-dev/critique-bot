# crit in Android Studio and other ACP clients (`crit --acp`)

`crit --acp` runs crit as an [Agent Client Protocol](https://agentclientprotocol.com) agent. Any ACP client can drive it: Android Studio's **Bring Your Own Agent** (Rabbit 2 and later), IntelliJ IDEA and the other JetBrains IDEs, and Zed. The client draws the conversation, the tool calls, the diffs, and the approval buttons, while crit runs the same agent loop it runs in the terminal, with the same config.json, roles, modes, and saved permission rules.

```
crit --acp [--config FILE] [--permission-mode ask|edits|auto|plan] [--role ROLE]
crit.exe --acp ...                       # the same, from the unzipped Windows program
```

The project folder comes from the client, as the `cwd` of its first `session/new`. You don't pass `--repo-dir`.

## Before you add it

Run `crit` once in a terminal in the project, the usual way, so that:

- the project has its `.crit/` folder and a config.json crit can find (or pass `--config` in the args below);
- for a model on a web page, the Edge profile is signed in. ACP has no sign-in screen for crit, so later runs reuse that profile headless. A local model (`backend: local`) needs nothing more.

## Android Studio

Open **Settings → Tools → AI → Agents** and add a custom agent. The JetBrains IDEs keep custom agents in `~/.jetbrains/acp.json` (on Windows, `C:\Users\<you>\.jetbrains\acp.json`):

```json
{
  "agent_servers": {
    "crit": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\crit\\bin\\crit.cmd",
      "args": ["--acp"]
    }
  }
}
```

`crit.cmd` is what `scripts\setup-crit.ps1` writes (default folder `%LOCALAPPDATA%\crit\bin`, and the file needs the full path because JSON does not expand `%LOCALAPPDATA%`). It already carries the config (through `CRIT_CONFIG`) and the proxy settings from setup. After `crit install` (or the one-line installer) the launcher is `C:\Users\<you>\.crit\bin\crit.cmd` instead. Without the script, point at the virtual environment's Python instead:

```json
"command": "C:\\path\\to\\critique-bot\\.venv\\Scripts\\python.exe",
"args": ["-m", "critique_bot", "--acp", "--config", "C:\\path\\to\\critique-bot\\config.json"]
```

On Linux and macOS, use `"command": "/home/<you>/.local/bin/crit"` (or wherever `which crit` points) with the same `"args": ["--acp"]`.

When a session opens, crit saves what the IDE offered (its name, its capabilities, and the MCP tool servers it passed, with env, header, and token values hidden) to `.crit/acp-client.json` in the project, and logs a one-line summary. That file shows which Android Studio tools could be wired in next.

Restart the IDE if crit does not appear in the agent picker. crit's own errors go to the agent log (stderr) and to `~/.crit/logs/crit.log`; add `--logs` to the args to see every log line in the agent log too.

## What you see

| In crit | In the IDE |
|---|---|
| The model's words | The agent's reply |
| Status lines ("Starting a new conversation", "Changed: …") | Agent thoughts (collapsed) |
| A tool call | A tool card with a kind (read, edit, execute, search, …), its files, and live command output |
| `edit_file` / `write_files` | A diff in the tool card |
| An approval (ask / edits modes, risky commands in auto) | The IDE's permission buttons: Yes, Yes and don't ask again, No, and Yes and switch to auto |
| Plan mode's "start on this plan?" | Permission buttons for auto / edits / ask, or No. No ends the turn with "What should change?", and your next message is the feedback. |
| A question with fixed answers | Permission buttons, one per answer, plus "Something else (answer in the conversation)" |
| A free-text question | The turn ends with the question. Your next message is the answer. |
| Ctrl+C | The IDE's Stop button (`session/cancel`) |
| ask / edits / auto / plan | The IDE's mode picker (`session/set_mode`) |

Slash commands in the prompt box: `/new` (fresh conversation with the model), `/undo` (restore what the last task changed), `/mode ask|edits|auto|plan`, `/role <id>`. A new session in the IDE (another `session/new` for the same folder) also starts a fresh conversation with the model.

## Limits

- **One folder per process.** A `session/new` for a different folder is refused. The IDE starts one crit per project, which is the usual case.
- **Session restore** (`loadSession` is true). The ACP session id is the id of crit's session log (`crit sessions` lists them). `session/load` with that id continues the session: crit replays the conversation (your tasks, its answers, the tool calls) as `session/update` notifications, then answers the load. A first `session/load` starts crit on that log, as `crit resume` does; a later one switches to it at your next prompt. A model API backend gets its message list back; the web chat starts a new chat with a summary of the log.
- **Text only.** Attached files come in as `@path`, or inline when the IDE sends their text. Images are left out with a note.
- **crit's own tools run edits and commands.** crit does not route them through the IDE's `fs/*` or `terminal/*` methods. The MCP servers the IDE passes in `session/new` (stdio and HTTP) are connected like the ones in `~/.crit/mcp.json`: their tools appear as `mcp__<server>__<tool>`, pinned and with untrusted output (see [extending.md](extending.md)).

## Protocol details

JSON-RPC 2.0, one message per line, protocol version 1.

| Direction | Method | Notes |
|---|---|---|
| IDE → crit | `initialize` | `authMethods: []`, `promptCapabilities.embeddedContext: true` |
| IDE → crit | `session/new` | answered once crit has started (config loaded, model reachable). A startup failure is the error on this request. |
| IDE → crit | `session/prompt` | answered with `stopReason`: `end_turn`, `cancelled`, or `max_turn_requests` (out of tool rounds) |
| IDE → crit | `session/cancel` | interrupts the running task |
| IDE → crit | `session/set_mode` | `ask`, `edits`, `auto`, `plan` |
| crit → IDE | `session/update` | `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `current_mode_update`, `available_commands_update` |
| crit → IDE | `session/request_permission` | approvals, plan review, multiple-choice questions |

Implementation: `src/critique_bot/agent_acp.py`, a drop-in replacement for the terminal UI module (see `ui_api.py`). Tests: `tests/test_agent_acp.py`.
