# crit desktop

A window for running several crit agents at once and watching what each one
is doing.

- **Agents list** (left): every agent with a live status: working, *needs
  you* (an approval, a question, or a plan to review), done, failed, stopped.
  What it is doing right now, and how long the task has run.
- **All agents**: one card per agent: task, current step, branch, files
  changed. Agents that need you come first.
- **Agent view**: the chat with that agent (approve edits and commands, answer
  questions, send the next task), its changed files with diffs, and a
  read-only terminal with the commands it ran.
- **Notifications** when an agent needs you or finishes while the window is in
  the background; the window title shows how many are waiting.

## Agents side by side

*New agent* (Ctrl+N) takes a task, a name, and a permission mode. By default
each agent gets **its own copy of the project**: a git worktree at
`<project>.crit/<name>` on branch `crit/<name>`, starting from the last commit.
Agents then never overwrite each other's edits. When one is done:

- **Apply to project** copies its changes into the project folder, uncommitted.
  If they no longer apply cleanly, git's three-way merge leaves conflict markers.
- **Delete** removes the agent, its folder and its branch.
- Or work with the branch yourself (`git merge crit/<name>`).

Untick *Own copy* to let an agent work in the project folder itself (also the
only choice outside a git repository).

## Browser

For a web chat model, the app starts **one shared Edge window** per project
(`critique-bot desktop-host`) and every agent works in **its own tab** of it.
Sign in once in that window. With a local model no browser is started.
`allowed_hosts` and the network filter apply as in the CLI.

## Run it

```
npm install
npm start -- /path/to/project          # or open the project from the window
```

On Windows, `scripts\setup-crit.ps1 -Desktop` does the install for you (through a proxy with `-ProxyHost`, `-ProxyPort`, and `-ProxyUser`) and points the app at the agent it installed. Then run `npm start` here.

On Ubuntu 24.04+ an Electron run from `node_modules` aborts with "The SUID
sandbox helper binary was found, but is not configured correctly". Either
`sudo chown root node_modules/electron/dist/chrome-sandbox && sudo chmod 4755
node_modules/electron/dist/chrome-sandbox`, or for development add
`--no-sandbox` (`npm start -- --no-sandbox /path/to/project`). The packaged
deb/AppImage does not need either.

The app runs, in order: `CRIT_COMMAND` (a path, or a JSON array such as
`["/venv/bin/python","-m","critique_bot"]`), `"command"` in the app's
`settings.json`, the agent bundled in `bin/critique-bot/`, then `critique-bot`
on PATH. In a source checkout:

```
CRIT_COMMAND='["/path/to/venv/bin/python","-m","critique_bot"]' PYTHONPATH=../src npm start -- ..
```

App settings live in `settings.json` in Electron's user-data folder
(`~/.config/crit/` on Linux): `command`, and `headedBrowser` (default true).
The list of projects and agents is kept there too; after a restart the agents
are listed as stopped, and a new task starts one again in its folder.

## Tests

```
npm test                                               # status and git units
CRIT_TEST_PYTHON=/path/to/python npm run selftest -- [screenshot-dir]
```

The self-test starts the real app (under `xvfb-run` on Linux) with two agents
in their own worktrees against `../vscode-extension/test/fake_model.py`: one in
auto mode, one that waits for an approval. It then applies one agent's change
to the project and deletes both.

## Package (one download per OS)

```
PYTHON=/path/to/python npm run bundle-agent   # freezes the agent into bin/critique-bot/
npm run dist                                  # AppImage + deb on Linux, NSIS on Windows, dmg on macOS
```

Build on each OS you ship for. The chat view is shared with the VS Code
extension (`../vscode-extension/media/chat.js`, copied in at build time).
