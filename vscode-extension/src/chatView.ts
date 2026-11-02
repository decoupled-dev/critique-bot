/**
 * The crit sidebar: a chat webview wired to one agent process.
 *
 * The agent starts on the first task (it opens the browser, which should not
 * happen just because the sidebar was opened). Events go to the webview as-is;
 * diffs open in VS Code's own diff editor; commands show in the "crit" terminal.
 */
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { AgentEvent, AgentProcess, resolveAgent } from "./agent";
import { FileDiff, parseUnifiedDiff, reverseApply } from "./diff";
import { CommandMirror } from "./terminal";

export const BEFORE_SCHEME = "crit-before";

type FromWebview =
  | { type: "ready" }
  | { type: "prompt"; text: string }
  | { type: "answer"; id: string; value: string; reason?: string }
  | { type: "set_mode"; mode: string }
  | { type: "set_role"; role: string }
  | { type: "new_chat" }
  | { type: "undo" }
  | { type: "interrupt" }
  | { type: "restart" }
  | { type: "open_diff"; diff: number; file: number }
  | { type: "open_file"; path: string }
  | { type: "show_terminal" }
  | { type: "show_log" };

export class ChatController implements vscode.WebviewViewProvider, vscode.TextDocumentContentProvider, vscode.Disposable {
  static readonly viewType = "crit.chat";

  private view: vscode.WebviewView | undefined;
  private agent: AgentProcess | undefined;
  private queued: string[] = [];
  private busy = false;
  private mode = "ask";
  /** A mode picked before the agent started; otherwise .bot/settings.json decides. */
  private modeOverride: string | undefined;
  private readonly diffs: FileDiff[][] = [];
  private readonly before = new Map<string, string>();
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changed.event;
  private readonly status: vscode.StatusBarItem;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly log: vscode.OutputChannel,
    private readonly mirror: CommandMirror,
  ) {
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.status.command = "crit.setMode";
    this.status.tooltip = "crit permission mode (click to change)";
    this.updateStatus();
    this.status.show();
  }

  // ------------------------------------------------------------------ webview

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    this.disposables.push(
      view.webview.onDidReceiveMessage((message: FromWebview) => this.onWebview(message)),
      view.onDidDispose(() => {
        if (this.view === view) {
          this.view = undefined;
        }
      }),
    );
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      `img-src ${webview.cspSource} data:`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${style}">
<title>crit</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }

  private post(message: unknown): void {
    void this.view?.webview.postMessage(message);
  }

  private onWebview(message: FromWebview): void {
    switch (message.type) {
      case "ready":
        this.post({ kind: "state", running: !!this.agent?.running, busy: this.busy, hello: this.agent?.hello });
        break;
      case "prompt":
        this.prompt(message.text);
        break;
      case "answer":
        this.agent?.answer(message.id, message.value, message.reason);
        break;
      case "set_mode":
        this.setMode(message.mode);
        break;
      case "set_role":
        this.agent?.send({ type: "set_role", role: message.role });
        break;
      case "new_chat":
        this.newChat();
        break;
      case "undo":
        this.undo();
        break;
      case "interrupt":
        this.interrupt();
        break;
      case "restart":
        void this.restart();
        break;
      case "open_diff":
        void this.openDiff(message.diff, message.file);
        break;
      case "open_file":
        void this.openFile(message.path);
        break;
      case "show_terminal":
        this.mirror.show(false);
        break;
      case "show_log":
        this.log.show(true);
        break;
    }
  }

  // ------------------------------------------------------------------ commands

  prompt(text: string): void {
    const task = text.trim();
    if (!task) {
      return;
    }
    if (this.agent?.running) {
      this.agent.prompt(task);
      this.setBusy(true);
      return;
    }
    this.queued.push(task);
    this.start();
  }

  interrupt(): void {
    this.agent?.interrupt();
  }

  undo(): void {
    this.agent?.send({ type: "undo" });
  }

  newChat(): void {
    if (this.agent?.running) {
      this.agent.send({ type: "new_chat" });
    }
    this.post({ kind: "cleared" });
  }

  setMode(mode: string): void {
    this.mode = mode;
    this.updateStatus();
    if (!this.agent?.send({ type: "set_mode", mode })) {
      this.modeOverride = mode;
    }
  }

  async pickMode(): Promise<void> {
    const modes = (this.agent?.hello?.modes as { id: string; label: string }[] | undefined) || [
      { id: "ask", label: "ask before edits and commands" },
      { id: "edits", label: "accept edits on (commands still ask)" },
      { id: "auto", label: "auto mode on (asks only for risky commands)" },
      { id: "plan", label: "plan mode on (reads only, then a plan to approve)" },
    ];
    const picked = await vscode.window.showQuickPick(
      modes.map((m) => ({ label: m.id, description: m.label, picked: m.id === this.mode })),
      { placeHolder: `Permission mode (now: ${this.mode})` },
    );
    if (picked) {
      this.setMode(picked.label);
    }
  }

  async restart(): Promise<void> {
    await this.stop();
    this.post({ kind: "cleared" });
    this.start();
  }

  async stop(): Promise<void> {
    const agent = this.agent;
    if (agent) {
      await agent.stop();
    }
  }

  // ------------------------------------------------------------------ agent

  private workspaceFolder(): vscode.WorkspaceFolder | undefined {
    const active = vscode.window.activeTextEditor?.document.uri;
    const owner = active ? vscode.workspace.getWorkspaceFolder(active) : undefined;
    return owner || vscode.workspace.workspaceFolders?.[0];
  }

  private start(): void {
    const folder = this.workspaceFolder();
    if (!folder || folder.uri.scheme !== "file") {
      this.post({ kind: "event", event: { type: "error", message: "Open a folder first: crit works on a local project." } });
      this.queued = [];
      return;
    }
    const settings = vscode.workspace.getConfiguration("crit");
    // Only the user's own settings count: a repository's .vscode/settings.json must not choose
    // the agent binary, its arguments, its config (and so the chat address), or the mode.
    const user = <T>(key: string): T | undefined => settings.inspect<T>(key)?.globalValue;
    const command = resolveAgent(this.context.extensionPath, user<string>("agentPath"));
    const agent = new AgentProcess(command);
    this.agent = agent;
    const workspace = folder.uri.fsPath;
    this.log.appendLine(`starting ${command.command} ${command.args.join(" ")} (${command.source}) in ${workspace}`);
    agent.on("event", (event: AgentEvent) => this.onAgentEvent(agent, event));
    agent.on("stderr", (line: string) => this.log.appendLine(line));
    agent.on("exit", (code: number | null, signal: string | null, reason: string) => {
      if (this.agent !== agent) {
        return;
      }
      this.log.appendLine(`agent exited (code ${code}${signal ? `, ${signal}` : ""})${reason ? `: ${reason}` : ""}`);
      this.agent = undefined;
      this.setBusy(false);
      this.queued = [];
      this.post({ kind: "stopped", code, reason });
    });
    this.post({ kind: "starting", source: command.source });
    const configPath = (user<string>("configPath") || "").trim();
    try {
      agent.start({
        workspace,
        config: configPath ? path.resolve(workspace, configPath) : undefined,
        mode: this.modeOverride || (user<string>("permissionMode") || "").trim() || undefined,
        extraArgs: user<string[]>("extraArgs") || [],
      });
    } catch (err) {
      this.post({ kind: "event", event: { type: "error", message: String(err) } });
    }
  }

  private onAgentEvent(agent: AgentProcess, event: AgentEvent): void {
    if (this.agent !== agent) {
      return;
    }
    switch (event.type) {
      case "hello":
        if (typeof event.mode === "string") {
          this.mode = event.mode;
          this.updateStatus();
        }
        break;
      case "ready":
        this.setBusy(false);
        if (this.queued.length) {
          const next = this.queued.shift()!;
          agent.prompt(next);
          this.setBusy(true);
        }
        break;
      case "mode":
        if (typeof event.mode === "string") {
          this.mode = event.mode;
          this.updateStatus();
        }
        break;
      case "tool_start":
        if (event.name === "run_command" && this.mirrorOn()) {
          const args = (event.args || {}) as { command?: string };
          this.mirror.command(String(args.command || ""));
        }
        break;
      case "tool_output":
        if (this.mirrorOn()) {
          this.mirror.output(String(event.text || ""));
        }
        break;
      case "tool_done":
        if (event.name === "run_command" && this.mirrorOn()) {
          this.mirror.done(!!event.ok, String(event.friendly || event.error || ""));
        }
        break;
      case "diff": {
        const files = parseUnifiedDiff(String(event.diff || ""));
        this.diffs.push(files);
        const index = this.diffs.length - 1;
        this.post({
          kind: "event",
          event: { ...event, index, files: files.map((f) => ({ path: f.path, added: f.added, deleted: f.deleted, text: f.text })) },
        });
        if (vscode.workspace.getConfiguration("crit").get<boolean>("openDiffsAfterTask")) {
          files.forEach((_f, i) => void this.openDiff(index, i));
        }
        return;
      }
      case "approval":
      case "question":
      case "plan_review":
        if (!this.view?.visible) {
          void vscode.commands.executeCommand("crit.chat.focus");
        }
        break;
    }
    this.post({ kind: "event", event });
  }

  private mirrorOn(): boolean {
    return vscode.workspace.getConfiguration("crit").get<boolean>("mirrorCommandsInTerminal", true);
  }

  private setBusy(busy: boolean): void {
    this.busy = busy;
    void vscode.commands.executeCommand("setContext", "crit.busy", busy);
    this.post({ kind: "busy", busy });
  }

  private updateStatus(): void {
    this.status.text = `$(comment-discussion) crit: ${this.mode}`;
  }

  // ------------------------------------------------------------------ diffs and files

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.before.get(uri.toString()) ?? "";
  }

  private resolveInWorkspace(relative: string): vscode.Uri | undefined {
    const folder = this.workspaceFolder();
    if (!folder) {
      return undefined;
    }
    const full = path.resolve(folder.uri.fsPath, relative);
    const root = path.resolve(folder.uri.fsPath);
    if (full !== root && !full.startsWith(root + path.sep)) {
      return undefined;
    }
    return vscode.Uri.file(full);
  }

  async openDiff(diffIndex: number, fileIndex: number): Promise<void> {
    const file = this.diffs[diffIndex]?.[fileIndex];
    if (!file) {
      return;
    }
    const target = this.resolveInWorkspace(file.path);
    if (!target) {
      void vscode.window.showWarningMessage(`crit: ${file.path} is outside the workspace.`);
      return;
    }
    let after = "";
    try {
      after = file.deleted ? "" : await fs.promises.readFile(target.fsPath, "utf8");
    } catch {
      after = "";
    }
    let beforeText: string;
    try {
      beforeText = file.deleted ? reverseApply("", { ...file, added: false }) : reverseApply(after, file);
    } catch (err) {
      // The file changed again since: show the diff text itself.
      const doc = await vscode.workspace.openTextDocument({ content: file.text, language: "diff" });
      await vscode.window.showTextDocument(doc, { preview: true });
      void vscode.window.showInformationMessage(`crit: ${file.path} changed since the task, so the saved diff is shown. (${err})`);
      return;
    }
    const beforeUri = vscode.Uri.from({
      scheme: BEFORE_SCHEME,
      path: "/" + file.path.replace(/\\/g, "/"),
      query: `d=${diffIndex}&f=${fileIndex}`,
    });
    this.before.set(beforeUri.toString(), beforeText);
    this.changed.fire(beforeUri);
    const right = file.deleted
      ? vscode.Uri.from({ scheme: BEFORE_SCHEME, path: beforeUri.path, query: "deleted" })
      : target;
    await vscode.commands.executeCommand("vscode.diff", beforeUri, right, `${file.path} (before crit ↔ now)`, {
      preview: true,
    });
  }

  private async openFile(relative: string): Promise<void> {
    const target = this.resolveInWorkspace(relative);
    if (!target) {
      return;
    }
    try {
      await vscode.window.showTextDocument(target, { preview: true });
    } catch {
      void vscode.window.showWarningMessage(`crit: cannot open ${relative}.`);
    }
  }

  dispose(): void {
    void this.stop();
    this.status.dispose();
    this.changed.dispose();
    for (const item of this.disposables) {
      item.dispose();
    }
  }
}
