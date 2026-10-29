/** Pure presentation and message routing for the bundled learning IDE. */
import { learningPolicyLabel, type LearningPolicy } from "./learningEnvironment";
import type { ClassroomSharingStatus } from "./classroomActivity";
import type { Assignment } from "./types";
import { publicSubmissionScore, receiptProgress, submissionLabel, submissionTime } from "./learningJourney";
import { STUDENT_PORTAL_COMMAND } from "./studentPortalLink";
export const LEARNING_HOME_COMMAND = "autograde.openLearningHome";
export const AUTOGRADE_IDE_APP_NAME = "Autograde IDE";

export interface LearningHomeDiscovery {
  readonly status: "idle" | "searching" | "complete" | "unavailable";
  readonly message?: string;
  readonly servers: readonly { readonly id: string; readonly displayName: string; readonly apiUrl: string; readonly portalUrl: string }[];
}

export interface LearningHomeState {
  readonly authenticated: boolean;
  readonly trusted: boolean;
  readonly hasWorkspace: boolean;
  readonly busy?: boolean;
  readonly learningPolicy?: LearningPolicy;
  readonly classroomStatus?: ClassroomSharingStatus;
  readonly classroomCommandStatus?: string;
  readonly checkpointStatus?: string;
  readonly remoteLearningStatus?: string;
  readonly sharedSession?: boolean;
  readonly sessionNeedsRestart?: boolean;
  readonly assignment?: Assignment;
  readonly hasAssignmentFolder?: boolean;
  readonly serviceBaseUrl?: string;
  readonly studentPortalBaseUrl?: string;
  readonly sessionGeneration?: number;
  readonly discovery?: LearningHomeDiscovery;
}

export type LearningHomeForm = "claim" | "server" | "portal";
export type LearningHomeFormMessage = { type: "learningForm"; form: LearningHomeForm; nonce: string; value: string; expectedServer?: string };
export interface LearningHomePresentation { readonly initialForm?: LearningHomeForm; readonly feedback?: string }

/** A separate bounded channel for inline input; values never become command arguments. */
export function learningHomeFormMessage(message: unknown, nonce: string): LearningHomeFormMessage | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message) || !nonce) return undefined;
  const value = message as Record<string, unknown>;
  if (value.type !== "learningForm" || value.nonce !== nonce || typeof value.form !== "string" || !["claim", "server", "portal"].includes(value.form)
    || typeof value.value !== "string") return undefined;
  const keys = value.form === "claim" ? ["type", "form", "nonce", "value", "expectedServer"] : ["type", "form", "nonce", "value"];
  if (Object.keys(value).length !== keys.length || Object.keys(value).some(key => !keys.includes(key))) return undefined;
  if (!value.value.trim() || value.value.length > (value.form === "claim" ? 128 : 2048) || /[\u0000-\u001f\u007f]/.test(value.value)) return undefined;
  if (value.form === "claim" && (typeof value.expectedServer !== "string" || value.expectedServer.length > 2048 || /[\u0000-\u001f\u007f]/.test(value.expectedServer))) return undefined;
  return value as LearningHomeFormMessage;
}

export function learningHomeEditMessage(message: unknown, nonce: string): boolean | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message) || !nonce) return undefined;
  const value = message as Record<string, unknown>;
  return Object.keys(value).length === 3 && value.type === "learningEdit" && value.nonce === nonce && typeof value.dirty === "boolean" ? value.dirty : undefined;
}

/** Discovery selection carries an opaque ID only; endpoints stay in the host's verified registry. */
export function learningHomeDiscoverySelection(message: unknown, nonce: string): string | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message) || !nonce) return undefined;
  const value = message as Record<string, unknown>;
  if (Object.keys(value).length !== 3 || value.type !== "learningDiscoverySelect" || value.nonce !== nonce
    || typeof value.id !== "string" || !/^[a-z0-9][a-z0-9-]{0,127}$/.test(value.id)) return undefined;
  return value.id;
}

const actions = {
  portal: { command: STUDENT_PORTAL_COMMAND, label: "학생 포털 · 내 수업", session: false, trust: false, workspace: false },
  downloads: { command: "autograde.openServerDownloads", label: "서버 과제·자료 다운로드", session: false, trust: false, workspace: false },
  configure: { command: "autograde.configureServiceAddress", label: "서버 주소 설정", session: false, trust: false, workspace: false },
  discoverServer: { command: "autograde.discoverServers", label: "주변 서버 검색", session: false, trust: false, workspace: false },
  connection: { command: "autograde.checkServerConnection", label: "서버 연결 확인", session: false, trust: false, workspace: false },
  claim: { command: "autograde.redeemAssignmentClaim", label: "수령 코드로 연결 · 과제 받기", session: false, trust: true, workspace: false },
  open: { command: "autograde.openAssignment", label: "기존 과제 이어서 열기", session: true, trust: true, workspace: false },
  accepted: { command: "autograde.acceptedAssignmentHistory", label: "수락 이력 · 과제 전환", session: true, trust: false, workspace: false },
  materials: { command: "autograde.studentMaterials", label: "내 배포 자료 · PDF / ZIP 받기", session: true, trust: false, workspace: false },
  content: { command: "autograde.syncContent", label: "강의자료 업데이트 · 변경분 받기 / 내 폴더에 저장", session: true, trust: false, workspace: false },
  remoteLearning: { command: "autograde.remoteLearning", label: "원격 C/C++/Python 실습 · 실행", session: true, trust: true, workspace: true },
  extensions: { command: "autograde.manageCourseExtensions", label: "수업 확장 설치·업데이트 확인", session: true, trust: true, workspace: true },
  submit: { command: "autograde.submitCurrentCommit", label: "현재 과제 공식 제출", session: true, trust: true, workspace: true },
  result: { command: "autograde.viewLatestResult", label: "이번 제출 결과 확인", session: true, trust: false, workspace: false },
  history: { command: "autograde.submissionHistory", label: "제출 이력 · 코드 복원", session: true, trust: false, workspace: false },
  checkpointSave: { command: "autograde.saveLearningCheckpoint", label: "지금 작업 서버 저장 · 공식 제출 아님", session: true, trust: true, workspace: true },
  checkpointRestore: { command: "autograde.restoreLearningCheckpoint", label: "서버 작업 저장본 · 새 폴더로 복원", session: true, trust: true, workspace: true },
  exportLearning: { command: "autograde.exportLearning", label: "내 학습내용 내보내기 · 폴더 / ZIP", session: false, trust: true, workspace: false },
  endSession: { command: "autograde.endLearningSession", label: "공용 실습 종료 · 임시 파일 정리", session: false, trust: false, workspace: false },
  exam: { command: "autograde.openExamMode", label: "시험 모드 · 좌석 / Agent 상태", session: false, trust: false, workspace: false },
} as const;
export type LearningHomeAction = keyof typeof actions;

export function learningHomeAction(message: unknown, nonce?: string): LearningHomeAction | undefined {
  if (!message || typeof message !== "object" || Array.isArray(message)) return undefined;
  const value = message as Record<string, unknown>;
  // No command IDs, arguments, URLs or authentication material are accepted from the webview.
  if (Object.keys(value).length !== (nonce ? 3 : 2) || value.type !== "learningAction" || typeof value.action !== "string" || nonce && value.nonce !== nonce) return undefined;
  return Object.prototype.hasOwnProperty.call(actions, value.action) ? value.action as LearningHomeAction : undefined;
}

export function learningHomeCommand(action: LearningHomeAction): string { return actions[action].command; }

export function learningHomeBlockedReason(action: LearningHomeAction, state: LearningHomeState): string | undefined {
  if (action === "endSession") return state.sharedSession ? undefined : "공용 실습 실행기로 시작한 세션에서 사용할 수 있습니다.";
  if (state.busy) return "이전 작업을 완료하거나 취소한 뒤 다시 선택하세요.";
  const item = actions[action];
  if (state.sharedSession && ((state.authenticated || state.sessionNeedsRestart) && ["claim", "configure", "discoverServer"].includes(action)
    || state.sessionNeedsRestart && item.session)) {
    return "필요한 코드를 내보낸 뒤 공용 실습을 종료하고, 새 세션에서 수령 코드로 연결하세요.";
  }
  if (action === "discoverServer" && state.discovery?.status === "searching") return "주변 서버를 검색하고 있습니다. 잠시 기다려 주세요.";
  if (item.trust && !state.trusted) return "파일을 열거나 제출하려면 신뢰할 수 있는 과제 폴더를 열고 작업 영역을 신뢰하세요.";
  if (item.session && !state.authenticated) return "학생 웹에서 새 수령 코드를 받아 먼저 연결하세요. 로그인 정보는 저장되지 않습니다.";
  if (item.workspace && !state.hasWorkspace) return "기존 과제 이어서 열기 또는 과제 다운로드로 제출할 작업 영역을 먼저 여세요.";
  if (["submit", "checkpointSave", "remoteLearning"].includes(action) && state.hasAssignmentFolder === false) return "현재 수령 코드의 과제 폴더를 먼저 연결하세요. 다른 작업 폴더는 제출하지 않습니다.";
  return undefined;
}

export function shouldOpenLearningHome(appName: string, folderCount: number, hasWorkspaceFile: boolean, openTabCount: number): boolean {
  return (appName === AUTOGRADE_IDE_APP_NAME || appName === `${AUTOGRADE_IDE_APP_NAME} Dev`)
    && folderCount === 0 && !hasWorkspaceFile && openTabCount === 0;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]!));
}

export function learningNextStep(state: LearningHomeState): { title: string; detail: string; action: LearningHomeAction } {
  if (state.sessionNeedsRestart) return { title: "공용 실습을 종료하세요", detail: "이전 학생의 공간은 다시 인증하지 않습니다. 필요한 코드를 내보낸 뒤 종료하고 새로 실행하세요.", action: "endSession" };
  if (!state.authenticated) return { title: "수령 코드로 과제를 연결하세요", detail: "학생 포털에서 내 수업과 과제를 선택하고 수령 코드를 받으세요. 웹 비밀번호는 IDE에 입력하지 않습니다.", action: "claim" };
  if (!state.assignment) return { title: "수락한 과제를 확인하세요", detail: "현재 인증으로 이용할 수 있는 과제와 자료를 확인하고 실습할 과제를 선택하세요.", action: "downloads" };
  if (!state.hasAssignmentFolder) return { title: "과제 폴더를 열어 실습을 이어가세요", detail: "기존 폴더를 연결하거나 과제·자료에서 새로 다운로드하세요. 공용 실습의 이전 코드는 서버 저장본에서 복원합니다.", action: "open" };
  if (state.assignment.latestSubmission) return { title: submissionLabel(state.assignment.latestSubmission.state), detail: ["received", "pinning", "verifying", "accepted", "queued", "running"].includes(state.assignment.latestSubmission.state)
    ? "접수번호가 발급되었습니다. 다시 제출하지 않아도 같은 접수의 채점 상태를 확인할 수 있습니다."
    : "이번 제출의 공개 결과와 이전 최고점을 확인하세요. 코드를 수정했다면 모두 저장한 뒤 공식 제출을 다시 진행하세요.", action: "result" };
  return { title: "코드를 저장하고 공식 제출하세요", detail: "자동 저장은 작업 보관용입니다. 점수를 받으려면 현재 과제를 공식 제출해야 합니다.", action: "submit" };
}

export function renderLearningHome(state: LearningHomeState, nonce: string, presentation: LearningHomePresentation = {}): string {
  const button = (action: LearningHomeAction, primary = false): string => {
    const reason = learningHomeBlockedReason(action, state);
    return `<button type="button" data-action="${action}"${primary ? ' class="primary"' : ""}${reason ? ` disabled title="${escapeHtml(reason)}"` : ""}>${actions[action].label}</button>`;
  };
  const status = state.authenticated ? "수령 코드 연결됨" : "수령 코드 연결 필요";
  const safeNonce = escapeHtml(nonce);
  const sharing = { inactive: "대기 · 연결된 과제 폴더 없음", sharing: "공유 중", retrying: "연결 지연 · 재시도 대기", unavailable: "공유 중단 · 서버 지원 또는 인증 확인 필요" }[state.classroomStatus ?? "inactive"];
  const next = learningNextStep(state);
  const submission = state.assignment?.latestSubmission;
  const best = submission?.previousBest;
  const bestScore = best ? publicSubmissionScore({state: "published", score: best.score, maxScore: best.maxScore}) : undefined;
  const progress = receiptProgress(submission?.state);
  const claimReason = learningHomeBlockedReason("claim", state) ?? (!state.serviceBaseUrl ? "채점 서버 주소를 먼저 설정하세요." : undefined);
  const configureReason = learningHomeBlockedReason("configure", state);
  const disabled = (reason?: string): string => reason ? ` disabled title="${escapeHtml(reason)}"` : "";
  const discovery = state.discovery;
  const discoveryMessage = discovery?.message ?? ({
    idle: "같은 실습실에서 교수자가 안내 중인 서버를 찾습니다.",
    searching: "주변 서버를 검색하고 있습니다. 잠시 기다려 주세요.",
    complete: discovery?.servers.length ? "접속할 기관을 선택하세요. 서버를 선택한 뒤 수령 코드를 입력합니다." : "등록된 서버를 찾지 못했습니다. 같은 실습실 네트워크인지 확인하거나 교수자가 안내한 주소를 직접 입력하세요.",
    unavailable: "자동 검색을 사용할 수 없습니다. 다시 검색하거나 교수자가 안내한 주소를 직접 입력하세요.",
  }[discovery?.status ?? "idle"]);
  const selectionReason = configureReason ?? (discovery?.status === "searching" ? "검색이 끝난 뒤 서버를 선택하세요." : undefined);
  const discoveredServers = (discovery?.servers ?? []).filter(server => /^[a-z0-9][a-z0-9-]{0,127}$/.test(server.id)).slice(0, 32);
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
.context,.next,.result { border: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); padding: 18px; margin: 16px 0; }
.next { border-left: 3px solid var(--vscode-focusBorder); }.context h2{font-size:20px}.eyebrow{font-size:12px;color:var(--vscode-descriptionForeground)}
.scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:12px}.scores dt{color:var(--vscode-descriptionForeground)}.scores dd{margin:4px 0;font-size:20px;font-weight:600}
.steps{display:flex;list-style:none;padding:0;gap:12px;flex-wrap:wrap}.steps li{color:var(--vscode-descriptionForeground);border-bottom:2px solid var(--vscode-widget-border,#888);padding:4px 0}.steps [aria-current=step]{color:var(--vscode-foreground);font-weight:600;border-color:var(--vscode-focusBorder)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap}.tools{margin:24px 0;border-top:1px solid var(--vscode-widget-border,#888);padding-top:16px}.tools>summary{cursor:pointer;font-size:17px;font-weight:600;padding:8px 0}.tools[open]>summary{margin-bottom:16px}code{overflow-wrap:anywhere}
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 24px; }
section { min-width: 0; }
.actions { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
button { border: 1px solid var(--vscode-button-border, var(--vscode-contrastBorder, transparent)); padding: 9px 12px; font: inherit; line-height: 1.4; text-align: left; overflow-wrap: anywhere; cursor: pointer; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); }
button:hover:not(:disabled) { background: var(--vscode-button-secondaryHoverBackground); }
button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
button.primary:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); }
button:focus-visible { outline: 2px solid var(--vscode-focusBorder); outline-offset: 2px; }
button:disabled { cursor: default; opacity: .55; }
form { margin: 12px 0; } label { display: block; font-weight: 600; margin-bottom: 6px; }
input { width: 100%; min-width: 0; padding: 10px; border: 1px solid var(--vscode-input-border,var(--vscode-widget-border,#888)); color: var(--vscode-input-foreground); background: var(--vscode-input-background); font: inherit; }
input:focus-visible { outline: 2px solid var(--vscode-focusBorder); outline-offset: 1px; }
.input-row { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 8px; align-items: start; }
.inline-connect { padding: 16px 0; } .feedback { color: var(--vscode-foreground); border-left: 3px solid var(--vscode-focusBorder); padding-left: 10px; } .feedback:empty { display: none; }
.settings { margin: 16px 0; } .settings>summary { cursor: pointer; padding: 8px 0; }
.discovery { margin: 20px 0; padding: 16px; border: 1px solid var(--vscode-widget-border,var(--vscode-contrastBorder,#888)); }
.discovery-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:12px; }.discovery-heading h2 { margin:0; }
.server-list { list-style:none; margin:12px 0 0; padding:0; display:grid; gap:12px; }.server-card { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:12px; padding-top:12px; border-top:1px solid var(--vscode-widget-border,#888); }.server-card p { margin:0; }.server-card strong { overflow-wrap:anywhere; }.server-endpoints { font-size:12px; }
aside { margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); }
@media (max-width: 420px) { body { padding: 16px; } h1 { font-size: 22px; } .input-row,.server-card { grid-template-columns: minmax(0,1fr); } }
</style></head><body><main data-nonce="${safeNonce}" data-initial-form="${presentation.initialForm ?? ""}" data-expected-server="${escapeHtml(state.serviceBaseUrl ?? "")}">
<header><h1>Autograde 학습 홈</h1><p class="lead">과제를 받고, 기존 코드를 이어서 작성하고, 제출 결과를 확인하세요.</p></header>
<p class="status" role="status" aria-live="polite">${status} · ${state.hasWorkspace ? "작업 영역 열림" : "작업 영역 없음"}${state.trusted ? "" : " · 제한 모드"}${state.busy ? " · 작업 진행 중" : ""}</p>
${state.assignment ? `<section class="context" aria-label="현재 수업과 과제"><p class="eyebrow">현재 수업 · ${escapeHtml(state.assignment.courseLabel ?? state.assignment.courseKey ?? "수업 정보 없음")}</p><h2>${escapeHtml(state.assignment.title)}</h2><p>${state.hasAssignmentFolder ? "과제 폴더 연결됨" : "수령 완료 · 과제 폴더 연결 필요"}${state.assignment.dueAt ? ` · 마감 ${escapeHtml(submissionTime(state.assignment.dueAt))}` : ""}</p></section>` : ""}
${submission ? `<section class="result" aria-label="이번 공식 제출"><h2>이번 공식 제출 · ${escapeHtml(submissionLabel(submission.state))}</h2><p>접수번호 <code>${escapeHtml(submission.id)}</code><br>접수 시각 ${escapeHtml(submissionTime(submission.receivedAt))}</p><ol class="steps" aria-label="채점 진행 단계">${progress.map(step => `<li${step.current ? ' aria-current="step"' : ''}>${step.complete ? "✓ " : ""}${step.label}</li>`).join("")}</ol><dl class="scores"><div><dt>이번 제출 점수</dt><dd>${escapeHtml(publicSubmissionScore(submission) ?? (submission.state === "published" ? "결과 확인 필요" : "아직 공개되지 않음"))}</dd></div><div><dt>이전 제출 중 최고점</dt><dd>${escapeHtml(bestScore ?? "공개 기록 없음")}</dd></div></dl><p class="detail">새 제출의 채점이 끝나기 전에는 이전 점수를 이번 점수로 표시하지 않습니다.</p></section>` : ""}
<section class="next" aria-labelledby="next-action"><p class="eyebrow">다음 할 일</p><h2 id="next-action">${escapeHtml(next.title)}</h2><p>${escapeHtml(next.detail)}</p>${button(next.action, true)}${!state.trusted ? '<p role="status">제한 모드에서는 과제 파일 작업이 비활성화됩니다. 신뢰할 수 있는 과제 폴더인지 확인하세요.</p>' : ""}</section>
<div class="toolbar">${button("portal")}${button("downloads")}${button("exportLearning")}</div>
<p class="detail">학생 포털의 웹 로그인과 IDE 수령 코드 연결은 별개입니다. 공용 PC를 떠날 때는 웹에서도 로그아웃하세요.</p>
<section class="discovery" aria-labelledby="discovery-heading" aria-busy="${discovery?.status === "searching"}"><div class="discovery-heading"><h2 id="discovery-heading">수업 서버 찾기</h2>${button("discoverServer")}</div><p role="status" aria-live="polite">${escapeHtml(discoveryMessage)}</p><p class="detail">등록된 기관과 접속 주소가 일치하는 서버만 표시합니다. 검색 결과만으로 로그인하거나 학교 네트워크 설정을 바꾸지 않습니다.</p>
${discoveredServers.length ? `<ul class="server-list">${discoveredServers.map(server => {
    const current = state.serviceBaseUrl === server.apiUrl && state.studentPortalBaseUrl === server.portalUrl;
    return `<li class="server-card"><div><strong>${escapeHtml(server.displayName)}</strong>${current ? '<p class="detail">현재 주소 · 연결 확인 후 사용하세요.</p>' : ''}<p class="detail server-endpoints">채점 서버 <code>${escapeHtml(server.apiUrl)}</code><br>학생 포털 <code>${escapeHtml(server.portalUrl)}</code></p></div><button type="button" data-discovery-id="${escapeHtml(server.id)}"${disabled(selectionReason)}>${current ? "연결 확인" : "이 서버 선택"}</button></li>`;
  }).join("")}</ul>` : ""}<p class="detail">검색되지 않으면 아래 서버 주소 설정에서 직접 연결할 수 있습니다.</p>${configureReason ? `<p class="detail">${escapeHtml(configureReason)}</p>` : ""}</section>
<section class="inline-connect" aria-labelledby="claim-heading"><h2 id="claim-heading">수령 코드로 연결</h2><p class="detail">학생 포털에서 받은 코드를 아래에 입력하세요. 비밀번호를 입력하지 마세요. 코드는 전송 즉시 지우며 저장하지 않습니다.</p><p class="detail">채점 서버 <code>${escapeHtml(state.serviceBaseUrl ?? "주소 설정 필요")}</code></p>
<form data-form="claim" autocomplete="off" novalidate><label for="claim-code">과제 수령 코드</label><div class="input-row"><input id="claim-code" type="password" maxlength="128" autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="claim-help"${disabled(claimReason)}><button type="submit" class="primary"${disabled(claimReason)}>코드로 연결</button></div><p id="claim-help" class="detail">${escapeHtml(claimReason ?? "이 입력란에서 Enter를 누르거나 코드로 연결을 선택하세요.")}</p></form></section>
<p id="learning-feedback" class="feedback" role="status" aria-live="polite">${escapeHtml(presentation.feedback ?? "")}</p>
<details id="server-settings" class="settings"${presentation.initialForm === "server" || presentation.initialForm === "portal" ? " open" : ""}><summary>서버 주소 설정</summary><p class="detail">채점 서버와 학생 포털은 주소가 다를 수 있습니다. 교수자가 안내한 주소를 각각 입력하세요.</p>
<form data-form="server" autocomplete="off" novalidate><label for="server-address">채점 서버 주소</label><div class="input-row"><input id="server-address" type="url" maxlength="2048" autocomplete="off" spellcheck="false" value="${escapeHtml(state.serviceBaseUrl ?? "")}"${disabled(configureReason)}><button type="submit"${disabled(configureReason)}>채점 서버 적용</button></div></form>
<form data-form="portal" autocomplete="off" novalidate><label for="portal-address">학생 포털 주소</label><div class="input-row"><input id="portal-address" type="url" maxlength="2048" autocomplete="off" spellcheck="false" value="${escapeHtml(state.studentPortalBaseUrl ?? "")}"${disabled(configureReason)}><button type="submit"${disabled(configureReason)}>학생 포털 적용</button></div></form>${configureReason ? `<p class="detail">${escapeHtml(configureReason)}</p>` : ""}</details>
<details class="tools"><summary>과제·자료·제출 도구</summary>
<div class="grid">
<section aria-labelledby="connect"><h2 id="connect">1. 수업 연결</h2><p>교수자가 안내한 채점 서버 주소를 설정하세요. 학생 웹에서 받은 수령 코드를 입력합니다.</p><div class="actions">${button("configure")}${button("connection")}${button("claim", true)}</div></section>
<section aria-labelledby="resume"><h2 id="resume">2. 실습과 배포 자료</h2><p>${state.sharedSession ? "이번 실습의 새 폴더에 과제를 받습니다. 이전 작업은 서버 작업 저장본 또는 제출 이력에서 새 폴더로 복원할 수 있습니다. 코드와 자료를 보관하려면 내 학습내용 내보내기를 사용하세요." : "IDE를 다시 열었다면 새 수령 코드로 연결한 뒤 기존 폴더를 여세요. 강의자료는 학생별 캐시에 변경분만 받고, 필요할 때 별도 폴더에 저장합니다. 과제 코드는 덮어쓰지 않습니다."}</p><div class="actions">${button("downloads", true)}${button("open")}${button("checkpointRestore")}${button("accepted")}${button("extensions")}</div></section>
<section aria-labelledby="submit"><h2 id="submit">3. 제출과 피드백</h2><p>제출할 과제 폴더를 확인하세요. 제출 접수와 채점 완료는 별도이며, 현재 제출의 결과와 이전 이력을 확인할 수 있습니다.</p><div class="actions">${button("submit", true)}${button("result")}${button("history")}${button("exportLearning")}</div></section>
</div></details>
<aside><h2>개인 원격 실행공간</h2><p role="status">${escapeHtml(state.remoteLearningStatus ?? "과제 연결 후 환경을 준비하세요")}</p><p class="detail">현재 코드를 서버에 저장한 뒤 Linux에서 컴파일·실행합니다. 공식 제출·채점은 별도입니다.</p>${button("remoteLearning")}</aside>
<aside aria-label="작업 코드 자동 저장"><h2>작업 코드 자동 저장 · 공식 제출과 별도</h2><p role="status" aria-live="polite">${escapeHtml(state.checkpointStatus ?? "대기 · 연결된 과제 폴더 없음")}</p><p>현재 인증으로 연결한 과제 폴더의 편집 내용을 저장하고, 변경된 코드를 30초마다 서버에 보관합니다. 다른 과제·강의자료·다른 프로그램 활동은 이 기능으로 보내지 않습니다. 서버의 접수 응답을 받아야 저장 완료로 표시합니다. 공용 실습에서 새로 받은 템플릿은 수정하거나 지금 저장을 눌렀을 때 보관합니다.</p><p>자동 저장은 공식 제출이나 채점을 시작하지 않습니다. 연결이 끊기거나 파일을 저장할 수 없으면 최신 코드가 서버에 없을 수 있습니다. 최신 상태를 확인하고 필요하면 내보내기를 사용하세요.</p>${state.sharedSession ? '<p><strong>공용 실습 종료 전 확인:</strong> 학습 홈의 종료 버튼을 사용하면 저장 재시도·내보내기·폐기를 선택할 수 있습니다. 창 닫기·강제 종료도 로컬 실습 내용과 로그인 정보를 삭제합니다. 마지막 서버 저장 이후 변경은 복구되지 않을 수 있습니다.</p>' : ''}${button("checkpointSave")}</aside>
${state.sharedSession ? `<aside aria-label="공용 실습 공간"><h2>공용 PC · 이번 실습만 보관</h2><p>Autograde IDE를 완전히 종료하면 이번 실행의 코드·자료·IDE 임시 기록을 정리합니다. 서버 저장이 확인된 작업본·공식 제출본과 별도로 내보낸 폴더·ZIP은 보존합니다. 다음 실행 시 남은 파일도 정리합니다. 서버 저장을 확인하지 못한 코드는 먼저 내보내세요. Mac에서는 창 닫기만으로 앱이 종료되지 않을 수 있습니다.</p>${state.sessionNeedsRestart ? '<p role="alert">이 학습 공간에서는 새 학생으로 다시 연결할 수 없습니다. 필요한 코드를 내보낸 뒤 실습을 종료하고 새로 실행하세요.</p>' : ''}<div class="actions">${button("endSession")}</div></aside>` : ''}
<details class="tools"><summary>교수자 요청·수업 공유·환경 안내</summary><aside><h2>교수자 요청</h2><p role="status">${escapeHtml(state.classroomCommandStatus ?? "과제 연결 후 요청을 확인할 수 있습니다.")}</p><p>공지, 과제 열기, 강의자료 동기화, 환경 점검, 저장·제출 요청은 먼저 확인창으로 알려드립니다. 수락하거나 거절할 수 있으며 확인 여부·처리 결과를 교수자에게 전달합니다. 임의 명령 실행·PC 잠금·파일 삭제는 제공하지 않습니다.</p><h2>수업 상태 공유 · ${sharing}</h2><p>로그인하여 연결한 과제에서 IDE 연결 상태, 과제 열기, 파일 저장 발생, 제출 요청 활동을 교수자에게 전달합니다. 파일명·경로·코드 내용·다른 프로그램·브라우저 활동은 이 기능으로 수집하지 않습니다. 활동 공유는 코드 백업이나 공식 제출이 아닙니다.</p><p class="detail">공식 제출 여부와 점수는 서버의 제출 접수·채점 기록으로만 표시합니다. 연결이 끊기면 활동 기록 일부가 누락될 수 있으며, 로그아웃·IDE 종료 후 공유를 중단합니다. 강제 종료는 서버가 마지막 연결 시각으로 판별합니다.</p><h2>학습 환경</h2><p>${learningPolicyLabel(state.learningPolicy)}</p><p class="detail">실행 설정에 따른 안내이며 정책 준수 인증이 아닙니다. 프로필·폴더 분리는 OS 샌드박스가 아니며, 같은 OS 계정의 다른 프로그램·브라우저·외부 AI 접근을 차단하지 않습니다.</p>${button("exam")}<p>로그인 정보는 저장하지 않습니다. 창을 다시 열면 새 수령 코드로 연결하세요. 연결 상태는 아래 상태 표시줄에서 확인할 수 있습니다.</p>${!state.trusted ? '<p>제한 모드에서는 과제 파일 열기·자료 저장·다운로드·제출이 비활성화됩니다. 신뢰할 수 있는 폴더만 신뢰하세요.</p>' : ""}<p class="detail">시험 모드는 별도 관리 서버와 Agent의 상태를 표시합니다. 확장 자체는 생성형 AI나 외부 프로그램·네트워크 사용을 차단하지 않습니다. 실제 제한과 해제는 감독자가 관리하는 Agent에서 수행합니다.</p></aside>
</details></main><script nonce="${safeNonce}">
const vscode = acquireVsCodeApi();
const main = document.querySelector('main');
const nonce = main.dataset.nonce;
const feedback = document.getElementById('learning-feedback');
const inputs = [...document.querySelectorAll('form[data-form] input')];
function focusForm(form) {
  const target = document.querySelector('form[data-form="' + form + '"] input');
  if (!target) return;
  if (form !== 'claim') document.getElementById('server-settings').open = true;
  target.scrollIntoView({block:'center'}); target.focus();
}
function reportEditing() {
  vscode.postMessage({type:'learningEdit', nonce, dirty:inputs.some(input => input.value !== input.defaultValue)});
}
inputs.forEach(input => input.addEventListener('input', reportEditing));
document.querySelectorAll('form[data-form]').forEach(form => form.addEventListener('submit', event => {
  event.preventDefault();
  const input = form.querySelector('input');
  if (input.disabled || !input.value.trim()) return;
  let value = input.value;
  if (form.dataset.form === 'claim') input.value = '';
  const message = {type:'learningForm',form:form.dataset.form,nonce,value};
  if (form.dataset.form === 'claim') message.expectedServer = main.dataset.expectedServer;
  vscode.postMessage(message);
  value = '';
}));
document.querySelectorAll('button[data-action]').forEach(button => {
  button.addEventListener('click', () => {
    if (button.disabled) return;
    vscode.postMessage({ type: 'learningAction', action: button.dataset.action, nonce });
  });
});
document.querySelectorAll('button[data-discovery-id]').forEach(button => {
  button.addEventListener('click', () => {
    if (button.disabled) return;
    vscode.postMessage({type:'learningDiscoverySelect', nonce, id:button.dataset.discoveryId});
  });
});
window.addEventListener('message', event => {
  const message = event.data;
  if (!message || message.nonce !== nonce) return;
  if (message.type === 'learningFocus' && ['claim','server','portal'].includes(message.form)) focusForm(message.form);
  if (message.type === 'learningFeedback' && typeof message.text === 'string') feedback.textContent = message.text;
});
window.addEventListener('pagehide', () => { inputs.forEach(input => {input.value = '';}); });
if (['claim','server','portal'].includes(main.dataset.initialForm)) focusForm(main.dataset.initialForm);
</script></body></html>`;
}
