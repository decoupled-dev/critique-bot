# crit

Local coding agent and patch reviewer. The model behind it is an adapter chosen in `config.json`: your company's AI service, its web page driven in Microsoft Edge (`backend: browser`, the default), or a model served on this PC such as Qwen or Gemma behind llama.cpp's `llama-server`, LM Studio, or vLLM (`backend: local`). Prompt composition, the job queue, the agent, and review output are the same with both.

**One command, `crit`, with modes.** The first word picks the mode:

```bash
crit "fix the failing test"           # coding agent (the default)
crit review --patch-file diff.patch   # code review: reads the project, then reports
crit ask --prompt "Summarize" a.txt   # one-shot prompt (alias: general)
crit chat                             # interactive conversation
crit setup | worker | submit | queue-status | gitlab-post
```

`--mode MODE` still works and means the same as the mode word. Details: [Modes](COMMANDS.md#modes).

Command reference for Linux (bash) and Windows PowerShell: [`COMMANDS.md`](COMMANDS.md).

How the code is layered (models, tools, engine, apps, routing) and the interface each layer offers: [`docs/architecture.md`](docs/architecture.md).

Prebuilt downloads for Linux (.deb and zip) and Windows: see [docs/install.md](docs/install.md).

## Requirements

- Python 3.10+
- For the web page: Microsoft Edge (`microsoft-edge-stable` on Linux; Edge is usually already on Windows), or Google Chrome if Edge is not installed. On Linux CI you may also need `playwright install-deps`
- For a local model: an OpenAI-compatible server such as llama.cpp's `llama-server` with a GGUF model (`llama-server -m model.gguf --port 8080 -c 32768`), or LM Studio or vLLM

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

That installs Playwright plus `rich` and `prompt_toolkit` for the `crit` screen. To install everything and put `crit` on `PATH` in one step, use `scripts/setup-crit.sh` (Linux, macOS) or `scripts\setup-crit.ps1` (Windows): [`docs/agent.md`](docs/agent.md#quick-setup-with-the-script).

Installed from a release (`.deb` or zip)? See [docs/install.md](docs/install.md). Either way there is no config to copy. crit keeps everything it makes for you in one private folder, `~/.crit` (`%USERPROFILE%\.crit` on Windows; `CRIT_HOME` moves it): `config.json`, the signed-in `browser-profile/`, `cache/`, and the self-installed program (`app/<version>/`, `bin/`). Settings from older versions are moved in once (the move is noted in the log file, `~/.crit/logs/crit.log`).

### First run

A config file found without asking (`CRIT_CONFIG_FILE`, `/etc/crit/crit.json`, `%ProgramData%\crit\crit.json`) is used with no questions. Otherwise `crit` asks, one question at a time:

```
How does crit reach the model?
  1) Local model (a model server on this PC or the network)
  2) Web UI (a page in the browser)
(Or use a config file: crit config --config-file PATH)
Choose 1 or 2:
```

- **Local model**: `Model server (ip:port):`, then crit checks it (`reachable, 2 models: qwen3-27b, gemma3-12b`, `refused: ...`, `timed out: ...`, `unknown host: ...`) and asks which model. A server on another PC is added to `allowed_hosts`. If the server answers 401 or 403, crit says `the server needs an API key` and asks `API key: ` (typing is not shown); a refused key is asked once more, then `Try another key? [Y/n]`.
- **Web UI**: `url:`, the model (Enter for the page's default), then the page elements with starter guesses that Enter accepts. `crit setup` checks them against the real page when you run it; it is not started by itself.

Then the names to hide, a line about personal data, `Saved to <path>.`, and your command carries on. Ctrl+C at any question writes nothing. Without a terminal (CI, pipes) crit asks nothing and names the one-line command instead. Every answer has a `crit config` flag:

```bash
crit config --url https://ai.company.com/ --selector prompt_input=textarea --selector assistant_messages=.reply
crit config --backend local --local-url 192.168.1.50:8080 --model qwen2.5-coder-14b
crit config --redact "Acme, Globex"                      # names to hide
crit config --check                                      # does the server (or the page's site) answer?
crit config                                              # show the settings in plain words
crit setup                                               # Web UI: point at its message box and replies
```

Field-by-field reference, every `crit config` flag, and which file a command uses: [`docs/config.json.md`](docs/config.json.md). For a model on this PC: `crit config --backend local --local-url http://127.0.0.1:8080 --model NAME` (no selectors or sign-in; see [Local model](docs/config.json.md#local-model-backend-local)).

A local server that needs an API key: `crit config --api-key` asks for it without showing it (`crit config --api-key -` reads it from stdin, `crit config --api-key none` removes it), or set `CRIT_API_KEY`. The key is kept only in your own `~/.crit/config.json` (a shared config file cannot supply it), is sent as `Authorization: Bearer <key>` only to the configured local server, and is never logged; `crit config` shows only `API key: set` or `API key: not set`. The context window comes from `local.num_ctx`; when that is 0, crit reads it from the server (llama.cpp `/props`, vLLM `/v1/models`) and otherwise assumes 32768. It never asks.

`crit setup` serves a page on `127.0.0.1`, opens Edge on the url, and lets you **click** the prompt box, the send button, a reply, and the stop button instead of hand-writing CSS. It ranks candidate selectors, saves them, and runs a live test round trip.

The page runs as it does in a normal browser, so it works as usual. crit controls what leaves on its own side, by what it types into the page: secrets are redacted and secret files need your yes. `"filter_chat_page": true` also restricts the page to the url's own site and `allowed_hosts`. Sign-in happens in a separate window.

Set `model` to the **visible label** to pick (for example `GPT-5.1`). The picker is a **button or clickable div that opens a panel**. Set `selectors.model_dropdown_identifier` (or top-level `model_dropdown_identifier`) to unique text, `aria-label`, `id`, or `data-testid` on that opener so other buttons are ignored. After that click, crit looks for the model name in the panel. `selectors.model_dropdown` is an optional CSS selector for the same opener; `selectors.model_option` can target items inside the open panel.

Set `selectors.stop_button` too. It is how crit knows a reply actually finished rather than merely paused, and without it a model that thinks for longer than `idle_ms` mid-answer yields a silently truncated review.

To write selectors by hand instead:

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

crit reuses a persistent Edge profile (default `~/.crit/browser-profile`; `user_data_dir` changes it) so you stay signed in. If that folder has no profile yet, the first run opens an Edge window so you can log in. After you sign in, that window closes and the same run continues headless. Later runs stay headless unless you pass `--headed`.

Set `user_data_dir` to `system` to open **real Microsoft Edge** (the desktop app, not a Playwright automation window). Chromium blocks remote debugging on the daily desktop profile (Windows: `%LOCALAPPDATA%\Microsoft\Edge\User Data`; HTTP 403), so crit uses a dedicated profile outside that folder (`msedge-user-data` in `~/.crit`, `%USERPROFILE%\.crit` on Windows). A missing dedicated profile opens a visible window for the first login. After you sign in, that window closes and the same run continues headless. Later runs reuse that session. Everyday Edge is left alone. Pass `--headed` when you want the window on a later run.

To attach to an Edge window you already started yourself, launch it with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`, then set `cdp_url` to `http://127.0.0.1:9222`.

Optional extra cookies: Playwright `storage_state` via config or `CRIT_STORAGE_STATE`.

Env overrides: `CRIT_URL`, `CRIT_MODEL`, `CRIT_STORAGE_STATE`, `CRIT_USER_DATA_DIR`, `CRIT_CDP_URL`.

## Check the install

Send a one-shot prompt through the signed-in page. The first run opens Edge when the browser profile is missing so you can sign in, then closes that window and continues headless. Later runs reuse that profile. Pass `--headed` to show the window anyway.

```bash
crit ask --prompt "Reply with exactly one word: PONG."
```

The setup UI has the same check: **Send test prompt**.

## Run

### Review

`crit review` gives the patch and the review format in [`prompts/review.txt`](prompts/review.txt) to the Code Reviewer. Before it reports, the reviewer reads what it needs with read-only tools: the changed files, their callers, tests, interfaces, manifests, git history. Edits, commands, web fetches, helper conversations, and paths outside the project are refused without asking, so a patch cannot make crit act. It has 20 tool rounds by default (`--max-rounds`), and its last reply is the review. It writes `{output-dir}/review.md` + `review.json` in the same format as before, so `gitlab-post` works unchanged. The transcript goes to `.crit/sessions/`.

```bash
crit review --patch-file diff.patch      # or: git diff main | crit review
```

`--quick` runs the one-shot review instead: one message with the patch and the changed files, and no tools. It's faster and uses fewer turns. The CI worker uses the one-shot review; `submit` refuses `--deep` for now, because the worker only runs one-shot reviews.

The one-shot review:

```bash
crit review --quick --patch-file diff.patch --output-dir ./out
```

`--headed` shows the window while you debug selectors. Omit `--patch-file` to read the patch from stdin. `--prompt-template` can replace the default review template (`{patch}` required; `{files}` is HEAD contents of changed files when they fit one paste). Review mode always loads those files from `--repo-dir` (or the CI checkout). When everything fits, one paste is ordered instructions → HEAD files → patch. When it would overflow, the worker sends those as separate turns in the same order (ACK on a file means keep it for the review).

### General

Sends your prompt as-is, with optional files (patch, source, or any UTF-8 text). Writes `{output-dir}/reply.md` + `reply.json`.

```bash
crit ask --prompt "Summarize this change and list risks" \
  diff.patch

crit ask --prompt-file instructions.txt \
  --file src/cli.py \
  src/config.py
```

`--file` and trailing paths both attach files. If the prompt contains `{files}` or `{patch}`, those contents replace the placeholder; otherwise they are appended after the prompt, labeled with each path.

### Interactive conversation

Interactive conversation in this terminal. Edge stays headless when a profile already exists; a missing profile opens the window so you can sign in, then closes it and continues headless. Pass `--headed` to show the window anyway. Type a message at `You>`; a `Thinking...` spinner shows until the assistant reply is printed. `exit` / `quit` / Ctrl-D ends the session and writes `{output-dir}/chat.md` + `chat.json`. Diagnostic logs go to `~/.crit/logs/crit.log`; pass `--logs` to also print them on stderr.

```bash
crit chat
```

Pass `--headed` to show the Edge window:

```bash
crit chat --headed
```

Optional first message and files:

```bash
crit chat --prompt "Let's go through this file" \
  src/cli.py
```

In-session commands: `/help`, `/file PATH [message]` to attach a file to the next turn, and a trailing `\` to continue a line.

### Agent

`crit` is the local coding agent. It edits the repo on this machine. The first run in a folder creates `.crit/`, picks the role crit plays in this project from its files (Code Reviewer, AOSP Expert, Expert Developer, AI Expert, Test Engineer, Android App Engineer, or Generic; the role's paragraph follows the instructions, the log file names it, and `/role` or `--role` changes it), asks once per user which text style looks right in this terminal, and, when crit has no settings yet, asks how crit reaches the model (see [First run](#first-run)): a local model server on this PC or the network (it asks for `ip:port`, checks it, and lists the server's models to pick from), or a Web UI in the browser (it asks for the address, the model, and the page elements; `crit setup` checks them later); the answers go to your user settings. `crit configure` asks again, and `crit config` shows or changes any setting. Then it asks you to sign in and opens Edge when the profile is not signed in yet. Later runs go straight to the task. The instructions in [`prompts/agent.txt`](prompts/agent.txt) go out in front of the first task, in the same message, so the first reply already works on it and emits `<tool_call>` blocks. Later turns are tool results, each followed by a short STATE block (files read, files changed, last command) so the model keeps track in a long conversation. Edits run as soon as you approve them, or at once in auto mode (`--yes`). Shift+Tab switches between ask, accept-edits, auto, and plan modes; `crit --plan` makes the model send a plan you approve before anything changes. Built-in skills (Android, AOSP, Android Automotive, JUnit/Espresso, Gradle, Java, Kotlin, Compose, C++/JNI, ASPICE, functional safety) go with the tasks they fit. A self-contained part of a task (an investigation, a contained change, a test run) can go to a short helper conversation that reports back (`delegate`), which keeps the main conversation small; several run at the same time. Only the conversation with the url leaves the PC, and with a local model nothing does: see [docs/security.md](docs/security.md). Switching between the web page and a local model is a `config.json` change (`backend`); see [Use a local model](docs/agent.md#use-a-local-model-instead-of-the-web-page). See [docs/agent.md](docs/agent.md#modes). A question with no file change is answered in words and that task ends. A reply that answers and also prints tool calls shows the answer and runs the calls. A refusal, or a question that only hands the task back, is sent back until the model calls a tool or finishes; after three in a row the next one ends the task as FAILED. A real question that needs your decision (or an `ask_user` call) is shown to you, and your answer goes back to the model. When the model says no edit is needed, or replies COMPLETED or DONE, that task ends; after an edit, a configured check command runs first. `crit undo` restores the files changed by the last task.

The session screen works like Claude Code: a header box with the folder, model, shell, permission mode, and role; a `>` input box with history (`.crit/history`), Alt+Enter or Ctrl+J for a new line, `/` for commands (`/help`, `/new`, `/clear`, `/undo`, `/permissions`, `/role`, `/status`, `/shell`, `/tools`, `/theme`, `/exit`), and `@` to complete file paths. Each tool step prints `● Read(src/app.py)` with a `⎿` result line; edits show a numbered diff. Edits, commands, web fetches, and paths outside the project ask first (yes / yes for this session / no with a note to the model); `crit --yes` or `"permissions": "auto"` in `.crit/settings.json` skips the prompts. Ctrl+C stops a task and the reply the page is still writing; twice on an empty prompt (or Ctrl+D, or `exit`) leaves. Piped output is plain text, and `NO_COLOR` is honored. Details: [`docs/agent.md`](docs/agent.md).

```bash
crit
crit "update the test cases"
crit --yes "fix the failing test"
crit undo
```

The first `crit` creates `.crit/` (settings, a symbol index in `.crit/cache/index.sqlite`, and session transcripts) and records `config.json` when that file is in the repo. `crit init` rebuilds the index without replacing settings. A task run from a subdirectory finds `.crit` by walking upward. A project's old `.bot/` folder is renamed to `.crit/` on first use (when both exist, `.crit` is used). When the project is your home folder itself, its data lives in `~/.crit/projects/home/`. The index works with the standard library alone; `pip install -e .[index]` adds tree-sitter parsers for more exact symbol spans in Python, Java, Kotlin, Go, Rust, C, C++, C#, JavaScript, and TypeScript.

In an IDE: `crit --acp` runs crit as an Agent Client Protocol agent, so Android Studio (Bring Your Own Agent), the other JetBrains IDEs, and Zed can use it as their coding agent. Setup and details: [`docs/acp.md`](docs/acp.md).

To put `crit` on `PATH` and reuse one `config.json` and one signed-in profile from any folder, run the setup script for your OS: `scripts/setup-crit.sh` on Linux and macOS, `.\scripts\setup-crit.ps1` on Windows. Script options and the manual steps for both: [`docs/agent.md`](docs/agent.md#quick-setup-with-the-script).

## Quiet sessions and the log file

A normal run shows only questions (first-run setup, sign-in, approvals, and the blocked-site `Allow?` question when `"page_network"` is `"strict"`), the agent's own screen, install and uninstall results, and one plain line when crit cannot continue (`Problem: ...`, or `crit stopped: ... Details: ~/.crit/logs/crit.log`). Everything else (blocked-site notices, the project's role, which config file is used, file moves, warnings) goes only to `~/.crit/logs/crit.log`. That file is always written, is readable only by you, is rotated at about 2 MB (3 files kept), and every line is redacted like outgoing messages. `--logs` also prints the log lines on stderr.

## Uninstall

`crit uninstall` (on Windows also Settings > Apps > Installed apps > crit > Uninstall) removes crit's program, settings, and sign-in, and keeps project data and each project's `.crit` folder. Details: [docs/install.md](docs/install.md#uninstall).

## CI runner (GitLab)

GitLab runner and project setup: [`docs/gitlab-ci.md`](docs/gitlab-ci.md).

CI jobs must **not** each launch Edge. On the runner PC, start **one worker**. The job calls **submit**, waits for `out/review.md`, then posts it on the MR. The worker owns the signed-in Edge.

```bash
# once, on the runner (systemd: packaging/crit-worker.service)
crit worker --config /opt/crit/config.json --logs

# each GitLab job
crit submit --config /opt/crit/config.json --output-dir out

# post the result (project + MR come from CI_PROJECT_ID / CI_MERGE_REQUEST_IID)
crit gitlab-post --review-file out/review.md --patch-file diff.patch
```

The job and the worker must share `queue_dir` (default: `.critique-queue` next to `config.json`). Concurrent MRs enqueue; the worker runs up to `max_parallel_tabs` reviews at once (default 1) with `min_interval_seconds` between starts. A job that keeps hitting browser errors is retried `max_attempts` times and then failed, so a broken session cannot spin the queue forever.

To see what CI sees:

```bash
crit queue-status --config /opt/crit/config.json
```

It prints whether the worker is alive, what is waiting or in progress, and how recent jobs ended; `--json` for scripts. It exits non-zero when no worker is running, which makes it a usable health check.

Copy [`.gitlab-ci.yml`](.gitlab-ci.yml) (Linux) or [`packaging/gitlab-ci.windows.yml`](packaging/gitlab-ci.windows.yml) (Windows) into the app repo. The runner must be self-hosted, **shell** executor, tag `crit`. `gitlab-post` needs `CRIT_GITLAB_TOKEN` (scope `api`). Shared GitLab.com / instance runners and Docker executors cannot run crit: there is no signed-in Edge and no shared queue.

`crit chat` is local/debug only. One-shot `crit review --quick --patch-file …` (no `submit`) still works for a single machine. Two browser one-shots at once will fight over the Edge profile.

## Deploy

Windows PowerShell and Linux zip (and pip wheel) instructions: [DEPLOY.md](DEPLOY.md).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
