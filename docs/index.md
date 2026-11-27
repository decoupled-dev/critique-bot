# crit

crit is a local-first coding agent. It runs a model you serve yourself (llama.cpp, vLLM,
SGLang, Ollama, LM Studio, TGI, MLX) through native tool calls, runs the agent's commands
inside an OS sandbox, and checks its own changes before it calls a task done. It also
reviews patches, and it knows its way around large Android and AOSP trees.

Nothing leaves your machine unless your configuration allows it.

## Start here

| I want to … | Read |
| --- | --- |
| install crit | [Install](install.md) |
| point it at my model server and check the server | [Local models and profiles](local-models.md) |
| learn the agent's screen, modes and approvals | [The agent](agent.md) |
| understand what crit may touch and reach | [Sandbox, egress and taint](security.md) |
| add tools, MCP servers, agents, commands or hooks | [Extending](extending.md) |
| use it from Android Studio, JetBrains IDEs or Zed | [IDEs (ACP)](acp.md) |
| review merge requests in CI | [Review in GitLab CI](gitlab-ci.md) |
| measure crit, or compare it with another agent | [The eval harness](eval.md), [crit vs opencode](comparison.md) |
| change crit itself | [Architecture](architecture.md), [Contributing](contributing.md) |

## What makes it different

- **Built for local models.** Model profiles and server presets carry each family's
  tool-call format, sampling and context limits, so the engine has no per-model code.
  `crit doctor model` probes a server and tells you whether native tool calls work.
- **A real sandbox.** On Linux (bubblewrap) and macOS (`sandbox-exec`) the agent's commands
  can write only the project and build caches, cannot read your keys, and reach the
  network only through crit's egress proxy. Untrusted content (a fetched page, a device
  log) switches off the "send data out" paths in auto mode.
- **Verified changes.** Edits go through a match ladder and a parse/lint guard; a
  configured check command runs before a task counts as done.
- **Extensible without code.** Manifest tools, MCP servers, file-defined agents, slash
  commands and hooks, all through one tool registry with the same approvals, and nothing
  from a cloned repository runs until you `crit trust` it.
- **Measured.** `crit eval` runs a suite of real tasks with a fixed model and reports
  pass@1, pass^k, tokens and tool errors; every harness change is judged by it.

## Not built yet

Pages planned for the public docs and not written yet: the headless server API (from the
OpenAPI spec, Stage 5) and the AOSP pack (Stage 9). See the [roadmap](https://github.com/OWNER/REPO/tree/main/plan).
<!-- TODO(decision: name): replace OWNER/REPO with the public repository. -->
