/**
 * What an agent is doing, derived from its protocol events
 * (docs/agent-protocol.md). Pure: no Electron, so tests run it directly.
 */
export type AgentState =
  | "starting" // process launched, no hello yet
  | "idle" // waiting for a task, none run yet
  | "working" // a task is running
  | "waiting" // a task needs an answer from the user
  | "done" // last task COMPLETED
  | "failed" // last task FAILED or BLOCKED, or the process died with an error
  | "stopped"; // last task INTERRUPTED/STOPPED, or the process ended

export interface AgentStatus {
  state: AgentState;
  /** One line: what it is doing now, or how the last task ended. */
  activity: string;
  /** The task text that is running or last ran. */
  task: string;
  taskStartedAt: number | null;
  taskEndedAt: number | null;
  /** Ids of approval/question/plan_review requests still open. */
  pending: string[];
  /** Files changed, from the "diff" events of every task. */
  changedFiles: string[];
  model: string;
  mode: string;
  tasks: number;
}

export function initialStatus(): AgentStatus {
  return {
    state: "starting",
    activity: "Starting…",
    task: "",
    taskStartedAt: null,
    taskEndedAt: null,
    pending: [],
    changedFiles: [],
    model: "",
    mode: "",
    tasks: 0,
  };
}

type Event = { type: string; [key: string]: unknown };

function str(value: unknown): string {
  return typeof value === "string" ? value : value === undefined || value === null ? "" : String(value);
}

function oneLine(text: string, max = 90): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > max ? line.slice(0, max - 1) + "…" : line;
}

/** A short description of a tool call, e.g. "run_command: npm test". */
export function describeTool(name: string, args: unknown): string {
  const a = (args && typeof args === "object" ? args : {}) as Record<string, unknown>;
  const detail = str(a.command || a.path || a.pattern || a.query || a.url || (Array.isArray(a.paths) ? a.paths.join(", ") : ""));
  const label = name.replace(/_/g, " ");
  return detail ? `${label}: ${oneLine(detail, 70)}` : label;
}

const END_STATE: Record<string, AgentState> = {
  COMPLETED: "done",
  FAILED: "failed",
  BLOCKED: "failed",
  INTERRUPTED: "stopped",
  STOPPED: "stopped",
};

/** Paths in a unified diff (the "+++ b/x" lines; "--- a/x" for deletions). */
export function diffPaths(diff: string): string[] {
  const paths: string[] = [];
  let minus = "";
  for (const line of diff.split("\n")) {
    if (line.startsWith("--- ")) {
      minus = line.slice(4).replace(/^a\//, "").trim();
    } else if (line.startsWith("+++ ")) {
      const plus = line.slice(4).replace(/^b\//, "").trim();
      const path = plus === "/dev/null" ? minus : plus;
      if (path && path !== "/dev/null" && !paths.includes(path)) {
        paths.push(path);
      }
    }
  }
  return paths;
}

/** The task was sent to the agent. */
export function onPrompt(status: AgentStatus, task: string, now: number): AgentStatus {
  return {
    ...status,
    state: "working",
    activity: "Starting the task…",
    task,
    taskStartedAt: now,
    taskEndedAt: null,
    pending: [],
    tasks: status.tasks + 1,
  };
}

/** The status after `event`. Never mutates `status`. */
export function reduce(status: AgentStatus, event: Event, now: number): AgentStatus {
  const s: AgentStatus = { ...status, pending: [...status.pending] };
  switch (event.type) {
    case "hello":
      s.model = str(event.model);
      s.mode = str(event.mode);
      if (s.state === "starting") {
        s.activity = "Connected";
      }
      return s;
    case "ready":
      // A finished task already got its state from "status"; slash commands never leave idle.
      // "working" here means the task ended without a status event.
      if (s.state === "starting" || s.state === "idle" || s.state === "working" || s.state === "waiting") {
        s.state = "idle";
        s.activity = "Waiting for a task";
      }
      s.pending = [];
      return s;
    case "mode":
      s.mode = str(event.mode);
      return s;
    case "thinking":
      if (event.active && (s.state === "working" || s.state === "waiting")) {
        s.activity = str(event.label) || "Thinking…";
      }
      return s;
    case "assistant":
      if (s.state === "working") {
        s.activity = oneLine(str(event.text)) || s.activity;
      }
      return s;
    case "tool_start":
      if (s.state === "working") {
        s.activity = describeTool(str(event.name), event.args);
      }
      return s;
    case "active":
      if (s.state === "working" && Number(event.count) > 0) {
        s.activity = `${event.count} helper chat${Number(event.count) === 1 ? "" : "s"} running`;
      }
      return s;
    case "approval":
    case "question":
    case "plan_review": {
      const id = str(event.id);
      if (id && !s.pending.includes(id)) {
        s.pending.push(id);
      }
      s.state = "waiting";
      s.activity =
        event.type === "approval"
          ? `Needs approval: ${oneLine(str(event.summary) || str(event.kind))}`
          : event.type === "question"
            ? `Asks: ${oneLine(str(event.text))}`
            : "Plan ready for review";
      return s;
    }
    case "cancel": {
      s.pending = s.pending.filter((id) => id !== str(event.id));
      if (s.state === "waiting" && !s.pending.length) {
        s.state = "working";
        s.activity = "Working…";
      }
      return s;
    }
    case "diff":
      for (const path of diffPaths(str(event.diff))) {
        if (!s.changedFiles.includes(path)) {
          s.changedFiles.push(path);
        }
      }
      return s;
    case "status": {
      const code = str(event.code).toUpperCase();
      s.state = END_STATE[code] || (event.ok ? "done" : "failed");
      const reason = oneLine(str(event.reason));
      s.activity = code ? `${code[0]}${code.slice(1).toLowerCase()}${reason ? `: ${reason}` : ""}` : reason || "Finished";
      s.taskEndedAt = now;
      s.pending = [];
      return s;
    }
    case "error":
      if (s.state === "starting") {
        s.state = "failed";
      }
      s.activity = oneLine(str(event.message)) || s.activity;
      return s;
    default:
      return s;
  }
}

/** The answer to request `id` was sent. */
export function onAnswered(status: AgentStatus, id: string): AgentStatus {
  const pending = status.pending.filter((p) => p !== id);
  if (status.state === "waiting" && !pending.length) {
    return { ...status, pending, state: "working", activity: "Working…" };
  }
  return { ...status, pending };
}

/** The process ended. */
export function onExit(status: AgentStatus, code: number | null, reason: string, now: number): AgentStatus {
  const failed = !!reason || (code !== null && code !== 0 && code !== 130);
  return {
    ...status,
    state: failed ? "failed" : "stopped",
    activity: reason ? `Agent stopped: ${oneLine(reason)}` : "Agent stopped",
    pending: [],
    taskEndedAt: status.taskEndedAt ?? (status.taskStartedAt ? now : null),
  };
}
