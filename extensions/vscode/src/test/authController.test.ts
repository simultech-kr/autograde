import assert from "node:assert/strict";
import test from "node:test";

import type { AutogradeClient } from "../api";

interface ModuleLoader {
  _load(request: string, parent: unknown, isMain: boolean): unknown;
}

const moduleLoader = require("node:module") as ModuleLoader;
const originalLoad = moduleLoader._load;
let warningResponses: Array<string | undefined> = [];
let warningCalls: unknown[][] = [];
let informationResponses: Array<string | undefined> = [];
let informationCalls: unknown[][] = [];
let copiedCodes: string[] = [];
let openedUrls: string[] = [];

const vscodeStub = {
  CancellationError: class CancellationError extends Error {},
  env: {
    remoteName: "wsl",
    clipboard: { writeText: async (value: string) => { copiedCodes.push(value); } },
    openExternal: async (value: string) => {
      openedUrls.push(value);
      return true;
    },
  },
  Uri: { parse: (value: string) => value },
  window: {
    showWarningMessage: async (...args: unknown[]) => {
      warningCalls.push(args);
      return warningResponses.shift();
    },
    showInformationMessage: async (...args: unknown[]) => {
      informationCalls.push(args);
      return informationResponses.shift();
    },
  },
};

moduleLoader._load = function loadWithVscodeStub(
  request: string,
  parent: unknown,
  isMain: boolean,
): unknown {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const { AuthenticationController } = require("../auth") as typeof import("../auth");
moduleLoader._load = originalLoad;

test("offline sign-out can explicitly clear only local credentials", async () => {
  warningResponses = ["이 기기에서만 로그아웃"];
  informationResponses = [];
  informationCalls = [];
  let cleared = 0;
  const authenticationChanges: boolean[] = [];
  const client = {
    tokens: {
      hasSession: async () => true,
      clear: async () => { cleared += 1; },
    },
    revokeCurrentSession: async () => { throw new Error("offline"); },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(
    client,
    "0.1.0",
    (authenticated) => authenticationChanges.push(authenticated),
  );

  await controller.signOut();

  assert.equal(cleared, 1);
  assert.deepEqual(authenticationChanges, [false]);
});

test("explicit sign-out revokes the current server session", async () => {
  warningResponses = [];
  informationResponses = [];
  informationCalls = [];
  let revoked = 0;
  const authenticationChanges: boolean[] = [];
  const client = {
    tokens: { hasSession: async () => true },
    revokeCurrentSession: async () => { revoked += 1; },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(
    client,
    "0.1.0",
    (authenticated) => authenticationChanges.push(authenticated),
  );

  await controller.signOut();

  assert.equal(revoked, 1);
  assert.deepEqual(authenticationChanges, [false]);
});

test("sign-out clears student UI state even after the in-memory session was already lost", async () => {
  warningResponses = [];
  informationResponses = [];
  informationCalls = [];
  let revoked = 0;
  const authenticationChanges: boolean[] = [];
  const client = {
    tokens: { hasSession: async () => false },
    revokeCurrentSession: async () => { revoked += 1; },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(
    client,
    "0.1.0",
    (authenticated) => authenticationChanges.push(authenticated),
  );

  await controller.signOut();

  assert.equal(revoked, 0);
  assert.deepEqual(authenticationChanges, [false]);
  assert.match(String(informationCalls[0]?.[0] ?? ""), /채점 화면을 지웠습니다/);
});

test("repeated sign-in revokes the current session before creating a new authorization", async () => {
  warningResponses = ["기존 세션 종료 후 다시 로그인"];
  informationResponses = [undefined];
  informationCalls = [];
  const events: string[] = [];
  const authenticationChanges: boolean[] = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: { hasSession: async () => true },
    revokeCurrentSession: async () => { events.push("revoke"); },
    createDeviceAuthorization: async () => {
      events.push("create");
      return {
        device_code: "device-code",
        user_code: "USER-CODE",
        verification_uri: "https://grade.example.edu/activate",
        expires_in: 300,
      };
    },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(
    client,
    "0.1.0",
    (authenticated) => authenticationChanges.push(authenticated),
  );

  await controller.signIn();

  assert.deepEqual(events, ["revoke", "create"]);
  assert.deepEqual(authenticationChanges, [false]);
});

test("sign-in confirmation prominently identifies the configured service origin", async () => {
  warningResponses = [];
  informationResponses = [undefined];
  informationCalls = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => ({
      device_code: "device-code",
      user_code: "USER-CODE",
      verification_uri: "https://grade.example.edu/activate",
      expires_in: 300,
    }),
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.0", () => {});

  await controller.signIn();

  assert.equal(informationCalls.length, 1);
  const options = informationCalls[0]?.[1] as { detail?: string } | undefined;
  assert.match(options?.detail ?? "", /접속할 서버: https:\/\/grade\.example\.edu/);
  assert.equal(informationCalls[0]?.[2], "grade.example.edu에서 로그인");
});

test("private-LAN HTTP sign-in is cancelled before the first server request", async () => {
  warningResponses = [undefined];
  warningCalls = [];
  informationResponses = [];
  informationCalls = [];
  let authorizationRequests = 0;
  const client = {
    transport: { getBaseUrl: () => "http://192.168.50.34:18081" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => {
      authorizationRequests += 1;
      throw new Error("must not be called");
    },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.1", () => {});

  await controller.signIn();

  assert.equal(authorizationRequests, 0);
  assert.equal(warningCalls.length, 1);
  assert.match(String(warningCalls[0]?.[0] ?? ""), /암호화되지 않은/);
  assert.match(String((warningCalls[0]?.[1] as { detail?: string })?.detail ?? ""), /token/);
});

test("private-LAN HTTP sign-in proceeds only after the explicit risk confirmation", async () => {
  warningResponses = ["위험을 이해하고 계속"];
  warningCalls = [];
  informationResponses = [undefined];
  informationCalls = [];
  let authorizationRequests = 0;
  const client = {
    transport: { getBaseUrl: () => "http://192.168.50.34:18081" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => {
      authorizationRequests += 1;
      return {
        device_code: "device-code",
        user_code: "USER-CODE",
        verification_uri: "http://192.168.50.34:18081/activate",
        verification_uri_complete: "http://192.168.50.34:18081/activate?code=USER-CODE",
        expires_in: 300,
      };
    },
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.1", () => {});

  await controller.signIn();

  assert.equal(authorizationRequests, 1);
  assert.equal(warningCalls.length, 1);
  assert.equal(informationCalls.length, 1);
  assert.match(
    String((informationCalls[0]?.[1] as { detail?: string })?.detail ?? ""),
    /http:\/\/192\.168\.50\.34:18081/,
  );
});

test("sign-in rejects a verification URL from a different origin before prompting", async () => {
  warningResponses = [];
  informationResponses = [undefined];
  informationCalls = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => ({
      device_code: "device-code",
      user_code: "USER-CODE",
      verification_uri: "https://login.example.edu/activate",
      expires_in: 300,
    }),
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.0", () => {});

  await assert.rejects(
    controller.signIn(),
    /인증 페이지 주소가 설정된 Autograde 서버와 일치하지 않습니다/,
  );
  assert.equal(informationCalls.length, 0);
});

test("sign-in validates verification_uri_complete as the same origin", async () => {
  warningResponses = [];
  informationResponses = [undefined];
  informationCalls = [];
  const client = {
    transport: { getBaseUrl: () => "http://127.0.0.1:18080" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => ({
      device_code: "device-code",
      user_code: "USER-CODE",
      verification_uri: "http://127.0.0.1:18080/activate",
      verification_uri_complete: "http://127.0.0.1:18081/activate?code=USER-CODE",
      expires_in: 300,
    }),
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.0", () => {});

  await assert.rejects(
    controller.signIn(),
    /인증 페이지 주소가 설정된 Autograde 서버와 일치하지 않습니다/,
  );
  assert.equal(informationCalls.length, 0);
});

test("copying a connection code does not open a browser without an origin-labelled confirmation", async () => {
  warningResponses = [];
  informationResponses = ["코드 복사", undefined];
  informationCalls = [];
  copiedCodes = [];
  openedUrls = [];
  const client = {
    transport: { getBaseUrl: () => "https://grade.example.edu" },
    tokens: { hasSession: async () => false },
    createDeviceAuthorization: async () => ({
      device_code: "device-code",
      user_code: "USER-CODE",
      verification_uri: "https://grade.example.edu/activate",
      expires_in: 300,
    }),
  } as unknown as AutogradeClient;
  const controller = new AuthenticationController(client, "0.1.0", () => {});

  await controller.signIn();

  assert.deepEqual(copiedCodes, ["USER-CODE"]);
  assert.deepEqual(openedUrls, []);
  assert.equal(informationCalls.length, 2);
  assert.equal(informationCalls[1]?.[2], "grade.example.edu에서 로그인");
});
