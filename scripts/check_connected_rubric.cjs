// Synthetic server-rendered fixtures only. All network access is blocked.
// node scripts/check_connected_rubric.cjs FIXTURES PLAYWRIGHT_MODULE CHROME_BINARY
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [root, modulePath, executablePath] = process.argv.slice(2);
const {chromium} = require(modulePath);
function find(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(entry => {
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? find(file) : /^connected-rubric-[a-z]+\.html$/.test(entry.name) ? [file] : [];
  });
}
(async () => {
  const files = find(root);
  assert.equal(files.length, 4);
  const browser = await chromium.launch({headless:true, executablePath});
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
      if (file.endsWith('-form.html')) {
        assert.equal(await page.locator('form[data-dirty-guard]').count(),1);
        if (width===375) await page.screenshot({path:path.join(root,`connected-rubric-${colorScheme}.png`),fullPage:true});
      }
      checks++;
    }
    console.log(`${checks} responsive light/dark and safe markup checks passed`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
