import { randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { EXAM_MODE_COMMAND, ExamClient, ExamError, ExamSession, examAgentLabel, normalizeExamOrigin, type ExamScope, type ExamViewState } from "./examMode";

type ExamAction = "connect" | "refresh" | "disconnect" | "configure";
export function examAction(message: unknown): ExamAction | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message)) return undefined;
  const value = message as Record<string, unknown>;
  if (Object.keys(value).length !== 2 || value.type !== "examAction") return undefined;
  return ["connect", "refresh", "disconnect", "configure"].includes(String(value.action)) ? value.action as ExamAction : undefined;
}
function escape(value: string): string { return value.replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]!)); }
function time(value?: number | null, seconds = true): string { return value ? new Date(value * (seconds ? 1000 : 1)).toLocaleString() : "확인되지 않음"; }
export function renderExamMode(state: ExamViewState, nonce: string, eligible: boolean, busy: boolean, scope?: ExamScope): string {
  const status = state.status; const safeNonce = escape(nonce);
  const examStatus = status?.exam.status === "active" ? "진행 중" : status?.exam.status === "ended" ? "종료" : status?.exam.status;
  const row = (label: string, value: string): string => `<dt>${label}</dt><dd>${escape(value)}</dd>`;
  const connected = !!status;
  return `<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${safeNonce}'; script-src 'nonce-${safeNonce}'; base-uri 'none'; form-action 'none';">
<title>Autograde 시험 모드</title><style nonce="${safeNonce}">
:root {color-scheme: light dark;} * {box-sizing:border-box;} body {font:var(--vscode-font-size) var(--vscode-font-family); color:var(--vscode-foreground); background:var(--vscode-editor-background); padding:20px; line-height:1.6; margin:0;}
main {max-width:700px;margin:auto;} h1 {font-size:1.5em;} p,dd {overflow-wrap:anywhere;} .status {padding:12px;border:1px solid var(--vscode-contrastBorder,var(--vscode-widget-border,#888));} .muted {color:var(--vscode-descriptionForeground);} dl {display:grid;grid-template-columns:minmax(80px,1fr) minmax(0,3fr);gap:8px;} dt {font-weight:600;} dd {margin:0;min-width:0;}
.actions {display:flex;flex-wrap:wrap;gap:8px;margin:18px 0;} button {font:inherit;white-space:normal;overflow-wrap:anywhere;max-width:100%;padding:8px 12px;cursor:pointer;color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground);border:1px solid var(--vscode-button-border,var(--vscode-contrastBorder,transparent));}
button.primary {color:var(--vscode-button-foreground);background:var(--vscode-button-background);} button:hover:not(:disabled) {filter:brightness(.93);} button:focus-visible {outline:2px solid var(--vscode-focusBorder);outline-offset:2px;} button:disabled {opacity:.55;cursor:default;} details {border-top:1px solid var(--vscode-widget-border,#888);padding-top:12px;} @media(max-width:420px) {body {padding:12px;} dl {grid-template-columns:1fr;} dd {margin-bottom:6px;}}
</style></head><body><main><h1>시험 모드</h1>
<p>감독자가 확인한 좌석과 별도 Agent의 상태를 확인합니다. 이 화면은 네트워크를 직접 차단하거나 해제하지 않습니다.</p>
${scope ? `<p class="muted">감독자 확인용 · 채점 서버: ${escape(scope.autogradeOrigin)} · 현재 과제 ID: ${escape(scope.assignmentId)}</p>` : ""}
<p class="status" role="status" aria-live="polite">${escape(examAgentLabel(state))}${busy ? " · 처리 중" : ""}</p>
${state.message ? `<p role="alert">${escape(state.message)}</p>` : ""}
${status ? `<dl>${row("시험", `${status.exam.title} (${examStatus})`)}${row("좌석", status.seat_id)}${row("과제", status.assignment_id)}${row("관리 서버", state.origin ?? "")}${row("Agent", `${status.agent.backend ?? (status.agent.mode === "simulation" ? "모의" : "미확인")} · ${status.agent.state}`)}${row("제한 기한", time(status.agent.lease_expires_at))}${row("남은 제한 시간", state.fresh && state.leaseRemainingSeconds !== undefined ? `${state.leaseRemainingSeconds}초 (Agent 보고 기준)` : "현재 상태 확인 필요")}${row("마지막 Agent 보고", time(status.agent.last_seen))}${row("마지막 조회", time(state.checkedAt, false))}${row("화면 연결 만료", time(status.expires_at))}</dl>` : `<p>${eligible ? "감독자에게 8자리 시험 연결 코드를 받은 뒤 연결하세요. 과제 수령 코드·학생 비밀번호와 다릅니다." : "먼저 Autograde에 로그인하고 수락한 과제를 선택하세요."}</p>`}
<div class="actions"><button class="primary" data-action="${connected ? "refresh" : "connect"}"${busy || !eligible ? " disabled" : ""}>${connected ? "상태 다시 확인" : "시험 연결"}</button>${connected ? `<button data-action="disconnect"${busy ? " disabled" : ""}>화면 연결 종료</button>` : ""}</div>
<p class="muted">화면 연결 종료·로그아웃·IDE 종료는 시험 종료나 통신 제한 해제가 아닙니다. 해제·복구는 감독자에게 요청하세요. 연결 오류일 때도 제한이 유지될 수 있습니다. 제출은 기존 과제 제출 기능을 사용하며 자동 최종 제출은 하지 않습니다.</p>
<details><summary>연결 설정 · 개인정보</summary><p>관리 서버 주소는 사용자 설정에만 저장합니다. 시험 코드는 저장하지 않으며 별도 시험 토큰은 현재 창의 메모리에만 유지합니다. Autograde 로그인 토큰은 관리 서버로 보내지 않습니다.</p><p>감독자의 수동 좌석 확인 방식입니다. 학생 신원·이 PC의 소유를 자동 증명하지 않습니다. 화면 공유 기능은 포함하지 않습니다.</p><button data-action="configure"${busy ? " disabled" : ""}>시험 관리 서버 주소 설정</button></details>
</main><script nonce="${safeNonce}">
const vscode=acquireVsCodeApi();
document.addEventListener('click',event=>{const button=event.target.closest('button[data-action]');if(button&&!button.disabled)vscode.postMessage({type:'examAction',action:button.dataset.action});});
window.addEventListener('message',event=>{const data=event.data;if(!data||data.type!=='examRender'||typeof data.html!=='string')return;const expanded=document.querySelector('details')?.open;const action=document.activeElement?.dataset.action;document.querySelector('main').innerHTML=data.html;document.querySelector('details').open=!!expanded;if(action)document.querySelectorAll('button[data-action]').forEach(button=>{if(button.dataset.action===action)button.focus();});});
</script></body></html>`;
}

/** UI runs in the existing workspace extension host, including WSL. No local privileged IPC. */
export class ExamModeView implements vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private readonly session: ExamSession;
  private readonly bar: vscode.StatusBarItem;
  private readonly clock: ReturnType<typeof setInterval>;
  private nonce = "";
  private rendered = "";
  private busy = false;
  private revision = 0;
  private disposed = false;
  private listeners: vscode.Disposable[] = [];
  public constructor(private readonly currentScope: () => ExamScope | undefined) {
    this.bar = vscode.window.createStatusBarItem("autograde.examMode", vscode.StatusBarAlignment.Left, 98);
    this.bar.command = EXAM_MODE_COMMAND;
    this.session = new ExamSession(currentScope, () => this.refresh());
    this.clock = setInterval(() => this.refresh(), 1000); this.clock.unref?.();
  }
  /** Deliberately ignore workspace/workspaceFolder values even in trusted repositories. */
  private configuredOrigin(): string {
    return vscode.workspace.getConfiguration("autograde").inspect<string>("examControlBaseUrl")?.globalValue ?? "";
  }
  public show(): void {
    if (this.disposed) return;
    if (this.panel) { this.panel.reveal(); this.refresh(); return; }
    const panel = vscode.window.createWebviewPanel("autograde.examMode", "Autograde 시험 모드", vscode.ViewColumn.One,
      {enableScripts: true, enableCommandUris: false, localResourceRoots: [], retainContextWhenHidden: false});
    this.panel = panel; this.nonce = randomUUID(); this.rendered = "";
    this.listeners.push(panel.webview.onDidReceiveMessage(message => this.handle(message)));
    this.listeners.push(panel.onDidDispose(() => {this.panel = undefined; for (const listener of this.listeners.splice(0)) listener.dispose();}));
    this.refresh();
  }
  private refresh(): void {
    if (this.disposed) return;
    const state = this.session.snapshot();
    if (this.panel) {
      const scope = this.currentScope();
      const html = renderExamMode(state, this.nonce, !!scope, this.busy, scope);
      if (html !== this.rendered) {
        if (!this.rendered) this.panel.webview.html = html;
        else void this.panel.webview.postMessage({type: "examRender", html: html.slice(html.indexOf("<main>") + 6, html.indexOf("</main>"))});
        this.rendered = html;
      }
    }
    if (state.phase === "disconnected" && !state.message) { this.bar.hide(); return; }
    this.bar.text = `$(shield) ${state.fresh ? "시험 모드" : "시험 상태 확인 필요"}`;
    this.bar.tooltip = `${examAgentLabel(state)}\n클릭하여 시험 모드를 확인하세요. 화면 연결 종료는 제한 해제가 아닙니다.`;
    this.bar.show();
  }
  public clear(): void { this.revision++; this.session.clear(); }
  public reconcile(): void { this.session.reconcile(); this.refresh(); }
  private async configure(): Promise<string | undefined> {
    const input = await vscode.window.showInputBox({title: "시험 관리 서버 주소", prompt: "감독자가 안내한 HTTPS 주소와 포트만 입력하세요. Autograde 채점 서버와 별개입니다.",
      value: this.configuredOrigin(), ignoreFocusOut: true, validateInput: value => {try {normalizeExamOrigin(value); return undefined;} catch (error) {return (error as Error).message;}}});
    if (input === undefined || this.disposed) return undefined;
    const origin = normalizeExamOrigin(input);
    this.clear();
    await vscode.workspace.getConfiguration("autograde").update("examControlBaseUrl", origin, vscode.ConfigurationTarget.Global);
    return origin;
  }
  private async handle(message: unknown): Promise<void> {
    const action = examAction(message);
    if (!action || this.busy || this.disposed || !this.panel) return;
    this.busy = true; this.refresh();
    try {
      if (action === "configure") { await this.configure(); return; }
      if (action === "disconnect") { this.clear(); return; }
      if (action === "refresh") { await this.session.refresh(); return; }
      const initialScope = this.currentScope();
      if (!initialScope) throw new ExamError("Autograde에 로그인하고 현재 수락한 과제를 먼저 선택하세요.");
      let origin = this.configuredOrigin();
      if (!origin) origin = await this.configure() ?? "";
      if (!origin) return;
      origin = normalizeExamOrigin(origin);
      const revision = this.revision;
      const code = await vscode.window.showInputBox({title: "시험 연결", prompt: `관리 서버: ${origin} · 과제: ${initialScope.assignmentId} · 감독자가 발급한 8자리 숫자 코드`,
        password: true, ignoreFocusOut: true, validateInput: value => /^\d{8}$/.test(value) ? undefined : "8자리 숫자만 입력하세요. 과제 수령 코드와 다릅니다."});
      if (code === undefined || this.disposed || revision !== this.revision) return;
      const current = this.currentScope();
      if (!current || current.generation !== initialScope.generation || current.assignmentId !== initialScope.assignmentId || current.autogradeOrigin !== initialScope.autogradeOrigin
        || normalizeExamOrigin(this.configuredOrigin()) !== origin) throw new ExamError("로그인·과제·주소가 변경되었습니다. 시험 연결을 다시 시작하세요.");
      await this.session.connect(new ExamClient(origin), code);
    } catch (error) {
      if (!this.disposed) void vscode.window.showErrorMessage(error instanceof ExamError ? error.message : "시험 연결을 완료하지 못했습니다. 주소와 상태를 다시 확인하세요.");
    } finally { this.busy = false; this.refresh(); }
  }
  public dispose(): void {
    this.disposed = true; this.revision++; clearInterval(this.clock); this.session.dispose(); this.bar.dispose();
    for (const listener of this.listeners.splice(0)) listener.dispose(); this.panel?.dispose(); this.panel = undefined;
  }
}
