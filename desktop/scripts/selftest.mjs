// Run test/selftest-main.ts inside the real app against the scripted fake model.
//   CRIT_TEST_PYTHON=/path/to/python node scripts/selftest.mjs [screenshot-dir]
// The Python needs critique-bot's dependencies (the repo's src/ is put on PYTHONPATH).
// On Linux without a display it runs under xvfb-run.
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const app = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(app, "..");
const python = process.env.CRIT_TEST_PYTHON;
if (!python) {
  console.error("set CRIT_TEST_PYTHON to a Python with critique-bot's dependencies");
  process.exit(2);
}
const shots = process.argv[2] ? resolve(process.argv[2]) : "";
if (shots) mkdirSync(shots, { recursive: true });

const work = mkdtempSync(join(tmpdir(), "crit-desktop-"));
const project = join(work, "proj");
mkdirSync(project);
const port = 15000 + Math.floor(Math.random() * 2000);
const fake = spawn(python, [join(repoRoot, "vscode-extension", "test", "fake_model.py"), String(port)], { stdio: "ignore" });

const g = (...args) => execFileSync("git", args, { cwd: project, stdio: "ignore" });
g("init", "-q", "-b", "main");
g("config", "user.email", "test@example.invalid");
g("config", "user.name", "test");
writeFileSync(join(project, "note.txt"), "one\n");
writeFileSync(join(project, ".gitignore"), "config.json\n");
g("add", "-A");
g("commit", "-q", "-m", "init");
writeFileSync(
  join(project, "config.json"),
  JSON.stringify({ backend: "local", url: `http://127.0.0.1:${port}`, model: "fake", local: { api: "ollama" } }),
);

const electron = join(app, "node_modules", "electron", "dist", process.platform === "win32" ? "electron.exe" : "electron");
const env = {
  ...process.env,
  CRIT_DESKTOP_SELFTEST: "1",
  CRIT_SELFTEST_REPO: project,
  CRIT_SELFTEST_SHOTS: shots,
  CRIT_COMMAND: JSON.stringify([python, "-m", "critique_bot"]),
  PYTHONPATH: join(repoRoot, "src"),
};
delete env.WAYLAND_DISPLAY;
const args = [app, "--no-sandbox", `--user-data-dir=${join(work, "user")}`, "--ozone-platform=x11", ...(process.env.CRIT_SELFTEST_DARK ? ["--force-dark-mode"] : [])];
const headless = process.platform === "linux" && (!process.env.DISPLAY || process.env.CRIT_TEST_XVFB !== "0");
const [cmd, cmdArgs] = headless ? ["xvfb-run", ["-a", "-s", "-screen 0 1440x900x24", electron, ...args]] : [electron, args];

const child = spawn(cmd, cmdArgs, { env, stdio: ["ignore", "pipe", "pipe"] });
let out = "";
child.stdout.on("data", (d) => {
  out += d;
  process.stdout.write(d);
});
child.stderr.on("data", (d) => process.stderr.write(d));
const timer = setTimeout(() => child.kill(), 240000);
child.on("close", (code) => {
  clearTimeout(timer);
  fake.kill();
  rmSync(work, { recursive: true, force: true });
  const ok = code === 0 && out.includes("CRIT_DESKTOP_SELFTEST_OK");
  console.log(ok ? "selftest passed" : `selftest failed (exit ${code})`);
  process.exit(ok ? 0 : 1);
});
