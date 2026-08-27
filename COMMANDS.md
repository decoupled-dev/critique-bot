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

## Modes

`--mode` selects how the bot builds the prompt. If you omit it, **review** is used unless you pass `--prompt` / `--prompt-file` (those select **general**).

| Mode | When to use | Prompt | Files | Output |
| --- | --- | --- | --- | --- |
| `review` (default) | Specialized code review | Review template ([`prompts/review.txt`](prompts/review.txt) or `--prompt-template`) | Patch required (`--patch-file`, `FILE`, or stdin) | `{output-dir}/review.md` + `review.json` |
| `general` | One-shot question | `--prompt` or `--prompt-file` (required) | Optional | `{output-dir}/reply.md` + `reply.json` |
| `chat` | Interactive conversation | Optional first message via `--prompt` / `--prompt-file` | Optional on the first turn; more via `/file` | `{output-dir}/chat.md` + `chat.json` |

`--mode review` cannot be combined with `--prompt` / `--prompt-file`. `--prompt-template` is review-only. `--prompt` and `--prompt-file` cannot be used together.

`chat` opens the browser window (same as `--headed`).

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
| `--headed` | all | Show the Edge window. Implied by `--mode chat`. Use this for first login. |
| `--cdp-url URL` | all | Attach to a running Edge, e.g. `http://127.0.0.1:9222`. |
| `--model NAME` | all | Override the config/env model dropdown label. |
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

Linux: `export NAME=value`. PowerShell: `$env:NAME = "value"`.

---

## Outputs

Default `--output-dir` is `out`.

| Mode | Written on success | Written on failure |
| --- | --- | --- |
| review | `review.md`, `review.json` | `screenshot.png`, `page.html` |
| general | `reply.md`, `reply.json` | same |
| chat | `chat.md`, `chat.json` (skipped if you quit with no turns) | same |

Logs go to **stderr**. The assistant reply (or chat transcript) is printed to **stdout**.
