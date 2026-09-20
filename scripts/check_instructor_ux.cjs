// Actual server-generated synthetic fixtures, not the design prototype.
// node scripts/check_instructor_ux.cjs FIXTURES PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [root, modulePath = 'playwright', executablePath] = process.argv.slice(2);
if (!root) throw new Error('Provide pytest --basetemp with test_instructor_ux.py fixtures');
const {chromium} = require(modulePath);
function find(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(e => {
    const file = path.join(dir, e.name);
    return e.isDirectory() ? find(file) : /^ux-[a-z]+\.html$/.test(e.name) ? [file] : [];
  });
}
(async () => {
  const files = find(root);
  assert.equal(files.length, 7, 'Expected catalog, home, settings, CLI release, recovery, published, validation fixtures');
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  let checks=0;
  try {
    const page=await browser.newPage();
    for (const file of files) for (const colorScheme of ['light','dark']) for (const width of [320,375,768,1024,1440]) {
      await page.setViewportSize({width,height:900}); await page.emulateMedia({colorScheme});
      await page.setContent(fs.readFileSync(file,'utf8'));
      await page.locator('details').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));
      assert.equal(await page.locator('script:not([data-instructor-enhancement])').count(),0);
      const metrics=await page.evaluate(()=>({
        width:innerWidth,document:document.documentElement.scrollWidth,
        cells:[...document.querySelectorAll('.cell-value')].filter(n=>n.scrollWidth>n.clientWidth+1).length,
        controls:[...document.querySelectorAll('input:not([type=hidden]),select,textarea,button')].filter(n=>n.getClientRects().length).filter(n=>n.getBoundingClientRect().right>innerWidth+1 || n.getBoundingClientRect().left<0).length
      }));
      assert.ok(metrics.document<=metrics.width+1 && !metrics.cells && !metrics.controls,`${file}/${width}/${colorScheme}: ${JSON.stringify(metrics)}`);
      if (['ux-catalog.html','ux-recovery.html','ux-published.html'].includes(path.basename(file)) && (width===375 || width===1440)) {
        await page.screenshot({path:path.join(root,`${path.basename(file,'.html')}-${width}-${colorScheme}.png`),fullPage:true});
      }
      checks++;
    }
    await page.emulateMedia({forcedColors:'active'});
    await page.setViewportSize({width:320,height:900});
    await page.setContent(fs.readFileSync(files.find(f=>f.endsWith('ux-catalog.html')),'utf8'));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.keyboard.press('Tab');
    assert.equal(await page.locator(':focus').getAttribute('id'),'course_key');
    console.log(`${checks} server-rendered viewport/theme checks and forced-colors/keyboard check passed`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
