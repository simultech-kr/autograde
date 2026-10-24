"""Classroom cards and student-confirmed IDE requests with snapshots and SSE.

This page also works when the optional Next.js deployment is disabled. No
student data is embedded in HTML. Commands are restricted to confirmed IDE work.
"""
from __future__ import annotations

import base64
import hashlib
import html
from urllib.parse import quote

from .instructor_browser import SCRIPT_HASH as INSTRUCTOR_SCRIPT_HASH


STYLE = r"""
#ag-classroom .cr-toolbar,#ag-classroom .cr-live,#ag-classroom .cr-summary,#ag-classroom .cr-tabs{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
#ag-classroom .cr-toolbar{align-items:end;margin:16px 0}#ag-classroom .cr-toolbar label{display:flex;flex:1 1 200px;max-width:460px;min-width:0;flex-direction:column;gap:6px}
#ag-classroom .cr-toolbar input,#ag-classroom .cr-toolbar select{width:100%;margin:0}
#ag-classroom .cr-live{justify-content:space-between;border-bottom:1px solid var(--border);padding:12px 0}
#ag-classroom .cr-live time,#ag-classroom .cr-muted{color:var(--muted);font-size:.875rem}
#ag-classroom .cr-summary{gap:0;border:1px solid var(--border);margin:16px 0}#ag-classroom .cr-summary>span{flex:1 1 145px;padding:14px 18px}#ag-classroom .cr-summary strong{display:block;font-size:1.3rem}
#ag-classroom .cr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,205px),1fr));gap:12px}
#ag-classroom button.cr-card{--signal:var(--muted);display:flex;flex-direction:column;align-items:stretch;gap:12px;text-align:left;padding:16px;margin:0;min-width:0;border:1px solid var(--border);border-top:4px solid var(--signal);background:var(--surface);color:var(--text);font-weight:400;border-radius:3px}
#ag-classroom button.cr-card[data-state=online]{--signal:var(--success)}#ag-classroom button.cr-card[data-state=stale]{--signal:var(--warning)}
#ag-classroom .cr-key{font-weight:750;font-size:1.05rem}#ag-classroom .cr-name{margin-top:-10px;color:var(--muted)}
#ag-classroom .cr-connection{display:flex;align-items:center;gap:7px;font-size:.875rem;font-weight:650;color:var(--signal)}
#ag-classroom .cr-icon{width:18px;flex-shrink:0;font-weight:750}#ag-classroom .cr-metric{display:flex;gap:8px;font-size:.875rem}#ag-classroom .cr-metric small{display:block;color:var(--muted);font-size:.75rem}
#ag-classroom .cr-notice{background:var(--secondary);border-left:3px solid var(--accent);padding:14px 16px;margin:16px 0}
#ag-classroom dialog{width:min(740px,calc(100% - 24px));max-height:calc(100dvh - 32px);padding:24px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text)}
#ag-classroom dialog::backdrop{background:rgb(0 0 0 / .5)}#ag-classroom .cr-dialog-header{display:flex;justify-content:space-between;gap:16px;align-items:start}#ag-classroom .cr-dialog-header>div{min-width:0}#ag-classroom .cr-dialog-header button{flex-shrink:0}#ag-classroom .cr-dialog-header h3{margin:0 0 4px}
#ag-classroom .cr-tabs{border-bottom:1px solid var(--border);margin:12px 0 20px;padding-bottom:12px}#ag-classroom .cr-tabs button{background:var(--secondary);color:var(--text);margin:0}#ag-classroom .cr-tabs button[aria-pressed=true]{background:var(--accent);color:var(--on-accent)}
#ag-classroom .cr-facts{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,3fr);gap:10px 16px;margin:12px 0 24px}#ag-classroom .cr-facts dt{color:var(--muted)}#ag-classroom .cr-facts dd{margin:0;overflow-wrap:anywhere}
#ag-classroom .cr-history{list-style:none;padding:0}#ag-classroom .cr-history li{display:flex;flex-wrap:wrap;gap:8px 16px;padding:12px 0;border-top:1px solid var(--border)}#ag-classroom .cr-history li>*{flex:1 1 200px;overflow-wrap:anywhere}#ag-classroom .cr-history code{font-size:.8rem}
#ag-classroom .cr-command-form{display:flex;flex-direction:column;gap:12px;border:1px solid var(--border);padding:16px;margin:16px 0}#ag-classroom .cr-command-form label{display:flex;flex-direction:column;gap:6px;min-width:0}#ag-classroom .cr-command-form select,#ag-classroom .cr-command-form textarea{width:100%;min-width:0;padding:10px;font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border)}#ag-classroom .cr-command-form textarea{resize:vertical}#ag-classroom .cr-command-form button{align-self:flex-start}
#ag-classroom .cr-commands{list-style:none;padding:0}#ag-classroom .cr-commands>li{padding:16px 0;border-top:1px solid var(--border);overflow-wrap:anywhere}#ag-classroom .cr-command-heading{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px 20px}#ag-classroom .cr-command-message{white-space:pre-wrap}
#ag-classroom [hidden]{display:none!important}#ag-classroom button:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
@media(max-width:480px){#ag-classroom dialog{padding:16px}#ag-classroom .cr-tabs{gap:6px}#ag-classroom .cr-tabs button{padding:8px 10px}#ag-classroom .cr-facts{grid-template-columns:1fr;gap:4px}#ag-classroom .cr-facts dd{margin-bottom:12px}}
"""

SCRIPT = r"""(() => {
  'use strict';
  const root = document.querySelector('[data-classroom-enhancement]');
  if (!root || !window.fetch || !window.AbortController) return;
  const q = selector => root.querySelector(selector);
  const course = root.dataset.courseKey;
  if (!/^[a-z0-9_-]+$/.test(course || '')) return;
  const prefix = '/instructor/api/v1/courses/' + encodeURIComponent(course) + '/classroom';
  const grid = q('.cr-grid'), dialog = q('dialog'), content = q('[data-cr-detail]');
  const assignmentSelect = q('[data-cr-assignment]'), filter = q('[data-cr-filter]'), search = q('[data-cr-search]');
  const labels = {online:'IDE 연결',stale:'신호 지연',offline:'연결 끊김',never_connected:'연결 기록 없음'};
  const activities = {workspace_opened:'과제 열기',file_saved:'실습 파일 저장',submission_started:'제출 시작',submission_finished:'제출 응답 확인',assignment_downloaded:'과제 다운로드',material_downloaded:'자료 다운로드',content_synced:'강의자료 동기화',session_started:'IDE 접속',session_ended:'IDE 연결 종료',heartbeat:'연결 신호'};
  const commandLabels = {notice:'공지 전달',open_assignment:'과제 열기',sync_materials:'자료 동기화',check_environment:'환경 점검',save_submit:'저장·제출'};
  const commandStates = {queued:'요청됨 · IDE 수신 대기',received:'IDE 수신 · 학생 확인 대기',accepted:'학생 수락 · 처리 중',completed:'처리 완료',declined:'학생 거절',failed:'처리 실패',expired:'요청 만료'};
  const commandResults = {notice_acknowledged:'학생이 공지를 확인했습니다.',assignment_opened:'학생 IDE에서 과제를 열었습니다.',materials_synced:'강의자료 변경 파일 동기화를 완료했습니다.',environment_checked:'환경 점검 결과를 수신했습니다.',submission_received:'서버가 공식 제출을 접수했습니다. 채점 완료와는 다릅니다.',student_declined:'학생이 요청을 거절했습니다.',cancelled:'학생이 작업을 취소했습니다.',action_failed:'작업을 완료하지 못했습니다.',scope_changed:'학생 계정·서버·과제 연결이 변경되었습니다.',workspace_unavailable:'과제 폴더를 확인할 수 없습니다.',unsupported:'현재 IDE가 이 요청을 지원하지 않습니다.',submission_unconfirmed:'공식 제출 접수 여부를 확인하지 못했습니다.'};
  const commandIntents = new Map(), commandPanel = q('[data-cr-command-panel]'), commandForm = q('[data-cr-command-form]'), commandKind = q('[data-cr-command-kind]'), commandText = q('[data-cr-command-message]'), commandSend = q('[data-cr-command-send]');
  let commandController = null, commandGeneration = 0, commandScope = '', commandFeedback = '';
  let detailPending = false, detailFailed = false;
  let assignment = new URL(location.href).searchParams.get('assignment_id') || '', data = null, selected = null, detail = null, tab = 'overview';
  let source, controller, detailController, timer, watchdog, generation = 0, detailGeneration = 0, attempts = 0, terminal = false, stream = 'connecting', lastButton = null;
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text != null) node.textContent = String(text); if (className) node.className = className; return node; };
  const icon = text => { const node = el('span',text,'cr-icon'); node.setAttribute('aria-hidden','true'); return node; };
  const timestamp = value => value ? new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) + ' KST' : '기록 없음';
  const activity = kind => kind ? activities[kind] || '수업 활동' : '활동 기록 없음';
  const formal = s => s.submission.state === 'assignment_required' ? '과제 선택 필요' : s.submission.state === 'not_assigned' ? '배정 대상 아님' : s.submission.submitted ? '공식 제출 접수' : '공식 미제출';
  const score = s => !s ? '제출 없음' : ['graded','published'].includes(s.state) && s.score != null && s.max_score != null ? `${s.score} / ${s.max_score}점` : ['failed','rejected','infra_failed','assessment_failed'].includes(s.state) ? '처리 오류 · 이력 확인' : '접수 완료 · 점수 미확정';
  const available = () => !document.hidden && navigator.onLine !== false && !terminal;
  const url = (suffix = '') => prefix + suffix + (assignment ? '?' + new URLSearchParams({assignment_id:assignment}) : '');
  function status(value) {
    stream = value;
    q('[data-cr-stream]').textContent = value === 'live' ? '● 실시간 수신' : value === 'paused' ? '수신 일시 중단' : value === 'denied' ? '접근 종료' : '실시간 연결 확인 중';
    q('[data-cr-stale]').hidden = value === 'live' || !data;
    q('[data-cr-detail-stale]').hidden = value === 'live';
  }
  function message(text) { q('[data-cr-error]').textContent = text || ''; q('[data-cr-error]').hidden = !text; }
  function stop() { generation++; source?.close(); source = null; controller?.abort(); clearTimeout(timer); clearTimeout(watchdog); }
  function close() { selected = null; detail = null; detailGeneration++; detailController?.abort(); commandGeneration++; commandController?.abort(); commandController = null; commandScope = ''; commandPanel.hidden = true; if (dialog.open) dialog.close(); content.replaceChildren(); lastButton?.focus(); }
  function denied(text) { terminal = true; stop(); close(); commandIntents.clear(); data = null; grid.replaceChildren(); q('[data-cr-summary]').replaceChildren(); q('[data-cr-auth]').hidden = false; status('denied'); message(text || '교수자 세션 또는 수업 권한이 변경되었습니다. 다시 인증해 주세요.'); }
  async function read(path, signal) {
    const response = await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',headers:{Accept:'application/json'},signal});
    let value; try { value = await response.json(); } catch (_) { throw new Error('서버 응답을 확인하지 못했습니다.'); }
    if (!response.ok) { const error = new Error(value?.error?.message || '요청을 처리하지 못했습니다.'); error.status = response.status; throw error; }
    if (value.contract_version !== '1' || value.course_key !== course || value.selected_assignment_id !== (assignment || null) || !/^[a-f0-9]{64}$/.test(value.version || '')) { const error = new Error('서버 응답 형식이 일치하지 않습니다. 서버와 웹 버전을 확인하세요.'); error.status = 403; throw error; }
    return value;
  }
  function cardBody(s, button) {
    button.dataset.state = s.connection.state;
    const connection = el('span',null,'cr-connection'); connection.append(icon('●'),el('span',labels[s.connection.state]));
    const recent = el('span',null,'cr-metric'), text = el('span',activity(s.activity?.kind));
    if (s.activity) text.append(el('small',timestamp(s.activity.received_at)));
    recent.append(icon('↗'),text);
    const submission = el('span',null,'cr-metric'); submission.append(icon(s.submission.submitted ? '✓' : '○'),el('span',formal(s)));
    button.replaceChildren(el('span',s.student_key,'cr-key'),...(s.student_name ? [el('span',s.student_name,'cr-name')] : []),connection,recent,submission);
  }
  function cards() {
    if (!data) return;
    const selectedStudents = [...data.students].sort((a,b) => a.student_key.localeCompare(b.student_key,'en')).filter(s => (!search.value || `${s.student_key} ${s.student_name}`.includes(search.value)) && (!assignment || filter.value === 'all' || (filter.value === 'submitted' ? s.submission.submitted : !s.submission.submitted && s.submission.state !== 'not_assigned')));
    const existing = new Map([...grid.children].map(node => [node.dataset.student,node]));
    for (const s of selectedStudents) {
      let button = existing.get(s.student_key);
      if (!button) { button = el('button',null,'cr-card'); button.type = 'button'; button.dataset.student = s.student_key; button.setAttribute('aria-haspopup','dialog'); const after = [...grid.children].find(n => n.dataset.student.localeCompare(s.student_key,'en') > 0); grid.insertBefore(button,after || null); }
      cardBody(s,button); existing.delete(s.student_key);
    }
    for (const button of existing.values()) button.remove();
    q('[data-cr-count]').textContent = `${selectedStudents.length}명 · 학번순 고정`;
    q('[data-cr-empty]').hidden = selectedStudents.length > 0;
  }
  function apply(value) {
    if (value.contract_version !== '1' || value.course_key !== course || value.selected_assignment_id !== (assignment || null) || !Array.isArray(value.students) || !Array.isArray(value.assignments) || !value.summary || !/^[a-f0-9]{64}$/.test(value.version || '')) throw new Error('서버 응답 형식이 일치하지 않습니다.');
    if (new Set(value.students.map(s => s.student_key)).size !== value.students.length || value.students.some(s => !labels[s.connection?.state] || !s.submission || typeof s.student_key !== 'string')) throw new Error('학생 상태 응답을 확인하지 못했습니다.');
    const changed = data?.version !== value.version; data = value; message(''); q('[data-cr-time]').textContent = '마지막 확인: ' + timestamp(value.server_time);
    if (!changed) return;
    const choices = [['','과제 선택 · 전체 수업 활동'],...value.assignments.map(a => [a.assignment_id,a.title || a.assignment_key])];
    if (assignment && !choices.some(([id]) => id === assignment)) choices.push([assignment,'선택한 과제 · 현재 조회 대상 없음']);
    assignmentSelect.replaceChildren(...choices.map(([id,title]) => { const option = el('option',title); option.value = id; return option; })); assignmentSelect.value = assignment;
    filter.disabled = !assignment;
    q('[data-cr-summary]').replaceChildren(...[['수강생',value.summary.total],['IDE 연결',value.summary.online],['신호 지연',value.summary.stale],['공식 제출',assignment ? value.summary.submitted : null]].map(([label,number]) => { const item = el('span',label); item.append(el('strong',number == null ? '과제 선택' : number + '명')); return item; }));
    cards();
    if (selected) {
      const current = data.students.find(s => s.student_key === selected);
      if (!current) close();
      else {
        if (detail && JSON.stringify(detail.student.submission) !== JSON.stringify(current.submission)) {
          detail = null; content.replaceChildren(el('p','현재 제출 기록을 다시 확인하고 있습니다…'));
        }
        void loadDetail();
      }
    }
  }
  function retry() {
    stop();
    if (terminal) return; status(available() ? 'reconnecting' : 'paused');
    if (available()) timer = setTimeout(connect,Math.min(30000,1000 * 2 ** Math.min(attempts++,5)));
  }
  function pulse() { clearTimeout(watchdog); watchdog = setTimeout(retry,45000); }
  async function connect() {
    if (!available()) { if (!terminal) status('paused'); return; }
    stop(); const token = generation; controller = new AbortController(); const current = controller;
    const timeout = setTimeout(() => current.abort(),15000);
    try {
      const value = await read(url(),current.signal); if (token !== generation || terminal) return; apply(value);
      if (!window.EventSource) { status('reconnecting'); timer = setTimeout(connect,5000); return; }
      source = new EventSource(url('/events'));
      source.addEventListener('snapshot',event => { if (token !== generation || terminal) return; try { apply(JSON.parse(event.data)); attempts = 0; status('live'); pulse(); } catch (_) { denied('서버 상태 응답을 확인하지 못했습니다. 다시 인증 후 연결해 주세요.'); } });
      source.addEventListener('heartbeat',() => { if (token === generation && !terminal) { status('live'); pulse(); } });
      source.addEventListener('access_revoked',() => { if (token === generation && !terminal) denied(); }); source.onerror = () => { if (token === generation && !terminal) retry(); }; pulse();
    } catch (error) {
      if (token !== generation || terminal) return;
      if ([401,403,404].includes(error.status)) denied(error.message);
      else { message('서버에 연결하지 못했습니다. 마지막 확인 상태를 표시하며 재연결합니다.'); retry(); }
    } finally { clearTimeout(timeout); }
  }
  const facts = entries => { const dl = el('dl',null,'cr-facts'); for (const [label,value] of entries) dl.append(el('dt',label),el('dd',value)); return dl; };
  const requestKey = () => 'req_' + [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2,'0')).join('');
  function commandFormState() {
    commandPanel.hidden = tab !== 'control' || !selected;
    q('[data-cr-command-history-stale]').hidden = tab !== 'control' || !(detailPending || detailFailed);
    if (!selected) return;
    const scope = selected + '/' + assignment;
    if (scope !== commandScope) {
      commandScope = scope; const prior = commandIntents.get(scope);
      commandKind.value = prior?.kind || 'notice'; commandText.value = prior?.message || '';
      commandFeedback = prior ? '이전 전송 결과가 확인되지 않았습니다. 같은 요청으로 다시 확인하세요.' : '';
    }
    const supported = detail?.student?.controls?.supported && root.dataset.csrf && !terminal;
    const actions = detail?.student?.controls?.actions || [];
    for (const option of commandKind.options) option.disabled = !actions.includes(option.value);
    const locked = !!commandController || commandIntents.has(scope);
    commandKind.disabled = !supported || locked; commandText.disabled = !supported || locked;
    commandText.required = commandKind.value === 'notice';
    commandSend.disabled = !supported || !!commandController || !actions.includes(commandKind.value) || (commandKind.value !== 'notice' && !assignment);
    commandSend.textContent = commandController ? '접수 확인 중…' : commandIntents.has(scope) ? '같은 요청 다시 확인' : '학생에게 요청';
    q('[data-cr-command-assignment]').textContent = '대상 과제: ' + (assignment || '미선택 · 공지만 가능') + ' · 요청 유효 시간 10분';
    q('[data-cr-command-save-warning]').hidden = commandKind.value !== 'save_submit';
    q('[data-cr-command-assignment-warning]').hidden = commandKind.value === 'notice' || !!assignment;
    q('[data-cr-command-feedback]').textContent = commandFeedback; q('[data-cr-command-feedback]').hidden = !commandFeedback;
  }
  commandKind.addEventListener('change',commandFormState);
  commandForm.addEventListener('submit',async event => {
    event.preventDefault();
    if (commandController || terminal || !selected || !detail?.student?.controls?.supported || !detail.student.controls.actions.includes(commandKind.value) || (commandKind.value !== 'notice' && !assignment)) return;
    const target = selected, scope = commandScope, token = ++commandGeneration;
    const payload = commandIntents.get(scope) || {csrf:root.dataset.csrf,kind:commandKind.value,message:commandText.value.trim(),...(assignment ? {assignment_id:assignment}:{}),request_key:requestKey()};
    commandIntents.set(scope,payload); commandController = new AbortController(); const current = commandController;
    commandFeedback = '서버 접수 확인 중…'; commandFormState(); const timeout = setTimeout(() => current.abort(),15000);
    try {
      const response = await fetch(prefix + '/students/' + encodeURIComponent(target) + '/commands',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',headers:{Accept:'application/json','Content-Type':'application/json'},body:JSON.stringify(payload),signal:current.signal});
      let result; try { result = await response.json(); } catch (_) { throw new Error('서버 응답을 확인하지 못했습니다.'); }
      if (!response.ok) { const error = new Error(result?.error?.message || '요청을 접수하지 못했습니다.'); error.status = response.status; throw error; }
      if (result.contract_version !== '1' || !result.command || !commandStates[result.command.status] || result.command.kind !== payload.kind || typeof result.duplicate !== 'boolean') throw new Error('요청 결과 형식을 확인하지 못했습니다.');
      commandIntents.delete(scope);
      if (token !== commandGeneration || selected !== target || terminal) return;
      commandText.value = ''; commandFeedback = result.duplicate ? '기존 요청을 확인했습니다. 새 요청을 중복 생성하지 않았습니다. 아래 이력에서 상태를 확인하세요.' : '요청을 접수했습니다. IDE 수신과 학생 확인 결과를 아래 이력에서 확인하세요.'; void loadDetail();
    } catch (error) {
      if (token !== commandGeneration || selected !== target || terminal) return;
      if ([401,403,404].includes(error.status)) denied(error.message);
      else if (error.status >= 400 && error.status < 500 && ![408,409,429].includes(error.status)) { commandIntents.delete(scope); commandFeedback = error.message; }
      else commandFeedback = '전송 결과를 확인하지 못했습니다. 같은 요청 다시 확인을 눌러 중복 없이 확인하세요.';
    } finally { clearTimeout(timeout); if (commandController === current) commandController = null; if (token === commandGeneration && selected === target && !terminal) commandFormState(); }
  });
  function commandHistory(commands) {
    const history = el('ol',null,'cr-commands');
    const checkKeys = {server:'서버 연결',workspace:'과제 폴더',compiler:'컴파일러',os:'운영체제',remote:'원격 환경'};
    const checkValues = {ok:'정상',failed:'확인 실패',missing:'없음',untrusted:'신뢰되지 않은 폴더',available:'사용 가능',not_checked:'점검하지 않음',macos:'macOS',linux:'Linux',windows:'Windows',other:'기타',none:'로컬',wsl:'WSL','ssh-remote':'SSH'};
    for (const command of commands) {
      const item = el('li'); item.dataset.commandId = command.command_id;
      const heading = el('div',null,'cr-command-heading'); heading.append(el('strong',commandLabels[command.kind] || 'IDE 요청'),el('span',command.status === 'expired' && command.accepted_at ? '만료 · 처리 결과 미확인' : commandStates[command.status] || '상태 확인 필요')); item.append(heading);
      if (command.message) item.append(el('p',command.message,'cr-command-message'));
      item.append(el('p','과제: ' + (command.assignment_id || '수업 공지') + ' · 요청자: ' + command.created_by,'cr-muted'));
      const result = commandResults[command.result_code] || (command.status === 'expired' ? command.accepted_at ? '수락 이후 결과가 도착하지 않았습니다. 이미 실행된 작업이 취소되었다는 뜻은 아닙니다.' : '요청 유효 시간이 지났습니다.' : '결과 대기');
      item.append(facts([['요청',timestamp(command.created_at)],['IDE 수신',timestamp(command.received_at)],['학생 확인',command.accepted_at ? '수락 · ' + timestamp(command.accepted_at) : command.status === 'declined' ? '거절 · ' + timestamp(command.finished_at) : '미확인'],['처리 결과',result + (command.finished_at ? ' · ' + timestamp(command.finished_at) : '')],['만료',timestamp(command.expires_at)]]));
      if (command.result?.submission_id) item.append(el('p','공식 제출 ID: ' + command.result.submission_id));
      if (command.result?.checks) { const checks = el('ul'); for (const [key,value] of Object.entries(command.result.checks)) checks.append(el('li',(checkKeys[key] || key) + ': ' + (checkValues[value] || value))); item.append(checks); }
      history.append(item);
    }
    return history;
  }
  function renderDetail() {
    commandFormState();
    if (!detail || !selected) return;
    const s = detail.student, latest = s.submission.latest, best = s.submission.previous_best;
    q('[data-cr-title]').textContent = `${s.student_key} ${s.student_name || ''}`;
    q('[data-cr-subtitle]').textContent = `${labels[s.connection.state]} · 마지막 신호 ${timestamp(s.connection.last_seen_at)}`;
    root.querySelectorAll('[data-cr-tab]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.crTab === tab)));
    const parts = [];
    if (tab === 'overview') {
      parts.push(el('h3','서버 코드 보관'),el('strong',s.code_storage.state === 'receipt_recorded' ? '서버 제출 접수 기록 있음' : '서버 제출 보관 기록 없음'),el('p',s.code_storage.message),el('p','제출 접수 기록과 현재 PC의 모든 코드 보관은 다릅니다. 서버가 검증하지 않은 보관 완료 상태는 표시하지 않습니다.','cr-muted'),el('h3','공식 제출'));
      parts.push(facts([['제출 상태',formal(s)],['최신 제출',timestamp(latest?.received_at)],['최신 결과',score(latest)],['이전 최고점',best ? `${best.score} / ${best.max_score}점 · 최신 제출 제외` : '이전 채점 기록 없음']]));
      parts.push(el('h3','제출 시각 → 코드 버전 → 점수'));
      const history = el('ul',null,'cr-history');
      for (const attempt of s.submissions) { const li = el('li'); li.append(el('strong',timestamp(attempt.received_at)),el('span',(attempt.submission_id === latest?.submission_id ? '최신 · ' : '') + score(attempt)),el('code',attempt.code_version || '코드 버전 기록 없음')); history.append(li); }
      parts.push(s.submissions.length ? history : el('p','공식 제출 이력이 없습니다.'),el('p','서버 보관 ≠ 공식 제출 ≠ 채점 완료'));
    } else if (tab === 'activity') {
      parts.push(el('h3','관측된 수업 활동'),facts([['과제 열기',s.analysis.workspace_opened + '회'],['파일 저장',s.analysis.file_saved + '회'],['제출 시도',s.analysis.submission_started + '회'],['제출 응답 확인',s.analysis.submission_finished + '회 · 공식 접수와 별도']]),el('p',s.analysis.note));
      const history = el('ol',null,'cr-history');
      for (const event of s.activities) { const li = el('li'); li.append(el('span',activity(event.kind)),el('time',timestamp(event.received_at))); history.append(li); }
      parts.push(history,el('p','IDE가 보고한 수업 활동만 표시합니다. 다른 앱·키 입력·웹 검색·화면은 수집하지 않습니다.','cr-muted'));
    } else {
      parts.push(el('h3','IDE 요청 이력'),el('p','요청됨 → IDE 수신 → 학생 수락 → 처리 결과. 수신은 학생 확인이나 실행 완료를 뜻하지 않습니다.','cr-muted'),commandHistory(s.commands || []));
      if (!s.commands?.length) parts.push(el('p','이 학생에게 보낸 IDE 요청이 없습니다.'));
    }
    content.replaceChildren(...parts);
  }
  async function loadDetail() {
    detailController?.abort(); const token = ++detailGeneration, target = selected;
    if (!target || terminal) return;
    detailPending = true; detailFailed = false; commandFormState();
    detailController = new AbortController(); const current = detailController;
    let timedOut = false; const timeout = setTimeout(() => { timedOut = true; current.abort(); },15000);
    try {
      const response = await read(url('/students/' + encodeURIComponent(target)),current.signal);
      if (token !== detailGeneration || selected !== target) return;
      if (response.student?.student_key !== target) { denied('학생 상세 응답이 일치하지 않습니다.'); return; }
      detail = response; renderDetail();
    } catch (error) {
      if (token !== detailGeneration || (current.signal.aborted && !timedOut)) return;
      if ([401,403,404].includes(error.status)) denied(error.message);
      else { detailFailed = true; if (!detail || tab !== 'control') content.replaceChildren(el('p','상세 기록을 불러오지 못했습니다. 다시 열어 확인해 주세요.')); }
    } finally { clearTimeout(timeout); if (token === detailGeneration) { detailPending = false; commandFormState(); } }
  }
  grid.addEventListener('click',event => {
    const button = event.target.closest('button[data-student]'); if (!button || !data || terminal) return;
    lastButton = button; selected = button.dataset.student; detail = null; tab = 'overview';
    q('[data-cr-title]').textContent = selected; q('[data-cr-subtitle]').textContent = '상세 기록 확인 중';
    content.replaceChildren(el('p','상세 기록을 확인하고 있습니다…')); if (!dialog.open) dialog.showModal(); void loadDetail();
  });
  q('[data-cr-close]').addEventListener('click',close); dialog.addEventListener('cancel',event => { event.preventDefault(); close(); });
  root.querySelectorAll('[data-cr-tab]').forEach(button => button.addEventListener('click',() => { tab = button.dataset.crTab; renderDetail(); }));
  assignmentSelect.addEventListener('change',() => {
    close(); stop(); assignment = assignmentSelect.value; data = null; grid.replaceChildren(); q('[data-cr-summary]').replaceChildren(); filter.value = 'all'; terminal = false;
    const address = new URL(location.href); if (assignment) address.searchParams.set('assignment_id',assignment); else address.searchParams.delete('assignment_id');
    history.replaceState(null,'',address.href); status('connecting'); void connect();
  });
  filter.addEventListener('change',cards); search.addEventListener('input',cards);
  q('[data-cr-refresh]').addEventListener('click',() => { if (!terminal) { attempts = 0; status('connecting'); void connect(); } });
  const visibility = () => { stop(); if (available()) void connect(); else if (!terminal) status('paused'); };
  document.addEventListener('visibilitychange',visibility); window.addEventListener('online',visibility); window.addEventListener('offline',visibility);
  window.addEventListener('pagehide',() => { stop(); close(); data = null; grid.replaceChildren(); });
  window.addEventListener('pageshow',event => { if (event.persisted) { terminal = false; data = null; close(); grid.replaceChildren(); void connect(); } });
  void connect();
})();"""

SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode("ascii")
SCRIPT_CSP = f"; script-src 'sha256-{INSTRUCTOR_SCRIPT_HASH}' 'sha256-{SCRIPT_HASH}'; connect-src 'self'"


def page(ui, course, session):
    """Return a course-scoped HTML shell; authentication is owned by the caller."""
    key = course["course_key"]
    escaped = html.escape(key, quote=True)
    csrf = html.escape(str((session or {}).get('csrf', '')), quote=True)
    auth = "/instructor/api/v1/entry?screen=classroom&course_key=" + quote(key, safe="")
    body = f'''<style>{STYLE}</style><section id="ag-classroom" data-classroom-enhancement data-course-key="{escaped}" data-csrf="{csrf}">
<p>학생 카드를 선택하면 코드·제출 기록과 수업 활동을 확인합니다. 학생 위치는 학번순으로 유지합니다.</p>
<p><a href="/courses/{escaped}/instructor/submissions">제출·채점 현황</a> · <a href="/app/classroom?course_key={escaped}">새 교실 화면</a></p>
<div class="cr-toolbar"><label>공식 제출 확인 과제<select data-cr-assignment><option value="">과제 선택 · 전체 수업 활동</option></select></label><button type="button" data-cr-refresh>다시 연결</button></div>
<div class="cr-live"><strong data-cr-stream role="status">실시간 연결 확인 중</strong><time data-cr-time>마지막 확인: 기록 없음</time></div>
<p class="cr-notice" data-cr-error role="alert" hidden></p><p data-cr-auth hidden><a href="{html.escape(auth, quote=True)}">교수자 인증</a></p>
<p class="cr-notice" data-cr-stale hidden>브라우저 수신이 중단되었거나 재연결 중입니다. 카드는 마지막 확인 상태이며 학생 IDE의 연결 종료를 뜻하지 않습니다.</p>
<div class="cr-summary" data-cr-summary></div>
<div class="cr-toolbar"><label>학생 검색<input data-cr-search placeholder="학번 또는 이름" maxlength="120"></label><label>제출 상태<select data-cr-filter disabled><option value="all">전체</option><option value="submitted">제출</option><option value="unsubmitted">미제출</option></select></label><span data-cr-count>명단 확인 중</span></div>
<div class="cr-grid" aria-label="학생별 현재 상태"></div><p data-cr-empty hidden>표시할 학생이 없습니다. 수강 명단 또는 검색 조건을 확인하세요.</p>
<p class="cr-muted">색상과 상태 이름을 함께 표시합니다. 접속 여부나 저장 횟수만으로 출석·집중도·성실도를 판단하지 않습니다.</p>
<dialog aria-labelledby="cr-student-title"><header class="cr-dialog-header"><div><h3 id="cr-student-title" data-cr-title>학생 상세</h3><p class="cr-muted" data-cr-subtitle></p></div><button type="button" data-cr-close>닫기</button></header>
<nav class="cr-tabs" aria-label="학생 상세 메뉴"><button type="button" data-cr-tab="overview" aria-pressed="true">코드·제출</button><button type="button" data-cr-tab="activity" aria-pressed="false">활동·분석</button><button type="button" data-cr-tab="control" aria-pressed="false">IDE 요청</button></nav>
<p class="cr-notice" data-cr-detail-stale hidden>실시간 수신 확인 중 · 마지막 확인 기록</p>
<section data-cr-command-panel hidden><h3>학생 IDE에 요청</h3><p>모든 요청은 학생이 확인한 뒤 실행합니다. 원격 명령 실행·PC 잠금·외부 앱 차단은 제공하지 않습니다.</p>
<p class="cr-muted">현재 인증한 과제만 처리합니다. 다른 과제 요청은 학생이 해당 과제 수령 코드로 다시 연결해야 수신할 수 있습니다.</p>
<form class="cr-command-form" data-cr-command-form><label>요청 기능<select aria-label="요청 기능" data-cr-command-kind><option value="notice">공지 전달</option><option value="open_assignment">과제 열기</option><option value="sync_materials">자료 동기화</option><option value="check_environment">환경 점검</option><option value="save_submit">저장·제출</option></select></label>
<p class="cr-muted" data-cr-command-assignment></p><label>학생 안내<textarea aria-label="학생 안내" data-cr-command-message rows="3" maxlength="1000" placeholder="학생이 요청을 이해하는 데 필요한 안내를 입력하세요."></textarea></label>
<p class="cr-notice" data-cr-command-save-warning hidden>학생 확인 후 현재 과제 코드를 저장·제출합니다. 이미 접수된 제출은 취소되지 않습니다.</p>
<p data-cr-command-assignment-warning hidden role="alert">팝업을 닫고 공식 제출 확인 과제를 먼저 선택하세요.</p>
<button type="submit" data-cr-command-send>학생에게 요청</button></form><p data-cr-command-feedback class="cr-notice" role="status" hidden></p></section>
<p class="cr-notice" data-cr-command-history-stale role="status" hidden>마지막 확인한 요청 이력입니다. 최신 학생 확인·처리 결과를 다시 조회하고 있습니다.</p><div data-cr-detail></div></dialog>
<noscript>학생 카드와 실시간 상태를 확인하려면 JavaScript를 활성화하세요. 제출 기록은 기존 제출·채점 현황에서도 조회할 수 있습니다.</noscript>
</section><script>{SCRIPT}</script>'''
    return "실습 교실 · 학생 활동", body
