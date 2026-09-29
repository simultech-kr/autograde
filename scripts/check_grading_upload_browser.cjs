// Real synthetic authoring responses; every request is fulfilled locally, never sent to a server.
// node scripts/check_grading_upload_browser.cjs FIXTURE PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), assert = require('node:assert/strict');
const [fixturePath, modulePath = 'playwright', executablePath] = process.argv.slice(2);
const {chromium} = require(modulePath);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
(async () => {
  const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
  let checks = 0;
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    let state = 'initial', posts = [], dialogs = [], submitted = [];
    await page.exposeFunction('recordUploadSubmit', entry => submitted.push(entry));
    await page.addInitScript(() => {
      window.invalidFields = [];
      document.addEventListener('invalid', event => window.invalidFields.push(event.target.name), true);
      window.addEventListener('submit', event => window.recordUploadSubmit({
        cancelled: event.defaultPrevented,
        status: event.target.querySelector('[data-save-status]')?.textContent || '',
      }));
    });
    await page.route('**/*', async route => {
      const request = route.request();
      assert.equal(new URL(request.url()).origin, new URL(fixture.url).origin);
      if (request.method() === 'POST') {
        const path = new URL(request.url()).pathname;
        posts.push(path);
        state = path.endsWith('/uploads/starter') ? 'starter' : path.endsWith('/grading-template') ? 'grading' : 'problem';
        await route.fulfill({status: 303, headers: {Location: fixture.url}, body: ''});
      } else {
        assert.equal(request.method(), 'GET');
        await route.fulfill({contentType: 'text/html', headers: {'Content-Security-Policy': fixture.csp}, body: fixture.states[state]});
      }
    });
    const upload = () => page.locator('form[action$="/grading-template"]');
    const status = () => upload().locator('[data-save-status]');
    const file = {name: 'grading-private.zip', mimeType: 'application/zip', buffer: Buffer.from('synthetic-never-sent-to-server')};
    const open = async value => {
      state = value; posts = []; dialogs = []; submitted = [];
      page.once('dialog', dialog => dialog.accept());
      await page.goto(fixture.url);
      page.removeAllListeners('dialog');
    };
    const submit = async button => Promise.all([page.waitForEvent('domcontentloaded'), button.click()]);
    const uploadButton = () => upload().getByRole('button', {name: '채점 템플릿 한 번에 저장', exact: true});

    await open('initial');
    assert.equal(await page.locator('form form').count(), 0);
    assert.equal(await page.locator('[data-save-status]').filter({hasText: '미저장'}).count(), 0); checks++;
    await page.locator('form[data-form-kind=problem] [name=description]').fill('저장한 문제 설명');
    await submit(page.getByRole('button', {name: '문제·일정 저장', exact: true}));
    const starter = page.locator('form[action$="/uploads/starter"]');
    await starter.locator('[type=file]').setInputFiles({...file, name: 'starter.zip'});
    assert.match(await starter.locator('[data-save-status]').innerText(), /파일 선택됨.*아직 서버에 저장되지/); checks++;
    await starter.locator('[name=starter_confirm]').check();
    await submit(starter.getByRole('button', {name: '자료 등록', exact: true}));
    await upload().locator('[type=file]').setInputFiles(file);
    assert.match(await status().innerText(), /파일 선택됨.*아직 서버에 저장되지/); checks++;
    await upload().locator('[name=grading_confirm]').check();
    await submit(uploadButton());
    assert.equal(posts.length, 3);
    assert.match(submitted[1].status, /업로드 중.*서버 저장 완료를 기다려/);
    assert.match(submitted[2].status, /업로드 중.*서버 저장 완료를 기다려/);
    assert.ok(submitted.every(item => !item.cancelled));
    assert.match(await page.locator('.draft-summary').innerText(), /교수자 채점 자료\n서버 등록 완료/);
    assert.match(await page.locator('main').innerText(), /저장 버전 4/); checks++;

    await open('starter');
    await upload().locator('[type=file]').setInputFiles(file);
    await uploadButton().click();
    assert.equal(posts.length, 0);
    assert.deepEqual(await page.evaluate(() => invalidFields), ['grading_confirm']);
    assert.match(await status().innerText(), /필수 검토 확인란.*아직 서버로 전송되지/);
    assert.equal(await upload().locator('[name=grading_confirm]').getAttribute('required'), ''); checks++;
    await upload().locator('[name=grading_confirm]').check();
    assert.match(await status().innerText(), /파일 선택됨.*아직 서버에 저장되지/); checks++;

    await open('starter');
    await upload().locator('[name=grading_confirm]').check();
    await uploadButton().click();
    assert.equal(posts.length, 0);
    assert.deepEqual(await page.evaluate(() => invalidFields), ['file']);
    assert.match(await status().innerText(), /ZIP 파일을 선택.*아직 서버로 전송되지/); checks++;

    await open('starter');
    await uploadButton().click();
    assert.equal(posts.length, 0);
    assert.deepEqual(await page.evaluate(() => invalidFields), ['file', 'grading_confirm']);
    assert.match(await status().innerText(), /ZIP 파일을 선택.*필수 검토 확인란.*서버로 전송되지/); checks++;

    await open('starter');
    await page.locator('form[data-form-kind=problem] [name=description]').fill('저장하지 않은 설명');
    await upload().locator('[type=file]').setInputFiles(file);
    await upload().locator('[name=grading_confirm]').check();
    page.once('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
    await uploadButton().click();
    assert.equal(posts.length, 0);
    assert.equal(dialogs.length, 1);
    assert.match(await status().innerText(), /저장을 취소했습니다.*서버로 전송되지/);
    assert.equal(await page.evaluate(() => !window.dispatchEvent(new Event('beforeunload', {cancelable: true}))), true); checks++;
    page.once('dialog', dialog => dialog.accept());
    await submit(uploadButton());
    assert.equal(posts.length, 1);
    assert.match(submitted.at(-1).status, /업로드 중/); checks++;

    await open('problem');
    const pendingStarter = page.locator('form[action$="/uploads/starter"]');
    await pendingStarter.locator('[type=file]').setInputFiles({...file, name: 'starter.zip'});
    await pendingStarter.locator('[name=starter_confirm]').check();
    await upload().locator('[type=file]').setInputFiles(file);
    await upload().locator('[name=grading_confirm]').check();
    page.once('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.accept(); });
    await submit(pendingStarter.getByRole('button', {name: '자료 등록', exact: true}));
    assert.equal(posts.length, 1);
    assert.equal(dialogs.length, 1);
    assert.match(dialogs[0], /파일 선택은 다음 화면에서 유지되지 않습니다.*파일은 영역별로 하나씩 저장/);
    assert.ok(posts[0].endsWith('/uploads/starter'));
    assert.equal(await upload().locator('[type=file]').inputValue(), '');
    assert.equal(await upload().locator('[name=grading_confirm]').isChecked(), false); checks++;

    await open('invalid');
    assert.match(await page.locator('[role=alert]').innerText(), /올바른 채점 템플릿 ZIP/);
    assert.equal(await upload().locator('[type=file]').inputValue(), '');
    assert.equal(await upload().locator('[name=grading_confirm]').isChecked(), true);
    await uploadButton().click();
    assert.equal(posts.length, 0);
    assert.deepEqual(await page.evaluate(() => invalidFields), ['file']);
    assert.match(await status().innerText(), /ZIP 파일을 선택.*서버로 전송되지/); checks++;
    await upload().locator('[type=file]').setInputFiles(file);
    assert.match(await status().innerText(), /파일 선택됨.*아직 서버에 저장되지/);
    await submit(uploadButton());
    assert.equal(posts.length, 1);
    assert.match(submitted.at(-1).status, /업로드 중/); checks++;
    assert.deepEqual(errors, []);
    console.log(`${checks} grading upload browser feedback checks passed`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
