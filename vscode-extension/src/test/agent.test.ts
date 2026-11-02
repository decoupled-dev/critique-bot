/**
 * AgentProcess against the real Python agent and a scripted local model
 * (test/fake_model.py). Needs a Python with critique-bot's dependencies:
 *
 *   CRIT_TEST_PYTHON=/path/to/python npm test
 *
 * Skipped when CRIT_TEST_PYTHON is not set.
 */
import assert from "node:assert/strict";
import { ChildProcess, execFileSync, spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { after, before, test } from "node:test";
import { AgentEvent, AgentProcess, resolveAgent } from "../agent";

const python = process.env.CRIT_TEST_PYTHON;
const extensionRoot = path.resolve(__dirname, "..", "..");
const repoRoot = path.resolve(extensionRoot, "..");
const port = 12000 + Math.floor(Math.random() * 2000);
let server: ChildProcess | undefined;

function workspace(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "crit-ext-"));
  fs.writeFileSync(path.join(dir, "note.txt"), "one\n");
  fs.writeFileSync(
    path.join(dir, "config.json"),
    JSON.stringify({ backend: "local", url: `http://127.0.0.1:${port}`, model: "fake", local: { api: "ollama" } }),
  );
  execFileSync("git", ["init", "-q"], { cwd: dir });
  return dir;
}

function agent(): AgentProcess {
  return record(new AgentProcess({ command: python!, args: ["-m", "critique_bot", "--mode", "agent", "--protocol"], source: "test" }));
}

function startIn(dir: string, process_: AgentProcess): void {
  process_.start({ workspace: dir, config: path.join(dir, "config.json"), env: { PYTHONPATH: path.join(repoRoot, "src") } });
}

/** Events in arrival order; next() consumes them, so events that arrive together are not missed. */
const seen = new WeakMap<AgentProcess, AgentEvent[]>();

function record(proc: AgentProcess): AgentProcess {
  const list: AgentEvent[] = [];
  seen.set(proc, list);
  proc.on("event", (event: AgentEvent) => list.push(event));
  return proc;
}

async function next(proc: AgentProcess, type: string, timeoutMs = 30000): Promise<AgentEvent> {
  const list = seen.get(proc)!;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const index = list.findIndex((event) => event.type === type);
    if (index >= 0) {
      return list.splice(0, index + 1)[index];
    }
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error(`no ${type} event; still queued: ${list.map((e) => e.type).join(", ")}`);
}

before(async () => {
  if (!python) {
    return;
  }
  server = spawn(python, [path.join(extensionRoot, "test", "fake_model.py"), String(port)], { stdio: "ignore" });
  await new Promise((r) => setTimeout(r, 800));
});

after(() => {
  server?.kill();
});

test("resolveAgent: setting, then bundled, then PATH", () => {
  assert.deepEqual(resolveAgent("/nowhere", "/opt/critique-bot").args, ["--mode", "agent", "--protocol"]);
  assert.deepEqual(resolveAgent("/nowhere", "/usr/bin/crit").args, ["--protocol"]);
  assert.equal(resolveAgent("/nowhere", "").source, "PATH");
});

test("a missing executable is reported, not thrown", async () => {
  const proc = new AgentProcess({ command: "/definitely/not/here/crit", args: [], source: "setting" });
  const exited = new Promise<string>((resolve) => proc.on("exit", (_c, _s, reason: string) => resolve(reason)));
  proc.start({ workspace: os.tmpdir() });
  assert.match(await exited, /could not find the agent/);
});

test("a task with an approved edit, end to end", { skip: !python && "set CRIT_TEST_PYTHON" }, async () => {
  const dir = workspace();
  const proc = agent();
  const events: string[] = [];
  proc.on("event", (e: AgentEvent) => events.push(e.type));
  const exited = new Promise<number | null>((resolve) => proc.on("exit", (code: number | null) => resolve(code)));
  startIn(dir, proc);
  const hello = await next(proc, "hello");
  assert.equal(hello.protocol, 1);
  await next(proc, "ready");
  proc.prompt("change note.txt from one to two");
  const approval = await next(proc, "approval");
  assert.equal(approval.kind, "edit");
  proc.answer(String(approval.id), "yes");
  const diff = await next(proc, "diff");
  assert.match(String(diff.diff), /\+two/);
  const status = await next(proc, "status");
  assert.equal(status.code, "COMPLETED");
  await next(proc, "ready");
  await proc.stop();
  assert.equal(await exited, 0);
  assert.equal(fs.readFileSync(path.join(dir, "note.txt"), "utf8"), "two\n");
  assert.equal(events[events.length - 1], "bye");
});

test("interrupt cancels a pending approval", { skip: !python && "set CRIT_TEST_PYTHON" }, async () => {
  const dir = workspace();
  const proc = agent();
  startIn(dir, proc);
  await next(proc, "ready");
  proc.prompt("change note.txt from one to two");
  const approval = await next(proc, "approval");
  proc.interrupt();
  const cancel = await next(proc, "cancel");
  assert.equal(cancel.id, approval.id);
  assert.equal((await next(proc, "status")).code, "INTERRUPTED");
  await proc.stop();
  assert.equal(fs.readFileSync(path.join(dir, "note.txt"), "utf8"), "one\n");
});
