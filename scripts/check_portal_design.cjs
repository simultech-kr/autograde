// Local-only production-browser QA for the synthetic portal design preview.
// No real credentials, students, API writes, or browser persistence are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.cache/code-oss/node_modules/playwright');

const target = new URL(process.argv[2] || 'http://127.0.0.1:18185/app/preview/portal');
assert.equal(target.protocol, 'http:');
assert.equal(target.hostname, '127.0.0.1');
assert.equal(target.pathname, '/app/preview/portal');
assert.ok(!target.username && !target.password && !target.search);
const output = path.resolve('build/portal-design-preview');
fs.mkdirSync(output, { recursive: true });
const checks = [], layouts = [], pageErrors = [], consoleErrors = [], cspErrors = [], prohibitedRequests = [];
const requests = [];
let browser;

async function ready() {
  const deadline = Date.now() + 60000;
  do {
    try { if ((await fetch(target, { signal: AbortSignal.timeout(2000) })).ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 500));
  } while (Date.now() < deadline);
  throw new Error('Local Next.js preview did not become ready within 60 seconds.');
}

async function createPage(width = 1440, colorScheme = 'light') {
  const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme, reducedMotion: 'reduce' });
  await context.route('**/*', route => {
    const req = route.request(), url = new URL(req.url());
    const allowed = req.method() === 'GET' && url.origin === target.origin &&
      (url.pathname === target.pathname || url.pathname.startsWith('/app/_next/') || url.pathname.endsWith('/favicon.ico'));
    requests.push({ method: req.method(), path: url.pathname, allowed });
    if (!allowed) { prohibitedRequests.push({ method: req.method(), path: url.pathname }); return route.abort(); }
    return route.continue();
  });
  await context.exposeBinding('reportPreviewCspViolation', (_source, detail) => cspErrors.push(detail));
  await context.addInitScript(() => document.addEventListener('securitypolicyviolation', event => {
    window.reportPreviewCspViolation({ directive: event.violatedDirective, blocked: event.blockedURI });
  }));
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  const response = await page.goto(target.href, { waitUntil: 'networkidle' });
  assert.equal(response.status(), 200);
  assert.match(response.headers()['cache-control'], /no-store/);
  assert.match(response.headers()['content-security-policy'], /nonce-/);
  await page.locator(`[data-theme="${colorScheme}"]`).waitFor();
  assert.match(await page.title(), /포털 디자인 미리보기/);
  return { context, page };
}

async function screen(page, name) {
  const labels = { student: '학생 · 내 수업', instructor: '교수자 · 수강 명단', signup: '회원가입', enrollment: '수강 선택' };
  await page.getByRole('navigation', { name: '디자인 화면 선택' }).getByRole('button', { name: labels[name], exact: true }).click();
  assert.equal(await page.locator('main h1').count(), 1);
}

async function inspectLayout(page, name, colorScheme, width) {
  await page.evaluate(() => document.fonts.ready);
  const measured = await page.evaluate(() => {
    const visible = element => {
      const box = element.getBoundingClientRect(), style = getComputedStyle(element);
      return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const activeRoot = document.querySelector('dialog[open]') || document;
    const controls = [...activeRoot.querySelectorAll('button,input:not([type="hidden"]),select,a')].filter(visible);
    return {
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      clipped: controls.filter(element => {
        const box = element.getBoundingClientRect(); return box.left < -1 || box.right > innerWidth + 1;
      }).map(element => (element.getAttribute('aria-label') || element.textContent || element.tagName).trim()),
      undersized: controls.filter(element => !['radio', 'checkbox'].includes(element.type) && element.tagName !== 'A' &&
        element.getBoundingClientRect().height < 43.5).map(element => (element.textContent || element.tagName).trim()),
    };
  });
  assert.equal(measured.overflow, false, `${name}/${width}/${colorScheme}: document overflows`);
  assert.deepEqual(measured.clipped, [], `${name}/${width}/${colorScheme}: clipped controls`);
  assert.deepEqual(measured.undersized, [], `${name}/${width}/${colorScheme}: undersized controls`);
  await page.screenshot({ path: path.join(output, `${name}-${colorScheme}-${width}.png`), fullPage: true });
  layouts.push({ name, colorScheme, width, passed: true, ...measured });
}

function course(page, title) { return page.locator('main article').filter({ has: page.getByRole('heading', { name: title, exact: true }) }); }
function dialog(page) { return page.getByRole('dialog'); }

async function verifyLayouts() {
  for (const colorScheme of ['light', 'dark']) for (const width of [1440, 375]) {
    const { context, page } = await createPage(width, colorScheme);
    for (const name of ['student', 'signup', 'enrollment', 'instructor']) {
      if (name === 'enrollment') {
        await page.locator('[name=student_key]').fill('20269909');
        await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
        await page.getByRole('heading', { name: '수강할 수업 선택', exact: true }).waitFor();
      } else await screen(page, name);
      await inspectLayout(page, name, colorScheme, width);
    }
    await page.getByRole('button', { name: '수강 명단 등록', exact: true }).click();
    await dialog(page).getByRole('button', { name: '파일 없이 예시 명단 불러오기' }).click();
    await inspectLayout(page, 'roster-dialog', colorScheme, width);
    for (let tab = 0; tab < 8; tab++) {
      await page.keyboard.press('Tab');
      // Chromium briefly reports BODY while Tab traverses browser chrome.
      // Background document controls must remain inert throughout the cycle.
      assert.equal(await page.evaluate(() => document.activeElement === document.body ||
        Boolean(document.activeElement?.closest('dialog[open]'))), true, 'Modal must not focus background controls');
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(await page.getByRole('button', { name: '수강 명단 등록', exact: true }).evaluate(element => element === document.activeElement), true);
    await page.reload({ waitUntil: 'networkidle' });
    await screen(page, 'student');
    await page.getByRole('button', { name: /COME3105 · WEEK 08/ }).click();
    await inspectLayout(page, 'assignment-dialog', colorScheme, width);
    await page.keyboard.press('Escape');
    await context.close();
  }
  checks.push('Four screens and two dialogs fit desktop/mobile in light/dark themes; modal keyboard traversal skips background controls and restores focus on Escape');
}

async function verifyFlows() {
  const { context, page } = await createPage();
  assert.match(await course(page, '소프트웨어 설계').innerText(), /명단 확인 대기/);
  await page.getByRole('combobox', { name: '과제 상태' }).selectOption('done');
  assert.equal(await page.getByRole('button', { name: /COME3105 · WEEK 09/ }).count(), 0);
  await page.getByRole('button', { name: /COME3105 · WEEK 08/ }).click();
  assert.match(await dialog(page).innerText(), /파일 입출력과 오류 처리/);
  assert.match(await dialog(page).getByText('이번 제출 점수', { exact: true }).locator('..').innerText(), /80/);
  assert.match(await dialog(page).getByText('이전 제출 중 최고점', { exact: true }).locator('..').innerText(), /95/);
  for (const state of ['received', 'queued', 'running', 'failed']) {
    await dialog(page).getByRole('combobox', { name: '제출 상태 체험' }).selectOption(state);
    assert.match(await dialog(page).getByText('이번 제출 점수', { exact: true }).locator('..').innerText(), /점수 미확정/);
    assert.match(await dialog(page).getByText('이전 제출 중 최고점', { exact: true }).locator('..').innerText(), /95/);
  }
  checks.push('Resubmission receipt, queue, grading and failure never reuse a previous score as the current result');
  await dialog(page).getByRole('button', { name: '수령 코드 발급 화면 보기' }).click();
  assert.match(await dialog(page).innerText(), /DEMO · 사용 불가/);
  await page.keyboard.press('Escape');
  await page.getByRole('combobox', { name: '과제 상태' }).selectOption('open');
  assert.equal(await page.getByRole('button', { name: /COME3105 · WEEK 08/ }).count(), 0);
  await page.getByRole('button', { name: /COME3105 · WEEK 09/ }).click();
  assert.match(await dialog(page).innerText(), /프로세스 생성과 실행/);
  assert.match(await dialog(page).innerText(), /아직 제출한 기록이 없습니다/);
  assert.equal(await dialog(page).getByText('이전 제출 중 최고점', { exact: true }).count(), 0);
  await page.keyboard.press('Escape');
  checks.push('Assignment filters and separate task dialogs distinguish unsubmitted process exercise from latest80/previous95 file exercise');
  await screen(page, 'instructor');
  assert.equal(await page.locator('tbody tr').count(), 8);
  await page.getByRole('textbox', { name: '학번 검색' }).fill('does-not-exist');
  await page.getByRole('heading', { name: '검색 결과가 없습니다.' }).waitFor();
  await page.getByRole('button', { name: '검색 초기화' }).click();
  assert.equal(await page.locator('tbody tr').count(), 8);
  await page.locator('[aria-label="연결 상태 필터"]').getByRole('button', { name: /^명단 대기/ }).click();
  assert.equal(await page.locator('tbody tr').count(), 2);
  await page.locator('[aria-label="연결 상태 필터"]').getByRole('button', { name: /^전체/ }).click();
  await page.getByRole('button', { name: '20269903 학생 상세 보기' }).click();
  assert.match(await dialog(page).innerText(), /수강 허용 명단에 학번이 없습니다/);
  await dialog(page).getByRole('button', { name: '수강 명단 추가하기' }).click();
  await dialog(page).getByRole('heading', { name: '수강 명단 등록', exact: true }).waitFor();
  await dialog(page).locator('input[type=file]').setInputFiles({ name: 'synthetic-invalid.csv', mimeType: 'text/csv', buffer: Buffer.from('student_key\n\nbad\n') });
  await dialog(page).getByRole('alert').filter({ hasText: '3행' }).waitFor();
  assert.equal(await dialog(page).getByRole('button', { name: '명단 추가 · 자동 연결 체험' }).isDisabled(), true);
  await dialog(page).locator('input[type=file]').setInputFiles({ name: 'synthetic-roster.csv', mimeType: 'text/csv', buffer: Buffer.from('student_key\n20269903\n20269907\n20269909\n20269903\n') });
  await dialog(page).getByText('중복 제외 3명 · 기존 명단에 추가').waitFor();
  await dialog(page).getByRole('button', { name: '명단 추가 · 자동 연결 체험' }).click();
  await page.getByRole('status').filter({ hasText: '대기 학생 2명' }).waitFor();
  assert.equal(await page.locator('tbody tr').count(), 9);
  assert.match(await page.locator('tbody tr').filter({ hasText: '20269903' }).innerText(), /수강 연결/);
  assert.match(await page.locator('tbody tr').filter({ hasText: '20269909' }).innerText(), /수강 신청 전/);
  checks.push('Instructor search/empty/reset/status filter and student detail work; invalid CSV reports original line; valid CSV deduplicates and matches pending students');
  await screen(page, 'student');
  const design = course(page, '소프트웨어 설계');
  assert.match(await design.innerText(), /수강 연결/);
  await design.getByRole('button', { name: '수업 열기' }).click();
  assert.equal(await dialog(page).count(), 0, 'Opening a course must not immediately open a single assignment');
  await page.getByRole('heading', { name: '소프트웨어 설계', exact: true }).waitFor();
  await page.getByRole('heading', { name: '강의자료', exact: true }).waitFor();
  await page.getByRole('button', { name: /책임 연쇄 패턴으로 요청 처리/ }).click();
  assert.match(await dialog(page).innerText(), /책임 연쇄 패턴으로 요청 처리/);
  assert.equal(await dialog(page).getByText('이전 제출 중 최고점', { exact: true }).count(), 0);
  await page.keyboard.press('Escape');
  checks.push('Instructor roster changes update current student course and open the matching pattern task');
  await screen(page, 'signup');
  await page.locator('[name=student_key]').fill('20269909');
  assert.equal(await page.getByRole('radio').count(), 0, 'Signup must not select any course');
  await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
  await page.getByRole('heading', { name: '수강할 수업 선택', exact: true }).waitFor();
  assert.match(await catalogRow(page, 'COME2201').innerText(), /명단 확인됨 · 선택 후 바로 연결/);
  assert.equal(await choice(page, 'COME2201').isChecked(), false);
  assert.equal(await page.getByRole('button', { name: '선택한 0개 수업 신청' }).isDisabled(), true);
  await page.getByRole('button', { name: '내 수업으로 이동', exact: true }).click();
  assert.equal(await page.locator('main article').count(), 0, 'Roster eligibility alone must not enroll on signup');
  await page.getByRole('button', { name: '수업 추가', exact: true }).click();
  await choice(page, 'COME2201').check();
  await page.getByRole('button', { name: '선택한 1개 수업 신청' }).click();
  assert.equal(await page.locator('main article').count(), 1);
  assert.match(await course(page, '소프트웨어 설계').innerText(), /수강 연결/);
  assert.equal(await page.getByRole('heading', { name: '새 강의자료', exact: true }).count(), 0);
  checks.push('Roster-first signup grants no course access until explicit course selection, then connects only the selected eligible course');
  assert.deepEqual(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });
  assert.equal((await context.cookies()).length, 0);
  await page.reload({ waitUntil: 'networkidle' });
  assert.match(await course(page, '소프트웨어 설계').innerText(), /명단 확인 대기/);
  checks.push('Preview uses no cookies or browser storage; reload restores the initial synthetic state');
  await context.close();
}

function choice(page, code) { return page.getByRole('checkbox', { name: new RegExp(code + ' 01분반 선택') }); }
function catalogRow(page, code) { return choice(page, code).locator('..').locator('..'); }

async function verifySignup() {
  for (const code of ['COME2201', 'COME3105']) {
    const { context, page } = await createPage(375, 'dark');
    await screen(page, 'signup');
    const input = page.locator('[name=student_key]');
    assert.equal(await page.getByRole('radio').count(), 0);
    assert.equal(await page.getByRole('checkbox').count(), 0);
    await input.fill('invalid');
    await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
    assert.equal(await input.getAttribute('aria-invalid'), 'true');
    assert.equal(await input.evaluate(element => element === document.activeElement), true);
    assert.match(await page.locator('#signup-error').innerText(), /숫자 4~20자리/);
    await input.fill('20269909');
    assert.equal(await input.getAttribute('aria-invalid'), 'false');
    await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
    await page.getByRole('heading', { name: '수강할 수업 선택', exact: true }).waitFor();
    assert.equal(await page.getByRole('checkbox').count(), 2);
    assert.equal(await page.getByRole('checkbox', { checked: true }).count(), 0);
    await page.getByRole('button', { name: '내 수업으로 이동', exact: true }).click();
    assert.equal(await page.locator('main article').count(), 0);
    await page.getByRole('heading', { name: '아직 신청한 수업이 없습니다.' }).waitFor();
    await page.getByRole('button', { name: '수업 추가', exact: true }).click();
    await choice(page, code).check();
    await page.getByRole('button', { name: '선택한 1개 수업 신청' }).click();
    assert.equal(await page.locator('main article').count(), 1);
    const title = code === 'COME2201' ? '소프트웨어 설계' : '시스템 프로그래밍';
    assert.match(await course(page, title).innerText(), /명단 확인 대기/);
    assert.equal(await page.getByRole('heading', { name: '새 강의자료', exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: /COME3105 · WEEK/ }).count(), 0);
    await page.getByRole('button', { name: '수업 추가', exact: true }).click();
    assert.equal(await choice(page, code).isChecked(), true);
    assert.equal(await choice(page, code).isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: '선택한 0개 수업 신청' }).isDisabled(), true);
    const other = code === 'COME2201' ? 'COME3105' : 'COME2201';
    assert.equal(await choice(page, other).isDisabled(), false);
    await context.close();
  }
  checks.push('Signup validates only account fields and creates zero enrollments; each subsequently selected course alone becomes pending and cannot be requested twice');
}

async function verifyMultiSelection() {
  const { context, page } = await createPage(375, 'light');
  // Existing memberships are visible, checked and disabled even before signup.
  await screen(page, 'enrollment');
  assert.equal(await choice(page, 'COME2201').isDisabled(), true);
  assert.equal(await choice(page, 'COME3105').isDisabled(), true);
  assert.match(await catalogRow(page, 'COME2201').innerText(), /명단 확인 대기/);
  assert.match(await catalogRow(page, 'COME3105').innerText(), /수강 중/);
  await screen(page, 'signup');
  await page.locator('[name=student_key]').fill('20269911');
  await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
  await choice(page, 'COME2201').check();
  await page.getByRole('textbox', { name: '수업 검색' }).fill('come3105');
  assert.equal(await page.getByRole('checkbox').count(), 1);
  await choice(page, 'COME3105').check();
  assert.equal(await page.getByRole('button', { name: '선택한 2개 수업 신청' }).isEnabled(), true);
  await page.getByRole('textbox', { name: '수업 검색' }).fill('없는 수업');
  await page.getByRole('heading', { name: '검색한 수업이 없습니다.' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '선택한 2개 수업 신청' }).isEnabled(), true, 'Hidden selections remain explicitly counted');
  await page.getByRole('button', { name: '수업 검색 초기화' }).click();
  assert.equal(await page.getByRole('checkbox', { checked: true }).count(), 2);
  await page.getByRole('button', { name: '선택한 2개 수업 신청' }).click();
  assert.equal(await page.locator('main article').count(), 2);
  for (const title of ['소프트웨어 설계', '시스템 프로그래밍']) assert.match(await course(page, title).innerText(), /명단 확인 대기/);
  await page.getByRole('button', { name: '수업 추가', exact: true }).click();
  assert.equal(await page.getByRole('checkbox', { checked: true }).count(), 2);
  for (const code of ['COME2201', 'COME3105']) assert.equal(await choice(page, code).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: '선택한 0개 수업 신청' }).isDisabled(), true);
  // A different synthetic account cannot inherit the previous student's choices.
  await screen(page, 'signup');
  await page.locator('[name=student_key]').fill('20269912');
  await page.getByRole('button', { name: '가입하고 수업 선택하기' }).click();
  assert.equal(await page.getByRole('checkbox', { checked: true }).count(), 0);
  for (const code of ['COME2201', 'COME3105']) assert.equal(await choice(page, code).isDisabled(), false);
  await context.close();
  checks.push('Current catalog supports search/empty-reset and multi-course selection across filters; existing memberships are disabled and choices never carry across student identities');
}

(async () => {
  await ready();
  browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  await verifyLayouts();
  await verifyFlows();
  await verifySignup();
  await verifyMultiSelection();
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(cspErrors, []);
  assert.deepEqual(consoleErrors, []);
  assert.deepEqual(prohibitedRequests, []);
  const report = { status: 'passed', synthetic_data_only: true, production_build: true, url: target.href,
    checks, layouts, page_errors: pageErrors, csp_errors: cspErrors, console_errors: consoleErrors,
    prohibited_requests: prohibitedRequests, network: { total: requests.length, methods: [...new Set(requests.map(item => item.method))],
      paths: [...new Set(requests.map(item => item.path))], api_requests: 0, mutations: 0 } };
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  fs.rmSync(path.join(output, 'failure.json'), { force: true });
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, layouts: layouts.length, api_requests: 0, output }));
})().catch(error => {
  fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ error: error.stack, pageErrors, cspErrors, consoleErrors, prohibitedRequests, checks, layouts }, null, 2) + '\n');
  console.error(error); process.exitCode = 1;
}).finally(async () => { await browser?.close(); });
