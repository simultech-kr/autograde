// Synthetic local portal QA. Start scripts/check_nextjs_pilot.py --serve first.
// node scripts/check_role_portals.cjs ORIGIN OUTPUT PLAYWRIGHT_MODULE CHROME [TEACHER_HTML_ROOT]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [input, output, modulePath, executablePath, teacherFixtures] = process.argv.slice(2);
const origin = new URL(input);
assert.equal(origin.protocol, 'http:'); assert.equal(origin.hostname, '127.0.0.1');
assert.equal(origin.pathname, '/'); assert.ok(!origin.username && !origin.password && !origin.search && !origin.hash);
assert.ok(output && modulePath && executablePath);
const {chromium} = require(modulePath);
const checks = [], errors = []; let layouts = 0;
fs.mkdirSync(output, {recursive: true});
const files = root => fs.readdirSync(root, {withFileTypes: true}).flatMap(entry => {
  const p = path.join(root, entry.name);
  return entry.isDirectory() ? files(p) : /^portal-instructor-.*\.html$/.test(entry.name) ? [p] : [];
});
async function responsive(page, name) {
  for (const theme of ['light', 'dark', 'contrast']) for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({width, height: 900});
    await page.emulateMedia({colorScheme: theme === 'dark' ? 'dark' : 'light', forcedColors: theme === 'contrast' ? 'active' : 'none'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${name}: ${theme} ${width} overflow`);
    const current = await page.locator('nav [aria-current="page"]').count();
    assert.ok(current <= 1, `${name} has ambiguous active navigation`);
    const badLinks = await page.locator('a[href]').evaluateAll(links => links.filter(a => {
      const r = a.getBoundingClientRect(); return r.width > 0 && (r.left < -1 || r.right > innerWidth + 1);
    }).map(a => a.getAttribute('href')));
    assert.deepEqual(badLinks, [], `${name}: clipped links at ${width}`);
    if ((theme === 'light' && width === 1440) || (theme === 'dark' && width === 375)) {
      await page.screenshot({path: path.join(output, `${name}-${theme}-${width}.png`), fullPage: name !== 'student-results'});
      await page.screenshot({path: path.join(output, `${name}-${theme}-${width}-viewport.png`), fullPage: false});
    }
    layouts++;
  }
}
async function main() {
  const browser = await chromium.launch({headless: true, executablePath});
  try {
    const student = await browser.newContext();
    await student.route('**/*', route => new URL(route.request().url()).origin === origin.origin ? route.continue() : route.abort());
    const page = await student.newPage(); page.on('pageerror', error => errors.push(String(error)));
    await page.goto(origin.origin + '/');
    await page.getByRole('heading', {name: 'Autograde 수업 포털', exact: true}).waitFor();
    assert.equal(await page.locator('a[href="/instructor"]').count(), 1);
    assert.ok(!(await page.locator('body').innerText()).includes('20260001'));
    await responsive(page, 'role-entry');
    await page.locator('a[href="/student"]').first().click();
    await page.locator('a[href="/courses/come2201/portal"]').click();
    await page.getByLabel('학번', {exact: true}).fill('20260001');
    await page.getByLabel('학생 전용 비밀번호', {exact: true}).fill('000001');
    await page.getByRole('button', {name: '내 수업으로 이동', exact: true}).click();
    await page.getByRole('heading', {name: '내 수업', exact: true}).waitFor();
    assert.equal(await page.locator('input[type="password"]').count(), 0);
    assert.ok(!(await page.locator('body').innerText()).includes('20260002'));
    await responsive(page, 'student-home');
    checks.push('Role entry and student chooser reach course-bound login and authenticated home');
    await page.locator('nav a[href="/courses/come2201/results"]').click();
    await page.getByRole('heading', {name: '내 제출 현황', exact: true}).waitFor();
    const first = page.locator('[data-submission-id]').first();
    assert.match(await first.innerText(), /이번 제출 점수\s*0\s*\/\s*100점/);
    assert.match(await first.innerText(), /이전 제출 중 최고점\s*100\s*\/\s*100점/);
    assert.ok(!(await page.locator('body').innerText()).includes('20260002'));
    assert.equal((await page.request.get(origin.origin + '/courses/come2201/results')).headers()['cache-control'], 'no-store');
    await responsive(page, 'student-results');
    checks.push('Current score zero and previous best 100 stay separate; private page uses no-store');
    await page.locator('nav a[href="/courses/come2201/assignments"]').click();
    await page.locator('input[name="assignment_id"]').first().check();
    await page.getByRole('button', {name: '선택한 실습 수령 코드 발급', exact: true}).click();
    await page.getByRole('heading', {name: '과제 수령 코드', exact: true}).waitFor();
    // Do not capture/log the synthetic claim itself. It must close the web session.
    await page.goto(origin.origin + '/courses/come2201/results');
    await page.getByRole('heading', {name: '제출 현황 로그인', exact: true}).waitFor();
    assert.equal(await page.locator('[data-submission-id]').count(), 0);
    checks.push('Claim issuance ends the student web session and hides private history until reauthentication');
    await student.close();

    const teacher = await browser.newContext({httpCredentials: {username: 'teacher', password: 'synthetic-next-pilot-password'}});
    const taught = await teacher.newPage(); taught.on('pageerror', error => errors.push(String(error)));
    await taught.goto(origin.origin + '/instructor');
    await taught.locator('article[data-course-key="come2201"]').waitFor();
    assert.equal(await taught.locator('article[data-course-key="come3105"]').count(), 0);
    assert.equal(await taught.locator('a[href="/instructor/admin#course-registration"]').count(), 0);
    await responsive(taught, 'teacher-home');
    await taught.locator('a[href="/courses/come2201/instructor"]').filter({hasText: '수업 열기'}).click();
    assert.ok((await taught.locator('body').innerText()).includes('수업 개요'));
    assert.equal((await taught.request.get(origin.origin + '/courses/come3105/instructor')).status(), 404);
    await responsive(taught, 'teacher-course');
    checks.push('Teacher portal shows only granted course, hides administrator registration and rejects another course');
    await teacher.close();

    if (teacherFixtures) {
      const staticContext = await browser.newContext();
      await staticContext.route('**/*', route => route.abort());
      const staticPage = await staticContext.newPage(); staticPage.on('pageerror', error => errors.push(String(error)));
      const fixtures = files(teacherFixtures); assert.ok(fixtures.length >= 3);
      for (const file of fixtures) {
        await staticPage.setContent(fs.readFileSync(file, 'utf8'));
        await responsive(staticPage, path.basename(file, '.html'));
      }
      await staticContext.close();
      checks.push('Administrator and all optional-module navigation layouts remain readable across viewport and themes');
    }
    assert.deepEqual(errors, []);
    const result = {ok: true, layouts, checks, pageErrors: errors, fixture: 'local synthetic accounts only', productionTested: false};
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {await browser.close();}
}
main().catch(error => {console.error(error); process.exitCode = 1;});
