# Security policy

crit runs a language model's tool calls on your machine, so its security boundary matters
as much as its features. Thank you for reporting problems responsibly.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Report it privately through
GitHub's security advisories: on the repository page, **Security → Report a
vulnerability** ([how it works](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).

Please include the crit version (`pip show critique-bot`, the release file, or the commit), the OS, the output of
`crit doctor sandbox`, the configuration that matters (with secrets removed), and the
smallest steps or repository that reproduce it.

<!-- TODO before launch: confirm these targets with the maintainers. -->
What to expect:

- an acknowledgement within **3 working days**;
- a first assessment (accepted, needs more information, or not a vulnerability) within
  **10 working days**;
- a fix or mitigation for accepted reports, released as soon as it is ready, normally
  within **90 days**; we agree a disclosure date with you and credit you in the advisory
  unless you prefer not to be named.

Only the latest release gets security fixes before 2.0; after 2.0, the latest minor
release of the current major version does.

## In scope

The threat model is: **the model and everything it reads are untrusted.** A repository you
clone, a file, a web page, a log, an MCP server's output or a model gone wrong may try to
make crit act against you. crit's job is to keep that contained. These count as
vulnerabilities:

- **Sandbox escape.** A command the agent runs inside the OS sandbox (bubblewrap on Linux,
  `sandbox-exec` on macOS) writes outside the allowed folders, reads a masked secret
  (`~/.ssh`, `~/.aws`, `~/.crit`, the project's `.env` files, ...), outlives crit, or
  rewrites crit's own rules (`.crit/settings.json`, `.git/hooks`, `.git/config`).
- **Egress bypass.** A sandboxed command, a tool, or crit itself reaches a host the egress
  policy does not allow (strict: only the model server and `allowed_hosts`), reaches cloud
  metadata or loopback ports that are not forwarded, or leaks data through DNS.
- **Taint bypass.** After untrusted content enters the session (a fetched page, an outside
  file, a device log, an untrusted workspace), auto mode still performs a network call, a
  push, or a write outside the project without asking.
- **Permission bypass.** A tool call runs without the approval the current mode requires;
  a cloned repository's `.crit/` files, hooks, manifest tools or MCP config take effect
  without `crit trust`; a committed config loosens the user's settings.
- **Secret exposure.** A secret (API key, token, password, private key) reaches the model,
  a log, a transcript, the audit log or a posted review comment unredacted, or an API key
  is sent to a host other than the configured server.
- **Injection into integrations.** Model output that makes crit run GitLab quick actions,
  mention users, or follow a token-carrying redirect.

## Out of scope

- What the model says, or a wrong edit inside the project in a mode where you approved it.
- Anything that needs the sandbox turned off (`"sandbox": "full"`), or Windows, where there is
  no OS sandbox yet (see [`docs/windows-lockdown.md`](docs/windows-lockdown.md)).
- The project's own build and test code doing what it does inside the sandbox (writing the
  project, reaching an allowed host).
- Limits documented in [`docs/security.md`](docs/security.md#what-this-cannot-guarantee),
  such as the lack of process or memory limits.
- Vulnerabilities in a dependency with no crit-specific impact: report those upstream.
  We still want to hear if crit's pinned version needs a bump.
- The optional web-chat backend's interaction with a third-party site, beyond crit's own
  redaction and filtering.

## How crit defends, in short

1. **OS sandbox.** Agent commands run in bubblewrap (Linux) or `sandbox-exec` (macOS):
   read-only system, writable project and caches, masked secrets, empty network namespace,
   own PID namespace. `crit doctor sandbox` runs real escape checks.
2. **Egress proxy.** The only network path out of the sandbox: per-host allow-list, ports
   80/443, no link-local, DNS-rebinding checks, and every request in the audit log.
3. **Taint.** Untrusted content in the session removes the "send data out" leg in auto mode
   until you lift it with `/trust`.
4. **Trust store and permissions.** Project extensions run nothing until approved by
   content hash; risky steps ask; the model cannot edit crit's own rules.
5. **Redaction and audit.** Secrets and configured names are redacted from everything sent
   and logged; `~/.crit/logs/audit.jsonl` records commands, writes, network requests and
   permission decisions (`crit audit`).

The full description, with what is verified and what is not, is in
[`docs/security.md`](docs/security.md).
