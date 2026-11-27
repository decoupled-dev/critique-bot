# Install

## Quick install (recommended)

One line, no administrator rights. It downloads the latest release, checks it against the
release's `SHA256SUMS.txt`, installs it for your user, and puts `crit` on your PATH:

Windows 10/11 (PowerShell):

```powershell
irm https://github.com/decoupled-dev/critique-bot/releases/latest/download/install.ps1 | iex
```

Linux (x64 or arm64):

```bash
curl -fsSL https://github.com/decoupled-dev/critique-bot/releases/latest/download/install.sh | sh
```

Then, in any project folder:

```
crit            # the coding agent; the first run asks how to reach the model
crit review     # review the current changes
crit config     # see or change the settings
```

The program goes in `~/.crit/app/<version>` (`%USERPROFILE%\.crit\app\<version>` on
Windows) and the `crit` launcher in `~/.crit/bin`, which is added to your PATH. On Windows it
also appears in Settings > Apps > Installed apps. Run the same line again to update;
[Uninstall](#uninstall) removes it. An older 1.0.x install is removed by the next install. Set `CRIT_CONFIG_FILE` (Linux) or `$env:CRIT_CONFIG_FILE` (Windows) before the
one-liner to also store the config file's path, so the first run asks nothing.

Instead of the one-liner you can download the zip yourself and run the program: the first
time it runs (a double-click on `crit.exe`, or `./crit` on Linux) it installs
itself for your user without a question ("Installed crit <version> in <path> and added it to
your PATH."), then does what you asked. `CRIT_NO_AUTO_INSTALL=1` turns that off; the .deb's
copy in /opt/crit never installs itself.

If the page stays blank, it probably needs its sign-in site (or a site for
its files). The log file `~/.crit/logs/crit.log` names each site crit blocked; allow the ones that belong to the service with
`crit config --allow-host <site>`, or with the **Allow** buttons on the `crit setup` page.

## Release files

Download from the repository's **Releases** page. Each release has:

| File | For |
|---|---|
| `crit_<version>_amd64.deb` | Ubuntu 22.04+, Debian 12+ (x64) |
| `crit_<version>_arm64.deb` | the same, on Arm (Graviton, Ampere, Raspberry Pi 5 with 64-bit OS) |
| `crit-<version>-linux-x64.zip`, `-linux-arm64.zip` | other Linux distributions with glibc 2.35 or newer (RHEL 9+, Fedora, ...) |
| `crit-<version>-windows-x64.zip` | Windows 10 and 11 (64-bit) |
| `install.ps1`, `install.sh` | the one-line installers above |
| `SHA256SUMS.txt` | checksums of every file |

You need Microsoft Edge or Google Chrome installed (crit drives the browser that is already
there), unless you use `"backend": "local"`.

## Check the download

Linux:

```bash
sha256sum --check --ignore-missing SHA256SUMS.txt
# optional, proves the file was built by this repository's release workflow:
gh attestation verify crit_1.0.7_amd64.deb --repo decoupled-dev/critique-bot
```

Windows (PowerShell):

```powershell
(Get-FileHash .\crit-1.0.7-windows-x64.zip -Algorithm SHA256).Hash
# compare with the line for that file in SHA256SUMS.txt
```

## Linux, .deb

```bash
sudo apt install ./crit_1.0.7_amd64.deb
```

The package is `crit`. It installs the program in `/opt/crit`, adds the `crit` command
(`/usr/bin/crit`), creates the `crit` system user (home `/var/lib/crit`), puts a starter config
at `/etc/crit/config.json` (used by the `crit-worker.service` worker service), and the examples
in `/usr/share/crit/examples`. IT may put a shared config file at `/etc/crit/crit.json`. It
replaces the old `critique-bot` package, so `apt` upgrades from it. Then [set it up](#set-up)
and run it in a project:

```bash
cd ~/src/my-project
crit                 # the coding agent; the first run asks a few questions
crit review          # review the current changes
```

To remove it, see [Uninstall](#uninstall).

## Linux, zip

```bash
unzip crit-1.0.7-linux-x64.zip
./crit-1.0.7-linux-x64/crit install
```

That installs it in `~/.crit` for your user and puts `crit` on your PATH, the same as the
one-line installer (running `./crit` any other way installs it too). Open a new terminal, then
[set it up](#set-up) and run `crit` in a project, as above.

## Windows

1. Unzip `crit-1.0.7-windows-x64.zip`.
2. Double-click `crit.exe` (or run `.\crit.exe install`). It installs itself in
   `%USERPROFILE%\.crit`, adds it to your user PATH, registers crit under Settings > Apps >
   Installed apps, and asks the first-run questions (see [Set up](#set-up)).
3. Open a new terminal; in a project folder run `crit` (and `crit review`).

For a shared machine such as the GitLab worker, an administrator installs it under
`C:\Program Files\crit` instead; see [DEPLOY.md](../DEPLOY.md).

The program is not code-signed yet, so SmartScreen may say "Windows protected your PC" on the
first run (More info > Run anyway) and company Defender policies may block it. For a company
rollout, sign `crit.exe` with the company's code-signing certificate, or have IT allow
it by its SHA-256 hash from `SHA256SUMS.txt`.

## Set up

There is nothing to copy or edit. crit keeps everything it makes for you in one folder,
readable by you only (0700; an owner-only ACL on Windows), created the first time it is needed:

| System | Folder |
|---|---|
| Linux, macOS | `~/.crit` |
| Windows | `%USERPROFILE%\.crit` (`C:\Users\<name>\.crit`) |
| Anywhere | `CRIT_HOME` when set |

```
~/.crit/
  config.json          your settings (0600); crit config --path prints it
  browser-profile/     the signed-in browser (user_data_dir's default)
  msedge-user-data/    only with "user_data_dir": "system"
  cache/               the saved copy of the config file, and other caches
  logs/crit.log        the log file (see below)
  projects/            project data (for example, the home folder's own, in projects/home/)
  app/<version>/       the installed program (crit install)
  bin/                 the crit launcher, on PATH
```

Projects keep their own `.crit/` folder (settings, `AGENT.md`, skills, cache, sessions,
history). A project's old `.bot/` folder is renamed to `.crit/` on first use; when both exist,
`.crit` is used. The .deb's program stays in `/opt/crit` (apt manages it); your files still go
in `~/.crit`. Settings from 1.0.x (`~/.config/critique-bot`, `%APPDATA%\critique-bot`) and the
old browser profiles are moved in once; the log file notes the move. A profile the browser has
open stays where it is, and the log file says so (sign in again, or close the browser and move
it).

**The log file.** A normal run shows only questions (first-run setup, sign-in, approvals, and
the blocked-site `Allow?` question when `"page_network"` is `"strict"`), the agent's own screen,
install and uninstall results, and one plain line when crit cannot continue (`Problem: ...`, or
`crit stopped: ... Details: ~/.crit/logs/crit.log`). Everything else (blocked-site notices, the
project's role, which config file is used, the saved copy of the config file, file moves,
warnings) goes only to `~/.crit/logs/crit.log`. The log file is always written, readable by you
only, rotated at about 2 MB (3 files kept), and every line is redacted like outgoing messages.
`--logs` also prints its lines on stderr.

**First run.** Run `crit` in a terminal in a project folder. If a config file is already in
place (`CRIT_CONFIG_FILE`, `/etc/crit/crit.json`, or
`%ProgramData%\crit\crit.json`), crit uses it with no question (the log file notes which
file). The old `/etc/critique-bot/crit.json` and `%ProgramData%\critique-bot\crit.json` are
still read when the new ones are missing. Otherwise it asks, one question at a time:

```
How does crit reach the model?
  1) Local model (a model server on this PC or the network)
  2) Web UI (a page in the browser)
(Or use a config file: crit config --config-file PATH)
Choose 1 or 2:
```

**1, Local model.** `Model server (ip:port):` takes `192.168.1.50:8080`, `localhost:8080`, or
a full `http(s)://host:port/path` (http when no scheme is given; the port is required then).
crit checks it at once (`Checking http://... ...`) and says one of: `reachable, N models: ...`,
`reachable, but it does not list models`, `refused: nothing is listening on ...`,
`timed out: ... does not answer (firewall or wrong address?)`, or `unknown host: ...`. On a
failure it asks `Try another address? [Y/n]`; `n` saves it anyway (crit checks again on the
next run). Then `Model [first]:` (Enter for the first, a number from the list, or a name), or
`Model name:` when the server lists none. A server on another PC goes into `allowed_hosts`,
and crit says `crit will send prompts only to <host>.`

If the server answers 401 or 403, crit says `the server needs an API key` and asks
`API key: ` (what you type is not shown). A refused key is asked once more, then
`Try another key? [Y/n]`. The key is saved in your own `~/.crit/config.json` as
`local.api_key` and sent as `Authorization: Bearer <key>` only to that server. crit does not
ask about the context window: with `local.num_ctx` at 0 it reads it from the server
(llama.cpp or vLLM) and otherwise assumes 32768.

**2, Web UI.** `url:` (https; plain http only for localhost), then
`Model (Enter for the page's default):`, then the page elements, each with a starter guess
that Enter accepts: message box, send button, stop button, replies, and (Enter to skip) the
model picker, its identifier, and its options. A string that is plainly not CSS (an unclosed
bracket or quote) is refused with one line. With a model but no model picker, one line says
crit keeps the page's current model. Then: `Saved. To check the page elements against the
real page: crit setup`. crit does not open `crit setup` by itself; run it when you like.

Either way it then asks for company, customer, or project names to hide (Enter for none),
says that personal data (emails, phone numbers, card numbers, ...) is hidden too, prints
`Saved to <path>.` and `Change any of this later with: crit config (see crit config --help)`,
and carries on with your command. Ctrl+C at any question: `Stopped; nothing was written.`
A double-click on `crit.exe` asks the same questions.

The browser window opens once to sign in (Web UI). Every other project folder asks nothing: the
role for a project is picked from its files (the log file says which; `crit --role NAME`
changes it), and the terminal style is asked once per user (`crit config --theme ID` changes it).

**Or set it up in one line** (scripts, CI images, IT rollout). Every first-run answer has a flag:

```bash
crit config --config-file /srv/team/crit.json                        # a config file for many people
crit config --url https://ai.example.com/ --selector prompt_input=textarea --selector assistant_messages=.reply
crit config --backend local --local-url 192.168.1.50:8080 --model qwen2.5-coder-14b   # allows that host too
crit config --api-key                                                # the local server's API key (not shown; - reads stdin, none removes)
crit config --redact "Acme, Globex"                                  # names to hide (--unredact removes)
crit config --selector send_button=                                  # clears an optional page element
crit config --check                                                  # does the server (or the page's site) answer?
crit config                                                          # review the settings
```

For rolling out one file to many people, see [config-file.md](config-file.md).

Later, crit asks only when something is needed. If the page tries to reach another site
(its CDN or sign-in host) that crit blocks, the log file names it; with `"page_network":
"strict"` crit also asks once, after the current step, whether to allow it from the next start
on (default No; only say yes if it belongs to the service).
A command that needs your GitLab address asks for it once. Without a terminal (CI, the worker,
an IDE) crit never asks; it names the `crit config --url ... --selector prompt_input=...
--selector assistant_messages=...` or `crit config --backend local --local-url http://IP:PORT`
command to run instead and stops (exit 1).

Which file a command uses: `--config FILE`, else `CRIT_CONFIG`, else the config recorded
for the project in `.crit/settings.json`, else an untracked `config.json` in the project root,
else your user file. See [config.json.md](config.json.md) for every setting and every
`crit config` flag.

## Settings that matter for company data

`crit config` shows these; the first run sets most of them:

- `"url"`: the address of the AI service. With `"egress": "strict"` (the default) it is the only host crit and its
  browser may reach, plus the hosts you list in `"allowed_hosts"`. A page that needs more
  hosts (its own CDN or sign-in) gets a line in the log file naming the host; add it only if it
  belongs to the service (crit asks, or `crit config --allow-host HOST`).
- `"cdp_url"` must be empty: strict mode does not attach to a browser crit did not start.
- `"redact_terms"`: company, customer, and project names replaced with `[REDACTED:name]`
  before anything is sent. `"redact_pii": true` (the default) also removes emails, phone
  numbers, card numbers, and national ID numbers.
- `"backend": "local"` keeps everything on the PC (a model server on localhost).

Messages in the terminal are plain sentences; diagnostics go to `~/.crit/logs/crit.log`
(add `--logs` to any command to also see them on stderr).

## Worker service (GitLab review runner, Linux)

```bash
sudoedit /etc/crit/config.json        # url, selectors, gitlab.base_url, queue_dir
sudo systemctl enable --now crit-worker
journalctl -u crit-worker -f
```

See [DEPLOY.md](../DEPLOY.md) and [gitlab-ci.md](gitlab-ci.md) for the runner setup.

## Uninstall

`crit uninstall` asks nothing. It removes `~/.crit` (the program, the launcher, your settings,
the sign-in browser profile, `msedge-user-data`, the cache, and the logs) but keeps
`~/.crit/projects`, which holds project data. It also removes the 1.0.x layouts (see below),
crit's PATH entries (the Windows user PATH; on Linux only the lines marked
`# added by crit install` or `# added by critique-bot install` in `~/.profile`, `~/.bashrc`, and
`~/.zshrc`), and the Windows Installed apps entry. It never removes a project's own `.crit` (or
`.bot`) folder. It ends with a line such as:

```
crit is removed (settings, sign-in, and program). Project .crit folders were kept.
```

**Windows:** Settings > Apps > Installed apps > crit > Uninstall, or run `crit uninstall`. The
running `crit.exe` is removed a few seconds after it exits.

**Linux, one-line install or zip:**

```bash
crit uninstall
```

**Linux, .deb:** `crit uninstall` removes your own files, then prints the command for the
package (crit never calls sudo itself):

```bash
crit uninstall
sudo apt purge crit
```

### Remove a 1.0.x install by hand

`crit uninstall` removes these for you. Without it, delete what exists:

- Linux: `~/.config/critique-bot`, `~/.local/share/critique-bot`, and the `crit` and
  `critique-bot` launchers in `~/.local/bin`; the lines marked `# added by critique-bot install`
  in `~/.profile`, `~/.bashrc`, and `~/.zshrc`.
- Linux, old .deb (program in `/opt/critique-bot`): `sudo apt purge critique-bot`.
- Windows: `%APPDATA%\critique-bot`, `%LOCALAPPDATA%\critique-bot`, and
  `%LOCALAPPDATA%\Programs\critique-bot` (and remove that folder from your user PATH).
- A pip install: `pip uninstall critique-bot`.

The old `.bot` folders in your projects are project data (settings, skills, sessions); keep
them or delete them as you like. crit renames one to `.crit` the next time it runs there.
