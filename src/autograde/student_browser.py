"""Small, hash-authorized student enhancements; no credentials are persisted."""
import base64
import hashlib

SCRIPT = r"""(() => {
  'use strict';
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
SCRIPT_CSP = f"; script-src 'sha256-{SCRIPT_HASH}'"
SCRIPT_TAG = '<script data-student-enhancement>' + SCRIPT + '</script>'
