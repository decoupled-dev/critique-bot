# Commands

Reference for **modes**, **CLI flags**, and **in-session chat commands**. Examples are given for **Linux (bash)** and **Windows PowerShell**.

Full command list for **Linux (bash)** and **Windows PowerShell**: [`COMMANDS.md`](COMMANDS.md). `--config` is required on every run. Copy [`config.example.json`](config.example.json) to `config.json` first.

How you invoke the bot:

| How | Linux (bash) | Windows PowerShell |
| --- | --- | --- |
| From a checkout | `python -m critique_bot ...` or `critique-bot ...` | `python -m critique_bot ...` or `critique-bot ...` |
| Standalone zip | `./critique-bot ...` | `.\critique-bot.exe ...` |

Replace `python -m critique_bot` with `critique-bot` / `./critique-bot` / `.\critique-bot.exe` as needed. The flags are the same.

Line continuation: bash uses `\`, PowerShell uses `` ` ``.

---

## Backends

`backend` in `config.json` (or `CRITIQUE_BACKEND`) selects how the model is called. Prompt flags, modes, and `submit` / `worker` are the same for every backend.

| `backend` | Calls | `model` | Needs |
| --- | --- | --- | --- |
| `browser` (default) | Web chat UI in Edge | Visible dropdown label | `url` + `selectors` |
| `ollama` | `POST {base_url}/chat/completions` | Ollama tag (`ollama list`) | Ollama running; default `base_url` `http://127.0.0.1:11434/v1` |
| `openai` | OpenAI Chat Completions | API model id | `OPENAI_API_KEY` or `CRITIQUE_API_KEY` |
| `openai-compatible` | Any OpenAI-style server | API model id | `base_url` (vLLM, LM Studio, Groq, …) |

Starters: [`config.example.json`](config.example.json), [`config.ollama.example.json`](config.ollama.example.json), [`config.openai.example.json`](config.openai.example.json), [`config.openai-compatible.example.json`](config.openai-compatible.example.json). `--headed` and `--cdp-url` apply only to `browser`.

---

## Setup and diagnostics

| Command | Meaning |
| --- | --- |
| `critique-bot setup --config PATH` | Local setup page on `127.0.0.1`; click elements in the chat UI to fill `selectors` |
| `critique-bot doctor --config PATH` | Check machine, config, login, selectors, and a live round trip |
| `critique-bot queue-status --config PATH` | Worker liveness, waiting/processing jobs, recent results |

**setup** serves a page with the standard library (no extra dependencies) and drives a real Edge window next to it. Click the prompt box, send button, a reply, and the stop button; it ranks candidate CSS selectors, writes them to your config, and can run a test prompt before you leave. Only for `backend: browser`.

Flags: `--config` (required; the file must already exist, copy an example first), `--port` (default `8765`; `0` picks a free one), `--no-open` to print the URL instead of launching your browser, `--logs`.

**doctor** never raises: each check reports `ok`, `warn`, `fail`, or `skip` with a hint. Warnings do not fail the run; the exit code is non-zero only when a check fails, so it works in a provisioning script.

Flags: `--config` (required), `--no-live` to skip anything that needs the browser or network, `--no-round-trip` to keep the live checks but not spend a real prompt, `--headed` to watch the browser (needed for a first login), `--json`, `--logs`.

**queue-status** exits non-zero when no worker heartbeat is fresh, so it doubles as a health check. Flags: `--config` (required), `--recent N` for how many finished jobs to list (default 10), `--json`, `--logs`.

---

## Production commands (GitLab / GitHub runner)

| Command | Where | Meaning |
| --- | --- | --- |
| `critique-bot worker --config PATH` | Runner PC, always on | One worker process; pulls jobs from `queue_dir` (browser: signed-in Edge; ollama/openai: HTTP) |
| `critique-bot submit --config PATH --patch-file diff.patch` | GitLab or GitHub job | Enqueue, wait, write `{output-dir}/review.md` |
| `critique-bot gitlab-post --review-file out/review.md --patch-file diff.patch` | GitLab job | Post inline comments + summary on the MR |
| `critique-bot github-post --review-file out/review.md --patch-file diff.patch` | GitHub job | Post inline comments + summary on the PR |

Worker flags: `--config` (required), `--headed`, `--cdp-url`, `--model`, `--logs` (default **on**).

Submit uses the same prompt/file flags as a one-shot review (`--patch-file`, `--file`, `--mode`, `--output-dir`, …). Extra: `--wait-timeout SEC` (default 1800), `--label NAME` (optional; default is GitLab MR IID, GitHub PR number, CI job id, or `local`). `--headed` is ignored. `--mode chat` is rejected.

Each submit creates its own job id and **only waits for that id**. The worker does not match by MR: it claims the oldest inbox file (FIFO, one at a time). GitLab/GitHub env (`CI_MERGE_REQUEST_IID`, `GITHUB_PR_NUMBER`, …) is stored on the job as `meta` and in the filename, e.g. `1735689600123-group-app-mr42-a1b2c3d4.json`.

If the worker is not running, submit exits immediately with an error. Config: `queue_dir`, `max_parallel_tabs` (default 1), `min_interval_seconds` (default 30), `interval_jitter_seconds` (default 5). Env: `CRITIQUE_QUEUE_DIR`, `CRITIQUE_MAX_PARALLEL_TABS`.

A job that fails is retried until it has been attempted `max_attempts` times (default 3), then marked failed so a broken browser session cannot spin the queue. Each job also gets a wall-clock limit (`job_timeout_seconds`, default derived from the reply timeouts); the worker fails it rather than hanging. Old result folders are pruned to the newest `result_retention` (default 200). Jobs still in flight when the worker stops are requeued so the next worker picks them up.

**gitlab-post** / **github-post** take the review that submit wrote and post it. Both strip the machine-readable JSON block from the summary, and with `--patch-file` they turn the review's file/line findings into inline comments on the diff. Shared flags: `--review-file` (required), `--patch-file`, `--api-url`, `--logs` (default **on**). GitLab adds `--project-id` and `--mr-iid`; GitHub adds `--repo owner/name` and `--pr NUMBER`. Everything else is read from CI env. They exit non-zero if the summary could not be posted, so a silent token failure does not pass as a green job.

Tokens: GitLab needs `CRITIQUE_GITLAB_TOKEN` (project access token, scope `api`) since `CI_JOB_TOKEN` cannot create notes. GitHub uses `GITHUB_TOKEN` (or `CRITIQUE_GITHUB_TOKEN`) with `pull-requests: write`.

---

## Modes

`--mode` selects how the bot builds the prompt. If you omit it, **review** is used unless you pass `--prompt` / `--prompt-file` (those select **general**).

| Mode | When to use | Prompt | Files | Output |
| --- | --- | --- | --- | --- |
| `review` (default) | Specialized code review | Review template ([`prompts/review.txt`](prompts/review.txt) or `--prompt-template`) | Patch required (`--patch-file`, `FILE`, or stdin) | `{output-dir}/review.md` + `review.json` |
| `general` | One-shot question | `--prompt` or `--prompt-file` (required) | Optional | `{output-dir}/reply.md` + `reply.json` |
| `chat` | Interactive conversation | Optional first message via `--prompt` / `--prompt-file` | Optional on the first turn; more via `/file` | `{output-dir}/chat.md` + `chat.json` |

`--mode review` cannot be combined with `--prompt` / `--prompt-file`. `--prompt-template` is review-only. `--prompt` and `--prompt-file` cannot be used together.

Chat mode is headless unless you pass `--headed`.

### Placeholders

In **review** templates, `{patch}` is required and is replaced with the patch/file body.

In **general** and **chat**, if the prompt contains `{files}` or `{patch}`, those are replaced with attached file contents. Otherwise files are appended after the prompt, labeled `--- file: <path> ---`.

---

## CLI flags

| Flag | Modes | Meaning |
| --- | --- | --- |
| `--config PATH` | all | JSON config (required). See [`config.example.json`](config.example.json). |
| `--mode {review,general,chat}` | all | Mode. Default: `review`, or `general` if `--prompt` / `--prompt-file` is set. |
| `--prompt TEXT` | general, chat | Prompt text. First message in chat. |
| `--prompt-file PATH` | general, chat | Read prompt text from a file. |
| `--file PATH` | all | Attach a UTF-8 file (repeatable). Patch, source, or any text file. |
| `FILE ...` | all | Trailing paths; same as `--file`. |
| `--patch-file PATH` | all | Patch/diff to include. In review, omit this to read a patch from stdin. |
| `--prompt-template PATH` | review | Template with a `{patch}` placeholder. |
| `--output-dir DIR` | all | Where replies and failure screenshots go. Default: `out`. |
| `--headed` | all | Show the Edge window (`browser` backend). Ignored for HTTP backends. |
| `--cdp-url URL` | all | Attach to a running Edge, e.g. `http://127.0.0.1:9222` (`browser` only). |
| `--model NAME` | all | Override the config/env model (dropdown label, Ollama tag, or API model id). |
| `--logs` / `--no-logs` | all | Diagnostic logs on stderr. Default: off (on for `worker`). A spinner shows while waiting for the assistant. |
| `--wait-timeout SEC` | submit | Seconds to wait for the worker (default 1800). |
| `--label NAME` | submit | Override the job slug in the queue filename. Default: MR/PR/CI id or `local`. |
| `-h` / `--help` | all | Print CLI help. |

---

## Chat session commands

Used only after `--mode chat` is running (`You>` prompt).

| Command | Meaning |
| --- | --- |
| any other text | Send that message |
| `exit`, `quit`, `/exit`, `/quit`, `/q` | Leave the session |
| Ctrl-D (Linux) / Ctrl-Z then Enter (Windows) | End of input; same as quit |
| Ctrl-C | Abort the session |
| `/help` | Print in-session help |
| `/file PATH [text]` | Attach a file to this turn (optional extra prompt after the path) |
| line ending with `\` | Continue on the next line (`... `) |

---

## Linux (bash)

Activate a checkout venv: `source .venv/bin/activate`

### Review

```bash
python -m critique_bot --config config.json --patch-file diff.patch --output-dir ./out
```

```bash
python -m critique_bot --config config.json --headed --patch-file diff.patch
```

```bash
git diff | python -m critique_bot --config config.json
```

```bash
python -m critique_bot --config config.json \
  --prompt-template prompts/review.txt \
  --file src/cli.py \
  src/config.py
```

### General

```bash
python -m critique_bot --config config.json --mode general \
  --prompt "Summarize this change and list risks" \
  diff.patch
```

```bash
python -m critique_bot --config config.json --mode general \
  --prompt-file instructions.txt \
  --file src/cli.py \
  src/config.py
```

`--prompt` also selects general if `--mode` is omitted:

```bash
python -m critique_bot --config config.json --prompt "What does this do?" src/cli.py
```

### Chat

```bash
python -m critique_bot --config config.json --mode chat
```

```bash
python -m critique_bot --config config.json --mode chat --headed
```

```bash
python -m critique_bot --config config.json --mode chat \
  --prompt "Let's go through this file" \
  src/cli.py
```

Inside the session:

```text
You> what's a good name for this project?
You> /file src/cli.py explain the entry point
You> this is a long \
... question continued on the next line
You> exit
```

### Worker / submit (runner)

```bash
python -m critique_bot worker --config /opt/critique-bot/config.json --logs
```

```bash
python -m critique_bot submit --config /opt/critique-bot/config.json \
  --patch-file diff.patch --output-dir ./out
```

```bash
python -m critique_bot queue-status --config /opt/critique-bot/config.json
```

```bash
python -m critique_bot gitlab-post --review-file ./out/review.md --patch-file diff.patch
```

```bash
python -m critique_bot github-post --review-file ./out/review.md --patch-file diff.patch
```

### Setup / doctor

```bash
python -m critique_bot setup --config config.json
```

```bash
python -m critique_bot doctor --config config.json
```

```bash
python -m critique_bot doctor --config config.json --no-live --json
```

### Other

```bash
python -m critique_bot --help
```

```bash
python -m critique_bot --config config.json --model "GPT-5.1" --patch-file diff.patch
```

```bash
python -m critique_bot --config config.json --cdp-url http://127.0.0.1:9222 --patch-file diff.patch
```

```bash
export CRITIQUE_MODEL="GPT-5.1"
export CRITIQUE_CHAT_URL="https://YOUR_CHAT_UI/"
python -m critique_bot --config config.json --mode chat
```

Standalone zip (after unzip):

```bash
./critique-bot --config config.json --mode chat
```

---

## Windows PowerShell

Activate a checkout venv: `.venv\Scripts\Activate.ps1`

If script execution is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Review

```powershell
python -m critique_bot --config config.json --patch-file diff.patch --output-dir .\out
```

```powershell
python -m critique_bot --config config.json --headed --patch-file diff.patch
```

```powershell
git diff | python -m critique_bot --config config.json
```

```powershell
Get-Content -Raw diff.patch | python -m critique_bot --config config.json
```

```powershell
python -m critique_bot --config config.json `
  --prompt-template prompts\review.txt `
  --file src\cli.py `
  src\config.py
```

### General

```powershell
python -m critique_bot --config config.json --mode general `
  --prompt "Summarize this change and list risks" `
  diff.patch
```

```powershell
python -m critique_bot --config config.json --mode general `
  --prompt-file instructions.txt `
  --file src\cli.py `
  src\config.py
```

`--prompt` also selects general if `--mode` is omitted:

```powershell
python -m critique_bot --config config.json --prompt "What does this do?" src\cli.py
```

### Chat

```powershell
python -m critique_bot --config config.json --mode chat
```

```powershell
python -m critique_bot --config config.json --mode chat --headed
```

```powershell
python -m critique_bot --config config.json --mode chat `
  --prompt "Let's go through this file" `
  src\cli.py
```

Inside the session:

```text
You> what's a good name for this project?
You> /file src\cli.py explain the entry point
You> this is a long \
... question continued on the next line
You> exit
```

To end with EOF instead of `exit`: press **Ctrl-Z**, then **Enter**.

### Worker / submit (runner)

```powershell
python -m critique_bot worker --config C:\critique-bot\config.json --logs
```

```powershell
python -m critique_bot submit --config C:\critique-bot\config.json `
  --patch-file diff.patch --output-dir .\out
```

### Other

```powershell
python -m critique_bot --help
```

```powershell
python -m critique_bot --config config.json --model "GPT-5.1" --patch-file diff.patch
```

```powershell
python -m critique_bot --config config.json --cdp-url http://127.0.0.1:9222 --patch-file diff.patch
```

```powershell
$env:CRITIQUE_MODEL = "GPT-5.1"
$env:CRITIQUE_CHAT_URL = "https://YOUR_CHAT_UI/"
python -m critique_bot --config config.json --mode chat
```

Standalone zip (after unzip):

```powershell
.\critique-bot.exe --config config.json --mode chat
```

---

## Environment variables

These override matching fields in `config.json`.

| Variable | Overrides |
| --- | --- |
| `CRITIQUE_CHAT_URL` | `url` |
| `CRITIQUE_MODEL` | `model` |
| `CRITIQUE_STORAGE_STATE` | `storage_state` |
| `CRITIQUE_USER_DATA_DIR` | `user_data_dir` |
| `CRITIQUE_CDP_URL` | `cdp_url` |
| `CRITIQUE_QUEUE_DIR` | `queue_dir` |
| `CRITIQUE_MAX_PARALLEL_TABS` | `max_parallel_tabs` |
| `CRITIQUE_BACKEND` | `backend` |
| `CRITIQUE_BASE_URL` | `base_url` |
| `CRITIQUE_API_KEY` (or `OPENAI_API_KEY`) | API key for `openai` / `openai-compatible` |

Linux: `export NAME=value`. PowerShell: `$env:NAME = "value"`.

Posting tokens are read from the environment only: `CRITIQUE_GITLAB_TOKEN` for `gitlab-post`, `GITHUB_TOKEN` or `CRITIQUE_GITHUB_TOKEN` for `github-post`.

The worker limits (`max_attempts`, `result_retention`, `job_timeout_seconds`) are config-only. See [`docs/config.json.md`](docs/config.json.md).

---

## Outputs

Default `--output-dir` is `out`.

| Mode | Written on success | Written on failure |
| --- | --- | --- |
| review | `review.md`, `review.json` | `screenshot.png`, `page.html` |
| general | `reply.md`, `reply.json` | same |
| chat | `chat.md`, `chat.json` (skipped if you quit with no turns) | same |
| submit | same as the mode, in `--output-dir` (copied from the worker queue), plus `status.json` and `job.json` (MR/PR label and CI meta) | same, plus `status.json` and `job.json` |

Diagnostic logs are **off** by default; pass `--logs` to write them to **stderr**. While waiting for the assistant, a spinner is shown on stderr (hidden when `--logs` is on, since log lines already show progress). The assistant reply (or chat transcript) is printed to **stdout**.
