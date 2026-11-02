// Freeze the Python agent with PyInstaller and put it in bin/critique-bot/,
// where the extension looks for it. Run on each OS you ship for.
//
//   PYTHON=/path/to/python node scripts/bundle-agent.mjs
//
// PYTHON must have critique-bot's dependencies and pyinstaller installed
// (pip install -c ../constraints.txt -e .. pyinstaller).
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const extension = resolve(here, "..");
const repo = resolve(extension, "..");
const spec = join(repo, "packaging", "critique-bot.spec");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");

const work = mkdtempSync(join(tmpdir(), "crit-bundle-"));
const dist = join(work, "dist");
console.log(`freezing the agent with ${python} (${spec})`);
const run = spawnSync(
  python,
  ["-m", "PyInstaller", spec, "--noconfirm", "--clean", "--distpath", dist, "--workpath", join(work, "build")],
  { cwd: repo, stdio: "inherit" },
);
if (run.status !== 0) {
  console.error(run.error ? String(run.error) : `PyInstaller failed (exit ${run.status})`);
  process.exit(1);
}
const built = join(dist, "critique-bot");
const exe = join(built, process.platform === "win32" ? "critique-bot.exe" : "critique-bot");
if (!existsSync(exe)) {
  console.error(`PyInstaller did not produce ${exe}`);
  process.exit(1);
}

// The frozen agent must speak the same protocol as this extension.
const check = spawnSync(exe, ["--mode", "agent", "--help"], { encoding: "utf8" });
if (!String(check.stdout).includes("--protocol")) {
  console.error("the frozen agent has no --protocol flag; is the repo up to date?");
  process.exit(1);
}

const target = join(extension, "bin", "critique-bot");
rmSync(target, { recursive: true, force: true });
cpSync(built, target, { recursive: true });
rmSync(work, { recursive: true, force: true });
console.log(`bundled agent: ${target}`);
