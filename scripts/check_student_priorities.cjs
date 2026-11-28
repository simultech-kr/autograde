// Run against a production Next build bound to loopback; the fixture starts and stops its own SQLite server.
// node scripts/check_student_priorities.cjs NEXT_ORIGIN FIXTURE_PORT [PYTHON] [PLAYWRIGHT_MODULE] [CHROME_BINARY]
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const path = require('node:path');
const {createInterface} = require('node:readline');

const root = path.resolve(__dirname, '..');
const [nextArg, portArg, python = 'python3', moduleName = 'playwright', executablePath] = process.argv.slice(2);
const viewports = [{width: 390, height: 844}, {width: 1440, height: 900}];
let fixtureProcess;

function options() {
  const url = new URL(nextArg);
  assert(url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.href === url.origin + '/' && url.port,
    'NEXT_ORIGIN must be a loopback http://127.0.0.1:PORT origin');
  const port = Number(portArg);
  assert(/^\d+$/.test(portArg) && Number.isInteger(port) && port >= 1024 && port <= 65535 && port !== Number(url.port),
    'FIXTURE_PORT must be a distinct local port between 1024 and 65535');
  return {next: url, port, origin: 'http://127.0.0.1:' + port};
}

async function withFixture({next, port, origin}, run) {
  const child = spawn(python, ['scripts/check_student_portal_fixture.py', '--scenario', 'priorities',
    '--auth-method', 'member', '--port', String(port), '--next-port', next.port], {
    cwd: root, stdio: ['ignore', 'pipe', 'inherit'],
    env: {...process.env, PYTHONPATH: [path.join(root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)},
  });
  fixtureProcess = child;
  const lines = createInterface({input: child.stdout});
  const startup = AbortSignal.timeout(30000);
  const closed = once(child, 'close');
  try {
    const [line] = await Promise.race([
      once(lines, 'line', {signal: startup}),
      closed.then(() => {throw new Error('Student fixture exited before startup');}),
    ]);
    // The bootstrap line contains only synthetic credentials. Keep it in memory.
    const fixture = JSON.parse(line);
    assert.equal(fixture.url, origin + '/app/student');
    await run(fixture);
  } finally {
    lines.close();
    child.kill('SIGTERM');
    await closed;
    fixtureProcess = undefined;
  }
}

async function login(page, fixture) {
  await page.goto(fixture.url);
  await page.locator('[name=student_key]').fill(fixture.student_key);
  await page.locator('[name=password]').fill(fixture.password);
  await page.locator('form').getByRole('button', {name:'로그인', exact:true}).click();
}

async function checkConsent(page, fixture) {
  await login(page, fixture);
  const dialog = page.getByRole('dialog');
  const select = dialog.getByLabel('이번에 선택할 수업');
  await select.waitFor();
  const names = await select.locator('option').allInnerTexts();
  for (const section of ['01분반', '02분반']) {
    assert(names.some(name => name.includes('실습 설계') && name.includes('2학기') && name.includes(section)));
  }
  await select.selectOption({label:names.find(name => name.includes('02분반'))});
  await dialog.getByRole('button', {name:'동의하지 않습니다', exact:true}).click();
  await page.getByRole('status').filter({hasText:/02분반.*동의하지/}).waitFor();
  async function decisions() {
    const reply = await page.request.get(new URL('/student/api/privacy', fixture.url).href);
    assert.equal(reply.status(), 200);
    const {consents} = (await reply.json()).result;
    return Object.fromEntries(consents.map(row => [row.course_key, row.decision]));
  }
  assert.deepEqual(await decisions(), {come2201:'unanswered', come3105:'declined'});
  await page.getByRole('navigation', {name:'학생 메뉴'}).getByRole('link', {name:'정보 활용 설정'}).click();
  const settings = page.getByRole('region', {name:'정보 활용 설정'});
  await settings.getByRole('button', {name:/01분반/}).click();
  await settings.getByRole('button', {name:'동의합니다', exact:true}).click();
  await settings.getByRole('status').filter({hasText:/01분반.*동의했/}).waitFor();
  await page.reload();
  await settings.getByRole('button', {name:/02분반.*동의하지 않음/}).waitFor();
  assert.deepEqual(await decisions(), {come2201:'granted', come3105:'declined'});
  return '01분반 동의 · 02분반 거절';
}

async function checkScores(page, fixture) {
  await login(page, fixture);
  const card = page.locator('[data-score-assignment="qa_feedback"]');
  async function visibleScore() {
    await card.waitFor();
    await page.waitForFunction(() => {
      const card = document.querySelector('[data-score-assignment="qa_feedback"]');
      const score = card?.querySelector('dl dd')?.getBoundingClientRect();
      return card?.contains(document.activeElement) && score && score.top >= 0 && score.bottom <= innerHeight;
    });
    const values = card.locator('dl dd');
    assert.equal(await values.nth(0).innerText(), '35 / 100점');
    assert.equal(await values.nth(1).innerText(), '95 / 100점');
    const rect = await values.nth(0).boundingBox();
    return Math.round(rect.y);
  }
  await page.getByRole('button', {name:/Observer.*평가 결과 보기/}).click();
  const home = await visibleScore();
  await page.getByRole('navigation', {name:'수업 메뉴'}).getByRole('link', {name:'실습 과제', exact:true}).click();
  await page.locator('[data-assignment="qa_feedback"]').getByRole('button', {name:/평가 결과 보기/}).click();
  const list = await visibleScore();
  await page.reload();
  const direct = await visibleScore();
  return '이번 35/100 · 이전 최고 95/100 · 점수 상단(홈/목록/새로고침) ' + [home, list, direct].join('/') + 'px';
}

async function checkActivation(page, fixture) {
  await page.goto(fixture.url);
  await page.getByRole('button', {name:'회원가입', exact:true}).click();
  await page.locator('[name=student_key]').fill(fixture.fixture.fresh_student);
  await page.locator('[name=password]').fill('Synthetic portal test PW 3491');
  await page.getByRole('button', {name:'가입하고 수업 선택'}).click();
  await page.getByRole('checkbox', {name:/01분반/}).check();
  await page.getByRole('button', {name:'선택한 1개 수업 신청'}).click();
  await page.getByRole('heading', {name:'내 수업', exact:true}).waitFor();
  const account = page.getByRole('navigation', {name:'학생 메뉴'}).getByRole('link', {name:'내 계정', exact:true});
  await account.click();
  const field = page.getByLabel('교수자가 제공한 활성화 코드');
  const accountURL = page.url();
  async function activate(value) {
    await field.fill(value);
    const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url() === new URL('/student/api/activate', fixture.url).href);
    await page.getByRole('button', {name:'계정 활성화', exact:true}).click();
    return await response;
  }
  const wrong = await activate('INCORRECT-SYNTHETIC-CODE');
  assert.equal(wrong.status(), 403);
  assert.equal((await wrong.json()).error.code, 'invalid_activation_code');
  await page.waitForFunction(() => document.querySelector('input[name="token"][aria-invalid="true"]') === document.activeElement);
  assert.equal(await field.inputValue(), '');
  const guidance = await field.evaluate(el => document.getElementById(el.getAttribute('aria-describedby'))?.textContent);
  assert.match(guidance, /새 코드를 요청/);
  assert.equal(page.url(), accountURL);
  const fixed = await activate(fixture.fixture.fresh_token);
  assert.equal(fixed.status(), 200);
  const {student, courses} = (await fixed.json()).result;
  assert(student.verified && courses.some(course => course.course_key === fixture.course_key && course.enrolled));
  return '오타 후 같은 화면·입력란 초점 · 재입력 후 수강 연결';
}

async function main() {
  const config = options();
  const health = await fetch(new URL('/app/healthz', config.next), {redirect:'error', signal:AbortSignal.timeout(5000)});
  assert(health.ok, 'Next.js health check failed');
  const {chromium} = require(moduleName.startsWith('.') ? path.resolve(moduleName) : moduleName);
  const browser = await chromium.launch({headless:true, executablePath});
  try {
    for (const viewport of viewports) {
      await withFixture(config, async fixture => {
        for (const [label, check] of [['분반 동의', checkConsent], ['과제별 결과', checkScores], ['활성화 복구', checkActivation]]) {
          const context = await browser.newContext({viewport, locale:'ko-KR', serviceWorkers:'block'});
          try {
            await context.route('**/*', route => new URL(route.request().url()).origin === config.origin ? route.continue() : route.abort());
            const page = await context.newPage();
            page.setDefaultTimeout(10000);
            console.log(viewport.width + '×' + viewport.height, label, await check(page, fixture));
          } finally {
            await context.close();
          }
        }
      });
    }
  } finally {
    await browser.close();
  }
}

process.on('exit', () => fixtureProcess?.kill('SIGTERM'));
process.once('SIGINT', () => process.exit(130));
process.once('SIGTERM', () => process.exit(143));
main().catch(error => { console.error(error.message); process.exitCode = 1; });
