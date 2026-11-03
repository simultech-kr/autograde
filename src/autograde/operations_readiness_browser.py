"""Progressive polling of cached administrator readiness; never starts checks."""

SCRIPT = r"""(() => {
  const panel = document.querySelector('[data-readiness-panel]');
  if (!panel) return;
  const overall = panel.querySelector('[data-readiness-overall]');
  const observed = panel.querySelector('[data-readiness-observed]');
  const submit = panel.querySelector('[data-readiness-submit]');
  const retry = panel.querySelector('[data-readiness-retry]');
  const ids = ['next', 'external', 'workers', 'backup'];
  const cards = new Map(ids.map(id => [id, panel.querySelector(`[data-readiness-card="${id}"]`)]));
  if (!overall || !observed || !retry || [...cards.values()].some(card => !card)) return;
  const states = {
    ok: ['정상', 'success'], error: ['문제 확인', 'error'], warning: ['확인 필요', 'warning'],
    unknown: ['미확인', 'neutral'], checking: ['점검 중', 'info'],
  };
  const labels = {
    not_checked: '아직 연결 점검을 실행하지 않았습니다.',
    checking: '연결을 점검하고 있습니다. 완료되면 결과가 갱신됩니다.',
    attention: '확인이 필요한 항목이 있습니다.',
    ready: '점검한 연결·작업자·백업 항목이 정상입니다.',
    stale: '이전 점검 결과입니다. 수업 시작 전에 다시 확인하세요.',
  };
  const text = (value, limit) => typeof value === 'string' && value.length <= limit;
  const date = value => value === null || (text(value, 40) && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)));
  const strings = value => Array.isArray(value) && value.length <= 16 && value.every(item => text(item, 1500));
  const valid = value => value && typeof value === 'object' && !Array.isArray(value) &&
    value.schema === 'autograde-classroom-readiness-v1' && Object.hasOwn(labels, value.state) &&
    typeof value.checking === 'boolean' && typeof value.stale === 'boolean' &&
    (value.state === 'checking') === value.checking && date(value.checked_at) &&
    Number.isInteger(value.fresh_for_seconds) && value.fresh_for_seconds >= 0 && value.fresh_for_seconds <= 300 &&
    Number.isInteger(value.retry_after_seconds) && value.retry_after_seconds >= 0 && value.retry_after_seconds <= 300 &&
    Array.isArray(value.checks) && value.checks.length === ids.length &&
    new Set(value.checks.map(item => item?.id)).size === ids.length &&
    value.checks.every(item => item && ids.includes(item.id) && Object.hasOwn(states, item.state) &&
      text(item.title, 120) && text(item.summary, 1500) && strings(item.details) && strings(item.actions) && date(item.checked_at));
  const timeLabel = value => {
    if (!value) return '아직 확인하지 않음';
    return new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'}).format(new Date(value)) + ' KST';
  };
  const list = (element, values) => {
    const items = values.map(value => { const item = document.createElement('li'); item.textContent = value; return item; });
    element.replaceChildren(...items);
  };
  let checking = panel.dataset.readinessChecking === 'true';
  let retryAfter = Number(panel.dataset.readinessRetryAfter) || 0;
  let timer = null, freshnessTimer = null, countdownTimer = null, expiryCheckedAt = null;
  let controller = null, epoch = 0, failures = 0, stopped = false, hiddenPage = false;
  let started = performance.now();
  const stopPending = () => {
    clearTimeout(timer); timer = null; epoch++;
    clearTimeout(freshnessTimer); freshnessTimer = null;
    clearTimeout(countdownTimer); countdownTimer = null;
    if (controller) controller.abort(); controller = null;
  };
  const allowed = () => !stopped && !hiddenPage && !document.hidden && navigator.onLine;
  const failure = message => {
    stopped = true; stopPending();
    overall.textContent = message;
    panel.dataset.readinessCurrent = 'false';
    if (submit) submit.disabled = true;
    retry.textContent = '결과 새로고침으로 로그인과 최신 상태를 확인한 뒤 다시 점검하세요.';
  };
  const watchCooldown = seconds => {
    clearTimeout(countdownTimer); countdownTimer = null;
    if (checking || !Number.isInteger(seconds) || seconds <= 0 || seconds > 300 || !allowed()) return;
    const deadline = performance.now() + seconds * 1000;
    const tick = () => {
      countdownTimer = null;
      if (!allowed()) return;
      const remaining = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
      retry.textContent = remaining > 0 ? `${remaining}초 후 다시 점검할 수 있습니다.` :
        '재점검 가능 여부를 서버에서 확인하고 있습니다.';
      // Counting down changes only the label. The server confirms eligibility.
      if (remaining > 0) countdownTimer = setTimeout(tick, 1000);
    };
    tick();
  };
  const watchFreshness = (checkedAt, freshFor, stale) => {
    clearTimeout(freshnessTimer); freshnessTimer = null;
    if (checking || stale || !checkedAt || checkedAt === expiryCheckedAt ||
        !Number.isInteger(freshFor) || freshFor < 0 || freshFor > 300 || !allowed()) return;
    // Use server-provided remaining lifetime, not the browser's wall clock.
    // One cached read at expiry can mark the old result stale, without probing.
    freshnessTimer = setTimeout(() => {
      expiryCheckedAt = checkedAt;
      started = performance.now();
      schedule(0);
    }, freshFor * 1000 + 100);
  };
  const render = data => {
    // Validate the complete response before changing any existing result.
    if (!valid(data)) throw new Error('invalid_readiness');
    checking = data.checking; retryAfter = data.retry_after_seconds;
    panel.dataset.readinessChecking = String(checking);
    panel.dataset.readinessState = data.state;
    panel.dataset.readinessCheckedAt = data.checked_at || '';
    panel.dataset.readinessRetryAfter = String(retryAfter);
    panel.dataset.readinessFreshFor = String(data.fresh_for_seconds);
    panel.dataset.readinessCurrent = String(!data.stale);
    overall.textContent = labels[data.state]; observed.textContent = timeLabel(data.checked_at);
    if (submit) { submit.disabled = checking || retryAfter > 0; submit.textContent = checking ? '점검 중…' : '준비 상태 점검'; }
    retry.textContent = checking ? '점검이 진행 중입니다. 새 요청을 만들지 않고 현재 결과를 확인합니다.' :
      retryAfter > 0 ? `${retryAfter}초 후 다시 점검할 수 있습니다.` :
      '점검은 요청할 때 실행합니다. 학생 자료나 서버 설정은 변경하지 않습니다.';
    watchCooldown(retryAfter);
    data.checks.forEach(item => {
      const card = cards.get(item.id), badge = card.querySelector('[data-readiness-badge]');
      card.dataset.readinessState = item.state;
      card.querySelector('[data-readiness-title]').textContent = item.title;
      badge.textContent = states[item.state][0]; badge.dataset.tone = states[item.state][1];
      card.querySelector('[data-readiness-summary]').textContent = item.summary;
      card.querySelector('[data-readiness-time]').textContent = timeLabel(item.checked_at);
      // Keep the <details>, its open state, and focused controls intact.
      list(card.querySelector('[data-readiness-details]'), item.details);
      list(card.querySelector('[data-readiness-actions]'), item.actions);
    });
    watchFreshness(data.checked_at, data.fresh_for_seconds, data.stale);
  };
  const schedule = delay => {
    clearTimeout(timer); timer = null;
    if (!allowed()) return;
    timer = setTimeout(poll, delay);
  };
  const poll = async () => {
    if (!allowed() || controller) return;
    if (performance.now() - started >= 60000) {
      failure('결과 자동 확인 시간이 지났습니다. 서버의 점검은 계속될 수 있으며 현재 상태는 확인되지 않았습니다.'); return;
    }
    const current = ++epoch;
    controller = new AbortController();
    const timeout = setTimeout(() => { if (current === epoch && controller) controller.abort(); }, 5000);
    try {
      const response = await fetch('/instructor/operations/readiness/status', {
        method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: {'Accept': 'application/json'}, signal: controller.signal,
      });
      if (current !== epoch) return;
      if ([401, 403, 404].includes(response.status)) {
        failure('로그인 또는 점검 권한을 확인할 수 없습니다. 자동 확인을 중단했습니다.'); return;
      }
      if (!response.ok || !(response.headers.get('Content-Type') || '').toLowerCase().includes('application/json')) throw new Error('readiness_unavailable');
      const raw = await response.text();
      if (current !== epoch) return;
      if (raw.length > 49152) throw new Error('readiness_too_large');
      const data = JSON.parse(raw);
      if (!valid(data)) { failure('점검 결과 형식을 확인할 수 없습니다. 이전 결과를 현재 상태로 판단하지 마세요.'); return; }
      render(data); failures = 0;
    } catch (_) {
      if (current !== epoch) return;
      failures++;
      panel.dataset.readinessCurrent = 'false';
      overall.textContent = '서버 상태를 확인하지 못했습니다. 표시된 결과는 마지막 확인 상태입니다.';
      if (failures >= 3) { failure('서버 연결을 확인하지 못해 자동 확인을 중단했습니다. 표시된 결과는 마지막 확인 상태입니다.'); return; }
    } finally {
      clearTimeout(timeout);
      if (current === epoch) {
        controller = null;
        if (failures) schedule(2000 * 2 ** (failures - 1));
        else if (checking) schedule(2000);
        else if (retryAfter > 0) schedule(retryAfter * 1000 + 100);
      }
    }
  };
  const resume = () => {
    if (stopped) return;
    stopPending();
    if (!allowed()) {
      if (!navigator.onLine) {
        panel.dataset.readinessCurrent = 'false';
        overall.textContent = '오프라인 · 표시된 결과는 마지막 확인 상태입니다. 연결되면 다시 확인합니다.';
      }
      return;
    }
    // Only re-read cached results on return; never submit another check.
    started = performance.now();
    schedule(0);
  };
  document.addEventListener('visibilitychange', resume);
  window.addEventListener('online', resume); window.addEventListener('offline', resume);
  window.addEventListener('pagehide', () => { hiddenPage = true; stopPending(); });
  window.addEventListener('pageshow', () => { if (hiddenPage) { hiddenPage = false; resume(); } });
  if (checking) schedule(2000);
  else if (retryAfter > 0 && retryAfter <= 300) schedule(retryAfter * 1000 + 100);
  watchCooldown(retryAfter);
  watchFreshness(panel.dataset.readinessCheckedAt, Number(panel.dataset.readinessFreshFor), panel.dataset.readinessState === 'stale');
})();"""
