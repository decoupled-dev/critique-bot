# Use crit from any folder (Windows)

Start here after the checkout already works: the repo is cloned, the virtual environment exists, the libraries are installed, `pip install -e .` has been run, and a one-word reply succeeded. That check looks like this in PowerShell, from the repo, with the venv activated:

```powershell
critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG."
```

The steps below leave that `config.json` and the signed-in Edge profile where they are, and make `crit` available in every folder. `bot-agent` is the same command. You type the task. You do not pass `--config` again.

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

## 2. Add a crit command that always uses this config

`pip install -e .` wrote `crit.exe` under `.venv\Scripts` (`bot-agent.exe` is the same program). Calling that exe directly still expects `--config` on each task. A small `.cmd` in front of it passes the config for you.

In PowerShell, from any directory:

```powershell
$repo = "C:\path\to\critique-bot"
$bin  = "$env:LOCALAPPDATA\critique-bot\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

@"
@echo off
"$repo\.venv\Scripts\crit.exe" --config "$repo\config.json" %*
"@ | Set-Content -Encoding ASCII "$bin\crit.cmd"

@"
@echo off
"$repo\.venv\Scripts\bot-agent.exe" --config "$repo\config.json" %*
"@ | Set-Content -Encoding ASCII "$bin\bot-agent.cmd"

@"
@echo off
"$repo\.venv\Scripts\critique-bot.exe" %*
"@ | Set-Content -Encoding ASCII "$bin\critique-bot.cmd"
```

`crit.cmd` always adds `--config` pointing at the file from the PONG check. `bot-agent.cmd` does the same thing. `critique-bot.cmd` is the same install for `setup`, `worker`, and `submit`. Those commands still take `--config` when you run them.

## 3. Put that folder on PATH

```powershell
$bin = "$env:LOCALAPPDATA\critique-bot\bin"
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $current) { $current = "" }
$parts = @($current -split ";" | Where-Object { $_ -and ($_ -ne $bin) })
[Environment]::SetEnvironmentVariable("Path", (($bin, $parts) -join ";"), "User")
```

This puts the new `bin` folder first. If `.venv\Scripts` is already on your user `PATH` from an earlier setup, the command above leaves it later in the list, so `crit` runs the `.cmd` that includes the config.

Close PowerShell and open a new window. The PATH change applies to new windows only.

## 4. Check the command

```powershell
Get-Command crit
crit --help
```

`Get-Command` should show `...\AppData\Local\critique-bot\bin\crit.cmd`. The help text is the critique-bot help.

## 5. Run it in another folder

```powershell
cd C:\agent-test
crit
crit "create a hello.txt file that says hello"
```

The first `crit` creates `C:\agent-test\.bot\` (settings, a symbol index, and a sessions folder), asks which text style looks right, then asks you to sign in and opens Edge. An empty folder is enough. `crit init` rebuilds the index later. An existing `.bot\settings.json` is left as it is.

The task uses the chat URL, selectors, and Edge profile from the clone's `config.json`. The first `crit` asks before it opens Edge. The window closes after you sign in, and later tasks stay headless. Add `--headed` when you want the window on a later task:

```powershell
crit --headed "create a hello.txt file that says hello"
```

Later tasks from the same folder, or from any other folder after the first `crit` there, are the task text alone:

```powershell
crit "update the test cases"
```

### The crit screen

Each session opens with a box that shows the folder, model, shell, and permission mode. Type at the `>` prompt:

| Key | Effect |
| --- | --- |
| Enter | Send the message. Pasted text with several lines stays one message. |
| Alt+Enter, Ctrl+J, or `\` at the end of a line | New line. |
| `/` | Command menu (below). |
| `@` | Complete a file or folder path in the project. Mentioned paths that exist are listed for the model as `Referenced files:`. |
| Up / Down | Earlier messages. History is kept in `.bot\history`. |
| Ctrl+C | Clear the input. On an empty line, press it twice to leave. During a task it stops the task. |
| Ctrl+D | Leave. |
| `?` on an empty line | Show these shortcuts. |

| Command | Effect |
| --- | --- |
| `/help` | Commands and shortcuts. |
| `/new` | Start a fresh chat. |
| `/clear` | Clear the screen. |
| `/undo` | Restore the files the last task changed. |
| `/permissions` | Switch between asking and not asking for the rest of the session. |
| `/status` | Model, shell, folder, permission mode, and background commands. |
| `/shell [name]` | List the shells, or switch the default for this session. |
| `/tools` | The tools the model can call. |
| `/theme` | Change the text style. |
| `/exit` | Leave (`exit` and `quit` work too). |

Each tool step prints one line such as `● Read(src\app.py)` with the result under it, `⎿  Read 120 lines`. Edits show a short numbered diff, and commands show the exit code, time, and the first lines of output. While the chat is replying, a line such as `✻ Thinking… (23s · ctrl+c to interrupt)` counts the seconds.

### Approvals

Reading and searching run without asking. Edits, commands, web fetches, and anything outside the project folder show a box first:

1. **Yes** runs this step.
2. **Yes, and don't ask again** remembers the choice for this session: all file edits, commands that start with the same program (for example every `npm` command), or one web site. Paths outside the project are always asked.
3. **No** (or Esc) skips the step. After `3` you can type what to do instead, and the model gets that note.

Use the arrow keys and Enter, or press `1`, `2`, `3`, `y`, `a`, or `n`. `crit --yes "task"` (also `--auto` or `-y`) or `"permissions": "auto"` in `.bot\settings.json` runs everything without asking. With no terminal (a pipe or a CI job) and no `--yes`, those steps are declined.

When the output is not a terminal, crit prints plain text with no colors or spinner. `NO_COLOR=1` keeps the layout and drops the colors.

## 6. Tune a project

### Project notes

The first `crit` writes `.bot\AGENT.md`. Text below the `<!-- notes start -->` line is sent with the instructions at the start of every session, the same way `CLAUDE.md` works for Claude Code. Put the build and test commands and the rules the model should follow there:

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
| `check_command` | The finish line after a real edit. A `Test with:` or `Test command:` line in `AGENT.md` is used when this key is absent; this key wins when both are set. A non-zero exit sends the output back to the model, up to two times. If it still fails, the task ends as FAILED. When the task ends, the program prints the on-disk diff once. Example: `".\\gradlew.bat testDebugUnitTest"`. |
| `max_result_chars` | Characters per tool-result message sent to the chat. The default is 40000 or `max_prompt_chars` from `config.json`, whichever is smaller. Values under 4000 are ignored. |
| `seed_instructions` | Set to `false` when the tool instructions already live in a ChatGPT Project (see below). The first turn then sends only the environment and project notes. |
| `theme` | Text style chosen on the welcome screen: `auto`, `dark`, `light`, `dark-colorblind`, `light-colorblind`, `dark-ansi`, or `light-ansi`. The session screen uses the same colors. `/theme` changes it. |
| `permissions` | `"auto"` runs edits, commands, and fetches without asking, like `--yes`. The default, `"ask"`, shows the approval box. |
| `syntax_preview` | `false` turns the welcome-screen syntax colors off. ctrl+t on that screen toggles it. |
| `shell` | Default shell for `run_command`: `auto` (the default: PowerShell 7, then Windows PowerShell 5.1 on Windows; bash, then sh elsewhere), `pwsh`, `powershell`, `cmd`, `bash` (Git Bash on Windows), `sh`, or `zsh`. A shell that is not installed falls back to `auto`. `/shell` changes it for one session. |
| `check_timeout` | Seconds the `check_command` may run. Default 600. |
| `compact_after_chars` | When the chat holds about this many characters, crit starts a new chat with a summary of the task so far. Default 300000; values under 10000 are ignored. |
| `reply_retries` | How many times a failed or empty reply from the chat page is retried before the task ends. Default 3. |

### Use a ChatGPT Project for the instructions

The tool protocol is long. Sending it as the first message works, but the model can lose track of it in a long chat. A ChatGPT Project keeps it pinned:

1. In chatgpt.com, create a Project and paste the `SYSTEM` section of `prompts\agent.txt` into the Project's instructions.
2. Point `chat_url` in `config.json` at that Project.
3. Set `"seed_instructions": false` in `.bot\settings.json`.

### Shells

On Windows, `run_command` uses PowerShell 7 (`pwsh.exe`) when it is installed, and Windows PowerShell 5.1 otherwise. On Linux and macOS it uses `bash`, or `sh` when bash is missing. The model can pick another installed shell for one call with `"shell"`: `"bash"` (on Windows this is Git Bash, never WSL's `System32\bash.exe`), `"cmd"`, `"pwsh"`, or `"powershell"`. The settings value `"shell"` (`auto`, `bash`, `pwsh`, `powershell`, `cmd`, `sh`, `zsh`) changes the default.

- The working folder persists like a terminal: `cd` in one command carries over to the next, and `cwd` sets where a command starts.
- Each command runs from a temporary script file, so long commands work. PowerShell reads that file through a short `-EncodedCommand`, so a Group Policy execution policy (`AllSigned`, `Restricted`) does not block it, and runs with `-NoProfile -ExecutionPolicy Bypass`, so `npm.ps1`, `npx.ps1`, and `Activate.ps1` are not blocked where policy allows. Output is UTF-8; a native tool that writes the OEM code page is still decoded.
- PowerShell exit codes follow the last statement: a native program's exit code, else 1 when a cmdlet failed or an error was thrown, else 0. `exit N` exits with N. A script made of `param(...)`, `begin`, `process`, and `end` blocks runs whole.
- On Windows PowerShell 5.1, a top-level `a && b` or `a || b` is rewritten so it runs. A command that uses `export`, `grep`, `sed`, a heredoc, or other bash syntax is not run; the model is told the PowerShell form, or to resend it with `"shell": "bash"` when Git Bash is installed. Quoted text and `@{...}` hashtables are not checked.
- Commands get a copy of your environment with secrets removed: names containing `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `ACCESS_KEY`, `PRIVATE_KEY`, or `CREDENTIAL`, and `CRITIQUE_*`, `OPENAI_*`, `ANTHROPIC_*`, `AWS_SECRET*`.
- A timeout or Ctrl+C stops the command and every process it started, and keeps the output so far. Servers and watchers run with `"background": true`; they are stopped when `crit` exits.

PowerShell 7 is recommended. Install it with:

```powershell
winget install --id Microsoft.PowerShell
```

### Undo

Before a task changes a file for the first time, `crit` saves a copy under `.bot\cache\undo\`. `crit undo` (or `/undo` inside a session) restores the files changed by the most recent task and deletes the files it created. Run it again to step back one more task. The last 20 tasks are kept.

## What stays where it is

| Path | Role |
| --- | --- |
| `C:\path\to\critique-bot\` | Checkout. The commands run code from here. |
| `C:\path\to\critique-bot\.venv\` | The install from `pip install -e .`. |
| `C:\path\to\critique-bot\config.json` | Chat URL and selectors. The `crit` command passes this every time. |
| `C:\path\to\critique-bot\.edge-profile\` | Signed-in Edge session. `user_data_dir` points here with a full path. |
| `%LOCALAPPDATA%\critique-bot\bin\` | `crit.cmd`, `bot-agent.cmd`, and `critique-bot.cmd` on your user `PATH`. |
| `C:\some-project\.bot\` | Per-project index and sessions, created the first time you run `crit` there. |
