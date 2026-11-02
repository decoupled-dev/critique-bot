// Freeze the Python agent into bin/critique-bot/ (shipped in the app's resources).
//   PYTHON=/path/to/python node scripts/bundle-agent.mjs
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const app = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const run = spawnSync(process.execPath, [join(app, "..", "vscode-extension", "scripts", "bundle-agent.mjs")], {
  stdio: "inherit",
  env: { ...process.env, BUNDLE_TARGET: app },
});
process.exit(run.status ?? 1);
