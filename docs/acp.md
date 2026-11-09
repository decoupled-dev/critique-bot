# crit in Android Studio and other ACP clients (`crit --acp`)

`crit --acp` runs crit as an [Agent Client Protocol](https://agentclientprotocol.com) agent. Any ACP client can drive it: Android Studio's **Bring Your Own Agent** (Rabbit 2 and later), IntelliJ IDEA and the other JetBrains IDEs, and Zed. The client draws the chat, the tool calls, the diffs, and the approval buttons, while crit runs the same agent loop it runs in the terminal, with the same config.json, roles, modes, and saved permission rules.

```
crit --acp [--config FILE] [--permission-mode ask|edits|auto|plan] [--role ROLE]
critique-bot --mode agent --acp ...      # the same, from the bundled binary
```

The project folder comes from the client, as the `cwd` of its first `session/new`. You don't pass `--repo-dir`.

## Before you add it

Run `crit` once in a terminal in the project, the usual way, so that:

- the project has its `.bot/` folder and a config.json crit can find (or pass `--config` in the args below);
- for a web chat model, the Edge profile is signed in. ACP has no sign-in screen for crit, so later runs reuse that profile headless. A local model (`backend: local`) needs nothing more.

## Android Studio

Open **Settings → Tools → AI → Agents** and add a custom agent. The JetBrains IDEs keep custom agents in `~/.jetbrains/acp.json` (on Windows, `C:\Users\<you>\.jetbrains\acp.json`):

```json
{
  "agent_servers": {
    "crit": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\critique-bot\\bin\\crit.cmd",
      "args": ["--acp"]
    }
  }
}
```

`crit.cmd` is what `scripts\setup-crit.ps1` writes (default folder `%LOCALAPPDATA%\critique-bot\bin`, and the file needs the full path because JSON does not expand `%LOCALAPPDATA%`). It already carries `--config` and the proxy settings from setup. Without the script, point at the virtual environment's Python instead:

```json
"command": "C:\\path\\to\\critique-bot\\.venv\\Scripts\\python.exe",
"args": ["-m", "critique_bot", "--mode", "agent", "--acp", "--config", "C:\\path\\to\\critique-bot\\config.json"]
```

On Linux and macOS, use `"command": "/home/<you>/.local/bin/crit"` (or wherever `which crit` points) with the same `"args": ["--acp"]`.

When a chat opens, crit saves what the IDE offered (its name, its capabilities, and the MCP tool servers it passed, with env, header, and token values hidden) to `.bot/acp-client.json` in the project, and logs a one-line summary. That file shows which Android Studio tools could be wired in next.

Restart the IDE if crit does not appear in the agent picker. The agent log (stderr) is where crit's own errors go; add `--logs` to the args for more.

## What you see

| In crit | In the IDE |
|---|---|
| The model's words | The agent's reply |
| Status lines ("Starting a new chat", "Changed: …") | Agent thoughts (collapsed) |
| A tool call | A tool card with a kind (read, edit, execute, search, …), its files, and live command output |
| `edit_file` / `write_files` | A diff in the tool card |
| An approval (ask / edits modes, risky commands in auto) | The IDE's permission buttons: Yes, Yes and don't ask again, No, and Yes and switch to auto |
| Plan mode's "start on this plan?" | Permission buttons for auto / edits / ask, or No. No ends the turn with "What should change?", and your next message is the feedback. |
| A question with fixed answers | Permission buttons, one per answer, plus "Something else (answer in chat)" |
| A free-text question | The turn ends with the question. Your next message is the answer. |
| Ctrl+C | The IDE's Stop button (`session/cancel`) |
| ask / edits / auto / plan | The IDE's mode picker (`session/set_mode`) |

Slash commands in the prompt box: `/new` (fresh chat with the model), `/undo` (restore what the last task changed), `/mode ask|edits|auto|plan`, `/role <id>`. A new chat in the IDE (another `session/new` for the same folder) also starts a fresh chat with the model.

## Limits

- **One folder per process.** A `session/new` for a different folder is refused. The IDE starts one crit per project, which is the usual case.
- **No session restore** (`loadSession` is false). Closing the chat ends it, though crit's own transcript is still saved under `.bot/`.
- **Text only.** Attached files come in as `@path`, or inline when the IDE sends their text. Images are left out with a note.
- **crit's own tools run edits and commands.** crit does not route them through the IDE's `fs/*` or `terminal/*` methods, and does not connect to MCP servers the IDE passes in `session/new`.

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

Implementation: `src/critique_bot/agent_acp.py`, a drop-in replacement for the terminal UI module, like `agent_protocol.py` (see [agent-protocol.md](agent-protocol.md)). Tests: `tests/test_agent_acp.py`.
