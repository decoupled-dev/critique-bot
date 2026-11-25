# The config file (for the people who roll crit out)

This is the option for rolling out one file to many people. People who use crit should not
have to configure anything: you write one **config file** with the url, the page elements, and
the rules, and put it where crit finds it (`CRIT_CONFIG_FILE`, or the system location
below). crit then asks no first-run questions and carries on (the log file `~/.crit/logs/crit.log` notes `Using <path>`). A person
can also point at it themselves with `crit config --config-file PATH` (the first-run question
names that command; at install time, `crit install --config-file PATH` or the installers'
`CRIT_CONFIG_FILE`). Every other folder the person opens crit in asks
nothing (the role for a project is picked from its files; the person signs in once).

A starter: [`config-file.example.json`](../config-file.example.json). The .deb also installs
it at `/usr/share/crit/examples/`. Copy it, set the `url`, check the selectors once
(see [Finding the selectors once](#finding-the-selectors-once)), and share that one file:

```json
{
  "url": "https://ai.company.com/",
  "selectors": {
    "prompt_input": "textarea, div[contenteditable='true'][role='textbox']",
    "send_button": "button[type='submit'], button[aria-label*='Send' i]",
    "stop_button": "button[aria-label*='Stop' i]",
    "assistant_messages": "[data-role='assistant'], .assistant-message"
  },
  "redact_terms": ["Company Inc", "Customer X", "Project Falcon"],
  "redact_pii": true,
  "egress": "strict",
  "page_network": "open",
  "allowed_hosts": [],
  "model": "",
  "timeout_ms": 180000,
  "max_prompt_chars": 120000,
  "max_file_chars": 32000,
  "max_files": 80,
  "gitlab": {
    "base_url": ""
  }
}
```

The selectors there are starter guesses that match many pages; confirm them with `crit setup`.
Empty values (`"model": ""`, `"base_url": ""`) mean crit's default.

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

JSON has no comments, so the table above is the reference for each key.

Keys this crit version does not know are ignored (noted in the log file), so a newer file
works with an older crit.

## Locked and adjustable keys

crit layers three things: its built-in defaults, then the config file, then the person's own
settings (`crit config`). The file decides these, and `crit config` refuses to change them
("This is set in the config file <path>; change it there.", exit 2):

- `url`, `backend`, `local`, `cdp_url`, `allowed_hosts`, `redact_pii`, `gitlab.base_url`;
- each selector the file sets (one it leaves out stays the person's);
- `egress` and `page_network`: a person may tighten `"open"` to `"strict"`, never loosen;
  `CRIT_EGRESS=open` does not loosen the file's `"strict"` either.
- `redact_terms`: the person may add names, never remove the file's.

Everything else (model, timeouts, limits) is a default: the file's value applies unless the
person set their own. Environment overrides such as `CRIT_URL` do not change a
locked key.

`crit config` shows the file first and marks what comes from it:

```
Config file: /srv/team/crit.json
url: https://ai.example.com/ (config file, locked)
...
```

## Where crit looks

The first of these that is set:

1. `config_file` in the person's user config (written by `crit config --config-file PATH` or
   `crit install --config-file PATH`; `--config-file none` removes it);
2. the `CRIT_CONFIG_FILE` environment variable;
3. the system location, if the file exists: `/etc/crit/crit.json` on Linux and macOS,
   `%ProgramData%\crit\crit.json` on Windows. The 1.0.x locations, `/etc/critique-bot/crit.json`
   and `%ProgramData%\critique-bot\crit.json`, are still read when the new file is missing.

With 2 or 3 the first run asks nothing and goes on (the log file notes `Using <path>`).

Paths may be UNC (`\\server\share\crit.json`; kept as given) or `C:\...`; quotes from a
pasted "Copy as path" are removed.

Personal things never come from this file: a `user_data_dir` in it (the browser profile with
the person's sign-in) is ignored with a log line, and so is a `local.api_key` (a local model
server's API key comes only from the person's own `~/.crit/config.json` or `CRIT_API_KEY`). The profile is `~/.crit/browser-profile`
unless the person sets their own.

## Deployment

- **A file share.** Put `crit.json` on a share everyone can read, and tell people the path
  (or set it at install, below). Changes reach everyone on their next crit start.
- **Windows (Intune, GPO, SCCM).** Copy the file to `%ProgramData%\crit\crit.json`
  (readable by Users). Nobody is asked anything.
- **Linux (Ansible, Puppet, an image).** Copy it to `/etc/crit/crit.json`, mode 0644.
  The .deb creates `/etc/crit` readable by everyone (its worker `config.json` in there
  stays 0640), so if that file exists nothing else is needed.
- **At install time.** `CRIT_CONFIG_FILE=/path/crit.json` with `install.sh`, or
  `$env:CRIT_CONFIG_FILE = "\\server\share\crit.json"` before `install.ps1`, or
  `crit install --config-file PATH`: crit stores the path for that user after installing.

The downloaded program installs itself the first time it runs (no question;
`CRIT_NO_AUTO_INSTALL=1` turns that off), so a double-click on `crit.exe` installs,
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
read it keeps a copy in crit's folder (`~/.crit/cache/config-file.cache.json`, owner-only, with the
source path and time). When the file cannot be read (the share is offline), crit uses that copy
and notes it in the log file: `<path> cannot be read; using its copy from <date>.` With neither the file nor
a copy, crit stops and says so. A file that is there but broken (bad JSON, missing keys) is
never replaced by the copy: crit stops and names the problem.
