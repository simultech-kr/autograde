// Render synthetic pytest fixtures in a real browser. No external requests.
// node scripts/check_instructor_rubric.cjs FIXTURES PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [root, modulePath = 'playwright', executablePath] = process.argv.slice(2);
if (!root) throw new Error('Supply the pytest temporary fixture directory');
const {chromium} = require(modulePath);
function find(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(entry => {
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? find(file) : /^rubric-[a-z]+\.html$/.test(entry.name) ? [file] : [];
  });
}
(async () => {
  const files = find(root);
  assert.equal(files.length, 5, 'Expected editor, preview, detail, list, recovery');
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  let checks = 0;
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    for (const file of files) for (const colorScheme of ['light','dark']) for (const width of [320,375,768,1440]) {
      await page.setViewportSize({width,height:900});
      await page.emulateMedia({colorScheme});
      await page.setContent(fs.readFileSync(file,'utf8'));
      await page.locator('details').evaluateAll(nodes => nodes.forEach(node => node.open=true));
      const metrics = await page.evaluate(() => ({
        width: innerWidth, document: document.documentElement.scrollWidth,
        controls: [...document.querySelectorAll('input:not([type=hidden]),textarea,select,button')]
          .filter(n=>n.getClientRects().length)
          .filter(n=>n.getBoundingClientRect().right>innerWidth+1 || n.getBoundingClientRect().left<0).length
      }));
      assert.ok(metrics.document<=width+1 && metrics.controls===0, `${file}/${width}/${colorScheme}: ${JSON.stringify(metrics)}`);
      assert.equal(await page.locator('script:not([data-instructor-enhancement])').count(),0);
      if (file.endsWith('rubric-preview.html') && width===375)
        await page.screenshot({path:path.join(root,`rubric-preview-${colorScheme}.png`),fullPage:true});
      checks++;
    }
    await page.setContent(fs.readFileSync(files.find(f=>f.endsWith('rubric-recovery.html')),'utf8'));
    assert.equal(await page.locator('form[data-recovered][data-dirty-guard]').count(),1);
    const preview = fs.readFileSync(files.find(f=>f.endsWith('rubric-preview.html')),'utf8');
    await page.setContent(preview);
    assert.equal(await page.locator('form[action$="/rubrics/register"]').evaluate(form=>form.checkValidity()),false);
    await page.locator('input[name=confirm]').check();
    assert.equal(await page.locator('form[action$="/rubrics/register"]').evaluate(form=>form.checkValidity()),true);
    console.log(`${checks} responsive light/dark checks, recovery and confirmation checks passed`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
