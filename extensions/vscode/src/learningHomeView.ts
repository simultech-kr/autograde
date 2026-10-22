import { randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { learningHomeAction, learningHomeBlockedReason, learningHomeCommand, renderLearningHome, type LearningHomeState } from "./learningHome";

/** The home delegates to existing commands; it never holds tokens or calls the server. */
export class LearningHomeView implements vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private listeners: vscode.Disposable[] = [];
  private busy = false;
  private disposed = false;
  private nonce = "";

  public constructor(private readonly currentState: () => LearningHomeState) {}

  public show(): void {
    if (this.disposed) return;
    if (this.panel) { this.panel.reveal(); return; }
    const panel = vscode.window.createWebviewPanel("autograde.learningHome", "Autograde 학습 홈", vscode.ViewColumn.One, {
      enableScripts: true, enableCommandUris: false, localResourceRoots: [], retainContextWhenHidden: false,
    });
    this.panel = panel;
    this.nonce = randomUUID();
    this.listeners.push(panel.webview.onDidReceiveMessage(message => this.handleMessage(message)));
    this.listeners.push(panel.onDidDispose(() => {
      if (this.panel === panel) this.panel = undefined;
      this.clearListeners();
    }));
    this.refresh();
  }

  public refresh(): void {
    if (this.panel) this.panel.webview.html = renderLearningHome({ ...this.currentState(), busy: this.busy }, this.nonce);
  }

  private async handleMessage(message: unknown): Promise<void> {
    if (this.disposed || !this.panel) return;
    const action = learningHomeAction(message);
    if (!action) return;
    const reason = learningHomeBlockedReason(action, { ...this.currentState(), busy: this.busy });
    if (reason) { void vscode.window.showInformationMessage(reason); return; }
    this.busy = true;
    this.refresh();
    try {
      // Fixed command with no webview-provided arguments; auth, trust and target checks stay in that command.
      await vscode.commands.executeCommand(learningHomeCommand(action));
    } catch {
      void vscode.window.showErrorMessage("작업을 완료하지 못했습니다. Autograde 연결 상태를 확인하고 다시 시도하세요.");
    } finally {
      this.busy = false;
      this.refresh();
    }
  }

  private clearListeners(): void { for (const listener of this.listeners.splice(0)) listener.dispose(); }

  public dispose(): void {
    this.disposed = true;
    const panel = this.panel;
    this.panel = undefined;
    this.clearListeners();
    panel?.dispose();
  }
}
