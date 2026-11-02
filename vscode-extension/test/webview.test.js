// The chat webview (media/chat.js) in jsdom: events render, answers post back,
// and agent text is never inserted as HTML.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { JSDOM } = require("jsdom");

const script = fs.readFileSync(path.join(__dirname, "..", "media", "chat.js"), "utf8");

function webview() {
  const dom = new JSDOM('<!DOCTYPE html><body><div id="app"></div></body>', { runScripts: "outside-only" });
  const posted = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (m) => posted.push(JSON.parse(JSON.stringify(m))) /* postMessage copies, like structured clone */, setState() {}, getState() {} });
  dom.window.eval(script);
  const send = (data) => dom.window.dispatchEvent(new dom.window.MessageEvent("message", { data }));
  const event = (e) => send({ kind: "event", event: e });
  const $ = (sel) => dom.window.document.querySelector(sel);
  const $$ = (sel) => [...dom.window.document.querySelectorAll(sel)];
  return { dom, posted, send, event, $, $$ };
}

const HELLO = {
  type: "hello", protocol: 1, version: "0.1.0", workspace: "/w", model: "gpt", shell: "bash", mode: "ask",
  modes: [], role: "generic", roles: [{ id: "generic", label: "Generic", summary: "" }, { id: "code-reviewer", label: "Code reviewer", summary: "" }],
};

test("says it is ready and sends typed tasks", () => {
  const w = webview();
  assert.deepEqual(w.posted[0], { type: "ready" });
  w.$("textarea").value = "fix the build";
  w.$("textarea").dispatchEvent(new w.dom.window.KeyboardEvent("keydown", { key: "Enter" }));
  assert.deepEqual(w.posted.at(-1), { type: "prompt", text: "fix the build" });
  assert.equal(w.$(".user").textContent, "fix the build");
});

test("hello fills the mode and role pickers", () => {
  const w = webview();
  w.event(HELLO);
  assert.equal(w.$("select.mode").value, "ask");
  assert.deepEqual(w.$$("select.role option").map((o) => o.value), ["generic", "code-reviewer"]);
  w.$("select.role").value = "code-reviewer";
  w.$("select.role").dispatchEvent(new w.dom.window.Event("change"));
  assert.deepEqual(w.posted.at(-1), { type: "set_role", role: "code-reviewer" });
});

test("agent text is escaped, markdown still renders", () => {
  const w = webview();
  w.event({ type: "assistant", text: "**Bold** <img src=x onerror=alert(1)> `code`\n\n```diff\n-a\n+b\n```" });
  const node = w.$(".assistant");
  assert.equal(node.querySelector("img"), null);
  assert.match(node.innerHTML, /&lt;img/);
  assert.equal(node.querySelector("strong").textContent, "Bold");
  assert.equal(node.querySelector("pre.diff .add").textContent, "+b");
});

test("an approval: No asks for a reason, then posts it", () => {
  const w = webview();
  w.event({
    type: "approval", id: "r1", kind: "command", summary: "Run make", detail: "make all", risky: "",
    options: [{ value: "yes", label: "Yes" }, { value: "always", label: "Yes, always" }, { value: "no", label: "No, and tell crit (esc)" }],
  });
  const buttons = w.$$(".approval .choices button");
  assert.deepEqual(buttons.map((b) => b.textContent), ["Yes", "Yes, always", "No, and tell crit"]);
  buttons[2].click();
  const reason = w.$(".approval .reason");
  assert.ok(!reason.classList.contains("hidden"));
  reason.value = "use ninja";
  w.$$(".approval .choices button").at(-1).click(); // Decline
  assert.deepEqual(w.posted.at(-1), { type: "answer", id: "r1", value: "no", reason: "use ninja" });
  assert.match(w.$(".approval .settled").textContent, /Declined: use ninja/);
});

test("a cancelled request is settled and cannot be answered", () => {
  const w = webview();
  w.event({ type: "question", id: "r2", text: "Which DB?", options: ["sqlite", "postgres"] });
  w.event({ type: "cancel", id: "r2" });
  assert.equal(w.$(".question .settled").textContent, "No longer waiting");
  assert.ok(w.$$(".question button").every((b) => b.disabled));
});

test("plan review: Yes in auto mode answers auto", () => {
  const w = webview();
  w.event({ type: "plan_review", id: "r3", options: [{ value: "auto", label: "Yes, auto" }, { value: "no", label: "No, keep planning" }] });
  w.$$(".plan .choices button")[0].click();
  assert.deepEqual(w.posted.at(-1), { type: "answer", id: "r3", value: "auto" });
});

test("tools, live output, and the changed-files card", () => {
  const w = webview();
  w.event({ type: "tool_start", name: "run_command", args: { command: "make" } });
  w.event({ type: "tool_output", text: "building...\n" });
  assert.match(w.$(".tool .output").textContent, /building/);
  w.event({ type: "tool_done", name: "run_command", args: { command: "make" }, ok: false, error: "exit 2", friendly: "That command didn't succeed.", result: "" });
  assert.ok(w.$(".tool").classList.contains("err"));
  assert.equal(w.$(".tool .tool-error").textContent, "That command didn't succeed.");
  w.event({ type: "diff", diff: "", index: 4, files: [{ path: "a.py", added: false, deleted: false, text: "--- a/a.py\n+++ b/a.py" }] });
  w.$$(".changes button").find((b) => b.textContent === "Open diff").click();
  assert.deepEqual(w.posted.at(-1), { type: "open_diff", diff: 4, file: 0 });
});

test("busy shows Stop; a stopped agent settles open requests", () => {
  const w = webview();
  w.send({ kind: "busy", busy: true });
  const stop = w.$$("button").find((b) => b.textContent === "Stop");
  assert.ok(!stop.classList.contains("hidden"));
  stop.click();
  assert.deepEqual(w.posted.at(-1), { type: "interrupt" });
  w.event({ type: "question", id: "r9", text: "?", options: [] });
  w.send({ kind: "stopped", code: 1, reason: "no config.json" });
  assert.ok(stop.classList.contains("hidden"));
  assert.equal(w.$(".question .settled").textContent, "No longer waiting");
  assert.match(w.$$(".system").at(-1).textContent, /crit stopped: no config\.json/);
});
