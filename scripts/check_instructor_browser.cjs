// Real server HTML and CSP; synthetic routed responses, no production mutations.
// node scripts/check_instructor_browser.cjs browser-fixtures.json PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [file, modulePath = 'playwright', executablePath] = process.argv.slice(2);
const fixtures = JSON.parse(fs.readFileSync(file, 'utf8'));
const {chromium} = require(modulePath);
(async () => {
  const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
  const errors = [];
  let checks = 0;
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    let fixture = fixtures.integrated, replies = [], calls = 0, loads = 0, status = 200;
    await page.route('**/*', async route => {
      assert.equal(route.request().method(), 'GET', 'browser must not perform automatic writes');
      if (route.request().url().endsWith('/status')) {
        calls++;
        await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(replies.shift() || fixtures.poll.data)});
      } else {
        loads++;
        await route.fulfill({contentType: 'text/html', headers: {'Content-Security-Policy': fixture.csp}, body: fixture.html});
      }
    });
    const open = async name => {
      fixture = fixtures[name]; replies = []; calls = 0; loads = 0; status = 200;
      // Dismiss intentional navigation guards only inside this test helper.
      page.once('dialog', dialog => dialog.accept());
      await page.goto(fixture.url);
      page.removeAllListeners('dialog');
    };
    const active = () => page.locator('[data-case]:not([hidden])');
    const form = () => page.locator('form:has([data-case-editor])');
    const unload = () => page.evaluate(() => !window.dispatchEvent(new Event('beforeunload', {cancelable: true})));
    await open('integrated');
    assert.equal(await page.evaluate(() => {const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size;}), true);
    assert.equal(await page.locator('input[name=due_at]').count(), 1); checks++;
    await page.getByText('테스트·배점 직접 편집', {exact: true}).click();
    assert.equal(await active().count(), 1); assert.equal(await unload(), false); checks++;
    await page.locator('[data-case-copy]:visible').click();
    assert.equal(await active().count(), 2);
    assert.equal(await page.locator('[name=test_1_output]').inputValue(), 'ok');
    assert.match(await page.locator('[data-case-total]').innerText(), /4점/);
    assert.equal(await unload(), true); checks++;
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('[data-case-remove]:visible').first().click();
    assert.equal(await active().count(), 2); checks++;
    page.once('dialog', dialog => dialog.accept());
    await page.locator('[data-case-remove]:visible').first().click();
    assert.equal(await active().count(), 1);
    assert.equal(await page.locator('[name=test_0_output]').inputValue(), 'ok');
    const payload = await form().evaluate(form => [...new FormData(form).keys()]);
    assert.equal(payload.filter(key => /^test_/.test(key)).length, 4);
    assert.ok(!payload.includes('test_1_output')); checks++;
    await page.locator('[name=test_0_weight]').fill('0');
    assert.match(await page.locator('[data-case-total]').innerText(), /배점을 확인/); checks++;
    await page.locator('[name=test_0_weight]').fill('');
    assert.match(await page.locator('[data-case-total]').innerText(), /배점을 확인/); checks++;
    // Another dirty form must not be silently discarded on an upload submit.
    page.once('dialog', dialog => dialog.dismiss());
    const cancelled = await page.locator('form:has(input[type=file])').first().evaluate(form =>
      !form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
    assert.equal(cancelled, true); checks++;
    for (let i = 1; i < 50; i++) await page.locator('[data-case-add]').click();
    assert.equal(await active().count(), 50);
    assert.equal(await page.locator('[data-case-add]').isDisabled(), true);
    assert.equal(await page.locator('[data-case-copy]:enabled').count(), 0);
    assert.equal(await form().evaluate(form => {const ids = [...form.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size;}), true); checks++;
    await open('recovered');
    assert.equal(await active().count(), 1);
    assert.equal(await page.locator('[name=test_0_title]').inputValue(), 'edited');
    assert.match(await form().locator('[data-save-status]').innerText(), /미저장/);
    assert.equal(await unload(), true); checks++;
    // CSP allows our exact script but rejects newly injected inline code.
    await page.evaluate(() => {const el = document.createElement('script'); el.textContent = 'window.untrustedExecuted = true'; document.body.append(el);});
    assert.equal(await page.evaluate(() => window.untrustedExecuted), undefined); checks++;
    await open('integrated');
    const title = page.locator('[name=title]'); const initial = await title.inputValue();
    await title.fill('unsaved'); assert.equal(await unload(), true);
    let warned = false;
    page.once('dialog', async dialog => {warned = dialog.type() === 'beforeunload'; await dialog.dismiss();});
    await page.getByRole('link', {name: '과제 목록으로', exact: true}).click();
    assert.equal(warned, true); assert.equal(page.url(), fixtures.integrated.url); checks++;
    await title.fill(initial); assert.equal(await unload(), false); checks++;
    await open('integrated');
    await page.locator('input[type=file]').first().setInputFiles({name:'example.zip', mimeType:'application/zip', buffer:Buffer.from('synthetic')});
    assert.equal(await unload(), true);
    await page.locator('input[type=file]').first().setInputFiles([]);
    assert.equal(await unload(), false); checks++;
    for (const theme of ['light', 'dark']) for (const width of [320, 375, 768, 1440]) {
      await open('integrated');
      await page.emulateMedia({colorScheme: theme}); await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
      if (width === 375) await page.screenshot({path:path.join(path.dirname(file), `case-editor-${theme}.png`), fullPage:true});
      checks++;
    }
    await page.clock.install();
    await open('poll');
    replies = [{...fixtures.poll.data, status: 'running'}];
    await page.clock.runFor(5001);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('검증 중'));
    assert.equal(calls, 1); checks++;
    await page.locator('[data-check-toggle]').click();
    await page.clock.runFor(20000); assert.equal(calls, 1); checks++;
    await page.locator('[data-check-toggle]').click(); await page.clock.runFor(1);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('확인됨'));
    assert.equal(calls, 2); checks++;
    await open('poll');
    await context.setOffline(true);
    await page.clock.runFor(20000);
    assert.equal(calls, 0);
    assert.match(await page.locator('[data-check-message]').innerText(), /오프라인/);
    await context.setOffline(false); await page.clock.runFor(5001);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('확인됨'));
    assert.equal(calls, 1); checks++;
    for (const code of [401, 403, 404]) {
      await open('poll'); status = code; await page.clock.runFor(5001);
      await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('권한'));
      await page.clock.runFor(60000); assert.equal(calls, 1); checks++;
    }
    await open('poll'); status = 503;
    for (const [index, delay] of [5001, 10001, 20001, 40001, 60001].entries()) {
      await page.clock.runFor(delay);
      await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('확인하지 못했습니다'));
      assert.equal(calls, index + 1);
    }
    assert.match(await page.locator('[data-check-toggle]').innerText(), /다시 시작/);
    await page.clock.runFor(120000); assert.equal(calls, 5); checks++;
    status = 200; await page.locator('[data-check-toggle]').click(); await page.clock.runFor(1);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('확인됨'));
    assert.equal(calls, 6); checks++;
    await open('poll'); replies = [{...fixtures.poll.data, draft_revision: 2}];
    await page.clock.runFor(5001);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('버전이 변경'));
    await page.clock.runFor(60000); assert.equal(calls, 1); assert.equal(loads, 1); checks++;
    await open('poll'); replies = [{...fixtures.poll.data, job_id: 'wrong-job'}];
    await page.clock.runFor(5001);
    await page.waitForFunction(() => document.querySelector('[data-check-message]').textContent.includes('확인하지 못했습니다'));
    assert.equal(loads, 1); checks++;
    await open('poll'); replies = [{...fixtures.poll.data, status: 'succeeded'}];
    const reloaded = page.waitForEvent('domcontentloaded');
    await page.clock.runFor(5001);
    await reloaded;
    assert.equal(loads, 2); checks++;
    assert.deepEqual(errors, []);
    // No script support still leaves native POST forms and all 50 case slots.
    const nojs = await browser.newContext({javaScriptEnabled: false});
    const fallback = await nojs.newPage();
    await fallback.route('**/*', route => route.fulfill({contentType: 'text/html', body: fixtures.integrated.html}));
    await fallback.goto(fixtures.integrated.url);
    assert.equal(await fallback.locator('[data-case]').count(), 50);
    assert.equal(await fallback.locator('[name=test_49_output]').isEnabled(), true);
    assert.equal(await fallback.locator('[data-case-add]').isVisible(), false); checks++;
    console.log(`${checks} instructor browser behavior/layout/security scenarios passed`);
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
