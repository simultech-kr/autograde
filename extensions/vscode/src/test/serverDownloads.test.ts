import assert from "node:assert/strict";
import test from "node:test";
import {parseServerDownloadsAction, renderServerDownloads, type ServerDownloadsState} from "../serverDownloads";
const rowKey = "row-" + "a".repeat(32);
const state: ServerDownloadsState = {revision: 7, authenticated: true, trusted: true, busy: false, status: "확인됨", sections: [
  {kind: "assignment", state: "ready", rows: [{key: rowKey, title: '<img src=x onerror="steal()">', metadata: "강의 & 과제", description: "</script><script>steal()</script>", available: true}]},
  {kind: "material", state: "ready", rows: []}, {kind: "content", state: "error", rows: [], error: "서버 설정을 확인하세요"},
]};
test("download messages allow only a bounded opaque key and current render revision", () => {
  assert.deepEqual(parseServerDownloadsAction({type: "serverDownloads", action: "download", revision: 7, rowKey}), {type: "serverDownloads", action: "download", revision: 7, rowKey});
  assert.ok(parseServerDownloadsAction({type: "serverDownloads", action: "refresh", revision: 7}));
  for (const message of [null, [], {type: "serverDownloads", action: "download", revision: 7}, {type: "serverDownloads", action: "download", revision: 7, rowKey: "../../secret"},
    {type: "serverDownloads", action: "download", revision: 7, rowKey, url: "https://evil.invalid"}, {type: "serverDownloads", action: "refresh", revision: 0},
    {type: "serverDownloads", action: "refresh", revision: NaN}, {type: "serverDownloads", action: "connect", revision: 7, arguments: []}]) assert.equal(parseServerDownloadsAction(message), undefined);
});
test("catalogue escapes all server content and uses nonce-only scripts and theme-aware responsive rows", () => {
  const html = renderServerDownloads(state, "safe-nonce");
  assert.doesNotMatch(html, /<img|<script>steal/); assert.match(html, /&lt;img/); assert.match(html, /강의 &amp; 과제/);
  assert.match(html, /default-src 'none'/); assert.match(html, /script-src 'nonce-safe-nonce'/); assert.match(html, /form-action 'none'/);
  assert.match(html, /minmax\(0, 1fr\)/); assert.match(html, /max-width: 520px/); assert.match(html, /var\(--vscode-foreground\)/);
  assert.match(html, /수락한 과제/); assert.match(html, /1개/); assert.match(html, /현재 학생에게 배포된 개인 자료가 없습니다/); assert.match(html, /서버 설정을 확인/);
});
test("signed-out and restricted views explain unavailable downloads without leaking prior catalogue", () => {
  const signedOut = renderServerDownloads({...state, authenticated: false}, "safe");
  assert.doesNotMatch(signedOut, /data-row-key/); assert.match(signedOut, /수령 코드로 연결/);
  const restricted = renderServerDownloads({...state, trusted: false}, "safe");
  assert.match(restricted, /제한 모드에서는 목록만/); assert.match(restricted, /data-row-key="[^"]+" disabled/);
  const pending = renderServerDownloads({...state, busy: true}, "safe");
  assert.match(pending, /진행 중인 작업/);
});
