# Deploy critique-bot (Windows and Linux)

Ship a **zip per OS** (no Python on the target) or a **pip wheel**. The bot needs Microsoft Edge on the target (`microsoft-edge-stable` on Linux; Edge is usually already on Windows).

Build **on the OS you want to ship**. A Linux binary will not run on Windows, and vice versa. PyInstaller cannot cross-compile this project.

| You are on | Shell | Binary in the zip |
| --- | --- | --- |
| Linux | bash / zsh | `critique-bot` |
| Windows | PowerShell | `critique-bot.exe` |

The zip bundles Python, Playwright’s Node driver, `config.example.json`, and the review template. It does **not** bundle Edge, and it does not bundle Playwright’s Chromium/Firefox/WebKit downloads. The bot drives system Edge (`channel=msedge`).

## Target requirements

- 64-bit Windows or Linux
- Microsoft Edge installed
- On Linux CI or headless servers you may also need Playwright OS libraries: `playwright install-deps` (only if you install via pip, not for the zip)
- Linux zips from GitHub Actions are built on Ubuntu 22.04. They need a glibc at least as new as that host (Ubuntu 22.04+, Debian 12+, RHEL 9+, or similar)

On Windows, use **PowerShell** (not Command Prompt). If `python` is missing, use the [Python launcher](https://docs.python.org/3/using/windows.html#python-launcher-for-windows) `py -3` in place of `python`.

## Standalone zip

### Build locally

Python 3.10+ on the build machine.

**Linux (bash)**

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[packaging]"
python scripts/build.py
```

Output: `dist/critique-bot-<version>-linux-x64.zip` (or `linux-arm64`).

**Windows (PowerShell)**

If scripts are blocked, allow them for this session only, then activate:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[packaging]'
python scripts/build.py
```

PowerShell treats `[` `]` as wildcards, so the extras spec must be quoted: `'.[packaging]'`.

Output: `dist/critique-bot-<version>-windows-x64.zip`.

`--skip-smoke` skips the bundled `--help` check after PyInstaller:

```bash
python scripts/build.py --skip-smoke
```

```powershell
python scripts/build.py --skip-smoke
```

`scripts/build.py` uses [`packaging/critique-bot.spec`](packaging/critique-bot.spec). Do not delete `_internal` after unzipping; the launcher needs it.

### Zip layout

**Linux**

```text
critique-bot-<version>-linux-x64/
  critique-bot
  _internal/
  config.example.json
  prompts/review.txt
  README.txt
```

**Windows**

```text
critique-bot-<version>-windows-x64\
  critique-bot.exe
  _internal\
  config.example.json
  prompts\review.txt
  README.txt
```

### Unpack and configure

**Linux (bash)**

```bash
unzip critique-bot-0.1.0-linux-x64.zip
cd critique-bot-0.1.0-linux-x64
cp config.example.json config.json
```

**Windows (PowerShell)**

```powershell
Expand-Archive .\critique-bot-0.1.0-windows-x64.zip -DestinationPath .
Set-Location .\critique-bot-0.1.0-windows-x64
Copy-Item .\config.example.json .\config.json
```

Edit `config.json` and set the chat URL plus CSS selectors. Keep `critique-bot` / `critique-bot.exe` next to `_internal`.

Instead of editing selectors by hand, run the setup page. It opens Edge on your chat URL and lets you click the prompt box, send button, a reply, and the stop button:

```bash
./critique-bot setup --config config.json          # Linux
.\critique-bot.exe setup --config config.json      # Windows
```

That serves on `127.0.0.1:8765` and writes the selectors it picks straight into `config.json`. Use `--port 0` if that port is taken and `--no-open` on a machine where you would rather open the printed URL yourself.

### First login (headed)

Later runs can omit `--headed`. The Edge profile defaults to `.edge-profile` in the current working directory.

**Linux (bash)**

```bash
./critique-bot --config config.json --headed --mode general --prompt "hello"
```

**Windows (PowerShell)**

```powershell
.\critique-bot.exe --config config.json --headed --mode general --prompt "hello"
```

### Verify the install

Send a one-shot prompt through the signed-in chat page. Use `--headed` on the first login.

```bash
./critique-bot --config config.json --mode general \
  --prompt "Reply with exactly one word: PONG." --headed
```

```powershell
.\critique-bot.exe --config config.json --mode general `
  --prompt "Reply with exactly one word: PONG." --headed
```

Or use **Send test prompt** in `critique-bot setup --config config.json`.

### Review a patch

**Linux (bash)**

```bash
./critique-bot --config config.json --patch-file diff.patch
```

**Windows (PowerShell)**

```powershell
.\critique-bot.exe --config config.json --patch-file diff.patch
```

### General prompt

**Linux (bash)**

```bash
./critique-bot --config config.json --mode general \
  --prompt "Summarize this" \
  notes.txt
```

**Windows (PowerShell)**

Line continuation is a backtick at the end of the line (no spaces after it):

```powershell
.\critique-bot.exe --config config.json --mode general `
  --prompt "Summarize this" `
  notes.txt
```

Or one line:

```powershell
.\critique-bot.exe --config config.json --mode general --prompt "Summarize this" notes.txt
```

## GitHub Actions (Linux + Windows)

[`.github/workflows/build.yml`](.github/workflows/build.yml) ships Linux/Windows zips and a pip wheel on GitHub-hosted runners.

**Build** trigger:

| Trigger | What happens |
| --- | --- |
| Actions tab → **Run workflow** | Uploads Linux zip, Windows zip, and a pip wheel as artifacts |
| Push a `v*` tag (for example `v0.1.0`) | Same builds, then attaches them to a GitHub Release |

**Linux (bash)**

```bash
git tag v0.1.0
git push origin v0.1.0
```

**Windows (PowerShell)**

```powershell
git tag v0.1.0
git push origin v0.1.0
```

Download artifacts from the workflow run, or from the Release page after tagging.

## Pip wheel (Python already on the target)

Use this when the machine already has Python 3.10+ and you prefer `pip` over a frozen zip. Still install Microsoft Edge. You do not need `playwright install` for Chromium; this bot uses system Edge.

**Linux (bash)**

```bash
python -m pip install build
python -m build --sdist --wheel
python -m pip install dist/critique_bot-*.whl
critique-bot --config config.json --patch-file diff.patch
```

From a git checkout instead of a wheel:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

**Windows (PowerShell)**

```powershell
python -m pip install build
python -m build --sdist --wheel
python -m pip install (Get-Item .\dist\critique_bot-*.whl).FullName
critique-bot --config config.json --patch-file diff.patch
```

`pip install dist\critique_bot-*.whl` does not expand globs the way bash does; `Get-Item` picks the wheel file.

From a git checkout:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
```

## Environment overrides

Same variable names on both OS.

**Linux (bash)**

```bash
export CRITIQUE_CHAT_URL="https://example.invalid/chat"
export CRITIQUE_MODEL="GPT-5.1"
./critique-bot --config config.json --patch-file diff.patch
```

**Windows (PowerShell)**

```powershell
$env:CRITIQUE_CHAT_URL = "https://example.invalid/chat"
$env:CRITIQUE_MODEL = "GPT-5.1"
.\critique-bot.exe --config config.json --patch-file diff.patch
```

Also: `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`, `CRITIQUE_QUEUE_DIR`.

## CI runner (GitLab)

Keep **one worker** running on the runner PC. CI jobs only call `submit`. The job and the worker must share `queue_dir` (GitLab **shell** executor).

1. Install Edge, unpack the zip (or pip-install) onto the runner, copy `config.example.json` to `config.json`.
2. Sign in once: `critique-bot worker --config config.json --headed --logs` (or one-shot `--headed`). Later runs reuse `.edge-profile`.
3. Start the worker at boot:
   - **Linux:** copy [`packaging/critique-bot-worker.service`](packaging/critique-bot-worker.service) to `/etc/systemd/system/`, edit paths, then `systemctl enable --now critique-bot-worker`.
   - **Windows:** [`packaging/worker-start.ps1`](packaging/worker-start.ps1) at logon, or a scheduled task.
4. Attach GitLab: tag the runner `critique-bot`. Copy [`.gitlab-ci.yml`](.gitlab-ci.yml) (Linux bash) or [`packaging/gitlab-ci.windows.yml`](packaging/gitlab-ci.windows.yml) (Windows PowerShell) into the project as `.gitlab-ci.yml`. Edit `CRITIQUE_BIN` / `CRITIQUE_CONFIG` in that YAML (`variables:`), not in GitLab CI/CD variables. Put `critique-bot` on the runner `PATH` (Windows: `worker-start.ps1` does this) so `CRITIQUE_BIN` can stay `critique-bot`. On Windows invoke with `& "$CRITIQUE_BIN" submit …`, not `$env:CRITIQUE_BIN submit`.
5. Each MR job writes `diff.patch`, runs `critique-bot submit … --output-dir out`, then posts it with `critique-bot gitlab-post` (`--review-file out/review.md --patch-file diff.patch`). That strips the machine-readable JSON block out of the comment and adds inline comments on the changed lines. Needs `CRITIQUE_GITLAB_TOKEN` (project access token, scope `api`).

Concurrent jobs enqueue. The worker runs up to `max_parallel_tabs` reviews at once (default 1) and waits `min_interval_seconds` (default 30) plus jitter between starts.

The worker keeps itself out of failure loops: a job is retried at most `max_attempts` times (default 3) before it is failed, each job gets a wall-clock limit (`job_timeout_seconds`), jobs still running when the worker stops are requeued for the next worker, and finished result folders are pruned to the newest `result_retention` (default 200) so the queue directory does not grow forever.

To check the runner without opening a MR:

```bash
critique-bot queue-status --config /opt/critique-bot/config.json
```

It prints worker liveness, waiting and in-progress jobs, and how recent jobs ended, and exits non-zero when no worker heartbeat is fresh — useful as a monitoring probe next to the systemd unit.

GitLab.com shared / instance runners and Docker executors cannot run this: no signed-in Edge, no shared queue.

Do not run two one-shot `critique-bot --patch-file` processes on the same profile; they will kill each other's Edge.

## What to copy besides the binary

| File | Purpose |
| --- | --- |
| `config.json` | Chat URL, selectors, model, timeouts, `queue_dir` (create from `config.example.json`) |
| `prompts/review.txt` | Default review template (`{patch}` placeholder). Override with `--prompt-template` or a `prompts/review.txt` next to the binary / in the cwd |
| `.edge-profile/` | Created on first run; keep it if you want to stay signed in |
| `.critique-queue/` | Worker inbox (created automatically next to `config.json` unless `queue_dir` is set) |

Do not ship `config.json` if it contains a private chat URL or session files (`storage_state.json`).
