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
crit            # the coding agent; the first run asks for the config file path
crit review     # review the current changes
crit config     # see or change the settings
```

Run the same line again to update. `critique-bot install --uninstall` removes it (your
settings stay). Set `CRIT_CONFIG_FILE` (Linux) or `$env:CRIT_CONFIG_FILE` (Windows) before the
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
   `%LOCALAPPDATA%\Programs\critique-bot` (for a shared machine, `C:\Program Files\critique-bot`
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

There is nothing to copy or edit. Your settings live in one file per user, created the
first time they are needed:

| System | File |
|---|---|
| Linux, macOS | `$XDG_CONFIG_HOME/critique-bot/config.json` (normally `~/.config/critique-bot/config.json`) |
| Windows | `%APPDATA%\critique-bot\config.json` |

The file and its folder are readable by you only. `crit config --path` prints where it is.

**The config file.** Your team gives you one file with the url, the page elements, and the
rules ([config-file.md](config-file.md)). Run `crit` in a terminal in a project folder:

- If the file is already in place (`CRITIQUE_CONFIG_FILE`, `/etc/critique-bot/crit.json`, or
  `%ProgramData%\critique-bot\crit.json`), crit says `Using <path>` and carries on. No question.
- Otherwise it asks once: `Config file path (Enter if none): `. Paste the path (`C:\...`,
  `\\server\share\crit.json`, or `/srv/...`; quotes are fine). A bad file is explained in one
  line and the question comes again.

Then the browser window opens once to sign in. Every other project folder asks nothing: the
role for a project is picked from its files (one line says which; `crit --role NAME` changes
it), and the terminal style is asked once per user (`crit config --theme ID` changes it).

**Without a config file** (Enter at the question): `No config file: crit needs the url and the
page elements.` Then:

1. A web page (cloud) or a model on this or another PC.
2. Cloud: the url (for example `https://ai.company.com/`; crit names the host and contacts no
   other site). Local: the server's address and which of its models to use.
3. Company, customer, or project names to hide (Enter for none).

It then says that personal data (emails, phone numbers, card numbers, ...) is hidden too. For
a web page it opens it so you can point at the message box, the send button, and the replies
(`crit setup`), then asks you to run the command again.

**Or set it up in one line** (scripts, CI images, IT rollout):

```bash
crit config --config-file /srv/team/crit.json                        # the config file
crit config --url https://ai.company.com/ --redact "Acme, Globex"     # or by hand; then: crit setup
crit config --backend local --local-url http://127.0.0.1:8080 --model qwen2.5-coder-14b
crit config                                                      # review the settings
```

Later, crit asks only when something is needed: if the page tries to reach another site
(its CDN or sign-in host), crit blocks it and, after the current step, asks once whether to
allow it from the next start on (default No; only say yes if it belongs to the service).
A command that needs your GitLab address asks for it once. Without a terminal (CI, the worker,
an IDE) crit never asks; it says which `crit config` command to run instead.

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
