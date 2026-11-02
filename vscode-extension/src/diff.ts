/**
 * Unified diffs from the agent: split per file, and rebuild a file's text
 * from before the task (the agent only reports the diff; the file on disk is
 * the "after" side) so VS Code can show its own side-by-side diff.
 */

export interface Hunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: string[];
}

export interface FileDiff {
  /** Path relative to the workspace ("" when the header has none). */
  path: string;
  oldPath: string;
  added: boolean;
  deleted: boolean;
  hunks: Hunk[];
  text: string;
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

function stripPrefix(name: string): string {
  const clean = name.replace(/\t.*$/, "").trim();
  if (clean === "/dev/null") {
    return clean;
  }
  return clean.replace(/^[ab]\//, "");
}

export function parseUnifiedDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  const lines = diff.replace(/\r\n/g, "\n").split("\n");
  let file: FileDiff | undefined;
  let hunk: Hunk | undefined;
  let body: string[] = [];
  const close = () => {
    if (file) {
      file.text = body.join("\n");
      files.push(file);
    }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("--- ") && i + 1 < lines.length && lines[i + 1].startsWith("+++ ")) {
      close();
      const oldPath = stripPrefix(line.slice(4));
      const newPath = stripPrefix(lines[i + 1].slice(4));
      file = {
        path: newPath === "/dev/null" ? oldPath : newPath,
        oldPath,
        added: oldPath === "/dev/null",
        deleted: newPath === "/dev/null",
        hunks: [],
        text: "",
      };
      body = [line, lines[i + 1]];
      hunk = undefined;
      i++;
      continue;
    }
    if (!file) {
      continue;
    }
    body.push(line);
    const match = HUNK.exec(line);
    if (match) {
      hunk = {
        oldStart: Number(match[1]),
        oldLines: match[2] === undefined ? 1 : Number(match[2]),
        newStart: Number(match[3]),
        newLines: match[4] === undefined ? 1 : Number(match[4]),
        lines: [],
      };
      file.hunks.push(hunk);
      continue;
    }
    if (hunk && (line.startsWith(" ") || line.startsWith("+") || line.startsWith("-") || line === "")) {
      hunk.lines.push(line);
    } else if (hunk && line.startsWith("\\")) {
      // "\ No newline at end of file"
    }
  }
  close();
  return files;
}

/**
 * The text before the change, from the text after it. Throws when a hunk's
 * new side does not match `after` (the file changed again since).
 */
export function reverseApply(after: string, file: FileDiff): string {
  if (file.added) {
    return "";
  }
  const eol = after.includes("\r\n") ? "\r\n" : "\n";
  // split("\n") keeps a trailing "" for a final newline. The agent diffs files split the same
  // way, and a git-style diff (which counts no such line) still matches: the "" is left as-is.
  const source = after.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let cursor = 0;
  for (const hunk of file.hunks) {
    const newSide: string[] = [];
    const oldSide: string[] = [];
    for (const line of hunk.lines) {
      const tag = line === "" ? " " : line[0];
      const text = line.slice(1);
      if (tag === " ") {
        newSide.push(text);
        oldSide.push(text);
      } else if (tag === "+") {
        newSide.push(text);
      } else {
        oldSide.push(text);
      }
    }
    // A blank line at the very end of the diff text is the split, not a context line.
    if (newSide.length > hunk.newLines && newSide[newSide.length - 1] === "" && oldSide[oldSide.length - 1] === "") {
      newSide.pop();
      oldSide.pop();
    }
    const start = hunk.newLines === 0 ? hunk.newStart : hunk.newStart - 1;
    if (start < cursor) {
      throw new Error("the diff's hunks overlap");
    }
    for (let k = 0; k < newSide.length; k++) {
      if (source[start + k] !== newSide[k]) {
        throw new Error(`the file no longer matches the diff near line ${start + k + 1}`);
      }
    }
    out.push(...source.slice(cursor, start), ...oldSide);
    cursor = start + newSide.length;
  }
  out.push(...source.slice(cursor));
  return out.join(eol);
}

/** Lines of a diff tagged for display: add, del, hunk, meta, or ctx. */
export function classifyLine(line: string): "add" | "del" | "hunk" | "meta" | "ctx" {
  if (line.startsWith("+++ ") || line.startsWith("--- ")) {
    return "meta";
  }
  if (line.startsWith("@@")) {
    return "hunk";
  }
  if (line.startsWith("+")) {
    return "add";
  }
  if (line.startsWith("-")) {
    return "del";
  }
  return "ctx";
}
