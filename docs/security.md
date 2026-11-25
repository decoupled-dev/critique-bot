# Security and data leaving the PC

**Rule: the only data that leaves this PC is the conversation with the configured url.** crit enforces it on its own side: it controls what it types into the page (secrets redacted, secret files only after your yes, nothing from outside the workspace unasked) and what the agent's commands and tools may reach. The page itself runs as it does when you use it in a browser, so it works normally; what that page does with the conversation is the service's business, as it is for a person using it. With an internal url (a company-hosted model), that conversation stays inside the company network too. With `backend: local` (a model served on this PC, see [config.json.md](config.json.md#local-model-backend-local)) nothing leaves the PC: the loader accepts only a localhost address for the server unless its host is listed in `allowed_hosts`, and the request is made directly, never through an `http_proxy`. Everything below enforces that rule, and the last section lists what it cannot cover.

## Verified

- **A full turn with a hosted AI service, traced at the system level** (`strace -f -e connect` over Python, Playwright's driver, and the browser) connected only to the url's own addresses. Nothing else was contacted, DNS aside.
- **Every channel a page can use to send data** was tried against a separate tracker host with the optional page filter on (`filter_chat_page`): `fetch`, XHR, `sendBeacon`, image, script, tracking pixel, iframe, a form POSTed into an iframe, WebSocket, `window.open` popup, preconnect, and a WebRTC STUN request. **Nothing reached the tracker.** With the filter off, the same page reached it nine ways, which proves the channels are real. This runs as `tests/test_browser_egress.py` wherever Chrome or Edge is installed.
- **Every program crit starts** (git, rg, the shell, PowerShell) was checked. In agent mode, none of them is pointed at another machine.

## What goes to the service

The service at `url` in `config.json` receives the instructions, your tasks, and what the model asks to see: file contents, search results, command output, and diffs. That is how the agent works. In review mode it receives the patch and the changed files. Before each message is sent, in every mode (agent, review, worker, interactive conversation, one-shot, the setup test), crit replaces known secret formats with `[REDACTED:kind]`:

- private keys
- AWS, GitHub, GitLab, Slack, Google, OpenAI, and Anthropic tokens
- JWTs
- a password inside a URL
- `password=`, `token=`, and `api_key=` style values in config and properties files
- secret entries in `strings.xml`
- `Authorization: Bearer`/`Basic` headers, npm, Stripe, SendGrid, Hugging Face, PyPI, Slack webhook, and Google OAuth tokens, Azure `AccountKey=`/SAS signatures, JDBC and Redis passwords, PGP private keys, kubeconfig key data, and GCP service-account JSON
- a private key cut off by a size limit (redaction runs before any truncation)
- personal data, unless `redact_pii` is `false`: emails (not `@example.com`/`.org`/`.net`, `@localhost`, `git@`, or `noreply@`), phone numbers (`+` international, and US / UK formats written with separators), payment cards (issuer prefix and Luhn check), IBANs (mod-97 check), US SSNs, Aadhaar (4-4-4 with its Verhoeff check) and PAN numbers, and UK National Insurance numbers. IP addresses, versions, dates, and plain numbers are left alone.
- the company, customer, and project names in `redact_terms` (any case, in identifiers such as `AcmeClient` or `com.acme.app`), sent as `[REDACTED:name]`; see [config.json.md](config.json.md#redact_terms-and-redact_pii)

Code that only names a secret (`BuildConfig.API_KEY`, `getPassword()`) is left alone. An edit or write that would put the marker back into a file is refused, so a real secret is never overwritten. Redaction is pattern matching: it is best effort, not a guarantee.

Files that usually hold secrets need your yes before they are read or edited (moved, rewritten, or deleted), in every mode, auto included, whether the model reads them, searches them, shows them from git (`HEAD:.env`), or names them in a command (`cat .env`):

- `.env`, `.envrc`, `*.env`, `.env.*`, `credentials*`, `auth.json`, `.vault-token`, `.dockercfg`, `kubeconfig`, `*.tfvars`, `*.tfstate`
- service-account keys (`serviceAccountKey.json`, `firebase-adminsdk-*.json`, `client_secret*.json`), `*.p12`, `*.pfx`, `*.ppk`
- `*.jks`, `*.keystore`, `*.pem`, and `*.key`
- `id_rsa`
- `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`, `.git-credentials`
- `keystore.properties`, `gradle.properties`, `local.properties`, `google-services.json`
- shell histories, and the browser profile's `Cookies` and `Login Data`
- anything under `.ssh`, `.aws`, `.docker`, `.kube`, `.gnupg`, or the browser profile folder

A workspace-wide search leaves those files out of its results. Review mode never attaches them whole; their diff hunks still go, redacted.

Anything outside the workspace (a read, a write, or a command run there) also needs your yes in every mode.

## What the browser contacts

- **`"page_network"` splits the page from crit.** Under `"egress": "strict"` crit's own traffic is limited either way (it types only into the page at `url`; agent commands offline; `web_fetch` off; network commands blocked; `cdp_url` refused; GitLab only at `gitlab.base_url`). With `"page_network": "open"` (the default) the page loads its own sites as it would for a person, through the system proxy, with no resolver catch-all and no page filter. The trade-off: third-party scripts the page loads (analytics, a CDN, a support widget) can see what crit types into it, just as they can see what a person types there. crit still redacts `redact_terms` and personal data before typing, and asks before sending secret files. With `"page_network": "strict"` the page reaches only the url's host and `allowed_hosts` (the resolver rules and filter below); use it when you know every host the page needs.

- **The page runs as normal.** Its scripts, images, API calls, WebSockets, service workers, and the hosts it calls behind the scenes all work, as they would for a person. crit does not filter them by default, because a web page often depends on hosts besides its own (a CDN, a sign-in or telemetry service) and blocking one can break it in ways that are hard to see.
- **Optional page filter: `"filter_chat_page": true`** in `config.json` restricts the page to the url's own site (for `ai.corp.example`, all of `corp.example`; on shared hosting such as `*.azurewebsites.net`, the exact host only) plus `allowed_hosts`. It covers page loads in any frame, `fetch`, XHR, beacons, and WebSockets, and blocks service workers. It is page-level: a Web Worker can open a WebSocket around it. Use it only when you know every host the page needs.
- **The browser's own services are off.** This covers background networking, sync, component updates, network time, field trials, Safe Browsing, browser sign-in, secure-DNS lookups at a public resolver, search suggestions, link prediction, the spelling and translation services, crash reports, and metrics. crit does it three ways: switches (`EDGE_PRIVATE_ARGS` and `BROWSER_QUIET_ARGS` in `browser.py`), preferences written into its own browser profile before each launch, and resolver rules that make the browser's service hosts (`BROWSER_SERVICE_HOSTS`: Google's and Microsoft's update, Safe Browsing, sign-in, and telemetry endpoints) unresolvable, unless the url itself is on that site or the host is in `allowed_hosts`. A page behind reCAPTCHA needs `www.google.com` in `allowed_hosts`. WebRTC may not send UDP to STUN or TURN servers, and DNS prefetching is off.
- **Verified with Google Chrome:** with these settings and a page open for 60 seconds, `strace` saw no connection that sent data. Before them, Chrome contacted clients2.google.com, www.gstatic.com, accounts.google.com, and www.google.com on its own. The one remaining `connect()` is Chrome's IPv6 reachability check (a UDP socket aimed at 2001:4860:4860::8888 that sends no packet). The Microsoft Edge hosts are from Microsoft's documentation; trace Edge the same way on your build.
- **crit refuses to run the browser as root** on Linux, because it would need `--no-sandbox`. Run crit as a normal user (the worker service runs as `critique-bot`), or set `CRIT_NO_SANDBOX=1` to accept an unsandboxed browser. In a container the sandbox is off with a warning.
- **The debugging port (used for helper tabs) is local and closed to web pages.** It listens on 127.0.0.1 only, and crit does not pass `--remote-allow-origins=*`. With that flag, any web page open on the PC could take over the signed-in browser. Without it, a page's connection is refused (verified: `403 Forbidden`), and crit's own connection still works.
- **Sign-in** happens in a plain browser window: no automation, no extra flags. The site and its login provider (Google, Microsoft, company SSO) see it like any sign-in. The window closes after sign-in, and crit then works headless on the same profile.

## What the model's tools may do

- **`web_fetch` is off.** Even a read tells a site what is being looked at. With `"web_fetch": true` in `.bot/settings.json` it reads documentation pages over https from a fixed list of sites:
  - Android: developer.android.com, source.android.com, android.googlesource.com
  - Kotlin, Gradle, Java: kotlinlang.org, docs.gradle.org, docs.oracle.com, openjdk.org
  - Python, Microsoft, MDN: docs.python.org, learn.microsoft.com, developer.mozilla.org
  - and a few others (see `DEFAULT_WEB_HOSTS`)

  The address must be a plain page address. No `?query` string, no user name or password, no port other than 443, no more than 300 characters, and no path that looks like encoded data or a secret. Redirects stay on the same host. Add sites with `"web_fetch_hosts": ["..."]`.
- **In auto mode, commands still ask.** Edits and read-only commands (`ls`, `git log`, `grep x a.py`) run. "Read-only" is decided per option, not per program: an option that runs a program or writes a file (`rg --pre`, `sort --compress-program`, `git --output`, `find -fprintf`, `git --ext-diff`), a `VAR=` prefix, a `$` expansion, gradle, or a path outside the workspace makes the command ask. crit's own git calls switch off repository hooks, `core.fsmonitor`, external diff, and textconv, so a cloned repository's `.git/config` cannot run code. Any other command asks, or runs once you said "always allow" for its program, because a command line can hide a connection in ways no check sees. A command that runs a file this task wrote or changed asks in every mode, "always allow" or not.
- **Saved rules** (`"allow"` and `"deny"` in your own `.bot/settings.json`) let trusted commands and paths run without asking, or keep others from ever running. An allow rule never covers a network command, a risky command, a secret file, or a file the task just wrote, and a committed settings file's `allow` is ignored.
- **Helper conversations never ask, so they only read.** They may not run builds, tests, or any command that is not read-only, may not fetch, and may not read secret files or files outside the workspace. The coordinator runs builds and tests after the reports.
- **Commands that can send data out do not run.** That covers:
  - web clients: `curl`, `wget`, `Invoke-WebRequest`/`irm`, `Net.WebClient`, `certutil -urlcache`, `Start-BitsTransfer`
  - remote shells and copies: `ssh`, `scp`, `sftp`, `ftp`, `nc`, `socat`, remote `rsync`
  - code and cloud tools: `git push`, `gh`, `glab`, and the cloud CLIs (`aws`, `az`, `gcloud`, ...)
  - publishing: `npm publish`, `twine upload`, `mvn deploy`, `gradlew publish`, `gradlew --scan`, `docker push`, `repo upload`
  - file servers (`python -m http.server`), DNS lookups and `ping`, mail, and `python -c` or `node -e` one-liners that open sockets
  - raw sockets (`/dev/tcp`, `openssl s_client`), opening a web address in a browser (`start`, `xdg-open`, `explorer`), and Windows file shares (`\\host\share`, `net use`)
  - git to an address the model chose (`git clone https://...`, `git ls-remote`, `git -c`, `GIT_SSH_COMMAND`, `git remote add`)
  - package managers pointed at another address, and installing a package the model named (`pip install x`, `npm install x`, `npx`); installing from the project's own files (`npm ci`, `pip install -r`) still works
  - encoded or decoded commands (`powershell -enc`, `base64 -d | sh`)

  The program is recognized with or without a path or quotes (`/usr/bin/curl`, `"curl"`).

  A command that runs a script file is checked too: if the script uses network APIs (`requests`, `urllib`, `socket`, `fetch`, `Invoke-WebRequest`, ...), it does not run. `"network_commands": "ask"` turns the block into a yes/no question that is asked in every mode.

  This list catches the obvious cases. It is not a boundary: the outbound firewall rule at the end of this page is.
- **Builds and package managers may still download dependencies.** Gradle, Maven, npm, and pip fetch from their configured repositories. That is the one other traffic besides the conversation, and it carries dependency names and versions, not your code. `npm install` no longer uploads the dependency list for an audit (`npm_config_audit=false`).
- **`"offline": true` stops that too.** Commands crit runs then reach no other machine:
  - web proxies point at a closed local port, which curl, pip, npm, git, and the JVM honor
  - npm, yarn, pnpm, pip, uv, Cargo, Go, Maven, and NuGet run in their offline modes
  - the Gradle wrapper gets `--offline`
  - git may use local repositories only (`GIT_ALLOW_PROTOCOL=file`)

  `web_fetch` stays off, and commands that send data out are blocked even with `"network_commands": "ask"`. Builds then need their dependencies in the local caches, from one online build beforehand or from a company mirror on this PC.
- **Telemetry is off for crit itself and everything it starts.** The first thing every crit command does is set `DO_NOT_TRACK=1` and about fifty tool-specific switches (`privacy.py`) in its own environment, so Playwright's driver, the browser, and every program crit or the model runs inherit them: pip, npm, yarn, Next.js, Nuxt, Gatsby, Astro, Storybook, Turbo, Angular, Expo, Cypress, Deno, Wrangler, Vercel, .NET, PowerShell, Azure, the Google Cloud SDK, SAM, HashiCorp tools, Pulumi, vcpkg, Homebrew, Flutter, the GitHub CLI, Hugging Face, Weights & Biases, Gradio, Streamlit, Semgrep, Scarf, and others. They are forced: a `DO_NOT_TRACK=0` in your shell does not turn reporting back on.
- **Commands that print environment variables ask first** (`printenv`, `gci env:`, `/proc/*/environ`), because the environment holds tokens.
- **CodeGraph is gone.** Earlier versions bundled CodeGraph, which sent usage telemetry (no code: language names, size ranges, command counts, a random install ID) to telemetry.getcodegraph.com when it built an index. It is removed, and crit deletes the copy it had unpacked (`critique-bot/codegraph` in the app-data folder). CodeGraph's own settings in `~/.codegraph` and any `.codegraph/` folder in a project are left alone; delete them if no other tool uses them.

## What the repository may not change

A cloned repository is untrusted input, so it cannot loosen crit's rules:

- **A committed config is refused.** When `.bot/settings.json` names a `config` that git tracks in the project (or git cannot confirm it does not: no git, a timeout, or the project is a subfolder of a repository), crit stops with an error instead of using its `url`, `allowed_hosts`, or `cdp_url`. Use your own untracked copy, or `--config`.
- **A committed `.bot/settings.json` loses its permission keys** (`permissions`, `confirm_risky`, `web_fetch`, `web_fetch_hosts`, `network_commands`, `check_command`, `auto_questions`). They count only in your own, untracked settings file. New projects git-ignore `.bot/settings.json`.
- **The check command asks like any other command**, since it can come from `.bot/AGENT.md`.
- **The model cannot rewrite crit's own files.** Writes to `.bot/settings.json`, `.bot/skills/`, and the config file named there are refused.

## The setup page

`critique-bot setup` serves its page on 127.0.0.1 only. Each run makes a random token that is in the page's address; every API call must send it back in a header, and requests whose `Host` or `Origin` is not that local address are refused. Another web page open on the PC cannot read or rewrite `config.json` through it.

## On disk

`.bot/sessions` (transcripts), `.bot/cache` (undo copies), and `.bot/history` (your typed tasks) stay on the PC. Undo copies are never made of secret files. Transcripts are saved as they were sent, with secrets redacted. They are git-ignored in `.bot/.gitignore` and the project's `.gitignore`, so they are not committed by accident. The folders are readable only by your user: 0700 on Linux and macOS, set every time they are used, and an owner-only ACL (icacls) on Windows.

## Dependencies

crit depends on three packages: `playwright`, `rich`, and `prompt_toolkit`. **None of them, and none of what they pull in, sends usage data, metrics, or errors anywhere.** Each pinned version was checked: rich, markdown-it, mdurl, pyee, greenlet, wcwidth, and typing_extensions contain no network code; pygments' only network code is in maintainer scripts that regenerate lexer data and never run on import; prompt_toolkit's only socket code is an optional telnet server crit does not use; Playwright's Node.js driver has no telemetry and contacts its download CDN only for `playwright install`, which crit never runs. `tests/test_privacy.py` fails when a new package appears in `constraints.txt` or `pyproject.toml` until it is reviewed the same way, and when crit's own code imports a reporting library (Sentry, PostHog, Segment, OpenTelemetry, ...). With what they pull in, that is ten packages, pinned to exact tested versions in [`constraints.txt`](../constraints.txt). Both setup scripts install with those pins, so a new release of any of them is never installed unreviewed. Nothing is downloaded at run time. One program ships inside a Python package: Playwright's driver, a Node.js runtime. Playwright takes regular security releases, so update it on a schedule even when nothing else changes. Every shipped package and its license text is in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md), generated offline by `scripts/third_party_notices.py`.

To check the pinned versions for known vulnerabilities, run `pip-audit -r constraints.txt` on a machine where sending the package list to the vulnerability database is acceptable.

## Scans

These run offline and are clean on this repository:

- **Committed secrets:** `gitleaks git .` (history) and `gitleaks dir` over the tracked files. Test fixtures that are fake secrets carry `gitleaks:allow`; their first commits are listed in `.gitleaksignore`.
- **Python security issues:** `bandit -c pyproject.toml -r src/critique_bot`. No medium or high findings. The reviewed false positives are skipped in `[tool.bandit]` in `pyproject.toml` or marked `# nosec` with the reason on the line above.
- **Licenses:** every shipped Python package is under MIT, BSD, Apache-2.0, or PSF terms (`pip-licenses --allow-only=...`).
- **SBOM:** `cyclonedx-py environment` lists the shipped Python packages in CycloneDX format.

## What this cannot guarantee

These controls are checks in crit, not a sandbox. For a hard guarantee, add an outbound firewall rule (Windows Defender Firewall or your company's proxy) that allows only the url and your package repositories. Step-by-step for Windows: [windows-lockdown.md](windows-lockdown.md).

- **The project's own code runs.** A build, a test, or a Gradle plugin is code from the repository, and it can open connections that crit cannot see. A command-line check also cannot recognize every program that talks to the network.
- **The model reads untrusted text.** Files in the repo and fetched docs can contain instructions aimed at the model ("prompt injection"). The controls above limit what such text could make it do, but cannot stop the model from reading it.
- **The page decides where its own traffic goes.** By default crit does not filter it, so whatever crit types into the page, the page's scripts can also hand to the services the site uses (its analytics or error reporting, for example). For a company-hosted AI service that is the company's own service. To limit the page anyway, set `"filter_chat_page": true` or use the outbound firewall rule above.
- **The browser and the operating system have their own background services.** On Windows, Edge's own services (SmartScreen, diagnostic data, and secure DNS when it is set to a public DNS-over-HTTPS provider) follow Windows and Edge settings or company policy. The flags above turn most of them off but not every one; the Edge policies `SmartScreenEnabled`, `DiagnosticData`, and `DnsOverHttpsMode` turn off the rest.
- **Local programs.** Any program running as your user can read the browser profile and connect to the local debugging port, as it can with any browser on that account.
- **GitLab comments are defanged.** Lines in the model's review that start with `/` (GitLab quick actions such as `/approve` or `/merge`) and `@` mentions are neutralized before posting, so text planted in a merge request cannot make the bot act. The GitLab token never follows a redirect and goes only to the configured GitLab host.
- **Review mode is a separate path.** `critique-bot` review, `worker`, and `submit` send patches to the service (redacted, without whole secret files) and post comments to the GitLab URL you configure. The CI templates upload only the review (`out/review.md`, `review.json`, `status.json`) as a job artifact. The prompt and a failed job's page snapshot stay in the worker's queue folder on this PC.
