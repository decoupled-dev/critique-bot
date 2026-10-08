# Use crit from any folder (Windows, Linux, macOS)

`crit` is the local coding agent. `bot-agent` is the same command. This page installs it once from a clone, then makes `crit` work in every folder with one `config.json` and one signed-in browser profile. You type the task. You do not pass `--config` again.

The commands are a shortcut into the clone and its virtual environment. Leave both where they are. Moving or deleting the repo removes the command.

## Quick setup with the script

The repo ships a setup script for each OS. Each one is safe to re-run: every step checks what is already in place.

### Linux and macOS

You need Python 3.10 or newer (with `venv`; on Debian and Ubuntu that is the `python3-venv` package) and Microsoft Edge or Google Chrome. From the clone:

```bash
scripts/setup-crit.sh
```

| Option | Effect |
| --- | --- |
| `--venv DIR` | Virtual environment to create or reuse. Default `<repo>/.venv`. |
| `--config FILE` | Config file the commands use. Default `<repo>/config.json`. |
| `--bin-dir DIR` | Where the commands are written. Default `~/.local/bin`. |
| `--proxy URL` | Send this proxy on every `pip install` (`--proxy URL`). Example: `http://username:password@10.1.2.3:8080`. A `uv` install uses the same URL through `HTTP_PROXY` and `HTTPS_PROXY`. |
| `--with-index` | Also install the `[index]` extra (tree-sitter parsers). |
| `--no-path` | Do not add the bin folder to `PATH`. |
| `--skip-browser-check` | Do not look for Edge or Chrome. |
| `--install-deps` | Linux: run `playwright install-deps` for the browser's system libraries. This may ask for `sudo`. |

The script:

1. Finds Python 3.10 or newer. When the only Python on `PATH` is older but the virtual environment already has a newer one, it uses that.
2. Creates the virtual environment and runs `pip install -e .` in it (`pip install -e ".[index]"` with `--with-index`). With `--proxy`, every `pip install` is `pip install ... --proxy URL`, including the pip upgrade. A virtual environment without pip (for example one made by `uv`) gets pip from `ensurepip`, or the install runs through `uv pip` with that proxy in the environment.
3. Checks that Edge or Chrome is installed. With `--install-deps` on Linux it also runs `playwright install-deps`.
4. Copies `config.example.json` to the config file when that file is missing.
5. Rewrites a relative `user_data_dir` in the config to an absolute path next to the config, so every folder uses the same signed-in profile.
6. Writes `crit`, `bot-agent`, and `critique-bot` into the bin folder. `crit` and `bot-agent` always pass `--config` with that file.
7. Adds the bin folder to `PATH` in the rc file of your login shell (`~/.zshrc` for zsh, `~/.bashrc` for bash, `~/.profile` otherwise), once, inside a marked block. `--no-path` skips this.
8. Runs `crit --help` to check the install, then prints the next steps.

Open a new terminal afterwards, or `source` the rc file it names, so the `PATH` change applies.

### Windows

From the clone, in PowerShell:

```powershell
.\scripts\setup-crit.ps1
```

If scripts are blocked:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1
```

| Option | Effect |
| --- | --- |
| `-Venv DIR` | Virtual environment to create or reuse. Default `<repo>\.venv`. |
| `-Config FILE` | Config file the commands use. Default `<repo>\config.json`. |
| `-BinDir DIR` | Where the commands are written. Default `%LOCALAPPDATA%\critique-bot\bin`. |
| `-Proxy URL` | Send this proxy on every `pip install` (`--proxy URL`). Example: `http://username:password@10.1.2.3:8080`. A `uv` install uses the same URL through `HTTP_PROXY` and `HTTPS_PROXY`. |
| `-WithIndex` | Also install the `[index]` extra (tree-sitter parsers). |
| `-NoPath` | Do not change the user `PATH`. |
| `-SkipBrowserCheck` | Do not look for Edge or Chrome. |

It does the same steps as the Linux script, writes `crit.cmd`, `bot-agent.cmd`, and `critique-bot.cmd`, and puts the bin folder first on your user `PATH`. Open a new PowerShell window afterwards.

### After the script

1. Pick the selectors and sign in. Use the config path the script printed:

   ```bash
   critique-bot setup --config /path/to/critique-bot/config.json
   ```

   ```powershell
   critique-bot setup --config C:\path\to\critique-bot\config.json
   ```

2. Check one reply:

   ```bash
   critique-bot --config /path/to/critique-bot/config.json --mode general --prompt "Reply with exactly one word: PONG."
   ```

   ```powershell
   critique-bot --config C:\path\to\critique-bot\config.json --mode general --prompt "Reply with exactly one word: PONG."
   ```

3. Go to a project and start:

   ```bash
   cd ~/my-project
   crit
   ```

   ```powershell
   cd C:\my-project
   crit
   ```

Then read [The crit screen](#the-crit-screen) and [Approvals](#approvals) below.

## Manual setup

Start here after the checkout already works: the repo is cloned, the virtual environment exists, `pip install -r requirements.txt` and `pip install -e .` have been run, and a one-word reply succeeded. From the repo, with the venv activated:

```bash
critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG."
```

```powershell
critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG."
```

The steps below leave that `config.json` and the signed-in browser profile where they are, and make `crit` available in every folder.

Use the real path of your clone everywhere this page says `C:\path\to\critique-bot` (Windows) or `/path/to/critique-bot` (Linux and macOS). If the virtual environment folder is named `venv` rather than `.venv`, use that name in the paths below.

On Linux you also need:

- Python 3.10 or newer, and the `python3-venv` package on Debian and Ubuntu.
- `microsoft-edge-stable`, or Google Chrome when Edge is not installed.
- A desktop session (or SSH with X forwarding) for the first sign-in. That one run opens a visible browser window. Later runs are headless.
- On a minimal server, the browser's system libraries: `playwright install-deps` from the venv (it may need `sudo`).

macOS needs Python 3.10 or newer and Edge or Chrome. The login window opens on the desktop as usual.

### 1. Point the profile at the login that just worked

A relative `user_data_dir` such as `.edge-profile` is resolved from the folder you run the command in. From another folder that opens a new, unsigned profile.

In the `config.json` that just returned PONG, set `user_data_dir` to the full path of the profile next to that file.

Windows:

```json
"user_data_dir": "C:\\path\\to\\critique-bot\\.edge-profile"
```

In JSON, each backslash is written twice.

Linux and macOS:

```json
"user_data_dir": "/path/to/critique-bot/.edge-profile"
```

Save the file. Confirm the folder is there:

```powershell
Test-Path C:\path\to\critique-bot\.edge-profile
```

```bash
test -d /path/to/critique-bot/.edge-profile && echo yes
```

That prints `True` (PowerShell) or `yes` (bash) when the signed-in profile is in place.

### 2. Add a crit command that always uses this config

`pip install -e .` wrote `crit` into the venv (`.venv\Scripts\crit.exe` on Windows, `.venv/bin/crit` on Linux and macOS). `bot-agent` is the same program. Calling it directly still expects `--config` on the first task in each folder. A small wrapper in front of it passes the config for you.

Windows (PowerShell, from any directory):

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

Linux and macOS (bash or zsh):

```bash
repo=/path/to/critique-bot
bin="$HOME/.local/bin"
mkdir -p "$bin"

cat > "$bin/crit" <<EOF
#!/bin/sh
exec "$repo/.venv/bin/crit" --config "$repo/config.json" "\$@"
EOF

cat > "$bin/bot-agent" <<EOF
#!/bin/sh
exec "$repo/.venv/bin/bot-agent" --config "$repo/config.json" "\$@"
EOF

cat > "$bin/critique-bot" <<EOF
#!/bin/sh
exec "$repo/.venv/bin/critique-bot" "\$@"
EOF

chmod +x "$bin/crit" "$bin/bot-agent" "$bin/critique-bot"
```

`crit` always adds `--config` pointing at the file from the PONG check. `bot-agent` does the same thing. `critique-bot` is the same install for `setup`, `worker`, and `submit`. Those commands still take `--config` when you run them.

### 3. Put that folder on PATH

Windows:

```powershell
$bin = "$env:LOCALAPPDATA\critique-bot\bin"
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $current) { $current = "" }
$parts = @($current -split ";" | Where-Object { $_ -and ($_ -ne $bin) })
[Environment]::SetEnvironmentVariable("Path", (($bin, $parts) -join ";"), "User")
```

This puts the new `bin` folder first. If `.venv\Scripts` is already on your user `PATH` from an earlier setup, the command above leaves it later in the list, so `crit` runs the `.cmd` that includes the config. Close PowerShell and open a new window. The PATH change applies to new windows only.

Linux and macOS: many distributions already put `~/.local/bin` on `PATH`. If yours does not, add it to `~/.bashrc` (bash) or `~/.zshrc` (zsh, the macOS default):

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

Open a new terminal. Deactivate the venv first if it is active in that terminal; an active venv puts its own `crit`, without the config, ahead of the wrapper.

### 4. Check the command

```powershell
Get-Command crit
crit --help
```

```bash
command -v crit
crit --help
```

`Get-Command` should show `...\AppData\Local\critique-bot\bin\crit.cmd`. `command -v` should show `~/.local/bin/crit` as a full path. The help text is the critique-bot help.

### 5. Run it in another folder

```powershell
mkdir C:\agent-test
cd C:\agent-test
crit
crit "create a hello.txt file that says hello"
```

```bash
mkdir -p ~/agent-test
cd ~/agent-test
crit
crit "create a hello.txt file that says hello"
```

The first `crit` creates `.bot` in that folder (settings, a symbol index, and a sessions folder), asks which text style looks right, then asks you to sign in. When the profile from step 1 is already signed in, it prints `Already signed in.` and goes on. Otherwise it opens the browser for the login, and the window closes after you sign in. An empty folder is enough. `crit init` rebuilds the index later. An existing `.bot/settings.json` is left as it is.

The task uses the chat URL, selectors, and browser profile from the clone's `config.json`. Tasks stay headless. Add `--headed` when you want the window on a later task:

```bash
crit --headed "create a hello.txt file that says hello"
```

Later tasks from the same folder, or from any other folder after the first `crit` there, are the task text alone:

```bash
crit "update the test cases"
crit --yes "fix the failing test"
crit undo
```

`crit` with no task opens the session screen and waits for you to type. `crit "task"` runs that task first, then waits for the next one.

## The crit screen

Each session opens with a box that shows the folder, model, shell, and permission mode. Type at the `>` prompt:

| Key | Effect |
| --- | --- |
| Enter | Send the message. Pasted text with several lines stays one message. |
| Alt+Enter, Ctrl+J, or `\` at the end of a line | New line. |
| `/` | Command menu (below). |
| `@` | Complete a file or folder path in the project. Mentioned paths that exist are listed for the model as `Referenced files:`. |
| Up / Down | Earlier messages. History is kept in `.bot/history`. |
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

Each tool step prints one line such as `● Read(src/app.py)` with the result under it, `⎿  Read 120 lines`. Edits show a short numbered diff, and commands show the exit code, time, and the first lines of output. While the chat is replying, a line such as `✻ Thinking… (23s · ctrl+c to interrupt)` counts the seconds.

Ctrl+C during a task ends that task as INTERRUPTED. It stops the running command and every process it started, stops background commands, and clicks the chat page's stop control so the half-written reply does not mix into the next one. The session stays open for the next task. If the page is still writing a reply when the next message goes out, crit waits for it to finish, or stops it after about 20 seconds. Both depend on [`selectors.stop_button`](config.json.md#stop_button).

### How a task runs

The instructions from [`prompts/agent.txt`](../prompts/agent.txt) go out in front of the first task, in the same message, so the first reply already works on it. The model answers with `<tool_call>` blocks. crit runs them and sends the results back, each followed by a short STATE block (files read, files changed, last command), until the model finishes.

- A question with no file change is answered in words, and that task ends.
- A reply that answers and also prints tool calls shows the answer and runs the calls.
- A refusal, a "what should I change?", or a promise to keep working is sent back until the model calls a tool or finishes. Three in a row are sent back; the next one ends the task as FAILED.
- A real question that needs your decision is shown to you, and your answer goes back to the model. The model can also ask with the `ask_user` tool. With no terminal, the model is told no user is available and decides itself.
- COMPLETED, DONE, or a reply that no edit is needed ends the task. After an edit, a set check command runs first (see `check_command` below).

### Approvals

Reading and searching run without asking. Edits, commands, web fetches, and anything outside the project folder show a box first:

1. **Yes** runs this step.
2. **Yes, and don't ask again** remembers the choice for this session: all file edits, commands that start with the same program (for example every `npm` command), or one web site. It never covers a command that chains, redirects, or nests another command; those are asked each time. Paths outside the project are always asked.
3. **No** (or Esc) skips the step. After `3` you can type what to do instead, and the model gets that note.

Use the arrow keys and Enter, or press `1`, `2`, `3`, `y`, `a`, or `n`. `crit --yes "task"` (also `--auto` or `-y`) or `"permissions": "auto"` in `.bot/settings.json` runs everything without asking. `/permissions` switches for the rest of the session. With no terminal (a pipe or a CI job) and no `--yes`, those steps are declined.

When the output is not a terminal, crit prints plain text with no colors or spinner. `NO_COLOR=1` keeps the layout and drops the colors.

## Tune a project

### Project notes

The first `crit` writes `.bot/AGENT.md`. Text below the `<!-- notes start -->` line is sent with the instructions at the start of every chat (the first task, `/new`, and each new chat after compaction), the same way `CLAUDE.md` works for Claude Code. Put the build and test commands and the rules the model should follow there:

```markdown
<!-- notes start -->
Build with: .\gradlew.bat assembleDebug
Test with: .\gradlew.bat testDebugUnitTest
Kotlin only; do not add Java files.
```

On Linux and macOS the same lines use `./gradlew`.

### Settings

`.bot/settings.json` takes these optional keys:

| Key | Effect |
| --- | --- |
| `check_command` | The finish line after a real edit. A `Test with:` or `Test command:` line in `AGENT.md` is used when this key is absent; this key wins when both are set. A non-zero exit sends the output back to the model, up to two times. If it still fails, the task ends as FAILED. When the task ends, the program prints the on-disk diff once. Example: `".\\gradlew.bat testDebugUnitTest"` or `"./gradlew testDebugUnitTest"`. |
| `max_result_chars` | Characters per tool-result message sent to the chat. The default is 40000 or `max_prompt_chars` from `config.json`, whichever is smaller. Values under 4000 are ignored. |
| `seed_instructions` | Set to `false` when the tool instructions already live in a ChatGPT Project (see below). The first task then goes out with only the environment and project notes in front of it. |
| `theme` | Text style chosen on the welcome screen: `auto`, `dark`, `light`, `dark-colorblind`, `light-colorblind`, `dark-ansi`, or `light-ansi`. The session screen uses the same colors. `/theme` changes it. |
| `permissions` | `"auto"` runs edits, commands, and fetches without asking, like `--yes`. The default, `"ask"`, shows the approval box. |
| `syntax_preview` | `false` turns the welcome-screen syntax colors off. ctrl+t on that screen toggles it. |
| `shell` | Default shell for `run_command`: `auto` (the default: PowerShell 7, then Windows PowerShell 5.1 on Windows; bash, then sh elsewhere), `pwsh`, `powershell`, `cmd`, `bash` (Git Bash on Windows), `sh`, or `zsh`. A shell that is not installed falls back to `auto`. `/shell` changes it for one session. |
| `check_timeout` | Seconds the `check_command` may run. Default 600. |
| `compact_after_chars` | When the chat holds about this many characters, crit starts a new chat with the instructions and a summary of the task so far. It also does this when the model breaks the tool format twice in a row. Default 300000; values under 10000 are ignored. |
| `reply_retries` | How many times a failed or empty reply from the chat page is retried before the task ends. Default 3. |

### Use a ChatGPT Project for the instructions

The tool protocol is long. Sending it as the first message works, but the model can lose track of it in a long chat. A ChatGPT Project keeps it pinned:

1. In chatgpt.com, create a Project and paste the `<<<SYSTEM>>>` section of `prompts/agent.txt` into the Project's instructions.
2. Point `url` in `config.json` at that Project.
3. Set `"seed_instructions": false` in `.bot/settings.json`.

`/new` and compaction open an empty chat inside the same Project or GPT: a conversation link such as `https://chatgpt.com/g/g-x/c/abc` becomes `https://chatgpt.com/g/g-x`.

### Tools

The model has 21 tools. `/tools` lists them in a session.

| Tool | What it does |
| --- | --- |
| `list_files`, `find_files` | List one folder, or find files by name at any depth. Generated trees such as `out`, `build`, `prebuilts`, and `node_modules` are skipped. |
| `read_files` | Read up to 20 files in one call, a window of a long file, or one function or class by name. An image or binary file returns only its type and size. |
| `search_code` | Regular-expression search. Definitions come first. |
| `edit_file`, `write_files`, `apply_patch` | Change files. Edits show a diff and a syntax check. |
| `move_file`, `delete_file` | Move, rename, or delete one file. |
| `run_command` | One command in the session shell. Takes `cwd`, `timeout`, `shell`, and `background`. |
| `command_output`, `kill_command` | Read new output from a background command, or stop it. |
| `git_status`, `git_diff`, `git_log`, `git_show` | Read git state. |
| `web_fetch` | Read one http or https page as text. Asks first, per site. |
| `ask_user` | Ask you one question when the task needs your decision. |
| `todo` | The task list for a job with several steps. |
| `skill` | Load a `SKILL.md` from `.bot/skills`, `.agents/skills`, or `.opencode/skills`. |
| `code_graph` | Callers, callees, and impact from the project's code graph. |

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

Before a task changes a file for the first time, `crit` saves a copy under `.bot/cache/undo/`. `crit undo` (or `/undo` inside a session) restores the files changed by the most recent task, brings back files it deleted or moved, and deletes the files it created. Run it again to step back one more task. The last 20 tasks are kept.

## What stays where it is

Windows:

| Path | Role |
| --- | --- |
| `C:\path\to\critique-bot\` | Checkout. The commands run code from here. |
| `C:\path\to\critique-bot\.venv\` | The install from `pip install -e .`. |
| `C:\path\to\critique-bot\config.json` | Chat URL and selectors. The `crit` command passes this every time. |
| `C:\path\to\critique-bot\.edge-profile\` | Signed-in browser session. `user_data_dir` points here with a full path. |
| `%LOCALAPPDATA%\critique-bot\bin\` | `crit.cmd`, `bot-agent.cmd`, and `critique-bot.cmd` on your user `PATH`. |
| `C:\some-project\.bot\` | Per-project settings, index, history, undo copies, and sessions, created the first time you run `crit` there. |

Linux and macOS:

| Path | Role |
| --- | --- |
| `/path/to/critique-bot/` | Checkout. The commands run code from here. |
| `/path/to/critique-bot/.venv/` | The install from `pip install -e .`. |
| `/path/to/critique-bot/config.json` | Chat URL and selectors. The `crit` command passes this every time. |
| `/path/to/critique-bot/.edge-profile/` | Signed-in browser session. `user_data_dir` points here with a full path. |
| `~/.local/bin/` | `crit`, `bot-agent`, and `critique-bot` wrappers on your `PATH` (set in `~/.bashrc` or `~/.zshrc`). |
| `~/some-project/.bot/` | Per-project settings, index, history, undo copies, and sessions, created the first time you run `crit` there. |
