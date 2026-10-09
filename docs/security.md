# Security and data leaving the PC

**Rule: the only data that leaves this PC is the conversation with the configured chat URL.** Everything below enforces that rule, and the last section lists what it cannot cover.

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

- **Every window crit drives (headless or visible) is filtered from the first request.** The page may reach only the chat's own hosts. For chatgpt.com these are chatgpt.com, openai.com, its CDNs, and its Cloudflare and Arkose challenge hosts. Navigations go through, so a Cloudflare check can finish. Everything else is aborted: trackers, Google one-tap, and websockets to other hosts. Verified on this PC: a full headless ChatGPT turn reached only chatgpt.com (320 requests), Google's sign-in script was blocked, and the whole browser made its HTTPS connections to chatgpt.com only.
- **The browser's own services are off.** This covers background networking, sync, component updates, safe-browsing pings, crash reports, and metrics. Playwright passes these flags for the browser it launches, and crit passes the same ones to desktop Edge.
- **The debugging port (used for helper tabs) is local and closed to web pages.** It listens on 127.0.0.1 only, and crit no longer passes `--remote-allow-origins=*`. With that flag, any web page open on the PC could take over the signed-in browser. Without it, a page's connection is refused (verified: `403 Forbidden`), and crit's own connection still works.
- **Sign-in** happens in a plain browser window: no automation, no filter, no extra flags. The site and its login provider (Google, Microsoft, company SSO) see it like any sign-in. The window closes after sign-in, and crit then works headless on the same profile.

## What the model's tools may do

- **`web_fetch` only reads.** It reads documentation pages over https from a fixed list of sites:
  - Android: developer.android.com, source.android.com, android.googlesource.com
  - Kotlin, Gradle, Java: kotlinlang.org, docs.gradle.org, docs.oracle.com, openjdk.org
  - Python, Microsoft, MDN: docs.python.org, learn.microsoft.com, developer.mozilla.org
  - and a few others (see `DEFAULT_WEB_HOSTS`)

  The address must be a plain page address. No `?query` string, no user name or password, no port other than 443, no more than 300 characters, and no path that looks like encoded data or a secret. Redirects stay on the same host. Add sites with `"web_fetch_hosts": ["..."]` in `.bot/settings.json`, or turn it off with `"web_fetch": false`.
- **Commands that can send data out do not run.** That covers:
  - web clients: `curl`, `wget`, `Invoke-WebRequest`/`irm`, `Net.WebClient`, `certutil -urlcache`, `Start-BitsTransfer`
  - remote shells and copies: `ssh`, `scp`, `sftp`, `ftp`, `nc`, `socat`, remote `rsync`
  - code and cloud tools: `git push`, `gh`, `glab`, and the cloud CLIs (`aws`, `az`, `gcloud`, ...)
  - publishing: `npm publish`, `twine upload`, `mvn deploy`, `gradlew publish`, `gradlew --scan`, `docker push`, `repo upload`
  - file servers (`python -m http.server`), DNS lookups, mail, and `python -c` or `node -e` one-liners that open sockets

  A command that runs a script file is checked too: if the script uses network APIs (`requests`, `urllib`, `socket`, `fetch`, `Invoke-WebRequest`, ...), it does not run. `"network_commands": "ask"` turns the block into a yes/no question that is asked in every mode.
- **Builds and package managers may still download dependencies.** Gradle, Maven, npm, and pip fetch from their configured repositories. `npm install` no longer uploads the dependency list for an audit (`npm_config_audit=false`).
- **Tool telemetry is off** in every command crit runs (`DO_NOT_TRACK=1` and the tool-specific switches): PowerShell's update check, .NET, npm, Next.js, Homebrew, Azure, Flutter, Hugging Face, and the GitHub CLI.
- **CodeGraph is gone.** Earlier versions bundled CodeGraph, which sent usage telemetry (no code: language names, size ranges, command counts, a random install ID) to telemetry.getcodegraph.com when it built an index. It is removed, and crit deletes the copy it had unpacked (`critique-bot/codegraph` in the app-data folder). CodeGraph's own settings in `~/.codegraph` and any `.codegraph/` folder in a project are left alone; delete them if no other tool uses them.

## On disk

`.bot/sessions` (transcripts), `.bot/cache` (undo copies), and `.bot/history` (your typed tasks) stay on the PC. They are git-ignored in `.bot/.gitignore` and the project's `.gitignore`, so they are not committed by accident. The folders are readable only by your user on Linux and macOS.

## Dependencies

crit depends on three packages: `playwright`, `rich`, and `prompt_toolkit`. With what they pull in, that is ten packages, pinned to exact tested versions in [`constraints.txt`](../constraints.txt). Both setup scripts install with those pins, so a new release of any of them is never installed unreviewed. No other program is bundled, and nothing is downloaded at run time. To check the pinned versions for known vulnerabilities, run `pip-audit -r constraints.txt` on a machine where sending the package list to the vulnerability database is acceptable.

## What this cannot guarantee

These controls are checks in crit, not a sandbox. For a hard guarantee, add an outbound firewall rule (Windows Defender Firewall or your company's proxy) that allows only the chat URL and your package repositories.

- **The project's own code runs.** A build, a test, or a Gradle plugin is code from the repository, and it can open connections that crit cannot see. A command-line check also cannot recognize every program that talks to the network.
- **The model reads untrusted text.** Files in the repo and fetched docs can contain instructions aimed at the model ("prompt injection"). The controls above limit what such text could make it do, but cannot stop the model from reading it.
- **The browser and the operating system have their own background services.** On Windows, Edge's own services (SmartScreen, diagnostic data) follow Windows and Edge settings or company policy. The flags above turn most of them off but not every one.
- **Local programs.** Any program running as your user can read the browser profile and connect to the local debugging port, as it can with any browser on that account.
- **Review mode is a separate path.** `critique-bot` review, `worker`, and `submit` send patches to the chat and post comments to the GitLab URL you configure. That is outside the agent.
