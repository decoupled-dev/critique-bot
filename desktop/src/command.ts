/** Which critique-bot executable the app runs, for agents and for the project host. */
import * as fs from "fs";
import * as path from "path";

export interface CritCommand {
  command: string;
  /** Arguments before the mode-specific ones (e.g. ["-m", "critique_bot"] for a Python). */
  baseArgs: string[];
  source: "CRIT_COMMAND" | "settings" | "bundled" | "PATH";
}

const EXE = process.platform === "win32" ? ".exe" : "";

/**
 * In order: the CRIT_COMMAND environment variable (a path, or a JSON array such
 * as ["/venv/bin/python","-m","critique_bot"]), the app setting, the agent
 * bundled with the app (resources/bin/critique-bot), then critique-bot on PATH.
 */
export function resolveCommand(resourceRoots: string[], setting?: string): CritCommand {
  const fromEnv = parse(process.env.CRIT_COMMAND);
  if (fromEnv) {
    return { ...fromEnv, source: "CRIT_COMMAND" };
  }
  const fromSetting = parse(setting);
  if (fromSetting) {
    return { ...fromSetting, source: "settings" };
  }
  for (const root of resourceRoots) {
    const bundled = path.join(root, "bin", "critique-bot", "critique-bot" + EXE);
    if (fs.existsSync(bundled)) {
      ensureExecutable([bundled, path.join(root, "bin", "critique-bot", "_internal", "playwright", "driver", "node" + EXE)]);
      return { command: bundled, baseArgs: [], source: "bundled" };
    }
  }
  return { command: "critique-bot", baseArgs: [], source: "PATH" };
}

function parse(raw: string | undefined): { command: string; baseArgs: string[] } | undefined {
  const text = (raw || "").trim();
  if (!text) {
    return undefined;
  }
  if (text.startsWith("[")) {
    try {
      const parts = JSON.parse(text);
      if (Array.isArray(parts) && parts.length && parts.every((p) => typeof p === "string")) {
        return { command: parts[0], baseArgs: parts.slice(1) };
      }
    } catch {
      /* fall through: treat as a path */
    }
  }
  return { command: text, baseArgs: [] };
}

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
      /* spawn reports the real problem */
    }
  }
}
