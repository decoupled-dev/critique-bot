/**
 * crit desktop: run several crit agents side by side and watch them.
 * The window is a plain page (renderer/) served from crit://app/ under a strict
 * CSP; it talks to Manager only through the preload bridge.
 */
import { app, BrowserWindow, dialog, ipcMain, Notification, protocol, shell } from "electron";
import * as fs from "fs";
import * as path from "path";
import { resolveCommand } from "./command";
import { Manager, NewAgent } from "./manager";

const APP_ROOT = path.resolve(__dirname, "..", "..", "..");
const RENDERER = path.join(APP_ROOT, "renderer");

protocol.registerSchemesAsPrivileged([
  { scheme: "crit", privileges: { standard: true, secure: true, supportFetchAPI: true } },
]);

let win: BrowserWindow | undefined;
let manager: Manager;
let quitting = false;

function settingsPath(): string {
  return path.join(app.getPath("userData"), "settings.json");
}

function readSettings(): { command?: string; headedBrowser?: boolean } {
  try {
    return JSON.parse(fs.readFileSync(settingsPath(), "utf8"));
  } catch {
    return {};
  }
}

function send(channel: string, ...args: unknown[]): void {
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, ...args);
  }
}

function serveRenderer(): void {
  const types: Record<string, string> = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
  };
  protocol.handle("crit", async (request) => {
    const url = new URL(request.url);
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
    const file = path.resolve(RENDERER, relative);
    if (url.host !== "app" || (file !== RENDERER && !file.startsWith(RENDERER + path.sep))) {
      return new Response("not found", { status: 404 });
    }
    try {
      const body = await fs.promises.readFile(file);
      return new Response(body, { headers: { "content-type": types[path.extname(file)] || "application/octet-stream" } });
    } catch {
      return new Response("not found", { status: 404 });
    }
  });
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 560,
    title: "crit",
    backgroundColor: "#1e1e1e",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  win.setMenuBarVisibility(false);
  // Links open in the system browser; the window itself never navigates away.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-frame-navigate", (event) => {
    if (!event.url.startsWith("crit://app/")) {
      event.preventDefault();
      if (/^https?:\/\//.test(event.url)) void shell.openExternal(event.url);
    }
  });
  win.on("closed", () => (win = undefined));
  void win.loadURL("crit://app/index.html");
}

function wireManager(): void {
  manager.on("projects", (projects) => send("projects", projects));
  manager.on("agent", (agent) => send("agent", agent));
  manager.on("removed", (id) => send("removed", id));
  manager.on("chat", (id, message) => send("chat", id, message));
  manager.on("term", (id, data) => send("term", id, data));
  manager.on("show-diff", (id, file) => send("show-diff", id, file));
  manager.on("show-log", (id, text) => send("show-log", id, text));
  manager.on("show-terminal", (id) => send("show-terminal", id));
  manager.on("attention", (id: string, kind: string, text: string) => {
    send("attention", id, kind, text);
    if (win && win.isFocused()) return;
    win?.flashFrame(true);
    if (Notification.isSupported()) {
      const title = kind === "waiting" ? "crit needs you" : kind === "done" ? "crit finished" : "crit stopped";
      const note = new Notification({ title, body: text, silent: kind !== "waiting" });
      note.on("click", () => {
        win?.show();
        win?.focus();
        send("select-agent", id);
      });
      note.show();
    }
  });
}

function wireIpc(): void {
  ipcMain.handle("state", () => ({ projects: manager.listProjects(), agents: manager.listAgents() }));
  ipcMain.handle("open-project", async (_e, folder?: string) => {
    let target = folder;
    if (!target) {
      const picked = await dialog.showOpenDialog(win!, { title: "Open a project folder", properties: ["openDirectory"] });
      if (picked.canceled || !picked.filePaths.length) return null;
      target = picked.filePaths[0];
    }
    return manager.openProject(target);
  });
  ipcMain.handle("close-project", (_e, id: string) => manager.closeProject(id));
  ipcMain.handle("create-agent", (_e, spec: NewAgent) => manager.createAgent(spec));
  ipcMain.on("chat", (_e, id: string, message: { type: string }) => manager.chatMessage(id, message));
  ipcMain.handle("agent", async (_e, id: string, action: string, arg?: string) => {
    switch (action) {
      case "prompt":
        return manager.prompt(id, String(arg || ""));
      case "interrupt":
        return manager.interrupt(id);
      case "stop":
        return manager.stopAgent(id);
      case "restart":
        return manager.restart(id);
      case "remove":
        return manager.removeAgent(id, false);
      case "discard": {
        const agent = manager.listAgents().find((a) => a.id === id);
        if (!agent) return;
        const answer = await dialog.showMessageBox(win!, {
          type: "warning",
          message: `Delete ${agent.name}?`,
          detail: agent.isolated
            ? `This deletes the folder ${agent.workspace} and branch ${agent.branch}, with any changes not applied to the project.`
            : "The agent stops and leaves the list. Its changes stay in the project folder.",
          buttons: ["Delete", "Cancel"],
          defaultId: 1,
          cancelId: 1,
        });
        if (answer.response !== 0) return false;
        await manager.removeAgent(id, true);
        return true;
      }
      case "changes":
        return manager.changes(id);
      case "file-diff":
        return manager.fileDiff(id, String(arg || ""));
      case "worktree-diff":
        return manager.worktreeDiff(id);
      case "apply":
        return manager.applyToProject(id);
      case "terminal":
        return manager.terminalText(id);
      case "reveal": {
        const agent = manager.listAgents().find((a) => a.id === id);
        if (agent) await shell.openPath(agent.workspace);
        return;
      }
    }
    throw new Error(`unknown action ${action}`);
  });
}

app.whenReady().then(async () => {
  serveRenderer();
  const settings = readSettings();
  const roots = [process.resourcesPath, APP_ROOT];
  manager = new Manager(
    () => resolveCommand(roots, settings.command),
    path.join(app.getPath("userData"), "state.json"),
    { headedBrowser: settings.headedBrowser !== false },
  );
  wireManager();
  wireIpc();
  createWindow();
  await manager.restore();
  for (const arg of process.argv.slice(app.isPackaged ? 1 : 2)) {
    if (!arg.startsWith("-") && fs.existsSync(arg) && fs.statSync(arg).isDirectory()) {
      await manager.openProject(arg).catch(() => undefined);
    }
  }
  if (process.env.CRIT_DESKTOP_SELFTEST) {
    const { runSelfTest } = await import("../test/selftest-main");
    runSelfTest(manager, () => win).then(
      (code) => app.exit(code),
      (err) => {
        console.error("SELFTEST_FAIL", err);
        app.exit(1);
      },
    );
  }
});

app.on("before-quit", (event) => {
  if (quitting || !manager) return;
  event.preventDefault();
  quitting = true;
  void manager.shutdown().finally(() => app.quit());
});

app.on("window-all-closed", () => app.quit());
