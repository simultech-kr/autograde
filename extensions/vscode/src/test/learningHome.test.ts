import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";
import { LEARNING_HOME_COMMAND, learningHomeAction, learningHomeBlockedReason, learningHomeCommand, renderLearningHome, shouldOpenLearningHome, type LearningHomeAction } from "../learningHome";

const ready = { authenticated: true, trusted: true, hasWorkspace: true };
const expected = {
  configure: "autograde.configureServiceAddress", connection: "autograde.checkServerConnection",
  claim: "autograde.redeemAssignmentClaim", open: "autograde.openAssignment",
  accepted: "autograde.acceptedAssignmentHistory", submit: "autograde.submitCurrentCommit",
  result: "autograde.viewLatestResult", history: "autograde.submissionHistory",
};

test("learning home accepts only known actions with no arguments or arbitrary command IDs", () => {
  for (const [action, command] of Object.entries(expected)) {
    assert.equal(learningHomeAction({type: "learningAction", action}), action);
    assert.equal(learningHomeCommand(action as LearningHomeAction), command);
  }
  for (const message of [undefined, null, [], "submit", 1, {type: "learningAction"},
    {type: "learningAction", action: "__proto__"}, {type: "learningAction", action: "constructor"},
    {type: "learningAction", action: "workbench.action.terminal.new"},
    {type: "learningAction", action: "submit", arguments: ["unsafe"]},
    {type: "learningAction", action: "configure", command: "evil"},
    {type: "other", action: "submit"}]) assert.equal(learningHomeAction(message), undefined);
});

test("automatic home never takes over normal hosts, existing folders, workspace files or editors", () => {
  for (const appName of ["Autograde IDE", "Autograde IDE Dev"]) {
    assert.equal(shouldOpenLearningHome(appName, 0, false, 0), true);
    assert.equal(shouldOpenLearningHome(appName, 1, false, 0), false);
    assert.equal(shouldOpenLearningHome(appName, 0, true, 0), false);
    assert.equal(shouldOpenLearningHome(appName, 0, false, 1), false);
  }
  for (const name of ["Visual Studio Code", "Visual Studio Code - Insiders", "Code - OSS", "Autograde IDE Preview", "Autograde IDE other"]) {
    assert.equal(shouldOpenLearningHome(name, 0, false, 0), false);
  }
});

test("session, trust, workspace and busy restrictions are rechecked independently of button state", () => {
  for (const action of Object.keys(expected) as LearningHomeAction[]) {
    assert.equal(learningHomeBlockedReason(action, ready), undefined);
    assert.match(learningHomeBlockedReason(action, {...ready, busy: true})!, /이전 작업/);
  }
  for (const action of ["open", "accepted", "submit", "result", "history"] as const) {
    assert.match(learningHomeBlockedReason(action, {...ready, authenticated: false})!, /수령 코드/);
  }
  for (const action of ["claim", "open", "submit"] as const) {
    assert.match(learningHomeBlockedReason(action, {...ready, trusted: false})!, /신뢰/);
  }
  assert.match(learningHomeBlockedReason("submit", {...ready, hasWorkspace: false})!, /작업 영역/);
  for (const action of ["configure", "connection"] as const) {
    assert.equal(learningHomeBlockedReason(action, {authenticated: false, trusted: false, hasWorkspace: false}), undefined);
  }
});

test("rendered home is theme-aware, responsive, nonce-protected and never requests or persists credentials", () => {
  const html = renderLearningHome(ready, "safe-nonce");
  for (const action of Object.keys(expected)) assert.ok(html.includes(`data-action="${action}"`));
  assert.match(html, /var\(--vscode-editor-background\)/);
  assert.match(html, /var\(--vscode-focusBorder\)/);
  assert.match(html, /@media \(max-width: 420px\)/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /default-src 'none'; style-src 'nonce-safe-nonce'; script-src 'nonce-safe-nonce'/);
  assert.match(html, /차단하지 않습니다/);
  assert.match(html, /로그인 정보는 저장하지 않습니다/);
  assert.doesNotMatch(html, /<input|<form|https?:\/\/|command:|localStorage|setState|fetch\(/);
  assert.doesNotMatch(html, / disabled/);
  const unauthenticated = renderLearningHome({authenticated: false, trusted: false, hasWorkspace: false}, "safe");
  assert.match(unauthenticated, /수령 코드 연결 필요/);
  assert.match(unauthenticated, /제한 모드/);
  assert.match(unauthenticated, /data-action="submit" class="primary" disabled/);
  assert.match(renderLearningHome(ready, '"><img src=x onerror=evil>'), /nonce="&quot;&gt;&lt;img/);
});

test("manual home and all delegated actions remain contributed commands", async () => {
  const manifest = JSON.parse(await readFile(path.join(__dirname, "../../package.json"), "utf8")) as {
    activationEvents: string[]; contributes: {commands: {command: string}[]; menus: {"view/title": {command: string}[]}};
  };
  assert.ok(manifest.activationEvents.includes(`onCommand:${LEARNING_HOME_COMMAND}`));
  for (const command of [LEARNING_HOME_COMMAND, ...Object.values(expected)]) {
    assert.ok(manifest.contributes.commands.some(item => item.command === command), command);
  }
  assert.ok(manifest.contributes.menus["view/title"].some(item => item.command === LEARNING_HOME_COMMAND));
});
