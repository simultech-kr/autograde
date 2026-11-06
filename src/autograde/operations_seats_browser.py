"""Read-only progressive seat monitoring; never submits a device policy."""

SCRIPT = r"""(() => {
  const panel = document.querySelector('#operations-seats');
  if (!panel) return;
  const body = panel.querySelector('table tbody');
  const template = panel.querySelector('[data-seat-row-template]');
  const search = panel.querySelector('[data-seat-search]');
  const status = panel.querySelector('[data-seat-refresh-status]');
  const refresh = panel.querySelector('[data-seat-refresh]');
  if (!body || !template || !search || !status || !refresh) return;
  const filters = ['all', 'attention', 'recent', 'disabled'];
  const tones = new Set(['neutral', 'info', 'success', 'warning', 'error']);
  const modes = new Set(['on_exit', 'next_start']);
  const states = new Set(['inactive', 'unknown', 'not_applicable', 'matched', 'pending_restart']);
  const text = (value, limit) => typeof value === 'string' && value.length <= limit;
  const date = value => value === null || (text(value, 40) && /^\d{4}-\d\d-\d\dT/.test(value) && Number.isFinite(Date.parse(value)));
  let filter = 'all', timer, controller, epoch = 0, failures = 0, paused = false, stopped = false, pageActive = true;
  const rows = () => [...body.querySelectorAll('tr[data-result-key]')];
  const record = row => row.querySelector('[data-seat-record]');
  const liveRows = () => rows().filter(row => row.dataset.seatRemoved !== 'true');
  const isDirty = row => {const select = row.querySelector('[data-seat-policy-select]'); return select && (select.dataset.seatDirty === 'true' || select.value !== select.dataset.serverValue);};
  const editing = () => rows().some(row => isDirty(row) || row.contains(document.activeElement));
  const applyFilters = () => {
    const query = search.value.trim().toLocaleLowerCase();
    const counts = {all: 0, attention: 0, recent: 0, disabled: 0};
    let shown = 0;
    for (const row of rows()) {
      const data = record(row)?.dataset;
      if (!data || row.dataset.seatRemoved === 'true') {row.hidden = true; continue;}
      counts.all++;
      const matches = {all: true, attention: row.dataset.attention === 'true', recent: data.seatRecent === 'true', disabled: data.seatActive === 'false'};
      for (const name of filters.slice(1)) if (matches[name]) counts[name]++;
      row.hidden = !matches[filter] || !(data.seatSearch || '').toLocaleLowerCase().includes(query);
      if (!row.hidden) shown++;
    }
    panel.querySelectorAll('[data-seat-count]').forEach(node => {node.textContent = String(counts[node.dataset.seatCount] ?? 0);});
    panel.querySelectorAll('[data-seat-filter]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.seatFilter === filter)));
    const message = panel.querySelector('[data-seat-filter-status]');
    if (message) message.textContent = `${counts.all}개 기기 중 ${shown}개 표시`;
    const empty = panel.querySelector('[data-seat-filter-empty]');
    if (empty) empty.hidden = shown > 0 || counts.all === 0;
    const none = panel.querySelector('[data-seat-empty]');
    if (none) none.hidden = counts.all > 0;
  };
  const reorder = () => {
    // Moving focused controls can lose keyboard focus in some browsers.
    if (editing()) return;
    for (const row of liveRows().sort((a,b) => Number(record(a)?.dataset.seatPriority || 0) - Number(record(b)?.dataset.seatPriority || 0)
      || (record(a)?.dataset.seatSearch || '').localeCompare(record(b)?.dataset.seatSearch || ''))) body.appendChild(row);
  };
  const valid = data => {
    if (!data || data.schema !== 'autograde-seats-status-v1' || typeof data.available !== 'boolean' || !date(data.observed_at)
      || !Array.isArray(data.seats) || data.seats.length > 10000) return false;
    const seen = new Set();
    return data.seats.every(item => {
      if (!item || !text(item.seat_id,64) || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(item.seat_id) || seen.has(item.seat_id)
        || !text(item.label,100) || ![item.active,item.attention,item.recent].every(value => typeof value === 'boolean')
        || !Number.isSafeInteger(item.priority) || item.priority < 0 || item.priority > 100
        || !modes.has(item.cleanup_mode) || !(item.reported_cleanup_mode === null || modes.has(item.reported_cleanup_mode))
        || !states.has(item.policy_state) || !date(item.last_seen_at)
        || !text(item.ide_version,64) || !text(item.extension_version,64)) return false;
      for (const [key, value] of Object.entries(item)) {
        if (key.endsWith('_label') && !text(value,400)) return false;
        if (key.endsWith('_tone') && !tones.has(value)) return false;
      }
      // Require every displayed field; a partial response must not erase old facts.
      for (const node of template.content.querySelectorAll('[data-seat-field]')) {
        if (!text(item[node.dataset.seatField],400)) return false;
      }
      for (const node of template.content.querySelectorAll('[data-seat-tone]')) {
        if (!tones.has(item[node.dataset.seatTone])) return false;
      }
      seen.add(item.seat_id); return true;
    });
  };
  const setPoliciesEnabled = enabled => rows().forEach(row => {
    row.querySelectorAll('select,button[type="submit"]').forEach(node => {node.disabled = !enabled || row.dataset.seatRemoved === 'true';});
  });
  const render = data => {
    const existing = new Map(rows().map(row => [row.dataset.resultKey,row]));
    const incoming = new Set(data.seats.map(item => item.seat_id));
    for (const [id,row] of existing) if (!incoming.has(id)) {
      row.dataset.seatRemoved = 'true'; row.hidden = true;
      row.querySelectorAll('select,button[type="submit"]').forEach(node => {node.disabled = true;});
    }
    for (const item of data.seats) {
      let row = existing.get(item.seat_id);
      if (!row) {
        row = template.content.querySelector('tr[data-result-key]').cloneNode(true);
        row.dataset.resultKey = item.seat_id;
        const select = row.querySelector('[data-seat-policy-select]');
        if (select) {
          select.id = 'seat-policy-' + item.seat_id;
          const label = row.querySelector('label[for]'); if (label) label.htmlFor = select.id;
          select.value = item.cleanup_mode; select.dataset.serverValue = item.cleanup_mode;
        }
        const form = row.querySelector('form');
        if (form) form.setAttribute('action', '/instructor/operations/seats/' + item.seat_id + '/policy');
        body.appendChild(row);
      }
      delete row.dataset.seatRemoved;
      row.dataset.attention = String(item.attention);
      const metadata = record(row);
      Object.assign(metadata.dataset, {seatId:item.seat_id,seatActive:String(item.active),seatRecent:String(item.recent),seatPriority:String(item.priority),seatSearch:item.label+' '+item.seat_id});
      row.querySelectorAll('[data-seat-field]').forEach(node => {node.textContent = item[node.dataset.seatField];});
      row.querySelectorAll('[data-seat-tone]').forEach(node => {node.dataset.tone = item[node.dataset.seatTone];});
      const select = row.querySelector('[data-seat-policy-select]');
      if (select) {
        const dirty = isDirty(row), focused = row.contains(document.activeElement);
        if (!dirty && !focused) select.value = item.cleanup_mode;
        select.dataset.serverValue = item.cleanup_mode;
      }
    }
    setPoliciesEnabled(true); reorder(); applyFilters();
    panel.dataset.seatCurrent = 'true';
    status.textContent = '서버 기기 보고를 갱신했습니다. 실행 정책 일치는 기기의 자기 보고이며 정리 성공과 별개입니다.'
      + (editing() ? ' 편집 중인 정책 선택은 유지했습니다.' : '');
  };
  const allowed = () => !stopped && !paused && pageActive && !document.hidden && navigator.onLine;
  const stopPending = () => {clearTimeout(timer); timer = null; epoch++; controller?.abort(); controller = null;};
  const schedule = delay => {clearTimeout(timer); timer = null; if (allowed()) timer = setTimeout(poll,delay);};
  const poll = async () => {
    if (!allowed() || controller) return;
    const generation = ++epoch; controller = new AbortController();
    const timeout = setTimeout(() => {if (generation === epoch) controller?.abort();},5000);
    try {
      const response = await fetch('/instructor/operations/seats/status', {method:'GET',credentials:'same-origin',cache:'no-store',redirect:'error',
        headers:{Accept:'application/json'},signal:controller.signal});
      if (generation !== epoch) return;
      if ([401,403,404].includes(response.status)) {
        stopped = true; refresh.disabled = true; setPoliciesEnabled(false); panel.dataset.seatCurrent = 'false';
        status.textContent = '로그인 또는 운영 권한을 확인할 수 없어 자동 확인을 중단했습니다. 교수자 화면을 다시 여세요.'; return;
      }
      if (!response.ok || !(response.headers.get('Content-Type') || '').toLowerCase().includes('application/json')) throw Error('unavailable');
      const raw = await response.text(); if (generation !== epoch) return;
      if (raw.length > 16777216) throw Error('too_large');
      const data = JSON.parse(raw);
      if (!valid(data) || !data.available) throw Error('unavailable');
      render(data); failures = 0;
    } catch (_) {
      if (generation !== epoch) return;
      failures++; panel.dataset.seatCurrent = 'false'; setPoliciesEnabled(false);
      status.textContent = failures >= 3 ? '기기 상태 자동 확인을 중단했습니다. 마지막 확인 내용이며, 연결 복구 후 지금 새로고침을 누르세요.'
        : '기기 상태를 확인하지 못했습니다. 표시된 내용은 마지막 확인 상태이며 정책 저장을 잠시 중지합니다.';
      if (failures >= 3) paused = true;
    } finally {
      clearTimeout(timeout);
      if (generation === epoch) {controller = null; schedule(failures ? Math.min(60000,15000 * 2 ** failures) : 15000);}
    }
  };
  panel.querySelector('[data-seat-controls]').hidden = false;
  search.addEventListener('input',applyFilters);
  panel.querySelectorAll('[data-seat-filter]').forEach(button => button.addEventListener('click',() => {
    if (filters.includes(button.dataset.seatFilter)) {filter = button.dataset.seatFilter; applyFilters();}
  }));
  panel.addEventListener('change',event => {
    if (event.target.matches('[data-seat-policy-select]')) {
      // A matching report from another operator must not discard unsaved intent.
      event.target.dataset.seatDirty = String(event.target.value !== event.target.dataset.serverValue);
      status.textContent = '정리 정책을 선택했습니다. 저장 버튼을 눌러야 서버에 반영됩니다.';
    }
  });
  panel.addEventListener('focusout',() => queueMicrotask(reorder));
  panel.addEventListener('submit',() => {pageActive = false; stopPending();});
  refresh.addEventListener('click',() => {if (!stopped) {paused = false; failures = 0; stopPending(); schedule(0);}});
  const resume = () => {
    stopPending(); panel.dataset.seatCurrent = 'false';
    if (!navigator.onLine && !stopped) {setPoliciesEnabled(false); status.textContent = '오프라인 · 마지막 확인 내용입니다. 연결되면 다시 확인합니다.';}
    if (allowed()) schedule(0);
  };
  document.addEventListener('visibilitychange',resume);
  window.addEventListener('online',resume); window.addEventListener('offline',resume);
  window.addEventListener('pagehide',() => {pageActive = false; stopPending();});
  window.addEventListener('pageshow',() => {pageActive = true; resume();});
  applyFilters(); schedule(15000);
})();"""
