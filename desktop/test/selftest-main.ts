/**
 * End-to-end check inside the real app (started by scripts/selftest.mjs with
 * CRIT_DESKTOP_SELFTEST=1): two agents in their own worktrees run against
 * the scripted fake model; one in auto mode, one that waits for an approval
 * which the test gives. Then the first agent's change is applied to the project.
 */
import type { BrowserWindow } from "electron";
import * as fs from "fs";
import * as path from "path";
import type { AgentSummary, Manager } from "../src/manager";

function wait<T>(what: string, check: () => T | undefined | false, timeoutMs = 90000): Promise<T> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      let value: T | undefined | false;
      try {
        value = check();
      } catch (err) {
        clearInterval(timer);
        reject(err);
        return;
      }
      if (value) {
        clearInterval(timer);
        resolve(value);
      } else if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        reject(new Error(`timed out waiting for ${what}`));
      }
    }, 200);
  });
}

async function shot(win: BrowserWindow | undefined, name: string): Promise<void> {
  const dir = process.env.CRIT_SELFTEST_SHOTS;
  if (!dir || !win) return;
  await new Promise((r) => setTimeout(r, 700));
  const image = await win.webContents.capturePage();
  fs.writeFileSync(path.join(dir, `${name}.png`), image.toPNG());
}

export async function runSelfTest(manager: Manager, getWin: () => BrowserWindow | undefined): Promise<number> {
  const repo = process.env.CRIT_SELFTEST_REPO;
  if (!repo) throw new Error("CRIT_SELFTEST_REPO is not set");
  const agents = new Map<string, AgentSummary>();
  manager.on("agent", (a: AgentSummary) => agents.set(a.id, a));
  const approvals = new Map<string, string>();
  manager.on("chat", (id: string, message: unknown) => {
    const m = message as { kind?: string; event?: { type?: string; id?: string } };
    if (m.kind === "event" && m.event?.type === "approval" && m.event.id) approvals.set(id, m.event.id);
  });

  const project = await manager.openProject(repo);
  const a = await manager.createAgent({ projectId: project.id, name: "fast", task: "change note.txt from one to two", mode: "auto", isolated: true });
  const b = await manager.createAgent({ projectId: project.id, name: "careful", task: "change note.txt from one to two", mode: "ask", isolated: true });
  const state = (id: string) => agents.get(id)?.status.state;

  await wait("the careful agent to ask for approval", () => state(b.id) === "waiting" && approvals.get(b.id));
  await wait("the fast agent to finish", () => state(a.id) === "done");
  getWin()?.webContents.send("select-agent", null);
  await shot(getWin(), "1-overview");
  getWin()?.webContents.send("select-agent", b.id);
  await shot(getWin(), "2-waiting");

  manager.chatMessage(b.id, { type: "answer", id: approvals.get(b.id)!, value: "yes" });
  await wait("the careful agent to finish", () => state(b.id) === "done");
  await shot(getWin(), "3-done");

  const read = (dir: string) => fs.readFileSync(path.join(dir, "note.txt"), "utf8");
  if (read(a.workspace) !== "two\n" || read(b.workspace) !== "two\n") throw new Error("an agent did not change its note.txt");
  if (read(repo) !== "one\n") throw new Error("the project folder changed before apply");
  const changes = await manager.changes(a.id);
  if (changes.length !== 1 || changes[0].path !== "note.txt") throw new Error(`unexpected changes: ${JSON.stringify(changes)}`);
  await manager.applyToProject(a.id);
  if (read(repo) !== "two\n") throw new Error("apply did not change the project folder");

  await manager.removeAgent(a.id, true);
  await manager.removeAgent(b.id, true);
  if (fs.existsSync(a.workspace) || fs.existsSync(b.workspace)) throw new Error("worktrees were not removed");
  await manager.shutdown();
  console.log("CRIT_DESKTOP_SELFTEST_OK");
  return 0;
}
