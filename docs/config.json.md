# config.json reference

Copy a starter to `config.json` next to the binary (CI default: `/opt/critique-bot/config.json`):

| Starter | Use |
| --- | --- |
| [`config.example.json`](../config.example.json) | Generic chat UI |
| [`config.chatgpt.example.json`](../config.chatgpt.example.json) | ChatGPT |

`--config` is required on every command. The file is JSON. Unknown keys are ignored. Env vars override matching fields when set.

GitLab runner layout and the worker/submit split: [`gitlab-ci.md`](gitlab-ci.md).

## `backend`

Optional. If present it must be `browser` (aliases: `web` / `playwright` / `ui`). HTTP backends (`ollama`, `openai`, `openai-compatible`) are not supported in this build.

## Minimal file that will start

A real `url` (not the `YOUR_CHAT_UI` placeholder) and two selectors:

```json
{
  "url": "https://chatgpt.com/",
  "selectors": {
    "prompt_input": "#prompt-textarea, [data-testid='prompt-textarea']",
    "assistant_messages": "[data-message-author-role='assistant'] .markdown"
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

That opens a local page, launches Edge on your chat URL, and lets you **click** the prompt box, the send button, a reply, and the stop button. It ranks the candidate selectors (preferring stable `data-*` and `aria-label` attributes over generated class hashes), writes them to `config.json`, and runs a live test.

By hand instead:

1. Open the chat UI in Edge.
2. Run `playwright codegen --channel msedge https://YOUR_CHAT_UI/` and click the prompt box, send, model picker, and an assistant reply.
3. Copy stable attributes (`data-testid`, `id`, `role`) rather than generated class hashes.
4. Verify with a one-shot prompt before starting the worker:

   `critique-bot --config config.json --mode general --prompt "Reply with exactly one word: PONG." --headed`

Comma-separated lists are OR: the first matching node is used.

---

## `url` (required for `browser`)

| | |
| --- | --- |
| Type | string |
| Env | `CRITIQUE_CHAT_URL` |
| Default | none |

Full URL of the chat page the bot should open. Must not contain `YOUR_CHAT_URL` / `YOUR_CHAT_UI`. Example: `https://chatgpt.com/`.

Edge is not allowed to call anything else after the chat page has loaded. Playwright aborts later XHR/fetch whose host is not the chat URL (or a subdomain of it). Loopback (`127.0.0.1` / `localhost`) stays open so CDP and the local setup page still work. For `https://chatgpt.com/` the first-party hosts that page itself is served from (`openai.com`, `oaistatic.com`, `oaiusercontent.com`, Cloudflare, Arkose) are also allowed; Google, GitHub, ads, and arbitrary sites are not. The first navigation is not intercepted, so login and Cloudflare challenges can finish.

The worker navigates here at session start. Login/SSO pages are detected and logged; the bot cannot complete a login by itself — use `worker --headed` once.

---

## `selectors` (object, required for `browser`)

| Key | Required | Meaning |
| --- | --- | --- |
| `prompt_input` | **yes** | Composer: `<textarea>`, contenteditable, or equivalent |
| `assistant_messages` | **yes** | Nodes whose **visible text** is the assistant reply. The bot reads the last match as it grows |
| `send_button` | no | Send control. Empty → press Enter in the prompt |
| `stop_button` | no | The "stop generating" control. **Strongly recommended:** it is how the bot knows a reply actually finished |
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

### `stop_button`

Optional but the most important selector for review quality. While this control is visible the assistant is still writing, so the bot keeps waiting no matter how long the model pauses. When it disappears, the reply is finished and the bot returns it after a short settle.

ChatGPT example:

```text
button[data-testid='stop-button'], button[aria-label*='Stop' i]
```

Leave it empty and the bot falls back to `idle_ms`: it assumes a reply that stopped changing for that long is done. A model that pauses longer than `idle_ms` mid-answer — extended thinking, a tool call, rate limiting — then yields a **silently truncated** review. When that happens the run logs a warning and `review.json` records `completion.complete: false`, so you can tell truncated output apart from finished output.

Even with `stop_button` empty, the bot also treats a visible `aria-busy="true"` on a reply bubble as "still generating".

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
| Default | `""` (browser: skip picker) |

**browser:** visible dropdown label. Empty is valid (uses whatever the signed-in session already has selected). Placeholder `YOUR_MODEL_NAME` is rejected.

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

Fallback completion signal, used only when no generating indicator is visible (see `selectors.stop_button`). After the assistant text stops changing for this long *and* nothing says generation is in progress, the reply is treated as complete. Too low: truncated reviews. Too high: extra wait on every job. ChatGPT example uses `6000`.

When `stop_button` is configured, `idle_ms` no longer decides completion — it only sizes the settle window after generation stops.

### `job_timeout_seconds`

| | |
| --- | --- |
| Type | number ≥ 0 |
| Default | `0` (auto: `timeout_ms × 2 + 60s`) |

Wall-clock ceiling for one queued job. If a job overruns, the worker marks it failed so a waiting `submit` (and the CI job behind it) stops blocking. A wedged Playwright call cannot be interrupted, so this bounds the *waiting*, not the work; the browser-restart path clears the session afterwards.

When files are staged across chat turns, the auto ceiling becomes `(staged files + 2) × timeout_ms + 60s` so ACK turns are not killed at the single-paste 420s default.

---

## Input caps

These protect the chat UI from huge patches. Values above the absolute max are clamped (with a warning).

| Key | Default | Absolute max | Meaning |
| --- | --- | --- | --- |
| `max_prompt_chars` | `120000` | `400000` | Entire prompt sent in **one** paste (template + files + patch). If files would push past this, they are sent one per chat turn instead |
| `max_file_chars` | `32000` | `200000` | Per attached file, after read |
| `max_files` | `80` | `400` | How many changed-file bodies are read from the checkout and sent (inlined or one per chat turn) |
| `max_read_bytes` | `16000000` | `64000000` | Bytes read from each path before decode |

Oversized / binary files are truncated or omitted and a short note is added to the prompt. Raise these only if the chat UI can actually accept that much paste.

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
| Default | `.edge-profile` |

Persistent Edge profile so cookies survive restarts.

| Value | Result |
| --- | --- |
| Relative path (default `.edge-profile`) | Resolved against the **process cwd**, not the config file. Worker systemd `WorkingDirectory=/opt/critique-bot` → `/opt/critique-bot/.edge-profile` |
| Absolute path | Used as-is. Prefer this on CI |
| `system` or `default` | Not the daily desktop profile (Chromium 136+ blocks remote debugging there and returns HTTP 403). Uses a dedicated dir **outside** Edge's default User Data path: Linux `~/.config/critique-bot/msedge-user-data`, Windows `%LOCALAPPDATA%\critique-bot\msedge-user-data` |

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

Inbox files are named `{timestamp-ms}-{label}-{serial}.json`. `label` is `group-app-mr42` (GitLab), a CI job id, or `local`. The worker claims the **oldest** inbox files first. Up to `max_parallel_tabs` reviews run at once (default **1**). Each `submit` waits only for the job id it just created; it does not look up reviews by MR.

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

How many reviews the worker may run at once. Edge tabs in the same signed-in instance (`1` is safest for ChatGPT rate limits). `3` means up to three MRs in flight; new sends still wait `min_interval_seconds`. Values above 8 are clamped to 8.

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

`--model` and `--cdp-url` override env and file. GitLab comment posting uses `CRITIQUE_GITLAB_TOKEN` (not this file); see [`gitlab-ci.md`](gitlab-ci.md).

---

## Suggested CI `config.json`

Adjust `url` / `selectors` to your chat UI. Keep paths absolute.

```json
{
  "backend": "browser",
  "url": "https://chatgpt.com/",
  "selectors": {
    "model_dropdown": "",
    "model_dropdown_identifier": "model-switcher",
    "model_option": "[role='menuitemradio'], [role='menuitem'], [role='option']",
    "prompt_input": "#prompt-textarea, [data-testid='prompt-textarea'], #mobile-composer-prompt, textarea[data-mobile-composer-prompt]",
    "send_button": "button[data-testid='send-button'], button[data-composer-submit], button.wm-composer-submitButton",
    "stop_button": "button[data-testid='stop-button'], button[aria-label*='Stop' i]",
    "assistant_messages": "[data-message-author-role='assistant'] .markdown, [data-assistant-markdown], [data-message-role='assistant']"
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

Do not commit this file if it contains a private chat URL or a `storage_state` path.

## Validation the loader enforces

- File exists and is a JSON object.
- If `backend` is set, it must be `browser` (or `web` / `playwright` / `ui`).
- `selectors.prompt_input` and `selectors.assistant_messages` are non-empty; `url` is non-empty and not a placeholder.
- If `storage_state` is set, that path is a file.
- Integer/float fields: `timeout_ms`, `idle_ms`, `result_retention`, and the `max_*` keys must be > 0; interval fields, `turn_pause_seconds`, and `job_timeout_seconds` must be ≥ 0.

A one-shot `--mode general` prompt (or **Send test prompt** in `critique-bot setup`) is the check the loader cannot do: Edge is installed, the profile holds a session, the selectors match the live page, and a real reply comes back.
