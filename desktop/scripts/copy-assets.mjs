// Copy the shared chat view (from the VS Code extension) and xterm into renderer/vendor/.
import { cpSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const app = resolve(here, "..");
const vendor = join(app, "renderer", "vendor");
mkdirSync(vendor, { recursive: true });
const copies = [
  [join(app, "..", "vscode-extension", "media", "chat.js"), "chat.js"],
  [join(app, "..", "vscode-extension", "media", "chat.css"), "chat.css"],
  [join(app, "node_modules", "@xterm", "xterm", "lib", "xterm.js"), "xterm.js"],
  [join(app, "node_modules", "@xterm", "xterm", "css", "xterm.css"), "xterm.css"],
];
for (const [from, name] of copies) {
  cpSync(from, join(vendor, name));
}
