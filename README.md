# Critique bot

General-purpose web LLM bot: it opens a ChatGPT-like UI in **headless Microsoft Edge**, pastes a prompt (and optional files), waits for the reply, then writes it to **stdout** and `{output-dir}`.

**Default mode is a specialized code reviewer.** Pass a patch and the bot wraps it in the review template. `--mode general` sends a one-shot prompt. `--mode chat` is an interactive conversation in the terminal.

## Requirements

- Python 3.10+
- Microsoft Edge installed (`microsoft-edge-stable` on Linux; Edge is usually already on Windows)
- On Linux CI, you may also need: `playwright install-deps`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Copy [`config.example.json`](config.example.json) to `config.json` and set the real chat URL plus CSS selectors for the prompt/send/reply. Set `model` to the **visible label** to pick (for example `GPT-5.1`). The picker is a **button or clickable div that opens a panel**. Set `selectors.model_dropdown_identifier` (or top-level `model_dropdown_identifier`) to unique text, `aria-label`, `id`, or `data-testid` on that opener so other buttons are ignored. After that click, the bot looks for the model name in the panel. `selectors.model_dropdown` is an optional CSS selector for the same opener; `selectors.model_option` can target items inside the open panel.

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

The bot reuses a persistent Edge profile (default `.edge-profile`) so you stay signed in. First run with `--headed`, log in to the chat UI, then later runs (including headless) reuse that session.

Set `user_data_dir` to `system` to open **real Microsoft Edge** (the desktop app, not a Playwright automation window). Chromium blocks remote debugging on the daily desktop profile, so the bot uses a dedicated Edge profile next to it (`microsoft-edge-critique-bot` on Linux). Log in once with `--headed`; later runs reuse that session. Everyday Edge is left alone. Without `--headed`, this path is headless too.

To attach to an Edge window you already started yourself, launch it with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`, then set `cdp_url` to `http://127.0.0.1:9222`.

Optional extra cookies: Playwright `storage_state` via config or `CRITIQUE_STORAGE_STATE`.

Env overrides: `CRITIQUE_CHAT_URL`, `CRITIQUE_MODEL`, `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`.

## Run

### Review (default)

Wraps the patch in [`prompts/review.txt`](prompts/review.txt) and writes `{output-dir}/review.md` + `review.json`.

```bash
python -m critique_bot --config config.json --patch-file diff.patch --output-dir ./out
```

`--headed` shows the window while you debug selectors. Omit `--patch-file` to read the patch from stdin. `--prompt-template` can replace the default review template (`{patch}` placeholder required).

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

## Deploy (Windows and Linux)

The bot is a CLI. Ship a **zip per OS** (no Python install on the target machine) or a **pip wheel**. Microsoft Edge must already be installed on the target (`microsoft-edge-stable` on Linux; Edge is usually already on Windows). Build **on the OS you want to ship** — a Linux binary will not run on Windows, and vice versa.

### Standalone zip (recommended)

From a Python 3.10+ checkout:

```bash
python -m pip install -e ".[packaging]"
python scripts/build.py
```

That writes `dist/critique-bot-<version>-linux-x64.zip` or `dist/critique-bot-<version>-windows-x64.zip`. Unzip on the target, copy `config.example.json` to `config.json`, then:

```bash
# Linux
./critique-bot --config config.json --patch-file diff.patch

# Windows
.\critique-bot.exe --config config.json --patch-file diff.patch
```

Keep `_internal` next to the executable. First login still uses `--headed` so the Edge profile (`.edge-profile` by default) can be reused later.

CI builds both zips: GitHub Actions workflow **Build** (manual run, or push a `v*` tag). Tagging `v0.1.0` also attaches the zips and a pip wheel to a GitHub Release.

### Pip wheel (Python already on the target)

```bash
python -m pip install build
python -m build --sdist --wheel
pip install dist/critique_bot-*.whl
critique-bot --config config.json --patch-file diff.patch
```

### Chat

Interactive conversation in this terminal. The browser window opens (same as `--headed`). Type a message at `You>`, then the assistant reply is printed. `exit` / `quit` / Ctrl-D ends the session and writes `{output-dir}/chat.md` + `chat.json`.

```bash
python -m critique_bot --config config.json --mode chat
```

Optional first message and files:

```bash
python -m critique_bot --config config.json --mode chat \
  --prompt "Let's go through this file" \
  src/cli.py
```

In-session commands: `/help`, `/file PATH [message]` to attach a file to the next turn, and a trailing `\` to continue a line.
