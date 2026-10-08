# Headless bare minimum

In headless mode only, drop images, fonts, media, motion, and browser background work so Edge does the minimum needed to send a prompt and read the reply. The reply is already read from the DOM; the renderer stays, because the page and the bot both need layout. A visible window is unchanged.

Only when the bot launches headless (`headed` is false). `--headed`, the first-login window, and the Cloudflare retry in `src/critique_bot/agent.py` stay a normal browser, because those runs need images and a real compositor.

Token generation time does not change. This cuts what Edge downloads and paints while it waits.

## Do not turn rendering off

The reply is already taken from the DOM. `_wait_for_reply` in `src/critique_bot/chat_client.py` calls `inner_text()`. Nothing OCRs the window. A screenshot is saved only when a run fails, in `src/critique_bot/output.py`.

Chromium still has to run the page. The chat UI is a script that creates the reply nodes as tokens arrive. There is no mode that returns that live DOM without the renderer process. Three different steps get conflated:

- **DOM and scripts.** Required. Without them there is no reply node to parse.
- **Style and layout.** Required. The bot's visibility checks, the stop-button probe, and Playwright's click hit-testing all use box size and computed style. `innerText` also forces layout. The page itself reads layout (virtualized message list, composer). A document with no layout comes back as 0×0, and both the site and the bot treat the reply as missing.
- **Paint (pixels).** Headless already does not put a window on screen. Paint still fills an offscreen buffer. Skipping that buffer is not a supported switch, and `--disable-software-rasterizer` on top of `--disable-gpu` is how headless runs go blank and break challenges. Not part of this change.

`DOM.getDocument` over CDP is the same renderer Playwright already uses. It does not avoid running the page.

## Do not call the send URL directly

The send button is a click, not a fixed request. The signed-in page then builds a streaming POST itself. The path is not enough to replay:

- The URL and JSON body change with the site. This bot is also aimed at whatever chat URL is in `config.json`, not one vendor.
- Immediately before that POST, the page script mints short-lived tokens and attaches them as headers. A copied path, without those tokens, is rejected. The tokens are produced by obfuscated page code and rotate. Reimplementing that is out of scope.
- The call also needs the browser's session cookies. Lifting those out and replaying them from Python is the same session, with none of the token logic, and it breaks the next time the site changes.

Watching the request from Playwright (method, path, status) can confirm what the click triggered. It does not yield a client we can call instead of the button.

The supported way to skip the page is the vendor's official API, with an API key. That is a different backend, a different auth model, and a different product from the signed-in web UI this bot drives. Not part of this change. The send path stays `_fill_prompt` plus `_send` in `src/critique_bot/chat_client.py`.

## Keep

The reply path needs these, so they stay:

- `document`, `script`, `stylesheet`, `xhr`, `fetch`, `websocket`, `eventsource`
- CSS. `_visible_count` in `src/critique_bot/chat_client.py` and the stop-button probe use layout and computed style (`display`, `visibility`, `opacity`, box size). Without the site's CSS the reply or the stop button can look missing.
- The chat host family already allowed by `request_is_allowed` (the page, its API, Cloudflare, Arkose)
- Canvas and WebGL. A headless challenge often uses them; disabling them would turn a signed-in headless run into a headed retry.

Do not set `animation: none`. A reply that fades in from `opacity: 0` would stay invisible.

## Drop (headless only)

In `src/critique_bot/browser.py`:

**Motion.** `quiet_context(context)` when `headed` is false, on the persistent context, desktop Edge, and an attached `cdp_url`:

- `context.add_init_script` injects `#critique-bot-quiet` with `animation-duration` / `transition-duration` of `0.01ms`, one iteration, and `scroll-behavior: auto`
- `page.emulate_media(reduced_motion="reduce")` on open tabs
- the same sheet into the current document, because an init script does not run on a tab that is already open

**Decorative requests.** Extend `_filter_chat_route` with a headless flag. After the host is allowed, abort resource types `image`, `imageset`, `media`, `font`, `texttrack`, `manifest`, and `ping`. Documents are never aborted (that is what makes `page.goto` hang). The route is still installed only after the chat page has loaded, same as `guard_page_network`.

**Process flags.** New `HEADLESS_ONLY_ARGS`, appended in `launch_edge` and `_start_desktop_edge` only when `headed` is false. Flags do nothing for a browser the user already started; the route and init script cover that attach.

- `--disable-gpu` (desktop headless already passes this; persistent headless does not)
- `--force-prefers-reduced-motion`
- `--disable-smooth-scrolling`
- `--hide-scrollbars`
- `--mute-audio`
- `--disable-remote-fonts`
- `--blink-settings=imagesEnabled=false`
- `--disable-accelerated-2d-canvas`
- `--disable-accelerated-video-decode`
- `--disable-extensions`
- `--disable-default-apps`
- `--disable-sync`
- `--disable-background-networking`
- `--disable-component-update`
- `--disable-domain-reliability`
- `--disable-client-side-phishing-detection`
- `--disable-breakpad`
- `--disable-crash-reporter`
- `--disable-hang-monitor`
- `--disable-notifications`
- `--disable-translate`
- `--no-pings`
- `--metrics-recording-only`
- `--disable-renderer-backgrounding`
- `--disable-background-timer-throttling`
- `--disable-backgrounding-occluded-windows`
- `--disable-features=Translate,MediaRouter,OptimizationHints,AudioServiceOutOfProcess,InterestFeedContentSuggestions,CalculateNativeWinOcclusion,HeavyAdIntervention,BackForwardCache,Prerender2`

The last three background flags keep the streaming page from being throttled while it is not focused. They are not extra features.

`EDGE_LAUNCH_ARGS` stays the shared set used for headed and headless.

## Read the reply once per poll

In `src/critique_bot/chat_client.py`, `_wait_for_reply` runs every `POLL_MS` (250ms). Each pass is several debugging calls: a generation probe, one `is_visible()` per assistant node, then `inner_text()` of the whole reply. `innerText` forces layout and copies the growing message back to Python four times a second. The loop only needs to know whether the text changed until the reply is done.

Replace that with one `page.evaluate` that returns `{generating, signal, count, length, tail}` for the latest visible assistant node. Call `innerText` once, when the function is about to return the reply.

When the stop signal was seen and then cleared, the settle wait is `idle_ms // 4` clamped to 300–2000ms. With the default `idle_ms` of 4000 that is an extra second after the page has already said it finished. Use a short constant (250ms) for that path. Keep the full `idle_ms` wait only when the page has no generation signal at all, which is the unreliable path.

Model selection (`MENU_OPEN_MS`) runs once per session, not once per turn. Leave it.

## Tests

In `tests/test_browser_chat.py`:

- `HEADLESS_ONLY_ARGS` is disjoint from the headed launch set, and includes images-off and reduced motion
- a fake context: headless `quiet_context` sets `reduced_motion` and the stylesheet; headed does not call it
- the route continues `script`, `stylesheet`, `xhr`, and `websocket`, and aborts `image`, `media`, and `font` only when headless

In the chat-client tests: one evaluate per poll, full text read only when the reply finishes, and the shorter settle after a stop signal.

## Work

- Headless-only reduced-motion init script and `quiet_context()` in `browser.py`, applied on every `launch_edge` path
- Add `HEADLESS_ONLY_ARGS` and pass them from `launch_edge` and `_start_desktop_edge` when `headed` is false
- In the existing chat route, abort image, media, font, and similar types on allowed hosts when the page is headless
- One evaluate per poll in `_wait_for_reply`; pull full text only when the reply finishes; shorten settle when the stop signal was seen
- Cover headless vs headed flags, `quiet_context`, resource aborts, and the lighter reply poll

## What was built

The reply loop, the headless request cuts, and the no-throttle flags are in. The long flag list and the 250ms settle are not. Measured in headless Chrome with 80 replies in the chat: counting the replies went from 46ms (one call per message, every 250ms while waiting) to 1ms, and one streaming poll from 2.5ms to 0.8ms.

- **One probe per poll.** `_reply_state` in `src/critique_bot/chat_client.py` returns `{count, generating, signal, length, hash}` in one `page.evaluate`. A change is detected from the length and hash of the newest reply's `textContent`, which needs no layout and catches edits in the middle that the tail would miss. `innerText` is read once, when the reply is done. Visibility follows Playwright's `is_visible`. `_count_replies` replaces the per-message `is_visible()` loop before a send, after a stop, and after a continue click.
- **Fallback.** `selectors.assistant_messages` comes from `config.json` and may use Playwright-only syntax (`:has-text()`, `>>`) that `querySelectorAll` rejects. That page then keeps the old locator path (`_wait_for_reply_locators`), remembered per page. A probe that keeps failing mid-reply switches to it too.
- **Settle.** After the stop control goes away, the reply ends once the control has been gone for two polls in a row and the text has held still for 500ms (it was `idle_ms // 4`, 1000ms by default). Not 250ms: a reply read while the page is still reformatting costs a whole extra round trip with a slow model.
- **Timing.** Each turn logs `send_s`, `first_text_s`, and `total_s`, so the remaining time can be seen per turn.
- **Headless requests.** When crit launches the browser headless (not `--headed`, not `cdp_url`), the chat route aborts `image`, `media`, `font`, `texttrack`, and `manifest` requests. Challenge hosts (Cloudflare, Turnstile, Arkose, captchas) are never cut. `CRIT_LEAN_HEADLESS=0` turns it off.
- **No throttling.** `--disable-renderer-backgrounding`, `--disable-background-timer-throttling`, and `--disable-backgrounding-occluded-windows` are added to desktop Edge in both modes, because a minimized or covered visible window is throttled too. Playwright already passes them, and most of the list above, when it launches the browser itself (`--disable-background-networking`, `--disable-extensions`, `--disable-sync`, `--disable-component-update`, `--disable-breakpad`, `--disable-hang-monitor`, `--disable-default-apps`, its own `--disable-features`).

Left out:

- **The rest of the flag list.** Most are already there through Playwright. The others (`--mute-audio`, `--hide-scrollbars`, `--disable-remote-fonts`, `--blink-settings=imagesEnabled=false`, `--metrics-recording-only`) are the usual headless-automation fingerprint, and this bot already works to look less automated (`AutomationControlled`, no `--enable-automation`); one more challenge costs far more than they save. A second `--disable-features=` would also replace Playwright's list instead of adding to it.
- **Reduced motion.** Small gain; can be added later behind the same headless check.
