import * as assert from "node:assert/strict";
import { test } from "node:test";
import { describeTool, diffPaths, initialStatus, onAnswered, onExit, onPrompt, reduce } from "../src/status";

test("a task goes working → waiting → working → done", () => {
  let s = initialStatus();
  s = reduce(s, { type: "hello", model: "fake", mode: "ask" }, 1);
  assert.equal(s.state, "starting");
  s = reduce(s, { type: "ready" }, 2);
  assert.equal(s.state, "idle");
  s = onPrompt(s, "fix the bug", 3);
  assert.equal(s.state, "working");
  assert.equal(s.tasks, 1);
  s = reduce(s, { type: "tool_start", name: "run_command", args: { command: "npm test" } }, 4);
  assert.equal(s.activity, "run command: npm test");
  s = reduce(s, { type: "approval", id: "r1", kind: "edit", summary: "Edit a.py" }, 5);
  assert.equal(s.state, "waiting");
  assert.deepEqual(s.pending, ["r1"]);
  assert.match(s.activity, /Needs approval: Edit a.py/);
  s = onAnswered(s, "r1");
  assert.equal(s.state, "working");
  s = reduce(s, { type: "diff", diff: "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n" }, 6);
  assert.deepEqual(s.changedFiles, ["a.py"]);
  s = reduce(s, { type: "status", code: "COMPLETED", ok: true, reason: "" }, 7);
  assert.equal(s.state, "done");
  assert.equal(s.taskEndedAt, 7);
  s = reduce(s, { type: "ready" }, 8);
  assert.equal(s.state, "done", "ready after a status keeps how the task ended");
});

test("cancel clears a request; interrupted and failed tasks", () => {
  let s = onPrompt(reduce(initialStatus(), { type: "ready" }, 0), "t", 1);
  s = reduce(s, { type: "question", id: "q", text: "Which one?" }, 2);
  assert.equal(s.state, "waiting");
  s = reduce(s, { type: "cancel", id: "q" }, 3);
  assert.equal(s.state, "working");
  assert.equal(reduce(s, { type: "status", code: "INTERRUPTED" }, 4).state, "stopped");
  assert.equal(reduce(s, { type: "status", code: "BLOCKED", reason: "no access" }, 4).state, "failed");
});

test("a task that ends without a status event goes back to idle on ready", () => {
  let s = onPrompt(reduce(initialStatus(), { type: "ready" }, 0), "t", 1);
  s = reduce(s, { type: "ready" }, 2);
  assert.equal(s.state, "idle");
});

test("process exit", () => {
  const s = onPrompt(reduce(initialStatus(), { type: "ready" }, 0), "t", 1);
  assert.equal(onExit(s, 0, "", 5).state, "stopped");
  const failed = onExit(s, 1, "no config.json", 5);
  assert.equal(failed.state, "failed");
  assert.match(failed.activity, /no config.json/);
  assert.equal(failed.taskEndedAt, 5);
});

test("diffPaths handles new and deleted files", () => {
  const diff = [
    "--- /dev/null",
    "+++ b/new.txt",
    "@@ -0,0 +1 @@",
    "+x",
    "--- a/old.txt",
    "+++ /dev/null",
    "@@ -1 +0,0 @@",
    "-y",
  ].join("\n");
  assert.deepEqual(diffPaths(diff), ["new.txt", "old.txt"]);
});

test("describeTool", () => {
  assert.equal(describeTool("read_file", { path: "src/a.ts" }), "read file: src/a.ts");
  assert.equal(describeTool("list_files", {}), "list files");
});
