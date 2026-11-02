# crit for VS Code

A sidebar chat for **crit**, the local coding agent from critique-bot. crit
reads your project, proposes edits and commands, and asks before it changes
anything (unless you switch to a mode that does not ask). It talks to a web
chat model through its own Edge window, or to a local model.

Works in VS Code, VSCodium, and Cursor.

## Using it

1. Open a project folder.
2. Open the crit chat in the right side bar (*View → Open View… → crit*,
   or run **crit: Focus Layout**). On editors without a right side bar it
   appears in the Explorer.
3. Type a task. The first one starts the agent. With a web chat model, the
   agent opens its Edge window, and asks you to sign in the first time.

What you see:

- **Approvals.** Edits show as a diff; commands show the command line. Answer
  Yes, Yes and don't ask again, No (with what to do instead), or Yes and
  switch to auto mode. Risky commands (a push, `git reset --hard`, a broad
  delete) ask in every mode.
- **Plan mode.** crit reads and proposes a plan; you pick how to carry it out,
  or say what to change.
- **Changed files.** When a task ends, each changed file gets *Open diff*,
  which opens VS Code's own diff editor (before the task ↔ now).
- **Commands.** The commands crit runs, with live output, show in a
  read-only **crit** terminal.
- **Mode and role.** Pick them at the top of the chat or from the status bar.
  `Ctrl+Alt+C` (`Cmd+Alt+C`) stops a running task.

- **Focus layout.** *crit: Focus Layout* (the layout icon on the chat)
  gives a plain screen: files on the left, editor and diffs in the middle,
  the crit terminal below, the chat on the right; the menu bar (Alt shows
  it), status bar, activity bar, minimap and breadcrumbs are hidden.
  *crit: Exit Focus Layout* puts your previous settings back. It changes
  user settings of the current profile, so for a separate setup start
  VS Code with `code --profile crit`.

The chat box also takes `/new`, `/mode <ask|edits|auto|plan>`,
`/role <name>`, and `/undo`.

## Settings

| Setting | Default | |
|---|---|---|
| `crit.agentPath` | empty | Agent executable. Empty: the bundled agent, else `crit` on PATH. |
| `crit.configPath` | empty | `config.json`, relative to the workspace. Empty: crit finds it. |
| `crit.permissionMode` | empty | Mode at start. Empty: `.bot/settings.json` decides. |
| `crit.extraArgs` | `[]` | Extra agent arguments, e.g. `["--headed"]`, `["--logs"]`. |
| `crit.mirrorCommandsInTerminal` | `true` | The read-only crit terminal. |
| `crit.openDiffsAfterTask` | `false` | Open every changed file's diff when a task ends. |

The agent's log is in the **crit** output channel (*crit: Show Agent Log*).

## Building

```
npm install
npm run compile
npm test                              # unit tests
CRIT_TEST_PYTHON=/path/to/python npm test   # plus end-to-end tests against the real agent
```

`CRIT_TEST_PYTHON` needs critique-bot's dependencies; the end-to-end tests
use `test/fake_model.py`, a scripted local model, so no browser or real model
is involved.

### One download per OS

The extension carries the agent, frozen with PyInstaller, so users install
nothing else. Build on each OS you ship for:

```
PYTHON=/path/to/python npm run bundle-agent   # -> bin/critique-bot/ (needs pyinstaller)
npm run package:target                        # -> crit-<os>-<arch>-<version>.vsix
```

Without a bundled agent, `npm run package` makes a universal `.vsix` that
uses `crit` from PATH.

The extension and the agent speak a versioned JSON-lines protocol
(`docs/agent-protocol.md` in the repo). The extension refuses an agent with a
different protocol version, so ship both from the same commit.
