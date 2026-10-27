// Synthetic HTML from test_instructor_download_http.py; no production accounts.
// node scripts/check_instructor_downloads.cjs FIXTURES PLAYWRIGHT_MODULE CHROME OUTPUT
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const [root, modulePath, executablePath, output] = process.argv.slice(2);
if (!root || !modulePath || !executablePath || !output) throw new Error('Provide fixtures, Playwright, Chrome, output');
const {chromium} = require(modulePath);
const find = directory => fs.readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
  const file = path.join(directory, entry.name);
  return entry.isDirectory() ? find(file) : entry.isFile() && /^teacher-.*-downloads\.html$/.test(entry.name) ? [file] : [];
});
(async () => {
  const files = find(root); assert.ok(files.length >= 3);
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({headless:true, executablePath});
  let layouts = 0, requests = 0; const errors = [];
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => { requests++; return route.abort(); });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    for (const file of files) for (const theme of ['light', 'dark', 'contrast']) for (const width of [320,375,768,1440]) {
      await page.setViewportSize({width,height:900});
      await page.emulateMedia({colorScheme:theme === 'dark' ? 'dark' : 'light', forcedColors:theme === 'contrast' ? 'active' : 'none'});
      await page.setContent(fs.readFileSync(file,'utf8'));
      const metrics = await page.evaluate(() => ({
        overflow:document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        downloads:[...document.querySelectorAll('a[download]')].map(link => ({href:link.getAttribute('href'), text:link.textContent.trim()})),
        clipped:[...document.querySelectorAll('.download-item,.download-item .button')].some(el => el.scrollWidth > el.clientWidth + 1),
      }));
      assert.equal(metrics.overflow,false,`${file} ${width} ${theme}: horizontal overflow`);
      assert.equal(metrics.clipped,false,`${file} ${width} ${theme}: clipped controls`);
      assert.ok(metrics.downloads.length > 0);
      assert.ok(metrics.downloads.every(link => link.text && /^\/courses\/come3105\/instructor\//.test(link.href)));
      if (path.basename(file) === 'teacher-assignment-downloads.html' && (width === 375 || width === 1440) && theme !== 'contrast') {
        await page.screenshot({path:path.join(output,`${theme}-${width}.png`),fullPage:true});
      }
      layouts++;
    }
    assert.deepEqual(errors,[]); assert.equal(requests,0);
    const result = {layouts, externalRequests:requests, pageErrors:errors.length};
    fs.writeFileSync(path.join(output,'summary.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
