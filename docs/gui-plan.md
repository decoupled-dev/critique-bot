# GUI plan

Status: proposal, not started. Written 2026-10-23.

Goal: a graphical front end for the bot with a chat pane, an integrated
Chromium browser, and a terminal, in the spirit of Cursor or Antigravity.

## Starting point

- `crit` already has a full terminal UI (`agent_ui.py`): modes, `/` command
  menu, `@` path completion, spinner and live command tail.
- `critique-bot setup` already runs a small local HTTP server (`setup_ui.py`)
  with a web page that talks to the Playwright-driven Edge window.
- The agent loop, the browser driver, and the local-model adapter are the hard
  parts and they exist. A GUI only needs a transport to them.

## Options considered

### 1. VS Code extension (recommended first step)

- Sidebar chat as a webview; the bot's edits shown as native diffs; VS Code's
  own integrated terminal.
- The extension spawns the Python agent as a subprocess and talks JSON lines
  over stdio or a local socket.
- Works in VS Code, VSCodium, and Cursor. No editor to maintain.
- Limit: VS Code's Simple Browser is an iframe, so it cannot show chatgpt.com
  or most chat sites. Sign-in and the selector picker keep using the headed
  Edge window the bot already opens.

### 2. Standalone Electron app

- Electron ships Chromium, so an integrated browser is free: a BrowserView
  shows the chat page, and the bot can drive that same view over CDP instead
  of a separate Edge process.
- xterm.js plus node-pty for the terminal; a chat pane reusing the same
  webview UI as option 1.
- Right choice for a product that stands alone, but we then own file editing,
  diff views, and the file tree. Tauri is lighter but uses the system webview,
  so driving it over CDP is less reliable.

### 3. Fork VS Code (code-oss) like Cursor or Antigravity

- Full control of the editor chrome.
- Cost: merge upstream every month forever, build installers per OS, and no
  access to Microsoft's extension marketplace (Open VSX instead). Cursor has a
  team on this. Little gain over option 1 for this project's size.

## Decision

Do not fork VS Code. Build in this order:

1. **Agent protocol.** A JSON-lines transport around the agent loop (requests:
   prompt, approve edit, approve command, set mode, new chat; events: text
   delta, proposed edit, command output, ask-permission, done). Pure Python,
   reused by every option below.
2. **VS Code extension** in `vscode-extension/`: sidebar chat webview,
   subprocess bridge to `crit`, diff preview and apply for edits, run commands
   in the integrated terminal.
3. **Electron shell** (optional, later): same protocol and same chat webview,
   plus BrowserView for the chat page and xterm.js terminal.
4. Fork only if the extension API blocks something specific.

## Decided (2026-11-02)

### Browser: Electron view sits beside Edge, does not replace it

- `browser_backend: "edge"` stays the default: Playwright-driven Edge, used by
  CI (headless MR reviews on the Windows runner), the CLI, and the VS Code
  extension.
- `browser_backend: "embedded"` is used only inside the Electron app. Electron
  starts with `--remote-debugging-port`; the agent attaches with
  `connect_over_cdp`, the same path `cdp_url` already uses for desktop Edge.
- If sign-in or Cloudflare blocks the embedded view (Google and Microsoft
  often refuse embedded browsers), fall back to the Edge window.
- The security filter is enforced in both backends: Edge through launch args
  and the host filter, Electron through `session.webRequest` with the same
  `allowed_hosts`. One test suite checks the blocked channels in both.
- Why not replace: headless CI needs Edge anyway, the saved login lives in the
  Edge profile, and a second copy of the filter is the riskiest part.

### Packaging: the Python agent ships inside the GUI, one download

- The agent is frozen with PyInstaller (`packaging/critique-bot.spec`, which
  already bundles Playwright and its Node driver) and placed in the GUI's
  resources. Users install nothing else; Edge is the system Edge, as today.
- VS Code extension: platform-specific VSIX packages
  (`vsce package --target win32-x64`, `linux-x64`, ...), each carrying the
  matching agent binary. The extension spawns it and speaks the JSON-lines
  protocol over stdio.
- Electron: the agent binary goes in `extraResources`; electron-builder makes
  one installer per OS (NSIS on Windows, AppImage/deb on Linux).
- The GUI and the agent share one version number and are built together in
  CI, so the protocol never mismatches. The agent reports its protocol
  version on start; the GUI refuses a mismatch with a clear message.
- Cost to watch: the frozen agent is large (Playwright driver plus Node), and
  unsigned Windows binaries trigger SmartScreen, so plan for code signing.
