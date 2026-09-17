// Optional visual regression check; no production data or persistent browser profile.
// node scripts/check_instructor_responsive.cjs FIXTURES PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const [root, modulePath = 'playwright', executablePath] = process.argv.slice(2);
if (!root) throw new Error('Provide the pytest --basetemp directory containing synthetic HTML fixtures');
const { chromium } = require(modulePath);
function find(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(entry => {
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? find(file) : /^(results|basic-results|legacy-results|source|sections|empty)\.html$/.test(entry.name) ? [file] : [];
  });
}
(async () => {
  const files = find(root);
  assert.equal(files.length, 6, 'Run test_instructor_responsive.py in a fresh temporary directory first');
  const browser = await chromium.launch({headless:true, ...(executablePath ? {executablePath} : {})});
  let checks = 0;
  try {
    const page = await browser.newPage();
    for (const file of files) {
      for (const colorScheme of ['light', 'dark']) {
        for (const width of [320, 375, 768, 1024, 1100, 1101, 1440]) {
          await page.setViewportSize({width, height:900});
          await page.emulateMedia({colorScheme});
          await page.setContent(fs.readFileSync(file, 'utf8'));
          await page.locator('details').evaluateAll(nodes => nodes.forEach(node => node.open = true));
          const metrics = await page.evaluate(() => ({
            viewport:document.documentElement.clientWidth, document:document.documentElement.scrollWidth,
            label:[...document.querySelectorAll('.cell-label')].map(n => getComputedStyle(n).display),
            rows:[...document.querySelectorAll('.result-table tbody tr')].map(n => getComputedStyle(n).display),
            overflow:[...document.querySelectorAll('.cell-value')].some(n => n.scrollWidth > n.clientWidth + 1),
          }));
          assert.ok(metrics.document <= metrics.viewport + 1, `${file} ${width} ${colorScheme}: page overflow ${JSON.stringify(metrics)}`);
          assert.equal(metrics.overflow, false, `${file} ${width}: cell overflow`);
          assert.ok(metrics.rows.every(display => display === (width <= 1100 ? 'block' : 'table-row')));
          assert.ok(metrics.label.every(display => display === (width <= 1100 ? 'block' : 'none')));
          const code = page.locator('.source-code');
          if (await code.count()) {
            await code.focus();
            await page.keyboard.press('ArrowRight');
            await page.waitForTimeout(100);
            assert.ok(await code.evaluate(n => n.scrollLeft > 0), 'Code must support keyboard horizontal scrolling');
          }
          if (path.basename(file) === 'results.html' && ((width === 375 && colorScheme === 'light') || (width === 1440 && colorScheme === 'dark'))) {
            await page.screenshot({path:path.join(root, `results-${width}-${colorScheme}.png`), fullPage:true});
          }
          checks++;
        }
      }
    }
    await page.emulateMedia({forcedColors:'active'});
    await page.setViewportSize({width:320,height:900});
    await page.setContent(fs.readFileSync(files.find(f => path.basename(f) === 'results.html'), 'utf8'));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
    console.log(`${checks} viewport/theme checks and forced-colors check passed`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
