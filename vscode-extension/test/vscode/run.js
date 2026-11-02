// In-editor smoke test: VS Code loads the extension, a task sent through the
// chat controller runs the agent (bundled, or crit.agentPath) against
// test/fake_model.py, and the file in the workspace changes.
// Started by test/vscode/launch.sh; not shipped in the .vsix.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

exports.run = async function run() {
  const ext = vscode.extensions.all.find((e) => e.packageJSON.name === "crit");
  assert.ok(ext, "extension not found");
  const api = await ext.activate();
  const commands = await vscode.commands.getCommands(true);
  for (const id of ["crit.newChat", "crit.interrupt", "crit.restart", "crit.setMode", "crit.showTerminal", "crit.askAboutSelection"]) {
    assert.ok(commands.includes(id), `missing command ${id}`);
  }
  await vscode.commands.executeCommand("crit.chat.focus");
  const folder = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const note = path.join(folder, "note.txt");
  api.controller.prompt("change note.txt from one to two");
  const deadline = Date.now() + 60000;
  while (fs.readFileSync(note, "utf8") !== "two\n") {
    if (Date.now() > deadline) throw new Error("note.txt did not change within 60s");
    await new Promise((r) => setTimeout(r, 250));
  }
  await api.controller.stop();
  console.log("CRIT_SMOKE_OK");
};
