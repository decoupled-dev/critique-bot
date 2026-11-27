# Commands

Reference for **modes**, **CLI flags**, and **in-session chat commands**. Examples are given for **Linux (bash)** and **Windows PowerShell**.

`--config PATH` names a config file. Without it, crit uses the one the project has stored, or your own settings in `~/.crit/config.json` (the first run asks the questions that create them); which file a command uses is in [`docs/config.json.md`](docs/config.json.md).

How you run crit:

| How | Linux (bash) | Windows PowerShell |
| --- | --- | --- |
| Installed (`.deb`, `crit install`, setup script, or `pip install -e .`) | `crit ...` | `crit ...` |
| From a checkout without installing | `crit ...` | `crit ...` |
| Standalone zip, not installed yet | `./crit ...` | `.\crit.exe ...` |

The examples below use `crit`; the flags are the same with the other forms.

Line continuation: bash uses `\`, PowerShell uses `` ` ``.

---

## Setup

| Command | Meaning |
| --- | --- |
| `crit setup --config PATH` | Local setup page on `127.0.0.1`; click elements in the chat UI to fill `selectors` |
| `crit queue-status --config PATH` | Worker liveness, waiting/processing jobs, recent results |

**setup** serves a page with the standard library (no extra dependencies) and drives a real Edge window next to it. Click the prompt box, send button, a reply, and the stop button; it ranks candidate CSS selectors, writes them to your config, and can run a test prompt before you leave.

Flags: `--config` (required; the file must already exist, copy an example first), `--port` (default `8765`; `0` picks a free one), `--no-open` to print the URL instead of launching your browser, `--logs`.

**queue-status** exits non-zero when no worker heartbeat is fresh, so it doubles as a health check. Flags: `--config` (required), `--recent N` for how many finished jobs to list (default 10), `--json`, `--logs`.

---

## Production commands (GitLab runner)

| Command | Where | Meaning |
| --- | --- | --- |
| `crit worker --config PATH` | Runner PC, always on | One worker process; pulls jobs from `queue_dir` (one signed-in Edge) |
| `crit submit --config PATH` | GitLab job | Build diff + file context from the checkout, enqueue, wait, write `{output-dir}/review.md` |
| `crit gitlab-post --review-file out/review.md --patch-file diff.patch` | GitLab job | Post inline comments + summary on the MR |

Worker flags: `--config` (required), `--headed`, `--cdp-url`, `--model`, `--logs` (default **on**).

Submit uses the same prompt/file flags as a one-shot review (`--patch-file`, `--file`, `--mode`, `--output-dir`, …). Extra: `--wait-timeout SEC` (default 1800), `--label NAME` (optional; default is GitLab MR IID, CI job id, or `local`). `--headed` is ignored. `--mode chat` is rejected. In GitLab CI, omit `--patch-file` so submit builds `diff.patch` from the job checkout and always loads changed file bodies. Small reviews stay one paste (instructions, then HEAD files, then the patch, in that order). If that would exceed `max_prompt_chars`, the worker sends **instructions first**, then each HEAD file (ACK means the file is kept for the review, not ignored), then the patch last. Markdown/RST skipped. `--include-changed-files` is always on in review mode; `--repo-dir` does the same against a local clone.

Each submit creates its own job id and **only waits for that id**. The worker does not match by MR: it claims the oldest inbox file (FIFO, one at a time). GitLab env (`CI_MERGE_REQUEST_IID`, `CI_PROJECT_PATH`, …) is stored on the job as `meta` and in the filename, e.g. `1735689600123-group-app-mr42-a1b2c3d4.json`.

If the worker is not running, submit exits immediately with an error. Config: `queue_dir`, `max_parallel_tabs` (default 1), `min_interval_seconds` (default 30), `interval_jitter_seconds` (default 5). Env: `CRIT_QUEUE_DIR`, `CRIT_MAX_PARALLEL_TABS`.

A job that fails is retried until it has been attempted `max_attempts` times (default 3), then marked failed so a broken browser session cannot spin the queue. Each job also gets a wall-clock limit (`job_timeout_seconds`, default derived from the reply timeouts); the worker fails it rather than hanging. Old result folders are pruned to the newest `result_retention` (default 200). Jobs still in flight when the worker stops are requeued so the next worker picks them up.

**gitlab-post** takes the review that submit wrote and posts it. It strips the machine-readable JSON block from the summary, and with `--patch-file` it turns the review's file/line findings into inline comments on the diff. Flags: `--review-file` (required), `--patch-file`, `--api-url`, `--project-id`, `--mr-iid`, `--logs` (default **on**). Everything else is read from GitLab CI env. It exits non-zero if the summary could not be posted, so a silent token failure does not pass as a green job.

Tokens: GitLab needs `CRIT_GITLAB_TOKEN` (project access token, scope `api`) since `CI_JOB_TOKEN` cannot create notes.

---

## Modes

One command, `crit`, runs every mode. The first word picks it; with no mode word, `crit` runs the coding agent:

```bash
crit "fix the failing test"                 # agent (default)
crit review --patch-file diff.patch         # code review (reads the project with read-only tools)
crit review --quick --patch-file diff.patch # one-shot review: one message, no tools
git diff | crit review                      # review from stdin
crit ask --prompt "Summarize this" notes.txt   # one-shot (alias: crit general)
crit chat                                   # interactive conversation
crit setup | worker | submit | queue-status | gitlab-post
```

`review`, `ask`, and `chat` use the `config.json` that `crit` stored for the project when `--config` is not given. A task that starts with a mode word must be quoted (`crit "review the auth code"`); unquoted, `review` is read as the mode.

`crit review` is the agent review: the Code Reviewer reads the changed files, callers, and tests with read-only tools (up to `--max-rounds`, default 20) and then sends the review in the usual format. Edits, commands, fetches, and helper chats are refused. `--quick` is the one-shot review. `submit` uses the one-shot review, and `submit --deep` is refused (the worker runs one-shot reviews only).

`--mode MODE` still works and means the same as the mode word; using both at once is an error.

| Mode | When to use | Prompt | Files | Output |
| --- | --- | --- | --- | --- |
| `review` | Specialized code review | Review template ([`prompts/review.txt`](prompts/review.txt) or `--prompt-template`) | Patch required (`--patch-file`, `FILE`, or stdin). HEAD files + patch are inlined when they fit (instructions, then files, then patch). Otherwise: instructions, then one file per chat turn, then the patch | `{output-dir}/review.md` + `review.json` |
| `general` | One-shot question | `--prompt` or `--prompt-file` (required) | Optional | `{output-dir}/reply.md` + `reply.json` |
| `chat` | Interactive conversation | Optional first message via `--prompt` / `--prompt-file` | Optional on the first turn; more via `/file` | `{output-dir}/chat.md` + `chat.json` |
| `agent` (default) | Local coding harness: `crit` with no mode word | Positional task, or `--prompt` | The model reads files itself with tools | `.crit/sessions/<stamp>/agent.md` + `agent.json` |

`--mode review` cannot be combined with `--prompt` / `--prompt-file`. `--prompt-template` is review-only. `--prompt` and `--prompt-file` cannot be used together.

Chat mode is headless when an Edge profile already exists. A missing profile opens the window so you can sign in, then closes it and continues headless. Pass `--headed` to show the window anyway. While a reply is coming, the spinner says `Thinking...`.

### Placeholders

In **review** templates, `{patch}` is required (the unified diff). `{files}` is the HEAD (original/current) contents of changed files when they fit in one paste; otherwise the worker sends instructions first, those files one per chat turn, then the patch. `{mr_context}` is GitLab MR metadata.

In **general** and **chat**, if the prompt contains `{files}` or `{patch}`, those are replaced with attached file contents. Otherwise files are appended after the prompt, labeled `--- file: <path> ---`.

---

## CLI flags

| Flag | Modes | Meaning |
| --- | --- | --- |
| `--config PATH` | all | JSON config (optional for `crit`, `review`, `ask`, and `chat`; see [Agent](#agent-crit)). See [`config.example.json`](config.example.json), or [`config.local.example.json`](config.local.example.json) for a model served on this PC (`backend: local`). |
| `--mode {review,general,chat,agent}` | all | Mode, the same as the mode word. Default: `agent`. |
| `--prompt TEXT` | general, chat, agent | Prompt text. First message in chat; the task in agent. |
| `--prompt-file PATH` | general, chat, agent | Read prompt text from a file. |
| `--file PATH` | all | Attach a UTF-8 file (repeatable). Patch, source, or any text file. |
| `FILE ...` | all | Trailing paths; same as `--file`. |
| `--patch-file PATH` | all | Patch/diff to include. In review, omit in GitLab CI to build it from the checkout; locally, omit to read stdin. |
| `--include-changed-files` | review | Always on in review mode (kept for older scripts). Loads HEAD contents of paths in the patch from `--repo-dir`. Inlined when they fit one paste; otherwise sent one per chat turn after the instructions and before the patch (ACK = keep for review). Markdown/RST skipped. |
| `--repo-dir DIR` | review | Checkout to read changed files from (default: current directory). |
| `--write-patch PATH` | review | Where to write a generated git diff (default: `diff.patch`). |
| `--prompt-template PATH` | review | Template with a `{patch}` placeholder. |
| `--output-dir DIR` | all | Where replies and failure screenshots go. Default: `out`. |
| `--headed` | all | Show the Edge window. A missing profile does this on its own. Does nothing with `backend: local`. |
| `--cdp-url URL` | all | Attach to a running Edge, e.g. `http://127.0.0.1:9222`. |
| `--model NAME` | all | Override the config/env model (visible dropdown label, or the local model's name such as `qwen2.5-coder:14b`). |
| `--logs` / `--no-logs` | all | Also print the log lines on stderr (they always go to `~/.crit/logs/crit.log`). Default: off (on for `worker`). A spinner shows while waiting for the assistant. |
| `--yes` / `--auto` (`crit -y`) | agent | Auto mode: run edits, commands, and web fetches without asking; risky commands still ask. Same as `"permissions": "auto"` in `.crit/settings.json`. |
| `--plan` | agent | Plan mode: read the code and send a plan; you approve it before anything changes. |
| `--permission-mode MODE` | agent | Start in `ask`, `edits` (accept file edits), `auto`, or `plan`. |
| `--role NAME` | agent | The role crit plays: `code-reviewer`, `aosp-expert`, `expert-developer`, `ai-expert`, `test-engineer`, `android-app-engineer`, or `generic` (any spelling, e.g. `AOSPExpert`). Stored in `.crit/settings.json` when the project has none yet; otherwise for this run only. Otherwise the first `crit` in a folder picks one from the project's files. |
| `--max-rounds N` | agent | Optional cap on tool rounds per task. Omit to run until the model stops. |
| `--wait-timeout SEC` | submit | Seconds to wait for the worker (default 1800). |
| `--label NAME` | submit | Override the job slug in the queue filename. Default: GitLab MR IID, CI job id, or `local`. |
| `-h` / `--help` | all | Print CLI help. |

---

## Chat session commands

Used only after `crit chat` is running (`You>` prompt).

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

## Agent (`crit`)

`crit` with no mode word is the agent (`--mode agent`). Full guide: [`docs/agent.md`](docs/agent.md).

Install it and put `crit` on `PATH` with the setup script (options: [`docs/agent.md`](docs/agent.md#quick-setup-with-the-script)):

```bash
scripts/setup-crit.sh
scripts/setup-crit.sh --with-index --install-deps
scripts/setup-crit.sh --proxy http://username:password@10.1.2.3:8080
```

```powershell
.\scripts\setup-crit.ps1
powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1 -WithIndex
.\scripts\setup-crit.ps1 -Proxy "http://username:password@10.1.2.3:8080"
.\scripts\setup-crit.ps1 -ProxyHost 10.1.2.3 -ProxyPort 8080 -ProxyUser "CORP\jdoe"
.\scripts\setup-crit.ps1 -ProxyHost proxy.corp.example -ProxyPort 3128 -CaFile C:\certs\corp-root.pem
```

The script's `crit` always passes `--config`. Without the script, pass `--config` on the first `crit` in a folder and it is stored in `.crit/settings.json`. A `config.json` already in the folder when `.crit` is created is recorded the same way.

| Command | Meaning |
| --- | --- |
| `crit` | Open the session screen and wait for a task |
| `crit "TASK"` | Run that task, then wait for the next one |
| `crit --yes "TASK"` | Auto mode: no approval prompts except risky commands (also `--auto`, `-y`) |
| `crit --plan "TASK"` | Plan first; approve the plan, then crit carries it out |
| `crit --headed "TASK"` | Show the browser window |
| `crit undo` | Restore the files the last task changed. Run again to step back further |
| `crit init` | Create `.crit/`, or rebuild the index without replacing settings |

The same commands in both shells:

```bash
cd ~/my-project
crit
crit "update the test cases"
crit --yes "fix the failing test"
crit undo
crit init
```

```powershell
cd C:\my-project
crit
crit "update the test cases"
crit --yes "fix the failing test"
crit undo
crit init
```

The first `crit` in a folder creates `.crit/` (or under `--repo-dir`), shows the text-style screen, and asks you to sign in. It opens the browser only when the profile is not signed in yet. A task run from a subdirectory finds `.crit` by walking upward. A project's old `.bot/` folder is renamed to `.crit/` on first use (when both exist, `.crit` is used). When the project is your home folder itself, its data lives in `~/.crit/projects/home/`.

Inside a session (the `>` prompt):

| Input | Meaning |
| --- | --- |
| any other text | Send that task |
| `@path` | Complete a file or folder path; mentioned files are listed for the model |
| `/help` | Commands and shortcuts |
| `/new` | Start a fresh chat |
| `/clear` | Clear the screen |
| `/undo` | Restore the files the last task changed |
| Shift+Tab, Alt+M | Switch mode: ask, accept edits, auto, plan |
| `/mode [name]` | List the modes or switch (`/permissions` is the same) |
| `/plan`, `/auto` | Switch to plan mode or auto mode |
| `/skills [name ...]` | List skills, or pin some for this session (`/skills off name` unpins) |
| `/agent [name]` | List the agents (the built-in roles and `.crit/agents`, `~/.crit/agents`), or switch to one (kept in `.crit/settings.json`; the next task starts a new chat) |
| `/role [name]` | Same as `/agent` |
| `/<command> [args]` | A command from `.crit/commands/<command>.md` or `~/.crit/commands/` ([docs/extending.md](docs/extending.md)) |
| `/status` | Model, shell, folder, mode, role, pinned skills, background commands |
| `/commands` | Shell commands run this session, with exit codes, times, and retries |
| `/shell [name]` | List the shells, or switch the default for this session |
| `/tools` | The tools the model can call |
| `/theme` | Change the text style |
| `/exit`, `exit`, `quit`, Ctrl+D | Leave |
| Ctrl+C | Clear the input; during a task, stop it; twice on an empty line, leave |
| Alt+Enter, Ctrl+J, trailing `\` | New line |

The instructions from `prompts/agent.txt` go out with the first task in one message. Tool results go back until the model finishes. A question with no file change is answered in words and that task ends. A refusal, or a question that only hands the task back, is sent back until the model calls a tool or finishes. A real question that needs your decision is shown to you. A reply of COMPLETED or DONE, or a reply that no edit is needed, ends that task. `--max-rounds` is an optional cap for a loop that never answers. Each session is saved to `.crit/sessions/<stamp>/agent.md` and `agent.json`.

---

## Linux (bash)

Activate a checkout venv: `source .venv/bin/activate`

### Review

```bash
crit review --quick --patch-file diff.patch --output-dir ./out
```

```bash
crit review --quick --headed --patch-file diff.patch
```

```bash
git diff | crit review --quick
```

```bash
crit review --quick \
  --prompt-template prompts/review.txt \
  --file src/cli.py \
  src/config.py
```

### General

```bash
crit ask --prompt "Summarize this change and list risks" \
  diff.patch
```

```bash
crit ask --prompt-file instructions.txt \
  --file src/cli.py \
  src/config.py
```

A one-shot question about a file:

```bash
crit ask --prompt "What does this do?" src/cli.py
```

### Chat

```bash
crit chat
```

```bash
crit chat --headed
```

```bash
crit chat --prompt "Let's go through this file" \
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
crit worker --config /opt/crit/config.json --logs
```

```bash
crit submit --config /opt/crit/config.json \
  --output-dir ./out
```

```bash
crit queue-status --config /opt/crit/config.json
```

```bash
crit gitlab-post --review-file ./out/review.md --patch-file diff.patch
```

### Setup

```bash
crit setup --config config.json
```

### Other

```bash
crit --help
```

```bash
# Extensions (docs/extending.md): approve a project's tools/hooks/MCP servers, test a tool, pin MCP tools
crit trust
crit trust .crit/tools/adb_logcat.toml
crit tool list
crit tool test adb_logcat --args '{"filter": "*:E"}'
crit mcp list
crit mcp approve git
```

```bash
crit review --quick --model "GPT-5.1" --patch-file diff.patch
```

```bash
crit review --quick --cdp-url http://127.0.0.1:9222 --patch-file diff.patch
```

```bash
export CRIT_MODEL="GPT-5.1"
export CRIT_URL="https://YOUR_CHAT_UI/"
crit chat
```

Standalone zip (after unzip, before it is installed):

```bash
./crit chat --config config.json
```

---

## Windows PowerShell

Activate a checkout venv: `.venv\Scripts\Activate.ps1`

If script execution is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Review

```powershell
crit review --quick --patch-file diff.patch --output-dir .\out
```

```powershell
crit review --quick --headed --patch-file diff.patch
```

```powershell
git diff | crit review --quick
```

```powershell
Get-Content -Raw diff.patch | crit review --quick
```

```powershell
crit review --quick `
  --prompt-template prompts\review.txt `
  --file src\cli.py `
  src\config.py
```

### General

```powershell
crit ask --prompt "Summarize this change and list risks" `
  diff.patch
```

```powershell
crit ask --prompt-file instructions.txt `
  --file src\cli.py `
  src\config.py
```

A one-shot question about a file:

```powershell
crit ask --prompt "What does this do?" src\cli.py
```

### Chat

```powershell
crit chat
```

```powershell
crit chat --headed
```

```powershell
crit chat --prompt "Let's go through this file" `
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
crit worker --config C:\crit\config.json --logs
```

```powershell
crit submit --config C:\crit\config.json `
  --output-dir .\out
```

### Other

```powershell
crit --help
```

```powershell
crit review --quick --model "GPT-5.1" --patch-file diff.patch
```

```powershell
crit review --quick --cdp-url http://127.0.0.1:9222 --patch-file diff.patch
```

```powershell
$env:CRIT_MODEL = "GPT-5.1"
$env:CRIT_URL = "https://YOUR_CHAT_UI/"
crit chat
```

Standalone zip (after unzip, before it is installed):

```powershell
.\crit.exe chat --config config.json
```

---

## Environment variables

These override matching fields in `config.json`.

| Variable | Overrides |
| --- | --- |
| `CRIT_URL` | `url` |
| `CRIT_MODEL` | `model` |
| `CRIT_STORAGE_STATE` | `storage_state` |
| `CRIT_USER_DATA_DIR` | `user_data_dir` |
| `CRIT_CDP_URL` | `cdp_url` |
| `CRIT_QUEUE_DIR` | `queue_dir` |
| `CRIT_MAX_PARALLEL_TABS` | `max_parallel_tabs` |
| `CRIT_API_KEY` | `local.api_key` (the local server's API key) |

Linux: `export NAME=value`. PowerShell: `$env:NAME = "value"`.

Posting tokens are read from the environment only: `CRIT_GITLAB_TOKEN` for `gitlab-post`.

The worker limits (`max_attempts`, `result_retention`, `job_timeout_seconds`) are config-only. See [`docs/config.json.md`](docs/config.json.md).

---

## Outputs

Default `--output-dir` is `out`.

| Mode | Written on success | Written on failure |
| --- | --- | --- |
| review | `review.md`, `review.json` | `screenshot.png`, `page.html` |
| general | `reply.md`, `reply.json` | same |
| chat | `chat.md`, `chat.json` (skipped if you quit with no turns) | same |
| submit | same as the mode, in `--output-dir` (copied from the worker queue), plus `status.json` and `job.json` (MR label and CI meta) | same, plus `status.json` and `job.json` |

Diagnostic logs always go to `~/.crit/logs/crit.log` (owner-only, rotated at about 2 MB, 3 files kept, redacted like outgoing messages); pass `--logs` to also write them to **stderr**. On screen, a normal run shows only questions, the agent's screen, install/uninstall results, and one `Problem: ...` or `crit stopped: ... Details: ~/.crit/logs/crit.log` line when it cannot continue. While waiting for the assistant, a spinner is shown on stderr (hidden when `--logs` is on, since log lines already show progress). The assistant reply (or chat transcript) is printed to **stdout**.
