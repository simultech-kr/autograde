import assert from "node:assert/strict";
import test from "node:test";

import type { AutogradeClient } from "../api";

interface ModuleLoader {
  _load(request: string, parent: unknown, isMain: boolean): unknown;
}

let storedAddress = "http://127.0.0.1:18080";
let inputResponse: string | undefined;
let warningResponse: string | undefined;
let inputCalls: Array<Record<string, unknown>> = [];
let warningCalls: unknown[][] = [];
let informationCalls: unknown[][] = [];
let updates: Array<{ key: string; value: unknown; target: unknown }> = [];

const GLOBAL_TARGET = 1;
const vscodeStub = {
  ConfigurationTarget: { Global: GLOBAL_TARGET },
  workspace: {
    getConfiguration: () => ({
      get: <T>(key: string, fallback: T): T => {
        if (key === "serviceBaseUrl") {
          return storedAddress as T;
        }
        if (key === "allowInsecureHttpPilot") {
          return false as T;
        }
        return fallback;
      },
      update: async (key: string, value: unknown, target: unknown) => {
        updates.push({ key, value, target });
        storedAddress = String(value);
      },
    }),
  },
  window: {
    showInputBox: async (options: Record<string, unknown>) => {
      inputCalls.push(options);
      return inputResponse;
    },
    showWarningMessage: async (...args: unknown[]) => {
      warningCalls.push(args);
      return warningResponse;
    },
    showInformationMessage: async (...args: unknown[]) => {
      informationCalls.push(args);
      return undefined;
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
  ServiceAddressController,
  serviceAddressValidationMessage,
} = require("../serviceAddress") as typeof import("../serviceAddress");
moduleLoader._load = originalLoad;

test("IPv4 shorthand is saved only to the machine-scoped service URL setting", async () => {
  resetUi();
  inputResponse = "203.0.113.10:20000";
  const events: string[] = [];
  const client = clientWithSession(false, events);
  const controller = new ServiceAddressController(
    client,
    async () => { events.push("end"); return true; },
    async () => { events.push("changed"); },
  );

  await controller.configure();

  assert.deepEqual(updates, [{
    key: "serviceBaseUrl",
    value: "https://203.0.113.10:20000",
    target: GLOBAL_TARGET,
  }]);
  assert.deepEqual(events, ["hasSession", "clear", "changed"]);
  assert.equal(inputCalls[0]?.value, "http://127.0.0.1:18080");
  assert.match(String(informationCalls.at(-1)?.[0] ?? ""), /https:\/\/203\.0\.113\.10:20000/);
});

test("an authenticated address change is confirmed and ends the old session before saving", async () => {
  resetUi();
  inputResponse = "[2001:db8::10]:20000";
  warningResponse = "로그아웃하고 주소 변경";
  const events: string[] = [];
  const client = clientWithSession(true, events);
  const controller = new ServiceAddressController(
    client,
    async () => { events.push("end"); return true; },
    async () => { events.push("changed"); },
  );

  await controller.configure();

  assert.deepEqual(events, ["hasSession", "end", "clear", "changed"]);
  assert.equal(updates[0]?.value, "https://[2001:db8::10]:20000");
  const details = (warningCalls[0]?.[1] as { detail?: string } | undefined)?.detail ?? "";
  assert.match(details, /현재 서버: http:\/\/127\.0\.0\.1:18080/);
  assert.match(details, /새 서버: https:\/\/\[2001:db8::10]:20000/);
});

test("cancelling session termination leaves the server address unchanged", async () => {
  resetUi();
  inputResponse = "203.0.113.10:20000";
  warningResponse = "로그아웃하고 주소 변경";
  const events: string[] = [];
  const controller = new ServiceAddressController(
    clientWithSession(true, events),
    async () => { events.push("end"); return false; },
    async () => { events.push("changed"); },
  );

  await controller.configure();

  assert.deepEqual(events, ["hasSession", "end"]);
  assert.deepEqual(updates, []);
  assert.equal(storedAddress, "http://127.0.0.1:18080");
});

test("public HTTP and credential-bearing input never reach configuration storage", async () => {
  resetUi();
  assert.match(serviceAddressValidationMessage("http://203.0.113.10:20000") ?? "", /차단/);
  assert.match(serviceAddressValidationMessage("https://user:secret@203.0.113.10") ?? "", /인증 정보/);

  inputResponse = "http://203.0.113.10:20000";
  const controller = new ServiceAddressController(
    clientWithSession(false, []),
    async () => true,
    async () => undefined,
  );
  await assert.rejects(controller.configure(), /기본적으로 차단/);
  assert.deepEqual(updates, []);
});

function clientWithSession(hasSession: boolean, events: string[]): AutogradeClient {
  return {
    tokens: {
      hasSession: async () => { events.push("hasSession"); return hasSession; },
      clear: async () => { events.push("clear"); },
    },
  } as unknown as AutogradeClient;
}

function resetUi(): void {
  storedAddress = "http://127.0.0.1:18080";
  inputResponse = undefined;
  warningResponse = undefined;
  inputCalls = [];
  warningCalls = [];
  informationCalls = [];
  updates = [];
}
