import { runtimeSessionActive, validRuntimeSource, type RemoteLearningState, type RuntimeLanguage, type RuntimeOutput } from "./remoteLearning";

export interface RuntimeInputs { readonly language: RuntimeLanguage; readonly sources: readonly string[]; readonly stdin: string; }
const escapeHtml = (value: string): string => value.replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]!));
const text = (value: string) => escapeHtml(value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ""));
export function runtimePhaseLabel(state: RemoteLearningState): string {
  const labels = {idle:"과제 연결 후 환경을 준비하세요", checking:"서버 환경 확인 중", saving:"현재 코드 서버 저장 중", preparing:"개인 실행공간 준비 중",
    running:state.resultLanguage === "python" ? "Linux에서 Python 실행 중" : "Linux에서 컴파일·실행 중", closing:"실행공간 삭제 확인 중", ready:"원격 실습 사용 가능", disabled:"이 서버는 원격 실습을 제공하지 않습니다",
    closed: state.cleanupVerified ? "서버 저장·원격 공간 삭제 확인됨" : "원격 실행공간 종료됨", error:"확인 필요"};
  return labels[state.phase];
}
export function defaultRuntimeSources(files: readonly string[], language: RuntimeLanguage): string[] {
  const available = files.filter(file => validRuntimeSource(file, language));
  const main = available.find(file => /(?:^|\/)main\.(?:c|cpp|cc|cxx|py)$/.test(file))
    ?? (language === "python" ? available.find(file => /(?:^|\/)__main__\.py$/.test(file)) : undefined);
  return main ? [main] : available.slice(0,1);
}
export function renderRemoteLearning(state: RemoteLearningState, inputs: RuntimeInputs, busy: boolean, connected: boolean, nonce: string): string {
  const safeNonce = escapeHtml(nonce), files = (state.snapshot?.filePaths ?? []).filter(file => validRuntimeSource(file, inputs.language));
  const button = (action: string, title: string, disabled = false, primary = false) => `<button type="button" data-action="${action}"${disabled ? ' disabled' : ''}${primary ? ' class="primary"' : ''}>${title}</button>`;
  const result = state.result;
  const python = inputs.language === "python";
  const languages: readonly RuntimeLanguage[] = state.info?.languages ?? ["c", "cpp", "python"];
  const languageNames = {c:"C · C17", cpp:"C++ · C++17", python:"Python · Python 3"};
  const output = (title: string, value: RuntimeOutput | null | undefined) => !value ? "" :
    `<section><h2>${title} <span class="muted">종료 코드 ${value.exit_code === null ? "없음" : value.exit_code}</span></h2><h3>표준출력</h3><pre>${text(value.stdout || "출력 없음")}</pre><h3>오류·진단</h3><pre>${text(value.stderr || "없음")}</pre></section>`;
  const resultLabel = result && {completed:"실행 완료",compile_error:"컴파일 실패 · 진단을 확인하세요",runtime_error:"실행 실패",timeout:"실행 제한 시간 초과",output_limit:"출력 크기 제한 도달"}[result.status];
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${safeNonce}'; script-src 'nonce-${safeNonce}'; base-uri 'none'; form-action 'none'">
<title>원격 C/C++/Python 실습</title><style nonce="${safeNonce}">
:root{color-scheme:light dark}*{box-sizing:border-box}body{margin:0;padding:20px;color:var(--vscode-foreground);background:var(--vscode-editor-background);font:var(--vscode-font-size)/1.6 var(--vscode-font-family)}main{max-width:900px;margin:auto}h1{font-size:24px;margin:0 0 6px}h2{font-size:17px}h3{font-size:13px;margin-bottom:4px}p{margin:8px 0;overflow-wrap:anywhere}.muted{color:var(--vscode-descriptionForeground);font-size:.9em}.status,section{border-top:1px solid var(--vscode-widget-border,var(--vscode-contrastBorder,#888));padding-top:16px;margin-top:20px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}button{min-height:44px;padding:8px 12px;font:inherit;cursor:pointer;color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground);border:1px solid var(--vscode-contrastBorder,transparent)}button.primary{color:var(--vscode-button-foreground);background:var(--vscode-button-background)}button:disabled{opacity:.55;cursor:default}button:focus-visible,select:focus,textarea:focus{outline:2px solid var(--vscode-focusBorder)}label{display:block;margin:12px 0 4px}select,textarea{width:100%;min-height:44px;padding:8px;font:inherit;color:var(--vscode-input-foreground);background:var(--vscode-input-background);border:1px solid var(--vscode-input-border,var(--vscode-widget-border,#888))}textarea{min-height:100px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;padding:12px;background:var(--vscode-textCodeBlock-background);font-family:var(--vscode-editor-font-family,monospace)}.alert{color:var(--vscode-errorForeground)}@media(max-width:460px){body{padding:12px}.actions button{flex:1 1 140px}}
</style></head><body><main><h1>원격 C/C++/Python 실습</h1><p class="muted">학생별 Linux 실행공간에서 코드를 실행합니다. 로컬 컴파일러나 Python 설치는 필요하지 않습니다. 실행은 공식 제출·채점과 별도입니다.</p>
<div class="status" role="status"><strong>${escapeHtml(runtimePhaseLabel(state))}</strong>${!connected ? '<p>새 수령 코드로 연결하고 신뢰한 과제 폴더를 여세요.</p>' : ''}
${state.message ? `<p class="alert" role="alert">${text(state.message)}</p>` : ''}
${state.snapshot ? `<p>서버 작업 저장 v${state.snapshot.checkpoint.version} · ${escapeHtml(new Date(state.snapshot.checkpoint.created_at).toLocaleString())}<br><span class="muted">코드 버전 ${escapeHtml(state.snapshot.checkpoint.checkpoint_id)}</span></p>` : ''}
${state.editedSinceResult && !result ? '<p class="alert">저장·실행 준비 중 추가 편집이 발생했습니다. 현재 편집 내용은 다시 저장·실행해 확인하세요.</p>' : ''}
${state.session ? `<p class="muted">원격 공간: ${escapeHtml(state.session.status)} · 만료 ${escapeHtml(new Date(state.session.expires_at).toLocaleString())}</p>` : ''}</div>
<div class="actions">${button("prepare", "환경 준비 · 현재 코드 저장", busy || !connected, true)}${button("refresh", "상태 새로고침", busy || !connected)}${button("close", "저장 후 원격 공간 종료", busy || !connected || !runtimeSessionActive(state.session))}</div>
<section><label for="language">언어</label><select id="language"${busy ? ' disabled' : ''}>${languages.map(language => `<option value="${language}"${inputs.language === language ? ' selected' : ''}>${languageNames[language]}</option>`).join('')}</select>
<label for="sources">${python ? '실행할 Python 파일 · 1개 선택' : '컴파일할 소스 · 여러 개 선택 가능'}</label><select id="sources"${python ? '' : ' multiple'} size="${Math.min(7, Math.max(2, files.length))}"${busy ? ' disabled' : ''}>${files.map(file => `<option value="${escapeHtml(file)}"${inputs.sources.includes(file) ? ' selected' : ''}>${escapeHtml(file)}</option>`).join('')}</select>
<p class="muted">환경 준비 후 현재 코드의 소스 목록을 표시합니다. 새 파일을 추가했다면 환경 준비를 다시 누르세요. ${python ? '다른 Python 모듈도 저장된 프로젝트에서 불러옵니다. 표준 라이브러리를 제공하며 추가 패키지 설치·대화형 터미널은 지원하지 않습니다.' : '헤더는 소스 묶음에 포함됩니다.'}</p>
<label for="stdin">표준입력 · 프로그램에 전달할 텍스트</label><textarea id="stdin"${busy ? ' disabled' : ''}>${escapeHtml(inputs.stdin)}</textarea>
<div class="actions">${button("run", python ? "현재 코드 저장 후 실행" : "현재 코드 저장 후 빌드·실행", busy || !connected || !state.info?.enabled || !runtimeSessionActive(state.session) || !files.length, true)}</div></section>
${result ? `<section aria-label="현재 실행 결과"><h2>${escapeHtml(resultLabel!)}</h2><p>실행한 코드 버전: ${escapeHtml(result.checkpoint_id)}</p>${state.editedSinceResult ? '<p class="alert">실행 이후 코드가 변경되었습니다. 현재 편집 내용의 결과는 다시 실행하여 확인하세요.</p>' : ''}</section>${state.resultLanguage === 'python' ? '<section><h2>Python 실행</h2><p class="muted">별도 컴파일 없음 · 문법 오류와 예외는 아래 오류·진단에서 확인하세요.</p></section>' : output("컴파일", result.compile)}${output("프로그램 실행", result.run)}` : ''}
<section><p>실습 종료는 서버 저장을 확인한 뒤 원격 실행공간을 삭제합니다. 서버 작업 저장본·공식 제출·점수는 보존됩니다. IDE의 공용 실습 종료를 선택하면 로컬 임시 파일도 정리합니다.</p><p class="muted">연결이 끊기거나 강제 종료되면 원격 삭제 완료를 즉시 확인할 수 없습니다. 서버의 만료·정리 정책에 따라 종료되며, 저장 확인된 코드까지만 복원할 수 있습니다.</p></section>
</main><script nonce="${safeNonce}">const vscode=acquireVsCodeApi();const read=()=>({language:document.getElementById('language').value,sources:Array.from(document.getElementById('sources').selectedOptions).map(o=>o.value),stdin:document.getElementById('stdin').value});document.querySelectorAll('button[data-action]').forEach(b=>b.addEventListener('click',()=>{if(!b.disabled)vscode.postMessage({type:'remoteLearning',action:b.dataset.action,input:read()});}));document.getElementById('language').addEventListener('change',()=>vscode.postMessage({type:'remoteLearning',action:'language',input:read()}));['stdin','sources'].forEach(id=>document.getElementById(id).addEventListener('input',()=>vscode.postMessage({type:'remoteLearning',action:'input',input:read()})));</script></body></html>`;
}
