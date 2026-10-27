/** Pure presentation and message routing for the bundled learning IDE. */
import { learningPolicyLabel, type LearningPolicy } from "./learningEnvironment";
import type { ClassroomSharingStatus } from "./classroomActivity";
export const LEARNING_HOME_COMMAND = "autograde.openLearningHome";
export const AUTOGRADE_IDE_APP_NAME = "Autograde IDE";

export interface LearningHomeState {
  readonly authenticated: boolean;
  readonly trusted: boolean;
  readonly hasWorkspace: boolean;
  readonly busy?: boolean;
  readonly learningPolicy?: LearningPolicy;
  readonly classroomStatus?: ClassroomSharingStatus;
  readonly classroomCommandStatus?: string;
  readonly checkpointStatus?: string;
  readonly sharedSession?: boolean;
  readonly sessionNeedsRestart?: boolean;
}

const actions = {
  downloads: { command: "autograde.openServerDownloads", label: "서버 과제·자료 다운로드", session: false, trust: false, workspace: false },
  configure: { command: "autograde.configureServiceAddress", label: "서버 주소 설정", session: false, trust: false, workspace: false },
  connection: { command: "autograde.checkServerConnection", label: "서버 연결 확인", session: false, trust: false, workspace: false },
  claim: { command: "autograde.redeemAssignmentClaim", label: "수령 코드로 연결 · 과제 받기", session: false, trust: true, workspace: false },
  open: { command: "autograde.openAssignment", label: "기존 과제 이어서 열기", session: true, trust: true, workspace: false },
  accepted: { command: "autograde.acceptedAssignmentHistory", label: "수락 이력 · 과제 전환", session: true, trust: false, workspace: false },
  materials: { command: "autograde.studentMaterials", label: "내 배포 자료 · PDF / ZIP 받기", session: true, trust: false, workspace: false },
  content: { command: "autograde.syncContent", label: "강의자료 업데이트 · 변경분 받기 / 내 폴더에 저장", session: true, trust: false, workspace: false },
  extensions: { command: "autograde.manageCourseExtensions", label: "수업 확장 설치·업데이트 확인", session: true, trust: true, workspace: true },
  submit: { command: "autograde.submitCurrentCommit", label: "현재 과제 제출", session: true, trust: true, workspace: true },
  result: { command: "autograde.viewLatestResult", label: "현재 제출 결과", session: true, trust: false, workspace: false },
  history: { command: "autograde.submissionHistory", label: "제출 이력 · 코드 복원", session: true, trust: false, workspace: false },
  checkpointSave: { command: "autograde.saveLearningCheckpoint", label: "지금 작업 서버 저장 · 공식 제출 아님", session: true, trust: true, workspace: true },
  checkpointRestore: { command: "autograde.restoreLearningCheckpoint", label: "서버 작업 저장본 · 새 폴더로 복원", session: true, trust: true, workspace: true },
  exportLearning: { command: "autograde.exportLearning", label: "내 학습내용 내보내기 · 폴더 / ZIP", session: false, trust: true, workspace: false },
  endSession: { command: "autograde.endLearningSession", label: "공용 실습 종료 · 임시 파일 정리", session: false, trust: false, workspace: false },
  exam: { command: "autograde.openExamMode", label: "시험 모드 · 좌석 / Agent 상태", session: false, trust: false, workspace: false },
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
  if (action === "endSession") return state.sharedSession ? undefined : "공용 실습 실행기로 시작한 세션에서 사용할 수 있습니다.";
  if (state.busy) return "이전 작업을 완료하거나 취소한 뒤 다시 선택하세요.";
  const item = actions[action];
  if (state.sharedSession && ((state.authenticated || state.sessionNeedsRestart) && ["claim", "configure"].includes(action)
    || state.sessionNeedsRestart && item.session)) {
    return "필요한 코드를 내보낸 뒤 공용 실습을 종료하고, 새 세션에서 수령 코드로 연결하세요.";
  }
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
  const sharing = { inactive: "대기 · 연결된 과제 폴더 없음", sharing: "공유 중", retrying: "연결 지연 · 재시도 대기", unavailable: "공유 중단 · 서버 지원 또는 인증 확인 필요" }[state.classroomStatus ?? "inactive"];
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
<section aria-labelledby="resume"><h2 id="resume">2. 실습과 배포 자료</h2><p>${state.sharedSession ? "이번 실습의 새 폴더에 과제를 받습니다. 이전 작업은 서버 작업 저장본 또는 제출 이력에서 새 폴더로 복원할 수 있습니다. 코드와 자료를 보관하려면 내 학습내용 내보내기를 사용하세요." : "IDE를 다시 열었다면 새 수령 코드로 연결한 뒤 기존 폴더를 여세요. 강의자료는 학생별 캐시에 변경분만 받고, 필요할 때 별도 폴더에 저장합니다. 과제 코드는 덮어쓰지 않습니다."}</p><div class="actions">${button("downloads", true)}${button("open")}${button("checkpointRestore")}${button("accepted")}${button("extensions")}</div></section>
<section aria-labelledby="submit"><h2 id="submit">3. 제출과 피드백</h2><p>제출할 과제 폴더를 확인하세요. 제출 접수와 채점 완료는 별도이며, 현재 제출의 결과와 이전 이력을 확인할 수 있습니다.</p><div class="actions">${button("submit", true)}${button("result")}${button("history")}${button("exportLearning")}</div></section>
</div>
<aside aria-label="작업 코드 자동 저장"><h2>작업 코드 자동 저장 · 공식 제출과 별도</h2><p role="status" aria-live="polite">${escapeHtml(state.checkpointStatus ?? "대기 · 연결된 과제 폴더 없음")}</p><p>현재 인증으로 연결한 과제 폴더의 편집 내용을 저장하고, 변경된 코드를 30초마다 서버에 보관합니다. 다른 과제·강의자료·다른 프로그램 활동은 이 기능으로 보내지 않습니다. 서버의 접수 응답을 받아야 저장 완료로 표시합니다. 공용 실습에서 새로 받은 템플릿은 수정하거나 지금 저장을 눌렀을 때 보관합니다.</p><p>자동 저장은 공식 제출이나 채점을 시작하지 않습니다. 연결이 끊기거나 파일을 저장할 수 없으면 최신 코드가 서버에 없을 수 있습니다. 최신 상태를 확인하고 필요하면 내보내기를 사용하세요.</p>${button("checkpointSave")}</aside>
${state.sharedSession ? `<aside aria-label="공용 실습 공간"><h2>공용 PC · 이번 실습만 보관</h2><p>Autograde IDE를 완전히 종료하면 이번 실행의 코드·자료·IDE 임시 기록을 정리합니다. 서버 저장이 확인된 작업본·공식 제출본과 별도로 내보낸 폴더·ZIP은 보존합니다. 다음 실행 시 남은 파일도 정리합니다. 서버 저장을 확인하지 못한 코드는 먼저 내보내세요. Mac에서는 창 닫기만으로 앱이 종료되지 않을 수 있습니다.</p>${state.sessionNeedsRestart ? '<p role="alert">이 학습 공간에서는 새 학생으로 다시 연결할 수 없습니다. 필요한 코드를 내보낸 뒤 실습을 종료하고 새로 실행하세요.</p>' : ''}<div class="actions">${button("endSession")}</div></aside>` : ''}
<aside><h2>교수자 요청</h2><p role="status">${escapeHtml(state.classroomCommandStatus ?? "과제 연결 후 요청을 확인할 수 있습니다.")}</p><p>공지, 과제 열기, 강의자료 동기화, 환경 점검, 저장·제출 요청은 먼저 확인창으로 알려드립니다. 수락하거나 거절할 수 있으며 확인 여부·처리 결과를 교수자에게 전달합니다. 임의 명령 실행·PC 잠금·파일 삭제는 제공하지 않습니다.</p><h2>수업 상태 공유 · ${sharing}</h2><p>로그인하여 연결한 과제에서 IDE 연결 상태, 과제 열기, 파일 저장 발생, 제출 요청 활동을 교수자에게 전달합니다. 파일명·경로·코드 내용·다른 프로그램·브라우저 활동은 이 기능으로 수집하지 않습니다. 활동 공유는 코드 백업이나 공식 제출이 아닙니다.</p><p class="detail">공식 제출 여부와 점수는 서버의 제출 접수·채점 기록으로만 표시합니다. 연결이 끊기면 활동 기록 일부가 누락될 수 있으며, 로그아웃·IDE 종료 후 공유를 중단합니다. 강제 종료는 서버가 마지막 연결 시각으로 판별합니다.</p><h2>학습 환경</h2><p>${learningPolicyLabel(state.learningPolicy)}</p><p class="detail">실행 설정에 따른 안내이며 정책 준수 인증이 아닙니다. 프로필·폴더 분리는 OS 샌드박스가 아니며, 같은 OS 계정의 다른 프로그램·브라우저·외부 AI 접근을 차단하지 않습니다.</p>${button("exam")}<p>로그인 정보는 저장하지 않습니다. 창을 다시 열면 새 수령 코드로 연결하세요. 연결 상태는 아래 상태 표시줄에서 확인할 수 있습니다.</p>${!state.trusted ? '<p>제한 모드에서는 과제 파일 열기·자료 저장·다운로드·제출이 비활성화됩니다. 신뢰할 수 있는 폴더만 신뢰하세요.</p>' : ""}<p class="detail">시험 모드는 별도 관리 서버와 Agent의 상태를 표시합니다. 확장 자체는 생성형 AI나 외부 프로그램·네트워크 사용을 차단하지 않습니다. 실제 제한과 해제는 감독자가 관리하는 Agent에서 수행합니다.</p></aside>
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
