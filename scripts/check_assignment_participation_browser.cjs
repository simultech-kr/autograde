// Actual service HTML and fixed CSP, synthetic responses only; never contacts a server.
// node scripts/check_assignment_participation_browser.cjs FIXTURE PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [fixturePath, modulePath = 'playwright', executablePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const {chromium} = require(modulePath);

(async () => {
  const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
  let checks = 0;
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    let payload = fixture.initial, calls = 0, loads = 0;
    const origin = new URL(fixture.url).origin;
    const live = new URL(fixture.url).pathname + '/live';
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(request.method(), 'GET', 'No browser writes are permitted');
      assert.equal(url.origin, origin, 'Every request must stay on the synthetic origin');
      if (url.pathname === live) {
        calls++;
        await route.fulfill({contentType: 'application/json', body: JSON.stringify(payload)});
      } else {
        assert.equal(url.pathname, new URL(fixture.url).pathname);
        loads++;
        await route.fulfill({contentType: 'text/html', headers: {'Content-Security-Policy': fixture.csp}, body: fixture.html});
      }
    });
    await page.clock.install();
    const assignment = () => page.locator('[data-results-assignment]');
    const view = () => page.locator('[data-results-filter]');
    const rows = () => page.locator('[data-result-key]:visible');
    const summary = id => page.locator(`[data-result-assignment-summary][data-assignment-id="${id}"]`);
    const [first, second] = fixture.assignment_ids;
    const open = async (query = '') => {
      payload = fixture.initial; calls = 0; loads = 0;
      await page.goto(fixture.url + query);
      assert.equal(await assignment().isEnabled(), true);
    };
    const expectRows = async (id, expectedStudents, submitted) => {
      const actual = await rows().evaluateAll(elements => elements.map(row => ({
        id: row.dataset.assignmentId,
        student: row.querySelector('th .cell-value').textContent.trim(),
        submitted: row.dataset.submitted,
      })));
      assert.equal(actual.length, expectedStudents.length);
      assert.deepEqual(actual.map(row => row.student).sort(), [...expectedStudents].sort());
      assert.ok(actual.every(row => row.id === id));
      if (submitted) assert.ok(actual.every(row => row.submitted === submitted));
      checks++;
    };
    const expectSelection = async (id, selectedView) => {
      assert.equal(await assignment().inputValue(), id);
      assert.equal(await view().inputValue(), selectedView);
      const query = new URL(page.url()).searchParams;
      assert.equal(query.get('assignment_id'), id || null);
      assert.equal(query.get('result_view'), selectedView === 'all' ? null : selectedView);
      checks++;
    };

    await open();
    assert.equal(await rows().count(), 6);
    assert.equal(await assignment().locator('option').count(), 3); checks++;
    await assignment().selectOption(first);
    await expectRows(first, ['20260001', '20260002', '20260003']);
    assert.equal(await page.locator('[data-results-course-summary]').isVisible(), false);
    assert.equal(await summary(second).isVisible(), false);
    await summary(first).getByRole('link', {name: '제출 1명', exact: true}).click();
    await expectRows(first, ['20260001'], 'yes');
    await expectSelection(first, 'submitted');
    assert.equal(await summary(first).locator('[aria-current=true]').innerText(), '제출 1명');
    assert.equal(loads, 1, 'Summary links should filter the page without navigation'); checks++;
    await summary(first).getByRole('link', {name: '미제출 2명', exact: true}).click();
    await expectRows(first, ['20260002', '20260003'], 'no');
    await expectSelection(first, 'unsubmitted');
    await assignment().selectOption(second);
    await expectRows(second, ['20260001'], 'no');
    await summary(second).getByRole('link', {name: '제출 2명', exact: true}).click();
    await expectRows(second, ['20260002', '20260003'], 'yes');
    assert.match(await rows().allTextContents().then(values => values.join(' ')), /제출 거절/);
    assert.match(await rows().allTextContents().then(values => values.join(' ')), /채점 환경 오류/); checks++;

    await open(`?assignment_id=${first}&result_view=unsubmitted`);
    await expectRows(first, ['20260002', '20260003'], 'no');
    await expectSelection(first, 'unsubmitted');
    await assignment().focus(); // Keep focus outside the content being refreshed.
    payload = fixture.updated;
    await page.clock.runFor(5001);
    await page.waitForFunction(() => document.querySelector('[data-results-content]').textContent.includes('이번 제출 채점 중'));
    await expectRows(first, ['20260003'], 'no');
    await expectSelection(first, 'unsubmitted');
    assert.equal(await summary(first).getByRole('link', {name: '제출 2명', exact: true}).count(), 1);
    assert.equal(await summary(first).getByRole('link', {name: '미제출 1명', exact: true}).count(), 1);
    assert.equal(calls, 1); checks++;

    payload = fixture.removed;
    await page.clock.runFor(5001);
    await page.waitForFunction(id => !document.querySelector(`[data-result-assignment-summary][data-assignment-id="${id}"]`), first);
    await expectRows(first, []);
    await expectSelection(first, 'unsubmitted');
    assert.match(await assignment().locator('option:checked').innerText(), /현재 조회 대상 없음/);
    assert.equal(await page.locator('[data-results-empty-filter]').isVisible(), true);
    assert.equal(await summary(second).isVisible(), false);
    assert.equal(calls, 2); checks++;

    // A missing selection in a bookmarked URL must also remain narrow from the start.
    await open('?assignment_id=basn_no_longer_listed&result_view=submitted');
    assert.equal(await rows().count(), 0);
    await expectSelection('basn_no_longer_listed', 'submitted');
    assert.equal(await page.locator('[data-results-empty-filter]').isVisible(), true); checks++;

    for (const theme of ['light', 'dark']) for (const width of [375, 1440]) {
      await page.emulateMedia({colorScheme: theme});
      await page.setViewportSize({width, height: 1000});
      await open(`?assignment_id=${first}&result_view=unsubmitted`);
      await expectRows(first, ['20260002', '20260003'], 'no');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true,
        `${theme} ${width}px must not overflow`);
      await page.screenshot({path: path.join(path.dirname(fixturePath), `participation-${theme}-${width}.png`), fullPage: true});
      checks++;
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`${checks} assignment participation browser checks passed`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
