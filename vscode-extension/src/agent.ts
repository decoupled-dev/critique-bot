/**
 * The crit agent as a child process speaking the JSON-lines protocol
 * (docs/agent-protocol.md). Plain Node: no `vscode` import, so tests run it
 * outside the editor.
 */
import { ChildProcess, spawn } from "child_process";
import { EventEmitter } from "events";
import * as fs from "fs";
import * as path from "path";

/** Must match PROTOCOL_VERSION in src/critique_bot/agent_protocol.py. */
export const PROTOCOL_VERSION = 1;

export type AgentEvent = { type: string; [key: string]: unknown };
export type AgentMessage = { type: string; [key: string]: unknown };

export interface AgentCommand {
  /** Executable to run. */
  command: string;
  /** Arguments that select the protocol front end. */
  args: string[];
  /** Where it came from, for messages: "setting", "bundled", or "PATH". */
  source: string;
}

export interface StartOptions {
  workspace: string;
  config?: string;
  mode?: string;
  role?: string;
  extraArgs?: string[];
  env?: NodeJS.ProcessEnv;
}

const EXE = process.platform === "win32" ? ".exe" : "";

/**
 * Which agent to run: the `crit.agentPath` setting, else the binary bundled in
 * the extension (bin/critique-bot/critique-bot), else `crit` on PATH.
 */
export function resolveAgent(extensionRoot: string, setting: string | undefined): AgentCommand {
  const configured = (setting || "").trim();
  if (configured) {
    const base = path.basename(configured).toLowerCase().replace(/\.exe$/, "");
    // The bundled binary and `critique-bot` need --mode agent; `crit` and `bot-agent` imply it.
    const args = base === "critique-bot" ? ["--mode", "agent", "--protocol"] : ["--protocol"];
    return { command: configured, args, source: "setting" };
  }
  const bundled = path.join(extensionRoot, "bin", "critique-bot", "critique-bot" + EXE);
  if (fs.existsSync(bundled)) {
    ensureExecutable([
      bundled,
      path.join(extensionRoot, "bin", "critique-bot", "_internal", "playwright", "driver", "node" + EXE),
    ]);
    return { command: bundled, args: ["--mode", "agent", "--protocol"], source: "bundled" };
  }
  return { command: "crit", args: ["--protocol"], source: "PATH" };
}

/** The .vsix keeps file modes, but a copy or sync step may not; the agent and its Node driver must run. */
function ensureExecutable(files: string[]): void {
  if (process.platform === "win32") {
    return;
  }
  for (const file of files) {
    try {
      const mode = fs.statSync(file).mode;
      if ((mode & 0o111) !== 0o111) {
        fs.chmodSync(file, mode | 0o755);
      }
    } catch {
      /* missing or read-only install: spawn reports the real problem */
    }
  }
}

/**
 * One agent session. Emits "event" for each protocol event, "stderr" for each
 * stderr line, and "exit" (code, signal, reason) once the process is gone.
 */
export class AgentProcess extends EventEmitter {
  private child: ChildProcess | undefined;
  private buffer = "";
  private errBuffer = "";
  private stderrTail: string[] = [];
  private greeted = false;
  private failure = "";
  private finished = false;
  hello: AgentEvent | undefined;

  constructor(private readonly agent: AgentCommand) {
    super();
  }

  get running(): boolean {
    return !!this.child && this.child.exitCode === null && this.child.signalCode === null;
  }

  start(options: StartOptions): void {
    if (this.child) {
      throw new Error("agent already started");
    }
    const args = [...this.agent.args, "--repo-dir", options.workspace];
    if (options.config) {
      args.push("--config", options.config);
    }
    if (options.mode) {
      args.push("--permission-mode", options.mode);
    }
    if (options.role) {
      args.push("--role", options.role);
    }
    args.push(...(options.extraArgs || []));
    const child = spawn(this.agent.command, args, {
      cwd: options.workspace,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1", ...options.env },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;
    child.stdout!.setEncoding("utf8");
    child.stderr!.setEncoding("utf8");
    child.stdout!.on("data", (chunk: string) => this.onStdout(chunk));
    child.stderr!.on("data", (chunk: string) => this.onStderr(chunk));
    child.stdin!.on("error", () => {
      /* the process exited; "exit" reports it */
    });
    child.on("error", (err: NodeJS.ErrnoException) => {
      this.failure =
        err.code === "ENOENT"
          ? `could not find the agent (${this.agent.command}, from ${this.agent.source}). ` +
            "Install critique-bot so `crit` is on PATH, or set crit.agentPath."
          : `could not start the agent: ${err.message}`;
      this.finish(null, null);
    });
    child.on("close", (code, signal) => this.finish(code, signal));
  }

  send(message: AgentMessage): boolean {
    const stdin = this.child?.stdin;
    if (!stdin || stdin.destroyed || !stdin.writable) {
      return false;
    }
    stdin.write(JSON.stringify(message) + "\n");
    return true;
  }

  prompt(text: string): boolean {
    return this.send({ type: "prompt", text });
  }

  answer(id: string, value: string, reason?: string): boolean {
    return this.send({ type: "answer", id, value, ...(reason ? { reason } : {}) });
  }

  interrupt(): boolean {
    return this.send({ type: "interrupt" });
  }

  /** Ask the agent to end; kill it if it has not gone after `graceMs`. */
  stop(graceMs = 5000): Promise<void> {
    const child = this.child;
    if (!child || !this.running) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.kill();
        resolve();
      }, graceMs);
      this.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
      this.send({ type: "shutdown" });
      child.stdin?.end();
    });
  }

  /** The last stderr lines, for an error message. */
  recentStderr(): string {
    return this.stderrTail.join("\n");
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) {
        this.onLine(line);
      }
    }
  }

  private onLine(line: string): void {
    let event: AgentEvent;
    try {
      event = JSON.parse(line);
    } catch {
      this.emit("stderr", `[not protocol] ${line}`);
      return;
    }
    if (!event || typeof event.type !== "string") {
      return;
    }
    if (event.type === "hello") {
      this.greeted = true;
      this.hello = event;
      if (event.protocol !== PROTOCOL_VERSION) {
        this.failure =
          `the agent speaks protocol ${String(event.protocol)}, this extension speaks ${PROTOCOL_VERSION}. ` +
          "Update the extension and the agent together.";
        this.emit("event", { type: "error", message: this.failure });
        this.child?.kill();
        return;
      }
    }
    this.emit("event", event);
  }

  private onStderr(chunk: string): void {
    this.errBuffer += chunk;
    const lines = this.errBuffer.split(/\r?\n/);
    this.errBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) {
        continue;
      }
      this.stderrTail.push(line);
      if (this.stderrTail.length > 40) {
        this.stderrTail.shift();
      }
      this.emit("stderr", line);
    }
  }

  private finish(code: number | null, signal: NodeJS.Signals | null): void {
    if (this.finished) {
      return;
    }
    this.finished = true;
    this.child = undefined;
    if (this.buffer.trim()) {
      this.onLine(this.buffer.trim());
      this.buffer = "";
    }
    let reason = this.failure;
    if (!reason && code !== 0 && code !== null) {
      const errors = this.stderrTail.filter((line) => /^error:/i.test(line));
      reason = errors.length ? errors[errors.length - 1].replace(/^error:\s*/i, "") : "";
      if (!reason && !this.greeted) {
        reason = this.stderrTail.slice(-3).join(" ") || `the agent exited with code ${code}`;
      }
    }
    this.emit("exit", code, signal, reason);
  }
}
