// Design-only fixture. Never loads an authenticated browser or a live server.
// node scripts/check_instructor_wireframe.cjs OUTPUT_DIR PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const [output, modulePath = 'playwright', executablePath] = process.argv.slice(2);
if (!output || !fs.statSync(output).isDirectory()) throw new Error('Provide an existing test output directory');
const {chromium} = require(modulePath);
const fragment = fs.readFileSync(path.join(__dirname, '../design/prototypes/instructor-ux-wireframe.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  const errors = [], requests = [];
  let layouts = 0;
  try {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {requests.push(route.request().url()); return route.abort();});
    const root = page.locator('#ag-instructor-wireframe');
    const next = () => root.locator('[data-action="next"]').click();
    async function load(width, colorScheme) {
      await page.setViewportSize({width,height:1000});
      await page.emulateMedia({colorScheme});
      await page.setContent(`<!doctype html><html lang="ko"><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>:root{color-scheme:light dark}body{margin:0}</style></head><body>${fragment}</body></html>`);
    }
    async function layout(name, width, theme) {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), `${name}/${width}/${theme}: page overflow`);
      const invalid = await root.locator('button,input,select').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length).filter(n=>n.getBoundingClientRect().right>innerWidth+1 || n.getBoundingClientRect().left<0).map(n=>n.textContent));
      assert.deepEqual(invalid, [], `${name}/${width}/${theme}: control clipping`);
      if ((width===375 || width===1024) && ['home','assignments','student'].includes(name)) await root.screenshot({path:path.join(output,`${name}-${width}-${theme}.png`)});
      layouts++;
    }
    for (const theme of ['light','dark']) for (const width of [320,375,768,1024,1440]) {
      await load(width,theme);
      await layout('home',width,theme);
      await root.locator('.ag-nav [data-view="assignments"]').click();
      await layout('assignments',width,theme);
      assert.match(await root.innerText(), /CLI 등록/);
      await root.locator('#ag-search').fill('없는 이름');
      assert.match(await root.innerText(), /조건에 맞는 과제가 없습니다/);
      await root.locator('#ag-search').fill('');
      await root.locator('#ag-filter').selectOption('초안');
      assert.equal(await root.locator('tbody tr').count(),1);
      await root.locator('#ag-filter').selectOption('all');
      await root.locator('[data-task="observer"]').click();
      await root.locator('summary').click();
      await layout('detail',width,theme);
      await root.locator('#ag-reason').fill('보충 실습');
      await root.locator('#ag-new-due').fill('2026-09-19T18:00');
      await root.locator('[data-action="extend"]').click();
      assert.match(await root.locator('#ag-extend-error').innerText(),/늦은 시간/);
      assert.equal(await root.locator('#ag-reason').inputValue(),'보충 실습');
      await root.locator('#ag-new-due').fill('2026-09-22T18:00');
      await root.locator('[data-action="extend"]').click();
      assert.match(await root.locator('#ag-impact').innerText(),/실제 변경하지 않습니다/);
      await root.locator('.ag-nav [data-view="results"]').click();
      await layout('results',width,theme);
      await root.locator('[data-view="student"]').click();
      await layout('student',width,theme);
      const stats=await root.locator('.ag-stat').allTextContents();
      assert.match(stats[0],/채점 대기/);
      assert.doesNotMatch(stats[0],/100/);
      assert.match(stats[1],/100 \/ 100/);
      assert.match(stats[2],/미확정/);
      await root.locator('#ag-comparison').selectOption('3-2');
      assert.match(await root.locator('#ag-diff').innerText(),/nullptr/);
      await root.locator('.ag-nav [data-view="assignments"]').click();
      await root.locator('[data-action="new"]').click();
      await root.locator('#ag-title').fill('');
      await next();
      assert.match(await root.locator('#ag-title-error').innerText(),/제목/);
      await root.locator('#ag-title').fill('학생 입력 보존 시험');
      await root.locator('#ag-due').fill('2026-09-17T18:00');
      await next();
      assert.equal(await root.locator('#ag-title').inputValue(),'학생 입력 보존 시험');
      assert.match(await root.locator('#ag-due-error').innerText(),/이후/);
      await root.locator('.ag-nav [data-view="home"]').click();
      assert.match(await root.locator('#ag-toast').innerText(),/저장되지 않은/);
      assert.equal(await root.locator('#ag-title').count(),1);
      await root.locator('#ag-course').selectOption('come2201-02');
      assert.equal(await root.locator('#ag-course').inputValue(),'come2201-01');
      await root.locator('#ag-due').fill('2026-09-25T18:00');
      for (let step=1;step<=6;step++) {
        await layout(`editor-${step}`,width,theme);
        if(step===2){await next();assert.match(await root.locator('#ag-toast').innerText(),/먼저 등록/);await root.locator('[data-action="upload"]').click();}
        await next();
      }
      assert.match(await root.innerText(),/실제 학생에게 배포되지 않았습니다/);
      await root.locator('#ag-course').selectOption('come2201-02');
      assert.match(await root.locator('#ag-content').innerText(),/이 분반에 등록된 과제가 없습니다/);
      assert.doesNotMatch(await root.locator('#ag-content').innerText(),/가상 학생 A/);
    }
    assert.deepEqual(errors, [], 'Browser runtime errors');
    assert.deepEqual(requests, [], 'Mockup must not use network');
    console.log(`${layouts} layout checks passed across 5 widths and 2 themes; navigation, filters, deadline validation, input preservation, course scope, six-step wizard, current/best score and diff checks passed; no network requests or JS errors.`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
