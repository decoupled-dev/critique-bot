# Deploy critique-bot (Windows and Linux)

Ship a **zip per OS** (no Python on the target) or a **pip wheel**. Microsoft Edge must already be installed on the target (`microsoft-edge-stable` on Linux; Edge is usually already on Windows).

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

Workflow: [`.github/workflows/build.yml`](.github/workflows/build.yml) (**Build**).

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

Also: `CRITIQUE_STORAGE_STATE`, `CRITIQUE_USER_DATA_DIR`, `CRITIQUE_CDP_URL`.

## What to copy besides the binary

| File | Purpose |
| --- | --- |
| `config.json` | Chat URL, selectors, model, timeouts (create from `config.example.json`) |
| `prompts/review.txt` | Default review template (`{patch}` placeholder). Override with `--prompt-template` or a `prompts/review.txt` next to the binary / in the cwd |
| `.edge-profile/` | Created on first run; keep it if you want to stay signed in |

Do not ship `config.json` if it contains a private chat URL or session files (`storage_state.json`).
