# config.json reference

You rarely edit this file by hand. `crit` asks for what it needs on the first run, and
`crit config` changes any setting from the command line (below). The starters remain for
reference and for CI images:

| Starter | Use |
| --- | --- |
| [`config.example.json`](../config.example.json) | Generic web page |
| [`config.local.example.json`](../config.local.example.json) | A model behind an OpenAI-compatible server (llama.cpp, LM Studio, vLLM) |

The file is JSON. Unknown keys are ignored. Env vars override matching fields when set.

## Where the file is

Every command uses the first of these that applies:

1. `--config FILE` on the command line;
2. the `CRITIQUE_CONFIG` environment variable;
3. the config recorded for the project in `.bot/settings.json` (`"config"`), if the file exists;
4. a `config.json` in the project root, if it exists and git does not track it;
5. your user config: `config.json` in crit's folder, `~/.crit/config.json`
   (`%USERPROFILE%\.crit\config.json` on Windows; `CRIT_HOME` moves the folder). Before 1.1
   it was `~/.config/critique-bot/config.json` (`%APPDATA%\critique-bot\config.json`); crit
   moves it once.

crit writes the user config owner-only (0600 in the 0700 `.crit` folder on Linux and macOS;
an owner-only ACL on Windows), replacing it in one step so it is never half-written.

When no file exists, a command in a terminal asks the first-run questions (local model or Web
UI; the server or the url, the model, the page elements; names to hide; see
[install.md](install.md#set-up)) and writes the user config. Without a terminal (CI, the
worker, `--acp`) it exits with a sentence naming `crit config --url ... --selector
prompt_input=... --selector assistant_messages=...` or `crit config --backend local
--local-url http://IP:PORT`. `crit configure` asks the questions again.

## The config file (`config_file`)

A team can hand out one shared file with `url`, `selectors`, and the rest; see
[config-file.md](config-file.md). Your user config then only names it:

```json
{ "config_file": "\\\\server\\share\\crit.json" }
```

crit finds it from `config_file`, else `CRITIQUE_CONFIG_FILE`, else `/etc/critique-bot/crit.json`
(`%ProgramData%\critique-bot\crit.json` on Windows). Its keys sit between crit's defaults and
your own settings: `url`, `backend`, `local`, `cdp_url`, `allowed_hosts`, `redact_pii`,
`gitlab.base_url`, and each selector it sets are locked; `egress` and `page_network` can only
be tightened; `redact_terms` add up; every other key is a default you may override. It must
set `url` (and, for `backend: browser`, `selectors.prompt_input` and
`selectors.assistant_messages`). A copy of the last good read is kept in
`cache/config-file.cache.json` in crit's folder and used when the file cannot be read. A
`user_data_dir` in it is ignored (the browser profile is personal).

The user config also keeps per-user preferences: `theme`, `syntax_preview`, and `welcomed`
(the first-run welcome was shown). They apply in every project; a project keeps its own
`theme` in `.bot/settings.json` only if one was set there.

## `crit config`

`crit config` (also `critique-bot config`) with no flags prints the settings in plain words:
the file and why it was chosen, backend, url, network mode, allowed sites, hidden
names, personal-data redaction, GitLab address, model. Tokens are never printed (they are
not in the file: `CRITIQUE_GITLAB_TOKEN` is an environment variable).

| Flag | Sets |
| --- | --- |
| `--config-file PATH` | `config_file` (checked first; `none` removes it) |
| `--url URL` | `url` (https only; plain http only for localhost), `backend: browser`; starter `selectors` when none are set (run `crit setup` next) |
| `--backend browser\|local` | `backend` (local defaults `url` to `http://127.0.0.1:8080`) |
| `--local-url ADDR` | the model server's `url` (`ip:port` or `http(s)://host:port`), `backend: local`; a server on another PC is added to `allowed_hosts` |
| `--model NAME` | `model` (`""` for the default) |
| `--allow-host H` / `--remove-host H` | `allowed_hosts` (repeatable; a URL is reduced to its host) |
| `--redact NAMES` / `--unredact NAME` | `redact_terms` (repeatable or comma-separated; at least 3 letters or digits) |
| `--pii on\|off` | `redact_pii` |
| `--egress strict\|open` | `egress` |
| `--page-network open\|strict` | `page_network` |
| `--theme ID` | the terminal style, in the user config (every project) |
| `--gitlab-url URL` | `gitlab.base_url` (https only; `""` removes it) |
| `--selector KEY=CSS` | one of `selectors` (repeatable); `KEY=` clears an optional one |
| `--check` | checks that the local model server answers (and which models it lists), or that the Web UI's site resolves and answers |
| `--reset` | removes the file (asks first in a terminal; needs `--yes` otherwise) |
| `--path` | prints the file's path |
| `--config FILE` | changes that file instead |

Every value is checked before anything is written, and a change that would stop a working
file from loading is refused with the reason (exit code 2). A file that is not complete yet
(for example only `--redact` so far) is saved with a "Not ready yet" note.

```bash
crit config --url https://ai.company.com/ --redact "Acme, Globex"
crit config --url https://ai.company.com/ --allow-host static.company-cdn.net
crit config --pii on --egress strict --gitlab-url https://gitlab.company.com
```

### Asked later, when needed

In an interactive terminal session (never in CI, the worker, `--acp`, or piped runs; `--yes`
does not answer these):

- The page tried to reach a site that is not allowed: it is blocked, and after the
  current step crit asks once per site "Allow it from now on? [y/N]". Yes adds it to
  `allowed_hosts`, effective from the next start.
- A command needs `gitlab.base_url` and none is set: crit asks for the https address and saves it.

GitLab runner layout and the worker/submit split: [`gitlab-ci.md`](gitlab-ci.md).

## `backend`

Optional. Which model crit talks to; everything above this layer (the agent, the review prompts, the queue) is the same for both.

| Value | Meaning |
| --- | --- |
| `browser` (default; also `web`, `playwright`, `ui`, `chat`) | The page at `url`, driven in Edge. Needs `selectors` and a signed-in profile. |
| `local` (also `llama.cpp`, `llama-server`, `lmstudio`, `vllm`, `openai-compatible`) | A model behind an OpenAI-compatible server, such as Qwen or Gemma in llama.cpp's `llama-server`. Needs `model`; `selectors` are ignored. See [Local model](#local-model-backend-local). |

Hosted APIs (`openai`, `anthropic`, ...) are not a backend: the only data that leaves the PC is the conversation with the configured url ([security.md](security.md)).

## Local model (`backend: local`)

The model runs behind an OpenAI-compatible server (llama.cpp's `llama-server`, LM Studio, vLLM) and crit calls its HTTP API directly, so there is no browser, no sign-in, and no selectors. Nothing leaves the PC: the server must be on `localhost` / `127.0.0.1` (a model on another machine in the LAN is allowed only when its host is listed in `allowed_hosts`), and the request never goes through an `http_proxy`. Ollama is not supported.

`crit configure` (or the first `crit` with no config) asks for these and writes the file. In one line: `crit config --backend local --local-url http://127.0.0.1:8080 --model NAME`. By hand:

```bash
llama-server -m models/qwen2.5-coder-14b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8080 -c 32768
```

```json
{
  "backend": "local",
  "url": "http://127.0.0.1:8080",
  "model": "qwen2.5-coder-14b",
  "local": { "num_ctx": 32768 }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | `http://127.0.0.1:8080` | Base URL of the server (`llama-server`'s default port; LM Studio uses `1234`). A path such as `/v1` may be included. |
| `model` | first model the server lists | The model name as the server lists it at `/v1/models` (llama-server: the file name, or `--alias`). `--model` and `CRITIQUE_MODEL` override it, as for the browser. |
| `local.api` | `openai` | The only API crit speaks: `/v1/chat/completions` and `/v1/models`. |
| `local.num_ctx` | 32768 | The context window the server was started with (`llama-server -c`). crit sizes its conversations to it; it is not sent to the server. |
| `local.temperature` | server default | Sampling temperature. 0 to 0.3 suits code. |

Replies are streamed, so the [waiting settings](#waiting-for-a-reply) apply the same way: `reply_start_ms` (at least 120 s here, since a large model may take that long to load), `reply_stall_ms` between two chunks, and `reply_max_ms` for the whole reply. A model that stops at its output limit is logged as cut off.

The agent's conversation-size defaults are scaled to the context window: a new conversation starts at about `num_ctx × 2` characters instead of 120,000, and tool results are capped at `num_ctx / 2` characters. `compact_after_chars` and `max_result_chars` in `.bot/settings.json` still win when set. Reasoning models (`qwen3`, `deepseek-r1`) work; their `<think>` blocks are removed from the reply before it is read.

### Model on another PC

1. On the PC with the model, start the server on the network: `llama-server ... --host 0.0.0.0 --port 8080`. Allow that port in its firewall only from the developers' PCs.
2. In config.json, set `url` to that PC's address and list its host in `allowed_hosts`, which is how crit is allowed to send code off this PC:

```json
{
  "backend": "local",
  "url": "http://192.168.1.50:8080",
  "model": "qwen2.5-coder-14b",
  "allowed_hosts": ["192.168.1.50"],
  "local": { "num_ctx": 32768 }
}
```

The server's http is not encrypted, and anyone who can reach the port can use the model. An SSH tunnel fixes both without opening the port: start the server with `--host 127.0.0.1` on its PC, run `ssh -N -L 8080:127.0.0.1:8080 you@gpu-box` on yours, and keep `url` at `http://127.0.0.1:8080` with no `allowed_hosts`.

A quick check that the server answers, before using the agent:

```bash
crit ask --config config.local.json --prompt "Reply with exactly one word: PONG."
```

## Minimal file that will start

A real `url` (not the `YOUR_CHAT_UI` placeholder) and two selectors:

```json
{
  "url": "https://ai.company.com/",
  "selectors": {
    "prompt_input": "textarea, [contenteditable='true'][role='textbox']",
    "assistant_messages": "[data-message-author-role='assistant'], .assistant-message"
  }
}
```

Everything else has a default. For CI, set `queue_dir` and `user_data_dir` to **absolute** paths.

## How to fill selectors

Selectors are CSS (Playwright locators) for **this** site; they break when the site redesigns.

The easy way is to let the bot write them:

```bash
critique-bot setup --config config.json
```

That opens a local page, launches Edge on the url, and lets you **click** the prompt box, the send button, a reply, and the stop button. It ranks the candidate selectors (preferring stable `data-*` and `aria-label` attributes over generated class hashes), writes them to `config.json`, and runs a live test.

By hand instead:

1. Open the page in Edge.
2. Run `playwright codegen --channel msedge https://YOUR_CHAT_UI/` and click the prompt box, send, model picker, and an assistant reply.
3. Copy stable attributes (`data-testid`, `id`, `role`) rather than generated class hashes.
4. Verify with a one-shot prompt before starting the worker:

   `critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG."`

Comma-separated lists are OR: the first matching node is used.

---

## `url` (required for `browser`; the server address for `local`)

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_CHAT_URL` |
| Default | none |

Full URL of the page the bot should open. Must not contain `YOUR_CHAT_URL` / `YOUR_CHAT_UI`. Example: `https://ai.company.com/`.

The page runs as it does in a normal browser: crit does not filter its requests, so a page that calls other hosts behind the scenes keeps working. crit controls what leaves on its own side, by what it types into the page (see [security.md](security.md)).

`"filter_chat_page": true` turns on a page filter: Edge may then reach only the url's own site (for `ai.corp.example`, `corp.example` and all its subdomains; on shared hosting such as `*.azurewebsites.net`, the exact host) plus the hosts in `allowed_hosts`. Every other request, page loads in any frame included, is aborted, and crit prints the blocked host names. No site has hosts built in: list the site's CDN and sign-in hosts in `allowed_hosts`. Default `false`.

The worker navigates here at session start. Login/SSO pages are detected and logged; the bot cannot complete a login by itself. A missing profile opens a visible window on that first run. After you sign in, that window closes and the same run continues headless. Pass `worker --headed` to show the window again later.

---

## `allowed_hosts`

| | |
| --- | --- |
| Type | list of host names |
| Default | `[]` |

The browser may reach only the url's host and its subdomains. Everything else is blocked: page loads in any frame, scripts, API calls, WebSockets, and popups. An internal AI site often needs neighbouring hosts, such as its API, its static files, or the company sign-in. List them here; each also covers its subdomains:

```json
"url": "https://ai.company.net/",
"allowed_hosts": ["api.company.net", "static.company.net", "login.microsoftonline.com"]
```

When crit blocks a host, it prints the host and this setting. Add only hosts the page needs. See [security.md](security.md).

## `page_network`

| | |
| --- | --- |
| Type | `"open"` or `"strict"` |
| Default | `"open"` |

How the browser treats the page itself under `"egress": "strict"`. crit's own traffic stays
strict either way: it types only into the page at `url`, the agent's commands run offline,
`web_fetch` is off, network commands are blocked, `cdp_url` is refused, and GitLab is reached
only at `gitlab.base_url`.

- `"open"` (the default): the page loads what it needs, as for a person: its CDN, sign-in, and
  file hosts resolve through the system proxy, service workers work, and there is no page
  filter. The browser's own background services stay off (switches, profile preferences, and
  the service-host denylist).
- `"strict"`: the page too reaches only the url's host and `allowed_hosts` (resolver rules
  that make every other name unresolvable, no proxy unless the PC has one, the page filter, no
  service workers). A blocked host is named, and crit offers to allow it.

`crit config --page-network strict` sets it. A config file's `"strict"` cannot be loosened.

## `redact_terms` and `redact_pii`

| | |
| --- | --- |
| Type | list of names / true or false |
| Default | `[]` / `true` |

`redact_terms` lists company, customer, or project names that must not reach the service. Each is sent as `[REDACTED:name]`, in any case, with spaces, hyphens, underscores, and dots between its words interchangeable or left out (`"Acme Corp"` also hides `acme-corp`, `ACME_CORP`, `AcmeCorp`, `acme.corp`), and inside identifiers at a camelCase, `_`, `.`, or `-` step (`AcmeClient`, `com.acme.app`, `ACME_API_URL`), but not inside a longer word (`Acmeville`). Each name needs at least 3 letters or digits and at most 200 characters; duplicates are dropped. `CRIT_REDACT_TERMS` (comma-separated) adds more, for CI. The setup scripts (`--redact-terms` / `-RedactTerms`), the first-run questions, and `critique-bot setup` fill this list.

```json
"redact_terms": ["Acme Corp", "Globex"],
"redact_pii": true
```

`redact_pii` (default `true`) also hides personal data: emails, phone numbers, payment cards, IBANs, US SSNs, Aadhaar and PAN numbers, and UK National Insurance numbers. Set `false` to send them as they are. See [security.md](security.md#what-goes-to-the-service).

## `selectors` (object, required for `browser`)

In plain words, as the first-run questions name them:

| Question | Key | What it points at |
| --- | --- | --- |
| Message box | `prompt_input` | the box you type a message into |
| Send button | `send_button` | the button that sends it (empty: crit presses Enter) |
| Stop button | `stop_button` | the button that stops a reply while it is being written; crit uses it to tell a finished reply from a pause |
| Replies | `assistant_messages` | each reply from the model; crit reads the last one |
| Model picker | `model_dropdown` | the control that opens the list of models |
| Model picker identifier | `model_dropdown_identifier` | text, a label, or an id that names that control exactly, so no other button is clicked |
| Model option | `model_option` | one entry in the open list of models |

The first run offers starter guesses for the first four; `crit setup` checks them against the
real page (and lets you click the right element instead). The details:

| Key | Required | Meaning |
| --- | --- | --- |
| `prompt_input` | **yes** | Composer: `<textarea>`, contenteditable, or equivalent |
| `assistant_messages` | **yes** | Nodes whose **visible text** is the assistant reply. The bot reads the last match as it grows |
| `send_button` | no | Send control. Empty → press Enter in the prompt |
| `stop_button` | no | The "stop generating" control. **Strongly recommended:** it is how the bot knows a reply actually finished, and how `crit` stops a reply |
| `continue_button` | no | A "Continue generating" control. Clicked once when it shows after a reply ends |
| `model_dropdown_identifier` | no | Pin the model **opener** (see below). Also accepted at the **top level** of the JSON |
| `model_dropdown` | no | CSS for the same opener, or a native `<select>` |
| `model_option` | no | CSS for items **inside** the open model panel |

### `prompt_input`

Must be visible after navigation or the run fails (`prompt input after navigation`). Prefer test ids over `textarea` alone if the page has several.

Example:

```text
textarea, [contenteditable='true'][role='textbox']
```

### `send_button`

If set, the bot clicks it; on failure it falls back to Enter. If omitted, Enter only.

Example:

```text
button[type='submit'], button[aria-label='Send' i]
```

### `assistant_messages`

Must match **each** assistant bubble (or the markdown inside it), not the whole transcript. Counting these nodes is how the bot knows a new reply started.

Example:

```text
[data-message-author-role='assistant'], .assistant-message
```

If this selector is too broad, the bot may think the reply finished too soon or grab the wrong text. If it is too narrow, it waits until `timeout_ms` and then fails.

### `stop_button`

Optional but the most important selector for review quality. While this control is visible the assistant is still writing, so the bot keeps waiting no matter how long the model pauses. When it disappears, the reply is finished and the bot returns it after a short settle.

Example:

```text
button[aria-label*='Stop' i]
```

Leave it empty and the bot falls back to `idle_ms`: it assumes a reply that stopped changing for that long is done. A model that pauses longer than `idle_ms` mid-answer — extended thinking, a tool call, rate limiting — then yields a **silently truncated** review. When that happens the run logs a warning and `review.json` records `completion.complete: false`, so you can tell truncated output apart from finished output.

Even with `stop_button` empty, the bot also treats a visible `aria-busy="true"` on a reply bubble as "still generating", and tries a few common stop-button selectors.

The `crit` agent uses the same signal two more ways. Before each send it checks whether the page is still writing an earlier reply; it waits up to about 20 seconds for that reply to finish, then clicks stop, so two replies never mix. Ctrl+C on a task, and a retry after a timed-out reply, click stop too. A wrong `stop_button` makes both slower and less reliable.

### `continue_button`

Optional. Some pages cut a long reply and show a "Continue generating" button. When this selector is set and the button is visible after a reply ends, the bot clicks it once, waits for the rest, and merges it into the same reply. When the page does not start writing again, the cut reply is kept. Empty (the default) never clicks. `critique-bot setup` does not pick this one; write it by hand.

### Model picker (`model` + dropdown fields)

Used only when `model` is non-empty. The value of `model` must be the **visible label** in the UI (for example `GPT-5.1`), not an API id.

**Opener** (click to open the panel):

1. `model_dropdown_identifier` — preferred. Unique text, `aria-label`, `title`, `id`, `data-testid`, `data-id`, or class substring on the opener. If the string looks like a CSS/Playwright locator (starts with `.` `#` `[` `/`, or `xpath=` / `text=`), it is used as a locator first; otherwise the bot searches accessible name, label, then the DOM (including open shadow roots).
2. `model_dropdown` — CSS for that opener. If the node is a `<select>`, the bot uses `select_option` and does not need a panel.
3. If both identifier and dropdown are empty, the bot **guesses** (risky: it may click the wrong control).

If `model_dropdown_identifier` is set and does **not** match, the bot **will not** click other buttons. That is intentional so a bad identifier cannot smash a random toolbar item.

**Option inside the open panel:**

- `model_option` — optional CSS such as `[role='menuitemradio'], [role='menuitem'], [role='option']`. If empty, the bot searches the panel for the `model` label (including shadow DOM).

Leave `model` as `""` to skip picking (uses whatever the signed-in session already has selected).

Placeholder `YOUR_MODEL_NAME` in `model` is rejected.

---

## `model`

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_MODEL` |
| CLI | `--model` (wins over env and file) |
| Default | `""` (browser: skip picker; local: the first model the server lists) |

**browser:** visible dropdown label. Empty is valid (uses whatever the signed-in session already has selected). Placeholder `YOUR_MODEL_NAME` is rejected.

**local:** the model name the server lists at `/v1/models`, for example `qwen2.5-coder-14b` or `gemma3-1b`.

---

## Timeouts

### `timeout_ms`

| | |
| --- | --- |
| Type | integer > 0 |
| Default | `180000` (3 minutes) |

Budget for page load, finding the prompt, selecting the model, and sending. Waiting for the reply is not cut off by it while the reply makes progress; see the four settings below.

### Waiting for a reply

A reply is waited for as long as it shows signs of life, not for a fixed time. Each case that ends the wait raises an error, and the agent retries it: first in the same conversation, then in a new conversation that carries a summary of the task (`reply_retries` in `.bot/settings.json`).

| Key | Default | What it does |
| --- | --- | --- |
| `reply_start_ms` | `60000` | After a send, no new reply and no generating signal for this long. If the prompt is still in the input box, the send did not go out, so it is sent once more and gets the same time again. If the box is empty, the page is not answering. |
| `reply_stall_ms` | `90000` | A reply started, then no text and no generating signal for this long: it stalled. |
| `thinking_max_ms` | `600000` | The page shows "generating" with no text yet (a thinking model) for at most this long. A long think is allowed; a stuck one is not. |
| `reply_max_ms` | `1200000` | Upper limit for one reply that keeps making progress. The larger of this and `timeout_ms` applies. |

### `idle_ms`

| | |
| --- | --- |
| Type | integer > 0 |
| Default | `4000` |

Fallback completion signal, used only when no generating indicator is visible (see `selectors.stop_button`). After the assistant text stops changing for this long *and* nothing says generation is in progress, the reply is treated as complete. Too low: truncated reviews. Too high: extra wait on every job. The first-run setup writes `6000`.

When `stop_button` is configured, `idle_ms` no longer decides completion — it only sizes the settle window after generation stops.

### `job_timeout_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `0` (auto: `timeout_ms × 2 + 60s`) |

Wall-clock ceiling for one queued job. If a job overruns, the worker marks it failed so a waiting `submit` (and the CI job behind it) stops blocking. A wedged Playwright call cannot be interrupted, so this bounds the *waiting*, not the work; the browser-restart path clears the session afterwards.

When files are staged across turns, the auto ceiling becomes `(staged files + 2) × timeout_ms + 60s` so instruction + ACK + patch turns are not killed at the single-paste 420s default.

---

## Input caps

These protect the page from huge patches. Values above the absolute max are clamped (with a warning).

| Key | Default | Absolute max | Meaning |
| --- | --- | --- | --- |
| `max_prompt_chars` | `120000` | `400000` | Cap for **one** paste. If template + HEAD files + patch would exceed this, the worker sends instructions, then files, then the patch as separate turns |
| `max_file_chars` | `32000` | `200000` | Per attached file, after read |
| `max_files` | `80` | `400` | How many changed-file bodies are read from the checkout and sent (inlined or one per turn, after instructions and before the patch) |
| `max_read_bytes` | `16000000` | `64000000` | Bytes read from each path before decode |

Oversized / binary files are truncated or omitted and a short note is added to the prompt. Raise these only if the page can actually accept that much paste.

When changed-file bodies plus the patch fit in `max_prompt_chars`, the worker (or local one-shot) still sends **one** paste. When they would not, the same Edge tab gets a short prime message, then each file one per turn (`ACK` only), then the review template and patch. Each file is pasted as soon as the previous ACK reply is complete. Markdown/RST bodies are skipped (their diffs are already in the patch). Original file bodies are always sent when they exist in the checkout. If a file turn gets no assistant reply, remaining file sends are dropped and the review prompt is still sent. Intermediate replies are discarded; only the last reply is `review.md`.

### `turn_pause_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `0` |

Pause after an ACK before the next paste in the same job. `0` (default) sends the next file as soon as the model acknowledges. This is not `min_interval_seconds` (that spaces **jobs**).

---

## Browser session

### `user_data_dir`

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_USER_DATA_DIR` |
| Default | `browser-profile` in crit's folder (`~/.crit/browser-profile`, `%USERPROFILE%\.crit\browser-profile`) |

Persistent Edge profile so cookies survive restarts. It is never asked: it is personal and
automatic. It comes only from your own config, `CRITIQUE_USER_DATA_DIR`, or the default; a
`user_data_dir` in a shared config file (`config_file`, `CRITIQUE_CONFIG_FILE`, the system
location) is ignored with a log line, and never resolved next to that file.

| Value | Result |
| --- | --- |
| Not set, or the old default `.edge-profile` | `~/.crit/browser-profile` (moved there once from `~/.config/critique-bot/.edge-profile` or `%APPDATA%\critique-bot\.edge-profile`) |
| Other relative path | Next to the config file that sets it, never the folder crit runs in |
| Absolute path | Used as-is. Prefer this on CI |
| `system` or `default` | Not the daily desktop profile (Chromium 136+ blocks remote debugging there and returns HTTP 403). Uses a dedicated dir **outside** Edge's default User Data path: `~/.crit/msedge-user-data` |

Do not share this directory with a human’s everyday Edge. Do not copy it between machines. Two processes must not use it at once (the worker already serializes jobs).

### `cdp_url`

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_CDP_URL` |
| CLI | `--cdp-url` |
| Default | `""` (launch Edge) |

If set (e.g. `http://127.0.0.1:9222`), the bot **attaches** to an Edge you started with `--remote-debugging-port=9222` **and** a non-default `--user-data-dir`. Empty string is ignored. Leave empty for the normal worker.

### `storage_state`

| | |
| --- | --- |
| Type | string (path) |
| Env | `CRITIQUE_STORAGE_STATE` |
| Default | `""` |

Optional Playwright `storage_state` JSON (cookies) seeded on first launch of the **Playwright** persistent profile. File must exist if this is set. Ignored when using the dedicated desktop (`system`) profile. A missing profile opens a visible Edge window for sign-in unless this file is set. Prefer that window over shipping cookie files.

---

## Queue (worker + `submit`)

Relative `queue_dir` is resolved **next to `config.json`**, unlike `user_data_dir`.

### `queue_dir`

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_QUEUE_DIR` |
| Default | `.critique-queue` beside the config file |

Worker and every GitLab job **must** share this directory. Layout: `inbox/`, `processing/`, `results/<job-id>/`, `worker.lock`, `worker.heartbeat`.

Inbox files are named `{timestamp-ms}-{label}-{serial}.json`. `label` is `group-app-mr42` (GitLab), a CI job id, or `local`. The worker claims the **oldest** inbox files first. Up to `max_parallel_tabs` reviews run at once (default **1**). Each `submit` waits only for the job id it just created; it does not look up reviews by MR.

CI recommendation: `"queue_dir": "/opt/critique-bot/.critique-queue"` (Windows: `C:\\critique-bot\\.critique-queue`).

### `min_interval_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `30` |

Minimum pause between **starting** sends. With one tab this is the gap after a review finishes before the next starts (if the review was shorter than this). With several tabs, starts are staggered so N conversations are not opened in the same second. `0` disables spacing.

### `interval_jitter_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `5` |

Extra random delay in `[0, jitter]` added to the interval, so traffic is less metronomic.

### `max_parallel_tabs`

| | |
| --- | --- |
| Type | integer 1–8 |
| Env | `CRITIQUE_MAX_PARALLEL_TABS` |
| Default | `1` |

How many reviews the worker may run at once. Edge tabs in the same signed-in instance (`1` is safest for the service's rate limits). `3` means up to three MRs in flight; new sends still wait `min_interval_seconds`. Values above 8 are clamped to 8.

If Edge cannot open remote debugging, the worker logs a warning and falls back to one tab.

CI recommendation: start at `1`.

### `max_attempts`

| | |
| --- | --- |
| Type | integer 1–20 |
| Default | `3` |

How many times one job may be handed back to the inbox after a recoverable browser error (a closed tab, a crashed Edge) before it is failed for good. Without a ceiling, a browser that is permanently broken — an expired login, say — would requeue the same job forever while the heartbeat stayed healthy, so every `submit` would burn its full `--wait-timeout`.

A job that runs out of attempts gets a `status.json` with `"gave up after N attempt(s)"`, which is what `queue-status` and `submit` report.

### `result_retention`

| | |
| --- | --- |
| Type | integer > 0 |
| Default | `200` |

How many finished job folders to keep under `results/`. The oldest are removed at worker start and after each job. Each folder holds `job.json`, `status.json`, the review, and any failure screenshot, so an unbounded queue directory fills the disk on a busy runner.

---

## `gitlab` (object, optional)

Used when reviewing a merge request: the bot loads title, ticket IDs, description, commit messages, and changed file/line ranges into the SYSTEM prompt, and `gitlab-post` uses the same instance URL.

| Key | Meaning |
| --- | --- |
| `base_url` | GitLab origin only, e.g. `https://gitlab.example.com`. API root is `{base_url}/api/v4`. Env: `CRITIQUE_GITLAB_URL`. In CI, `CI_SERVER_URL` / `CI_API_V4_URL` also work |

Do **not** put `project_id`, `mr_iid`, or `mr_url` here. Those change per pipeline and are read from GitLab CI (`CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_MERGE_REQUEST_URL`) or from `gitlab-post --project-id` / `--mr-iid` / `--mr-url`.

The merge-request endpoint is:

`{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}`

Posting still needs `CRITIQUE_GITLAB_TOKEN` (not this file); see [`gitlab-ci.md`](gitlab-ci.md).

---

## Environment override summary

| Env | Config key |
| --- | --- |
| `CRITIQUE_CHAT_URL` | `url` |
| `CRITIQUE_MODEL` | `model` |
| `CRITIQUE_STORAGE_STATE` | `storage_state` |
| `CRITIQUE_USER_DATA_DIR` | `user_data_dir` |
| `CRITIQUE_CDP_URL` | `cdp_url` |
| `CRITIQUE_QUEUE_DIR` | `queue_dir` |
| `CRITIQUE_MAX_PARALLEL_TABS` | `max_parallel_tabs` |
| `CRITIQUE_GITLAB_URL` | `gitlab.base_url` |
| `CRIT_REDACT_TERMS` | added to `redact_terms` (comma-separated) |

`--model` and `--cdp-url` override env and file. GitLab comment posting uses `CRITIQUE_GITLAB_TOKEN` (not this file); see [`gitlab-ci.md`](gitlab-ci.md).

---

## Suggested CI `config.json`

Adjust `url` / `selectors` to your page. Keep paths absolute.

```json
{
  "backend": "browser",
  "url": "https://ai.company.com/",
  "selectors": {
    "model_dropdown": "",
    "model_dropdown_identifier": "",
    "model_option": "[role='menuitemradio'], [role='menuitem'], [role='option']",
    "prompt_input": "textarea, [contenteditable='true'][role='textbox']",
    "send_button": "button[type='submit'], button[aria-label='Send' i]",
    "stop_button": "button[aria-label*='Stop' i]",
    "assistant_messages": "[data-message-author-role='assistant'], .assistant-message"
  },
  "model": "",
  "timeout_ms": 180000,
  "idle_ms": 6000,
  "max_attempts": 3,
  "result_retention": 200,
  "max_prompt_chars": 120000,
  "max_file_chars": 32000,
  "max_files": 80,
  "max_read_bytes": 16000000,
  "user_data_dir": "/opt/critique-bot/.edge-profile",
  "cdp_url": "",
  "storage_state": "",
  "queue_dir": "/opt/critique-bot/.critique-queue",
  "min_interval_seconds": 30,
  "interval_jitter_seconds": 5,
  "turn_pause_seconds": 0,
  "max_parallel_tabs": 1,
  "gitlab": {
    "base_url": "https://gitlab.example.com"
  }
}
```

Do not commit this file if it contains a private url or a `storage_state` path.

## Validation the loader enforces

- File exists and is a JSON object.
- If `backend` is set, it must be `browser` (or `web` / `playwright` / `ui` / `chat`) or `local` (or `llama.cpp` / `llama-server` / `lmstudio` / `vllm` / `openai-compatible`).
- `browser`: `selectors.prompt_input` and `selectors.assistant_messages` are non-empty; `url` is non-empty and not a placeholder.
- `local`: `url` is an http(s) address on this PC (or a host listed in `allowed_hosts`), without a user name or password; `local.api` is `openai` when set; `local.num_ctx` is > 0 when set.
- If `storage_state` is set, that path is a file.
- `redact_terms` is a list of strings, each with at least 3 letters or digits and at most 200 characters; `redact_pii` is `true` or `false`.
- Integer/float fields: `timeout_ms`, `idle_ms`, `result_retention`, and the `max_*` keys must be > 0; interval fields, `turn_pause_seconds`, and `job_timeout_seconds` must be ≥ 0.

A one-shot `--mode general` prompt (or **Send test prompt** in `critique-bot setup`) is the check the loader cannot do: Edge is installed, the profile holds a session, the selectors match the live page, and a real reply comes back.
