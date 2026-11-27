# Security review: OS sandbox and egress controls

Adversarial review of crit's Linux (bubblewrap) sandbox, the egress proxy, environment
scrubbing and the masked-path list, against the claims in `docs/security.md`. The real Linux
path was exercised (bwrap 0.11.1, unprivileged user namespaces working). Every probe ran in
throwaway directories with a fake `HOME`/`CRIT_HOME`; all planted secrets are fake.

## Findings

| ID | Title | Severity | Affected | Preconditions | Impact | Repro test | Suggested fix |
|----|-------|----------|----------|---------------|--------|------------|---------------|
| SB-1 | **Fixed** (wip/fix-contain: `secret_paths.HOME_CREDENTIALS`, `masked`/`unmask` settings). Common credential files not in the mask list | **High** — direct secret read, no extra bug needed | `sandbox/__init__.py:63-68` (`SECRET_HOME_PATHS`); same list drives `macos.py:74` | workspace-write (default) or read-only; any sandboxed command (model output, repo build step, MCP server). Exfil needs `open` egress or the token quoted into chat | Reads `~/.npmrc` (npm publish token), `~/.cargo/credentials.toml` (crates.io), `~/.gem/credentials`, `~/.config/composer/auth.json`, `~/.m2/settings.xml` (repo passwords) — none masked, all readable | `test_sandbox_secret_files.py::test_common_credential_files_are_masked`, `::test_npmrc_token_is_not_readable` (real bwrap) | Add these (and `~/.boto`, `~/.s3cfg`, `~/.config/{op,sops,doctl,helm}`, `~/.databricks`) to `SECRET_HOME_PATHS` |
| SB-2 | **Fixed** (wip/fix-contain: `CACHE_AUTORUN` read-only; `~/.gradle`/`~/.m2` top folders read-only, sub-folders writable; cached artifacts remain a documented residual). Writable build caches are a code-persistence vector | **High** — arbitrary code execution later, outside the sandbox | `sandbox/__init__.py:52` (`DEFAULT_CACHES`), writable via `build_policy` 285-300; no entry in `PROTECTED_WORKSPACE` | workspace-write (default); a later Gradle run by the user or in `full` mode | A sandboxed command plants `~/.gradle/init.gradle` or `~/.gradle/init.d/*.gradle`, auto-run as Groovy by the next Gradle invocation outside the sandbox — the exact "plant something that runs later" the `.git/hooks` protection is meant to stop. `~/.m2/settings.xml` similarly reroutes dependency downloads | `test_sandbox_cache_persistence.py::test_gradle_init_scripts_cannot_be_planted`, `::test_planted_gradle_init_does_not_persist` (real bwrap) | Treat auto-run files in writable caches as protected (read-only ro-binds): `~/.gradle/init.gradle`, `~/.gradle/init.d`, `~/.gradle/gradle.properties`, `~/.m2/settings.xml`; or mount caches so only their sub-trees used for artifacts are writable |
| SB-3 | **Fixed** (wip/fix-contain: unbounded-depth breadth-first scan, 200k dirs / 5 s budget; an incomplete scan is announced and audited, `masked` covers the rest). `.env` files below two levels are left readable | Medium — in-workspace secret reaches the chat | `sandbox/__init__.py:201-227` (`_env_files`: `depth < 2`, `limit_dirs=400`) | workspace-write/read-only, `env_files` not true; a `.env` nested 3+ deep (monorepo), or any `.env` after 400 dirs scanned (e.g. AOSP) | Deep `.env` is never masked; a command reads it and the contents go to the model/chat, defeating the documented `.env` masking | `test_sandbox_env_depth.py::test_deep_env_is_masked` (and `::test_shallow_env_is_masked` sanity) | Mask `.env*`/`.envrc` by glob at read time (deny by basename pattern under the workspace) instead of a bounded pre-scan |

## What held up (tested, no break found)

- **Egress allow-list suffix matching** (`egress.Policy.permits`): `evil-allowed.com`, `allowed.com.evil.com` denied; `sub.allowed.com` allowed; case-insensitive; trailing dot normalised. No suffix/prefix confusion.
- **Loopback / link-local via the proxy** (`egress_proxy.decide` + `_open`): `127.0.0.1`/`::1` only on forwarded `ports`; `169.254.169.254` and other link-local/multicast/unspecified denied; **IPv4-mapped IPv6** (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`) correctly classified as loopback/link-local by this Python and denied; decimal/octal literals fall through to `_open`, which resolves then refuses loopback/link-local (DNS-rebinding guard checks *all* `getaddrinfo` results before connecting).
- **CONNECT port policy**: only 443/80 tunnel; 22 and other ports denied. Non-forwarded loopback ports denied.
- **Taint restriction**: a tainted session in auto mode reaches only the model host; `allowed_hosts` and session escapes are correctly *not* honoured while tainted (the `restricted()` check precedes `extra_hosts`).
- **Git persistence**: `.git/config` and `.git/hooks` are ro-bound even though the workspace is writable; `core.hooksPath`/`core.fsmonitor`/`core.sshCommand` cannot be planted because every git config path (`.git/config`, `~/.gitconfig`, `~/.config/git/config`) is read-only.
- **Correctly masked**: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.netrc`, `~/.git-credentials`, `~/.pypirc`, `~/.pgpass`, `~/.vault-token`, `~/.password-store`, `~/.config/gh|glab-cli`, keyrings, browser profiles, `/run/user/<uid>` (ssh/gpg-agent + session bus), all of `~/.crit`. Shallow (`.env`) files masked.
- **Secret env scrubbing** (`agent_shell.scrubbed_environment`): `*TOKEN*`, `*SECRET*`, `*API_KEY*`, `CRIT_*`, `OPENAI_*`, `ANTHROPIC_*`, `AWS_SECRET*`, and named `GITHUB_TOKEN`/`NPM_TOKEN`/... removed before a command runs.
- **Network namespace**: unshared (empty but loopback); raw sockets / UDP / ICMP cannot leave; the only path out is the bridge unix socket to the proxy. The proxy's TCP port lives in crit's own (host) netns, unreachable from the sandbox netns.
- **Read-only mode** and **write-outside-workspace** both fail closed (confirmed by the existing `tests/test_sandbox.py` escape suite, re-verified).

## Residual risks (not turned into failing tests)

- **Fixed (wip/fix-contain): mach-lookup is now an allow-list (`macos.MACH_SERVICES`; LaunchServices, dnssd, securityd, nsurlsessiond excluded); untested on a real Mac.** SB-5 (macOS, logic review only — no Mac here): unrestricted `(allow mach-lookup)` in `macos.py:60`. A blanket mach-lookup lets a sandboxed process reach system daemons (e.g. `nsurlsessiond`, `mDNSResponder`) that can perform network I/O out-of-process, a known way to sidestep Seatbelt `network-outbound` limits. The macOS masked-file list inherits SB-1's gaps too. Should be scoped to the specific services crit needs. Review-only; not executable in this Linux environment.
- **Fixed (wip/fix-contain): under taint in auto mode the forwarded ports close unless device logs are the only taint source (`EgressProxy.taint_kinds`, `taint.kinds()`).** Tainted session can still reach the forwarded adb port (5037). `decide` returns the forwarded-port decision before the taint check, so a tainted auto-mode session can still talk to the local adb server (`decide("127.0.0.1", 5037)` = allowed while `restricted()`). Data stays on loopback but could leave via a connected device; device-mutating adb actions still prompt. Low/info.
- **Verified + documented (wip/fix-contain): with no backend `session.sandbox` is None, so `perm.contained` is False and auto mode asks before every non-read-only command (`test_sandbox.AutoModeTests.test_a_missing_backend_keeps_auto_mode_asking`).** Fail-open when bwrap is unavailable. `agent_shell._apply_sandbox` runs commands unsandboxed (documented) and relies on the permission layer. In auto mode the containment then rests entirely on that layer; worth confirming auto still prompts (or refuses) when `availability().ok` is false — out of scope for this sandbox/egress pass.
- **Private-range (RFC1918 / IPv6 ULA) reachability in `open` egress.** `_open` blocks only loopback/link-local/multicast/unspecified, so in `open` mode (or for an `allowed_host` that resolves internally) a command can reach LAN/internal hosts. This is by the `open` design but is a blast-radius note.
- **HTTP forward `Host:` header** is passed through unchanged while the connection target is the URL host; no egress bypass (the socket still goes to the allowed host), but a note for domain-fronting-style behaviour.

## Overall assessment

The Linux containment is solid on its core promises: filesystem isolation, the empty network
namespace, the proxy-only egress path, DNS-rebinding and link-local guards, the allow-list,
taint restriction, and git-hook persistence all held up under real bwrap and direct policy
probing. The confirmed weaknesses are **completeness gaps, not design breaks**: the
masked-secret list misses several ubiquitous credential files (SB-1), writable build caches
re-open the very "plant code that runs later" hole the git-hook protection closes (SB-2), and
the `.env` scan is too shallow for large trees (SB-3). SB-1 and SB-2 are the ones to fix
first; both are reachable by any single sandboxed command with no further exploit.
