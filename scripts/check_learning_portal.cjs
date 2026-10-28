// Read-only browser QA for scripts/check_learning_runtime.py --serve.
// Synthetic student 20260002 only; does not issue claims or modify IDE sessions.
// node scripts/check_learning_portal.cjs ORIGIN OUTPUT PLAYWRIGHT_MODULE CHROME
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [input, output, modulePath, executablePath] = process.argv.slice(2);
const origin = new URL(input);
assert.equal(origin.protocol, 'http:');
assert.equal(origin.hostname, '127.0.0.1');
assert.equal(origin.pathname, '/');
assert.ok(!origin.username && !origin.password && !origin.search && !origin.hash);
assert.ok(output && modulePath && executablePath);
const {chromium} = require(modulePath);
const checks = [], layouts = [], errors = [], checkedLinks = new Set();
fs.mkdirSync(output, {recursive: true});
fs.rmSync(path.join(output, 'report.json'), {force: true});
const url = route => origin.origin + route;

async function navigate(page, route) {
  let last;
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      const response = await page.goto(url(route), {waitUntil: 'domcontentloaded', timeout: 15000});
      if (response.status() < 500) return response;
      last = new Error(`${route} returned ${response.status()}`);
    } catch (error) { last = error; }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw last;
}

async function layout(page, name) {
  for (const theme of ['light', 'dark']) for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({width, height: 900});
    await page.emulateMedia({colorScheme: theme});
    await page.evaluate(() => document.fonts.ready);
    const geometry = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      clipped: Array.from(document.querySelectorAll('a[href], input, select, button')).filter(element => {
        const r = element.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (r.left < -1 || r.right > innerWidth + 1);
      }).map(element => element.tagName + ':' + (element.getAttribute('href') || element.getAttribute('name') || element.textContent)),
      current: document.querySelectorAll('nav [aria-current="page"]').length,
      brokenAnchors: Array.from(document.querySelectorAll('a[href^="#"]')).map(a => a.getAttribute('href').slice(1))
        .filter(id => id && !document.getElementById(id)),
    }));
    assert.equal(geometry.overflow, false, `${name} ${theme} ${width}: horizontal overflow`);
    assert.deepEqual(geometry.clipped, [], `${name} ${theme} ${width}: clipped controls/links`);
    assert.ok(geometry.current <= 1, `${name}: ambiguous active navigation`);
    assert.deepEqual(geometry.brokenAnchors, [], `${name}: broken in-page destinations`);
    if ((theme === 'light' && width === 1440) || (theme === 'dark' && width === 375)) {
      await page.screenshot({path: path.join(output, `${name}-${theme}-${width}.png`), fullPage: true});
      await page.screenshot({path: path.join(output, `${name}-${theme}-${width}-viewport.png`), fullPage: false});
    }
    layouts.push({page: name, theme, width, passed: true});
  }
}

async function verifyLocalLinks(page, role) {
  const links = await page.locator('a[href]').evaluateAll(elements => [...new Set(elements.map(e => e.getAttribute('href')))]);
  for (const href of links) {
    if (!href.startsWith('/') || href.startsWith('//') || href.startsWith('/app')) continue;
    if (role === 'student' && href.startsWith('/instructor')) continue;
    const target = href.split('#')[0];
    const key = role + ':' + target;
    if (checkedLinks.has(key)) continue;
    const response = await page.request.get(url(target), {maxRedirects: 0});
    assert.ok(response.status() === 200 || response.status() === 303,
      `${role} link ${target} returned ${response.status()}`);
    checkedLinks.add(key);
  }
}

async function context(browser, options = {}) {
  const ctx = await browser.newContext(options);
  await ctx.route('**/*', route => new URL(route.request().url()).origin === origin.origin ? route.continue() : route.abort());
  const page = await ctx.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  return {ctx, page};
}

async function main() {
  const browser = await chromium.launch({headless: true, executablePath});
  try {
    const anonymous = await context(browser);
    assert.equal((await navigate(anonymous.page, '/')).status(), 200);
    await anonymous.page.getByRole('heading', {name: 'Autograde 수업 포털', exact: true}).waitFor();
    assert.equal(await anonymous.page.locator('a[href="/student"]').count(), 1);
    assert.equal(await anonymous.page.locator('a[href="/instructor"]').count(), 1);
    assert.doesNotMatch(await anonymous.page.locator('body').innerText(), /2026000[12]|lcp_|lr_/);
    await layout(anonymous.page, 'role-entry');
    assert.equal((await anonymous.page.request.get(url('/instructor'))).status(), 401);
    await navigate(anonymous.page, '/courses/come2201/portal');
    assert.equal(await anonymous.page.locator('input[type="password"]').count(), 1);
    assert.equal(await anonymous.page.locator('[data-accepted-assignment]').count(), 0);
    assert.equal(await anonymous.page.locator('[data-runtime-state]').count(), 0);
    assert.doesNotMatch(await anonymous.page.locator('body').innerText(), /2026000[12]|lcp_|lr_/);
    await layout(anonymous.page, 'student-login');
    checks.push('Anonymous role entry and login never disclose student identities, acceptances or spaces; instructor requires authentication');
    await anonymous.ctx.close();

    const teacher = await context(browser, {httpCredentials: {username: 'teacher', password: 'synthetic-next-pilot-password'}});
    assert.equal((await navigate(teacher.page, '/instructor')).status(), 200);
    assert.equal(await teacher.page.locator('article[data-course-key="come2201"]').count(), 1);
    assert.equal(await teacher.page.locator('article[data-course-key="come3105"]').count(), 0);
    assert.equal(await teacher.page.locator('a[href="/instructor/admin#course-registration"]').count(), 0);
    await layout(teacher.page, 'teacher-home');
    await verifyLocalLinks(teacher.page, 'teacher');
    await teacher.page.getByLabel('수업 찾기', {exact: true}).fill('come2201');
    await teacher.page.getByRole('button', {name: '조회', exact: true}).click();
    assert.match(teacher.page.url(), /q=come2201/);
    assert.equal(await teacher.page.locator('article[data-course-key="come2201"]').count(), 1);
    await teacher.page.getByLabel('운영 상태', {exact: true}).selectOption('archived');
    await teacher.page.getByRole('button', {name: '조회', exact: true}).click();
    assert.equal(await teacher.page.locator('article[data-course-key]').count(), 0);
    assert.match(await teacher.page.locator('body').innerText(), /조건에 맞는 수업이 없습니다/);
    checks.push('Teacher course search and status filters work through real GET HTTP; grants and administrator controls stay scoped');
    assert.equal((await navigate(teacher.page, '/courses/come2201/instructor')).status(), 200);
    assert.ok(await teacher.page.locator('a[href="/courses/come2201/instructor/environments"]').count() >= 1);
    await layout(teacher.page, 'teacher-course');
    await verifyLocalLinks(teacher.page, 'teacher');
    assert.equal((await navigate(teacher.page, '/courses/come2201/instructor/environments')).status(), 200);
    assert.equal(await teacher.page.locator('.portal-runtime-status[data-state="ready"]').count(), 1);
    assert.equal(await teacher.page.locator('form[method="post"]').count(), 0);
    assert.doesNotMatch(await teacher.page.locator('body').innerText(), /worker\.sock|\/private\/|\/tmp\/|Bearer /);
    await layout(teacher.page, 'teacher-environments');
    await verifyLocalLinks(teacher.page, 'teacher');
    assert.equal((await teacher.page.request.get(url('/courses/come3105/instructor'))).status(), 404);
    assert.equal((await teacher.page.request.get(url('/courses/come3105/instructor/environments'))).status(), 404);
    checks.push('Teacher environment page shows actual worker connection, has no mutation forms, omits internal paths and rejects another course');
    await teacher.ctx.close();

    const student = await context(browser);
    await navigate(student.page, '/student');
    await student.page.locator('a[href="/courses/come2201/portal"]').click();
    await student.page.getByLabel('학번', {exact: true}).fill('20260002');
    await student.page.getByLabel('학생 전용 비밀번호', {exact: true}).fill('000002');
    await student.page.getByRole('button', {name: '내 수업으로 이동', exact: true}).click();
    await student.page.getByRole('heading', {name: '내 수업', exact: true}).waitFor();
    assert.equal(await student.page.locator('input[type="password"]').count(), 0);
    assert.equal(await student.page.locator('#accepted-assignments').count(), 1);
    assert.equal(await student.page.locator('#learning-space').count(), 1);
    assert.equal(await student.page.locator('[data-runtime-state="ready"]').count(), 1);
    const text = await student.page.locator('body').innerText();
    assert.match(text, /20260002/);
    assert.doesNotMatch(text, /20260001|20260003|worker\.sock|\/private\/|Bearer /);
    await layout(student.page, 'student-home');
    await verifyLocalLinks(student.page, 'student');
    const privateResponse = await student.page.request.get(url('/courses/come2201/portal'));
    assert.equal(privateResponse.headers()['cache-control'], 'no-store');
    assert.equal((await student.page.request.get(url('/courses/come2201/portal?student_id=1'))).status(), 400);
    const otherCourse = await student.page.request.get(url('/courses/come3105/portal'));
    assert.match(await otherCourse.text(), /type="password"/);
    await student.page.locator('nav a[href="/courses/come2201/results"]').click();
    await student.page.getByRole('heading', {name: '내 제출 현황', exact: true}).waitFor();
    assert.doesNotMatch(await student.page.locator('body').innerText(), /20260001|20260003/);
    await layout(student.page, 'student-results');
    checks.push('Student 20260002 sees own acceptance and remote-space section, no other identities; course isolation, query rejection and no-store remain intact');
    await student.ctx.close();

    assert.deepEqual(errors, []);
    const report = {ok: true, checkCount: checks.length, layoutCount: layouts.length, checks, layouts,
      linkCount: checkedLinks.size, links: [...checkedLinks], pageErrors: errors,
      fixture: 'Synthetic local remote Linux fixture; read-only browser flow, no claims issued',
      productionTested: false};
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ok: report.ok, checkCount: report.checkCount, layoutCount: report.layoutCount,
      linkCount: report.linkCount, report: path.join(output, 'report.json')}));
  } finally {await browser.close();}
}

main().catch(error => {
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ok: false, error: String(error),
    completedChecks: checks, layoutCount: layouts.length, productionTested: false}, null, 2) + '\n');
  console.error(error); process.exitCode = 1;
});
