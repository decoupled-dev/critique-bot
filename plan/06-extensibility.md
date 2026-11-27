# Stage 6: Extensibility (bring your own tools, MCP, file-defined agents, commands, hooks)

## Goal
Let users add tools, agents, commands and hooks with files, not code changes, and run any
agent headless as a service. Every extension goes through the same registry, permissions,
sandbox, taint and audit as the built-ins.

## Why
- Consolidated, well-described CLI-like tools are what Anthropic and OpenAI recommend.
  Manifest tools are cheap, sandboxable, and keep the tool boundary narrow.
- MCP is the de facto standard, so supporting it is needed to match opencode. But
  poisoning, rug pulls and output injection are documented attacks, and more tools hurt small
  models: defer-load schemas. Changing the tool list mid-session breaks the cache.
- Every major CLI defines agents in files (Claude Code, opencode): tool allowlist, model,
  permissions, step cap, depth/concurrency, isolation, summary return. Writer orchestration
  (several agents writing the same code) fails (MAST, CooperBench −30%). So agents are
  *profiles*, and the default way one agent uses another is as a tool.

## Scope
Manifest tools; MCP client (stdio + streamable HTTP); file-defined agents (roles migrate);
custom slash commands; hooks; headless agent service.

Out of scope: Python plugins (Stage 9), WASM (not planned), an MCP *server* (small, optional
at the end: `crit mcp serve` exposing read-only tools via `ToolSpec.to_mcp()`).

## Design

### Where extensions live
```
~/.crit/                 user scope        .crit/ (repo)              project scope
  tools/*.toml             manifest tools     tools/*.toml
  agents/*.md              agents             agents/*.md
  commands/*.md            slash commands     commands/*.md
  hooks.toml               hooks              hooks.toml
  mcp.json                 MCP servers        mcp.json
  mcp-lock.json            pinned hashes      (never committed by crit; ignored)
```
Project-scope files that **grant** power (tools that write, hooks, MCP servers) are
inactive until the user approves them once per content hash (`crit trust .crit/tools/x.toml`),
because a cloned repo must not run code just by being opened. This extends today's
`SECURITY_KEYS` rule.

### Manifest tools (`tools_manifest.py`, tools layer)
```toml
name = "adb_logcat"
description = """Read the device log, filtered. Use for crashes and ANRs on a connected device.
Not for build logs (use the log tool). Output: newest lines last, capped."""
read_only = true
timeout_s = 30
output_cap_tokens = 4000
sandbox = "workspace-write"     # or read-only / full (full requires approval)
network = []                    # extra hosts this tool may reach via the proxy
taint = "untrusted"             # its output taints the session

[parameters]                    # JSON Schema (subset)
type = "object"
required = ["filter"]
[parameters.properties.filter]
type = "string"
description = "logcat filterspec, e.g. 'ActivityManager:I *:S'"
[parameters.properties.lines]
type = "integer"
minimum = 10
maximum = 5000

[run]
argv = ["adb", "logcat", "-d", "-t", "{lines|500}", "{filter}"]   # no shell; each {x} is one argv element
# or: stdin = "json"  (arguments as JSON on stdin), env = {"FILTER" = "{filter}"}
```
- Never runs through a shell; templating can only fill whole argv elements.
- `crit tool list | show | test <name> [--args JSON]`: test validates the schema, runs the
  examples given in `[[examples]]`, and checks the output cap and the timeout.
- Description lint: required sections (purpose, not-for, output), like Stage 2's checklist.

### MCP client (`mcp_client.py`, tools layer)
- Transports: stdio and streamable HTTP. JSON-RPC 2.0, our own small client or the official
  `mcp` SDK as an optional extra; decide by size.
- Config `mcp.json` (same shape as Claude Code's `mcpServers`, so users can copy theirs).
- **Pinning:** on first connect, store a sha256 of each tool's name + description + schema in
  `mcp-lock.json`. On a change, show a diff and ask; keep the old version until approved
  (protects against rug pulls).
- **Prefixing:** tools appear as `mcp__<server>__<tool>` (namespacing, Anthropic advice).
- **Deferred schemas:** when total tools > `max_loaded_tools` (default 20 for local
  profiles), MCP tools are listed only by name + one line, and a `tool_search` meta-tool loads
  full schemas. The loaded set is decided at session start (cache-stable); loading more
  mid-session appends them as a message, never reorders the declared list.
- **Untrusted output:** wrapped as `<untrusted source="mcp:server/tool">…</untrusted>` and
  taints the session (Stage 4).
- Approval prompts show full arguments.
- ACP clients that pass `mcpServers` get them connected through the same path.

### File-defined agents (`agents.py`, engine layer)
```markdown
---
name: aosp-debugger
description: Diagnoses AOSP crashes and build breaks from logs; proposes minimal fixes.
model: profile:qwen3.6-27b          # or a provider/model id
mode: ask                            # ask | plan | auto | read-only
tools: [read_files, search_code, log, run_command, edit_file, adb_logcat, git_blame]
deny_tools: [web_fetch]
skills: [aosp, android-debugging]
max_steps: 80
max_depth: 1                         # may delegate one level
max_parallel: 3
isolation: none                      # none | worktree (required for writers it starts)
output: summary                      # summary | json:<schema-file> | review
budget: {tokens: 400000, minutes: 45}
sandbox: aosp
---
You are ... (the agent's system-prompt body)
```
- The roles in `roles.py` become built-in agent files under `src/critique_bot/agents/`
  (code-reviewer, aosp-expert, expert-developer, …). `/role` becomes `/agent`, with `/role`
  kept as an alias.
- `delegate(agent=..., task=..., scope=...)` runs another agent as a tool: fresh context, a
  task template (objective, output format, scope, tools), summary return (≤ 2K tokens).
  Writers get `isolation: worktree` forced, and their diff comes back for the parent to apply.
- On the last allowed step, tools are removed so the agent must summarize (opencode's trick).
- Plan mode is enforced through permissions, not the prompt (Stage 5 `modes.py`).

### Headless agent service
`crit serve --agent aosp-debugger` exposes `POST /v1/agents/{name}/runs` (task in, events
or final JSON out) on the Stage 5 server. Same auth, sandbox and audit. This is what gets
you "Bedrock/AgentCore-like" without the cloud: any agent file becomes an API.

### Slash commands (`commands.py`, apps)
`.crit/commands/fix-issue.md` with frontmatter (`description`, `agent`, `mode`,
`arguments`) and a body template (`$ARGUMENTS`, `$1`). Example: `/fix-issue 1234`.

### Hooks (`hooks.py`, engine)
`hooks.toml` events: `session_start`, `user_prompt`, `pre_tool`, `post_tool`, `pre_compact`,
`task_finished`. Each hook is an argv run under the sandbox, with event JSON on stdin and
JSON on stdout (`{"decision": "allow|deny|ask", "message": "...", "append_context": "..."}`),
plus a timeout. Uses: a team policy (deny `git push --force`), formatting after edits, and
notifications.

## Work items
1. Extension discovery + the trust store (hash approval) + `crit trust`.
2. Manifest tools: parser, schema subset validation, argv templating, sandboxed run,
   `crit tool` commands, description lint.
3. MCP client: transports, pinning, prefixing, deferred schemas + `tool_search`, untrusted
   wrapping, approvals; ACP `mcpServers` hookup.
4. Agents: file format, loader, built-in agents from roles, `/agent`, `delegate` with
   templates, worktree isolation, last-step summary, budgets.
5. Headless agent runs on the server.
6. Slash commands.
7. Hooks.
8. Docs: `docs/extending.md` with complete examples (an adb tool, a Jira MCP, a reviewer agent,
   a fix-issue command, a deny-force-push hook).
9. Eval: suite with a 40-tool MCP set loaded (a distraction test) vs none; record in
   `plan/results/06/`.

## Tests
- Manifest: injection attempts in arguments (`; rm -rf`, `$(…)`) stay single argv elements.
- Trust: a repo with a new `.crit/tools/x.toml` does nothing until approved; editing it
  after approval requires re-approval.
- MCP: a fake server changes a description → blocked until approved; a poisoned output →
  session tainted, and an egress attempt is denied in auto mode.
- Agents: a tool not in the allowlist → refused; depth and parallel caps enforced; a writer
  sub-agent works in a worktree and its diff comes back.
- Hooks: a deny decision stops the tool call; a timeout → ask.

## Completion targets
- [ ] A user can add a tool with one TOML file, test it with `crit tool test`, and see it in
      `/tools`, with no code changes
- [ ] MCP: works with at least 3 public servers (filesystem, git, one HTTP); pinning and
      taint verified by tests
- [ ] With 40 MCP tools configured, suite pass@1 drops ≤ 2 points against none (deferred
      schemas working)
- [ ] All former roles exist as agent files; `/role` still works
- [ ] `crit serve --agent X` + `POST /v1/agents/X/runs` documented and tested
- [ ] Every extension type goes through permission, sandbox and audit (test per type)

## Risks
- **Tool sprawl hurts small models:** default caps, deferred schemas, and per-agent allowlists.
- **Trust UX friction:** a single approval per content hash, with a readable summary of what
  each file grants.
