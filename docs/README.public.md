<!-- DRAFT of the public README (plan/10, item 5). It replaces README.md at launch.
     TODO(decision: name): the project/package name, OWNER/REPO and the install command.
     TODO: the comparison table, only with measured numbers from docs/comparison.md. -->

# crit

**A local-first coding agent.** crit drives the model you run yourself (llama.cpp, vLLM,
SGLang, Ollama, LM Studio and other OpenAI-compatible servers) through native tool calls,
with per-model profiles instead of per-model code. The agent's commands run inside an OS
sandbox (bubblewrap on Linux, `sandbox-exec` on macOS) with an egress proxy, so a
repository or a web page cannot quietly send your code or keys anywhere. Changes are
checked before a task counts as done: edits go through a match ladder and a parse/lint
guard, and your check command runs after an edit. It reviews patches too, and it is built
to work in very large trees such as AOSP. Nothing leaves your machine unless your
configuration allows it.

## Quickstart (about a minute, with a model server already running)

1. **Install** (Python 3.10+):

   ```bash
   pipx install critique-bot            # TODO(decision: name): the published package name
   # or, from a checkout:
   python3 -m venv .venv && . .venv/bin/activate
   pip install -c constraints.txt -e .
   ```

2. **Point crit at your model server.** Any one of these:

   ```bash
   # Ollama (set OLLAMA_CONTEXT_LENGTH=32768 before `ollama serve`; the default is too small)
   crit config --backend local --local-url http://127.0.0.1:11434 --model qwen3:8b

   # llama.cpp: llama-server -m MODEL.gguf --jinja -c 32768 --port 8080
   crit config --backend local --local-url http://127.0.0.1:8080 --model MODEL

   # vLLM: vllm serve MODEL --enable-auto-tool-choice --tool-call-parser <parser> --port 8000
   crit config --backend local --local-url http://127.0.0.1:8000 --model MODEL
   ```

3. **Check the server**, then **run a task** in your project:

   ```bash
   crit doctor model                 # context size, native tool calls, the launch line to use
   crit doctor sandbox               # can commands run in the OS sandbox here?
   cd your-project
   crit "fix the failing test"
   ```

crit asks before edits and commands (Shift+Tab switches between ask, accept-edits, auto and
plan modes); `crit undo` restores the files the last task changed.

Launch lines for each server, model profiles and how to tune them:
[docs/local-models.md](local-models.md).

## How it compares

TODO: a table with **measured** numbers only, from one documented `crit eval` command, the
same model and server for both agents, and the opencode version named. Method and template:
[docs/comparison.md](comparison.md).

## Security model, in short

- **Sandbox:** commands can write the project and build caches only, cannot read `~/.ssh`,
  `~/.aws`, `.env` files or crit's own settings, and have no direct network.
- **Egress:** the only way out is crit's proxy, which allows the model server and the hosts
  you list; every request lands in the audit log (`crit audit`).
- **Taint:** after crit reads untrusted content (a fetched page, a file outside the project,
  a device log), auto mode stops sending anything out until you say so.
- **Trust:** tools, hooks and MCP servers that come with a cloned repository do nothing
  until you approve them with `crit trust`.

Details and limits: [docs/security.md](security.md). Reporting a vulnerability:
[SECURITY.md](../SECURITY.md).

## More

- [Install options](install.md) (release zip, .deb, setup scripts)
- [The agent](agent.md): screen, modes, approvals, skills
- [Extending](extending.md): manifest tools, MCP, agents, commands, hooks
- [IDEs via ACP](acp.md): Android Studio, JetBrains IDEs, Zed
- [Review in CI](gitlab-ci.md)
- [The eval harness](eval.md)
- [Architecture](architecture.md) and [contributing](../CONTRIBUTING.md)

## License

Apache License 2.0. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
