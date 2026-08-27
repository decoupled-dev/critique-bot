# Critique bot

Python CLI that opens a ChatGPT-like web UI in **headless Microsoft Edge**, selects a model, pastes a patch, waits for the reply, then writes it to **stdout** and `{output-dir}/review.md` + `review.json`.

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

Copy [`config.example.json`](config.example.json) to `config.json` and set the real chat URL plus CSS selectors for the prompt/send/reply. Set `model` to the **visible label** to pick (for example `GPT-5.1`). The bot walks the DOM, including open shadow roots and combobox/dropdown lists, and clicks the matching control. `selectors.model_dropdown` is optional: only set it if the list stays closed until you click a specific opener.

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

The bot reuses a persistent Edge profile (default `.edge-profile`) so you stay signed in. First run with `--headed`, log in to the chat UI, then later runs (including headless) reuse that session.

Set `user_data_dir` to `system` to open **real Microsoft Edge** (the desktop app, not a Playwright automation window). Chromium blocks remote debugging on the daily desktop profile, so the bot uses a dedicated Edge profile next to it (`microsoft-edge-critique-bot` on Linux). Log in once in that window; later runs reuse the session. Everyday Edge is left alone.

To attach to an Edge window you already started yourself, launch it with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`, then set `cdp_url` to `http://127.0.0.1:9222`.

Optional extra cookies: Playwright `storage_state` via config or `CRITIQUE_STORAGE_STATE`.

Env overrides: `CRITIQUE_CHAT_URL`, `CRITIQUE_MODEL`, `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`.

## Run

```bash
python -m critique_bot --config config.json --patch-file diff.patch --output-dir ./out
```

`--headed` shows the window while you debug selectors. Omit `--patch-file` to read the patch from stdin.
