# Use bot-agent from any folder (Windows)

Start here after the checkout already works: the repo is cloned, the virtual environment exists, the libraries are installed, `pip install -e .` has been run, and a one-word reply succeeded. That check looks like this in PowerShell, from the repo, with the venv activated:

```powershell
critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG." --headed
```

The steps below leave that `config.json` and the signed-in Edge profile where they are, and make `bot-agent` available in every folder. You type the task. You do not pass `--config` again.

Use the real path of your clone everywhere this page says `C:\path\to\critique-bot`. If the virtual environment folder is named `venv` rather than `.venv`, use that name in the paths below.

Leave the clone and the virtual environment where they are. The new command is a shortcut into that install. Moving or deleting the repo removes the command.

## 1. Point the profile at the login that just worked

A `user_data_dir` of `.edge-profile` is resolved from the folder you run the command in. From another folder that opens a new, unsigned profile.

In the `config.json` that just returned PONG, set `user_data_dir` to the full path of the profile next to that file:

```json
"user_data_dir": "C:\\path\\to\\critique-bot\\.edge-profile"
```

In JSON, each backslash is written twice. Save the file. Confirm the folder is there:

```powershell
Test-Path C:\path\to\critique-bot\.edge-profile
```

That command prints `True` when the signed-in profile is in place.

## 2. Add a bot-agent command that always uses this config

`pip install -e .` wrote `bot-agent.exe` under `.venv\Scripts`. Calling that exe directly still expects `--config` on each task. A small `.cmd` in front of it passes the config for you.

In PowerShell, from any directory:

```powershell
$repo = "C:\path\to\critique-bot"
$bin  = "$env:LOCALAPPDATA\critique-bot\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

@"
@echo off
"$repo\.venv\Scripts\bot-agent.exe" --config "$repo\config.json" %*
"@ | Set-Content -Encoding ASCII "$bin\bot-agent.cmd"

@"
@echo off
"$repo\.venv\Scripts\critique-bot.exe" %*
"@ | Set-Content -Encoding ASCII "$bin\critique-bot.cmd"
```

`bot-agent.cmd` always adds `--config` pointing at the file from the PONG check. `critique-bot.cmd` is the same install for `setup`, `worker`, and `submit`. Those commands still take `--config` when you run them.

## 3. Put that folder on PATH

```powershell
$bin = "$env:LOCALAPPDATA\critique-bot\bin"
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $current) { $current = "" }
$parts = @($current -split ";" | Where-Object { $_ -and ($_ -ne $bin) })
[Environment]::SetEnvironmentVariable("Path", (($bin, $parts) -join ";"), "User")
```

This puts the new `bin` folder first. If `.venv\Scripts` is already on your user `PATH` from an earlier setup, the command above leaves it later in the list, so `bot-agent` runs the `.cmd` that includes the config.

Close PowerShell and open a new window. The PATH change applies to new windows only.

## 4. Check the command

```powershell
Get-Command bot-agent
bot-agent --help
```

`Get-Command` should show `...\AppData\Local\critique-bot\bin\bot-agent.cmd`. The help text is the critique-bot help.

## 5. Run it in another folder

```powershell
cd C:\agent-test
bot-agent init
bot-agent "create a hello.txt file that says hello"
```

`init` creates `C:\agent-test\.bot\` (settings, a symbol index, and a sessions folder) and indexes the files there. An empty folder is enough. Run `bot-agent init` again later to rebuild the index. An existing `.bot\settings.json` is left as it is.

The task uses the chat URL, selectors, and Edge profile from the clone's `config.json`. Add `--headed` on that first task if you want the Edge window visible:

```powershell
bot-agent --headed "create a hello.txt file that says hello"
```

Later tasks from the same folder, or from any other folder after `bot-agent init` there, are the task text alone:

```powershell
bot-agent "update the test cases"
```

## What stays where it is

| Path | Role |
| --- | --- |
| `C:\path\to\critique-bot\` | Checkout. The commands run code from here. |
| `C:\path\to\critique-bot\.venv\` | The install from `pip install -e .`. |
| `C:\path\to\critique-bot\config.json` | Chat URL and selectors. The `bot-agent` command passes this every time. |
| `C:\path\to\critique-bot\.edge-profile\` | Signed-in Edge session. `user_data_dir` points here with a full path. |
| `%LOCALAPPDATA%\critique-bot\bin\` | `bot-agent.cmd` and `critique-bot.cmd` on your user `PATH`. |
| `C:\some-project\.bot\` | Per-project index and sessions, created by `bot-agent init`. |
