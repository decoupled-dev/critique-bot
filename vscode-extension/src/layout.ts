import * as vscode from "vscode";

/**
 * "Focus layout": files on the left, editor and diffs in the middle, crit chat
 * on the right, the crit terminal below; menus, status bar, activity bar and
 * other chrome hidden. The settings go to the current profile (user level),
 * never to the project's .vscode/settings.json; the previous values are kept
 * and put back by exitFocusLayout.
 */
const FOCUS_SETTINGS: ReadonlyArray<[string, unknown]> = [
  ["workbench.activityBar.location", "hidden"],
  ["workbench.statusBar.visible", false],
  ["window.menuBarVisibility", "toggle"],
  ["window.commandCenter", false],
  ["workbench.layoutControl.enabled", false],
  ["workbench.navigationControl.enabled", false],
  ["editor.minimap.enabled", false],
  ["breadcrumbs.enabled", false],
  ["workbench.startupEditor", "none"],
  ["workbench.tips.enabled", false],
];

const SAVED_KEY = "crit.focusLayout.saved";
const CONTEXT_KEY = "crit.focusLayout";
/** The chat's view container (package.json: viewsContainers.secondarySidebar). */
export const CHAT_CONTAINER_COMMAND = "workbench.view.extension.crit";

type Saved = Record<string, { value: unknown }>;

export function isFocused(context: vscode.ExtensionContext): boolean {
  return context.globalState.get<Saved>(SAVED_KEY) !== undefined;
}

export function syncContext(context: vscode.ExtensionContext): void {
  void vscode.commands.executeCommand("setContext", CONTEXT_KEY, isFocused(context));
}

export async function enterFocusLayout(
  context: vscode.ExtensionContext,
  showTerminal: () => void,
  log: vscode.OutputChannel,
): Promise<void> {
  // Keep the values from before the first enter, so entering twice still restores the original.
  const saved: Saved = context.globalState.get<Saved>(SAVED_KEY) ?? {};
  for (const [key, value] of FOCUS_SETTINGS) {
    const section = key.slice(0, key.lastIndexOf("."));
    const name = key.slice(key.lastIndexOf(".") + 1);
    const config = vscode.workspace.getConfiguration(section);
    if (!(key in saved)) {
      saved[key] = { value: config.inspect(name)?.globalValue };
    }
    try {
      await config.update(name, value, vscode.ConfigurationTarget.Global);
    } catch (err) {
      // Older editors (or VS Code forks) may not know a setting; skip it.
      delete saved[key];
      log.appendLine(`focus layout: skipped ${key}: ${String(err)}`);
    }
  }
  await context.globalState.update(SAVED_KEY, saved);
  syncContext(context);
  await arrange(showTerminal);
}

export async function exitFocusLayout(context: vscode.ExtensionContext, log: vscode.OutputChannel): Promise<void> {
  const saved = context.globalState.get<Saved>(SAVED_KEY);
  if (!saved) {
    return;
  }
  for (const [key, { value }] of Object.entries(saved)) {
    const section = key.slice(0, key.lastIndexOf("."));
    const name = key.slice(key.lastIndexOf(".") + 1);
    try {
      await vscode.workspace.getConfiguration(section).update(name, value, vscode.ConfigurationTarget.Global);
    } catch (err) {
      log.appendLine(`focus layout: could not restore ${key}: ${String(err)}`);
    }
  }
  await context.globalState.update(SAVED_KEY, undefined);
  syncContext(context);
}

/** Open the panes: explorer left, chat right, crit terminal below. */
async function arrange(showTerminal: () => void): Promise<void> {
  const run = async (command: string) => {
    try {
      await vscode.commands.executeCommand(command);
    } catch {
      /* not available in this editor */
    }
  };
  await run("workbench.view.explorer");
  if (vscode.workspace.getConfiguration("crit").get<boolean>("mirrorCommandsInTerminal", true)) {
    showTerminal();
  }
  await run(CHAT_CONTAINER_COMMAND);
  await run("crit.chat.focus");
}
