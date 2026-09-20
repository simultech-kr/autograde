// Synthetic personal-account pages. No external browsing or persistent profile.
// node scripts/check_instructor_identity.cjs FIXTURES PLAYWRIGHT_MODULE [CHROME_BINARY]
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const [root,modulePath='playwright',executablePath]=process.argv.slice(2);
if (!root) throw new Error('Supply pytest temporary fixtures');
const {chromium}=require(modulePath);
function find(dir) {
  return fs.readdirSync(dir,{withFileTypes:true}).flatMap(entry=>{
    const file=path.join(dir,entry.name);
    return entry.isDirectory()?find(file):/^personal-(home|course|admin)\.html$/.test(entry.name)?[file]:[];
  });
}
(async()=>{
  const files=find(root); assert.equal(files.length,3);
  const browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
  let checks=0;
  try {
    const page=await browser.newPage();
    await page.route('**/*',route=>route.abort());
    for(const file of files) for(const colorScheme of ['light','dark']) for(const width of [320,375,768,1440]) {
      await page.setViewportSize({width,height:900}); await page.emulateMedia({colorScheme});
      await page.setContent(fs.readFileSync(file,'utf8'));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${file}/${width}/${colorScheme}`);
      if(!file.endsWith('personal-admin.html')) {
        assert.equal(await page.locator('option[value=come3105]').count(),0);
        assert.equal(await page.locator('form[action="/instructor/courses"]').count(),0);
      } else assert.equal(await page.locator('form[action="/instructor/courses"]').count(),1);
      assert.equal(await page.locator('script:not([data-instructor-enhancement])').count(),0);
      if(file.endsWith('personal-home.html') && width===375)
        await page.screenshot({path:path.join(root,`personal-home-${colorScheme}.png`),fullPage:true});
      checks++;
    }
    console.log(`${checks} personal-account responsive/theme and scoped-control checks passed`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
