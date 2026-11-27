# Internal security review (2026-11-27)

Three adversarial reviewers who had not written the code attacked crit on this machine
(real bubblewrap 0.11.1, fake home folders, loopback-only exfiltration). Each confirmed
finding got a test asserting the secure behaviour; each test now passes and stays in
`tests/security/` as a regression guard. This is an internal review: it hardens crit
before an independent audit, it does not replace one.

| Report | Findings | High | Status |
|---|---|---|---|
| [sandbox-egress.md](sandbox-egress.md) | SB-1..3 | 2 | fixed (merge 5477fbf) |
| [permissions-injection.md](permissions-injection.md) | PM-1..15 | 4 | fixed (merges 0c37dac, 5477fbf, 9b47c3b) |
| [extensions-server-supply.md](extensions-server-supply.md) | SF-1..13 | 3 | fixed (merges 5477fbf, 9b47c3b); SF-12, SF-13 partly |

31 findings: 8 high, 15 medium/medium-high, 8 low/info. 0 open as tests.

## What held up when attacked
Filesystem isolation and the empty network namespace; the egress allow-list (suffix,
case, trailing dot, IPv4-mapped IPv6, decimal/octal literals, DNS rebinding,
link-local/metadata); `.git/config` and hooks read-only in the sandbox; `crit serve`
authentication (constant-time token check, path variants, no CORS, 0600 files);
command chaining in allow rules (`;`, `&&`, `|`, `$()`, backticks, redirects);
secret-file reads through crit's tools; MCP transport (no redirects, bounded sizes);
data at rest redacted in 0700 folders; CI permissions and SHA-pinned actions.

## Accepted or remaining risk
- `run_command git diff` / `git log -p` can show a tracked secret file (redaction only);
  a tracked-file check per command is too slow on AOSP trees.
- `git commit` is not treated as running project code (commit hooks).
- Poisoned cached artifacts (jars, wheels, go objects) in writable build caches;
  `"sandbox": "read-only"` avoids it.
- A `.env` created after the session starts is not masked by the sandbox (crit's own
  tools still ask before reading it).
- SF-12: no detached release signature yet; installers verify GitHub build provenance
  when `gh` is available. SF-13: `constraints.txt` has no hashes (pip's hash mode refuses
  the editable install); CI tool installs are hash-pinned.
- macOS sandbox profile changes are unit-tested only, not run on a Mac.
- Approvals do not cover modules a covered script imports, or packages `uvx`/`npx` fetch.
- Prompt injection through files the model may edit (AGENTS.md, .crit/AGENT.md) and
  Windows shell parsing were out of scope.

## Before calling crit secure
An independent audit and penetration test, a first green CI run on all platforms, a
real-Mac check of the sandbox profile, and gitleaks/trufflehog on the history to publish.
