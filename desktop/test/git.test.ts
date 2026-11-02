import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { addWorktree, applyToProject, git, removeWorktree, safeName, withoutCritGitignore, worktreeChanges, worktreePath } from "../src/git";

async function repo(): Promise<string> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "crit-git-"));
  const root = path.join(dir, "proj");
  fs.mkdirSync(root);
  await git(root, ["init", "-q", "-b", "main"]);
  await git(root, ["config", "user.email", "test@example.invalid"]);
  await git(root, ["config", "user.name", "test"]);
  fs.writeFileSync(path.join(root, "a.txt"), "one\n");
  fs.writeFileSync(path.join(root, "gone.txt"), "bye\n");
  fs.writeFileSync(path.join(root, ".gitignore"), "node_modules/\n");
  await git(root, ["add", "-A"]);
  await git(root, ["commit", "-q", "-m", "init"]);
  return root;
}

test("safeName and worktreePath", () => {
  assert.equal(safeName("Fix Login Bug!"), "fix-login-bug");
  assert.equal(safeName("../.."), "agent");
  assert.equal(worktreePath("/w/proj", "a b"), path.join("/w", "proj.crit", "a-b"));
});

test("worktree: changes, apply to the project, remove", async () => {
  const root = await repo();
  const wt = await addWorktree(root, "one");
  assert.equal(wt.branch, "crit/one");
  assert.ok(fs.existsSync(path.join(wt.path, "a.txt")));

  fs.writeFileSync(path.join(wt.path, "a.txt"), "two\n");
  fs.writeFileSync(path.join(wt.path, "new.txt"), "fresh\n");
  fs.rmSync(path.join(wt.path, "gone.txt"));
  fs.mkdirSync(path.join(wt.path, ".bot"));
  fs.writeFileSync(path.join(wt.path, ".bot", "settings.json"), "{}");
  fs.appendFileSync(path.join(wt.path, ".gitignore"), ".bot/cache/\n.bot/sessions/\n.bot/history\n");

  const changes = await worktreeChanges(wt.path, wt.base);
  assert.deepEqual(
    changes.sort((a, b) => a.path.localeCompare(b.path)),
    [
      { status: "M", path: "a.txt" },
      { status: "D", path: "gone.txt" },
      { status: "A", path: "new.txt" },
    ],
    "crit's own .bot and .gitignore lines are not the agent's changes",
  );
  // The worktree's real index is untouched.
  assert.equal((await git(wt.path, ["diff", "--cached", "--name-only"])).stdout.trim(), "");

  const applied = await applyToProject(wt.path, wt.base, root);
  assert.equal(applied.length, 3);
  assert.equal(fs.readFileSync(path.join(root, "a.txt"), "utf8"), "two\n");
  assert.equal(fs.readFileSync(path.join(root, "new.txt"), "utf8"), "fresh\n");
  assert.ok(!fs.existsSync(path.join(root, "gone.txt")));

  await removeWorktree(root, wt);
  assert.ok(!fs.existsSync(wt.path));
  const branches = (await git(root, ["branch", "--list", "crit/*"])).stdout.trim();
  assert.equal(branches, "");
});

test("apply refuses changes that conflict, leaving the project as it was", async () => {
  const root = await repo();
  const wt = await addWorktree(root, "two");
  fs.writeFileSync(path.join(wt.path, "a.txt"), "agent\n");
  fs.writeFileSync(path.join(root, "a.txt"), "user\n");
  await assert.rejects(applyToProject(wt.path, wt.base, root));
  const text = fs.readFileSync(path.join(root, "a.txt"), "utf8");
  assert.ok(text === "user\n" || text.includes("<<<<<<<"), "either untouched or a 3-way merge with markers");
  await removeWorktree(root, wt);
});

test("addWorktree needs a commit and a free name", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "crit-git-"));
  await git(dir, ["init", "-q"]);
  await assert.rejects(addWorktree(dir, "x"), /no commits/);
  const root = await repo();
  const wt = await addWorktree(root, "dup");
  await assert.rejects(addWorktree(root, "dup"), /already exists/);
  await removeWorktree(root, wt);
});

test("withoutCritGitignore keeps real .gitignore edits", () => {
  const real = "diff --git a/.gitignore b/.gitignore\n--- a/.gitignore\n+++ b/.gitignore\n@@ -1 +1,2 @@\n x\n+dist/\n";
  assert.equal(withoutCritGitignore(real), real);
});
