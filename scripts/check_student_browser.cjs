// Synthetic server-rendered student pages only; no production accounts or traffic.
// node scripts/check_student_browser.cjs FIXTURES_JSON PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [file, modulePath = 'playwright', executablePath] = process.argv.slice(2);
const fixtures = JSON.parse(fs.readFileSync(file, 'utf8'));
const {chromium} = require(modulePath);
(async () => {
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  let checks = 0;
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const expiresAt = Date.parse(fixtures.claim.html.match(/data-claim-expires="([^"]+)"/)[1]);
    await page.clock.install({time:new Date(expiresAt - 600000)});
    let current = 'login', posts = 0;
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.addInitScript(() => {
      window.copied = [];
      Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {
        if (window.failCopy) throw new Error('test permission denied');
        window.copied.push(text);
      }}});
    });
    await page.route('**/*', route => {
      if (route.request().method() === 'POST') posts++;
      return route.fulfill({contentType:'text/html', headers:{'Content-Security-Policy':fixtures[current].csp}, body:fixtures[current].html});
    });
    const open = async name => { current = name; await page.goto('https://student.example.test/' + name); };
    for (const theme of ['light', 'dark']) for (const width of [320, 375, 768, 1440]) {
      await page.emulateMedia({colorScheme:theme});
      await page.setViewportSize({width, height:812});
      for (const name of ['login', 'assignments', 'claim']) {
        await open(name);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${name}/${width}/${theme} overflow`);
        if (name === 'assignments') {
          const button = page.getByRole('button', {name:'선택한 실습 수령 코드 발급', exact:true});
          assert.equal(await button.isDisabled(), true);
          assert.equal(await button.evaluate(el => {const b = el.getBoundingClientRect(); return b.top >= 0 && b.bottom <= innerHeight;}), true, 'claim button below fold');
          await page.locator('input[type=radio]').first().check();
          assert.equal(await button.isEnabled(), true);
          assert.equal(await page.locator('[data-selected-assignment]').innerText(), await page.locator('.assignment-choice strong').first().innerText());
        }
        if (width === 375) await page.screenshot({path:path.join(path.dirname(file), `student-${name}-${theme}.png`), fullPage:true});
        checks++;
      }
    }
    await open('claim');
    await page.getByRole('button', {name:'수령 코드 복사', exact:true}).click();
    assert.equal(await page.evaluate(() => window.copied[0]), await page.locator('#claim-code').innerText());
    assert.match(await page.locator('[data-copy-status]').innerText(), /복사했습니다/); checks++;
    await page.evaluate(() => { window.failCopy = true; });
    await page.getByRole('button', {name:'서버 주소 복사', exact:true}).click();
    assert.match(await page.locator('[data-copy-status]').innerText(), /자동 복사를 사용할 수 없습니다/);
    assert.equal(await page.evaluate(() => window.getSelection().toString()), await page.locator('#api-address').innerText()); checks++;
    await open('claim');
    await page.clock.fastForward(601000);
    assert.match(await page.locator('[data-claim-expires]').innerText(), /만료되었습니다/);
    assert.equal(await page.getByRole('button', {name:'수령 코드 복사', exact:true}).isDisabled(), true); checks++;
    // Loading an already expired response must not restart its lifetime.
    await open('claim');
    assert.match(await page.locator('[data-claim-expires]').innerText(), /만료되었습니다/);
    assert.equal(await page.getByRole('button', {name:'수령 코드 복사', exact:true}).isDisabled(), true); checks++;
    await page.evaluate(() => {const el=document.createElement('script'); el.textContent='window.untrustedExecuted=true'; document.body.append(el);});
    assert.equal(await page.evaluate(() => window.untrustedExecuted), undefined); checks++;
    assert.equal(posts, 0); assert.deepEqual(errors, []);
    const nojs = await browser.newContext({javaScriptEnabled:false});
    const fallback = await nojs.newPage();
    await fallback.route('**/*', route => route.fulfill({contentType:'text/html', body:fixtures.assignments.html}));
    await fallback.goto('https://student.example.test/assignments');
    assert.equal(await fallback.locator('input[type=radio]').count(), 10);
    assert.equal(await fallback.getByRole('button', {name:'선택한 실습 수령 코드 발급', exact:true}).isEnabled(), true);
    assert.equal(await fallback.locator('form[data-assignment-picker]').getAttribute('method'), 'post'); checks++;
    console.log(`${checks} student browser layout/interaction/security scenarios passed`);
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
