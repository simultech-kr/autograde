/** Only opaque keys and a render revision travel back from the webview. */
export const SERVER_DOWNLOADS_COMMAND = "autograde.openServerDownloads";
export type DownloadKind = "assignment" | "material" | "content";
export interface DownloadRow { readonly key: string; readonly title: string; readonly metadata: string; readonly description: string; readonly available: boolean; readonly unavailableReason?: string }
export interface DownloadSection { readonly kind: DownloadKind; readonly state: "loading" | "ready" | "error"; readonly rows: readonly DownloadRow[]; readonly error?: string }
export interface ServerDownloadsState { readonly revision: number; readonly authenticated: boolean; readonly trusted: boolean; readonly busy: boolean; readonly status: string; readonly sections: readonly DownloadSection[] }
export type ServerDownloadsAction = {type: "serverDownloads"; action: "refresh" | "connect"; revision: number} | {type: "serverDownloads"; action: "download"; revision: number; rowKey: string};
export function parseServerDownloadsAction(value: unknown): ServerDownloadsAction | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const item = value as Record<string, unknown>;
  if (item.type !== "serverDownloads" || !Number.isSafeInteger(item.revision) || Number(item.revision) < 1) return undefined;
  if ((item.action === "refresh" || item.action === "connect") && Object.keys(item).length === 3) return item as unknown as ServerDownloadsAction;
  if (item.action === "download" && Object.keys(item).length === 4 && typeof item.rowKey === "string" && /^row-[a-f0-9]{32}$/.test(item.rowKey)) return item as unknown as ServerDownloadsAction;
  return undefined;
}
const escape = (value: string) => value.replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]!));
const labels: Record<DownloadKind, {title: string; description: string; empty: string; action: string}> = {
  assignment: {title: "수락한 과제", description: "현재 수령 코드로 연결된 과제입니다. 새 폴더에 받으며 작성 중인 코드는 보존합니다.", empty: "현재 연결에서 받을 과제가 없습니다. 학생 웹에서 과제를 수락하고 해당 수령 코드로 연결하세요.", action: "과제 받기"},
  material: {title: "내 배포 자료", description: "교수자가 본인에게 배포한 PDF·ZIP입니다. 과제 코드와 분리해 저장합니다.", empty: "현재 학생에게 배포된 개인 자료가 없습니다.", action: "파일 받기"},
  content: {title: "수업 강의자료", description: "교수자가 공개한 자료 모음입니다. 변경분을 받고 별도 폴더에 저장할 수 있습니다.", empty: "현재 수업에서 받을 수 있는 공개 강의자료가 없습니다.", action: "자료 받기·업데이트"},
};
export function renderServerDownloads(state: ServerDownloadsState, nonce: string): string {
  const safeNonce = escape(nonce);
  const sections = state.authenticated ? state.sections.map(section => {
    const label = labels[section.kind], count = section.state === "ready" ? ` <span class="count">${section.rows.length}개</span>` : "";
    const body = section.state === "loading" ? '<p class="empty">목록을 불러오는 중입니다.</p>' : section.state === "error" ? `<p class="error" role="status">${escape(section.error ?? "목록을 불러오지 못했습니다. 새로고침하세요.")}</p>` : !section.rows.length ? `<p class="empty">${escape(label.empty)}</p>` : `<ul>${section.rows.map(row => {
      const reason = !row.available ? row.unavailableReason ?? "현재 다운로드할 수 없습니다." : !state.trusted ? "학습 폴더를 신뢰한 뒤 다운로드하세요." : state.busy ? "진행 중인 작업이 끝날 때까지 기다려 주세요." : undefined;
      return `<li><div class="row-copy"><h3>${escape(row.title)}</h3><p class="meta">${escape(row.metadata)}</p>${row.description ? `<p>${escape(row.description)}</p>` : ""}${!row.available ? `<p class="availability">${escape(reason!)}</p>` : ""}</div><button data-action="download" data-row-key="${escape(row.key)}"${reason ? ` disabled title="${escape(reason)}"` : ""}>${escape(label.action)}</button></li>`;
    }).join("")}</ul>`;
    return `<section aria-labelledby="${section.kind}"><header><h2 id="${section.kind}">${label.title}${count}</h2><p class="detail">${label.description}</p></header>${body}</section>`;
  }).join("") : `<section><h2>수령 코드로 연결하세요</h2><p>학생 웹에서 발급받은 수령 코드로 연결하면 수락한 과제와 본인에게 공개된 자료를 확인할 수 있습니다.</p><button data-action="connect"${state.busy ? " disabled" : ""}>수령 코드로 연결</button></section>`;
  return `<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${safeNonce}'; script-src 'nonce-${safeNonce}'; base-uri 'none'; form-action 'none';">
<title>서버 과제·자료</title><style nonce="${safeNonce}">
:root { color-scheme: light dark; } * { box-sizing: border-box; }
body { margin: 0; padding: 24px; color: var(--vscode-foreground); background: var(--vscode-editor-background); font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); line-height: 1.6; }
main { max-width: 980px; margin: auto; } .top { display: flex; align-items: start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 24px; margin: 0 0 8px; } h2 { font-size: 18px; margin: 0 0 4px; } h3 { font-size: 15px; margin: 0 0 4px; font-weight: 600; overflow-wrap: anywhere; }
p { margin: 0 0 8px; overflow-wrap: anywhere; white-space: pre-wrap; } .detail, .meta, .count { color: var(--vscode-descriptionForeground); }
.status, .notice { margin: 16px 0; padding: 12px 0; border-block: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); } .notice { border-left: 3px solid var(--vscode-focusBorder); padding-left: 12px; }
section { margin-top: 28px; min-width: 0; } section header { margin-bottom: 12px; } .count { font-size: 13px; font-weight: 400; }
ul { list-style: none; margin: 0; padding: 0; } li { display: grid; grid-template-columns: minmax(0, 1fr) minmax(120px, 180px); align-items: center; gap: 16px; padding: 16px 0; border-top: 1px solid var(--vscode-widget-border, var(--vscode-contrastBorder, #888)); }
.row-copy { min-width: 0; } .empty { padding: 12px 0; color: var(--vscode-descriptionForeground); } .error, .availability { color: var(--vscode-editorWarning-foreground, var(--vscode-foreground)); }
button { max-width: 100%; padding: 9px 12px; border: 1px solid var(--vscode-button-border, var(--vscode-contrastBorder, transparent)); color: var(--vscode-button-foreground); background: var(--vscode-button-background); cursor: pointer; font: inherit; line-height: 1.4; overflow-wrap: anywhere; }
button:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); } button:focus-visible { outline: 2px solid var(--vscode-focusBorder); outline-offset: 2px; } button:disabled { opacity: .55; cursor: default; }
@media (max-width: 520px) { body { padding: 16px; } li { grid-template-columns: minmax(0, 1fr); gap: 8px; } li button { justify-self: start; } }
</style></head><body><main><header class="top"><div><h1>서버 과제·자료</h1><p class="detail">수락한 과제와 교수자가 배포한 자료를 확인하고 내려받습니다.</p></div><button data-action="refresh"${!state.authenticated || state.busy ? " disabled" : ""}>목록 새로고침</button></header>
<p class="status" role="status" aria-live="polite">${escape(state.status)}</p>${state.authenticated && !state.trusted ? '<p class="notice">제한 모드에서는 목록만 확인할 수 있습니다. 다운로드하려면 현재 학습 폴더를 신뢰하세요.</p>' : ""}
${sections}<footer class="notice"><p>다른 과제는 학생 웹에서 수령 코드를 발급받아 연결하세요. 자료는 과제 제출물에 포함하지 않습니다. 공용 실습 공간에 받은 파일은 IDE 종료 시 정리되므로 보관할 파일은 내보내세요.</p></footer>
</main><script nonce="${safeNonce}">const vscode=acquireVsCodeApi(); const revision=${state.revision};
document.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',()=>{if(button.disabled)return;const message={type:'serverDownloads',action:button.dataset.action,revision};if(button.dataset.rowKey)message.rowKey=button.dataset.rowKey;vscode.postMessage(message);}));
</script></body></html>`;
}
