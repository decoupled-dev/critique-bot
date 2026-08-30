# config.json reference

Copy [`config.example.json`](../config.example.json) to `config.json` next to the binary (CI default: `/opt/critique-bot/config.json`). ChatGPT starters: [`config.chatgpt.example.json`](../config.chatgpt.example.json).

`--config` is required on every command. The file is JSON. Unknown keys are ignored. Env vars override matching fields when set.

GitLab runner layout and the worker/submit split: [`gitlab-ci.md`](gitlab-ci.md).

## Minimal file that will start

Required: a real `url` (not the `YOUR_CHAT_UI` placeholder) and two selectors.

```json
{
  "url": "https://chatgpt.com/",
  "selectors": {
    "prompt_input": "#prompt-textarea, [data-testid='prompt-textarea']",
    "assistant_messages": "[data-message-author-role='assistant'] .markdown"
  }
}
```

Everything else has a default. For CI, also set `queue_dir` and `user_data_dir` to **absolute** paths so the worker’s working directory cannot shift the Edge profile.

## How to fill selectors

The bot drives a **web chat UI in Edge**. It does not call an LLM API. Selectors are CSS (Playwright locators) for **this** site; they break when the site redesigns.

1. Open the chat UI in Edge.
2. Run `playwright codegen --channel msedge https://YOUR_CHAT_UI/` and click the prompt box, send, model picker, and an assistant reply.
3. Copy stable attributes (`data-testid`, `id`, `role`) rather than generated class hashes.
4. Verify with `--headed --mode general --prompt "hello"` before starting the worker.

Comma-separated lists are OR: the first matching node is used.

---

## `url` (required)

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_CHAT_URL` |
| Default | none |

Full URL of the chat page the bot should open. Must not contain `YOUR_CHAT_URL` / `YOUR_CHAT_UI`. Example: `https://chatgpt.com/`.

The worker navigates here at session start. Login/SSO pages are detected and logged; the bot cannot complete a login by itself — use `worker --headed` once.

---

## `selectors` (object, required)

| Key | Required | Meaning |
| --- | --- | --- |
| `prompt_input` | **yes** | Composer: `<textarea>`, contenteditable, or equivalent |
| `assistant_messages` | **yes** | Nodes whose **visible text** is the assistant reply. The bot waits until the last match stops growing (`idle_ms`) |
| `send_button` | no | Send control. Empty → press Enter in the prompt |
| `model_dropdown_identifier` | no | Pin the model **opener** (see below). Also accepted at the **top level** of the JSON |
| `model_dropdown` | no | CSS for the same opener, or a native `<select>` |
| `model_option` | no | CSS for items **inside** the open model panel |

### `prompt_input`

Must be visible after navigation or the run fails (`prompt input after navigation`). Prefer test ids over `textarea` alone if the page has several.

ChatGPT example:

```text
#prompt-textarea, [data-testid='prompt-textarea'], #mobile-composer-prompt, textarea[data-mobile-composer-prompt]
```

### `send_button`

If set, the bot clicks it; on failure it falls back to Enter. If omitted, Enter only.

ChatGPT example:

```text
button[data-testid='send-button'], button[data-composer-submit], button.wm-composer-submitButton
```

### `assistant_messages`

Must match **each** assistant bubble (or the markdown inside it), not the whole transcript. Counting these nodes is how the bot knows a new reply started.

ChatGPT example:

```text
[data-message-author-role='assistant'] .markdown, [data-assistant-markdown], [data-message-role='assistant']
```

If this selector is too broad, the bot may think the reply finished too soon or grab the wrong text. If it is too narrow, it waits until `timeout_ms` and then fails.

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
| Default | `""` (skip picker) |

Visible dropdown label. Empty is valid.

---

## Timeouts

### `timeout_ms`

| | |
| --- | --- |
| Type | integer > 0 |
| Default | `180000` (3 minutes) |

Budget for: page load, finding the prompt, selecting the model, sending, and waiting for the reply to **start and finish**. Slow models or large patches need more. CI job timeout is 1 hour; submit wait default is 1800s — keep `timeout_ms` below that.

### `idle_ms`

| | |
| --- | --- |
| Type | integer > 0 |
| Default | `4000` |

After the assistant text stops changing for this long, the reply is treated as complete. Too low: truncated reviews. Too high: extra wait on every job. ChatGPT example uses `6000`.

---

## Input caps

These protect the chat UI from huge patches. Values above the absolute max are clamped (with a warning).

| Key | Default | Absolute max | Meaning |
| --- | --- | --- | --- |
| `max_prompt_chars` | `120000` | `400000` | Entire prompt sent to the UI (template + patch) |
| `max_file_chars` | `32000` | `200000` | Per attached file, after read |
| `max_files` | `80` | `400` | How many files are included |
| `max_read_bytes` | `16000000` | `64000000` | Bytes read from each path before decode |

Oversized / binary files are truncated or omitted and a short note is added to the prompt. Raise these only if the chat UI can actually accept that much paste.

---

## Browser session

### `user_data_dir`

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_USER_DATA_DIR` |
| Default | `.edge-profile` |

Persistent Edge profile so cookies survive restarts.

| Value | Result |
| --- | --- |
| Relative path (default `.edge-profile`) | Resolved against the **process cwd**, not the config file. Worker systemd `WorkingDirectory=/opt/critique-bot` → `/opt/critique-bot/.edge-profile` |
| Absolute path | Used as-is. Prefer this on CI |
| `system` or `default` | Not the daily desktop profile (Chromium 136+ blocks remote debugging there). Uses a sibling dir: Linux `~/.config/microsoft-edge-critique-bot`, Windows `%LOCALAPPDATA%\Microsoft\Edge\User Data-critique-bot` |

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

Optional Playwright `storage_state` JSON (cookies) seeded on first launch of the **Playwright** persistent profile. File must exist if this is set. Ignored when using the dedicated desktop (`system`) profile. Prefer logging in once with `--headed` over shipping cookie files.

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

Inbox files are named `{timestamp-ms}-{label}-{serial}.json`. `label` is `group-app-mr42` (GitLab), `acme-app-pr8` (GitHub), a CI job id, or `local`. The worker claims the **oldest** inbox files first. Up to `max_parallel_tabs` reviews run at once (default **1**). Each `submit` waits only for the job id it just created; it does not look up reviews by MR.

CI recommendation: `"queue_dir": "/opt/critique-bot/.critique-queue"` (Windows: `C:\\critique-bot\\.critique-queue`).

### `min_interval_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `30` |

Minimum pause between **starting** sends. With one tab this is the gap after a review finishes before the next starts (if the review was shorter than this). With several tabs, starts are staggered so N chats are not opened in the same second. `0` disables spacing.

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

How many Edge **tabs** (reviews) the worker may run at once in the **same** signed-in Edge. `1` is sequential (safest for ChatGPT rate limits). `3` means up to three MRs in flight; new sends still wait `min_interval_seconds`. Values above 8 are clamped to 8.

If remote debugging cannot be opened, the worker logs a warning and falls back to one tab.

CI recommendation: start at `1`, raise to `2` or `3` only after you confirm the chat UI does not block parallel sessions.

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

`--model` and `--cdp-url` override env and file. GitLab comment posting uses `CRITIQUE_GITLAB_TOKEN` (not this file); see [`gitlab-ci.md`](gitlab-ci.md).

---

## Suggested CI `config.json`

Adjust `url` and `selectors` to your chat UI. Keep paths absolute.

```json
{
  "url": "https://chatgpt.com/",
  "selectors": {
    "model_dropdown": "",
    "model_dropdown_identifier": "model-switcher",
    "model_option": "[role='menuitemradio'], [role='menuitem'], [role='option']",
    "prompt_input": "#prompt-textarea, [data-testid='prompt-textarea'], #mobile-composer-prompt, textarea[data-mobile-composer-prompt]",
    "send_button": "button[data-testid='send-button'], button[data-composer-submit], button.wm-composer-submitButton",
    "assistant_messages": "[data-message-author-role='assistant'] .markdown, [data-assistant-markdown], [data-message-role='assistant']"
  },
  "model": "",
  "timeout_ms": 180000,
  "idle_ms": 6000,
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
  "max_parallel_tabs": 1
}
```

Do not commit this file if it contains a private chat URL or a `storage_state` path.

## Validation the loader enforces

- File exists and is a JSON object.
- `selectors.prompt_input` and `selectors.assistant_messages` are non-empty.
- `url` is non-empty and not a placeholder.
- `model`, if set, is not `YOUR_MODEL_NAME`.
- If `storage_state` is set, that path is a file.
- Integer/float fields: `timeout_ms`, `idle_ms`, and the `max_*` keys must be > 0; interval fields must be ≥ 0.
