# Security and data leaving the PC

**Rule: the only data that leaves this PC is the conversation with the configured chat URL.** crit enforces it on its own side: it controls what it types into the chat page (secrets redacted, secret files only after your yes, nothing from outside the workspace unasked) and what the agent's commands and tools may reach. The chat page itself runs as it does when you use it in a browser, so the chat UI works normally; what that page does with the conversation is the chat service's business, as it is for a person using it. With an internal chat URL (a company-hosted model), that conversation stays inside the company network too. With `backend: local` (a model served on this PC, see [config.json.md](config.json.md#local-model-backend-local)) nothing leaves the PC: the loader accepts only a localhost address for the server unless its host is listed in `allowed_hosts`, and the request is made directly, never through an `http_proxy`. Everything below enforces that rule, and the last section lists what it cannot cover.

## Verified

- **A full ChatGPT turn, traced at the system level** (`strace -f -e connect` over Python, Playwright's driver, and the browser) connected to four addresses. All four are chatgpt.com's own. Nothing else was contacted, DNS aside.
- **Every channel a page can use to send data** was tried against a separate tracker host with the optional page filter on (`filter_chat_page`): `fetch`, XHR, `sendBeacon`, image, script, tracking pixel, iframe, a form POSTed into an iframe, WebSocket, `window.open` popup, preconnect, and a WebRTC STUN request. **Nothing reached the tracker.** With the filter off, the same page reached it nine ways, which proves the channels are real. This runs as `tests/test_browser_egress.py` wherever Chrome or Edge is installed.
- **Every program crit starts** (git, rg, the shell, PowerShell) was checked. In agent mode, none of them is pointed at another machine.

## What goes to the chat

The chat at `url` in `config.json` receives the instructions, your tasks, and what the model asks to see: file contents, search results, command output, and diffs. That is how the agent works. In review mode it receives the patch and the changed files. Before each message is sent, in every mode (agent, review, worker, chat, one-shot, the setup test), crit replaces known secret formats with `[REDACTED:kind]`:

- private keys
- AWS, GitHub, GitLab, Slack, Google, OpenAI, and Anthropic tokens
- JWTs
- a password inside a URL
- `password=`, `token=`, and `api_key=` style values in config and properties files
- secret entries in `strings.xml`

Code that only names a secret (`BuildConfig.API_KEY`, `getPassword()`) is left alone. An edit or write that would put the marker back into a file is refused, so a real secret is never overwritten. Redaction is pattern matching: it is best effort, not a guarantee.

Files that usually hold secrets need your yes before they are read, in every mode, auto included, whether the model reads them, searches them, shows them from git (`HEAD:.env`), or names them in a command (`cat .env`):

- `.env`, `.envrc`
- `*.jks`, `*.keystore`, `*.pem`, and `*.key`
- `id_rsa`
- `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`, `.git-credentials`
- `keystore.properties`, `gradle.properties`, `local.properties`, `google-services.json`
- shell histories, and the browser profile's `Cookies` and `Login Data`
- anything under `.ssh`, `.aws`, `.docker`, `.kube`, `.gnupg`, or the browser profile folder

A workspace-wide search leaves those files out of its results. Review mode never attaches them whole; their diff hunks still go, redacted.

Anything outside the workspace (a read, a write, or a command run there) also needs your yes in every mode.

## What the browser contacts

- **The chat page runs as normal.** Its scripts, images, API calls, WebSockets, service workers, and the hosts it calls behind the scenes all work, as they would for a person. crit does not filter them by default, because a chat UI often depends on hosts besides its own (a CDN, a sign-in or telemetry service) and blocking one can break it in ways that are hard to see.
- **Optional page filter: `"filter_chat_page": true`** in `config.json` restricts the page to the chat's own site (for `chat.corp.example`, all of `corp.example`; on shared hosting such as `*.azurewebsites.net`, the exact host only) plus `allowed_hosts`. It covers page loads in any frame, `fetch`, XHR, beacons, and WebSockets, and blocks service workers. It is page-level: a Web Worker can open a WebSocket around it. Use it only when you know every host the chat UI needs.
- **The browser's own services are off.** This covers background networking, sync, component updates, safe-browsing pings, crash reports, and metrics. Playwright passes these flags for the browser it launches, and crit passes the same ones to desktop Edge. WebRTC may not send UDP to STUN or TURN servers, and DNS prefetching is off.
- **The debugging port (used for helper tabs) is local and closed to web pages.** It listens on 127.0.0.1 only, and crit does not pass `--remote-allow-origins=*`. With that flag, any web page open on the PC could take over the signed-in browser. Without it, a page's connection is refused (verified: `403 Forbidden`), and crit's own connection still works.
- **Sign-in** happens in a plain browser window: no automation, no extra flags. The site and its login provider (Google, Microsoft, company SSO) see it like any sign-in. The window closes after sign-in, and crit then works headless on the same profile.

## What the model's tools may do

- **`web_fetch` is off.** Even a read tells a site what is being looked at. With `"web_fetch": true` in `.bot/settings.json` it reads documentation pages over https from a fixed list of sites:
  - Android: developer.android.com, source.android.com, android.googlesource.com
  - Kotlin, Gradle, Java: kotlinlang.org, docs.gradle.org, docs.oracle.com, openjdk.org
  - Python, Microsoft, MDN: docs.python.org, learn.microsoft.com, developer.mozilla.org
  - and a few others (see `DEFAULT_WEB_HOSTS`)

  The address must be a plain page address. No `?query` string, no user name or password, no port other than 443, no more than 300 characters, and no path that looks like encoded data or a secret. Redirects stay on the same host. Add sites with `"web_fetch_hosts": ["..."]`.
- **In auto mode, commands still ask.** Edits and read-only commands (`ls`, `git log`, `grep`) run. Any other command asks, or runs once you said "always allow" for its program, because a command line can hide a connection in ways no check sees. A command that runs a file this task wrote or changed asks in every mode, "always allow" or not.
- **Saved rules** (`"allow"` and `"deny"` in your own `.bot/settings.json`) let trusted commands and paths run without asking, or keep others from ever running. An allow rule never covers a network command, a risky command, a secret file, or a file the task just wrote, and a committed settings file's `allow` is ignored.
- **Helper chats never ask, so they only read.** They may not run builds, tests, or any command that is not read-only, may not fetch, and may not read secret files or files outside the workspace. The coordinator runs builds and tests after the reports.
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
- **Builds and package managers may still download dependencies.** Gradle, Maven, npm, and pip fetch from their configured repositories. That is the one other traffic besides the chat, and it carries dependency names and versions, not your code. `npm install` no longer uploads the dependency list for an audit (`npm_config_audit=false`).
- **`"offline": true` stops that too.** Commands crit runs then reach no other machine:
  - web proxies point at a closed local port, which curl, pip, npm, git, and the JVM honor
  - npm, yarn, pnpm, pip, uv, Cargo, Go, Maven, and NuGet run in their offline modes
  - the Gradle wrapper gets `--offline`
  - git may use local repositories only (`GIT_ALLOW_PROTOCOL=file`)

  `web_fetch` stays off, and commands that send data out are blocked even with `"network_commands": "ask"`. Builds then need their dependencies in the local caches, from one online build beforehand or from a company mirror on this PC.
- **Tool telemetry is off** in every command crit runs (`DO_NOT_TRACK=1` and the tool-specific switches): PowerShell's update check, .NET, npm, Next.js, Homebrew, Azure, Flutter, Hugging Face, and the GitHub CLI.
- **CodeGraph is gone.** Earlier versions bundled CodeGraph, which sent usage telemetry (no code: language names, size ranges, command counts, a random install ID) to telemetry.getcodegraph.com when it built an index. It is removed, and crit deletes the copy it had unpacked (`critique-bot/codegraph` in the app-data folder). CodeGraph's own settings in `~/.codegraph` and any `.codegraph/` folder in a project are left alone; delete them if no other tool uses them.

## What the repository may not change

A cloned repository is untrusted input, so it cannot loosen crit's rules:

- **A committed config is refused.** When `.bot/settings.json` names a `config` that git tracks in the project, crit stops with an error instead of using its chat `url`, `allowed_hosts`, or `cdp_url`. Use your own untracked copy, or `--config`.
- **A committed `.bot/settings.json` loses its permission keys** (`permissions`, `confirm_risky`, `web_fetch`, `web_fetch_hosts`, `network_commands`, `check_command`, `auto_questions`). They count only in your own, untracked settings file. New projects git-ignore `.bot/settings.json`.
- **The check command asks like any other command**, since it can come from `.bot/AGENT.md`.
- **The model cannot rewrite crit's own files.** Writes to `.bot/settings.json`, `.bot/skills/`, and the config file named there are refused.
- **In VS Code, a repository's `.vscode/settings.json` cannot set** `crit.agentPath`, `crit.extraArgs`, `crit.configPath`, or `crit.permissionMode`. Only your user settings count.

## The setup page

`critique-bot setup` serves its page on 127.0.0.1 only. Each run makes a random token that is in the page's address; every API call must send it back in a header, and requests whose `Host` or `Origin` is not that local address are refused. Another web page open on the PC cannot read or rewrite `config.json` through it.

## On disk

`.bot/sessions` (transcripts), `.bot/cache` (undo copies), and `.bot/history` (your typed tasks) stay on the PC. Transcripts are saved as they were sent, with secrets redacted. They are git-ignored in `.bot/.gitignore` and the project's `.gitignore`, so they are not committed by accident. The folders are readable only by your user on Linux and macOS.

## Dependencies

crit depends on three packages: `playwright`, `rich`, and `prompt_toolkit`. With what they pull in, that is ten packages, pinned to exact tested versions in [`constraints.txt`](../constraints.txt). Both setup scripts install with those pins, so a new release of any of them is never installed unreviewed. No other program is bundled, and nothing is downloaded at run time. To check the pinned versions for known vulnerabilities, run `pip-audit -r constraints.txt` on a machine where sending the package list to the vulnerability database is acceptable.

## What this cannot guarantee

These controls are checks in crit, not a sandbox. For a hard guarantee, add an outbound firewall rule (Windows Defender Firewall or your company's proxy) that allows only the chat URL and your package repositories. Step-by-step for Windows: [windows-lockdown.md](windows-lockdown.md).

- **The project's own code runs.** A build, a test, or a Gradle plugin is code from the repository, and it can open connections that crit cannot see. A command-line check also cannot recognize every program that talks to the network.
- **The model reads untrusted text.** Files in the repo and fetched docs can contain instructions aimed at the model ("prompt injection"). The controls above limit what such text could make it do, but cannot stop the model from reading it.
- **The chat page decides where its own traffic goes.** By default crit does not filter it, so whatever crit types into the page, the page's scripts can also hand to the services the chat site uses (its analytics or error reporting, for example). For a company-hosted chat that is the company's own service. To limit the page anyway, set `"filter_chat_page": true` or use the outbound firewall rule above.
- **The browser and the operating system have their own background services.** On Windows, Edge's own services (SmartScreen, diagnostic data, and secure DNS when it is set to a public DNS-over-HTTPS provider) follow Windows and Edge settings or company policy. The flags above turn most of them off but not every one; the Edge policies `SmartScreenEnabled`, `DiagnosticData`, and `DnsOverHttpsMode` turn off the rest.
- **Local programs.** Any program running as your user can read the browser profile and connect to the local debugging port, as it can with any browser on that account.
- **Review mode is a separate path.** `critique-bot` review, `worker`, and `submit` send patches to the chat (redacted, without whole secret files) and post comments to the GitLab URL you configure. The CI templates upload only the review (`out/review.md`, `review.json`, `status.json`) as a job artifact. The prompt and a failed job's page snapshot stay in the worker's queue folder on this PC.
