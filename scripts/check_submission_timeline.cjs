// Synthetic pages from test_submission_timeline.py only; no signed-in profile.
// node scripts/check_submission_timeline.cjs FIXTURES PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [root, modulePath = 'playwright', executablePath] = process.argv.slice(2);
if (!root) throw new Error('Provide the test-only pytest --basetemp directory');
const {chromium} = require(modulePath);
function find(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(e => {
    const f = path.join(dir, e.name);
    return e.isDirectory() ? find(f) : /^(timeline|comparison)\.html$/.test(e.name) ? [f] : [];
  });
}
(async () => {
  const files = find(root); assert.equal(files.length, 4);
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  let count = 0;
  try {
    const page = await browser.newPage();
    for (const file of files) for (const colorScheme of ['light', 'dark']) for (const width of [320,375,768,1440]) {
      await page.setViewportSize({width,height:900}); await page.emulateMedia({colorScheme});
      await page.setContent(fs.readFileSync(file,'utf8'));
      assert.equal(await page.locator('script').count(), 0);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), `${file} ${width}: page overflow`);
      assert.ok(await page.locator('.cell-value').evaluateAll(nodes => nodes.every(n => n.scrollWidth <= n.clientWidth + 1)));
      if (width === 375 && colorScheme === 'light') {
        if (file.endsWith('timeline.html')) await page.locator('details').first().scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(path.dirname(file),path.basename(file,'.html')+'-mobile.png')});
      }
      count++;
    }
    console.log(`${count} timeline/comparison viewport and theme checks passed`);
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
