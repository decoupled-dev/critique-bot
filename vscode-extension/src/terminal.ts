/**
 * A read-only "crit" terminal that mirrors the commands the agent runs and
 * their live output. The agent runs commands in its own shell session; this
 * is a view of it, so typing here does nothing.
 */
import * as vscode from "vscode";

export class CommandMirror implements vscode.Disposable {
  private terminal: vscode.Terminal | undefined;
  private readonly write = new vscode.EventEmitter<string>();
  private opened = false;
  private pending: string[] = [];

  show(preserveFocus = true): void {
    this.ensure().show(preserveFocus);
  }

  command(command: string): void {
    this.out(`\r\n\x1b[1;36m$ ${crlf(command)}\x1b[0m\r\n`);
  }

  output(text: string): void {
    this.out(crlf(text));
  }

  done(ok: boolean, error: string): void {
    if (ok) {
      this.out("\x1b[32m✓ done\x1b[0m\r\n");
    } else {
      this.out(`\x1b[31m✗ ${crlf(error || "failed")}\x1b[0m\r\n`);
    }
  }

  dispose(): void {
    this.terminal?.dispose();
    this.write.dispose();
  }

  private out(text: string): void {
    this.ensure();
    if (this.opened) {
      this.write.fire(text);
    } else {
      this.pending.push(text);
    }
  }

  private ensure(): vscode.Terminal {
    if (this.terminal && this.terminal.exitStatus === undefined) {
      return this.terminal;
    }
    this.opened = false;
    const pty: vscode.Pseudoterminal = {
      onDidWrite: this.write.event,
      open: () => {
        this.opened = true;
        this.write.fire("\x1b[2mCommands crit runs show here. This terminal is read-only.\x1b[0m\r\n");
        for (const text of this.pending.splice(0)) {
          this.write.fire(text);
        }
      },
      close: () => {
        this.terminal = undefined;
        this.opened = false;
      },
    };
    this.terminal = vscode.window.createTerminal({ name: "crit", pty, isTransient: true });
    return this.terminal;
  }
}

function crlf(text: string): string {
  return text.replace(/\r?\n/g, "\r\n");
}
