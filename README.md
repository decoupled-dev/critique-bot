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

Copy [`config.example.json`](config.example.json) to `config.json` and set the real chat URL plus CSS selectors. Fastest way to discover selectors:

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

Optional session cookies: log in once with `--headed`, then save Playwright storage state and set `storage_state` in config or `CRITIQUE_STORAGE_STATE`.

Env overrides: `CRITIQUE_CHAT_URL`, `CRITIQUE_MODEL`, `CRITIQUE_STORAGE_STATE`.

## Run

```bash
python -m critique_bot --config config.json --patch-file diff.patch --output-dir ./out
```

`--headed` shows the window while you debug selectors. Omit `--patch-file` to read the patch from stdin.
