# The config file (for the people who roll crit out)

People who use crit should not have to configure anything. You write one **config file**
with the url, the page elements, and the rules, and put it where crit finds it. After
installing, crit either finds it on its own, or asks once for its path:

```
Config file path (Enter if none):
```

Then it says `Using <path>` and carries on. Every other folder the person opens crit in asks
nothing (the role for a project is picked from its files; the person signs in once).

A starter: [`config-file.example.json`](../config-file.example.json) (also in every release bundle).

## What it holds

It is a JSON object with the same keys as [config.json](config.json.md). Two things are required:

| Key | Required | What |
| --- | --- | --- |
| `url` | always | the https address of the page crit uses (for `backend: local`, the model server) |
| `selectors.prompt_input` | `backend: browser` (the default) | the message box |
| `selectors.assistant_messages` | `backend: browser` | where replies appear |

A file without them is refused, and crit stops with one line naming what is missing:

```
/srv/team/crit.json is missing: url, selectors.prompt_input
```

Everything else is optional; a key you leave out keeps crit's default:

| Key | Default | Notes |
| --- | --- | --- |
| `selectors.send_button`, `stop_button`, `continue_button`, model picker keys | empty | see [config.json.md](config.json.md#selectors-object-required-for-browser) |
| `allowed_hosts` | `[]` | other hosts the page may reach (sign-in, file hosts) |
| `egress` | `"strict"` | crit's own traffic: only the url's host and `allowed_hosts` |
| `page_network` | `"open"` | `"strict"`: the page too reaches only those hosts |
| `redact_terms` | `[]` | company, customer, project names to hide |
| `redact_pii` | `true` | hide emails, phone numbers, card numbers, national IDs |
| `backend`, `local` | `browser` | `local`: a model server (llama.cpp, LM Studio, vLLM) |
| `gitlab.base_url` | none | your GitLab, for reviews |
| `model`, `timeout_ms`, `idle_ms`, `max_files`, other limits | built-in | see config.json.md |

JSON has no comments. An optional block you could add to the example:

```json
"model": "gpt-4.1",
"gitlab": { "base_url": "https://gitlab.example.com" },
"timeout_ms": 240000,
"max_files": 40
```

Keys this crit version does not know are ignored (noted with `--logs`), so a newer file
works with an older crit.

## Locked and adjustable keys

crit layers three things: its built-in defaults, then the config file, then the person's own
settings (`crit config`). The file decides these, and `crit config` refuses to change them
("This is set in the config file <path>; change it there.", exit 2):

- `url`, `backend`, `local`, `cdp_url`, `allowed_hosts`, `redact_pii`, `gitlab.base_url`;
- each selector the file sets (one it leaves out stays the person's);
- `egress` and `page_network`: a person may tighten `"open"` to `"strict"`, never loosen;
  `CRITIQUE_EGRESS=open` does not loosen the file's `"strict"` either.
- `redact_terms`: the person may add names, never remove the file's.

Everything else (model, timeouts, limits) is a default: the file's value applies unless the
person set their own. Environment overrides such as `CRITIQUE_CHAT_URL` do not change a
locked key.

`crit config` shows the file first and marks what comes from it:

```
Config file: /srv/team/crit.json
url: https://ai.example.com/ (config file, locked)
...
```

## Where crit looks

The first of these that is set:

1. `config_file` in the person's user config (written by the first-run question or
   `crit config --config-file PATH`; `--config-file none` removes it);
2. the `CRITIQUE_CONFIG_FILE` environment variable;
3. the system location, if the file exists: `/etc/critique-bot/crit.json` on Linux and macOS,
   `%ProgramData%\critique-bot\crit.json` on Windows.

With 2 or 3 the first run asks nothing: it says `Using <path>` and goes on.

Paths may be UNC (`\\server\share\crit.json`; kept as given) or `C:\...`; quotes from a
pasted "Copy as path" are removed.

## Deployment

- **A file share.** Put `crit.json` on a share everyone can read, and tell people the path
  (or set it at install, below). Changes reach everyone on their next crit start.
- **Windows (Intune, GPO, SCCM).** Copy the file to `%ProgramData%\critique-bot\crit.json`
  (readable by Users). Nobody is asked anything.
- **Linux (Ansible, Puppet, an image).** Copy it to `/etc/critique-bot/crit.json`, mode 0644.
  The .deb creates `/etc/critique-bot` readable by everyone (its worker `config.json` in there
  stays 0640), so if that file exists nothing else is needed.
- **At install time.** `CRIT_CONFIG_FILE=/path/crit.json` with `install.sh`, or
  `$env:CRIT_CONFIG_FILE = "\\server\share\crit.json"` before `install.ps1`, or
  `critique-bot install --config-file PATH`: crit stores the path for that user after installing.

The downloaded program installs itself the first time it runs (no question;
`CRIT_NO_AUTO_INSTALL=1` turns that off), so a double-click on `critique-bot.exe` installs,
asks for the config file, and explains how to start crit in a project folder.

## Finding the selectors once

On one PC, without a config file:

1. `crit config --url https://ai.example.com/`
2. `crit setup`: a local page opens a browser window; click the message box, the send
   button, the stop button, and a reply. The live test sends one message.
3. Copy `url`, `selectors`, and `allowed_hosts` from that user config
   (`crit config --path`) into the config file.

## Updates

crit reads the file on every start, so an edit applies from the next start. After each good
read it keeps a copy next to the user config (`config-file.cache.json`, owner-only, with the
source path and time). When the file cannot be read (the share is offline), crit uses that copy
and says once: `<path> cannot be read; using its copy from <date>.` With neither the file nor
a copy, crit stops and says so. A file that is there but broken (bad JSON, missing keys) is
never replaced by the copy: crit stops and names the problem.
