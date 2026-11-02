import assert from "node:assert/strict";
import { test } from "node:test";
import { parseUnifiedDiff, reverseApply } from "../diff";

test("the agent's own diff (split on newlines) rebuilds the old text", () => {
  // As agent_edit.hunk_diff makes it: the trailing "" after a final newline is a context line.
  const diff = "--- a/note.txt\n+++ b/note.txt\n@@ -1,2 +1,2 @@\n-one\n+two\n ";
  const [file] = parseUnifiedDiff(diff);
  assert.equal(file.path, "note.txt");
  assert.equal(reverseApply("two\n", file), "one\n");
});

test("a git-style diff with several hunks and files", () => {
  const before = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"].join("\n") + "\n";
  const after = ["a", "B", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"].join("\n") + "\n";
  const diff = [
    "--- a/x.txt",
    "+++ b/x.txt",
    "@@ -1,3 +1,3 @@",
    " a",
    "-b",
    "+B",
    " c",
    "@@ -10,3 +10,4 @@",
    " j",
    " k",
    " l",
    "+m",
    "--- a/y.txt",
    "+++ b/y.txt",
    "@@ -1 +1 @@",
    "-old",
    "+new",
  ].join("\n");
  const files = parseUnifiedDiff(diff);
  assert.deepEqual(
    files.map((f) => f.path),
    ["x.txt", "y.txt"],
  );
  assert.equal(reverseApply(after, files[0]), before);
  assert.equal(reverseApply("new\n", files[1]), "old\n");
});

test("added and deleted files", () => {
  const diff = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n--- a/gone.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-bye\n-now";
  const [added, deleted] = parseUnifiedDiff(diff);
  assert.equal(added.added, true);
  assert.equal(added.path, "new.txt");
  assert.equal(reverseApply("hi\n", added), "");
  assert.equal(deleted.deleted, true);
  assert.equal(deleted.path, "gone.txt");
  // Git counts lines, so the deleted file ended with a newline (no "\\ No newline" marker).
  assert.equal(reverseApply("", { ...deleted, added: false }), "bye\nnow\n");
});

test("CRLF files keep their line endings", () => {
  const [file] = parseUnifiedDiff("--- a/w.txt\n+++ b/w.txt\n@@ -1,2 +1,2 @@\n x\n-y\n+z");
  assert.equal(reverseApply("x\r\nz\r\n", file), "x\r\ny\r\n");
});

test("a file that changed again is reported, not silently mangled", () => {
  const [file] = parseUnifiedDiff("--- a/n.txt\n+++ b/n.txt\n@@ -1 +1 @@\n-one\n+two");
  assert.throws(() => reverseApply("three\n", file), /no longer matches/);
});
