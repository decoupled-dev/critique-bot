/**
 * Projects and their agents. No Electron import: main.ts wires the `emit`
 * callback to the window, and the self-test drives this class directly.
 *
 * - A project is a folder. Its host (`critique-bot desktop-host`) resolves the
 *   config and role and, for a web chat model, keeps one shared Edge window.
 * - An agent is one `critique-bot --mode agent --protocol` process. It works in
 *   the project folder, or in its own git worktree on branch crit/<name>.
 */
import { ChildProcess, spawn } from "child_process";
import { EventEmitter } from "events";
import * as fs from "fs";
import * as path from "path";
import { AgentEvent, AgentProcess } from "../../vscode-extension/src/agent";
import { FileDiff, parseUnifiedDiff } from "../../vscode-extension/src/diff";
import { CritCommand } from "./command";
import * as gitOps from "./git";
import { AgentStatus, initialStatus, onAnswered, onExit, onPrompt, reduce } from "./status";

export type HostState = "off" | "starting" | "ready" | "failed";

export interface HostInfo {
  state: HostState;
  message: string;
  config: string;
  role: string | null;
  local: boolean;
  cdpUrl: string | null;
}

export interface ProjectSummary {
  id: string;
  path: string;
  name: string;
  isGit: boolean;
  host: HostInfo;
}

export interface AgentSummary {
  id: string;
  projectId: string;
  name: string;
  workspace: string;
  isolated: boolean;
  branch: string | null;
  running: boolean;
  createdAt: number;
  status: AgentStatus;
}

export interface NewAgent {
  projectId: string;
  name: string;
  task: string;
  mode: string;
  /** Own git worktree (branch crit/<name>) instead of the project folder. */
  isolated: boolean;
}

/** Messages from the chat view (vscode-extension/media/chat.js). */
export type ChatMessage = { type: string; [key: string]: unknown };

export interface ManagerEvents {
  projects: (projects: ProjectSummary[]) => void;
  agent: (agent: AgentSummary) => void;
  removed: (id: string) => void;
  /** A message for the agent's chat view. */
  chat: (id: string, message: unknown) => void;
  /** Text for the agent's command terminal. */
  term: (id: string, data: string) => void;
  /** Something the user should see: a request, or a task that ended. */
  attention: (id: string, kind: "waiting" | "done" | "failed", text: string) => void;
  "show-diff": (id: string, file: { path: string; text: string }) => void;
  "show-log": (id: string, text: string) => void;
  "show-terminal": (id: string) => void;
}

interface Project {
  id: string;
  path: string;
  isGit: boolean;
  host: HostInfo;
  hostProc?: ChildProcess;
  hostReady?: Promise<HostInfo>;
  hostLog: string[];
}

interface Agent {
  id: string;
  projectId: string;
  name: string;
  workspace: string;
  worktree: gitOps.Worktree | null;
  mode: string;
  createdAt: number;
  proc?: AgentProcess;
  status: AgentStatus;
  queued: string[];
  chatLog: unknown[];
  term: string;
  stderr: string[];
  diffs: FileDiff[][];
}

interface SavedState {
  projects: { path: string }[];
  agents: {
    id: string;
    projectPath: string;
    name: string;
    workspace: string;
    worktree: gitOps.Worktree | null;
    mode: string;
    createdAt: number;
    task: string;
    activity: string;
  }[];
}

const TERM_LIMIT = 512 * 1024;
const CHAT_LIMIT = 4000;

export class Manager extends EventEmitter {
  private readonly projects = new Map<string, Project>();
  private readonly agents = new Map<string, Agent>();
  private counter = 0;

  constructor(
    private readonly command: () => CritCommand,
    private readonly statePath: string | undefined,
    private readonly options: { headedBrowser: boolean } = { headedBrowser: true },
  ) {
    super();
  }

  override emit<K extends keyof ManagerEvents>(event: K, ...args: Parameters<ManagerEvents[K]>): boolean {
    return super.emit(event, ...args);
  }

  // ------------------------------------------------------------------ projects

  async openProject(folder: string): Promise<ProjectSummary> {
    const resolved = path.resolve(folder);
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
      throw new Error(`${resolved} is not a folder`);
    }
    const root = (await gitOps.repoRoot(resolved)) || resolved;
    for (const project of this.projects.values()) {
      if (project.path === root) {
        return this.projectSummary(project);
      }
    }
    const project: Project = {
      id: `p${++this.counter}`,
      path: root,
      isGit: (await gitOps.repoRoot(root)) !== undefined,
      host: { state: "off", message: "", config: "", role: null, local: false, cdpUrl: null },
      hostLog: [],
    };
    this.projects.set(project.id, project);
    this.emitProjects();
    this.save();
    return this.projectSummary(project);
  }

  closeProject(projectId: string): void {
    const project = this.projects.get(projectId);
    if (!project) {
      return;
    }
    for (const agent of [...this.agents.values()].filter((a) => a.projectId === projectId)) {
      void agent.proc?.stop();
      this.agents.delete(agent.id);
      this.emit("removed", agent.id);
    }
    this.stopHost(project);
    this.projects.delete(projectId);
    this.emitProjects();
    this.save();
  }

  listProjects(): ProjectSummary[] {
    return [...this.projects.values()].map((p) => this.projectSummary(p));
  }

  listAgents(): AgentSummary[] {
    return [...this.agents.values()].map((a) => this.agentSummary(a));
  }

  private projectSummary(project: Project): ProjectSummary {
    return { id: project.id, path: project.path, name: path.basename(project.path), isGit: project.isGit, host: { ...project.host } };
  }

  private emitProjects(): void {
    this.emit("projects", this.listProjects());
  }

  /** Start the project's host once; every agent of the project waits for it. */
  private ensureHost(project: Project): Promise<HostInfo> {
    if (project.hostReady && project.host.state !== "failed" && project.host.state !== "off") {
      return project.hostReady;
    }
    const cmd = this.command();
    const args = [...cmd.baseArgs, "desktop-host", "--repo-dir", project.path];
    if (!this.options.headedBrowser) {
      args.push("--no-headed");
    }
    project.host = { ...project.host, state: "starting", message: "Starting the browser…" };
    project.hostLog = [];
    this.emitProjects();
    project.hostReady = new Promise<HostInfo>((resolve, reject) => {
      let settled = false;
      let child: ChildProcess;
      try {
        child = spawn(cmd.command, args, {
          cwd: project.path,
          env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
          stdio: ["pipe", "pipe", "pipe"],
          windowsHide: true,
        });
      } catch (err) {
        reject(err);
        return;
      }
      project.hostProc = child;
      let buffer = "";
      const fail = (message: string) => {
        project.host = { ...project.host, state: "failed", message };
        this.emitProjects();
        if (!settled) {
          settled = true;
          reject(new Error(message));
        }
      };
      child.stdout!.setEncoding("utf8");
      child.stderr!.setEncoding("utf8");
      child.stdout!.on("data", (chunk: string) => {
        buffer += chunk;
        let nl: number;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (!line) continue;
          let msg: { type?: string; [k: string]: unknown };
          try {
            msg = JSON.parse(line);
          } catch {
            project.hostLog.push(line);
            continue;
          }
          if (msg.type === "host") {
            project.host = {
              state: "ready",
              message: msg.local ? "Local model" : msg.cdp_url ? "Shared browser ready" : "Ready",
              config: String(msg.config || ""),
              role: typeof msg.role === "string" ? msg.role : null,
              local: !!msg.local,
              cdpUrl: typeof msg.cdp_url === "string" ? msg.cdp_url : null,
            };
            this.emitProjects();
            if (!settled) {
              settled = true;
              resolve(project.host);
            }
          } else if (msg.type === "error") {
            fail(String(msg.message || "the project host failed"));
          }
        }
      });
      child.stderr!.on("data", (chunk: string) => {
        for (const line of chunk.split(/\r?\n/)) {
          if (line.trim()) project.hostLog.push(line);
        }
        if (project.hostLog.length > 200) project.hostLog.splice(0, project.hostLog.length - 200);
      });
      child.stdin!.on("error", () => undefined);
      child.on("error", (err: NodeJS.ErrnoException) => {
        fail(
          err.code === "ENOENT"
            ? `could not find ${cmd.command} (${cmd.source}). Install critique-bot, or set CRIT_COMMAND.`
            : `could not start ${cmd.command}: ${err.message}`,
        );
      });
      child.on("close", (code) => {
        if (project.hostProc !== child) return;
        project.hostProc = undefined;
        if (project.host.state === "ready" || project.host.state === "starting") {
          const tail = project.hostLog.filter((l) => /^error:/i.test(l)).pop() || project.hostLog.slice(-2).join(" ");
          if (code === 0 && project.host.state === "ready") {
            project.host = { ...project.host, state: "off", message: "Browser closed" };
            this.emitProjects();
          } else {
            fail(tail || `the project host exited (code ${code})`);
          }
        }
      });
    });
    project.hostReady.catch(() => undefined);
    return project.hostReady;
  }

  private stopHost(project: Project): void {
    const child = project.hostProc;
    project.hostProc = undefined;
    project.hostReady = undefined;
    project.host = { ...project.host, state: "off", message: "" };
    if (child) {
      child.stdin?.end();
      const timer = setTimeout(() => child.kill(), 8000);
      child.once("close", () => clearTimeout(timer));
    }
  }

  // ------------------------------------------------------------------ agents

  async createAgent(spec: NewAgent): Promise<AgentSummary> {
    const project = this.projects.get(spec.projectId);
    if (!project) {
      throw new Error("open a project first");
    }
    const taken = new Set([...this.agents.values()].filter((a) => a.projectId === project.id).map((a) => a.name));
    let name = gitOps.safeName(spec.name || "");
    if (!spec.name?.trim()) {
      let n = 1;
      while (taken.has(`agent-${n}`)) n++;
      name = `agent-${n}`;
    }
    if (taken.has(name)) {
      throw new Error(`there is already an agent called ${name}`);
    }
    let worktree: gitOps.Worktree | null = null;
    if (spec.isolated) {
      if (!project.isGit) {
        throw new Error("this folder is not a git repository, so agents can only work in the folder itself");
      }
      worktree = await gitOps.addWorktree(project.path, name);
    }
    const agent: Agent = {
      id: `a${Date.now().toString(36)}${++this.counter}`,
      projectId: project.id,
      name,
      workspace: worktree ? worktree.path : project.path,
      worktree,
      mode: spec.mode || "",
      createdAt: Date.now(),
      status: { ...initialStatus(), state: "idle", activity: "Not started" },
      queued: [],
      chatLog: [],
      term: "",
      stderr: [],
      diffs: [],
    };
    this.agents.set(agent.id, agent);
    this.emitAgent(agent);
    this.save();
    if (spec.task.trim()) {
      this.prompt(agent.id, spec.task);
    }
    return this.agentSummary(agent);
  }

  prompt(id: string, text: string): void {
    const agent = this.agents.get(id);
    const task = text.trim();
    if (!agent || !task) {
      return;
    }
    const busy = ["starting", "working", "waiting"].includes(agent.status.state);
    if (agent.proc?.running && agent.proc.hello && !busy && !agent.queued.length) {
      this.send(agent, task);
      return;
    }
    agent.queued.push(task);
    if (!agent.proc) {
      void this.startAgent(agent);
    }
  }

  private send(agent: Agent, task: string): void {
    if (!agent.proc?.prompt(task)) {
      return;
    }
    if (!task.startsWith("/")) {
      agent.status = onPrompt(agent.status, task, Date.now());
      this.emitAgent(agent);
      this.save();
    }
    this.post(agent, { kind: "busy", busy: true });
  }

  private async startAgent(agent: Agent): Promise<void> {
    const project = this.projects.get(agent.projectId);
    if (!project) return;
    agent.status = { ...agent.status, state: "starting", activity: "Starting…" };
    this.emitAgent(agent);
    const placeholder = new AgentProcess({ command: "", args: [], source: "" });
    agent.proc = placeholder; // keeps a second prompt from starting another process
    this.post(agent, { kind: "starting", source: "bundled" });
    let host: HostInfo;
    try {
      host = await this.ensureHost(project);
    } catch (err) {
      agent.proc = undefined;
      agent.queued = [];
      agent.status = onExit(agent.status, 1, String((err as Error).message || err), Date.now());
      this.post(agent, { kind: "stopped", code: 1, reason: String((err as Error).message || err) });
      this.emitAgent(agent);
      return;
    }
    if (agent.proc !== placeholder) return; // stopped while waiting
    const cmd = this.command();
    const proc = new AgentProcess({ command: cmd.command, args: [...cmd.baseArgs, "--mode", "agent", "--protocol"], source: cmd.source });
    agent.proc = proc;
    proc.on("event", (event: AgentEvent) => this.onAgentEvent(agent, proc, event));
    proc.on("stderr", (line: string) => {
      agent.stderr.push(line);
      if (agent.stderr.length > 2000) agent.stderr.splice(0, agent.stderr.length - 2000);
    });
    proc.on("exit", (code: number | null, _signal: string | null, reason: string) => {
      if (agent.proc !== proc) return;
      agent.proc = undefined;
      agent.queued = [];
      const before = agent.status.state;
      agent.status = onExit(agent.status, code, reason, Date.now());
      this.post(agent, { kind: "stopped", code, reason });
      this.emitAgent(agent);
      this.save();
      if (reason && before !== "stopped") {
        this.emit("attention", agent.id, "failed", `${agent.name}: ${reason}`);
      }
    });
    const extraArgs = host.cdpUrl ? ["--cdp-url", host.cdpUrl] : [];
    try {
      proc.start({
        workspace: agent.workspace,
        config: host.config || undefined,
        mode: agent.mode || undefined,
        role: host.role || undefined,
        extraArgs,
        env: { CRITIQUE_CDP_OWN_TAB: "1" },
      });
    } catch (err) {
      agent.proc = undefined;
      this.post(agent, { kind: "stopped", code: 1, reason: String(err) });
    }
  }

  private onAgentEvent(agent: Agent, proc: AgentProcess, event: AgentEvent): void {
    if (agent.proc !== proc) return;
    const before = agent.status;
    agent.status = reduce(agent.status, event, Date.now());
    switch (event.type) {
      case "ready":
        this.post(agent, { kind: "busy", busy: false });
        if (agent.queued.length) {
          this.send(agent, agent.queued.shift()!);
        }
        break;
      case "mode":
        if (typeof event.mode === "string") agent.mode = event.mode;
        break;
      case "tool_start":
        if (event.name === "run_command") {
          const args = (event.args || {}) as { command?: string };
          this.term(agent, `\r\n\x1b[1;36m$ ${String(args.command || "")}\x1b[0m\r\n`);
        }
        break;
      case "tool_output":
        this.term(agent, String(event.text || "").replace(/\r?\n/g, "\r\n"));
        break;
      case "tool_done":
        if (event.name === "run_command") {
          const ok = !!event.ok;
          const note = String(event.friendly || event.error || "");
          this.term(agent, ok ? "\x1b[32m✓ done\x1b[0m\r\n" : `\x1b[31m✗ ${note || "failed"}\x1b[0m\r\n`);
        }
        break;
      case "diff": {
        const files = parseUnifiedDiff(String(event.diff || ""));
        agent.diffs.push(files);
        const index = agent.diffs.length - 1;
        this.post(agent, {
          kind: "event",
          event: { ...event, index, files: files.map((f) => ({ path: f.path, added: f.added, deleted: f.deleted, text: f.text })) },
        });
        this.emitAgent(agent);
        return;
      }
    }
    this.post(agent, { kind: "event", event });
    if (agent.status !== before) {
      this.emitAgent(agent);
    }
    if (agent.status.state !== before.state) {
      this.save();
      const s = agent.status;
      if (s.state === "waiting") this.emit("attention", agent.id, "waiting", `${agent.name}: ${s.activity}`);
      if (s.state === "done") this.emit("attention", agent.id, "done", `${agent.name} finished: ${s.task}`);
      if (s.state === "failed") this.emit("attention", agent.id, "failed", `${agent.name}: ${s.activity}`);
    }
  }

  /** A message from the agent's chat view. */
  chatMessage(id: string, message: ChatMessage): void {
    const agent = this.agents.get(id);
    if (!agent) return;
    const proc = agent.proc;
    switch (message.type) {
      case "ready":
        // A (re)loaded chat view: send everything it missed.
        this.emit("chat", id, { kind: "state", running: !!proc?.running, busy: agent.status.state === "working" || agent.status.state === "waiting" });
        for (const item of agent.chatLog) this.emit("chat", id, item);
        break;
      case "prompt":
        this.prompt(id, String(message.text || ""));
        break;
      case "answer": {
        const answerId = String(message.id || "");
        proc?.answer(answerId, String(message.value || ""), message.reason ? String(message.reason) : undefined);
        agent.status = onAnswered(agent.status, answerId);
        this.emitAgent(agent);
        break;
      }
      case "set_mode":
        agent.mode = String(message.mode || "");
        if (!proc?.send({ type: "set_mode", mode: agent.mode })) {
          this.post(agent, { kind: "event", event: { type: "mode", mode: agent.mode, label: agent.mode } });
        }
        agent.status = { ...agent.status, mode: agent.mode };
        this.emitAgent(agent);
        this.save();
        break;
      case "set_role":
        proc?.send({ type: "set_role", role: String(message.role || "") });
        break;
      case "new_chat":
        this.newChat(id);
        break;
      case "undo":
        proc?.send({ type: "undo" });
        break;
      case "interrupt":
        this.interrupt(id);
        break;
      case "restart":
        void this.restart(id);
        break;
      case "open_diff": {
        const file = agent.diffs[Number(message.diff)]?.[Number(message.file)];
        if (file) this.emit("show-diff", id, { path: file.path, text: file.text });
        break;
      }
      case "show_terminal":
        this.emit("show-terminal", id);
        break;
      case "show_log":
        this.emit("show-log", id, agent.stderr.join("\n"));
        break;
    }
  }

  interrupt(id: string): void {
    this.agents.get(id)?.proc?.interrupt();
  }

  newChat(id: string): void {
    const agent = this.agents.get(id);
    if (!agent) return;
    agent.proc?.send({ type: "new_chat" });
    agent.chatLog = [];
    this.emit("chat", id, { kind: "cleared" });
  }

  async stopAgent(id: string): Promise<void> {
    const agent = this.agents.get(id);
    const proc = agent?.proc;
    if (!agent || !proc) return;
    if (!proc.running) {
      agent.proc = undefined; // still waiting for the host
      agent.queued = [];
      agent.status = onExit(agent.status, 0, "", Date.now());
      this.emitAgent(agent);
      return;
    }
    await proc.stop();
  }

  async restart(id: string): Promise<void> {
    const agent = this.agents.get(id);
    if (!agent) return;
    await this.stopAgent(id);
    agent.chatLog = [];
    this.emit("chat", id, { kind: "cleared" });
    void this.startAgent(agent);
  }

  /** Stop the agent and forget it; with `discard`, also delete its worktree and branch. */
  async removeAgent(id: string, discard: boolean): Promise<void> {
    const agent = this.agents.get(id);
    if (!agent) return;
    await this.stopAgent(id);
    if (discard && agent.worktree) {
      const project = this.projects.get(agent.projectId);
      if (project) await gitOps.removeWorktree(project.path, agent.worktree);
    }
    this.agents.delete(id);
    this.emit("removed", id);
    this.save();
  }

  async changes(id: string): Promise<{ status: string; path: string }[]> {
    const agent = this.agents.get(id);
    if (!agent?.worktree) return agent ? agent.status.changedFiles.map((p) => ({ status: "M", path: p })) : [];
    try {
      return await gitOps.worktreeChanges(agent.worktree.path, agent.worktree.base);
    } catch (err) {
      if (!fs.existsSync(agent.worktree.path)) return []; // deleted meanwhile
      throw err;
    }
  }

  /** The latest diff the agent reported for `file` (agents working in the project folder). */
  fileDiff(id: string, file: string): string {
    const agent = this.agents.get(id);
    for (const diff of [...(agent?.diffs || [])].reverse()) {
      const found = diff.find((f) => f.path === file);
      if (found) return found.text;
    }
    return "";
  }

  /** The worktree's full diff against where it started (for the diff view). */
  async worktreeDiff(id: string): Promise<string> {
    const agent = this.agents.get(id);
    if (!agent?.worktree) return "";
    return gitOps.worktreePatch(agent.worktree.path, agent.worktree.base);
  }

  /** Copy a worktree agent's changes into the project folder (uncommitted). */
  async applyToProject(id: string): Promise<string[]> {
    const agent = this.agents.get(id);
    const project = agent && this.projects.get(agent.projectId);
    if (!agent || !project) throw new Error("no such agent");
    if (!agent.worktree) throw new Error("this agent already works in the project folder");
    return gitOps.applyToProject(agent.worktree.path, agent.worktree.base, project.path);
  }

  async shutdown(): Promise<void> {
    this.save();
    await Promise.all([...this.agents.values()].map((a) => a.proc?.stop(4000)));
    for (const project of this.projects.values()) this.stopHost(project);
  }

  // ------------------------------------------------------------------ plumbing

  private post(agent: Agent, message: unknown): void {
    const kind = (message as { kind?: string }).kind;
    if (kind !== "busy") {
      agent.chatLog.push(message);
      if (agent.chatLog.length > CHAT_LIMIT) agent.chatLog.splice(0, agent.chatLog.length - CHAT_LIMIT);
    }
    this.emit("chat", agent.id, message);
  }

  private term(agent: Agent, data: string): void {
    agent.term += data;
    if (agent.term.length > TERM_LIMIT) agent.term = agent.term.slice(-TERM_LIMIT);
    this.emit("term", agent.id, data);
  }

  terminalText(id: string): string {
    return this.agents.get(id)?.term || "";
  }

  private agentSummary(agent: Agent): AgentSummary {
    return {
      id: agent.id,
      projectId: agent.projectId,
      name: agent.name,
      workspace: agent.workspace,
      isolated: !!agent.worktree,
      branch: agent.worktree?.branch || null,
      running: !!agent.proc?.running,
      createdAt: agent.createdAt,
      status: agent.status,
    };
  }

  private emitAgent(agent: Agent): void {
    this.emit("agent", this.agentSummary(agent));
  }

  // ------------------------------------------------------------------ saved state

  private save(): void {
    if (!this.statePath) return;
    const state: SavedState = {
      projects: [...this.projects.values()].map((p) => ({ path: p.path })),
      agents: [...this.agents.values()].map((a) => {
        const project = this.projects.get(a.projectId);
        return {
          id: a.id,
          projectPath: project ? project.path : "",
          name: a.name,
          workspace: a.workspace,
          worktree: a.worktree,
          mode: a.mode,
          createdAt: a.createdAt,
          task: a.status.task,
          activity: a.status.activity,
        };
      }),
    };
    try {
      fs.mkdirSync(path.dirname(this.statePath), { recursive: true });
      fs.writeFileSync(this.statePath + ".tmp", JSON.stringify(state, null, 2));
      fs.renameSync(this.statePath + ".tmp", this.statePath);
    } catch {
      /* the app still works without saved state */
    }
  }

  /** Reopen the projects and list the agents from last time (stopped; a new task starts them again). */
  async restore(): Promise<void> {
    if (!this.statePath || !fs.existsSync(this.statePath)) return;
    let state: SavedState;
    try {
      state = JSON.parse(fs.readFileSync(this.statePath, "utf8"));
    } catch {
      return;
    }
    for (const saved of state.projects || []) {
      try {
        await this.openProject(saved.path);
      } catch {
        /* folder gone */
      }
    }
    const byPath = new Map([...this.projects.values()].map((p) => [p.path, p]));
    for (const saved of state.agents || []) {
      const project = byPath.get(saved.projectPath);
      if (!project || !fs.existsSync(saved.workspace)) continue;
      const agent: Agent = {
        id: saved.id,
        projectId: project.id,
        name: saved.name,
        workspace: saved.workspace,
        worktree: saved.worktree,
        mode: saved.mode,
        createdAt: saved.createdAt,
        status: { ...initialStatus(), state: "stopped", task: saved.task || "", activity: "Stopped (app restarted). A new task starts it again." },
        queued: [],
        chatLog: [],
        term: "",
        stderr: [],
        diffs: [],
      };
      this.agents.set(agent.id, agent);
      this.emitAgent(agent);
    }
  }
}
