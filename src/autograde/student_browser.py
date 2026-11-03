"""Small, hash-authorized student enhancements; no credentials are persisted."""
import base64
import hashlib

SCRIPT = r"""(() => {
  'use strict';
  const privacy = document.querySelector('[data-privacy-prompt]');
  if (privacy && typeof privacy.showModal === 'function') {
    const choices = privacy.querySelector('[data-privacy-choice]');
    const status = privacy.querySelector('[data-privacy-status]');
    const later = privacy.querySelector('[data-privacy-later]');
    const originalFocus = document.activeElement;
    let ready = false, saving = false;
    const close = () => {privacy.close(); if (originalFocus && originalFocus.focus) originalFocus.focus();};
    const belongs = value => value && value.course_key === privacy.dataset.course && value.student_key === privacy.dataset.student;
    const read = async (endpoint, options = {}) => {
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(endpoint, {credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...options, signal: controller.signal});
        if (!response.ok || !(response.headers.get('content-type') || '').includes('application/json')) throw new Error('not confirmed');
        const body = await response.json();
        if (body.ok !== true || !body.result) throw new Error('not confirmed');
        return body.result;
      } finally {clearTimeout(timer);}
    };
    const dismiss = () => {
      if (saving) return;
      close();
      if (!ready) return;
      const fields = new URLSearchParams({csrf: choices.elements.csrf.value, policy_version: choices.elements.policy_version.value});
      // This only dismisses this login session's reminder, never records a choice.
      void read(privacy.dataset.dismiss, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: fields.toString()}).catch(() => {});
    };
    later.addEventListener('click', dismiss);
    privacy.addEventListener('cancel', event => {event.preventDefault(); dismiss();});
    choices.addEventListener('submit', async event => {
      event.preventDefault();
      const decision = event.submitter && event.submitter.value;
      if (!ready || saving || !['granted', 'declined'].includes(decision)) return;
      saving = true;
      const buttons = privacy.querySelectorAll('button'); buttons.forEach(button => {button.disabled = true;});
      status.textContent = '선택을 저장하고 있습니다…';
      try {
        const fields = new URLSearchParams({csrf: choices.elements.csrf.value, policy_version: choices.elements.policy_version.value,
          revision: choices.elements.revision.value, decision});
        const result = await read(privacy.dataset.endpoint, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: fields.toString()});
        if (!belongs(result) || result.decision !== decision || !Number.isSafeInteger(result.revision)) throw new Error('different choice');
        close();
      } catch (_) {
        // A request may have committed even when its response was lost. Never replay.
        ready = false; choices.hidden = true;
        status.textContent = '저장 결과를 확인하지 못했습니다. 정보 활용 설정에서 현재 선택을 확인해 주세요. 수업은 계속 이용할 수 있습니다.';
      } finally {saving = false; buttons.forEach(button => {button.disabled = false;});}
    });
    void read(privacy.dataset.endpoint).then(result => {
      if (!belongs(result) || result.decision !== 'unanswered' || result.prompt_dismissed === true ||
          !result.policy || result.policy.version !== choices.elements.policy_version.value || !Number.isSafeInteger(result.revision) || result.revision < 0) return;
      choices.elements.revision.value = String(result.revision); ready = true;
      if (document.visibilityState === 'visible') privacy.showModal();
    }).catch(() => {}); // The independent settings tab remains available.
  }
  const form = document.querySelector('[data-assignment-picker]');
  if (form) {
    const summary = form.querySelector('[data-selected-assignment]');
    const button = form.querySelector('button[type=submit]');
    const refresh = () => {
      const chosen = form.querySelector('input[name=assignment_id]:checked');
      summary.textContent = chosen ? chosen.closest('label').querySelector('strong').textContent : '실습을 하나 선택하세요';
      button.disabled = !chosen;
    };
    form.addEventListener('change', refresh);
    window.addEventListener('pageshow', refresh);
    refresh();
  }
  document.querySelectorAll('[data-copy-target]').forEach(button => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    button.hidden = false;
    button.addEventListener('click', async () => {
      const status = document.querySelector('[data-copy-status]');
      try {
        if (!navigator.clipboard || !window.isSecureContext) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(target.textContent.trim());
        status.textContent = button.dataset.copyLabel + '를 복사했습니다. 실습 PC의 확장에 붙여넣으세요.';
      } catch (_) {
        const range = document.createRange(); range.selectNodeContents(target);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        status.textContent = '자동 복사를 사용할 수 없습니다. 선택된 텍스트를 길게 누르거나 Ctrl+C / ⌘C로 복사하세요.';
      }
    });
  });
  const timer = document.querySelector('[data-claim-expires]');
  if (timer) {
    const end = Date.parse(timer.dataset.claimExpires);
    let interval;
    const tick = () => {
      const seconds = Number.isFinite(end) ? Math.max(0, Math.ceil((end - Date.now()) / 1000)) : 0;
      timer.textContent = seconds ? `남은 시간 ${Math.floor(seconds / 60)}분 ${String(seconds % 60).padStart(2, '0')}초` : '수령 코드가 만료되었습니다. 다시 인증하여 새 코드를 받으세요.';
      if (!seconds) {
        timer.setAttribute('role', 'status');
        document.querySelectorAll('[data-copy-target="claim-code"]').forEach(button => { button.disabled = true; });
        clearInterval(interval);
      }
    };
    interval = setInterval(tick, 1000); tick();
    window.addEventListener('pageshow', tick);
  }
})();"""

SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
SCRIPT_CSP = f"; script-src 'sha256-{SCRIPT_HASH}'; connect-src 'self'"
SCRIPT_TAG = '<script data-student-enhancement>' + SCRIPT + '</script>'
