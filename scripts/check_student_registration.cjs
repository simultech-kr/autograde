// Browser integration against a disposable PilotFixture on loopback only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('../.cache/code-oss/node_modules/playwright');
const origin = new URL(process.argv[2] || 'http://127.0.0.1:18183');
assert.equal(origin.protocol, 'http:');
assert.equal(origin.hostname, '127.0.0.1');
assert.ok(!origin.username && !origin.password && origin.pathname === '/' && !origin.search);
const output = path.resolve('build/student-registration-ui');
fs.mkdirSync(output, {recursive: true});
const checks = [], layouts = [], failures = [];
const password = 'synthetic browser member password';
const studentKey = '20269991';
const course = '/courses/come2201';
const browserOptions = {headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'};
let browser;

async function layout(page, name) {
  for (const colorScheme of ['light', 'dark']) for (const width of [375, 1280]) {
    await page.setViewportSize({width, height: 900});
    await page.emulateMedia({colorScheme});
    const measured = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      clipped: [...document.querySelectorAll('input:not([type=hidden]),select,button,a')].filter(e => {
        const r = e.getBoundingClientRect();
        return r.width && r.height && (r.left < -1 || r.right > innerWidth + 1);
      }).length,
      small: [...document.querySelectorAll('input:not([type=hidden]),select,button')].filter(e => e.getBoundingClientRect().height < 44).length,
    }));
    assert.equal(measured.overflow, false, name + ': overflow');
    assert.equal(measured.clipped, 0, name + ': clipped');
    assert.equal(measured.small, 0, name + ': small controls');
    await page.screenshot({path: path.join(output, `${name}-${colorScheme}-${width}.png`), fullPage: true});
    layouts.push({name, colorScheme, width, passed: true});
  }
}

(async () => {
  browser = await chromium.launch(browserOptions);
  const student = await browser.newContext();
  const teacher = await browser.newContext({httpCredentials: {username: 'reviewer', password: 'synthetic-next-pilot-password'}});
  for (const context of [student, teacher]) {
    await context.route('**/*', route => {
      const target = new URL(route.request().url());
      return target.origin === origin.origin || ['data:', 'about:'].includes(target.protocol) ? route.continue() : route.abort();
    });
  }
  const page = await student.newPage(), instructor = await teacher.newPage();
  for (const item of [page, instructor]) item.on('pageerror', error => failures.push(error.message));
  await page.goto(origin.origin + '/student');
  await page.getByRole('link', {name: '회원가입', exact: true}).click();
  await layout(page, 'signup');
  await page.locator('[name=student_key]').fill(studentKey);
  await page.locator('[name=password]').fill(password);
  assert.equal(await page.locator('[name=course_key]').count(), 0);
  await page.getByRole('button', {name: '회원가입', exact: true}).click();
  await page.waitForURL('**/student/account');
  await page.locator('form[action="/student/enroll"]:has(input[value="come2201"])').getByRole('button', {name: '수강 신청', exact: true}).click();
  assert.match(await page.locator('body').innerText(), /수강 명단 확인 대기/);
  checks.push('signup creates only an account; separate course selection stays pending without roster and identity proof');
  await layout(page, 'pending');
  await instructor.goto(origin.origin + course + '/instructor/registrations');
  assert.match(await instructor.locator('body').innerText(), new RegExp(studentKey));
  const template = await instructor.request.get(origin.origin + course + '/instructor/registrations/template.csv');
  assert.equal(template.status(), 200); assert.match(await template.text(), /student_key/);
  await instructor.locator('input[type=file]').setInputFiles({name: 'students.csv', mimeType: 'text/csv', buffer: Buffer.from(`student_key\n${studentKey}\n${studentKey}\n`)});
  await instructor.getByRole('button', {name: '명단 변경 미리보기', exact: true}).click();
  assert.match(await instructor.locator('body').innerText(), /아직 명단을 변경하지 않았습니다/);
  await instructor.locator('[name=confirm]').check();
  await instructor.getByRole('button', {name: '검토한 명단 반영', exact: true}).click();
  await instructor.getByRole('link', {name: '가입·수강 명단 확인', exact: true}).click();
  assert.match(await instructor.locator('body').innerText(), /본인 확인 코드 입력 필요/);
  await instructor.locator('form[action$="/registrations/code"] [name=student_key]').fill(studentKey);
  await instructor.locator('form[action$="/registrations/code"] [name=reason]').fill('Synthetic browser student identity confirmed');
  await instructor.locator('form[action$="/registrations/code"] [name=confirm]').check();
  await instructor.getByRole('button', {name: '일회용 코드 발급', exact: true}).click();
  const activation = await instructor.locator('input[readonly]').inputValue();
  assert.match(activation, /^ACT1-/);
  await page.locator('form[action="/student/activate"] [name=token]').fill(activation);
  await page.getByRole('button', {name: '계정 활성화', exact: true}).click();
  await instructor.goto(origin.origin + course + '/instructor/registrations');
  assert.match(await instructor.locator('body').innerText(), /명단 일치 · 연결 완료/);
  checks.push('instructor reviews roster impact, confirms upload and issues an identity-bound single-use activation code');
  await layout(instructor, 'roster-matched');
  await page.getByRole('link', {name: '명단 연결 상태 새로고침'}).click();
  assert.match(await page.locator('body').innerText(), /수강 중/);
  await layout(page, 'account-matched');
  await page.getByRole('link', {name: '내 수업 열기', exact: true}).click();
  await page.getByRole('link', {name: '과제 받기', exact: true}).click();
  await page.locator('input[name=assignment_id]').first().check();
  await page.getByRole('button', {name: '선택한 실습 수령 코드 발급'}).click();
  assert.match(await page.locator('body').innerText(), /AK1-/);
  checks.push('same common session opens matched course and issues assignment claim code');
  await page.goto(origin.origin + '/student/account');
  await page.getByRole('button', {name: '로그아웃', exact: true}).click();
  await page.getByRole('link', {name: '로그인', exact: true}).click();
  await layout(page, 'member-login');
  await page.locator('[name=student_key]').fill(studentKey);
  await page.locator('[name=password]').fill(password);
  await page.getByRole('button', {name: '로그인', exact: true}).click();
  await page.waitForURL('**/student/account');
  assert.match(await page.locator('body').innerText(), /수강 중/);
  checks.push('logout clears member access and account password logs back into SQLite enrollment');
  const legacy = await browser.newContext();
  await legacy.route('**/*', route => new URL(route.request().url()).origin === origin.origin ? route.continue() : route.abort());
  const old = await legacy.newPage();
  await old.goto(origin.origin + course);
  await old.locator('[name=student_key]').fill('20260001');
  await old.locator('[name=password]').fill('000001');
  await old.getByRole('button', {name: '과제 확인', exact: true}).click();
  await old.getByRole('link', {name: '기존 학생 · 회원 계정 만들기'}).click();
  assert.equal(await old.locator('[name=student_key]').count(), 0);
  await layout(old, 'legacy-activation');
  await old.locator('[name=password]').fill(password);
  await old.getByRole('button', {name: '회원 계정 만들기', exact: true}).click();
  await old.waitForURL('**/student/account');
  assert.match(await old.locator('body').innerText(), /20260001/);
  checks.push('legacy course password proves existing identity before common-account activation');
  assert.deepEqual(failures, []);
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({status: 'passed', synthetic_data_only: true,
    checks, layouts, page_errors: failures}, null, 2) + '\n');
  console.log(JSON.stringify({status: 'passed', checks: checks.length, layouts: layouts.length}));
})().catch(error => {console.error(error); process.exitCode = 1;}).finally(async () => {await browser?.close();});
