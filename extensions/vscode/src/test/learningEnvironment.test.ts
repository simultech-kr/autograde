import assert from "node:assert/strict";
import * as path from "node:path";
import test from "node:test";
import { learningEnvironment, learningPolicyLabel } from "../learningEnvironment";

const root = path.resolve("test-learner");
const hints = {AUTOGRADE_LEARNING_POLICY: "restricted", AUTOGRADE_LEARNER_WORKSPACE: path.join(root, "workspace"),
  AUTOGRADE_LEARNER_MATERIALS: path.join(root, "materials")};

test("launcher status is descriptive and ignored by ordinary VS Code", () => {
  assert.deepEqual(learningEnvironment("Visual Studio Code", hints), {policy: "unmanaged"});
  assert.deepEqual(learningEnvironment("Autograde IDE", hints), {policy: "restricted", materialsDirectory: hints.AUTOGRADE_LEARNER_MATERIALS});
  assert.equal(learningEnvironment("Autograde IDE Dev", {...hints, AUTOGRADE_LEARNING_POLICY: "standard"}).policy, "standard");
  for (const policy of [undefined, "", "enforced", "verified", "<script>"]) {
    assert.deepEqual(learningEnvironment("Autograde IDE", {...hints, AUTOGRADE_LEARNING_POLICY: policy}), {policy: "unmanaged"});
  }
  for (const workspace of [undefined, "", "relative", "https://example.invalid", "/bad\0path"]) {
    assert.deepEqual(learningEnvironment("Autograde IDE", {...hints, AUTOGRADE_LEARNER_WORKSPACE: workspace}), {policy: "unmanaged"});
  }
});

test("folder picker hint only accepts the expected materials sibling", () => {
  for (const target of [undefined, "relative", "https://example.invalid", path.join(root, "workspace"), path.join(root, "other", "materials")]) {
    assert.equal(learningEnvironment("Autograde IDE", {...hints, AUTOGRADE_LEARNER_MATERIALS: target}).materialsDirectory, undefined);
  }
  assert.match(learningPolicyLabel("restricted"), /시작 설정/);
  assert.doesNotMatch(learningPolicyLabel("restricted"), /보장|검증 완료|차단 완료/);
  assert.match(learningPolicyLabel(), /미확인/);
});
