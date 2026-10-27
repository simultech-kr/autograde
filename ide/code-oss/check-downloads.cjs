#!/usr/bin/env node
// Synthetic webview QA: no server, student data, credentials or external requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
if (process.argv.length !== 6) throw new Error('Usage: check-downloads.cjs COMPILED_VIEW PLAYWRIGHT_MODULE CHROME_BINARY OUTPUT_DIR');
const [compiled, playwright, executablePath, output] = process.argv.slice(2).map(value => path.resolve(value));
const {renderServerDownloads, parseServerDownloadsAction} = require(compiled);
const {chromium} = require(playwright);
const nonce = 'download-layout-fixture';
const row = (letter, title, available = true) => ({key: `row-${letter.repeat(32)}`, title,
  metadata: 'COME2201 · 파일 v2 · 2026. 10. 28. 오후 1:50 KST',
  description: '교수자가 등록한 자료입니다. 파일을 받은 후 설명에 따라 실습합니다.',
  available, unavailableReason: '배포 시작 전입니다.'});
const sections = [
  {kind: 'assignment', state: 'ready', rows: [row('a', 'C++ Iterator 패턴 실습 — 긴 이름과 마감시간 표시 확인')]},
  {kind: 'material', state: 'ready', rows: [row('b', '개인 실습 안내.pdf'), row('c', '다음 주 참고 코드.zip', false)]},
  {kind: 'content', state: 'ready', rows: [row('d', '공개 강의자료와 예제 코드')]},
];
const themes = {
  light: ['light', '#222', '#fff', '#555', '#005a9e', '#fff', '#bbb'],
  dark: ['dark', '#eee', '#1e1e1e', '#bbb', '#005a9e', '#fff', '#777'],
  highcontrast: ['dark', '#fff', '#000', '#fff', '#ff0', '#000', '#fff'],
};
(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, executablePath});
  let layouts = 0, clicks = 0;
  const requests = [], errors = [];
  try {
    const context = await browser.newContext({offline: true, serviceWorkers: 'block'});
    await context.route('**/*', route => {requests.push(route.request().url()); return route.abort();});
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    for (const [theme, [scheme, foreground, background, description, button, buttonText, border]] of Object.entries(themes)) {
      await page.emulateMedia({colorScheme: scheme, forcedColors: theme === 'highcontrast' ? 'active' : 'none'});
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({width, height: 1000});
        for (const mode of ['ready', 'restricted', 'busy', 'partial', 'empty', 'guest']) {
          const state = {revision: 7, authenticated: mode !== 'guest', trusted: mode !== 'restricted', busy: mode === 'busy',
            status: '서버 과제와 자료 목록을 확인하세요.', sections: mode === 'empty' ? sections.map(item => ({...item, rows: []}))
              : mode === 'partial' ? sections.map(item => item.kind === 'material' ? {...item, state: 'error', rows: [], error: '자료 서비스를 사용할 수 없습니다. 교수자에게 설정을 확인하세요.'} : item) : sections};
          const style = `<style nonce="${nonce}">:root { --vscode-font-family:Arial,sans-serif;--vscode-font-size:14px;--vscode-foreground:${foreground};--vscode-editor-background:${background};--vscode-descriptionForeground:${description};--vscode-widget-border:${border};--vscode-button-border:${border};--vscode-button-background:${button};--vscode-button-hoverBackground:${button};--vscode-button-foreground:${buttonText};--vscode-focusBorder:${border}; }</style>`;
          const stub = `<script nonce="${nonce}">window.__messages=[];window.acquireVsCodeApi=()=>({postMessage:message=>window.__messages.push(message)});</script>`;
          await page.goto('about:blank');
          await page.setContent(renderServerDownloads(state, nonce).replace('</head>', `${style}${stub}</head>`));
          const label = `${theme}/${width}/${mode}`;
          assert.equal(await page.getByRole('heading', {level: 1}).count(), 1, label);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${label}: page overflow`);
          assert.deepEqual(await page.locator('main, section, li, button').evaluateAll(nodes => nodes.filter(node => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().right > innerWidth + 1).map(node => node.tagName)), [], `${label}: clipped content`);
          const buttons = page.locator('button');
          for (let i = 0; i < await buttons.count(); i++) {
            const target = buttons.nth(i), disabled = await target.isDisabled();
            const count = await page.evaluate(() => window.__messages.length);
            await target.evaluate(node => node.click());
            const messages = await page.evaluate(() => window.__messages);
            assert.equal(messages.length, count + (disabled ? 0 : 1), `${label}: button gating`);
            if (!disabled) { assert.ok(parseServerDownloadsAction(messages.at(-1)), `${label}: bounded message`); clicks++; }
          }
          if (mode === 'ready' && [375, 1440].includes(width)) await page.screenshot({path: path.join(output, `${theme}-${width}.png`), fullPage: true});
          layouts++;
        }
      }
    }
    assert.deepEqual(errors, []); assert.deepEqual(requests, []);
    const summary = {layouts, dispatchedActions: clicks, externalRequests: requests.length, pageErrors: errors.length};
    fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify(summary, null, 2) + '\n');
    console.log(JSON.stringify(summary));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
