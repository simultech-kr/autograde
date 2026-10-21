// Synthetic rendered pages; no live student data or credentials.
// node scripts/check_student_materials_browser.cjs FIXTURES_JSON PLAYWRIGHT_MODULE CHROME_BINARY OUTPUT_DIR
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const [fixture, modulePath, executablePath, output] = process.argv.slice(2);
const {chromium} = require(modulePath);

(async () => {
  const pages = JSON.parse(fs.readFileSync(fixture, 'utf8'));
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({headless:true, executablePath});
  let checks = 0;
  try {
    const page = await browser.newPage();
    for (const [name, html] of Object.entries(pages)) {
      for (const colorScheme of ['light', 'dark']) {
        await page.emulateMedia({colorScheme});
        for (const width of [320, 375, 768, 1440]) {
          await page.setViewportSize({width, height:1000});
          await page.setContent(html);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false,
            `${name}/${width}/${colorScheme}: page overflow`);
          assert.equal(await page.getByRole('heading', {level:1}).count(), 1);
          for (const form of await page.locator('form').all()) {
            if (await form.getAttribute('method') === 'post') {
              assert.equal(await form.locator('input[name="csrf"]').count(), 1);
            }
          }
          const content = await page.innerText('main');
          if (name === 'student-open') {
            assert.equal(await page.locator('form[action$="/download"]').count(), 1);
            assert.equal(await page.locator('form[action$="/download"]').getAttribute('method'), 'post');
            assert.ok(content.includes('배포 시작 전'));
            assert.ok(content.includes('20260001.pdf'));
            assert.ok(!content.includes('20260002.zip'), 'another student filename must not appear');
          }
          if (name === 'student-closed') {
            assert.equal(await page.locator('form[action$="/download"]').count(), 0);
            assert.ok(content.includes('배포 종료'));
          }
          if (name === 'student-login') {
            assert.equal(await page.locator('form[action$="/materials/login"]').count(), 1);
            assert.equal(await page.getByLabel('학생 전용 비밀번호').getAttribute('type'), 'password');
          }
          if (name === 'instructor-draft') {
            assert.equal(await page.getByLabel('개별 파일 (학번.pdf 또는 학번.zip)').count(), 1);
            assert.equal(await page.locator('form[action$="/upload-batch"] input[type="file"]').count(), 1);
            assert.ok(content.includes('활성 수강 중 미배정 3명'));
          }
          if (name === 'instructor-receipts') {
            assert.ok(content.includes('요청 기록 있음 1명'));
            assert.ok(content.includes('요청 기록 없음 1명'));
            assert.equal(await page.locator('input[type="file"]').count(), 0, 'published files are frozen');
          }
          if ((width === 1440 && colorScheme === 'light') || (width === 375 && colorScheme === 'dark')) {
            await page.screenshot({path:path.join(output, `${name}-${width}-${colorScheme}.png`), fullPage:true});
          }
          checks++;
        }
      }
    }
    await page.setContent(pages['student-open']);
    await page.emulateMedia({forcedColors:'active'});
    await page.setViewportSize({width:320, height:1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('.skip-link').evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main-content');
    console.log(`${checks} student-material viewport/theme scenarios plus keyboard/forced-colors passed`);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
