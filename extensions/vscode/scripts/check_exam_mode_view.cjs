// Synthetic webview rendering only: no OS capture, student data, credentials or firewall changes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const originalLoad = Module._load;
Module._load = function(name, parent, main) {
  return name === "vscode" ? {} : originalLoad.call(this, name, parent, main);
};
const {renderExamMode} = require("../out/examModeView.js");
Module._load = originalLoad;
const playwright = require(process.argv[2] || "playwright");
const outputDirectory = path.resolve(process.argv[3] || path.join(__dirname, "../.data-build/exam-mode-ui"));
const scope = {generation: 1, assignmentId: "assignment-observer-decorator-midterm-2026-very-long-course-section", autogradeOrigin: "https://grade.example.edu:20000"};
const status = {
  exam: {id: "exam-demo", title: "시스템 프로그래밍 중간고사 · 시험용 예시", status: "active"}, seat_id: "A-01",
  assignment_id: scope.assignmentId, autograde_origin: scope.autogradeOrigin, server_time: 1792701000, expires_at: 1792729800,
  agent: {online: true, mode: "enforced", backend: "windows-wfp-hyperv", state: "installed", last_seen: 1792700998, lease_expires_at: 1792701180, verified: false},
  permissions: {can_release: false}, token: "synthetic-secret-must-never-render",
};
const state = {phase: "connected", fresh: true, origin: "https://exam.example.edu:20100", checkedAt: 1792701000000, leaseRemainingSeconds: 180, status};
const themeVariables = {
  light: "--vscode-foreground:#202020;--vscode-editor-background:#ffffff;--vscode-descriptionForeground:#545454;--vscode-button-secondaryForeground:#222222;--vscode-button-secondaryBackground:#e5e5e5;--vscode-button-foreground:#ffffff;--vscode-button-background:#0067c0;--vscode-focusBorder:#0067c0;--vscode-widget-border:#b6b6b6;",
  dark: "--vscode-foreground:#eeeeee;--vscode-editor-background:#1e1e1e;--vscode-descriptionForeground:#cccccc;--vscode-button-secondaryForeground:#ffffff;--vscode-button-secondaryBackground:#454545;--vscode-button-foreground:#ffffff;--vscode-button-background:#0e639c;--vscode-focusBorder:#75beff;--vscode-widget-border:#808080;",
};
function documentFor(theme) {
  return renderExamMode(state, "render-test", true, false, scope).replace("</head>", `<style nonce="render-test">:root{--vscode-font-size:14px;--vscode-font-family:system-ui;${themeVariables[theme]}}</style></head>`);
}
async function main() {
  fs.mkdirSync(outputDirectory, {recursive: true});
  const browser = await playwright.chromium.launch({channel: "chrome", headless: true});
  const errors = [];
  let checked = 0;
  try {
    for (const theme of ["light", "dark"]) for (const width of [320, 375, 1024]) {
      const page = await browser.newPage({viewport: {width, height: 1100}, colorScheme: theme});
      page.on("pageerror", error => errors.push(error.message));
      await page.route("**/*", route => route.abort());
      await page.evaluate(() => {window.__messages = []; window.acquireVsCodeApi = () => ({postMessage: value => window.__messages.push(value)});});
      await page.setContent(documentFor(theme));
      await page.getByRole("button", {name: "상태 다시 확인"}).click();
      assert.deepEqual(await page.evaluate(() => window.__messages), [{type: "examAction", action: "refresh"}]);
      assert.match(await page.locator("[role=status]").innerText(), /설치 보고.*미검증/);
      assert.doesNotMatch(await page.locator("body").innerText(), /synthetic-secret-must-never-render/);
      const before = await page.evaluate(() => ({body: document.body.scrollWidth, document: document.documentElement.scrollWidth, viewport: innerWidth}));
      assert.ok(before.body <= width && before.document <= width, `${theme} ${width}px overflow: ${JSON.stringify(before)}`);
      await page.locator("summary").click();
      const after = await page.evaluate(() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth));
      assert.ok(after <= width, `${theme} ${width}px expanded overflow`);
      await page.screenshot({path: path.join(outputDirectory, `exam-mode-${theme}-${width}.png`), fullPage: true});
      const offline = renderExamMode({...state, fresh: false, message: "시험 서버와의 연결이 끊어져 현재 제한 상태를 확인할 수 없습니다."}, "render-test", true, false, scope);
      const content = offline.slice(offline.indexOf("<main>") + 6, offline.indexOf("</main>"));
      await page.getByRole("button", {name: "상태 다시 확인"}).focus();
      await page.evaluate(html => window.postMessage({type: "examRender", html}, "*"), content);
      await page.getByText("상태 확인 불가 · 이전 응답", {exact: true}).waitFor();
      assert.equal(await page.locator("details").evaluate(element => element.open), true, "updates must preserve expanded settings");
      assert.equal(await page.evaluate(() => document.activeElement.dataset.action), "refresh", "updates must preserve keyboard focus");
      assert.deepEqual(errors, []);
      await page.close(); checked++;
    }
  } finally {await browser.close();}
  console.log(JSON.stringify({ok: true, scenarios: checked, themes: ["light", "dark"], widths: [320, 375, 1024], screenshots: outputDirectory, realIdeHostTested: false}));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
