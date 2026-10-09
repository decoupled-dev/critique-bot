# Security and data leaving the PC

**Rule: the only data that leaves this PC is the conversation with the configured chat URL.** With an internal chat URL (a company-hosted model), that conversation stays inside the company network too. The chat site's own extra hosts (API, static files, sign-in) go in `allowed_hosts` in `config.json`; nothing else is reachable. Everything below enforces that rule, and the last section lists what it cannot cover.

## Verified

- **A full ChatGPT turn, traced at the system level** (`strace -f -e connect` over Python, Playwright's driver, and the browser) connected to four addresses. All four are chatgpt.com's own. Nothing else was contacted, DNS aside.
- **Every channel a page can use to send data** was tried against a separate tracker host while the filter was on: `fetch`, XHR, `sendBeacon`, image, script, tracking pixel, iframe, a form POSTed into an iframe, WebSocket, `window.open` popup, preconnect, and a WebRTC STUN request. **Nothing reached the tracker.** With the filter off, the same page reached it nine ways, which proves the channels are real. This runs as `tests/test_browser_egress.py` wherever Chrome or Edge is installed.
- **Every program crit starts** (git, rg, the shell, PowerShell) was checked. In agent mode, none of them is pointed at another machine.

## What goes to the chat

The chat at `url` in `config.json` receives the instructions, your tasks, and what the model asks to see: file contents, search results, command output, and diffs. That is how the agent works. Before each message is sent, crit replaces known secret formats with `[REDACTED:kind]`:

- private keys
- AWS, GitHub, GitLab, Slack, Google, OpenAI, and Anthropic tokens
- JWTs
- a password inside a URL
- `password=`, `token=`, and `api_key=` style values in config and properties files
- secret entries in `strings.xml`

Code that only names a secret (`BuildConfig.API_KEY`, `getPassword()`) is left alone. An edit or write that would put the marker back into a file is refused, so a real secret is never overwritten.

Files that usually hold secrets need your yes before they are read, in every mode, auto included:

- `.env`
- `*.jks`, `*.keystore`, `*.pem`, and `*.key`
- `id_rsa`
- `.netrc` and `.npmrc`
- `keystore.properties`
- anything under `.ssh` or `.aws`

## What the browser contacts

- **Every window crit drives (headless or visible) is filtered from the first request.** The filter is set on the whole browser context, so new tabs and popups are covered too. The page may reach only the chat's own hosts. For chatgpt.com these are chatgpt.com, openai.com, its CDNs, and its Cloudflare and Arkose challenge hosts. This applies to everything:
  - page loads, in any frame (so no third-party iframe and no form posted into one)
  - scripts, images, `fetch`, XHR, beacons
  - **WebSockets**, which Playwright's request filter does not see, so they have a filter of their own
- **Service workers cannot register.** Their requests would bypass the page's filter.
- **WebRTC may not send UDP** to STUN or TURN servers, and DNS prefetching is off.
- **The browser's own services are off.** This covers background networking, sync, component updates, safe-browsing pings, crash reports, and metrics. Playwright passes these flags for the browser it launches, and crit passes the same ones to desktop Edge.
- **The debugging port (used for helper tabs) is local and closed to web pages.** It listens on 127.0.0.1 only, and crit no longer passes `--remote-allow-origins=*`. With that flag, any web page open on the PC could take over the signed-in browser. Without it, a page's connection is refused (verified: `403 Forbidden`), and crit's own connection still works.
- **Sign-in** happens in a plain browser window: no automation, no filter, no extra flags. The site and its login provider (Google, Microsoft, company SSO) see it like any sign-in. The window closes after sign-in, and crit then works headless on the same profile.

## What the model's tools may do

- **`web_fetch` is off.** Even a read tells a site what is being looked at. With `"web_fetch": true` in `.bot/settings.json` it reads documentation pages over https from a fixed list of sites:
  - Android: developer.android.com, source.android.com, android.googlesource.com
  - Kotlin, Gradle, Java: kotlinlang.org, docs.gradle.org, docs.oracle.com, openjdk.org
  - Python, Microsoft, MDN: docs.python.org, learn.microsoft.com, developer.mozilla.org
  - and a few others (see `DEFAULT_WEB_HOSTS`)

  The address must be a plain page address. No `?query` string, no user name or password, no port other than 443, no more than 300 characters, and no path that looks like encoded data or a secret. Redirects stay on the same host. Add sites with `"web_fetch_hosts": ["..."]`.
- **Commands that can send data out do not run.** That covers:
  - web clients: `curl`, `wget`, `Invoke-WebRequest`/`irm`, `Net.WebClient`, `certutil -urlcache`, `Start-BitsTransfer`
  - remote shells and copies: `ssh`, `scp`, `sftp`, `ftp`, `nc`, `socat`, remote `rsync`
  - code and cloud tools: `git push`, `gh`, `glab`, and the cloud CLIs (`aws`, `az`, `gcloud`, ...)
  - publishing: `npm publish`, `twine upload`, `mvn deploy`, `gradlew publish`, `gradlew --scan`, `docker push`, `repo upload`
  - file servers (`python -m http.server`), DNS lookups, mail, and `python -c` or `node -e` one-liners that open sockets

  A command that runs a script file is checked too: if the script uses network APIs (`requests`, `urllib`, `socket`, `fetch`, `Invoke-WebRequest`, ...), it does not run. `"network_commands": "ask"` turns the block into a yes/no question that is asked in every mode.
- **Builds and package managers may still download dependencies.** Gradle, Maven, npm, and pip fetch from their configured repositories. That is the one other traffic besides the chat, and it carries dependency names and versions, not your code. `npm install` no longer uploads the dependency list for an audit (`npm_config_audit=false`).
- **`"offline": true` stops that too.** Commands crit runs then reach no other machine:
  - web proxies point at a closed local port, which curl, pip, npm, git, and the JVM honor
  - npm, yarn, pnpm, pip, uv, Cargo, Go, Maven, and NuGet run in their offline modes
  - the Gradle wrapper gets `--offline`
  - git may use local repositories only (`GIT_ALLOW_PROTOCOL=file`)

  `web_fetch` stays off, and commands that send data out are blocked even with `"network_commands": "ask"`. Builds then need their dependencies in the local caches, from one online build beforehand or from a company mirror on this PC.
- **Tool telemetry is off** in every command crit runs (`DO_NOT_TRACK=1` and the tool-specific switches): PowerShell's update check, .NET, npm, Next.js, Homebrew, Azure, Flutter, Hugging Face, and the GitHub CLI.
- **CodeGraph is gone.** Earlier versions bundled CodeGraph, which sent usage telemetry (no code: language names, size ranges, command counts, a random install ID) to telemetry.getcodegraph.com when it built an index. It is removed, and crit deletes the copy it had unpacked (`critique-bot/codegraph` in the app-data folder). CodeGraph's own settings in `~/.codegraph` and any `.codegraph/` folder in a project are left alone; delete them if no other tool uses them.

## On disk

`.bot/sessions` (transcripts), `.bot/cache` (undo copies), and `.bot/history` (your typed tasks) stay on the PC. Transcripts are saved as they were sent, with secrets redacted. They are git-ignored in `.bot/.gitignore` and the project's `.gitignore`, so they are not committed by accident. The folders are readable only by your user on Linux and macOS.

## Dependencies

crit depends on three packages: `playwright`, `rich`, and `prompt_toolkit`. With what they pull in, that is ten packages, pinned to exact tested versions in [`constraints.txt`](../constraints.txt). Both setup scripts install with those pins, so a new release of any of them is never installed unreviewed. No other program is bundled, and nothing is downloaded at run time. To check the pinned versions for known vulnerabilities, run `pip-audit -r constraints.txt` on a machine where sending the package list to the vulnerability database is acceptable.

## What this cannot guarantee

These controls are checks in crit, not a sandbox. For a hard guarantee, add an outbound firewall rule (Windows Defender Firewall or your company's proxy) that allows only the chat URL and your package repositories.

- **The project's own code runs.** A build, a test, or a Gradle plugin is code from the repository, and it can open connections that crit cannot see. A command-line check also cannot recognize every program that talks to the network.
- **The model reads untrusted text.** Files in the repo and fetched docs can contain instructions aimed at the model ("prompt injection"). The controls above limit what such text could make it do, but cannot stop the model from reading it.
- **The browser and the operating system have their own background services.** On Windows, Edge's own services (SmartScreen, diagnostic data, and secure DNS when it is set to a public DNS-over-HTTPS provider) follow Windows and Edge settings or company policy. The flags above turn most of them off but not every one; the Edge policies `SmartScreenEnabled`, `DiagnosticData`, and `DnsOverHttpsMode` turn off the rest.
- **Local programs.** Any program running as your user can read the browser profile and connect to the local debugging port, as it can with any browser on that account.
- **Review mode is a separate path.** `critique-bot` review, `worker`, and `submit` send patches to the chat and post comments to the GitLab URL you configure. That is outside the agent.
