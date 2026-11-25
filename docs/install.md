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
Windows) and the `crit` and `critique-bot` launchers in `~/.crit/bin`, which is added to your
PATH. Run the same line again to update. `critique-bot install --uninstall` removes it (your
settings stay). An install from before 1.1 (`%LOCALAPPDATA%\Programs\critique-bot`, or
`~/.local/share/critique-bot` with launchers in `~/.local/bin`) is removed by the next install. Set `CRIT_CONFIG_FILE` (Linux) or `$env:CRIT_CONFIG_FILE` (Windows) before the
one-liner to also store the config file's path, so the first run asks nothing.

Instead of the one-liner you can download the zip yourself and run the program: the first
time it runs (a double-click on `critique-bot.exe`, or `./critique-bot` on Linux) it installs
itself for your user without a question ("Installed crit <version> in <path> and added it to
your PATH."), then does what you asked. `CRIT_NO_AUTO_INSTALL=1` turns that off; the .deb's
copy in /opt/critique-bot never installs itself.

If the page stays blank, it probably needs its sign-in site (or a site for
its files). crit names each site it blocked; allow the ones that belong to the service with
`crit config --allow-host <site>`, or with the **Allow** buttons on the `crit setup` page.

## Release files

Download from the repository's **Releases** page. Each release has:

| File | For |
|---|---|
| `critique-bot_<version>_amd64.deb` | Ubuntu 22.04+, Debian 12+ (x64) |
| `critique-bot_<version>_arm64.deb` | the same, on Arm (Graviton, Ampere, Raspberry Pi 5 with 64-bit OS) |
| `critique-bot-<version>-linux-x64.zip`, `-linux-arm64.zip` | other Linux distributions with glibc 2.35 or newer (RHEL 9+, Fedora, ...) |
| `critique-bot-<version>-windows-x64.zip` | Windows 10 and 11 (64-bit) |
| `install.ps1`, `install.sh` | the one-line installers above |
| `SHA256SUMS.txt` | checksums of every file |

You need Microsoft Edge or Google Chrome installed (crit drives the browser that is already
there), unless you use `"backend": "local"`.

## Check the download

Linux:

```bash
sha256sum --check --ignore-missing SHA256SUMS.txt
# optional, proves the file was built by this repository's release workflow:
gh attestation verify critique-bot_1.0.0_amd64.deb --repo decoupled-dev/critique-bot
```

Windows (PowerShell):

```powershell
(Get-FileHash .\critique-bot-1.0.0-windows-x64.zip -Algorithm SHA256).Hash
# compare with the line for that file in SHA256SUMS.txt
```

## Linux, .deb

```bash
sudo apt install ./critique-bot_1.0.0_amd64.deb
```

This installs the program in `/opt/critique-bot`, adds the `crit` and `critique-bot`
commands, creates the `critique-bot` system user, and puts a starter config at
`/etc/critique-bot/config.json` (used by the worker service). Then [set it up](#set-up)
and run it in a project:

```bash
cd ~/src/my-project
crit                 # the coding agent; the first run asks a few questions
crit review          # review the current changes
```

Remove with `sudo apt remove critique-bot` (`purge` also deletes `/etc/critique-bot`).

## Linux, zip

```bash
unzip critique-bot-1.0.0-linux-x64.zip -d ~/.local/opt
ln -s ~/.local/opt/critique-bot-1.0.0-linux-x64/critique-bot ~/.local/bin/critique-bot
printf '#!/bin/sh\nexec %s --as-crit "$@"\n' ~/.local/opt/critique-bot-1.0.0-linux-x64/critique-bot > ~/.local/bin/crit
chmod +x ~/.local/bin/crit
```

Then [set it up](#set-up) and run `crit` in a project, as above.

## Windows

1. Unzip `critique-bot-1.0.0-windows-x64.zip` to a folder only you can write to, for example
   `%USERPROFILE%\.crit\app\1.0.0` (for a shared machine, `C:\Program Files\critique-bot`
   as an administrator).
2. Add that folder to your user PATH (Settings > System > About > Advanced system settings >
   Environment Variables), then open a new terminal.
3. In a project folder run `crit` (and `crit review`); the first run asks a few questions
   (see [Set up](#set-up)). `crit.cmd` runs `critique-bot.exe --as-crit`, so `critique-bot`
   takes the same commands.

The program is not code-signed yet, so SmartScreen may say "Windows protected your PC" on the
first run (More info > Run anyway) and company Defender policies may block it. For a company
rollout, sign `critique-bot.exe` with the company's code-signing certificate, or have IT allow
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
  app/<version>/       the installed program (critique-bot install)
  bin/                 the crit and critique-bot launchers, on PATH
```

Projects keep their own `.bot/` folder. The .deb's program stays in `/opt/critique-bot`
(apt manages it); your files still go in `~/.crit`. Settings from before 1.1
(`~/.config/critique-bot`, `%APPDATA%\critique-bot`) and the old browser profiles are moved
in once, with one line: `Moved crit's files to <path>.` A profile the browser has open stays
where it is; a line says so (sign in again, or close the browser and move it).

**First run.** Run `crit` in a terminal in a project folder. If a config file is already in
place (`CRITIQUE_CONFIG_FILE`, `/etc/critique-bot/crit.json`, or
`%ProgramData%\critique-bot\crit.json`), crit says `Using <path>` and carries on with no
question. Otherwise it asks, one question at a time:

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
A double-click on `critique-bot.exe` asks the same questions.

The browser window opens once to sign in (Web UI). Every other project folder asks nothing: the
role for a project is picked from its files (one line says which; `crit --role NAME` changes
it), and the terminal style is asked once per user (`crit config --theme ID` changes it).

**Or set it up in one line** (scripts, CI images, IT rollout). Every first-run answer has a flag:

```bash
crit config --config-file /srv/team/crit.json                        # a config file for many people
crit config --url https://ai.company.com/ --selector prompt_input=textarea --selector assistant_messages=.reply
crit config --backend local --local-url 192.168.1.50:8080 --model qwen2.5-coder-14b   # allows that host too
crit config --redact "Acme, Globex"                                  # names to hide (--unredact removes)
crit config --selector send_button=                                  # clears an optional page element
crit config --check                                                  # does the server (or the page's site) answer?
crit config                                                          # review the settings
```

For rolling out one file to many people, see [config-file.md](config-file.md).

Later, crit asks only when something is needed: if the page tries to reach another site
(its CDN or sign-in host), crit blocks it and, after the current step, asks once whether to
allow it from the next start on (default No; only say yes if it belongs to the service).
A command that needs your GitLab address asks for it once. Without a terminal (CI, the worker,
an IDE) crit never asks; it names the `crit config --url ... --selector prompt_input=...
--selector assistant_messages=...` or `crit config --backend local --local-url http://IP:PORT`
command to run instead and stops (exit 1).

Which file a command uses: `--config FILE`, else `CRITIQUE_CONFIG`, else the config recorded
for the project in `.bot/settings.json`, else an untracked `config.json` in the project root,
else your user file. See [config.json.md](config.json.md) for every setting and every
`crit config` flag.

## Settings that matter for company data

`crit config` shows these; the first run sets most of them:

- `"url"`: the address of the AI service. With `"egress": "strict"` (the default) it is the only host crit and its
  browser may reach, plus the hosts you list in `"allowed_hosts"`. A page that needs more
  hosts (its own CDN or sign-in) shows a plain message naming the host; add it only if it
  belongs to the service (crit asks, or `crit config --allow-host HOST`).
- `"cdp_url"` must be empty: strict mode does not attach to a browser crit did not start.
- `"redact_terms"`: company, customer, and project names replaced with `[REDACTED:name]`
  before anything is sent. `"redact_pii": true` (the default) also removes emails, phone
  numbers, card numbers, and national ID numbers.
- `"backend": "local"` keeps everything on the PC (a model server on localhost).

Messages in the terminal are plain sentences; add `--logs` to any command for diagnostics.

## Worker service (GitLab review runner, Linux)

```bash
sudoedit /etc/critique-bot/config.json        # url, selectors, gitlab.base_url, queue_dir
sudo systemctl enable --now critique-bot-worker
journalctl -u critique-bot-worker -f
```

See [DEPLOY.md](../DEPLOY.md) and [gitlab-ci.md](gitlab-ci.md) for the runner setup.
