import assert from "node:assert/strict";
import test from "node:test";
import {resolveUnsavedSharedExit} from "../sharedExitDecision";

test("failed shared save requires a second explicit discard; cancel and export cannot quit", async () => {
  for (const actions of [[undefined], ["저장 없이 종료"], ["미저장 변경 폐기 선택", undefined], ["먼저 내보내기"], ["미저장 변경 폐기 선택", "미저장 변경을 폐기하고 종료"]]) {
    let exports = 0; const choices = [...actions], details: string[] = [];
    const allowed = await resolveUnsavedSharedExit({evidence: () => "서버 보관 v2 · 이전 저장본", retry: async () => false,
      exportWork: async () => {exports++;}, choose: async (_message, detail) => {details.push(detail); return choices.shift();}});
    assert.equal(allowed, actions[1] === "미저장 변경을 폐기하고 종료");
    assert.equal(exports, actions[0] === "먼저 내보내기" ? 1 : 0);
    assert.match(details[0]!, /v2/); assert.match(details[0]!, /로컬 코드·로그인 정보가 삭제/);
  }
});
test("retry only permits exit after an acknowledged save and failed retry remains interactive", async () => {
  let retries = 0, prompts = 0;
  assert.equal(await resolveUnsavedSharedExit({evidence: () => "저장 안 됨", retry: async () => ++retries === 2,
    exportWork: async () => {throw new Error("unexpected export");}, choose: async () => {prompts++; return "서버 저장 재시도";}}), true);
  assert.equal(retries, 2); assert.equal(prompts, 2);
});
