"""Hash-authorized, read-only live updates for the instructor results screen."""
import base64
import hashlib


SCRIPT = r"""(() => {
  'use strict';
  if (!window.fetch || !window.AbortController || !window.DOMParser) return;
  const panel = document.querySelector('[data-live-results]');
  if (!panel) return;
  const content = panel.querySelector('[data-results-content]');
  const refresh = panel.querySelector('[data-results-refresh]');
  const toggle = panel.querySelector('[data-results-toggle]');
  const filter = panel.querySelector('[data-results-filter]');
  const assignment = panel.querySelector('[data-results-assignment]');
  const message = panel.querySelector('[data-results-message]');
  const checked = panel.querySelector('[data-results-time]');
  if (!content || !refresh || !toggle || !filter || !message || !checked) return;
  let endpoint;
  try {
    endpoint = new URL(panel.dataset.liveResults, window.location.href);
    const courseEndpoint = endpoint.pathname === `/courses/${encodeURIComponent(panel.dataset.courseKey)}/instructor/submissions/live`;
    const singleEndpoint = window.location.pathname === '/instructor' && endpoint.pathname === '/v1/instructor/dashboard/live';
    if (endpoint.origin !== window.location.origin || endpoint.username || endpoint.password ||
        (!courseEndpoint && !singleEndpoint) ||
        endpoint.search || endpoint.hash) return;
  } catch (_) { return; }
  const refreshLabel = refresh.textContent;
  const formatTime = time => `마지막 확인: ${new Date(time).toLocaleTimeString('ko-KR', {timeZone: 'Asia/Seoul'})} (KST)`;
  let timer, controller, requestTimeout, highlightTimer;
  let failures = 0, paused = false, terminal = false, navigating = false, epoch = 0;
  let pending = null, lastHTML = content.innerHTML, latestTime = -Infinity;
  const rows = () => [...content.querySelectorAll('[data-result-key]')];
  const summaries = () => [...content.querySelectorAll('[data-result-assignment-summary]')];
  const initial = new URL(window.location.href).searchParams;
  let selectedAssignment = assignment ? (initial.get('assignment_id') || '') : '';
  const views = new Set(['all', 'submitted', 'pending', 'attention', 'unsubmitted']);
  if (views.has(initial.get('result_view'))) filter.value = initial.get('result_view');
  const updateAssignments = () => {
    if (!assignment) return;
    const choices = [['', '전체 과제'], ...summaries().map(item => [item.dataset.assignmentId, item.dataset.assignmentLabel])];
    if (selectedAssignment && !choices.some(([id]) => id === selectedAssignment)) {
      // A deleted/archived selection must not silently expand to the whole course.
      choices.push([selectedAssignment, '선택한 과제 · 현재 조회 대상 없음']);
    }
    assignment.replaceChildren(...choices.map(([value, label]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = label;
      return option;
    }));
    assignment.value = selectedAssignment;
    assignment.disabled = false;
  };
  const syncAddress = () => {
    const url = new URL(window.location.href);
    if (selectedAssignment) url.searchParams.set('assignment_id', selectedAssignment);
    else url.searchParams.delete('assignment_id');
    if (filter.value !== 'all') url.searchParams.set('result_view', filter.value);
    else url.searchParams.delete('result_view');
    refresh.href = url.href;
    try { window.history.replaceState(null, '', url.href); } catch (_) { /* Filtering still works without history access. */ }
  };
  const stop = () => {
    epoch++;
    clearTimeout(timer); clearTimeout(requestTimeout);
    if (controller) controller.abort();
    controller = null;
  };
  const available = () => !terminal && !navigating && !document.hidden && navigator.onLine !== false;
  const schedule = (delay = 5000) => {
    clearTimeout(timer);
    if (!paused && available()) timer = setTimeout(() => poll(), delay);
  };
  const applyFilter = () => {
    let visible = 0;
    const predicates = {
      all: () => true,
      submitted: row => row.dataset.submitted === 'yes',
      pending: row => row.dataset.pending === 'yes',
      attention: row => row.dataset.attention === 'yes',
      unsubmitted: row => row.dataset.submitted === 'no'
    };
    const matches = predicates[filter.value] || predicates.all;
    rows().forEach(row => {
      row.hidden = Boolean(selectedAssignment && row.dataset.assignmentId !== selectedAssignment) || !matches(row);
      if (!row.hidden) visible++;
    });
    summaries().forEach(item => { item.hidden = Boolean(selectedAssignment && item.dataset.assignmentId !== selectedAssignment); });
    const courseSummary = content.querySelector('[data-results-course-summary]');
    if (courseSummary) courseSummary.hidden = Boolean(selectedAssignment);
    const selection = content.querySelector('[data-results-selection]');
    if (selection) selection.textContent = selectedAssignment ?
      `선택한 과제 · 현재 명단 ${visible}명 · 제출·미제출 인원은 보기 조건과 관계없이 해당 과제 전체를 기준으로 표시합니다.` :
      `전체 과제 · 현재 명단 ${visible}건 · 같은 학생도 과제마다 별도 행으로 표시합니다.`;
    content.querySelectorAll('[data-results-choice]').forEach(link => {
      const params = new URL(link.href, window.location.href).searchParams;
      if (selectedAssignment === params.get('assignment_id') && filter.value === params.get('result_view')) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
    const empty = content.querySelector('[data-results-empty-filter]');
    if (empty) empty.hidden = visible !== 0;
  };
  const busy = () => {
    const selection = window.getSelection();
    return content.contains(document.activeElement) ||
      Boolean(selection && !selection.isCollapsed && String(selection).trim());
  };
  const signature = row => row.dataset.version || JSON.stringify([row.innerHTML, row.dataset.submitted, row.dataset.pending, row.dataset.attention]);
  const apply = update => {
    const before = new Map(rows().map(row => [row.dataset.resultKey, signature(row)]));
    const open = new Set([...content.querySelectorAll('details[id]')].filter(el => el.open).map(el => el.id));
    const active = content.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    const scrollX = window.scrollX, scrollY = window.scrollY;
    const anchor = scrollY > 0 && rows().find(row => !row.hidden && row.getBoundingClientRect().bottom > 0 &&
      row.getBoundingClientRect().top < window.innerHeight);
    const anchorKey = anchor && anchor.dataset.resultKey;
    const anchorTop = anchor && anchor.getBoundingClientRect().top;
    content.replaceChildren(...update.document.body.childNodes);
    content.querySelectorAll('details[id]').forEach(el => { el.open = open.has(el.id); });
    updateAssignments();
    applyFilter();
    clearTimeout(highlightTimer);
    rows().forEach(row => {
      if (before.get(row.dataset.resultKey) !== signature(row)) row.classList.add('results-updated');
    });
    highlightTimer = setTimeout(() => rows().forEach(row => row.classList.remove('results-updated')), 2500);
    if (active) {
      const replacement = [...content.querySelectorAll('[data-focus-key]')].find(el => el.dataset.focusKey === active);
      if (replacement) replacement.focus({preventScroll: true});
    }
    const nextAnchor = anchorKey && rows().find(row => row.dataset.resultKey === anchorKey && !row.hidden);
    if (nextAnchor) window.scrollTo(scrollX, scrollY + nextAnchor.getBoundingClientRect().top - anchorTop);
    else window.scrollTo(scrollX, scrollY);
    lastHTML = update.html;
    pending = null;
    refresh.textContent = refreshLabel;
    checked.textContent = formatTime(update.time);
    message.textContent = paused ? '최신 결과를 반영했습니다. 자동 확인은 일시 중지 상태입니다.' : '최신 결과 반영됨 · 제출한 학생부터 표시합니다.';
  };
  const validate = data => {
    if (!data || data.course_key !== panel.dataset.courseKey || typeof data.html !== 'string' ||
        data.html.length > 4 * 1024 * 1024 || typeof data.generated_at !== 'string' ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(data.generated_at)) throw new Error('response');
    const time = Date.parse(data.generated_at);
    if (!Number.isFinite(time) || time < latestTime) throw new Error('timestamp');
    // The endpoint renders escaped server HTML. Reject active content as a second boundary.
    const parsed = new DOMParser().parseFromString(data.html, 'text/html');
    const allowed = new Set(['HTML', 'HEAD', 'BODY', 'DIV', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER',
      'H1', 'H2', 'H3', 'H4', 'P', 'SMALL', 'SPAN', 'STRONG', 'B', 'EM', 'I', 'CODE', 'PRE',
      'A', 'BR', 'HR', 'UL', 'OL', 'LI', 'DL', 'DT', 'DD', 'TABLE', 'CAPTION', 'COLGROUP',
      'COL', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TH', 'TD', 'DETAILS', 'SUMMARY', 'TIME']);
    for (const element of parsed.querySelectorAll('*')) {
      if (!allowed.has(element.tagName)) throw new Error('element');
      for (const attribute of element.attributes) {
        if (/^on/i.test(attribute.name) || ['src', 'srcdoc', 'style', 'formaction', 'action', 'is'].includes(attribute.name.toLowerCase())) throw new Error('attribute');
        if (attribute.name.toLowerCase() === 'href') {
          const url = new URL(attribute.value, window.location.href);
          if (!['http:', 'https:'].includes(url.protocol) || url.origin !== window.location.origin || url.username || url.password) throw new Error('link');
        }
      }
    }
    if (!parsed.body.querySelector('[data-results-empty-filter]')) throw new Error('fragment');
    return {html: parsed.body.innerHTML, time, document: parsed};
  };
  const poll = async (manual = false) => {
    if (!available() || (paused && !manual)) return;
    clearTimeout(timer);
    const current = ++epoch;
    const abort = new AbortController(); controller = abort;
    const timeout = setTimeout(() => abort.abort(), 10000); requestTimeout = timeout;
    try {
      const response = await fetch(endpoint.href, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal});
      if (current !== epoch) return;
      if ([401, 403, 404].includes(response.status) || (response.status >= 400 && response.status < 500 && response.status !== 429)) {
        terminal = true; pending = null; toggle.hidden = true; refresh.textContent = refreshLabel;
        message.textContent = '자동 확인을 중단했습니다. 세션 또는 접근 권한을 확인하고 화면을 다시 열어 주세요.';
        return;
      }
      if (!response.ok) throw new Error('status');
      const data = await response.json();
      if (current !== epoch) return;
      const update = validate(data);
      failures = 0; latestTime = update.time;
      if (lastHTML === update.html) {
        pending = null; refresh.textContent = refreshLabel;
        checked.textContent = formatTime(update.time);
        message.textContent = paused ? '최신 상태를 확인했습니다. 자동 확인은 일시 중지 상태입니다.' : '최신 상태 확인됨 · 제출한 학생부터 표시합니다.';
      } else if (!manual && busy()) {
        pending = update; refresh.textContent = '새 결과 적용';
        message.textContent = '새 결과가 있습니다. 확인 중인 내용을 유지합니다. 새 결과 적용을 누르면 목록이 갱신됩니다.';
      } else apply(update);
    } catch (_) {
      if (current !== epoch) return;
      failures++;
      message.textContent = '새 결과를 확인하지 못했습니다. 마지막으로 확인한 결과를 표시합니다.';
      if (failures >= 5) {
        paused = true; toggle.textContent = '자동 확인 다시 시작';
        message.textContent += ' 연결을 확인한 뒤 자동 확인을 다시 시작하세요.';
      }
    } finally {
      clearTimeout(timeout);
      if (current === epoch) { controller = null; schedule(Math.min(60000, 5000 * 2 ** failures)); }
    }
  };
  filter.addEventListener('change', () => { applyFilter(); syncAddress(); });
  if (assignment) assignment.addEventListener('change', () => {
    selectedAssignment = assignment.value;
    applyFilter(); syncAddress();
  });
  content.addEventListener('click', event => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
    const link = event.target.closest && event.target.closest('[data-results-choice]');
    if (!assignment || !link || !content.contains(link)) return;
    const params = new URL(link.href, window.location.href).searchParams;
    const selected = params.get('assignment_id'), view = params.get('result_view');
    if (!summaries().some(item => item.dataset.assignmentId === selected) || !views.has(view)) return;
    event.preventDefault();
    selectedAssignment = selected; filter.value = view;
    assignment.value = selectedAssignment;
    applyFilter(); syncAddress();
  });
  filter.disabled = false;
  refresh.addEventListener('click', event => {
    if (terminal || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
    event.preventDefault();
    if (pending) { apply(pending); return; }
    stop(); failures = 0;
    if (!available()) {
      message.textContent = '오프라인 · 연결이 복구되면 새 결과를 확인할 수 있습니다.';
      return;
    }
    message.textContent = '새 결과를 확인합니다.';
    poll(true);
  });
  toggle.hidden = false;
  toggle.textContent = '자동 확인 일시 중지';
  toggle.addEventListener('click', () => {
    if (terminal) return;
    paused = !paused; stop(); failures = 0;
    toggle.textContent = paused ? '자동 확인 다시 시작' : '자동 확인 일시 중지';
    message.textContent = paused ? '자동 확인 일시 중지 · 새로고침으로 결과를 확인할 수 있습니다.' : '새 결과를 다시 확인합니다.';
    schedule(0);
  });
  const resume = () => {
    stop();
    if (terminal || paused || navigating) return;
    message.textContent = navigator.onLine === false ? '오프라인 · 연결이 복구되면 다시 확인합니다.' :
      document.hidden ? '화면이 활성화되면 새 결과를 확인합니다.' : '5초마다 새 결과를 확인합니다.';
    schedule();
  };
  const leave = () => { navigating = true; pending = null; stop(); };
  document.addEventListener('visibilitychange', resume);
  window.addEventListener('online', resume); window.addEventListener('offline', resume);
  window.addEventListener('pagehide', leave);
  window.addEventListener('pageshow', () => { navigating = false; resume(); });
  document.addEventListener('submit', event => { if (!event.defaultPrevented) leave(); });
  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
    const link = event.target.closest && event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const destination = new URL(link.href, window.location.href);
    if (destination.origin !== window.location.origin || destination.pathname !== window.location.pathname || destination.search !== window.location.search) leave();
  });
  updateAssignments(); applyFilter(); syncAddress(); resume();
})();"""

SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode('utf-8')).digest()).decode('ascii')
SCRIPT_CSP = f"; script-src 'sha256-{SCRIPT_HASH}'; connect-src 'self'"
SCRIPT_TAG = '<script data-instructor-results-enhancement>' + SCRIPT + '</script>'
