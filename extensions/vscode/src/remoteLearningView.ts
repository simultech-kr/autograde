import { randomUUID } from "node:crypto";
import * as vscode from "vscode";
import type { LearningCheckpointScope, SavedLearningSnapshot } from "./learningCheckpoint";
import { RemoteLearningController, MAX_RUNTIME_STDIN, validateRuntimeRun, type LearningRuntimeApi, type RuntimeLanguage } from "./remoteLearning";
import { defaultRuntimeSources, renderRemoteLearning, runtimePhaseLabel, type RuntimeInputs } from "./remoteLearningMarkup";

export class RemoteLearningView {
  readonly controller: RemoteLearningController;
  private panel?: vscode.WebviewPanel;
  private listeners: vscode.Disposable[] = [];
  private nonce = "";
  private disposed = false;
  private inputs: RuntimeInputs = {language: "cpp", sources: [], stdin: ""};
  constructor(api: LearningRuntimeApi, private readonly options: {
    currentScope(): LearningCheckpointScope | undefined;
    saveSnapshot(scope: LearningCheckpointScope): Promise<SavedLearningSnapshot>;
    onStatus(status: string): void;
  }) {
    this.controller = new RemoteLearningController(api, {...options, currentScope: () => this.scope(), onState: state => {
      const files = state.snapshot?.filePaths ?? [];
      const retained = this.inputs.sources.filter(file => files.includes(file));
      this.inputs = {...this.inputs, sources: retained.length ? retained : defaultRuntimeSources(files, this.inputs.language)};
      this.refresh(); options.onStatus(runtimePhaseLabel(state));
    }});
  }
  private scope(): LearningCheckpointScope | undefined { return !this.disposed && vscode.workspace.isTrusted ? this.options.currentScope() : undefined; }
  reconcile(): void {
    const old = this.controller.getState(); this.controller.reconcile();
    if (old !== this.controller.getState() && this.controller.getState().phase === "idle") this.inputs = {language:"cpp",sources:[],stdin:""};
    this.refresh();
  }
  markEdited(): void { this.controller.markEdited(); }
  async show(): Promise<void> {
    if (this.disposed) return;
    this.reconcile();
    if (this.panel) this.panel.reveal();
    else {
      const panel = vscode.window.createWebviewPanel("autograde.remoteLearning", "Autograde 원격 실습", vscode.ViewColumn.One,
        {enableScripts: true, enableCommandUris: false, localResourceRoots: [], retainContextWhenHidden: false});
      this.panel = panel; this.nonce = randomUUID();
      this.listeners.push(panel.webview.onDidReceiveMessage(value => this.handle(value)));
      this.listeners.push(panel.onDidDispose(() => { if (this.panel === panel) this.panel = undefined; this.clearListeners(); }));
    }
    this.refresh(); if (this.scope()) await this.controller.refresh();
  }
  private refresh(): void {
    if (this.panel && !this.disposed) this.panel.webview.html = renderRemoteLearning(this.controller.getState(), this.inputs,
      this.controller.isBusy, Boolean(this.scope()), this.nonce);
  }
  private async handle(value: unknown): Promise<void> {
    if (this.disposed || !this.panel || !value || typeof value !== "object" || Array.isArray(value) || this.controller.isBusy) return;
    const message = value as Record<string, unknown>;
    if (Object.keys(message).sort().join() !== "action,input,type" || message.type !== "remoteLearning"
      || !["prepare","refresh","run","close","language","input"].includes(String(message.action))) return;
    const input = message.input as Record<string, unknown> | undefined;
    if (!input || typeof input !== "object" || Array.isArray(input) || Object.keys(input).sort().join() !== "language,sources,stdin"
      || input.language !== "c" && input.language !== "cpp" && input.language !== "python" || !Array.isArray(input.sources) || input.sources.length > 32
      || input.sources.some(source => typeof source !== "string" || source.length > 1024)
      || typeof input.stdin !== "string" || Buffer.byteLength(input.stdin) > MAX_RUNTIME_STDIN) return;
    this.inputs = {language: input.language as RuntimeLanguage, sources: input.sources, stdin: input.stdin};
    if (message.action === "input") return;
    if (message.action === "language") {
      this.inputs = {...this.inputs, sources: defaultRuntimeSources(this.controller.getState().snapshot?.filePaths ?? [], this.inputs.language)};
      this.refresh(); return;
    }
    if (!this.scope()) { this.reconcile(); return; }
    if (message.action === "prepare") await this.controller.prepare();
    if (message.action === "refresh") await this.controller.refresh();
    if (message.action === "close") await this.controller.close();
    if (message.action === "run") {
      try { await this.controller.run(validateRuntimeRun(this.inputs)); }
      catch { void vscode.window.showInformationMessage("실행할 소스 파일과 언어, 표준입력을 확인하세요."); }
    }
    this.refresh();
  }
  async finishBeforeExit(): Promise<boolean> {
    this.reconcile();
    if (!this.controller.needsClose) return true;
    return this.controller.close();
  }
  private clearListeners(): void { for (const listener of this.listeners.splice(0)) listener.dispose(); }
  dispose(): void {
    this.disposed = true; this.controller.dispose(); this.clearListeners(); this.panel?.dispose(); this.panel = undefined;
    this.inputs = {language:"cpp",sources:[],stdin:""};
  }
}
