/** Pure presentation and message routing for the bundled learning IDE. */
export const LEARNING_HOME_COMMAND = "autograde.openLearningHome";
export const AUTOGRADE_IDE_APP_NAME = "Autograde IDE";

export interface LearningHomeState {
  readonly authenticated: boolean;
  readonly trusted: boolean;
  readonly hasWorkspace: boolean;
  readonly busy?: boolean;
}

const actions = {
  configure: { command: "autograde.configureServiceAddress", label: "서버 주소 설정", session: false, trust: false, workspace: false },
  connection: { command: "autograde.checkServerConnection", label: "서버 연결 확인", session: false, trust: false, workspace: false },
  claim: { command: "autograde.redeemAssignmentClaim", label: "수령 코드로 연결 · 과제 받기", session: false, trust: true, workspace: false },
  open: { command: "autograde.openAssignment", label: "기존 과제 이어서 열기", session: true, trust: true, workspace: false },
  accepted: { command: "autograde.acceptedAssignmentHistory", label: "수락 이력 · 과제 전환", session: true, trust: false, workspace: false },
  submit: { command: "autograde.submitCurrentCommit", label: "현재 과제 제출", session: true, trust: true, workspace: true },
  result: { command: "autograde.viewLatestResult", label: "현재 제출 결과", session: true, trust: false, workspace: false },
  history: { command: "autograde.submissionHistory", label: "제출 이력 · 코드 복원", session: true, trust: false, workspace: false },
} as const;
export type LearningHomeAction = keyof typeof actions;

export function learningHomeAction(message: unknown): LearningHomeAction | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message)) return undefined;
  const value = message as Record<string, unknown>;
  // No command IDs, arguments, URLs or authentication material are accepted from the webview.
  if (Object.keys(value).length !== 2 || value.type !== "learningAction" || typeof value.action !== "string") return undefined;
  return Object.prototype.hasOwnProperty.call(actions, value.action) ? value.action as LearningHomeAction : undefined;
}

export function learningHomeCommand(action: LearningHomeAction): string { return actions[action].command; }

export function learningHomeBlockedReason(action: LearningHomeAction, state: LearningHomeState): string | undefined {
  if (state.busy) return "이전 작업을 완료하거나 취소한 뒤 다시 선택하세요.";
  const item = actions[action];
  if (item.trust && !state.trusted) return "파일을 열거나 제출하려면 신뢰할 수 있는 과제 폴더를 열고 작업 영역을 신뢰하세요.";
  if (item.session && !state.authenticated) return "학생 웹에서 새 수령 코드를 받아 먼저 연결하세요. 로그인 정보는 저장되지 않습니다.";
  if (item.workspace && !state.hasWorkspace) return "기존 과제 이어서 열기 또는 과제 다운로드로 제출할 작업 영역을 먼저 여세요.";
  return undefined;
}

export function shouldOpenLearningHome(appName: string, folderCount: number, hasWorkspaceFile: boolean, openTabCount: number): boolean {
  return (appName === AUTOGRADE_IDE_APP_NAME || appName === `${AUTOGRADE_IDE_APP_NAME} Dev`)
    && folderCount === 0 && !hasWorkspaceFile && openTabCount === 0;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]!));
}

export function renderLearningHome(state: LearningHomeState, nonce: string): string {
  const button = (action: LearningHomeAction, primary = false): string => {
    const reason = learningHomeBlockedReason(action, state);
    return `<button type="button" data-action="${action}"${primary ? ' class="primary"' : ""}${reason ? ` disabled title="${escapeHtml(reason)}"` : ""}>${actions[action].label}</button>`;
  };
  const status = state.authenticated ? "수령 코드 연결됨" : "수령 코드 연결 필요";
  const safeNonce = escapeHtml(nonce);
  return `<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${safeNonce}'; script-src 'nonce-${safeNonce}'; base-uri 'none'; form-action 'none';">
<title>Autograde 학습 홈</title><style nonce="${safeNonce}">
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); background: var(--vscode-editor-background); line-height: 1.6; }
main { max-width: 880px; margin: auto; }
h1 { font-size: 26px; font-weight: 600; margin: 0 0 6px; }
h2 { font-size: 17px; margin: 0 0 8px; }
p { margin: 0 0 12px; overflow-wrap: anywhere; }
.lead, .detail { color: var(--vscode-descriptionForeground); }
.status { border-block: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); padding: 12px 0; margin: 20px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 24px; }
section { min-width: 0; }
.actions { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
button { border: 1px solid var(--vscode-button-border, var(--vscode-contrastBorder, transparent)); padding: 9px 12px; font: inherit; line-height: 1.4; text-align: left; overflow-wrap: anywhere; cursor: pointer; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); }
button:hover:not(:disabled) { background: var(--vscode-button-secondaryHoverBackground); }
button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
button.primary:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); }
button:focus-visible { outline: 2px solid var(--vscode-focusBorder); outline-offset: 2px; }
button:disabled { cursor: default; opacity: .55; }
aside { margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); }
@media (max-width: 420px) { body { padding: 16px; } h1 { font-size: 22px; } }
</style></head><body><main>
<header><h1>Autograde 학습 홈</h1><p class="lead">과제를 받고, 기존 코드를 이어서 작성하고, 제출 결과를 확인하세요.</p></header>
<p class="status" role="status" aria-live="polite">${status} · ${state.hasWorkspace ? "작업 영역 열림" : "작업 영역 없음"}${state.trusted ? "" : " · 제한 모드"}${state.busy ? " · 작업 진행 중" : ""}</p>
<div class="grid">
<section aria-labelledby="connect"><h2 id="connect">1. 수업 연결</h2><p>교수자가 안내한 채점 서버 주소를 설정하세요. 학생 웹에서 받은 수령 코드를 입력합니다.</p><div class="actions">${button("configure")}${button("connection")}${button("claim", true)}</div></section>
<section aria-labelledby="resume"><h2 id="resume">2. 실습 이어서 하기</h2><p>IDE를 다시 열었다면 새 수령 코드로 연결한 뒤 기존 폴더를 여세요. 기존 코드를 다시 다운로드할 필요는 없습니다.</p><div class="actions">${button("open")}${button("accepted")}</div></section>
<section aria-labelledby="submit"><h2 id="submit">3. 제출과 피드백</h2><p>제출할 과제 폴더를 확인하세요. 제출 접수와 채점 완료는 별도이며, 현재 제출의 결과와 이전 이력을 확인할 수 있습니다.</p><div class="actions">${button("submit", true)}${button("result")}${button("history")}</div></section>
</div>
<aside><p>로그인 정보는 저장하지 않습니다. 창을 다시 열면 새 수령 코드로 연결하세요. 연결 상태는 아래 상태 표시줄에서 확인할 수 있습니다.</p>${!state.trusted ? '<p>제한 모드에서는 과제 파일 열기·다운로드·제출이 비활성화됩니다. 신뢰할 수 있는 폴더만 신뢰하세요.</p>' : ""}<p class="detail">개념연구 1단계: Autograde 학습 기능을 통합한 화면입니다. 시험 통제 기능이 아니며, 생성형 AI나 외부 프로그램·네트워크 사용을 차단하지 않습니다.</p></aside>
</main><script nonce="${safeNonce}">
const vscode = acquireVsCodeApi();
document.querySelectorAll('button[data-action]').forEach(button => {
  button.addEventListener('click', () => {
    if (button.disabled) return;
    vscode.postMessage({ type: 'learningAction', action: button.dataset.action });
  });
});
</script></body></html>`;
}
