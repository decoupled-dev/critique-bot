import * as vscode from "vscode";
import { BEFORE_SCHEME, ChatController } from "./chatView";
import { enterFocusLayout, exitFocusLayout, syncContext } from "./layout";
import { CommandMirror } from "./terminal";

let controller: ChatController | undefined;

/** Returns the chat controller, which the in-editor test (test/vscode/) drives. */
export function activate(context: vscode.ExtensionContext): { controller: ChatController } {
  const log = vscode.window.createOutputChannel("crit");
  const mirror = new CommandMirror();
  controller = new ChatController(context, log, mirror);
  const chat = controller;
  syncContext(context);

  context.subscriptions.push(
    log,
    mirror,
    chat,
    vscode.window.registerWebviewViewProvider(ChatController.viewType, chat, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.workspace.registerTextDocumentContentProvider(BEFORE_SCHEME, chat),
    vscode.commands.registerCommand("crit.newChat", () => chat.newChat()),
    vscode.commands.registerCommand("crit.undo", () => chat.undo()),
    vscode.commands.registerCommand("crit.interrupt", () => chat.interrupt()),
    vscode.commands.registerCommand("crit.restart", () => chat.restart()),
    vscode.commands.registerCommand("crit.setMode", () => chat.pickMode()),
    vscode.commands.registerCommand("crit.showTerminal", () => mirror.show(false)),
    vscode.commands.registerCommand("crit.showLog", () => log.show(true)),
    vscode.commands.registerCommand("crit.focusLayout", () => enterFocusLayout(context, () => mirror.show(true), log)),
    vscode.commands.registerCommand("crit.exitFocusLayout", () => exitFocusLayout(context, log)),
    vscode.commands.registerCommand("crit.askAboutSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        return;
      }
      const relative = vscode.workspace.asRelativePath(editor.document.uri, false);
      const sel = editor.selection;
      const where = sel.isEmpty ? relative : `${relative}:${sel.start.line + 1}-${sel.end.line + 1}`;
      const question = await vscode.window.showInputBox({ prompt: `Ask crit about ${where}` });
      if (question) {
        await vscode.commands.executeCommand("crit.chat.focus");
        chat.prompt(`${question}\n\nReferenced files:\n- ${where}`);
      }
    }),
  );
  return { controller: chat };
}

export async function deactivate(): Promise<void> {
  await controller?.stop();
}
