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

## Open questions

- Should the Electron BrowserView replace the Playwright Edge process entirely
  (one Chromium, driven over CDP), or sit beside it?
- Packaging: ship the Python agent inside the extension (PyInstaller, see
  `packaging/critique-bot.spec`) or require a local install?
