import { randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { learningHomeAction, learningHomeBlockedReason, learningHomeCommand, learningHomeDiscoverySelection, learningHomeEditMessage, learningHomeFormMessage,
  renderLearningHome, type LearningHomeForm, type LearningHomeState } from "./learningHome";

export interface LearningHomeCallbacks {
  claim(code: string, expectedServer: string): Promise<void>;
  configureServer(address: string): Promise<void>;
  configurePortal(address: string): Promise<void>;
  discoverServers?(): Promise<void>;
  selectDiscoveredServer?(id: string): Promise<void>;
}

/** Input stays in the visible document until sent directly to a controller, never in commands or storage. */
export class LearningHomeView implements vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private listeners: vscode.Disposable[] = [];
  private busy = false;
  private disposed = false;
  private nonce = "";
  private dirty = false;
  private deferredRefresh = false;
  private scope = "";
  private initialForm?: LearningHomeForm;
  private feedback?: string;
  private inputGeneration = 0;

  public constructor(private readonly currentState: () => LearningHomeState, private readonly callbacks?: LearningHomeCallbacks) {}

  public show(form?: LearningHomeForm): void {
    if (this.disposed) return;
    if (this.panel) {
      this.panel.reveal();
      if (this.scope && this.scope !== this.stateScope(this.currentState())) this.clearInputState();
      if (form && this.dirty) { this.focusForm(form); return; }
      this.initialForm = form;
      this.refresh();
      return;
    }
    const panel = vscode.window.createWebviewPanel("autograde.learningHome", "Autograde 학습 홈", vscode.ViewColumn.One, {
      enableScripts: true, enableCommandUris: false, localResourceRoots: [], retainContextWhenHidden: false,
    });
    this.panel = panel;
    this.nonce = randomUUID();
    this.initialForm = form;
    this.listeners.push(panel.webview.onDidReceiveMessage(message => this.handleMessage(message)));
    this.listeners.push(panel.onDidChangeViewState(event => {
      if (this.panel !== panel) return;
      // A hidden webview must never retain a previous student's draft code or server form.
      if (!event.webviewPanel.visible) { this.clearInputState(); panel.webview.html = ""; }
      else if (!this.scope) this.refresh();
    }));
    this.listeners.push(panel.onDidDispose(() => {
      if (this.panel !== panel) return;
      this.panel = undefined;
      this.clearInputState();
      this.clearListeners();
    }));
    this.refresh();
  }

  private stateScope(state: LearningHomeState): string {
    return JSON.stringify([state.authenticated, state.trusted, state.sessionGeneration, state.serviceBaseUrl,
      state.studentPortalBaseUrl, state.sharedSession, state.sessionNeedsRestart, state.assignment?.id]);
  }

  public refresh(): void {
    if (!this.panel || !this.panel.visible) return;
    const state = { ...this.currentState(), busy: this.busy };
    const scope = this.stateScope(state);
    if (this.scope && this.scope !== scope) this.clearInputState();
    if (this.dirty) { this.deferredRefresh = true; return; }
    this.scope = scope;
    this.deferredRefresh = false;
    this.nonce = randomUUID();
    this.panel.webview.html = renderLearningHome(state, this.nonce, {initialForm: this.initialForm, feedback: this.feedback});
    this.initialForm = undefined;
  }

  private focusForm(form: LearningHomeForm): void {
    if (!this.panel?.visible) { this.initialForm = form; return; }
    void this.panel.webview.postMessage({type: "learningFocus", form, nonce: this.nonce});
  }

  private showFeedback(text: string): void {
    this.feedback = text;
    if (this.panel?.visible) void this.panel.webview.postMessage({type: "learningFeedback", text, nonce: this.nonce});
  }

  private async handleMessage(message: unknown): Promise<void> {
    if (this.disposed || !this.panel?.visible) return;
    // Invalidate messages from a document whose auth, trust or destination changed before a timer refresh.
    if (this.scope !== this.stateScope(this.currentState())) { this.refresh(); return; }
    const editing = learningHomeEditMessage(message, this.nonce);
    if (editing !== undefined) {
      this.dirty = editing;
      if (!editing && this.deferredRefresh) this.refresh();
      return;
    }
    const selectedServer = learningHomeDiscoverySelection(message, this.nonce);
    if (selectedServer) {
      const state = this.currentState();
      const reason = learningHomeBlockedReason("configure", {...state, busy: this.busy})
        ?? (state.discovery?.status === "searching" ? "검색이 끝난 뒤 서버를 선택하세요." : undefined);
      if (reason) { this.showFeedback(reason); return; }
      if (this.dirty) { this.showFeedback("입력 중인 수령 코드나 주소를 먼저 적용하거나 지운 뒤 서버를 선택하세요."); return; }
      // Re-check the current registry, even if an older rendered list contained the ID.
      if (state.discovery?.servers.slice(0, 32).filter(server => server.id === selectedServer).length !== 1) {
        this.showFeedback("이 서버는 현재 검색 결과에 없습니다. 주변 서버를 다시 검색하세요."); return;
      }
      if (!this.callbacks?.selectDiscoveredServer) { this.showFeedback("서버 자동 검색을 사용하려면 Autograde 확장을 업데이트하세요."); return; }
      await this.runDiscoveryOperation(() => this.callbacks!.selectDiscoveredServer!(selectedServer), false);
      return;
    }
    const form = learningHomeFormMessage(message, this.nonce);
    if (form) {
      const reason = learningHomeBlockedReason(form.form === "claim" ? "claim" : "configure", {...this.currentState(), busy: this.busy});
      if (reason) { this.showFeedback(reason); return; }
      if (!this.callbacks) { this.showFeedback("이 입력 기능을 사용하려면 Autograde 확장을 업데이트하세요."); return; }
      if (form.form === "claim" && form.expectedServer !== (this.currentState().serviceBaseUrl ?? "")) {
        this.showFeedback("채점 서버 주소가 변경되었습니다. 주소를 확인하고 새 수령 코드를 입력하세요."); return;
      }
      this.dirty = false;
      this.feedback = undefined;
      this.busy = true;
      this.refresh();
      const operationGeneration = this.inputGeneration;
      try {
        const operation = form.form === "claim" ? this.callbacks.claim(form.value, form.expectedServer!)
          : form.form === "server" ? this.callbacks.configureServer(form.value) : this.callbacks.configurePortal(form.value);
        form.value = "";
        message = undefined;
        await operation;
        // A resolved callback may mean cancellation in an older host. Only current state conveys success.
      } catch (error) {
        if (this.inputGeneration === operationGeneration) {
          this.feedback = error instanceof Error && error.name === "OperationCancelledError"
            ? "취소했습니다. 다시 입력해 주세요."
            : form.form === "claim" ? "연결하지 못했습니다. 서버 주소와 수령 코드의 유효기간을 확인하고 다시 입력하세요."
              : "주소를 적용하지 못했습니다. 교수자가 안내한 주소를 확인하고 다시 입력하세요.";
        }
      } finally {
        // Remove references promptly; no secret-bearing error or submitted value is rendered or recorded.
        form.value = "";
        this.busy = false;
        this.refresh();
      }
      return;
    }
    const action = learningHomeAction(message, this.nonce);
    if (!action) return;
    const reason = learningHomeBlockedReason(action, { ...this.currentState(), busy: this.busy });
    if (reason) { void vscode.window.showInformationMessage(reason); return; }
    if (action === "discoverServer") {
      if (this.dirty) { this.showFeedback("입력 중인 수령 코드나 주소를 먼저 적용하거나 지운 뒤 주변 서버를 검색하세요."); return; }
      if (!this.callbacks?.discoverServers) { this.showFeedback("서버 자동 검색을 사용하려면 Autograde 확장을 업데이트하세요."); return; }
      await this.runDiscoveryOperation(() => this.callbacks!.discoverServers!(), true);
      return;
    }
    if (action === "claim" || action === "configure" || action === "portal" && !this.currentState().studentPortalBaseUrl) {
      this.focusForm(action === "claim" ? "claim" : action === "configure" ? "server" : "portal");
      return;
    }
    this.busy = true;
    this.refresh();
    try {
      await vscode.commands.executeCommand(learningHomeCommand(action));
    } catch {
      void vscode.window.showErrorMessage("작업을 완료하지 못했습니다. Autograde 연결 상태를 확인하고 다시 시도하세요.");
    } finally {
      this.busy = false;
      this.refresh();
    }
  }

  private async runDiscoveryOperation(operation: () => Promise<void>, search: boolean): Promise<void> {
    this.feedback = undefined;
    this.busy = true;
    this.refresh();
    const generation = this.inputGeneration;
    try { await operation(); }
    catch (error) {
      if (this.inputGeneration === generation) this.feedback = error instanceof Error && error.name === "OperationCancelledError"
        ? "취소했습니다. 현재 서버 설정을 확인하세요."
        : search ? "주변 서버를 검색하지 못했습니다. 다시 검색하거나 교수자가 안내한 주소를 직접 입력하세요."
          : "서버를 선택하지 못했습니다. 주변 서버를 다시 검색하고 접속 주소를 확인하세요.";
    } finally {
      this.busy = false;
      this.refresh();
    }
  }

  private clearInputState(): void {
    this.nonce = randomUUID();
    this.inputGeneration++;
    this.dirty = false;
    this.deferredRefresh = false;
    this.scope = "";
    this.feedback = undefined;
    this.initialForm = undefined;
  }

  private clearListeners(): void { for (const listener of this.listeners.splice(0)) listener.dispose(); }

  public dispose(): void {
    this.disposed = true;
    const panel = this.panel;
    this.panel = undefined;
    this.clearInputState();
    this.clearListeners();
    if (panel) { panel.webview.html = ""; panel.dispose(); }
  }
}
