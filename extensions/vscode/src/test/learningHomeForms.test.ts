import assert from "node:assert/strict";
import test from "node:test";
import { learningHomeAction, learningHomeDiscoverySelection, learningHomeEditMessage, learningHomeFormMessage, renderLearningHome } from "../learningHome";

test("inline form channel accepts only exact bounded fields for the current document", () => {
  const code = {type: "learningForm", form: "claim", nonce: "current", value: "123456", expectedServer: "https://grade.example.test"};
  assert.deepEqual(learningHomeFormMessage(code, "current"), code);
  for (const form of ["server", "portal"]) {
    const address = {type: "learningForm", form, nonce: "current", value: "127.0.0.1:20000"};
    assert.deepEqual(learningHomeFormMessage(address, "current"), address);
    assert.equal(learningHomeFormMessage({...address, expectedServer: "unexpected"}, "current"), undefined);
    assert.equal(learningHomeFormMessage({...address, value: "a".repeat(2049)}, "current"), undefined);
  }
  for (const message of [null, [], "123456", {...code, nonce: "old"}, {...code, args: ["secret"]}, {...code, value: "a".repeat(129)},
    {...code, form: "constructor"}, {...code, form: {toString: () => "claim"}}, {...code, expectedServer: undefined},
    {...code, expectedServer: "a".repeat(2049)}, {...code, value: "  "}, {...code, value: "abc\n"}, {...code, value: "abc\u0000"},
    {...code, expectedServer: "server\r"}, {...code, value: 123456}]) {
    assert.equal(learningHomeFormMessage(message, "current"), undefined);
  }
  assert.equal(learningHomeFormMessage(code, ""), undefined);
});

test("discovery selection accepts only a bounded ID in the current document, never an endpoint", () => {
  const valid = {type: "learningDiscoverySelect", nonce: "current", id: "school-1"};
  assert.equal(learningHomeDiscoverySelection(valid, "current"), "school-1");
  for (const message of [null, [], "school-1", {...valid, nonce: "old"}, {...valid, id: ""}, {...valid, id: "a".repeat(129)},
    {...valid, id: "school\n"}, {...valid, id: "https://evil.example"}, {...valid, id: "constructor()"}, {...valid, id: 1},
    {...valid, apiUrl: "https://evil.example"}, {...valid, command: "vscode.openFolder"}]) {
    assert.equal(learningHomeDiscoverySelection(message, "current"), undefined);
  }
  assert.equal(learningHomeDiscoverySelection(valid, ""), undefined);
});

test("editing and command channels require their own exact schema and nonce", () => {
  for (const dirty of [false, true]) assert.equal(learningHomeEditMessage({type: "learningEdit", nonce: "current", dirty}, "current"), dirty);
  for (const message of [null, [], {type: "learningEdit", nonce: "old", dirty: true},
    {type: "learningEdit", nonce: "current", dirty: "true"}, {type: "learningEdit", nonce: "current", dirty: true, value: "secret"}]) {
    assert.equal(learningHomeEditMessage(message, "current"), undefined);
  }
  assert.equal(learningHomeAction({type: "learningAction", action: "result", nonce: "current"}, "current"), "result");
  assert.equal(learningHomeAction({type: "learningAction", action: "result"}, "current"), undefined);
  assert.equal(learningHomeAction({type: "learningAction", action: "result", nonce: "old"}, "current"), undefined);
});

test("inline forms escape server and feedback data and clear a code before dispatch without webview persistence", () => {
  const html = renderLearningHome({authenticated: false, trusted: true, hasWorkspace: false,
    serviceBaseUrl: 'https://grade.example.test/\"><img src=x onerror=evil>', studentPortalBaseUrl: 'https://portal.example.test/\"'}, "current",
    {initialForm: "portal", feedback: "<img src=x onerror=evil>"});
  assert.doesNotMatch(html, /<img|localStorage|sessionStorage|setState|fetch\(/);
  assert.match(html, /id="claim-code" type="password" maxlength="128" autocomplete="off"/);
  assert.doesNotMatch(html, /id="claim-code"[^>]*\svalue=/);
  assert.match(html, /id="server-settings" class="settings" open/);
  assert.match(html, /form-action 'none'/);
  const script = html.slice(html.lastIndexOf('<script nonce='));
  assert.ok(script.indexOf("input.value = ''") < script.indexOf("vscode.postMessage(message)"));
  assert.match(script, /pagehide/);
  assert.match(script, /type:'learningForm'/);
  assert.match(script, /message.expectedServer = main.dataset.expectedServer/);
  assert.doesNotMatch(script, /value:\s*input\.defaultValue|learningAction[^\n]*value/);
});
