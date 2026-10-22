#!/usr/bin/env node
// Synthetic Webview smoke test. No live server, student records, credentials or external requests.
// node ide/code-oss/check-home.cjs COMPILED_HOME PLAYWRIGHT_MODULE CHROME_BINARY OUTPUT_DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const args = process.argv.slice(2);
if (args.length !== 4) {
  process.stderr.write('Usage: node ide/code-oss/check-home.cjs COMPILED_HOME PLAYWRIGHT_MODULE CHROME_BINARY OUTPUT_DIR\n');
  process.exit(2);
}
const [compiledHome, playwrightModule, executablePath, output] = args.map(value => path.resolve(value));
const { renderLearningHome, learningHomeBlockedReason } = require(compiledHome);
const { chromium } = require(playwrightModule);
const widths = [320, 375, 768, 1440];
const actions = ['configure', 'connection', 'claim', 'open', 'accepted', 'submit', 'result', 'history'];
const nonce = 'autograde-synthetic-browser-test';
const themes = {
  light: {
    scheme: 'light', foreground: '#333333', background: '#ffffff', description: '#555555',
    secondaryForeground: '#333333', secondaryBackground: '#e8e8e8', secondaryHover: '#dddddd',
    buttonForeground: '#ffffff', buttonBackground: '#005a9e', buttonHover: '#004c87', border: '#cccccc', focus: '#005a9e',
  },
  dark: {
    scheme: 'dark', foreground: '#eeeeee', background: '#1e1e1e', description: '#bbbbbb',
    secondaryForeground: '#ffffff', secondaryBackground: '#3a3d41', secondaryHover: '#45494e',
    buttonForeground: '#ffffff', buttonBackground: '#005a9e', buttonHover: '#006bbd', border: '#666666', focus: '#75beff',
  },
  highcontrast: {
    scheme: 'dark', foreground: '#ffffff', background: '#000000', description: '#ffffff',
    secondaryForeground: '#ffffff', secondaryBackground: '#000000', secondaryHover: '#222222',
    buttonForeground: '#000000', buttonBackground: '#ffff00', buttonHover: '#ffffff', border: '#ffffff', focus: '#ffff00',
  },
};

function syntheticHtml(state, theme) {
  const style = `<style nonce="${nonce}">:root {
    --vscode-font-family: Arial, sans-serif; --vscode-font-size: 14px;
    --vscode-foreground: ${theme.foreground}; --vscode-editor-background: ${theme.background};
    --vscode-descriptionForeground: ${theme.description}; --vscode-widget-border: ${theme.border};
    --vscode-contrastBorder: ${theme.border}; --vscode-button-border: ${theme.border};
    --vscode-button-secondaryForeground: ${theme.secondaryForeground};
    --vscode-button-secondaryBackground: ${theme.secondaryBackground};
    --vscode-button-secondaryHoverBackground: ${theme.secondaryHover};
    --vscode-button-foreground: ${theme.buttonForeground}; --vscode-button-background: ${theme.buttonBackground};
    --vscode-button-hoverBackground: ${theme.buttonHover}; --vscode-focusBorder: ${theme.focus};
  }</style>`;
  const stub = `<script nonce="${nonce}">window.__messages = [];
    window.acquireVsCodeApi = () => ({postMessage: message => window.__messages.push(message)});</script>`;
  return renderLearningHome(state, nonce).replace('</head>', `${style}${stub}</head>`);
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath });
  const screenshots = [];
  const requests = [];
  let layouts = 0;
  let dispatchedActions = 0;
  let disabledActions = 0;
  try {
    const context = await browser.newContext({ offline: true, serviceWorkers: 'block' });
    await context.route('**/*', route => {
      requests.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const [themeName, theme] of Object.entries(themes)) {
      await page.emulateMedia({ colorScheme: theme.scheme, forcedColors: themeName === 'highcontrast' ? 'active' : 'none' });
      for (const width of widths) {
        await page.setViewportSize({ width, height: 1000 });
        for (const authenticated of [false, true]) {
          for (const trusted of [false, true]) {
            for (const hasWorkspace of [false, true]) {
              for (const busy of [false, true]) {
                const state = { authenticated, trusted, hasWorkspace, busy };
                const stateName = `${authenticated ? 'connected' : 'guest'}-${trusted ? 'trusted' : 'restricted'}-${hasWorkspace ? 'workspace' : 'empty'}-${busy ? 'busy' : 'idle'}`;
                const label = `${themeName}/${width}/${stateName}`;
                // A VS Code Webview HTML replacement creates a fresh document. Reset the JS realm too,
                // so the previous fixture's top-level `const vscode` does not survive setContent().
                await page.goto('about:blank');
                await page.setContent(syntheticHtml(state, theme), { waitUntil: 'load' });
                assert.equal(await page.getByRole('heading', { level: 1 }).count(), 1, `${label}: title`);
                assert.equal(await page.getByRole('button').count(), actions.length, `${label}: action count`);
                assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${label}: page horizontal overflow`);
                const clipped = await page.locator('button, section, main').evaluateAll(nodes => nodes
                  .filter(node => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().left < -1 || node.getBoundingClientRect().right > innerWidth + 1)
                  .map(node => node.tagName + ':' + node.textContent?.slice(0, 40)));
                assert.deepEqual(clipped, [], `${label}: clipped content`);
                const text = await page.innerText('main');
                assert.ok(text.includes(authenticated ? '수령 코드 연결됨' : '수령 코드 연결 필요'), `${label}: authentication state`);
                assert.ok(text.includes(hasWorkspace ? '작업 영역 열림' : '작업 영역 없음'), `${label}: workspace state`);
                assert.equal(text.includes('제한 모드'), !trusted, `${label}: trust state`);
                assert.equal(text.includes('작업 진행 중'), busy, `${label}: busy state`);
                assert.ok(text.includes('생성형 AI나 외부 프로그램·네트워크 사용을 차단하지 않습니다'), `${label}: scope boundary`);

                const enabled = actions.filter(action => !learningHomeBlockedReason(action, state));
                if (enabled.length) {
                  await page.keyboard.press('Tab');
                  assert.equal(await page.locator(`button[data-action="${enabled[0]}"]`).evaluate(node => node === document.activeElement), true, `${label}: first keyboard action`);
                  const focus = await page.locator(`button[data-action="${enabled[0]}"]`).evaluate(node => getComputedStyle(node).outlineStyle);
                  assert.notEqual(focus, 'none', `${label}: visible keyboard focus`);
                }
                for (const action of actions) {
                  const button = page.locator(`button[data-action="${action}"]`);
                  const reason = learningHomeBlockedReason(action, state);
                  assert.equal(await button.isDisabled(), !!reason, `${label}/${action}: availability`);
                  const before = await page.evaluate(() => window.__messages.length);
                  if (reason) {
                    assert.equal(await button.getAttribute('title'), reason, `${label}/${action}: blocked reason`);
                    await button.evaluate(node => node.click());
                    assert.equal(await page.evaluate(() => window.__messages.length), before, `${label}/${action}: disabled dispatch`);
                    disabledActions++;
                  } else {
                    await button.click();
                    const messages = await page.evaluate(() => window.__messages);
                    assert.equal(messages.length, before + 1, `${label}/${action}: single dispatch`);
                    assert.deepEqual(messages.at(-1), { type: 'learningAction', action }, `${label}/${action}: fixed message`);
                    dispatchedActions++;
                  }
                }
                // Representative full-page artifacts, not screenshots of a live student or IDE session.
                if ((themeName === 'light' && width === 1440 && authenticated && trusted && hasWorkspace && !busy)
                  || (themeName === 'dark' && width === 375 && !authenticated && trusted && !hasWorkspace && !busy)
                  || (themeName === 'highcontrast' && width === 320 && authenticated && trusted && hasWorkspace && !busy)
                  || (themeName === 'dark' && width === 768 && authenticated && !trusted && hasWorkspace && busy)) {
                  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
                  const screenshot = path.join(output, `${themeName}-${width}-${stateName}.png`);
                  await page.screenshot({ path: screenshot, fullPage: true });
                  screenshots.push(screenshot);
                }
                layouts++;
              }
            }
          }
        }
      }
    }
    assert.deepEqual(errors, [], 'Browser runtime errors');
    assert.deepEqual(requests, [], 'Synthetic page attempted external requests');
    const summary = { ok: true, layouts, dispatchedActions, disabledActions, externalRequests: requests.length, screenshots };
    fs.writeFileSync(path.join(output, 'summary.json'), `${JSON.stringify(summary, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(summary)}\n`);
  } finally {
    await browser.close();
  }
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
