/**
 * Git worktrees, so each agent can work on its own copy of the project
 * (branch crit/<name>) without overwriting another agent's edits.
 */
import { execFile } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export class GitError extends Error {}

export interface GitResult {
  stdout: string;
  stderr: string;
}

export function git(cwd: string, args: string[], env?: NodeJS.ProcessEnv, input?: string): Promise<GitResult> {
  return new Promise((resolve, reject) => {
    const child = execFile(
      "git",
      args,
      { cwd, env: { ...process.env, ...env }, maxBuffer: 64 * 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        if (err) {
          const message = String(stderr || err.message).trim();
          reject(new GitError(`git ${args[0]}: ${message}`));
          return;
        }
        resolve({ stdout: String(stdout), stderr: String(stderr) });
      },
    );
    if (input !== undefined) {
      child.stdin?.end(input);
    }
  });
}

/** The repository's top folder, or undefined when `folder` is not in a git repository. */
export async function repoRoot(folder: string): Promise<string | undefined> {
  try {
    const { stdout } = await git(folder, ["rev-parse", "--show-toplevel"]);
    return path.resolve(stdout.trim());
  } catch {
    return undefined;
  }
}

export async function hasCommits(repo: string): Promise<boolean> {
  try {
    await git(repo, ["rev-parse", "--verify", "HEAD"]);
    return true;
  } catch {
    return false;
  }
}

/** Agent names become branch and folder names. */
export function safeName(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, "-")
      .replace(/^[-.]+|[-.]+$/g, "")
      .slice(0, 40) || "agent"
  );
}

/** Where worktrees for `repo` go: a sibling folder, <repo>.crit/<name>. */
export function worktreePath(repo: string, name: string): string {
  return path.join(path.dirname(repo), `${path.basename(repo)}.crit`, safeName(name));
}

export interface Worktree {
  path: string;
  branch: string;
  base: string;
}

/** A new worktree on branch crit/<name>, starting from the current commit. */
export async function addWorktree(repo: string, name: string): Promise<Worktree> {
  if (!(await hasCommits(repo))) {
    throw new GitError("the repository has no commits yet, so there is nothing to copy; commit once or use the project folder");
  }
  const base = (await git(repo, ["rev-parse", "HEAD"])).stdout.trim();
  const branch = `crit/${safeName(name)}`;
  const target = worktreePath(repo, name);
  if (fs.existsSync(target)) {
    throw new GitError(`${target} already exists; pick another name`);
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  await git(repo, ["worktree", "add", "-b", branch, target, base]);
  return { path: target, branch, base };
}

/**
 * Everything that differs in the worktree from `base`: tracked edits plus new
 * files, as a binary patch. Uses a scratch index so the worktree's own index
 * is untouched. `.bot/` (crit's own state) is left out.
 */
export async function worktreePatch(worktree: string, base: string): Promise<string> {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "crit-index-"));
  const index = path.join(scratch, "index");
  try {
    const env = { GIT_INDEX_FILE: index };
    await git(worktree, ["read-tree", base], env);
    await git(worktree, ["add", "-A", "--", ".", ":(exclude).bot"], env);
    const { stdout } = await git(worktree, ["diff", "--cached", "--binary", base, "--", ".", ":(exclude).bot"], env);
    return withoutCritGitignore(stdout);
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

/** Lines crit itself appends to .gitignore when it sets up a folder (bot_home._GITIGNORE_LINES). */
const CRIT_IGNORE_LINES = new Set([".bot/cache/", ".bot/sessions/", ".bot/history"]);

/** Drop a .gitignore change that only adds crit's own lines: setup, not the agent's work. */
export function withoutCritGitignore(patch: string): string {
  const parts = patch.split(/^(?=diff --git )/m);
  return parts
    .filter((part) => {
      if (!part.startsWith("diff --git a/.gitignore b/.gitignore")) return true;
      const body = part.split("\n").filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---) /.test(l));
      return !body.every((l) => l.startsWith("+") && CRIT_IGNORE_LINES.has(l.slice(1).trim()));
    })
    .join("");
}

/** Files that differ in the worktree from `base`, with git's status letter. */
export async function worktreeChanges(worktree: string, base: string): Promise<{ status: string; path: string }[]> {
  const patch = await worktreePatch(worktree, base);
  const files: { status: string; path: string }[] = [];
  let current: { status: string; path: string } | undefined;
  for (const line of patch.split("\n")) {
    const header = /^diff --git a\/(.+) b\/(.+)$/.exec(line);
    if (header) {
      current = { status: "M", path: header[2] };
      files.push(current);
    } else if (current && line.startsWith("new file mode")) {
      current.status = "A";
    } else if (current && line.startsWith("deleted file mode")) {
      current.status = "D";
    }
  }
  return files;
}

/**
 * Apply the worktree's changes to the project folder (not committed there).
 * Fails without touching anything if they do not apply cleanly.
 */
export async function applyToProject(worktree: string, base: string, project: string): Promise<string[]> {
  const patch = await worktreePatch(worktree, base);
  if (!patch.trim()) {
    return [];
  }
  try {
    await git(project, ["apply", "--check", "--binary", "-"], undefined, patch);
  } catch {
    // Fall back to a three-way merge against the base commit, which leaves conflict markers to resolve.
    await git(project, ["apply", "--3way", "--binary", "-"], undefined, patch);
    return (await worktreeChanges(worktree, base)).map((f) => f.path);
  }
  await git(project, ["apply", "--binary", "-"], undefined, patch);
  return (await worktreeChanges(worktree, base)).map((f) => f.path);
}

/** Remove the worktree folder and its crit/<name> branch. */
export async function removeWorktree(repo: string, worktree: Worktree): Promise<void> {
  await git(repo, ["worktree", "remove", "--force", worktree.path]);
  try {
    await git(repo, ["branch", "-D", worktree.branch]);
  } catch {
    /* already gone, or checked out elsewhere */
  }
}
