import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";

import { ApiError, type ApiTransport, type AutogradeClient, RequestCancelledError, TokenManager } from "../api";
import { OperationCancelledError } from "../deviceAuth";
import { AssignmentWorkspaceError, assignmentWorkspaceErrorMessage, type AssignmentWorkspaceErrorCode } from "../assignmentWorkspace";
import type { DeviceTokenPollOptions } from "../deviceAuth";
import type { AssignmentClaim, DeviceAuthorization, TokenResponse } from "../types";

interface ModuleLoader {
  _load(request: string, parent: unknown, isMain: boolean): unknown;
}

class CancellationError extends Error {}

let inputResponses: Array<string | undefined> = [];
let inputCalls: Array<Record<string, unknown>> = [];
let progressCalls: Array<Record<string, unknown>> = [];
let informationCalls: unknown[][] = [];
let inputInterceptor: (() => Promise<string | undefined>) | undefined;
const cancellationListeners = new Set<() => void>();

const cancellationToken = {
  isCancellationRequested: false,
  onCancellationRequested: (listener: () => void) => {
    cancellationListeners.add(listener);
    return { dispose: () => { cancellationListeners.delete(listener); } };
  },
};

const vscodeStub = {
  CancellationError,
  env: { remoteName: "wsl" },
  ProgressLocation: { Notification: 15 },
  window: {
    showInputBox: async (options: Record<string, unknown>) => {
      inputCalls.push(options);
      if (inputInterceptor) return inputInterceptor();
      return inputResponses.shift();
    },
    showInformationMessage: async (...args: unknown[]) => {
      informationCalls.push(args);
      return undefined;
    },
    withProgress: async <T>(
      options: Record<string, unknown>,
      task: (progress: { report(value: unknown): void }, token: typeof cancellationToken) => Promise<T>,
    ): Promise<T> => {
      progressCalls.push(options);
      return task({ report: () => undefined }, cancellationToken);
    },
  },
};

const moduleLoader = require("node:module") as ModuleLoader;
const originalLoad = moduleLoader._load;
moduleLoader._load = function loadWithVscodeStub(
  request: string,
  parent: unknown,
  isMain: boolean,
): unknown {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const {
  AssignmentClaimController,
  claimCodeValidationMessage,
  normalizeClaimCode,
} = require("../assignmentClaim") as typeof import("../assignmentClaim");
moduleLoader._load = originalLoad;

const DEVICE: DeviceAuthorization = {
  device_code: "device-code",
  user_code: "USER-CODE",
  verification_uri: "https://grade.example.edu/activate",
  expires_in: 300,
  poll_interval: 2,
};
const CLAIM: AssignmentClaim = {
  assignmentId: "asn_observer_cpp",
  courseKey: "cse101-2026f",
  deliveryMode: "bundle",
  acceptanceId: "aac_01",
};
const TOKENS: TokenResponse = {
  access_token: "access-from-claim",
  refresh_token: "refresh-from-claim",
  expires_in: 300,
  token_type: "Bearer",
};

test("claim code approves a pending device then bootstraps the existing in-memory session", async () => {
  resetUi();
  inputResponses = ["  ak1 2345-6789-abcd  "];
  const events: string[] = [];
  const createCalls: Array<{ deviceName: string; extensionVersion: string; signal?: AbortSignal; expectedBaseUrl?: string; claimCode?: string }> = [];
  const redeemCalls: Array<{ claimCode: string; deviceCode: string; signal?: AbortSignal; expectedBaseUrl?: string }> = [];
  const exchangeCalls: Array<{ deviceCode: string; expectedBaseUrl?: string }> = [];
  const stored: Array<{ tokens: TokenResponse; expectedBaseUrl: string }> = [];
  const acceptedAssignments: Array<string | undefined> = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: {
      storeSession: async (tokens: TokenResponse, expectedBaseUrl: string) => {
        events.push("store");
        stored.push({ tokens, expectedBaseUrl });
      },
    },
    createDeviceAuthorization: async (
      deviceName: string,
      extensionVersion: string,
      signal?: AbortSignal,
      expectedBaseUrl?: string,
      claimCode?: string,
    ) => {
      events.push("create");
      createCalls.push({ deviceName, extensionVersion, signal, expectedBaseUrl, claimCode });
      return DEVICE;
    },
    redeemAssignmentClaim: async (
      claimCode: string,
      deviceCode: string,
      signal?: AbortSignal,
      expectedBaseUrl?: string,
    ) => {
      events.push("redeem");
      redeemCalls.push({ claimCode, deviceCode, signal, expectedBaseUrl });
      return CLAIM;
    },
    exchangeDeviceCode: async (deviceCode: string, _signal?: AbortSignal, expectedBaseUrl?: string) => {
      events.push("exchange");
      exchangeCalls.push({ deviceCode, expectedBaseUrl });
      return TOKENS;
    },
  } as unknown as AutogradeClient;
  const pollCalls: DeviceTokenPollOptions[] = [];
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async (assignmentId) => { events.push("callback"); acceptedAssignments.push(assignmentId); },
    async (options) => {
      pollCalls.push(options);
      return options.exchange(options.deviceCode, new AbortController().signal);
    },
  );

  await controller.redeem();

  assert.equal(inputCalls.length, 1);
  assert.equal(inputCalls[0]?.password, true);
  assert.equal(inputCalls[0]?.ignoreFocusOut, true);
  assert.equal("value" in (inputCalls[0] ?? {}), false);
  assert.match(String(inputCalls[0]?.prompt ?? ""), /과제 수령 코드/);
  assert.match(String(inputCalls[0]?.prompt ?? ""), /학생 포털/);
  assert.doesNotMatch(String(inputCalls[0]?.prompt ?? ""), /20010|20000/);
  assert.match(String(inputCalls[0]?.prompt ?? ""), /접속할 서버: https:\/\/grade\.example\.edu/);
  assert.equal(createCalls.length, 1);
  assert.equal(createCalls[0]?.extensionVersion, "0.2.0");
  assert.equal(createCalls[0]?.claimCode, "AK1-2345-6789-ABCD");
  assert.equal(createCalls[0]?.expectedBaseUrl, "https://grade.example.edu");
  assert.match(createCalls[0]?.deviceName ?? "", /wsl/);
  assert.ok(createCalls[0]?.signal instanceof AbortSignal);
  assert.deepEqual(redeemCalls.map(({ claimCode, deviceCode }) => ({ claimCode, deviceCode })), [{
    claimCode: "AK1-2345-6789-ABCD",
    deviceCode: "device-code",
  }]);
  assert.ok(redeemCalls[0]?.signal instanceof AbortSignal);
  assert.equal(redeemCalls[0]?.expectedBaseUrl, "https://grade.example.edu");
  assert.equal(pollCalls.length, 1);
  assert.equal(pollCalls[0]?.deviceCode, "device-code");
  assert.equal(pollCalls[0]?.expiresInSeconds, 300);
  assert.equal(pollCalls[0]?.initialIntervalSeconds, 2);
  assert.deepEqual(exchangeCalls, [{
    deviceCode: "device-code",
    expectedBaseUrl: "https://grade.example.edu",
  }]);
  assert.deepEqual(stored, [{
    tokens: TOKENS,
    expectedBaseUrl: "https://grade.example.edu",
  }]);
  assert.deepEqual(acceptedAssignments, ["asn_observer_cpp"]);
  assert.deepEqual(events, ["create", "redeem", "exchange", "store", "callback"]);
  assert.equal(progressCalls.length, 2);
});

test("claim flow cannot install returned tokens if the origin flips at the store boundary", async () => {
  resetUi();
  inputResponses = ["AK1-2345-6789-ABCD"];
  const originalOrigin = "https://grade.example.edu";
  const changedOrigin = "https://other-grade.example.edu";
  let activeOrigin = originalOrigin;
  let stores = 0;
  const transport = {
    getBaseUrl: () => activeOrigin,
    request: async () => { throw new Error("unexpected request"); },
    requestBytes: async () => { throw new Error("unexpected request"); },
  } as unknown as ApiTransport;
  const tokens = new TokenManager(transport);
  let callbacks = 0;
  const client = {
    transport,
    tokens: { storeSession: async (response: TokenResponse, expectedBaseUrl: string) => {
      stores += 1;
      activeOrigin = changedOrigin;
      await tokens.storeSession(response, expectedBaseUrl);
    } },
    createDeviceAuthorization: async () => DEVICE,
    redeemAssignmentClaim: async () => CLAIM,
  } as unknown as AutogradeClient;
  const controller = new AssignmentClaimController(
    client,
    "0.2.1",
    async () => { callbacks += 1; },
    async () => TOKENS,
  );

  await assert.rejects(
    controller.redeem(),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );

  assert.equal(stores, 1);
  assert.equal(await tokens.hasSession(), false);
  assert.equal(callbacks, 0);
});

test("a code entered in the learning home connects without opening an input prompt", async () => {
  resetUi();
  const calls: Array<{ stage: string; code: string; origin?: string }> = [];
  const accepted: Array<string | undefined> = [];
  const controller = new AssignmentClaimController(baseClient({
    createDeviceAuthorization: async (_device, _version, _signal, origin, code) => {
      calls.push({ stage: "device", code: code ?? "", origin });
      return DEVICE;
    },
    redeemAssignmentClaim: async (code, _device, _signal, origin) => {
      calls.push({ stage: "redeem", code, origin });
      return CLAIM;
    },
  }), "0.5.19", async id => { accepted.push(id); }, async () => TOKENS);

  assert.equal(await controller.redeem("  ak1 2345-6789-abcd  "), true);

  assert.equal(inputCalls.length, 0);
  assert.deepEqual(calls, [
    { stage: "device", code: "AK1-2345-6789-ABCD", origin: "https://grade.example.edu" },
    { stage: "redeem", code: "AK1-2345-6789-ABCD", origin: "https://grade.example.edu" },
  ]);
  assert.deepEqual(accepted, [CLAIM.assignmentId]);
  assert.equal(progressCalls.length, 2);
  assert.doesNotMatch(JSON.stringify({ progressCalls, informationCalls }), /AK1-2345-6789-ABCD|ak1 2345/);
});

test("invalid direct claim input never falls back to a prompt or sends a request", async () => {
  for (const code of ["", " \t\n ", "short", "AK1-2345-6789-ABCI", `${" ".repeat(257)}AK1-2345-6789-ABCD`]) {
    resetUi();
    const controller = new AssignmentClaimController(baseClient({
      createDeviceAuthorization: async () => { assert.fail("invalid direct code must not be sent"); },
    }), "0.5.19", async () => { assert.fail("invalid code cannot open an assignment"); });

    await assert.rejects(controller.redeem(code), /수령 코드/);
    assert.equal(inputCalls.length, 0);
    assert.equal(progressCalls.length, 0);
  }
});

test("direct claim input cannot bypass the HTTPS requirement", async () => {
  resetUi();
  const controller = new AssignmentClaimController(baseClient({
    baseUrl: "http://192.168.50.34:20000",
    createDeviceAuthorization: async () => { assert.fail("claim code must not reach a LAN HTTP server"); },
  }), "0.5.19", async () => undefined);

  await assert.rejects(controller.redeem("AK1-2345-6789-ABCD"), /HTTPS 서버 주소/);
  assert.equal(inputCalls.length, 0);
  assert.equal(progressCalls.length, 0);
});

test("direct claim submission shares the duplicate guard with command-based submission", async () => {
  resetUi();
  const response = deferred<AssignmentClaim>();
  const started = deferred<void>();
  const receivedCodes: string[] = [];
  let callbacks = 0;
  const controller = new AssignmentClaimController(baseClient({
    redeemAssignmentClaim: async code => {
      receivedCodes.push(code);
      started.resolve();
      return response.promise;
    },
  }), "0.5.19", async () => { callbacks += 1; }, async () => TOKENS);

  const first = controller.redeem("AK1-2345-6789-ABCD");
  await started.promise;
  assert.equal(await controller.redeem("AK1-ZZZZ-XXXX-WWWW"), false);
  assert.equal(await controller.redeem(), false);
  assert.equal(inputCalls.length, 0);
  assert.deepEqual(receivedCodes, ["AK1-2345-6789-ABCD"]);
  response.resolve(CLAIM);
  assert.equal(await first, true);
  assert.equal(callbacks, 1);
});

test("direct claim submission stops if its server or student session changes during preparation", async () => {
  for (const change of ["origin", "logout", "replace"] as const) {
    resetUi();
    let origin = "https://grade.example.edu";
    const transport = { getBaseUrl: () => origin } as ApiTransport;
    const tokens = new TokenManager(transport);
    await tokens.storeSession(TOKENS, origin);
    const client = {
      transport,
      tokens,
      createDeviceAuthorization: async () => {
        if (change === "origin") origin = "https://other-grade.example.edu";
        if (change === "logout") await tokens.clear();
        if (change === "replace") await tokens.storeSession({ ...TOKENS, access_token: "new-student-access" }, origin);
        return DEVICE;
      },
      redeemAssignmentClaim: async () => { assert.fail("changed authentication context must stop redemption"); },
    } as unknown as AutogradeClient;
    const controller = new AssignmentClaimController(client, "0.5.19",
      async () => { assert.fail("stale claim must not open an assignment"); },
      async () => { assert.fail("stale claim must not poll for tokens"); });

    await assert.rejects(controller.redeem("AK1-2345-6789-ABCD"),
      (error: unknown) => error instanceof ApiError && error.code === "login_required");
    assert.equal(inputCalls.length, 0);
    if (change === "logout") assert.equal(await tokens.hasSession(), false);
    if (change === "replace") assert.equal(await tokens.getAccessToken(), "new-student-access");
  }
});

test("closing or invalidating the prompt performs no device or redemption request", async () => {
  for (const entered of [undefined, "", " \t\n ", "short"] as const) {
    resetUi();
    inputResponses = [entered];
    let requests = 0;
    const client = baseClient({
      createDeviceAuthorization: async () => { requests += 1; return DEVICE; },
    });
    const controller = new AssignmentClaimController(
      client,
      "0.2.0",
      async () => undefined,
      async () => TOKENS,
    );

    if (entered === undefined) {
      await controller.redeem();
    } else {
      await assert.rejects(controller.redeem(), entered.trim() ? /AK1-XXXX-XXXX-XXXX/ : /수령 코드를 입력하세요/);
    }
    assert.equal(requests, 0);
  }
});

test("claim-code transcription differences canonicalize and confusing characters are rejected", () => {
  assert.equal(normalizeClaimCode(" ak1 2345-6789-abcd "), "AK1-2345-6789-ABCD");
  assert.equal(normalizeClaimCode("AK1--2345  6789 ABCD"), "AK1-2345-6789-ABCD");
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABCI"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC0"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC\u00a0D"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC\u017f"), undefined);
  assert.match(claimCodeValidationMessage("AK1-2345-6789-ABCI") ?? "", /0, 1, I, L, O, U/);
});

test("private-LAN HTTP is blocked before soliciting or sending the secret", async () => {
  resetUi();
  let requests = 0;
  const client = baseClient({
    baseUrl: "http://192.168.50.34:18081",
    createDeviceAuthorization: async () => { requests += 1; return DEVICE; },
  });
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async () => undefined,
    async () => TOKENS,
  );

  await assert.rejects(controller.redeem(), /HTTPS 서버 주소/);
  assert.equal(inputCalls.length, 0);
  assert.equal(requests, 0);
});

test("loopback HTTP remains available for local development", async () => {
  resetUi();
  inputResponses = ["AK1-2345-6789-ABCD"];
  let redemptions = 0;
  const client = baseClient({
    baseUrl: "http://127.0.0.1:18080",
    redeemAssignmentClaim: async () => { redemptions += 1; return CLAIM; },
  });
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async () => undefined,
    async () => TOKENS,
  );

  await controller.redeem();
  assert.equal(redemptions, 1);
});

test("a lost redemption response recovers through the already-approved device", async () => {
  resetUi();
  inputResponses = ["AK1-2345-6789-ABCD"];
  const stored: TokenResponse[] = [];
  const callbackIds: Array<string | undefined> = [];
  const client = baseClient({
    storeSession: async (tokens) => { stored.push(tokens); },
    redeemAssignmentClaim: async () => {
      throw new ApiError("response lost", 0, "network_error");
    },
  });
  let polls = 0;
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async (assignmentId) => { callbackIds.push(assignmentId); },
    async () => { polls += 1; return TOKENS; },
  );

  await controller.redeem();

  assert.equal(polls, 1);
  assert.deepEqual(stored, [TOKENS]);
  assert.deepEqual(callbackIds, [undefined]);
  assert.match(String(informationCalls[0]?.[0] ?? ""), /복구했습니다/);
});

test("an explicit claim denial does not poll or install a session", async () => {
  resetUi();
  inputResponses = ["AK1-2345-6789-ABCD"];
  let polls = 0;
  let stored = 0;
  const client = baseClient({
    storeSession: async () => { stored += 1; },
    redeemAssignmentClaim: async () => {
      throw new ApiError("claim denied", 403, "assignment_claim_denied");
    },
  });
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async () => undefined,
    async () => { polls += 1; return TOKENS; },
  );

  await assert.rejects(controller.redeem(), /수령 코드 확인.*\/v1\/assignment-claims\/redeem[\s\S]*HTTP 403.*assignment_claim_denied/);
  assert.equal(polls, 0);
  assert.equal(stored, 0);
});

test("cancelling token polling installs neither session nor assignment", async () => {
  resetUi();
  inputResponses = ["AK1-2345-6789-ABCD"];
  let stored = 0;
  let callbacks = 0;
  const client = baseClient({ storeSession: async () => { stored += 1; } });
  const controller = new AssignmentClaimController(
    client,
    "0.2.0",
    async () => { callbacks += 1; },
    async () => { throw new OperationCancelledError(); },
  );

  await assert.rejects(controller.redeem(), (error: unknown) => error instanceof CancellationError);
  assert.equal(stored, 0);
  assert.equal(callbacks, 0);
});

test("assignment-claim controller has no persistence or clipboard path for the raw code", async () => {
  const source = await readFile(path.resolve(__dirname, "../../src/assignmentClaim.ts"), "utf8");
  assert.doesNotMatch(source, /globalState|workspaceState|SecretStorage|\.secrets\b/);
  assert.doesNotMatch(source, /clipboard|writeText|getConfiguration/);
  assert.match(source, /entered\s*=\s*undefined/);
  assert.match(source, /ephemeralClaimCode\s*=\s*undefined/);
});

type FailureStage = "device" | "redeem" | "token" | "store" | "assignment";
function failingController(stage: FailureStage, failure: unknown): InstanceType<typeof AssignmentClaimController> {
  const fail = async (): Promise<never> => { throw failure; };
  return new AssignmentClaimController(baseClient({
    ...(stage === "device" ? { createDeviceAuthorization: fail } : {}),
    ...(stage === "redeem" ? { redeemAssignmentClaim: fail } : {}),
    ...(stage === "store" ? { storeSession: fail } : {}),
  }), "0.5.7", stage === "assignment" ? fail : async () => undefined,
  stage === "token" ? fail : async () => TOKENS);
}

const FAILURE_STAGES: readonly FailureStage[] = ["device", "redeem", "token", "store", "assignment"];
const FAILURE_LABELS: Record<FailureStage, RegExp> = {
  device: /기기 연결 준비 \(\/v1\/device-authorizations\)/,
  redeem: /수령 코드 확인 \(\/v1\/assignment-claims\/redeem\)/,
  token: /로그인 연결 \(\/v1\/device-authorizations\/token\)/,
  store: /로그인 연결 \(\/v1\/device-authorizations\/token\)/,
  assignment: /수락 목록 확인 · 과제 다운로드/,
};

test("each claim stage reports HTTP and an allowlisted code without server message or student secrets", async () => {
  const secrets = ["AK1-2345-6789-ABCD", DEVICE.device_code, TOKENS.access_token, TOKENS.refresh_token,
    "student-identity-private", "device-name-private", "raw-response-body-private"];
  for (const stage of FAILURE_STAGES) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    const original = new ApiError(secrets.join(" "), 500, "internal_error");
    await assert.rejects(failingController(stage, original).redeem(), (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.notEqual(error, original);
      assert.match(error.message, FAILURE_LABELS[stage]);
      assert.match(error.message, /HTTP 500 · 오류 코드: internal_error/);
      assert.equal(error.status, 500); assert.equal(error.code, "internal_error");
      assert.equal(error.cause, undefined);
      for (const secret of secrets) assert.ok(!error.message.includes(secret), `${stage} leaked a secret`);
      if (stage === "assignment") assert.match(error.message, /과제 새로고침/);
      else if (stage === "device") assert.match(error.message, /서버 로그/);
      else assert.match(error.message, /새.*코드/);
      return true;
    });
  }
});

test("unknown exception strings and unrecognized error codes are never reflected", async () => {
  for (const stage of FAILURE_STAGES) {
    for (const failure of [
      new Error("raw-body-student-20211001 AK1-2345-6789-ABCD"),
      new ApiError("raw-body-student-20211001", 500, "student_20211001"),
      new ApiError("raw-body-student-20211001", 500, "internal_error\nAK1-2345-6789-ABCD"),
      { message: "raw-body-student-20211001", code: "AK1-2345-6789-ABCD" },
    ]) {
      resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
      await assert.rejects(failingController(stage, failure).redeem(), (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, FAILURE_LABELS[stage]);
        assert.doesNotMatch(error.message, /20211001|AK1-2345-6789-ABCD|raw-body/);
        if (error instanceof ApiError) assert.equal(error.code, "unclassified_error");
        return true;
      });
    }
  }
});

test("rejected preparation claim asks for a new code rather than treating it as lost connectivity", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  await assert.rejects(failingController("device", new ApiError("private response", 401, "invalid_grant")).redeem(), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.match(error.message, /기기 연결 준비[\s\S]*HTTP 401[\s\S]*새 코드를 발급/);
    assert.doesNotMatch(error.message, /private response|서버 연결 확인 후/);
    return true;
  });
});

test("non-HTTP statuses are not presented as HTTP responses", async () => {
  for (const status of [0, NaN, Infinity, -1, 99, 600, 401.5]) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    await assert.rejects(failingController("device", new ApiError("secret", status, "network_error")).redeem(), (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 0);
      assert.match(error.message, /HTTP 응답 없음\/확인 불가/);
      assert.doesNotMatch(error.message, /secret|NaN|Infinity/);
      return true;
    });
  }
});

test("cancellation remains a cancellation at every claim stage", async () => {
  for (const stage of FAILURE_STAGES) {
    for (const failure of [new CancellationError(), new RequestCancelledError(), new OperationCancelledError()]) {
      resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
      await assert.rejects(failingController(stage, failure).redeem(), (error: unknown) => error instanceof CancellationError);
    }
  }
});

test("all uncertain redemption responses still recover without redeeming the code twice", async () => {
  for (const code of ["network_error", "request_timeout", "invalid_response"]) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    let redemptions = 0; let polls = 0; let callbacks = 0;
    const controller = new AssignmentClaimController(baseClient({
      redeemAssignmentClaim: async () => { redemptions += 1; throw new ApiError("secret response", 0, code); },
    }), "0.5.7", async () => { callbacks += 1; }, async () => { polls += 1; return TOKENS; });
    await controller.redeem();
    assert.equal(redemptions, 1); assert.equal(polls, 1); assert.equal(callbacks, 1);
    assert.match(String(informationCalls[0]?.[0]), /복구했습니다/);
  }
});

test("failed lost-response recovery identifies login stage without leaking original details", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const controller = new AssignmentClaimController(baseClient({
    redeemAssignmentClaim: async () => { throw new ApiError("secret redeem response", 0, "network_error"); },
  }), "0.5.7", async () => { assert.fail("callback must not run"); }, async () => {
    throw new ApiError("secret token response", 503, "service_unavailable");
  });
  await assert.rejects(controller.redeem(), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.match(error.message, /로그인 연결/); assert.match(error.message, /HTTP 503/);
    assert.match(error.message, /기기 승인 복구를 완료하지 못했습니다/);
    assert.doesNotMatch(error.message, /secret/);
    return true;
  });
});

test("malformed device authorization has a safe preparation-stage response error", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const controller = new AssignmentClaimController(baseClient({
    createDeviceAuthorization: async () => ({ ...DEVICE, expires_in: NaN }),
  }), "0.5.7", async () => { assert.fail("callback must not run"); });
  await assert.rejects(controller.redeem(), /기기 연결 준비[\s\S]*invalid_response/);
});

test("duplicate invocation cannot replace the active input or submit its code twice", async () => {
  resetUi(); const input = deferred<string | undefined>(); inputInterceptor = () => input.promise;
  let requests = 0; let redemptions = 0; let stores = 0;
  const controller = new AssignmentClaimController(baseClient({
    createDeviceAuthorization: async () => { requests += 1; return DEVICE; },
    redeemAssignmentClaim: async () => { redemptions += 1; return CLAIM; },
    storeSession: async () => { stores += 1; },
  }), "0.5.7", async () => undefined, async () => TOKENS);
  const first = controller.redeem();
  await controller.redeem();
  assert.equal(inputCalls.length, 1); assert.equal(requests, 0);
  assert.match(String(informationCalls[0]?.[0]), /이미 진행/);
  input.resolve("AK1-2345-6789-ABCD"); await first;
  assert.equal(requests, 1); assert.equal(redemptions, 1); assert.equal(stores, 1);
  inputInterceptor = undefined; inputResponses = ["AK1-2345-6789-ABCD"];
  await controller.redeem();
  assert.equal(inputCalls.length, 2); assert.equal(redemptions, 2);
});

test("duplicate invocation during a pending redemption cannot start another prompt or request", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const response = deferred<AssignmentClaim>(); const started = deferred<void>(); let redemptions = 0;
  const controller = new AssignmentClaimController(baseClient({
    redeemAssignmentClaim: async () => { redemptions += 1; started.resolve(); return response.promise; },
  }), "0.5.7", async () => undefined, async () => TOKENS);
  const first = controller.redeem(); await started.promise;
  await controller.redeem();
  assert.equal(inputCalls.length, 1); assert.equal(redemptions, 1);
  response.resolve(CLAIM); await first;
});

test("all prompt, error and cancellation exits release the claim guard for an explicit retry", async () => {
  for (const scenario of ["empty", "closed", "server", "cancelled"] as const) {
    resetUi(); inputResponses = [scenario === "empty" ? "" : scenario === "closed" ? undefined : "AK1-2345-6789-ABCD"];
    let shouldFail = scenario === "server" || scenario === "cancelled"; let callbacks = 0;
    const controller = new AssignmentClaimController(baseClient({ createDeviceAuthorization: async () => {
      if (shouldFail) {
        shouldFail = false;
        throw scenario === "cancelled" ? new RequestCancelledError() : new ApiError("private", 500, "internal_error");
      }
      return DEVICE;
    } }), "0.5.7", async () => { callbacks += 1; }, async () => TOKENS);
    if (scenario === "closed") await controller.redeem();
    else await assert.rejects(controller.redeem());
    inputResponses = ["AK1-2345-6789-ABCD"];
    await controller.redeem();
    assert.equal(inputCalls.length, 2); assert.equal(callbacks, 1);
  }
});

test("expired or already-used codes stop before polling and never reopen an empty prompt automatically", async () => {
  for (const stage of ["device", "redeem"] as const) {
    for (const [status, code] of [[401, "invalid_grant"], [400, "expired_token"], [403, "assignment_claim_denied"]] as const) {
      resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
      const fail = async (): Promise<never> => { throw new ApiError("private expired/used code", status, code); };
      const controller = new AssignmentClaimController(baseClient({
        ...(stage === "device" ? { createDeviceAuthorization: fail } : { redeemAssignmentClaim: fail }),
        storeSession: async () => { assert.fail("must not store session"); },
      }), "0.5.7", async () => { assert.fail("must not resume assignment"); },
      async () => { assert.fail("must not poll denied device"); });
      await assert.rejects(controller.redeem(), (error: unknown) => {
        assert.ok(error instanceof ApiError); assert.equal(error.code, code);
        assert.match(error.message, /새.*코드/); assert.doesNotMatch(error.message, /private/);
        return true;
      });
      assert.equal(inputCalls.length, 1); assert.equal(progressCalls.length, 1);
    }
  }
});

test("a cancellation before, during or after preparation cannot advance into redeem or token storage", async () => {
  for (const when of ["before", "device", "redeem", "lost-response", "token"] as const) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    if (when === "before") cancellationToken.isCancellationRequested = true;
    let creates = 0; let redeems = 0; let polls = 0;
    const controller = new AssignmentClaimController(baseClient({
      createDeviceAuthorization: async () => { creates += 1; if (when === "device") cancelProgress(); return DEVICE; },
      redeemAssignmentClaim: async () => {
        redeems += 1;
        if (when === "redeem" || when === "lost-response") cancelProgress();
        if (when === "lost-response") throw new ApiError("private lost response", 0, "network_error");
        return CLAIM;
      },
      storeSession: async () => { assert.fail("cancelled work must not install tokens"); },
    }), "0.5.7", async () => { assert.fail("cancelled work must not resume assignment"); }, async () => {
      polls += 1; if (when === "token") cancelProgress(); return TOKENS;
    });
    await assert.rejects(controller.redeem(), (error: unknown) => error instanceof CancellationError);
    assert.equal(creates, when === "before" ? 0 : 1);
    assert.equal(redeems, when === "before" || when === "device" ? 0 : 1);
    assert.equal(polls, when === "token" ? 1 : 0);
    assert.equal(cancellationListeners.size, 0);
  }
});

test("origin changes at input, preparation, redemption and polling stop all later stages", async () => {
  for (const when of ["input", "device", "redeem", "token"] as const) {
    resetUi(); let origin = "https://grade.example.edu";
    const changeOrigin = () => { origin = "https://other-grade.example.edu"; };
    inputInterceptor = async () => { if (when === "input") changeOrigin(); return "AK1-2345-6789-ABCD"; };
    let creates = 0; let redeems = 0; let polls = 0;
    const client = baseClient({
      getBaseUrl: () => origin,
      createDeviceAuthorization: async () => { creates += 1; if (when === "device") changeOrigin(); return DEVICE; },
      redeemAssignmentClaim: async () => { redeems += 1; if (when === "redeem") changeOrigin(); return CLAIM; },
      storeSession: async () => { assert.fail("must not install session for a changed origin"); },
    });
    const controller = new AssignmentClaimController(client, "0.5.7", async () => { assert.fail("must not open stale assignment"); },
      async () => { polls += 1; if (when === "token") changeOrigin(); return TOKENS; });
    await assert.rejects(controller.redeem(), (error: unknown) => error instanceof ApiError && error.code === "login_required");
    assert.equal(creates, when === "input" ? 0 : 1);
    assert.equal(redeems, when === "input" || when === "device" ? 0 : 1);
    assert.equal(polls, when === "token" ? 1 : 0);
  }
});

test("device preparation network failure does not try redeeming or recovering an uncreated authorization", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const controller = new AssignmentClaimController(baseClient({
    createDeviceAuthorization: async () => { throw new ApiError("private network payload", 0, "network_error"); },
    redeemAssignmentClaim: async () => { assert.fail("device preparation failed"); },
  }), "0.5.7", async () => { assert.fail("device preparation failed"); }, async () => { assert.fail("device preparation failed"); });
  await assert.rejects(controller.redeem(), /기기 연결 준비[\s\S]*network_error/);
  assert.equal(inputCalls.length, 1); assert.equal(progressCalls.length, 1);
});

test("typed local folder failures keep their fixed guidance without exposing mutable error details", async () => {
  for (const code of ["folder_unavailable", "marker_invalid", "different_server", "different_assignment", "active_assignment_mismatch"] as const) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    const failure = new AssignmentWorkspaceError(code);
    failure.message = "private student identity AK1-2345-6789-ABCD /private/student-folder";
    await assert.rejects(failingController("assignment", failure).redeem(), (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /기존 과제 폴더 확인/);
      assert.ok(error.message.includes(assignmentWorkspaceErrorMessage(code)));
      assert.doesNotMatch(error.message, /private|AK1-2345-6789-ABCD|과제 새로고침/);
      assert.equal(error.cause, undefined);
      return true;
    });
  }
});

test("unknown typed workspace codes cannot smuggle secrets through claim guidance", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const failure = new AssignmentWorkspaceError("AK1-2345-6789-ABCD" as AssignmentWorkspaceErrorCode);
  await assert.rejects(failingController("assignment", failure).redeem(), (error: unknown) => {
    assert.ok(error instanceof Error); assert.match(error.message, /기존 과제 폴더 확인/);
    assert.doesNotMatch(error.message, /AK1-2345-6789-ABCD/);
    return true;
  });
});

test("same-origin logout or identity replacement cannot resurrect an in-flight claim session", async () => {
  for (const change of ["logout", "replace"] as const) {
    for (const when of ["input", "device", "redeem", "lost-response", "token"] as const) {
      resetUi(); const baseUrl = "https://grade.example.edu";
      const transport = { getBaseUrl: () => baseUrl } as ApiTransport;
      const tokens = new TokenManager(transport);
      await tokens.storeSession(TOKENS, baseUrl);
      const otherTokens: TokenResponse = { ...TOKENS, access_token: "other-student-access", refresh_token: "other-student-refresh" };
      const changeIdentity = async () => {
        if (change === "logout") await tokens.clear();
        else await tokens.storeSession(otherTokens, baseUrl);
      };
      inputInterceptor = async () => { if (when === "input") await changeIdentity(); return "AK1-2345-6789-ABCD"; };
      let installs = 0; let callbacks = 0; let creates = 0; let redemptions = 0; let polls = 0;
      const client = {
        transport,
        tokens: {
          getSessionGeneration: () => tokens.getSessionGeneration(),
          storeSession: async (response: TokenResponse, expectedBaseUrl: string) => {
            installs += 1; await tokens.storeSession(response, expectedBaseUrl);
          },
        },
        createDeviceAuthorization: async () => { creates += 1; if (when === "device") await changeIdentity(); return DEVICE; },
        redeemAssignmentClaim: async () => {
          redemptions += 1;
          if (when === "redeem" || when === "lost-response") await changeIdentity();
          if (when === "lost-response") throw new ApiError("private lost response", 0, "network_error");
          return CLAIM;
        },
      } as unknown as AutogradeClient;
      const controller = new AssignmentClaimController(client, "0.5.8", async () => { callbacks += 1; }, async () => {
        polls += 1; if (when === "token") await changeIdentity(); return TOKENS;
      });
      await assert.rejects(controller.redeem(), (error: unknown) => error instanceof ApiError && error.code === "login_required");
      assert.equal(installs, 0); assert.equal(callbacks, 0);
      assert.equal(creates, when === "input" ? 0 : 1);
      assert.equal(redemptions, when === "input" || when === "device" ? 0 : 1);
      assert.equal(polls, when === "token" ? 1 : 0);
      if (change === "logout") assert.equal(await tokens.hasSession(), false);
      else assert.equal(await tokens.getAccessToken(), otherTokens.access_token);
    }
  }
});

test("a token poll checks the captured identity before sending another exchange request", async () => {
  resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
  const baseUrl = "https://grade.example.edu";
  const transport = { getBaseUrl: () => baseUrl } as ApiTransport;
  const tokens = new TokenManager(transport);
  let exchanges = 0; let callbacks = 0;
  const client = {
    transport, tokens,
    createDeviceAuthorization: async () => DEVICE, redeemAssignmentClaim: async () => CLAIM,
    exchangeDeviceCode: async () => { exchanges += 1; return TOKENS; },
  } as unknown as AutogradeClient;
  const controller = new AssignmentClaimController(client, "0.5.8", async () => { callbacks += 1; }, async options => {
    await tokens.clear();
    return options.exchange(options.deviceCode, new AbortController().signal);
  });
  await assert.rejects(controller.redeem(), (error: unknown) => error instanceof ApiError && error.code === "login_required");
  assert.equal(exchanges, 0); assert.equal(callbacks, 0); assert.equal(await tokens.hasSession(), false);
});

test("own synchronous session installation is allowed but a following logout or replacement blocks assignment UI", async () => {
  for (const afterStore of ["unchanged", "logout", "replace"] as const) {
    resetUi(); inputResponses = ["AK1-2345-6789-ABCD"];
    const baseUrl = "https://grade.example.edu";
    const transport = { getBaseUrl: () => baseUrl } as ApiTransport;
    const tokens = new TokenManager(transport);
    const otherTokens: TokenResponse = { ...TOKENS, access_token: "newer-student-access", refresh_token: "newer-student-refresh" };
    let callbacks = 0;
    const client = {
      transport,
      tokens: {
        getSessionGeneration: () => tokens.getSessionGeneration(),
        storeSession: (response: TokenResponse, expectedBaseUrl: string) => {
          const installation = tokens.storeSession(response, expectedBaseUrl);
          return installation.then(async () => {
            if (afterStore === "logout") await tokens.clear();
            if (afterStore === "replace") await tokens.storeSession(otherTokens, baseUrl);
          });
        },
      },
      createDeviceAuthorization: async () => DEVICE, redeemAssignmentClaim: async () => CLAIM,
    } as unknown as AutogradeClient;
    const controller = new AssignmentClaimController(client, "0.5.8", async () => { callbacks += 1; }, async () => TOKENS);
    if (afterStore === "unchanged") {
      await controller.redeem(); assert.equal(callbacks, 1);
      assert.equal(await tokens.getAccessToken(), TOKENS.access_token);
    } else {
      await assert.rejects(controller.redeem(), (error: unknown) => error instanceof ApiError && error.code === "login_required");
      assert.equal(callbacks, 0);
      if (afterStore === "logout") assert.equal(await tokens.hasSession(), false);
      else assert.equal(await tokens.getAccessToken(), otherTokens.access_token);
    }
  }
});

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}

interface ClientOverrides {
  readonly baseUrl?: string;
  readonly getBaseUrl?: () => string;
  readonly storeSession?: (tokens: TokenResponse, expectedBaseUrl: string) => Promise<void>;
  readonly createDeviceAuthorization?: AutogradeClient["createDeviceAuthorization"];
  readonly redeemAssignmentClaim?: AutogradeClient["redeemAssignmentClaim"];
}

function baseClient(overrides: ClientOverrides = {}): AutogradeClient {
  return {
    transport: { getBaseUrl: overrides.getBaseUrl ?? (() => overrides.baseUrl ?? "https://grade.example.edu") },
    tokens: { storeSession: overrides.storeSession ?? (async () => undefined) },
    createDeviceAuthorization:
      overrides.createDeviceAuthorization ?? (async () => DEVICE),
    redeemAssignmentClaim:
      overrides.redeemAssignmentClaim ?? (async () => CLAIM),
    exchangeDeviceCode: async () => TOKENS,
  } as unknown as AutogradeClient;
}

function resetUi(): void {
  inputResponses = [];
  inputCalls = [];
  progressCalls = [];
  informationCalls = [];
  inputInterceptor = undefined;
  cancellationToken.isCancellationRequested = false;
  cancellationListeners.clear();
}

function cancelProgress(): void {
  cancellationToken.isCancellationRequested = true;
  for (const listener of cancellationListeners) listener();
}
