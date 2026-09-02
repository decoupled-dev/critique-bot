# Critique bot

Patch-review bot that drives a ChatGPT-like web UI in Microsoft Edge. Prompt composition, the job queue, and review output stay the same; the model is whatever is signed into that page.

**Default mode is a specialized code reviewer.** Pass a patch and the bot wraps it in the review template. `--mode general` sends a one-shot prompt. `--mode chat` is an interactive conversation in the terminal.

Command reference for Linux (bash) and Windows PowerShell: [`COMMANDS.md`](COMMANDS.md).

## Requirements

- Python 3.10+
- Microsoft Edge (`microsoft-edge-stable` on Linux; Edge is usually already on Windows), or Google Chrome if Edge is not installed. On Linux CI you may also need `playwright install-deps`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Copy [`config.example.json`](config.example.json) (or [`config.chatgpt.example.json`](config.chatgpt.example.json)) to `config.json`. Field-by-field reference: [`docs/config.json.md`](docs/config.json.md). Then let the setup UI fill in the selectors:

```bash
critique-bot setup --config config.json
```

That serves a page on `127.0.0.1`, opens Edge on your chat URL, and lets you **click** the prompt box, the send button, a reply, and the stop button instead of hand-writing CSS. It ranks candidate selectors, saves them, and runs a live test round trip.

Edge only talks to that chat URL after the page has loaded (plus loopback for CDP). Third-party XHR is aborted; the first navigation is left alone so login can finish.

Set `model` to the **visible label** to pick (for example `GPT-5.1`). The picker is a **button or clickable div that opens a panel**. Set `selectors.model_dropdown_identifier` (or top-level `model_dropdown_identifier`) to unique text, `aria-label`, `id`, or `data-testid` on that opener so other buttons are ignored. After that click, the bot looks for the model name in the panel. `selectors.model_dropdown` is an optional CSS selector for the same opener; `selectors.model_option` can target items inside the open panel.

Set `selectors.stop_button` too. It is how the bot knows a reply actually finished rather than merely paused, and without it a model that thinks for longer than `idle_ms` mid-answer yields a silently truncated review.

To write selectors by hand instead:

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

The bot reuses a persistent Edge profile (default `.edge-profile`) so you stay signed in. First run with `--headed`, log in to the chat UI, then later runs (including headless) reuse that session.

Set `user_data_dir` to `system` to open **real Microsoft Edge** (the desktop app, not a Playwright automation window). Chromium blocks remote debugging on the daily desktop profile (Windows: `%LOCALAPPDATA%\Microsoft\Edge\User Data`; HTTP 403), so the bot uses a dedicated profile outside that folder (`%LOCALAPPDATA%\critique-bot\msedge-user-data` on Windows, `~/.config/critique-bot/msedge-user-data` on Linux). Log in once with `--headed`; later runs reuse that session. Everyday Edge is left alone. Without `--headed`, this path is headless too.

To attach to an Edge window you already started yourself, launch it with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`, then set `cdp_url` to `http://127.0.0.1:9222`.

Optional extra cookies: Playwright `storage_state` via config or `CRITIQUE_STORAGE_STATE`.

Env overrides: `CRITIQUE_CHAT_URL`, `CRITIQUE_MODEL`, `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`.

## Check the install

Send a one-shot prompt through the signed-in chat page. Use `--headed` on the first login so you can sign in; later runs reuse `.edge-profile`.

```bash
critique-bot --config config.json --mode general \
  --prompt "Reply with exactly one word: PONG." --headed
```

The setup UI has the same check: **Send test prompt**.

## Run

### Review (default)

Wraps the patch in [`prompts/review.txt`](prompts/review.txt) and writes `{output-dir}/review.md` + `review.json`.

```bash
python -m critique_bot --config config.json --patch-file diff.patch --output-dir ./out
```

`--headed` shows the window while you debug selectors. Omit `--patch-file` to read the patch from stdin. `--prompt-template` can replace the default review template (`{patch}` required; `{files}` is HEAD contents of changed files when they fit one paste). `--include-changed-files` loads those files from `--repo-dir` and inlines them, or sends at most 8 per chat turn (next file on ACK) if the prompt would overflow. `patch_only_file_count` or more changed files (default 10): patch only.

### General

Sends your prompt as-is, with optional files (patch, source, or any UTF-8 text). Writes `{output-dir}/reply.md` + `reply.json`. `--prompt` also selects this mode if `--mode` is omitted.

```bash
python -m critique_bot --config config.json --mode general \
  --prompt "Summarize this change and list risks" \
  diff.patch

python -m critique_bot --config config.json --mode general \
  --prompt-file instructions.txt \
  --file src/cli.py \
  src/config.py
```

`--file` and trailing paths both attach files. If the prompt contains `{files}` or `{patch}`, those contents replace the placeholder; otherwise they are appended after the prompt, labeled with each path.

### Chat

Interactive conversation in this terminal. Edge stays headless unless you pass `--headed`. Type a message at `You>`; a spinner shows until the assistant reply is printed. `exit` / `quit` / Ctrl-D ends the session and writes `{output-dir}/chat.md` + `chat.json`. Diagnostic logs are off by default; pass `--logs` to print them on stderr.

```bash
python -m critique_bot --config config.json --mode chat
```

Pass `--headed` to show the Edge window:

```bash
python -m critique_bot --config config.json --mode chat --headed
```

Optional first message and files:

```bash
python -m critique_bot --config config.json --mode chat \
  --prompt "Let's go through this file" \
  src/cli.py
```

In-session commands: `/help`, `/file PATH [message]` to attach a file to the next turn, and a trailing `\` to continue a line.

## CI runner (GitLab)

GitLab runner and project setup: [`docs/gitlab-ci.md`](docs/gitlab-ci.md).

CI jobs must **not** each launch Edge. On the runner PC, start **one worker**. The job calls **submit**, waits for `out/review.md`, then posts it on the MR. The worker owns the signed-in Edge.

```bash
# once, on the runner (systemd: packaging/critique-bot-worker.service)
critique-bot worker --config /opt/critique-bot/config.json --logs

# each GitLab job
critique-bot submit --config /opt/critique-bot/config.json --output-dir out

# post the result (project + MR come from CI_PROJECT_ID / CI_MERGE_REQUEST_IID)
critique-bot gitlab-post --review-file out/review.md --patch-file diff.patch
```

The job and the worker must share `queue_dir` (default: `.critique-queue` next to `config.json`). Concurrent MRs enqueue; the worker runs up to `max_parallel_tabs` reviews at once (default 1) with `min_interval_seconds` between starts. A job that keeps hitting browser errors is retried `max_attempts` times and then failed, so a broken session cannot spin the queue forever.

To see what CI sees:

```bash
critique-bot queue-status --config /opt/critique-bot/config.json
```

It prints whether the worker is alive, what is waiting or in progress, and how recent jobs ended; `--json` for scripts. It exits non-zero when no worker is running, which makes it a usable health check.

Copy [`.gitlab-ci.yml`](.gitlab-ci.yml) (Linux) or [`packaging/gitlab-ci.windows.yml`](packaging/gitlab-ci.windows.yml) (Windows) into the app repo. The runner must be self-hosted, **shell** executor, tag `critique-bot`. `gitlab-post` needs `CRITIQUE_GITLAB_TOKEN` (scope `api`). Shared GitLab.com / instance runners and Docker executors cannot run this bot: there is no signed-in Edge and no shared queue.

`--mode chat` is local/debug only. One-shot `critique-bot --patch-file …` (no `submit`) still works for a single machine. Two browser one-shots at once will fight over the Edge profile.

## Deploy

Windows PowerShell and Linux zip (and pip wheel) instructions: [DEPLOY.md](DEPLOY.md).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
