# Use bot-agent from any folder (Windows)

Start here after the checkout already works: the repo is cloned, the virtual environment exists, the libraries are installed, `pip install -e .` has been run, and a one-word reply succeeded. That check looks like this in PowerShell, from the repo, with the venv activated:

```powershell
critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG."
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

The task uses the chat URL, selectors, and Edge profile from the clone's `config.json`. If that profile is missing, the first task opens an Edge window so you can sign in. Add `--headed` when you want the window on a later task:

```powershell
bot-agent --headed "create a hello.txt file that says hello"
```

Later tasks from the same folder, or from any other folder after `bot-agent init` there, are the task text alone:

```powershell
bot-agent "update the test cases"
```

## 6. Tune a project

### Project notes

`bot-agent init` writes `.bot\AGENT.md`. Text below the `<!-- notes start -->` line is sent with the instructions at the start of every session, the same way `CLAUDE.md` works for Claude Code. Put the build and test commands and the rules the model should follow there:

```markdown
<!-- notes start -->
Build with: .\gradlew.bat assembleDebug
Test with: .\gradlew.bat testDebugUnitTest
Kotlin only; do not add Java files.
```

### Settings

`.bot\settings.json` takes these optional keys:

| Key | Effect |
| --- | --- |
| `check_command` | Runs after the model reports COMPLETED and at least one file changed. A non-zero exit sends the output back to the model, up to two times. If it still fails, the task ends as FAILED. Example: `".\\gradlew.bat testDebugUnitTest"`. |
| `max_result_chars` | Characters per tool-result message sent to the chat. The default is 40000 or `max_prompt_chars` from `config.json`, whichever is smaller. Values under 4000 are ignored. |
| `seed_instructions` | Set to `false` when the tool instructions already live in a ChatGPT Project (see below). The first turn then sends only the environment and project notes. |

### Use a ChatGPT Project for the instructions

The tool protocol is long. Sending it as the first message works, but the model can lose track of it in a long chat. A ChatGPT Project keeps it pinned:

1. In chatgpt.com, create a Project and paste the `SYSTEM` section of `prompts\agent.txt` into the Project's instructions.
2. Point `chat_url` in `config.json` at that Project.
3. Set `"seed_instructions": false` in `.bot\settings.json`.

### PowerShell

`run_command` uses PowerShell 7 (`pwsh.exe`) when it is installed, and Windows PowerShell 5.1 otherwise. PowerShell 7 is recommended: it accepts `&&` and `||`, which models write often. On 5.1, a command that uses `&&`, `export`, `grep`, or other bash syntax is not run, and the model is told the PowerShell form to use instead. Install it with:

```powershell
winget install --id Microsoft.PowerShell
```

### Undo

Before a task changes a file for the first time, `bot-agent` saves a copy under `.bot\cache\undo\`. `bot-agent undo` restores the files changed by the most recent task and deletes the files it created. Run it again to step back one more task. The last 20 tasks are kept.

## What stays where it is

| Path | Role |
| --- | --- |
| `C:\path\to\critique-bot\` | Checkout. The commands run code from here. |
| `C:\path\to\critique-bot\.venv\` | The install from `pip install -e .`. |
| `C:\path\to\critique-bot\config.json` | Chat URL and selectors. The `bot-agent` command passes this every time. |
| `C:\path\to\critique-bot\.edge-profile\` | Signed-in Edge session. `user_data_dir` points here with a full path. |
| `%LOCALAPPDATA%\critique-bot\bin\` | `bot-agent.cmd` and `critique-bot.cmd` on your user `PATH`. |
| `C:\some-project\.bot\` | Per-project index and sessions, created by `bot-agent init`. |
