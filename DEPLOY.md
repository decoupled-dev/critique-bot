# Deploy critique-bot (Windows and Linux)

Ship a **zip per OS** (no Python on the target) or a **pip wheel**. The bot needs Microsoft Edge on the target (`microsoft-edge-stable` on Linux; Edge is usually already on Windows).

Build **on the OS you want to ship**. A Linux binary will not run on Windows, and vice versa. PyInstaller cannot cross-compile this project.

| You are on | Shell | Binary in the zip |
| --- | --- | --- |
| Linux | bash / zsh | `critique-bot` |
| Windows | PowerShell | `critique-bot.exe` |

The zip bundles Python, Playwright’s Node driver, `rich` and `prompt_toolkit` (the `crit` agent screen), `config.example.json`, and the review and agent prompts. It does **not** bundle Edge, and it does not bundle Playwright’s Chromium/Firefox/WebKit downloads. The bot drives system Edge (`channel=msedge`).

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
  prompts/agent.txt
  README.txt
```

**Windows**

```text
critique-bot-<version>-windows-x64\
  critique-bot.exe
  _internal\
  config.example.json
  prompts\review.txt
  prompts\agent.txt
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

### First login

If `.edge-profile` is missing in the current working directory, the first run opens an Edge window so you can sign in. After you sign in, that window closes and the same run continues headless. Later runs reuse that profile headless. Pass `--headed` when you want the window on a later run.

**Linux (bash)**

```bash
./critique-bot --config config.json --mode general --prompt "hello"
```

**Windows (PowerShell)**

```powershell
.\critique-bot.exe --config config.json --mode general --prompt "hello"
```

### Verify the install

Send a one-shot prompt through the signed-in chat page. The first run opens Edge when the profile is missing.

```bash
./critique-bot --config config.json --mode general \
  --prompt "Reply with exactly one word: PONG."
```

```powershell
.\critique-bot.exe --config config.json --mode general `
  --prompt "Reply with exactly one word: PONG."
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

### Coding agent

The zip has no separate `crit` binary. `crit` is `critique-bot --mode agent`, and everything it needs is in the zip. Run it from the project folder:

**Linux (bash)**

```bash
cd ~/my-project
/path/to/critique-bot-0.1.0-linux-x64/critique-bot --mode agent --config /path/to/config.json
```

**Windows (PowerShell)**

```powershell
cd C:\my-project
C:\path\to\critique-bot-0.1.0-windows-x64\critique-bot.exe --mode agent --config C:\path\to\config.json
```

Set `user_data_dir` in that `config.json` to an absolute path so every project folder uses the same signed-in profile. The short `crit` command and the setup scripts are for a source install; see [Pip wheel](#pip-wheel-python-already-on-the-target).

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

To also put `crit`, `bot-agent`, and `critique-bot` on `PATH` with one shared `config.json`, run the setup script from the checkout instead. It creates the venv, installs, and writes the commands into `~/.local/bin`:

```bash
scripts/setup-crit.sh
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

Or let the setup script do that and put `crit`, `bot-agent`, and `critique-bot` on your user `PATH` (in `%LOCALAPPDATA%\critique-bot\bin`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1
```

Script options and the manual steps: [`docs/bot-agent.md`](docs/bot-agent.md#quick-setup-with-the-script). The wheel installs `crit` and `bot-agent` as well; `pip` pulls in `rich` and `prompt_toolkit` with it.

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

GitLab API calls (`gitlab-post`, MR context) ignore `http_proxy` / `https_proxy` by default, so a proxy set for other traffic never sees the GitLab token. Set `CRITIQUE_GITLAB_PROXY` to a proxy URL to send them through one, or to `env` to use the usual proxy variables. They never follow HTTP redirects (set `gitlab.base_url` to the final `https://` address), and the token is only sent to the host in `gitlab.base_url` / `CRITIQUE_GITLAB_URL` / `CI_SERVER_URL`: an `--mr-url` on another host is refused.

## CI runner (GitLab)

Keep **one worker** running on the runner PC. CI jobs only call `submit`. The job and the worker must share `queue_dir` (GitLab **shell** executor).

1. Install Edge, unpack the zip (or pip-install) onto the runner, copy `config.example.json` to `config.json`.
2. Sign in once: `critique-bot worker --config config.json --logs` (the window opens when `.edge-profile` is missing). Later runs reuse that profile.
3. Start the worker at boot, **as its own user** (see "Separate the worker from CI" below):
   - **Linux:** copy [`packaging/critique-bot-worker.service`](packaging/critique-bot-worker.service) to `/etc/systemd/system/`, follow the one-time commands at its top (user `critique-bot`, queue group), edit paths, then `systemctl enable --now critique-bot-worker`.
   - **Windows:** install under `C:\Program Files\critique-bot` and run [`packaging/worker-start.ps1`](packaging/worker-start.ps1) at logon or as a scheduled task under a dedicated account. It sets the install folder's ACL (SYSTEM, Administrators, the worker account; read/execute for `$RunnerAccount`) and does not change `PATH`.
4. Attach GitLab: tag the runner `critique-bot`. Copy [`.gitlab-ci.yml`](.gitlab-ci.yml) (Linux bash) or [`packaging/gitlab-ci.windows.yml`](packaging/gitlab-ci.windows.yml) (Windows PowerShell) into the project as `.gitlab-ci.yml`. Edit `CRITIQUE_BIN` / `CRITIQUE_CONFIG` in that YAML (`variables:`), not in GitLab CI/CD variables. Use the full path of the exe in `CRITIQUE_BIN`, or a `PATH` entry only administrators can write. On Windows invoke with `& "$CRITIQUE_BIN" submit …`, not `$env:CRITIQUE_BIN submit`.
5. Each MR job runs `critique-bot submit … --output-dir out` (submit builds `diff.patch` from the checkout and attaches changed files), then posts with `critique-bot gitlab-post` (`--review-file out/review.md --patch-file diff.patch`). That strips the machine-readable JSON block out of the comment and adds inline comments on the changed lines. Needs `CRITIQUE_GITLAB_TOKEN` (see the token notes below).

### Separate the worker from CI

A shell runner runs each job as the runner's OS user, and the job runs whatever the branch's `.gitlab-ci.yml` says. If the worker runs as that same user, **any developer who can push a branch** can read the signed-in browser profile (`.edge-profile`, i.e. SSO cookies for the chat site), every queued prompt and result, `config.json`, and any token in the environment. So:

- **Worker user.** Run the worker as a dedicated user (`critique-bot`), never as `gitlab-runner` and never as root (as root Chromium runs without its sandbox). The systemd unit does this and adds hardening (`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, `UMask=0077`, …). Keep `user_data_dir` (the profile) and `queue_dir` under the worker's home, e.g. `/var/lib/critique-bot/`.
- **Queue group.** Add `gitlab-runner` to group `critique-bot` and create the queue folder `2750 critique-bot:critique-bot`. With `CRITIQUE_QUEUE_SHARED=1` (set in the unit; a setgid queue folder is detected too) the worker keeps `inbox/` at `2770` (CI may drop jobs), `results/` and each result folder at `2750` with `0640` `status.json` / review files (CI reads them), and `processing/`, failure snapshots (`page.html`, `screenshot.png`) and the profile owner-only. Every CI job in the group can read every result folder; there is no per-job ACL.
- **Install folder.** `root:critique-bot`, no access for others; the CI user only needs to read `config.json` and run the binary. `config.json` must not hold secrets.
- **Runner scope.** Register the runner for the specific project(s) only (not shared/group-wide), turn on *Protected* (runs only for protected branches) when you can, and disable *Run untagged jobs*.
- **Token.** Use a project access token, not a personal one, with role **Reporter** and scope **`api`**: Reporter is the lowest role that can read a private project's MR diff and create MR discussions, and no narrower scope than `api` can post notes. Set an expiry and rotate it. Prefer a **protected**, masked CI/CD variable. Protected variables are hidden from pipelines on unprotected branches, so if you must post on feature-branch MRs the variable has to be unprotected; then treat the token as readable by everyone who can push a branch (that is why it is Reporter-only and project-scoped).
- **Windows.** Same model: the worker under its own account (`worker-start.ps1`), `$RunnerAccount` gets read/execute on the install folder and modify on the queue folder only; the browser profile is made private to the worker account by crit (icacls). Without a separate account, a CI job can read the profile.

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
| `.edge-profile/` | Created on first run; keep it if you want to stay signed in. Holds SSO cookies: owner-only, never readable by the CI user |
| `.critique-queue/` | Worker inbox (created automatically next to `config.json` unless `queue_dir` is set) |

Do not ship `config.json` if it contains a private chat URL or session files (`storage_state.json`).
