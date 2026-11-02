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

The first `crit` creates `.bot` in that folder (settings, a symbol index, and a sessions folder), asks which text style looks right, asks which role crit plays in this project (see [Roles](#roles)), then asks you to sign in. When the profile from step 1 is already signed in, it prints `Already signed in.` and goes on. Otherwise it opens a normal browser window for the login: no automation, no extra command-line flags, and no network filter, so Google and Microsoft sign-in work and there is no "unsupported command-line flag" bar. For chatgpt.com the window closes by itself once the session cookie is saved; for other sites, close it when you are signed in. If the chat page later shows a **Log in** button, crit warns that it is not signed in: a signed-out ChatGPT answers with a smaller model and may return empty replies to the tool instructions. An empty folder is enough. `crit init` rebuilds the index later. An existing `.bot/settings.json` is left as it is.

The task uses the chat URL, selectors, and browser profile from the clone's `config.json`. Tasks stay headless. Add `--headed` when you want the window on a later task:

```bash
crit --headed "create a hello.txt file that says hello"
```

Later tasks from the same folder, or from any other folder after the first `crit` there, are the task text alone:

```bash
crit "update the test cases"
crit --yes "fix the failing test"
crit --plan "add a VHAL property for seat heating"
crit undo
```

`crit` with no task opens the session screen and waits for you to type. `crit "task"` runs that task first, then waits for the next one.

## The crit screen

Each session opens with a box that shows the folder, model (and, for a local model, the server it runs on), shell, mode, and role. Type at the `>` prompt:

| Key | Effect |
| --- | --- |
| Enter | Send the message. Pasted text with several lines stays one message. |
| Alt+Enter, Ctrl+J, or `\` at the end of a line | New line. |
| Shift+Tab (or Alt+M) | Switch the mode: ask, accept edits, auto, plan (see [Modes](#modes)). The line under the input shows the mode. |
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
| `/mode [name]` | List the modes, or switch to `ask`, `edits`, `auto`, or `plan`. `/permissions` is the same. |
| `/plan` | Plan mode for the next tasks. |
| `/auto` | Auto mode for the rest of the session. |
| `/skills [name ...]` | List the skills, or pin some for every task this session. `/skills off name` unpins. |
| `/role [name]` | Show the roles, or switch this project's role (see [Roles](#roles)). The choice is kept in `.bot/settings.json`, and the next task starts a new chat with the role's instructions. |
| `/status` | Model, shell, folder, mode, role, pinned skills, and background commands. |
| `/shell [name]` | List the shells, or switch the default for this session. |
| `/tools` | The tools the model can call. |
| `/commands` | The shell commands run this session: exit code, time, shell, and whether one was retried or moved to the background. |
| `/theme` | Change the text style. |
| `/exit` | Leave (`exit` and `quit` work too). |

While tools run, a status line counts them and the time, for example `✻ Running 1 shell command… (2m 14s · ctrl+c to interrupt)` or `Reading 3 files, searching 1 pattern…`, with the last lines of a command's output above it. Each tool step then prints one line such as `● Read(src/app.py)` with the result under it, `⎿  Read 120 lines`. Edits show a short numbered diff, and commands show the exit code, time, and the first lines of output. While the chat is replying, a line such as `✻ Thinking… (23s · ctrl+c to interrupt)` counts the seconds.

Ctrl+C during a task ends that task as INTERRUPTED. It stops the running command and every process it started, stops background commands, and clicks the chat page's stop control so the half-written reply does not mix into the next one. The session stays open for the next task. If the page is still writing a reply when the next message goes out, crit waits for it to finish, or stops it after about 20 seconds. Both depend on [`selectors.stop_button`](config.json.md#stop_button).

### How a task runs

The instructions from [`prompts/agent.txt`](../prompts/agent.txt) go out in front of the first task, in the same message, so the first reply already works on it. The role's paragraph (see [Roles](#roles)) follows them. The model answers with `<tool_call>` blocks. crit runs them and sends the results back, each followed by a short STATE block (files read, files changed, last command), until the model finishes.

- A question with no file change is answered in words, and that task ends.
- A reply that answers and also prints tool calls shows the answer and runs the calls.
- A refusal, a "what should I change?", or a promise to keep working is sent back until the model calls a tool or finishes. Three in a row are sent back; the next one ends the task as FAILED.
- A real question that needs your decision is shown to you, and your answer goes back to the model. The model can also ask with the `ask_user` tool. With no terminal, the model is told no user is available and decides itself.
- COMPLETED, DONE, or a reply that no edit is needed ends the task. After an edit, a set check command runs first (see `check_command` below).

### Roles

A role is the persona the instructions are shaped around. The first interactive `crit` in a folder asks for one on a screen like the text-style one (arrows move, Enter keeps, Esc means Generic for now and asks again next time), and stores it as `"role"` in `.bot/settings.json`, so it is asked once per project. The role's paragraph goes out right after the standard instructions in every new chat. The header box and `/status` show the current role.

| Role | `"role"` | What changes |
| --- | --- | --- |
| Code Reviewer | `code-reviewer` | Reads the diff, callers, and tests; reports findings by severity (correctness, security, API breaks, missing tests, conventions) with file, line, and the smallest fix. Rewrites code only when the task asks for a fix. |
| AOSP Expert | `aosp-expert` | Android platform work: frameworks, system services, HALs, SELinux, Soong, vendor partitions, Automotive. Keeps changes in the owning layer and respects the Treble boundary. Pins the `aosp` skill. |
| Expert Developer | `expert-developer` | Complete, production-grade changes that follow the codebase's own patterns, with tests and a passing build. |
| AI Expert | `ai-expert` | Software that uses ML models and LLM APIs: prompts, tool calling, agents, retrieval, evaluation, on-device inference. Treats model output as untrusted. |
| Test Engineer | `test-engineer` | Tests that prove behavior in the project's framework; a failing test first for a bug; production code changes only when a test exposes a bug. |
| Android App Engineer | `android-app-engineer` | Kotlin, Jetpack, Compose, Hilt, coroutines, Gradle; lifecycle, permissions, process death. Pins the `android` skill. |
| Generic | `generic` | The standard instructions only. The default when nothing is stored. |

`crit --role NAME` sets the role for one run (`AOSPExpert`, `aosp-expert`, and `aosp expert` all work). On a project with no stored role it is stored too; later it applies to that run only. `/role NAME` during a session stores the role and starts a new chat for the next task. A piped run (no terminal) never asks: it uses the stored role or Generic. The text of each role is a `<<<ROLE_...>>>` section of [`prompts/agent.txt`](../prompts/agent.txt).

### Modes

Shift+Tab cycles through four modes, as in Claude Code. The line under the input box shows the one in use.

| Mode | What asks first | Start with |
| --- | --- | --- |
| **ask** (default) | Every edit, command, web fetch, and path outside the project. | `crit`, or `"permissions": "ask"` |
| **edits** (accept edits) | Commands, fetches, and outside paths. File edits in the project run at once. | `--permission-mode edits` |
| **auto** | Commands that are not read-only (once per program with "don't ask again"), and risky commands (below). Edits, reads, and read-only commands run, and questions from the model are answered with "decide yourself". | `crit --yes` (also `--auto`, `-y`), `/auto` |
| **plan** | Nothing changes. The model reads the code and sends a plan, then you approve it. | `crit --plan`, `/plan` |

**Risky commands** ask in every mode, auto included. These are deletes of a whole tree (`rm -rf /`, `~`, `.`, `.git`, a drive root), `git reset --hard`, `git clean -f`, `git checkout -- .`, any `git push`, `repo upload`, `fastboot flash`/`erase`, `dd`, `mkfs`, `format`, `sudo`, shutdown or restart, publishing a package, piping a download into a shell, reading a file that usually holds secrets, anything outside the workspace, and running a file this task wrote. That box has only Yes and No; "don't ask again" does not cover them. `"confirm_risky": false` lets auto mode run them too.

**Plan mode.** The task goes out with plan-mode instructions. The model may read and search files, ask the code graph, fetch web pages, and run read-only commands (`git log`, `git diff`, `ls`, `grep`, `Get-Content`, `./gradlew tasks`, `adb devices`). Edits, writes, and builds are not run; the model is told to finish reading and send the plan. The plan comes back as **Goal, Findings, Steps, Risks, Verify**, and a box asks:

1. **Yes, start now in auto mode**
2. **Yes, start and auto-accept edits**
3. **Yes, start and ask before each edit and command**
4. **No, keep planning**, then type what to change (or type `no, also update the tests`)

On a yes the mode switches and the model carries out the plan in the same chat. Esc keeps the plan and changes nothing. A question ("why does X fail?") is answered as usual and is not planned.

### Approvals

Reading and searching run without asking. In ask mode, edits, commands, web fetches, and anything outside the project folder show a box first:

1. **Yes** runs this step.
2. **Yes, and don't ask again** remembers the choice for this session: all file edits, commands that start with the same program (for example every `npm` command), or one web site. It never covers a command that chains, redirects, or nests another command; those are asked each time. Paths outside the project are always asked.
3. **No** (or Esc) skips the step. After `3` you can type what to do instead, and the model gets that note.
4. **Yes, and switch to auto mode** runs this step and stops asking about edits for the rest of the session (commands that are not read-only, and risky ones, still ask).

When there is no "don't ask again" row (a path outside the project), No is `2` and auto is `3`.

Use the arrow keys and Enter, or type an answer and press Enter: a number (`1`, `2.`, `(3)`), or a word in any case (`yes`, `Yes`, `ok`, `always`, `auto`, `no`). The highlight follows what you type. `no, use pnpm instead` or `3 use pnpm` declines and sends the rest as the note. Text that matches no option is not taken as an answer; crit asks again. With no terminal (a pipe or a CI job), steps that would ask are declined.

### Builds

crit treats a build differently from other commands. Builds include `gradlew`, `gradle`, `mvn`, AOSP `m`/`mm`, `make`, `ninja`, `cmake --build`, `atest`, `npm install`, `npm ci`, `dotnet build`, `cargo build`, and `build.sh`/`build.ps1`.

- **Time.** A build gets at least 30 minutes (`build_timeout`), whatever timeout the model asked for. Other commands get 2 minutes unless the model asks for more, up to 10.
- **Nothing is lost at the timeout.** A command still running at its timeout is not killed. It continues as a background job (`b1`), and the model waits for it with `command_output`. A command waiting for input (`[y/n]`, `password:`) is still stopped.
- **The JDK and SDK are set for the project.** crit finds every installed JDK. It looks in Android Studio's own `jbr`, `C:\Program Files\Java`, Eclipse Adoptium, Microsoft, Zulu, Corretto, `~/.gradle/jdks`, `~/.jdks`, SDKMAN, `/usr/lib/jvm`, and `/Library/Java/JavaVirtualMachines`. It then picks the one the project needs: AGP 8 needs JDK 17+, AGP 7 needs 11, and the wrapper's Gradle version caps the newest. When JAVA_HOME fits, it is kept. The Android SDK is found from `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `sdk.dir` in `local.properties`, or the default folder (`%LOCALAPPDATA%\Android\Sdk` on Windows). Commands run with both set, and ENVIRONMENT tells the model which ones.
- **The result starts with what matters.** A failed build's result begins with **SUMMARY**: Gradle's "What went wrong", the `e:`/`error:` lines, and BUILD FAILED. Then comes **HINTS** for the failures crit recognizes:
  - SDK not found (with the `sdk.dir` line to write)
  - wrong Java (with the installed JDKs)
  - a corporate proxy's HTTPS certificate (`trustStoreType=Windows-ROOT` on Windows)
  - network or proxy settings
  - locked files
  - out of memory
  - SDK licenses or a missing SDK package
  - a missing `gradle-wrapper.jar`
  - PowerShell's `.\` rule
  - the Windows path length limit
  
  A successful `assembleDebug` lists the APK/AAB files it wrote under **ARTIFACTS**, with their sizes.
- **One automatic retry.** A build that failed because Gradle's files were locked or its daemon died is retried once after `gradlew --stop`. A build cut off by a dropped download or a 5xx from a repository is retried once after 5 seconds. Compile errors, a wrong JDK, and certificate problems are not retried, since they would fail the same way. The result says `retried once: <reason>`.
- **The wrapper is called the right way.** In PowerShell, a bare `gradlew` or `./gradlew` becomes `.\gradlew.bat`, and in cmd it becomes `gradlew.bat`. `--console=plain` is added. Windows PowerShell 5.1's `NativeCommandError` decoration around a program's stderr is removed from the output.
- **The model does not hand the work back.** A reply such as "please build the APK yourself in Android Studio" or "run this command on your machine" is sent back, up to twice, with the instruction to run it with `run_command` and fix the cause from the hints.

Every command is listed by `/commands` and saved in `.bot/sessions/<stamp>/agent.json` under `commands`.

### Helper chats: small sessions for parts of a task

The main chat stays sharp when it stays small. A part of a task whose details the main chat does not need can go to a **helper chat**: a fresh chat that does that part and sends back only a short report. The main chat (the coordinator) calls the `delegate` tool with a self-contained brief, for example "find where the camera is opened and report the file, the class, and the line", "read `PaymentService.kt` and summarize how retries work", "add the permission to `AndroidManifest.xml`", or "run the unit tests and report only the failures". The files a helper reads and the output it sees never land in the main chat; only its report does. Several briefs run at the same time when the parts do not depend on each other.

- **Every brief starts a fresh chat.** A helper tab opens on its first brief and stays open, but each new brief starts a new chat in it, with the instructions in front, so nothing of an earlier brief carries over.
- **The chats cannot see each other.** crit is what they share. Every helper works on the same folder through crit's tools, and its final report goes back to the coordinator as the result of the `delegate` call.
- **Each file has one owner.** A brief lists the files that helper may change, and crit refuses an edit to any other file. Two helpers never get the same file, and a brief with no files is read only.
- **Helpers only read.** Helpers cannot ask you anything, so they run only read-only commands: no builds or tests, no fetches, no secret files, nothing outside the workspace. The coordinator runs builds and tests itself after the reports.
- **The coordinator finishes the job.** It reviews the helpers' changes (`git_diff`), fixes what does not fit together, and trusts a build or test result only when the report quotes it. The diff at the end, the check command, and `/undo` cover the helpers' changes too.
- **One approval.** In ask mode the split is approved once, for the files it lists. A read-only split runs without asking, also in plan mode, so planning can investigate several places at once.
- The status line shows `Running 2 helper chats…` with each helper's current step.

`"helper_sessions"` in `.bot/settings.json` sets how many helper chats may run at once (default 2, at most 4, 0 turns it off). Each helper's chat counts against the same ChatGPT account limits. With the browser backend, helpers need its remote debugging, which crit turns on for its own browser; with no remote debugging, the task runs in one chat and `delegate` says so. With a local model, helpers are plain extra conversations with the server.

### Long sessions and replies that never come

**A reply that never comes.** crit waits for a reply as long as it shows signs of life, not for a fixed time. A thinking model that shows "generating" for minutes before writing is left alone, up to 10 minutes. These cases count as no reply:

- **The send did not go out.** No reply and no generating signal within 60 seconds while the prompt is still in the input box. crit sends it once more.
- **The page does not answer.** No reply 60 seconds after a send that went out.
- **The reply stalled.** It started, then nothing for 90 seconds.
- **The page returned an empty reply, or showed its own error.**

crit then stops whatever the page is still writing and tries again: once in the same chat when nothing was answered yet, then in a **new chat** that gets the instructions, a summary of the task (files read and changed, the to-do list, the last command and its output), and the message it was waiting on. A reply that started and then stalled leaves a half-written answer in the chat, so that retry goes to a new chat at once. A broken conversation therefore cannot block the task. The timing settings are in [`docs/config.json.md`](config.json.md#waiting-for-a-reply).

**A chat that keeps going wrong.** A web chat that has been pushed through a few retries often stays bad: replies come back cut off, the format breaks, simple steps fail, while the same step works at once in a fresh chat. crit keeps a short health record of the last six replies and leaves the chat when:

- three of the last six replies were bad (cut off, retried, made-up results, broken tool format, a refusal, or the task handed back), even with good replies between them;
- a reply was cut off twice in the same chat (the first time, the same chat is asked for the rest);
- the model breaks the tool format twice in a row.

**A long session.** A web chat slows down and forgets early instructions as it grows. In the middle of a task crit starts a new chat when any of these happens, and a new task starts in a new chat at half of each:

- the chat holds about 120,000 characters (`compact_after_chars`);
- it has had 30 messages (`compact_after_turns`);
- it has been open 60 minutes (`compact_after_minutes`).

Before moving, crit asks the old chat for a **handoff note** in its own words: the task, what it learned about the code (files, symbols, line numbers), what it changed and why, what is left, and what failed. The new chat gets the instructions, crit's summary of the task, that note, the message in progress, and a list of the earlier tasks of the session with the files each one changed. At a task boundary only the list is needed. `"handoff_notes": false` skips the note and saves one round trip.

### Use a local model instead of the chat page

The model behind crit is an adapter chosen by `backend` in `config.json`. `browser` (the default) drives the chat page in Edge. `local` calls a model served on this PC, such as Qwen or Gemma in Ollama, or anything behind an OpenAI-compatible server (llama.cpp, LM Studio, vLLM). The agent, the roles, the skills, the modes, and the review commands are the same with both; only the way a message reaches the model changes.

```bash
ollama pull qwen2.5-coder:14b
cp config.local.example.json config.json
crit "explain the build setup"
```

With a local model there is no sign-in step and no browser window. The header shows the model and where it runs (`qwen2.5-coder:14b · local at 127.0.0.1:11434`). The first run in a folder still asks for the text style and the role. Helper chat tabs (`delegate`) work: each helper is its own conversation with the server. Starting a new chat is immediate, and a reply that stops arriving is retried the same way as a page that stops answering.

A local model has a smaller context window than a hosted one, so crit starts a new chat sooner (about `num_ctx × 2` characters) and sends smaller tool results (`num_ctx / 2` characters). Set `local.num_ctx` in `config.json` to what the model and the memory allow; `compact_after_chars` and `max_result_chars` in `.bot/settings.json` still override these defaults. Nothing leaves the PC: the server must be on localhost unless its host is in `allowed_hosts`. Keys and limits: [`config.json.md`](config.json.md#local-model-backend-local).

### Privacy

Only the conversation with the chat URL leaves this PC. With `backend: local` nothing leaves it at all: the model is called on localhost, and a model on another machine must be opted into through `allowed_hosts`. The browser is filtered to the chat's hosts on every channel (page loads, WebSockets, popups, service workers, WebRTC), `web_fetch` is off unless turned on, commands that can send data out do not run, tool telemetry is off, and known secrets are redacted before a message goes to the chat. Builds may still download dependencies unless `"offline": true`. Details and limits: [security.md](security.md).

### How replies are read

crit reads each reply from the page with its whitespace intact. The browser's `innerText` collapses runs of spaces and tabs in a paragraph, which used to strip the indentation from every `old_string`, `new_string`, and written file. The reader keeps text exactly as the model wrote it, keeps code blocks verbatim (without the language label and Copy button), and puts back the backticks of inline code. When the reply selector matches a whole turn and the markdown inside it, only the markdown is read, so the "ChatGPT said:" heading is never taken for the reply.

Edits and whole-file rewrites of XML (`.xml`, layouts, `AndroidManifest.xml`, `.csproj`, `.svg`, ...) are checked like Python and JSON: a change that would make a well-formed file malformed (an unescaped `&`, a mismatched tag, a missing `xmlns:android`) is not applied, and the model gets the line and the reason. Line endings (CRLF), a BOM, and the encoding of the file are kept.

Environment switches: `CRIT_NO_SANDBOX=1` adds `--no-sandbox` (needed only on Linux as root or in a container, where crit adds it by itself); `CRIT_LEAN_HEADLESS=0` keeps images and fonts in headless runs.

### Skills

crit ships expert guidance for these domains, and sends the ones that fit each task along with it:

| Skill | Covers |
| --- | --- |
| `android` | App architecture, lifecycle, manifest and permissions, coroutines, Hilt, Room, WorkManager, R8. |
| `aosp` | Platform work: Soong/`Android.bp`, system services, Binder/AIDL, HALs and VINTF, SELinux, init, overlays, API surfaces, `atest`. |
| `aaos` | Android Automotive: CarService and Car APIs, Vehicle HAL properties, driver distraction, occupant zones, car audio, power. |
| `android-testing` | JUnit 4/5, Espresso, Compose UI tests, Robolectric, Mockito/MockK, coroutine and Flow tests, flaky tests. |
| `android-debugging` | adb, logcat, tombstones, ANRs, dumpsys, bugreport, Perfetto, SELinux denials. |
| `gradle` | Wrapper, AGP, variants, version catalogs, JDK/toolchain errors, dependency conflicts. |
| `java` | Java 17/21 and AOSP style: null handling, exceptions, concurrency, collections. |
| `kotlin` | Kotlin 2.x: null safety, coroutines and Flow, Java interop, KSP, ktlint/detekt. |
| `jetpack-compose` | State, side effects, recomposition, Material 3, navigation, UI tests. |
| `cpp-native` | NDK, JNI, AOSP native code, ownership, thread safety, sanitizers. |
| `aspice` | Automotive SPICE: traceability, verification evidence, change impact, work products. |
| `automotive-safety` | ISO 26262, MISRA and AUTOSAR C++ themes, ISO 21434, defensive coding, deviations. |

How a skill is chosen for a task:

- **Words in the task.** "fix the espresso test" gets `android-testing`; "add a VHAL property" gets `aaos`; "ASPICE traceability" gets `aspice`. At most two skills go with a task (`max_skills`).
- **The project's files.** When the task names no domain, one skill is chosen from the files in the project, for example `AndroidManifest.xml` picks `android` and `build/envsetup.sh` picks `aosp`.
- **Pinned skills.** `/skills aosp aaos` pins skills for the session, and `"skills": ["aspice"]` in settings pins them for every session.

A skill goes out once per chat, and a new chat sends it again. The line `Skills: android-testing` shows which ones went with a task. The model can load any other skill with the `skill` tool.

To add a project skill, write `.bot/skills/<name>/SKILL.md`:

```markdown
---
name: our-hal
description: Rules for our vehicle HAL
keywords: vhal, our-hal, seat heating
files: hardware/our/vehicle
---
# Our HAL
...
```

A project skill with the same name as a built-in one replaces it.

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
| `max_result_chars` | Characters per tool-result message sent to the chat. The default is 20000 or `max_prompt_chars` from `config.json`, whichever is smaller; with a local model, `num_ctx / 2`. Smaller results keep the chat sharp for longer. Values under 4000 are ignored. |
| `seed_instructions` | Set to `false` when the tool instructions already live in a ChatGPT Project (see below). The first task then goes out with only the environment and project notes in front of it. |
| `theme` | Text style chosen on the welcome screen: `auto`, `dark`, `light`, `dark-colorblind`, `light-colorblind`, `dark-ansi`, or `light-ansi`. The session screen uses the same colors. `/theme` changes it. |
| `permissions` | The starting mode: `"ask"` (the default), `"edits"`, `"auto"` (like `--yes`), or `"plan"`. See [Modes](#modes). |
| `confirm_risky` | `false` lets auto mode run risky commands (a broad delete, `git reset --hard`, a push, a flash) without asking. Default `true`. |
| `auto_questions` | `"ask"` keeps showing the model's questions in auto mode. The default, `"decide"`, tells the model to choose itself. |
| `skills` | Skills sent with every task, for example `["aosp", "aaos"]`. The role's own skills (see [Roles](#roles)) are added to these. |
| `role` | The role crit plays in this project: `code-reviewer`, `aosp-expert`, `expert-developer`, `ai-expert`, `test-engineer`, `android-app-engineer`, or `generic`. Asked on the first interactive run; `/role` and `--role` change it. See [Roles](#roles). |
| `auto_skills` | `false` stops choosing skills from the task and project files; pinned skills still go. Default `true`. |
| `max_skills` | How many skills go with one task. Default 2. |
| `network_commands` | `"block"` (the default): commands that can send data off the PC do not run. `"ask"`: they run after a yes, asked in every mode. See [security.md](security.md). |
| `web_fetch` | `true` lets `web_fetch` read documentation sites (off by default: nothing leaves the PC but the chat). |
| `web_fetch_hosts` | More documentation sites `web_fetch` may read once it is on, for example `["docs.example.com"]`. |
| `offline` | `true`: commands reach no other machine. Package managers run offline, the Gradle wrapper gets `--offline`, git uses local repositories only, and `web_fetch` stays off. Builds then need their dependencies already cached. See [security.md](security.md). |
| `helper_sessions` | Helper chats a task may hand parts to with `delegate` (see [Helper chats](#helper-chats-small-sessions-for-parts-of-a-task)). Default 2, at most 4; 0 turns it off. |
| `build_timeout` | Seconds a build (gradle, mvn, m, npm install, ...) runs before it moves to the background. Default 1800. |
| `command_retries` | Automatic retries of a build that failed for a passing reason (locked files, a dropped download). Default 1; 0 turns it off. |
| `background_on_timeout` | `false` stops a command at its timeout instead of keeping it as a background job. Default `true`. |
| `syntax_preview` | `false` turns the welcome-screen syntax colors off. ctrl+t on that screen toggles it. |
| `shell` | Default shell for `run_command`: `auto` (the default: PowerShell 7, then Windows PowerShell 5.1 on Windows; bash, then sh elsewhere), `pwsh`, `powershell`, `cmd`, `bash` (Git Bash on Windows), `sh`, or `zsh`. A shell that is not installed falls back to `auto`. `/shell` changes it for one session. |
| `check_timeout` | Seconds the `check_command` may run. Default 600. |
| `compact_after_chars` | When the chat holds about this many characters, crit starts a new chat in the middle of a task; a new task starts in a new chat at half of it (see [Long sessions and replies that never come](#long-sessions-and-replies-that-never-come)). Default 120000, or `num_ctx × 2` with a local model; values under 10000 are ignored. |
| `compact_after_turns` | Start a new chat after this many messages in one chat (half of it between tasks). Default 30; at least 5. |
| `compact_after_minutes` | Start a new chat after it has been open this many minutes. Default 60; at least 5. |
| `handoff_notes` | `false` skips asking the old chat for a handoff note before moving to a new one. Default `true`. |
| `reply_retries` | How many times a failed, empty, or missing reply is retried before the task ends. The first retry goes to the same chat when nothing was answered yet; a stalled reply, an unhealthy chat, and every later retry go to a new chat with a summary of the task. Default 3. |

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
| `skill` | Load a built-in skill or a `SKILL.md` from `.bot/skills`, `.agents/skills`, or `.opencode/skills`. With no name, list them. |

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

### What a write must look like

Whole-file writes are where a model loses code, so `write_files` checks them before anything touches the disk:

- **Line numbers copied from a read are removed.** Contents where every line starts with `N|`, counting up from the first line, are the read output pasted back; the prefixes are stripped and the result says so.
- **A placeholder for left-out code is refused.** A comment line such as `// ... existing code ...`, `# rest of the file unchanged`, or `/* omitted for brevity */` means the model shortened the file. The write is not applied, and the result says which line and to use `edit_file` instead. The same check applies to `new_string` in `edit_file`. A bare `...` (an abstract Python body) and prose inside strings are not placeholders.
- **A rewrite that shrinks a file a lot needs `overwrite: true`.** Replacing a file of 40 or more lines with fewer than 40% as many is almost always a cut-off or elided rewrite. The result names both line counts; a real shrink is sent again with `overwrite` true, and the result then notes the shrink so `crit undo` is one step away.
- **A file changed on disk since it was read is not replaced blindly.** If a helper, a build, or you changed the file after the model read it, `write_files` without `overwrite` asks for a fresh read, so the model's copy never overwrites the newer one. The model's own edits keep its copy current.
- **Invalid JSON in a write names the fix.** When a `write_files` or `edit_file` call does not parse, the result repeats the escaping rules (a quote as `\"`, a newline as `\n`, a backslash as `\\`) and says to send a long file in parts.

Edits (`edit_file`) already match `old_string` exactly, then ignoring the `N|` prefixes, surrounding whitespace, and markdown punctuation, then by closest text; every loose match must be unique, and the result shows the closest block when nothing matches. Line endings, a BOM, and the file's encoding are kept, every write goes through a temporary file, and a checkpoint is saved first for `crit undo`.

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
