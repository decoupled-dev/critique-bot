/** The only bridge between the page and the main process. */
import { contextBridge, ipcRenderer } from "electron";

const channels = [
  "projects",
  "agent",
  "removed",
  "chat",
  "term",
  "show-diff",
  "show-log",
  "show-terminal",
  "attention",
  "select-agent",
] as const;

contextBridge.exposeInMainWorld("crit", {
  state: () => ipcRenderer.invoke("state"),
  openProject: (folder?: string) => ipcRenderer.invoke("open-project", folder),
  closeProject: (id: string) => ipcRenderer.invoke("close-project", id),
  createAgent: (spec: unknown) => ipcRenderer.invoke("create-agent", spec),
  agent: (id: string, action: string, arg?: string) => ipcRenderer.invoke("agent", id, action, arg),
  chat: (id: string, message: unknown) => ipcRenderer.send("chat", id, message),
  on: (channel: (typeof channels)[number], listener: (...args: unknown[]) => void) => {
    if (!channels.includes(channel)) throw new Error(`unknown channel ${channel}`);
    ipcRenderer.on(channel, (_event, ...args) => listener(...args));
  },
});
