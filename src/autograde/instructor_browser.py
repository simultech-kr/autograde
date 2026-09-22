"""Fixed, hash-authorized progressive enhancement; no browser persistence."""
import base64
import hashlib


SCRIPT = r"""(() => {
  'use strict';
  const guarded = [...document.querySelectorAll('form[data-dirty-guard]')];
  const baselines = new Map();
  let leaving = false;
  const fingerprint = form => JSON.stringify([...form.elements]
    .filter(el => el.name && !el.disabled && !['hidden', 'submit', 'button'].includes(el.type))
    .map(el => [el.name, el.type === 'file' ? [...el.files].map(f => [f.name, f.size, f.lastModified]) :
      ['checkbox', 'radio'].includes(el.type) ? el.checked : el.value]));
  const dirty = form => form.hasAttribute('data-recovered') || baselines.get(form) !== fingerprint(form);
  const anyDirty = () => guarded.some(dirty);
  const showDirty = form => {
    const notice = form.querySelector('[data-save-status]');
    if (notice) notice.textContent = dirty(form) ? '미저장 변경이 있습니다. 이 영역의 저장 버튼을 눌러 주세요.' : '입력 변경 없음';
  };

  document.querySelectorAll('[data-case-editor]').forEach(editor => {
    const form = editor.closest('form');
    const rows = [...editor.querySelectorAll('[data-case]')];
    const filled = row => [...row.querySelectorAll('input:not([type=checkbox]),textarea')].some(el => el.value !== '');
    const controls = row => [...row.querySelectorAll('input,textarea')];
    const clear = row => controls(row).forEach(el => { el.value = el.type === 'checkbox' ? 'yes' : ''; el.checked = false; });
    rows.forEach(row => { row.hidden = !filled(row); });
    if (rows.every(row => row.hidden)) rows[0].hidden = false;
    const refresh = () => {
      const ordered = [...editor.querySelectorAll('[data-case]')];
      const active = ordered.filter(row => !row.hidden);
      [...active, ...ordered.filter(row => row.hidden)].forEach((row, index) => {
        controls(row).forEach(el => {
          const name = el.name.replace(/^test_\d+_/, `test_${index}_`);
          const label = el.id && [...row.querySelectorAll('label')].find(l => l.htmlFor === el.id);
          if (el.id) el.id = name;
          if (label) label.htmlFor = name;
          el.name = name;
          el.disabled = row.hidden;
        });
        row.querySelector('summary').textContent = `케이스 ${index + 1}`;
        row.querySelector('[data-case-copy]').disabled = active.length >= 50;
      });
      editor.querySelector('[data-case-add]').disabled = active.length >= 50;
      const weights = active.map(row => row.querySelector('[name$="_weight"]'));
      const invalid = weights.some((el, index) => el.validity.badInput ||
        (el.value === '' && filled(active[index])) ||
        (el.value !== '' && (!Number.isFinite(Number(el.value)) || Number(el.value) <= 0 || Number(el.value) > 10000)));
      const total = weights.reduce((sum, el) => sum + Number(el.value || 0), 0);
      editor.querySelector('[data-case-total]').textContent = `${active.length} / 50개 · ` +
        (invalid || !Number.isFinite(total) ? '배점을 확인하세요' : `입력 배점 합계 ${Number(total.toPrecision(12))}점`);
    };
    const add = source => {
      const row = rows.find(r => r.hidden);
      if (!row) return;
      clear(row);
      if (source) controls(row).forEach((el, i) => { el.value = controls(source)[i].value; el.checked = controls(source)[i].checked; });
      row.hidden = false;
      row.open = true;
      editor.querySelector('[data-case-list]').append(row);
      refresh(); showDirty(form);
      row.querySelector('input').focus();
    };
    editor.querySelectorAll('[data-case-control]').forEach(el => { el.hidden = false; });
    editor.querySelector('[data-case-add]').addEventListener('click', () => add(null));
    rows.forEach(row => {
      row.querySelector('[data-case-copy]').addEventListener('click', () => add(row));
      row.querySelector('[data-case-remove]').addEventListener('click', () => {
        if ((filled(row) || row.querySelector('[type=checkbox]').checked) && !window.confirm('이 케이스를 입력 화면에서 삭제할까요? 저장 전까지 서버 자료는 유지됩니다.')) return;
        clear(row); row.hidden = true; refresh(); showDirty(form);
        editor.querySelector('[data-case-add]').focus();
      });
    });
    editor.addEventListener('input', refresh);
    refresh();
  });
  guarded.forEach(form => {
    if (form.hasAttribute('data-recovered')) {
      for (let parent = form.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }
    baselines.set(form, fingerprint(form)); showDirty(form);
    form.addEventListener('input', () => showDirty(form));
    form.addEventListener('change', () => showDirty(form));
  });
  window.addEventListener('beforeunload', event => {
    if (!leaving && anyDirty()) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('submit', event => {
    const otherDirty = guarded.some(form => form !== event.target && dirty(form));
    if (otherDirty && !window.confirm('다른 입력 영역에 저장하지 않은 변경이 있습니다. 계속하면 해당 변경이 사라집니다. 계속할까요?')) {
      event.preventDefault(); return;
    }
    leaving = true;
  });
  window.addEventListener('pageshow', () => { leaving = false; });

  document.querySelectorAll('[data-check-poll]').forEach(panel => {
    const message = panel.querySelector('[data-check-message]');
    const button = panel.querySelector('[data-check-toggle]');
    const checked = panel.querySelector('[data-check-time]');
    const labels = {queued: '검증 대기', running: '검증 중', succeeded: '검증 통과', failed: '검증 실패', interrupted: '검증 중단'};
    let timer, controller, failures = 0, paused = false, terminal = false, epoch = 0;
    const stop = () => { epoch++; clearTimeout(timer); if (controller) controller.abort(); };
    const schedule = (delay = 5000) => {
      clearTimeout(timer);
      if (!paused && !terminal && !document.hidden && navigator.onLine) timer = setTimeout(poll, delay);
    };
    const poll = async () => {
      if (paused || terminal || document.hidden || !navigator.onLine) return;
      const current = ++epoch;
      const abort = new AbortController(); controller = abort;
      const timeout = setTimeout(() => abort.abort(), 10000);
      try {
        const response = await fetch(panel.dataset.checkPoll, {credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal});
        if (current !== epoch) return;
        if ([401, 403, 404].includes(response.status)) {
          terminal = true; button.hidden = true;
          message.textContent = '자동 확인을 중단했습니다. 세션 또는 접근 권한을 확인하고 화면을 다시 열어 주세요.'; return;
        }
        if (!response.ok) throw new Error('status');
        const data = await response.json();
        if (current !== epoch) return;
        if (data.job_id !== panel.dataset.jobId || String(data.revision) !== panel.dataset.revision || !Object.hasOwn(labels, data.status)) throw new Error('response');
        failures = 0;
        checked.textContent = `마지막 확인: ${new Date().toLocaleTimeString('ko-KR')}`;
        if (data.draft_revision !== data.revision) {
          terminal = true; button.hidden = true;
          message.textContent = '자료 버전이 변경되었습니다. 현재 자료의 검증 결과는 새로고침으로 확인하세요.'; return;
        }
        message.textContent = labels[data.status] + ' · 서버 상태 확인됨';
        if (!['queued', 'running'].includes(data.status)) {
          terminal = true; button.hidden = true;
          if (!anyDirty()) window.location.reload();
          else message.textContent += ' · 입력을 저장한 뒤 새로고침하세요.';
          return;
        }
      } catch (_) {
        if (current !== epoch) return;
        failures++;
        message.textContent = '서버 상태를 확인하지 못했습니다. 표시된 결과는 마지막 확인 상태입니다.';
        if (failures >= 5) {
          paused = true; button.textContent = '자동 확인 다시 시작';
          message.textContent += ' 연결을 확인한 뒤 다시 시작하세요.';
        }
      } finally {
        clearTimeout(timeout);
        if (current === epoch) { controller = null; schedule(Math.min(60000, 5000 * 2 ** failures)); }
      }
    };
    button.hidden = false;
    button.addEventListener('click', () => {
      paused = !paused; stop(); failures = 0;
      button.textContent = paused ? '자동 확인 다시 시작' : '자동 확인 일시 중지';
      message.textContent = paused ? '자동 확인 일시 중지 · 서버 검증은 계속됩니다.' : '서버 상태를 다시 확인합니다.';
      schedule(0);
    });
    const resume = () => {
      stop();
      if (!terminal && !paused) {
        message.textContent = !navigator.onLine ? '오프라인 · 연결이 복구되면 다시 확인합니다.' : '화면이 활성화되면 서버 상태를 확인합니다.';
        schedule();
      }
    };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('online', resume); window.addEventListener('offline', resume);
    window.addEventListener('pagehide', stop); window.addEventListener('pageshow', resume);
    schedule();
  });
})();"""

SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode('utf-8')).digest()).decode('ascii')
SCRIPT_CSP = f"; script-src 'sha256-{SCRIPT_HASH}'; connect-src 'self'"
SCRIPT_TAG = '<script data-instructor-enhancement>' + SCRIPT + '</script>'
