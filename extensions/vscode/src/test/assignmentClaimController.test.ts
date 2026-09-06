import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";

import { ApiError } from "../api";
import type { AutogradeClient } from "../api";
import { OperationCancelledError } from "../deviceAuth";
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

const cancellationToken = {
  isCancellationRequested: false,
  onCancellationRequested: (_listener: () => void) => ({ dispose: () => undefined }),
};

const vscodeStub = {
  CancellationError,
  env: { remoteName: "wsl" },
  ProgressLocation: { Notification: 15 },
  window: {
    showInputBox: async (options: Record<string, unknown>) => {
      inputCalls.push(options);
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
  const createCalls: Array<{ deviceName: string; extensionVersion: string; signal?: AbortSignal }> = [];
  const redeemCalls: Array<{ claimCode: string; deviceCode: string; signal?: AbortSignal }> = [];
  const exchangeCalls: string[] = [];
  const stored: TokenResponse[] = [];
  const acceptedAssignments: Array<string | undefined> = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: {
      storeSession: async (tokens: TokenResponse) => { events.push("store"); stored.push(tokens); },
    },
    createDeviceAuthorization: async (
      deviceName: string,
      extensionVersion: string,
      signal?: AbortSignal,
    ) => {
      events.push("create");
      createCalls.push({ deviceName, extensionVersion, signal });
      return DEVICE;
    },
    redeemAssignmentClaim: async (
      claimCode: string,
      deviceCode: string,
      signal?: AbortSignal,
    ) => {
      events.push("redeem");
      redeemCalls.push({ claimCode, deviceCode, signal });
      return CLAIM;
    },
    exchangeDeviceCode: async (deviceCode: string) => {
      events.push("exchange");
      exchangeCalls.push(deviceCode);
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
  assert.match(String(inputCalls[0]?.prompt ?? ""), /과제 수령 코드\(과제 키\)/);
  assert.match(String(inputCalls[0]?.prompt ?? ""), /저장되지 않습니다/);
  assert.equal(createCalls.length, 1);
  assert.equal(createCalls[0]?.extensionVersion, "0.2.0");
  assert.match(createCalls[0]?.deviceName ?? "", /wsl/);
  assert.ok(createCalls[0]?.signal instanceof AbortSignal);
  assert.deepEqual(redeemCalls.map(({ claimCode, deviceCode }) => ({ claimCode, deviceCode })), [{
    claimCode: "AK1-2345-6789-ABCD",
    deviceCode: "device-code",
  }]);
  assert.ok(redeemCalls[0]?.signal instanceof AbortSignal);
  assert.equal(pollCalls.length, 1);
  assert.equal(pollCalls[0]?.deviceCode, "device-code");
  assert.equal(pollCalls[0]?.expiresInSeconds, 300);
  assert.equal(pollCalls[0]?.initialIntervalSeconds, 2);
  assert.deepEqual(exchangeCalls, ["device-code"]);
  assert.deepEqual(stored, [TOKENS]);
  assert.deepEqual(acceptedAssignments, ["asn_observer_cpp"]);
  assert.deepEqual(events, ["create", "redeem", "exchange", "store", "callback"]);
  assert.equal(progressCalls.length, 2);
});

test("closing or invalidating the prompt performs no device or redemption request", async () => {
  for (const entered of [undefined, "short"] as const) {
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
      await assert.rejects(controller.redeem(), /AK1-XXXX-XXXX-XXXX/);
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

  await assert.rejects(controller.redeem(), /claim denied/);
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

interface ClientOverrides {
  readonly baseUrl?: string;
  readonly storeSession?: (tokens: TokenResponse) => Promise<void>;
  readonly createDeviceAuthorization?: AutogradeClient["createDeviceAuthorization"];
  readonly redeemAssignmentClaim?: AutogradeClient["redeemAssignmentClaim"];
}

function baseClient(overrides: ClientOverrides = {}): AutogradeClient {
  return {
    transport: { getBaseUrl: () => overrides.baseUrl ?? "https://grade.example.edu" },
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
}
