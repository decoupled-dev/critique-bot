// Build a .vsix. With the bundled agent in bin/critique-bot/ (scripts/bundle-agent.mjs),
// the package is platform-specific and named for this OS and CPU, e.g.
// crit-win32-x64-0.1.0.vsix. Without it (or with --no-target), it is a universal
// package that uses `crit` from PATH.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const extension = resolve(here, "..");
const pkg = JSON.parse(readFileSync(join(extension, "package.json"), "utf8"));
const noTarget = process.argv.includes("--no-target");
const exe = join(extension, "bin", "critique-bot", process.platform === "win32" ? "critique-bot.exe" : "critique-bot");
const bundled = existsSync(exe);

// VS Code's target names: win32-x64, win32-arm64, linux-x64, linux-arm64, darwin-x64, darwin-arm64, ...
const target = `${process.platform}-${process.arch}`;

copyFileSync(join(extension, "..", "LICENSE"), join(extension, "LICENSE"));

const npx = process.platform === "win32" ? "npx.cmd" : "npx";
const args = ["vsce", "package", "--allow-missing-repository"];
if (bundled && !noTarget) {
  args.push("--target", target, "--out", `crit-${target}-${pkg.version}.vsix`);
  console.log(`packaging for ${target} with the bundled agent`);
} else {
  if (!noTarget) {
    console.log("no bundled agent in bin/critique-bot (run npm run bundle-agent); packaging a universal .vsix that uses crit from PATH");
  }
  args.push("--out", `crit-${pkg.version}.vsix`);
  if (bundled) {
    // A universal package must not carry one OS's binary.
    args.push("--ignoreFile", ".vscodeignore.universal");
  }
}
const run = spawnSync(npx, args, { cwd: extension, stdio: "inherit", shell: process.platform === "win32" });
process.exit(run.status ?? 1);
