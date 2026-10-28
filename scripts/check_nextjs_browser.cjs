// Production Next.js + the check_nextjs_pilot.py --serve synthetic Python fixture.
// node scripts/check_nextjs_browser.cjs NEXT_ORIGIN PYTHON_ORIGIN OUTPUT_DIR [PLAYWRIGHT_MODULE] [CHROME_BINARY]
// This local GET-only proxy models same-origin routing, not an HTTPS/Nginx deployment.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const [nextInput, pythonInput, output, modulePath = 'playwright', executablePath] = process.argv.slice(2);
function localOrigin(input) {
  const url = new URL(input);
  assert.equal(url.protocol, 'http:');
  assert.equal(url.hostname, '127.0.0.1', 'QA must only target explicit loopback listeners');
  assert.equal(url.pathname, '/');
  assert.ok(!url.username && !url.password && !url.search && !url.hash);
  return url;
}
const nextOrigin = localOrigin(nextInput), pythonOrigin = localOrigin(pythonInput);
assert.ok(output, 'OUTPUT_DIR is required');
fs.mkdirSync(output, {recursive: true});
const {chromium} = require(modulePath);
const credentials = {username: 'reviewer', password: 'synthetic-next-pilot-password'};
const resultPath = '/instructor/api/v1/courses/come2201/results';
const checks = [], errors = [], cspErrors = [];
const check = name => checks.push(name);

async function main() {
  const proxy = http.createServer((request, response) => {
    if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); response.end(); return; }
    const upstream = request.url === '/app' || request.url.startsWith('/app/') || request.url.startsWith('/app?') ? nextOrigin : pythonOrigin;
    const forwarded = http.request({hostname: '127.0.0.1', port: upstream.port,
      method: request.method, path: request.url,
      headers: {...request.headers, host: upstream.host}}, incoming => {
      response.writeHead(incoming.statusCode, incoming.headers); incoming.pipe(response);
    });
    forwarded.on('error', error => { if (!response.headersSent) response.writeHead(502); response.end(String(error)); });
    request.pipe(forwarded);
  });
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${proxy.address().port}`;
  const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
  try {
    const publicShell = await fetch(origin + '/app');
    assert.equal(publicShell.status, 200);
    const html = await publicShell.text();
    assert.ok(!html.includes('20260001') && !html.includes(credentials.password));
    const csp = publicShell.headers.get('content-security-policy');
    assert.match(csp, /script-src[^;]*'nonce-/);
    assert.ok(!csp.includes('unsafe-eval') && !csp.includes('unsafe-inline'));
    assert.match(publicShell.headers.get('cache-control'), /no-store/);
    check('Production shell has no student/password data, uses a nonce CSP and no-store');

    const anonymous = await browser.newContext();
    const anonymousPage = await anonymous.newPage();
    const anonymousErrors = [];
    anonymousPage.on('pageerror', error => anonymousErrors.push(String(error)));
    anonymousPage.on('console', message => { if (message.type() === 'error') anonymousErrors.push(message.text()); });
    await anonymousPage.goto(origin + '/app');
    try {
      await anonymousPage.getByTestId('auth-entry').waitFor({state: 'visible'});
    } catch (error) {
      console.error('Anonymous rendered page:', await anonymousPage.locator('body').innerText());
      console.error('Anonymous console:', anonymousErrors);
      await anonymousPage.screenshot({path: path.join(output, 'anonymous-failure.png'), fullPage: true});
      throw error;
    }
    assert.equal(await anonymousPage.getByTestId('result-row').count(), 0);
    assert.equal(await anonymousPage.getByTestId('course-navigation').count(), 0);
    await anonymous.close();
    check('Fresh unauthenticated context shows explicit authentication entry and no results');

    const context = await browser.newContext({httpCredentials: credentials, viewport: {width: 1440, height: 1050}, reducedMotion: 'reduce'});
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== origin || !['GET', 'HEAD'].includes(route.request().method())) {
        await route.abort('blockedbyclient');
        throw new Error('QA attempted a nonlocal or mutating browser request');
      }
      await route.continue();
    });
    const page = await context.newPage();
    const responses = [], requests = [];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('console', message => { if (/Content Security Policy|Refused to/.test(message.text())) cspErrors.push(message.text()); });
    page.on('response', response => {
      if (response.url().includes('/instructor/api/v1/')) responses.push({url: response.url(), status: response.status()});
    });
    page.on('request', request => { if (request.url().includes('/instructor/api/v1/')) requests.push(request.url()); });
    await page.goto(origin + '/instructor/api/v1/entry?screen=results&course_key=come2201');
    await page.getByTestId('result-row').first().waitFor();
    assert.equal(await page.getByTestId('result-row').count(), 20);
    assert.ok(responses.some(r => r.url.includes('/entry?') && r.status === 303));
    assert.ok(responses.some(r => r.url.endsWith('/bootstrap') && r.status === 200));
    assert.ok(responses.some(r => r.url.includes(resultPath) && r.status === 200));
    assert.equal(new URL(page.url()).pathname, '/app');
    check('Fresh credential context follows real Python Basic entry 303 → bootstrap → paginated results');

    const courseNav = page.getByTestId('course-navigation');
    assert.equal(await courseNav.getByRole('link', {name: '수업 홈', exact: true}).getAttribute('href'), '/courses/come2201/instructor');
    assert.equal(await courseNav.locator('[aria-current="page"]').innerText(), '제출·채점');
    assert.equal(await page.getByRole('navigation', {name: '포털 이동'}).getByRole('link', {name: '학생 포털'}).getAttribute('href'), '/app/student');
    check('Authorized portal navigation retains current course and identifies the active section');

    for (const row of await page.getByTestId('result-row').all()) {
      assert.match(await row.getByTestId('current-score').innerText(), /^0\s*\/\s*100/);
      assert.match(await row.getByTestId('previous-best').innerText(), /100\s*\/\s*100/);
      assert.match(await row.innerText(), /보완 필요/, 'partial credit is text, not colour alone');
    }
    check('Current zero remains distinct from previous best 100 in all 20 visible rows');
    for (const row of await page.getByTestId('result-row').all()) {
      const review = await row.getByRole('link', {name:/이력·코드 확인/}).getAttribute('href');
      const download = row.getByTestId('submission-download');
      assert.equal(await download.getAttribute('href'), review + '/download.zip');
      assert.notEqual(await download.getAttribute('download'), null);
    }
    check('Each native ZIP download refers to the exact displayed submission receipt');
    const selectedID = await page.getByTestId('assignment-select').evaluate(select => [...select.options].find(option => option.value)?.value);
    assert.ok(selectedID);
    await page.getByTestId('assignment-select').selectOption(selectedID);
    let selectedReply = page.waitForResponse(r => r.url().includes(resultPath) && new URL(r.url()).searchParams.get('assignment_id') === selectedID);
    await page.getByTestId('apply-filters').click(); await selectedReply;
    await page.getByTestId('assignment-downloads').waitFor();
    assert.equal(await page.getByTestId('assignment-downloads').getAttribute('href'), `/courses/come2201/instructor/assignments/${selectedID}/downloads`);
    await page.getByTestId('assignment-select').selectOption('');
    selectedReply = page.waitForResponse(r => r.url().includes(resultPath) && !new URL(r.url()).searchParams.has('assignment_id'));
    await page.getByTestId('apply-filters').click(); await selectedReply;
    await page.waitForFunction(() => !document.querySelector('[data-testid="assignment-downloads"]'));
    check('Selected assignment exposes its course-scoped downloads page and all-assignment view hides it');
    const firstKeys = await page.getByTestId('result-row').evaluateAll(rows => rows.map(row => row.dataset.rowKey || row.textContent));
    const secondResponse = page.waitForResponse(r => r.url().includes(resultPath) && new URL(r.url()).searchParams.get('page') === '2');
    await page.getByTestId('next-page').click();
    await secondResponse;
    await page.waitForFunction(first => [...document.querySelectorAll('[data-testid="result-row"]')].every(row => !first.includes(row.dataset.rowKey || row.textContent)), firstKeys);
    assert.equal(await page.getByTestId('result-row').count(), 20);
    check('Real server pagination replaces 20 rows without duplicates from the previous page');

    await page.getByTestId('student-search').fill('20260001');
    const filtered = page.waitForResponse(r => r.url().includes(resultPath) && new URL(r.url()).searchParams.get('student') === '20260001');
    await page.getByTestId('apply-filters').click();
    await filtered;
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="result-row"]')].every(row => row.textContent.includes('20260001')));
    assert.equal(await page.getByTestId('result-row').count(), 20);
    await page.getByTestId('status-select').selectOption('unsubmitted');
    const empty = page.waitForResponse(r => r.url().includes(resultPath) && new URL(r.url()).searchParams.get('status') === 'unsubmitted');
    await page.getByTestId('apply-filters').click();
    await empty;
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="result-row"]').length === 0);
    check('Real student and status filters yield the expected 20 rows and empty state');

    await page.getByTestId('student-search').fill('');
    await page.getByTestId('status-select').selectOption('all');
    await page.getByTestId('apply-filters').click();
    await page.getByTestId('result-row').first().waitFor();
    const scoped = await context.request.get(origin + '/instructor/api/v1/courses/come3105/results');
    assert.equal(scoped.status(), 200);
    assert.equal((await scoped.json()).rows.length, 0);
    await page.getByTestId('course-select').selectOption('come3105');
    await page.waitForFunction(() => document.querySelector('[data-testid="course-navigation"] a')?.getAttribute('href') === '/courses/come3105/instructor');
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="result-row"]').length === 0);
    assert.equal(await page.getByTestId('student-search').inputValue(), '');
    await page.getByTestId('course-select').selectOption('come2201');
    await page.getByTestId('result-row').first().waitFor();
    check('Real course switch clears the old roster and resets filter inputs');

    await page.getByTestId('result-row').first().getByRole('link', {name:/이력·코드 확인/}).focus();
    const stable = await page.evaluate(() => {
      window.__readingRow = document.querySelector('[data-testid="result-row"]');
      window.__readingFocus = document.activeElement;
      return window.scrollY;
    });
    const unchanged = page.waitForResponse(r => r.url().includes(resultPath) && new URL(r.url()).searchParams.has('version'));
    await unchanged;
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => window.__readingRow === document.querySelector('[data-testid="result-row"]')), true);
    assert.equal(await page.evaluate(() => document.activeElement === window.__readingFocus), true);
    assert.equal(await page.evaluate(() => window.scrollY), stable);
    check('Unchanged real polling preserves row identity, keyboard focus and scroll');

    await context.setOffline(true);
    const beforeOffline = requests.filter(url => url.includes('/results')).length;
    await page.waitForTimeout(6200);
    assert.equal(requests.filter(url => url.includes('/results')).length, beforeOffline);
    const resumed = page.waitForResponse(r => r.url().includes(resultPath));
    await context.setOffline(false);
    await resumed;
    check('Offline state suspends results requests and online event resumes them');

    // A delayed real fixture reply tests whether a changed course can receive old data.
    let releaseDelayed, startedDelayed;
    const delayedGate = new Promise(resolve => { releaseDelayed = resolve; });
    const delayedStarted = new Promise(resolve => { startedDelayed = resolve; });
    const delayedPattern = '**/instructor/api/v1/courses/come2201/results*';
    await page.route(delayedPattern, async route => {
      if (new URL(route.request().url()).searchParams.get('student') !== '20260002') return route.continue();
      const realReply = await route.fetch();
      startedDelayed();
      await delayedGate;
      await route.fulfill({response: realReply});
    });
    await page.getByTestId('student-search').fill('20260002');
    await page.getByTestId('apply-filters').click();
    await delayedStarted;
    const otherResponse = page.waitForResponse(r => r.url().includes('/courses/come3105/results'));
    await page.getByTestId('course-select').selectOption('come3105');
    const otherResults = await otherResponse;
    assert.equal(otherResults.status(), 200);
    releaseDelayed();
    await page.waitForTimeout(200);
    assert.equal(await page.getByTestId('course-select').inputValue(), 'come3105');
    assert.equal(await page.getByTestId('result-row').count(), 0);
    await page.unroute(delayedPattern);
    await page.getByTestId('course-select').selectOption('come2201');
    await page.getByTestId('result-row').first().waitFor();
    check('A delayed previous-course response cannot restore the previous roster');

    await page.evaluate(() => { window.__toggleRow = document.querySelector('[data-testid="result-row"]'); });
    await page.getByTestId('poll-toggle').uncheck();
    assert.equal(await page.evaluate(() => window.__toggleRow === document.querySelector('[data-testid="result-row"]')), true);
    await page.getByTestId('poll-toggle').check();
    assert.equal(await page.getByTestId('result-row').count(), 20);
    check('Changing automatic polling does not discard the displayed rows');

    // A version conflict must preserve the list being read until explicit refresh.
    const conflictPattern = '**/instructor/api/v1/courses/*/results*';
    await page.route(conflictPattern, async route => {
      await route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({
        contract_version: '1', request_id: 'synthetic-conflict', server_time: new Date().toISOString(),
        error: {code: 'result_set_changed', message: 'Synthetic changed result set'}})});
    });
    await page.getByTestId('update-notice').waitFor();
    assert.equal(await page.getByTestId('result-row').count(), 20);
    assert.equal(await page.getByTestId('next-page').isDisabled(), true);
    await page.getByTestId('poll-toggle').uncheck();
    assert.equal(await page.getByTestId('result-row').count(), 20);
    assert.equal(await page.getByTestId('update-notice').isVisible(), true);
    await page.unroute(conflictPattern);
    await page.getByRole('button', {name: '최신 결과 반영', exact: true}).click();
    await page.getByTestId('result-row').first().waitFor();
    await page.getByTestId('poll-toggle').check();
    check('Intercepted 409 preserves the list and notice through polling changes until explicit refresh');

    const noStorage = await page.evaluate(() => ({local: Object.keys(localStorage), session: Object.keys(sessionStorage)}));
    assert.deepEqual(noStorage, {local: [], session: []});
    for (const mode of ['light', 'dark']) {
      await page.emulateMedia({colorScheme: mode});
      for (const width of [320, 375, 768, 1024, 1440]) {
        await page.setViewportSize({width, height: width > 1000 ? 1050 : 900});
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
        assert.equal(overflow, false, `document overflows at ${width}px in ${mode}`);
        const navBounds = await courseNav.locator('a').evaluateAll(links => links.map(link => {
          const rect = link.getBoundingClientRect(); return {left: rect.left, right: rect.right, height: rect.height};
        }));
        assert.equal(navBounds.length, 6);
        for (const link of navBounds) assert.ok(link.left >= 0 && link.right <= width + 1 && link.height >= 40,
          `portal navigation clipped or too small at ${width}px in ${mode}`);
        assert.equal(await page.getByTestId('result-row').count(), 20);
        const flat = await page.locator('section[aria-label="제출 요약"]').evaluate(section => {
          const parent = getComputedStyle(section);
          return {
            gap: parent.gap,
            columns: parent.gridTemplateColumns.split(/\s+/).length,
            items: [...section.children].map(node => {
              const style = getComputedStyle(node);
              return {shadow: style.boxShadow, radius: style.borderRadius,
                background: style.backgroundImage, left: style.borderLeftWidth, top: style.borderTopWidth};
            }),
          };
        });
        assert.equal(flat.gap, '0px');
        assert.equal(flat.items.length, 5);
        for (const item of flat.items) {
          assert.equal(item.shadow, 'none');
          assert.equal(item.radius, '0px');
          assert.equal(item.background, 'none');
        }
        assert.ok(flat.columns >= 1 && flat.columns <= 5);
        assert.deepEqual(flat.items.map(item => item.left), flat.items.map((_, i) =>
          i % flat.columns === 0 ? '0px' : '1px'));
        assert.deepEqual(flat.items.map(item => item.top), flat.items.map((_, i) =>
          i >= flat.columns ? '1px' : '0px'));
        await page.screenshot({path: path.join(output, `next-results-${mode}-${width}.png`), fullPage: true});
        if ((mode === 'light' && width === 1440) || (mode === 'dark' && width === 375)) {
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.screenshot({path: path.join(output, `next-results-${mode}-${width}-viewport.png`), fullPage: false});
          if (mode === 'dark' && width === 375) {
            await page.getByTestId('result-row').first().scrollIntoViewIfNeeded();
            await page.screenshot({path: path.join(output, 'next-results-dark-375-results-viewport.png'), fullPage: false});
          }
        }
      }
    }
    check('320/375/768/1024/1440px light and dark captures have no page-level horizontal overflow');
    check('Flat summary uses no shadows or rounded cards, with correct desktop/mobile dividers in both themes');

    await page.emulateMedia({colorScheme: 'light'});
    await page.setViewportSize({width: 1440, height: 900});
    await page.evaluate(() => window.scrollTo(0, 0));
    const firstScore = await page.getByTestId('current-score').first().locator('strong').boundingBox();
    assert.ok(firstScore && firstScore.y >= 0 && firstScore.y + firstScore.height <= 900,
      'the first current score must be readable without scrolling on a 1440×900 desktop');
    await page.screenshot({path: path.join(output, 'next-results-light-1440x900-viewport.png'), fullPage: false});
    check('A 1440×900 desktop shows the first latest score without scrolling');

    // Layout stress uses the same authenticated synthetic fixture with only
    // display strings replaced. It does not change server or user data.
    const longName = 'LongCourseStudentAssignmentName'.repeat(8);
    const bootstrapPattern = '**/instructor/api/v1/bootstrap';
    const longResultsPattern = '**/instructor/api/v1/courses/come2201/results*';
    await page.route(bootstrapPattern, async route => {
      const actual = await route.fetch();
      const data = await actual.json();
      data.principal.display_name = longName;
      data.courses = data.courses.map(course => ({...course, name: longName}));
      await route.fulfill({response: actual, json: data});
    });
    await page.route(longResultsPattern, async route => {
      const actual = await route.fetch();
      const data = await actual.json();
      if (actual.status() === 200) {
        data.assignments = data.assignments.map(assignment => ({...assignment, title: longName}));
        data.rows = data.rows.map(row => ({...row, student_name: longName, title: longName}));
      }
      await route.fulfill({response: actual, json: data});
    });
    await page.goto(origin + '/app?course_key=come2201');
    await page.getByTestId('result-row').first().waitFor();
    assert.ok((await page.getByTestId('result-row').first().innerText()).includes(longName));
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false,
        `long display strings overflow at ${width}px`);
      assert.equal(await page.locator('table[role="table"] tbody[role="rowgroup"] tr[role="row"]').count(), 20);
      assert.equal(await page.locator('th[scope="row"][role="rowheader"]').count(), 20);
    }
    // Text enlargement is deliberately separate from devicePixelRatio: the
    // latter changes raster density, not layout or readable text size.
    await page.setViewportSize({width: 768, height: 900});
    const normalCellFont = await page.locator('td[data-label="실습 과제"]').first().evaluate(node => parseFloat(getComputedStyle(node).fontSize));
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    const enlargedCellFont = await page.locator('td[data-label="실습 과제"]').first().evaluate(node => parseFloat(getComputedStyle(node).fontSize));
    assert.equal(enlargedCellFont, normalCellFont * 2, 'result text must follow the user font size');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false,
      '200% text enlargement overflows');
    await page.evaluate(() => { document.documentElement.style.removeProperty('font-size'); });
    await page.unroute(bootstrapPattern);
    await page.unroute(longResultsPattern);
    await page.goto(origin + '/app?course_key=come2201');
    await page.getByTestId('result-row').first().waitFor();
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('.skip-link').evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
    check('Long display strings, 200% root-font enlargement, semantic mobile rows and keyboard skip navigation remain usable');

    await page.emulateMedia({forcedColors: 'active'});
    await page.setViewportSize({width: 1440, height: 1050});
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(output, 'next-results-forced-colors-1440-viewport.png'), fullPage: false});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    check('Forced-colors desktop view retains content without horizontal overflow');
    await page.emulateMedia({forcedColors: 'none'});
    await page.setViewportSize({width: 1440, height: 1050});
    assert.equal(cspErrors.length, 0, 'production scripts/styles must load without CSP violations');
    // strict-dynamic trusts script creation by an already trusted script. Inject
    // into the raw HTML instead, so this probe is parser-inserted without a nonce.
    const cspProbe = await context.newPage();
    const blockedScripts = [];
    cspProbe.on('console', message => { if (/Content Security Policy/i.test(message.text())) blockedScripts.push(message.text()); });
    await cspProbe.route('**/app?csp_probe=1', async route => {
      const actual = await route.fetch();
      assert.match(actual.headers()['content-security-policy'], /script-src[^;]*'nonce-/);
      const injected = (await actual.text()).replace('<head>', '<head><script>window.__untrustedParserScript = true</script>');
      assert.ok(injected.includes('<script>window.__untrustedParserScript = true</script>'));
      await route.fulfill({response: actual, body: injected});
    });
    await cspProbe.goto(origin + '/app?csp_probe=1');
    assert.equal(await cspProbe.evaluate(() => window.__untrustedParserScript), undefined);
    assert.ok(blockedScripts.length > 0, 'the parser-inserted script must generate an actual CSP refusal');
    await cspProbe.close();
    check('Actual production CSP blocks a nonce-free parser-inserted script in intercepted HTML');

    // These responses are deliberately intercepted; live ACL/challenge checks above are not mocked.
    for (const status of [401, 403]) {
      let errorCalls = 0;
      const routePattern = '**/instructor/api/v1/courses/*/results*';
      await page.route(routePattern, async route => {
        errorCalls++;
        await route.fulfill({status, contentType: 'application/json', body: JSON.stringify({
          contract_version: '1', request_id: `synthetic-${status}`, server_time: new Date().toISOString(),
          error: {code: status === 401 ? 'instructor_auth_required' : 'access_denied', message: `Synthetic ${status} boundary`}})});
      });
      await page.getByTestId('refresh-results').click();
      await page.getByTestId('api-error').waitFor({state: 'visible'});
      assert.equal(await page.getByTestId('result-row').count(), 0);
      const afterError = errorCalls;
      await page.waitForTimeout(6200);
      assert.equal(errorCalls, afterError, `${status} must halt automatic polling`);
      await page.unroute(routePattern);
      await page.goto(origin + '/instructor/api/v1/entry?screen=results&course_key=come2201');
      await page.getByTestId('result-row').first().waitFor();
    }
    check('Intercepted 401 and 403 both clear stale data and halt automatic polling');
    assert.deepEqual(errors, []);
    await context.close();
    const report = {checks, screenshots: path.resolve(output), node: process.version, browser: browser.version(),
      limitations: ['Chrome automation with supplied test credentials does not validate native password-prompt UX.',
        'Loopback HTTP proxy is not the deployment Nginx/TLS configuration.',
        'Root-font enlargement is not the browser native zoom setting.',
        'Safari and Edge fresh-profile Basic reuse remain unverified.',
        '401/403/409 checks use explicitly intercepted synthetic failures.']};
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await browser.close();
    await new Promise(resolve => proxy.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
