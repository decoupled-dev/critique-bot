# Agent protocol (`crit --protocol`)

How a GUI drives the agent. The VS Code extension in `vscode-extension/` uses
it; a later Electron app will use the same one.

```
crit --protocol [--repo-dir DIR] [--config FILE] [--permission-mode ask|edits|auto|plan] [--role ROLE]
critique-bot --mode agent --protocol ...      # the same, from the bundled binary
```

The process reads **one JSON object per line on stdin** and writes **one JSON
object per line on stdout**, UTF-8, `\n`-terminated. Nothing else is written
to stdout: stray prints and child processes are pointed at stderr, and child
processes get an empty stdin. Stderr carries logs (`--logs` for more) and
fatal `error: ...` lines; show it in an output panel.

For IDEs that speak the Agent Client Protocol (Android Studio, JetBrains, Zed), use `crit --acp` instead: [acp.md](acp.md).

Implementation: `src/critique_bot/agent_protocol.py`, a drop-in replacement
for the terminal UI module `agent_ui.py`. Tests: `tests/test_agent_protocol.py`.

## Version

The first event is `hello` with `"protocol": 1`. A GUI refuses a different
number. Any incompatible change bumps it. Adding a new event type or a new
field is compatible; GUIs ignore what they do not know.

## Session flow

```
agent → hello
agent → ready                      (waiting for a task)
gui   → prompt {text}
agent → note{kind:"task"}, thinking, assistant, tool_start, tool_output, tool_done, ...
agent → approval {id, ...}         (only when the mode asks)
gui   → answer {id, value}
agent → diff, status {code}
agent → ready                      (next task; same chat)
gui   → shutdown                   (or close stdin)
agent → bye {code}
```

`bye` is always the last event. If the process ends before `hello` (no
config.json, browser failed to start), there is only `bye` with a non-zero
code; the reason is on stderr.

## Events (agent → GUI)

| type | fields | meaning |
|---|---|---|
| `hello` | `protocol, version, workspace, model, shell, mode, modes[{id,label}], role, roles[{id,label,summary}]` | session opened |
| `ready` | `mode, role` | idle, waiting for `prompt` |
| `note` | `kind` (`task`, `note`, `work`, `good`, `bad`), `text` | status line |
| `assistant` | `text` (markdown) | the model's words |
| `thinking` | `active`, `label` | spinner on/off while the model replies |
| `tool_start` | `name, args` | a tool began |
| `tool_output` | `text` | a chunk of live command output |
| `tool_done` | `name, args, ok, error, friendly, result` | a tool ended; `friendly` is a plain-words error |
| `active` | `name, count` | helper chats running for a `delegate` call |
| `approval` | `id, kind, summary, detail, risky, options[{value,label}]` | needs an `answer`; `kind` is `edit`, `command`, `network`, or `outside`; `detail` is a unified diff for edits |
| `plan_review` | `id, options[{value,label}]` | plan mode: the plan above is ready |
| `question` | `id, text, options[]` | the model asks; options may be empty (free text) |
| `cancel` | `id` | a request is no longer waiting (interrupt, or the GUI closed) |
| `diff` | `diff` | unified diff of what the task changed on disk |
| `undone` | `paths[]` | files restored by `undo` |
| `mode` | `mode, label` | the permission mode changed |
| `role` | `role, label, saved` | the role changed; the next task starts a new chat |
| `status` | `code, ok, reason` | the task ended: `COMPLETED`, `FAILED`, `BLOCKED`, `INTERRUPTED`, `STOPPED` |
| `error` | `message` | a message from the GUI was not understood, or a fatal error |
| `bye` | `code` | the process is about to exit |

## Messages (GUI → agent)

| type | fields | meaning |
|---|---|---|
| `prompt` | `text` | the next task; `/new`, `/mode X`, `/role X`, `/undo`, `/exit` typed as text also work |
| `answer` | `id, value, reason?` | answer to `approval`, `plan_review`, or `question` |
| `set_mode` | `mode` | `ask`, `edits`, `auto`, or `plan`; works mid-task |
| `set_role` | `role` | stored in `.bot/settings.json`; applies from the next task |
| `new_chat` | | start a fresh chat before the next task |
| `undo` | | restore files from the last task's checkpoint (while idle) |
| `interrupt` | | stop the running task, like Ctrl+C; ignored while idle |
| `shutdown` | | end the session after the current step |

### Answers

- `approval`: `value` is one of the offered options: `yes`, `always` (do not
  ask again for this key this session), `no` (with `reason`, which is passed
  to the model), or `auto` (yes, and switch to auto mode). A value that was
  not offered counts as `no`. A risky command offers only `yes` and `no`.
- `plan_review`: `auto`, `edits`, or `ask` start the work in that mode; `no`
  with a `reason` asks for a revised plan; `no` without one, or `stop`, keeps
  the plan and changes nothing.
- `question`: `value` is the answer text (an option, or free text). Empty
  means no answer.
