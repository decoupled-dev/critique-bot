# Extending crit

crit reads tools, MCP servers, agents, slash commands and hooks from files. Nothing needs a
code change. Every extension tool goes through the same tool registry as the built-in ones,
so it gets the same approvals, saved rules, output caps and transcript as `run_command`.

## Where the files go

```
~/.crit/                 your own (trusted)        .crit/ in a project (comes with the code)
  tools/*.toml|*.json      manifest tools            tools/*.toml|*.json
  agents/*.md              agents                    agents/*.md
  commands/*.md            slash commands            commands/*.md
  hooks.toml|hooks.json    hooks                     hooks.toml|hooks.json
  mcp.json                 MCP servers               mcp.json
  trust.json               your approvals            (never in the project)
  mcp-lock.json            pinned MCP tools          (never in the project)
```

When both scopes define the same name, the project's file wins, but only once it is active (see the next section).

## Trust: a cloned repository runs nothing by being opened

A project's tools, hooks and `mcp.json` stay **off** until you approve them:

```
$ crit trust                       # what the project has, and whether each file is on
off: not approved            /work/app/.crit/tools/adb_logcat.toml
    tool adb_logcat (read only, sandbox workspace-write, timeout 30s)
      runs: adb logcat -d -t '{lines|500}' '{filter}'
$ crit trust .crit/tools/adb_logcat.toml
$ crit trust --list                # every approval on this machine
$ crit trust --revoke .crit/tools/adb_logcat.toml
```

An approval covers the file's content (its sha256). If the file changes later, for example
after a `git pull`, it is off again until you approve it again. At session start crit lists
every file that is off and gives the command that turns it on.

Project agents and commands are prompts, so they load without approval. Until you approve
one, though, it cannot set a looser permission mode than `ask` (agents) or `ask`/`plan`
(commands). The model cannot write to any extension file or to `trust.json` /
`mcp-lock.json`, in either scope.

## Manifest tools

One file defines one tool. The tool runs as a single argv and **never through a shell**.

`~/.crit/tools/adb_logcat.toml` (TOML needs Python 3.11 or later; a `.json` file with the
same keys works everywhere):

```toml
name = "adb_logcat"
description = """Read the log of the connected Android device, filtered. Use for crashes, ANRs and
service errors on a device. Not for build logs (use the log tool) or for saved bugreports.
Output: the newest lines last, capped at about 4000 tokens."""
read_only = true            # true: runs without asking; false: asks like a command (also in auto mode)
timeout_s = 30
output_cap_tokens = 4000
sandbox = "workspace-write" # read-only | workspace-write | full (full always asks)
network = []                # hosts it reaches (recorded now; enforced by the Stage 4 proxy)
taint = "untrusted"         # its output is wrapped as untrusted and flags the session

[parameters]                # JSON Schema subset: type, properties, required, enum, minimum,
type = "object"             # maximum, minLength, maxLength, pattern, items, default
required = ["filter"]

[parameters.properties.filter]
type = "string"
description = "logcat filterspec, e.g. 'ActivityManager:I *:S'"

[parameters.properties.lines]
type = "integer"
minimum = 10
maximum = 5000

[run]
argv = ["adb", "logcat", "-d", "-t", "{lines|500}", "{filter}"]
# stdin = "json"            # also pass the arguments as one JSON object on stdin
# env = { LOG_FILTER = "{filter}" }
# allow_dash_values = true  # only if a value may legitimately start with "-"

[[examples]]                # what `crit tool test` runs
args = { filter = "*:E", lines = 50 }
expect_ok = true

[[examples]]
args = { filter = "--output=/tmp/x" }
expect_ok = false           # option-like values are refused
```

Templating rules:

- `{name}` or `{name|default}` must be a **whole** argv element (or a whole env value).
  `"--lines={lines}"` is an error; write `"--lines", "{lines}"`.
- Whatever the model passes, including `; rm -rf /` or `$(...)`, becomes exactly one argv element.
- A string value that starts with `-` is refused, so an argument cannot become an option
  (`--output=...`), unless you set `allow_dash_values = true`.
- An optional argument with no value and no default drops its element.
- The program runs in the workspace with your environment minus secrets (tokens and keys
  are removed, as for `run_command`). Output is capped, and secrets in it are redacted.

Check and try a tool:

```
$ crit tool list
$ crit tool show adb_logcat      # what the model sees, the argv, and the description lint
$ crit tool test adb_logcat      # schema check, the [[examples]], output cap and timeout
$ crit tool test adb_logcat --args '{"filter": "CarService:V *:S"}'
```

The description lint wants three things: a **purpose** sentence, a **not-for** sentence
(when to use something else), and the **output** (what comes back). Small models pick tools
by their descriptions.

In a session the tool shows up in `/tools`, and the model calls it like any other tool.

## MCP servers

`~/.crit/mcp.json` has the same shape as Claude Code's `mcpServers`, so you can copy yours:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/specs"],
      "read_only_tools": ["read_file", "list_directory", "search_files"]
    },
    "git": {
      "command": "uvx",
      "args": ["mcp-server-git", "--repository", "."]
    },
    "jira": {
      "type": "http",
      "url": "https://mcp.example.com/jira/mcp",
      "headers": {"Authorization": "Bearer <token>"},
      "timeout_s": 60
    }
  }
}
```

- **Transports:** stdio (`command` and `args`, optionally `env` and `cwd`) and streamable
  HTTP (`"type": "http"`, `url`, `headers`). crit uses its own small JSON-RPC client, so
  no extra package is needed. With `"offline": true` in the settings, HTTP servers are skipped.
- **Names:** tools appear as `mcp__<server>__<tool>`, for example `mcp__git__git_log`.
- **Approvals:** each call asks like a command and shows the full arguments. Tools you list
  in `read_only_tools` run without asking. The server's own `readOnlyHint` is only a claim,
  so crit does not trust it.
- **Pinning:** the first time crit sees a tool, it stores a sha256 of the tool's name, description and
  input schema in `~/.crit/mcp-lock.json`. If the server later describes the tool
  differently, the tool is left out of the session until you look at the change:

  ```
  $ crit mcp list                  # servers, tools, and which ones changed
  $ crit mcp approve git           # shows the diff of each changed tool, then pins it
  ```
- **Untrusted output:** results come back inside `<untrusted source="mcp:git/git_log">…</untrusted>`
  and carry a taint flag that the Stage 4 taint tracking reads. Text inside a result cannot
  close that block.
- **Many tools:** when the session has more than `max_loaded_tools` tools in total (default
  20, set in `.crit/settings.json`), MCP tools are listed only by name and one line, and a
  `tool_search` tool returns their full schemas. Because crit's built-in tools already
  number more than 20, this is the usual case. The tool list is fixed at session start and
  never changes during the session, which keeps the model server's prompt cache valid.
- **IDEs:** the MCP servers an ACP client (Android Studio, Zed) passes in `session/new`
  are connected the same way.

A project's `.crit/mcp.json` needs `crit trust .crit/mcp.json` first.

## Agents

An agent is a profile: instructions, a tool allowlist, limits. The former roles are now
built-in agents (`code-reviewer`, `aosp-expert`, `expert-developer`, `ai-expert`,
`test-engineer`, `android-app-engineer`, `generic`), and `/role` is an alias of `/agent`.

`.crit/agents/reviewer.md`:

```markdown
---
name: reviewer
description: Reviews the current diff for defects and missing tests; never edits.
mode: read-only                 # ask | edits | auto | plan | read-only
tools: [read_files, search_code, list_files, find_files, git_diff, git_log, git_show, log]
deny_tools: [web_fetch]
skills: [aosp]
max_steps: 30                   # its tool rounds; the last one has no tools, so it summarizes
max_depth: 0
max_parallel: 2                 # copies one delegate call may start
isolation: none                 # agents that can write always get a worktree
output: summary
budget: {tokens: 200000, minutes: 20}
---
You review code. Read the diff with git_diff, then the callers and tests of what changed.
Report findings by severity with file:line, what goes wrong, and the smallest fix.
```

Use an agent in two ways:

- **As the main agent:** run `/agent reviewer`. The next task starts a new conversation
  with the agent's instructions, and the choice is saved in `.crit/settings.json`.
  `/agent` with no name lists the agents.
- **As a tool:** the model calls `delegate` with an agent:
  `{"tasks": [{"agent": "reviewer", "brief": "Review the change to CarPowerManager.java"}]}`
  (or `{"agent": ..., "task": ..., "scope": [...]}`). The agent runs in a fresh chat with
  its own instructions, and only a summary of about 2K tokens comes back. Its allowlist is
  enforced: a call outside `tools`, or one in `deny_tools`, is refused. An agent that
  can write works in a throwaway **git worktree**, which starts from HEAD plus your
  uncommitted changes. Its changes come back as a patch, and the main agent reviews the
  patch and applies it with `apply_patch`. Two agents never write to the same files.

Not applied yet: `model` (an agent runs on the session's model), `budget`, `sandbox`,
`flow`. crit reads these fields now so that the engine and sandbox stages can use them.
Helpers cannot delegate, so a delegated agent's depth is always 1.

## Slash commands

`.crit/commands/fix-issue.md` becomes `/fix-issue`:

```markdown
---
description: Fix a Jira issue end to end
agent: expert-developer
mode: plan
arguments: [issue]
---
Fix $issue. Read it with the jira tool, reproduce the bug with a failing test, fix it with
the smallest change, run the module's tests, and summarize what changed.
Extra notes from the command line: $ARGUMENTS
```

`/fix-issue CAR-1234` sends the filled-in template as the task. In the template, `$ARGUMENTS`
is everything typed after the command, `$1` to `$9` are the individual words, and `$issue`
is the word for a named argument. If the template uses none of these, what you typed is
appended to it. The command's `agent` and `mode` switch first, as `/agent` and `/mode` do.
The built-in commands (`/help`, `/mode`, and so on) cannot be replaced, and `/help` lists
your commands.

## Hooks

`~/.crit/hooks.toml`:

```toml
[[pre_tool]]
matcher = "run_command"                       # a regular expression on the tool name
argv = ["python3", "/home/me/.crit/hooks/no_force_push.py"]
timeout_s = 10

[[post_tool]]
matcher = "edit_file|write_files"
argv = ["ruff", "format", "--quiet", "."]

[[task_finished]]
argv = ["notify-send", "crit", "task finished"]
```

`/home/me/.crit/hooks/no_force_push.py`:

```python
import json, re, sys

event = json.load(sys.stdin)
command = str(event.get("arguments", {}).get("command", ""))
if re.search(r"\bgit\s+push\b.*(--force\b|-f\b|--force-with-lease\b)", command):
    print(json.dumps({"decision": "deny", "message": "force push is not allowed by team policy"}))
else:
    print(json.dumps({"decision": "allow"}))
```

The events are `session_start`, `user_prompt`, `pre_tool`, `post_tool`, `pre_compact` and
`task_finished`. A hook gets the event as JSON on stdin (`event`, `workspace`, and then
`tool`, `arguments` and `permission` for pre_tool; `result` for post_tool; `prompt`;
`task`, `status` and `changed`; `reason`). It may print one JSON object:

| key | effect |
|---|---|
| `"decision": "allow"` | go on (no output at all, with exit 0, means the same) |
| `"decision": "deny"` | pre_tool: the call does not run and the model is told why. user_prompt: the task does not start |
| `"decision": "ask"` | pre_tool: the call asks you, even in auto mode |
| `"message"` | the reason, shown to you and to the model |
| `"append_context"` | text added for the model (session_start: in the project notes; user_prompt: with the task; post_tool: after the tool's output) |

A hook that times out, cannot start, or prints something other than JSON counts as
**ask**, because a broken policy hook must not let calls through. When several hooks run
for one event, deny beats ask, ask beats allow, and their `append_context` texts are joined.
Project hooks (`.crit/hooks.toml`) need `crit trust` first.

## What is not built yet

- **A sandbox for extension processes.** Manifest tools, hooks and stdio MCP servers start
  through one function, `extensions.runner.run_argv` (`use_runner` replaces it), and Stage 4
  plugs its OS sandbox and egress proxy in there. Today they run with your user's rights,
  without secrets in their environment, and `network`/`sandbox` are recorded, not enforced.
- **The headless agent service** (`crit serve --agent X`, `POST /v1/agents/X/runs`) comes
  with the Stage 5 server. It resolves the agent with `extensions.agent_files.find_agent` and runs it
  as the role of a `run_agent` session.
