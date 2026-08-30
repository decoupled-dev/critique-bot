# Critique bot

General-purpose LLM bot. Prompt composition, the job queue, and review output are the same for every backend. The model itself is pluggable:

| `backend` | How the model is called | Starter config |
| --- | --- | --- |
| `browser` (default) | Headless Edge drives a ChatGPT-like web UI | [`config.example.json`](config.example.json), [`config.chatgpt.example.json`](config.chatgpt.example.json) |
| `ollama` | Local OpenAI-compatible HTTP API (`http://127.0.0.1:11434/v1`) | [`config.ollama.example.json`](config.ollama.example.json) |
| `openai` | OpenAI Chat Completions | [`config.openai.example.json`](config.openai.example.json) |
| `openai-compatible` | Any `/v1/chat/completions` server (vLLM, LM Studio, Groq, …) | [`config.openai-compatible.example.json`](config.openai-compatible.example.json) |

**Default mode is a specialized code reviewer.** Pass a patch and the bot wraps it in the review template. `--mode general` sends a one-shot prompt. `--mode chat` is an interactive conversation in the terminal.

Command reference for Linux (bash) and Windows PowerShell: [`COMMANDS.md`](COMMANDS.md).

## Requirements

- Python 3.10+
- **browser** backend: Microsoft Edge (`microsoft-edge-stable` on Linux; Edge is usually already on Windows), or Google Chrome if Edge is not installed. On Linux CI you may also need `playwright install-deps`
- **ollama** backend: [Ollama](https://ollama.com) with a pulled model (`ollama serve` / `sudo systemctl start ollama`)
- **openai** / **openai-compatible**: network access and an API key if the server requires one

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Copy a starter file to `config.json`. Field-by-field reference: [`docs/config.json.md`](docs/config.json.md).

### Local Ollama

```bash
cp config.ollama.example.json config.json
# set "model" to a name from `ollama list` (for example llama3, codellama, mistral)
ollama serve   # if the service is not already running
python -m critique_bot --config config.json --mode general --prompt "Say hello in one sentence"
```

### OpenAI (or compatible) API

```bash
cp config.openai.example.json config.json
export OPENAI_API_KEY=sk-...
python -m critique_bot --config config.json --mode general --prompt "Say hello in one sentence"
```

For Groq, vLLM, LM Studio, or similar, copy [`config.openai-compatible.example.json`](config.openai-compatible.example.json) and set `base_url` + `model`. Prefer `CRITIQUE_API_KEY` / `OPENAI_API_KEY` over putting a key in the JSON file.

### Browser (ChatGPT-like UI)

Copy [`config.example.json`](config.example.json) (or [`config.chatgpt.example.json`](config.chatgpt.example.json)) and set the real chat URL plus CSS selectors for the prompt/send/reply. Set `model` to the **visible label** to pick (for example `GPT-5.1`). The picker is a **button or clickable div that opens a panel**. Set `selectors.model_dropdown_identifier` (or top-level `model_dropdown_identifier`) to unique text, `aria-label`, `id`, or `data-testid` on that opener so other buttons are ignored. After that click, the bot looks for the model name in the panel. `selectors.model_dropdown` is an optional CSS selector for the same opener; `selectors.model_option` can target items inside the open panel.

```bash
playwright codegen --channel msedge https://YOUR_CHAT_UI/
```

The bot reuses a persistent Edge profile (default `.edge-profile`) so you stay signed in. First run with `--headed`, log in to the chat UI, then later runs (including headless) reuse that session.

Set `user_data_dir` to `system` to open **real Microsoft Edge** (the desktop app, not a Playwright automation window). Chromium blocks remote debugging on the daily desktop profile, so the bot uses a dedicated Edge profile next to it (`microsoft-edge-critique-bot` on Linux). Log in once with `--headed`; later runs reuse that session. Everyday Edge is left alone. Without `--headed`, this path is headless too.

To attach to an Edge window you already started yourself, launch it with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`, then set `cdp_url` to `http://127.0.0.1:9222`.

Optional extra cookies: Playwright `storage_state` via config or `CRITIQUE_STORAGE_STATE`.

Env overrides: `CRITIQUE_BACKEND`, `CRITIQUE_CHAT_URL`, `CRITIQUE_MODEL`, `CRITIQUE_BASE_URL`, `CRITIQUE_API_KEY`, `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`.

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

## CI runner (GitLab or GitHub)

GitLab runner and project setup: [`docs/gitlab-ci.md`](docs/gitlab-ci.md).

CI jobs must **not** each launch the model. On the runner PC, start **one worker**. The job calls **submit**, waits for `out/review.md`, then posts that file as a comment on the MR or PR. Browser backend: the worker owns the signed-in Edge. Ollama/OpenAI: the worker makes HTTP calls (no browser).

```bash
# once, on the runner (systemd: packaging/critique-bot-worker.service)
critique-bot worker --config /opt/critique-bot/config.json --logs

# each GitLab / GitHub job
critique-bot submit --config /opt/critique-bot/config.json \
  --patch-file diff.patch --output-dir out
```

The job and the worker must share `queue_dir` (default: `.critique-queue` next to `config.json`). Concurrent MRs/PRs enqueue; the worker runs up to `max_parallel_tabs` reviews at once (default 1) with `min_interval_seconds` between starts.

| Host | Job definition | Runner |
| --- | --- | --- |
| GitLab | [`.gitlab-ci.yml`](.gitlab-ci.yml) | Self-hosted, **shell** executor, tag `critique-bot` |
| GitHub | [`packaging/github-review.yml`](packaging/github-review.yml) → `.github/workflows/review.yml` | Self-hosted Actions runner, labels `self-hosted, critique-bot` |

GitHub-hosted `ubuntu-latest` / `windows-latest` cannot run the **browser** backend: there is no signed-in Edge and no shared queue. Ollama or OpenAI on a self-hosted runner still uses the worker + `queue_dir` split.

`--mode chat` is local/debug only. One-shot `critique-bot --patch-file …` (no `submit`) still works for a single machine. Two browser one-shots at once will fight over the Edge profile; HTTP backends do not.

## Deploy

Windows PowerShell and Linux zip (and pip wheel) instructions: [DEPLOY.md](DEPLOY.md).
