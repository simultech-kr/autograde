import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { DISCOVERY_SERVICE, DiscoveryRecords, ServerDiscoveryController, discoverServers, discoveryOrigin,
  trustedDiscoveryServers, type DiscoveredServer, type DiscoverySocket, type TrustedDiscoveryServer } from "../serverDiscovery";

const trusted: TrustedDiscoveryServer = {institutionId: "school", displayName: "학교 · 실습실", apiUrl: "https://api.example.test:20000", portalUrl: "https://portal.example.test:20010"};
const found: DiscoveredServer = {...trusted, id: trusted.institutionId};
const instance = `Classroom.${DISCOVERY_SERVICE}`;
const record = (name: string, type: string, data: unknown, ttl = 120) => ({name, type, data, ttl});
const ptr = (ttl = 120) => record(`${DISCOVERY_SERVICE}.`, "PTR", `${instance}.`, ttl);
const srv = (target = "api.example.test.", port = 20000, ttl = 120) => record(instance, "SRV", {target, port}, ttl);
const txt = (fields: string[] = ["v=1", "institution=school", `portal=${trusted.portalUrl}`], ttl = 120) => record(instance, "TXT", fields.map(value => Buffer.from(value)), ttl);
const packet = () => ({answers: [ptr()], additionals: [srv(), txt()]});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  return {promise: new Promise<T>((yes, no) => {resolve = yes; reject = no;}), resolve: (value: T) => resolve(value), reject: (error: Error) => reject(error)};
}

class Socket extends EventEmitter implements DiscoverySocket {
  queries: Array<Array<{name: string; type: "PTR" | "SRV" | "TXT"}>> = [];
  destroyed = 0;
  queryError?: Error;
  destroyError?: Error;
  query(questions: Array<{name: string; type: "PTR" | "SRV" | "TXT"}>, callback?: (error: Error | null) => void): void {
    this.queries.push(questions); callback?.(this.queryError ?? null);
  }
  destroy(callback?: () => void): void {
    this.destroyed++;
    if (this.destroyError) throw this.destroyError;
    callback?.();
  }
}

test("trusted server profiles accept canonical HTTPS origins and reject credential-bearing or ambiguous settings", () => {
  assert.deepEqual(trustedDiscoveryServers([trusted]), [trusted]);
  assert.equal(discoveryOrigin("https://api.example.test/"), "https://api.example.test");
  assert.equal(discoveryOrigin("https://api.example.test:20000"), trusted.apiUrl);
  for (const url of ["http://api.example.test", "https://user:secret@api.example.test", "https://api.example.test/path",
    "https://api.example.test?token=secret", "https://api.example.test#fragment", "https://127.0.0.1", "https://[::1]",
    "https://localhost", "https://api.example.test\\evil", " https://api.example.test", "https://api.example.test\n"]) {
    assert.throws(() => discoveryOrigin(url), url);
  }
  for (const value of [undefined, {}, [trusted, trusted], Array.from({length: 33}, (_, n) => ({...trusted, institutionId: `school-${n}`})),
    [{...trusted, secret: "student-code"}], [{...trusted, displayName: "school\n"}], [{...trusted, institutionId: "../school"}],
    [{...trusted, portalUrl: trusted.apiUrl}]]) assert.throws(() => trustedDiscoveryServers(value));
});

test("DNS-SD combines PTR/SRV/TXT across responses and uses trusted labels and canonical URLs only", () => {
  const cache = new DiscoveryRecords([trusted]);
  assert.deepEqual(cache.ingest({answers: [ptr()]}), [instance.toLowerCase()]);
  assert.deepEqual(cache.servers(), []);
  cache.ingest({answers: [srv()]}); assert.deepEqual(cache.servers(), []);
  cache.ingest({answers: [txt()], additionals: [record("api.example.test", "A", "203.0.113.99")]});
  assert.deepEqual(cache.servers(), [found]);
  cache.ingest(packet()); assert.deepEqual(cache.servers(), [found]);
});

test("an SRV arriving before PTR does not suppress the missing TXT follow-up query", () => {
  const cache = new DiscoveryRecords([trusted]);
  cache.ingest({answers: [srv()]});
  assert.deepEqual(cache.ingest({answers: [ptr()]}), [instance.toLowerCase()]);
});

test("discovery rejects records that only partially match the institution allowlist", () => {
  for (const additionals of [
    [srv("evil.example.test"), txt()], [srv(undefined, 443), txt()],
    [srv(), txt(["v=1", "institution=another", `portal=${trusted.portalUrl}`])],
    [srv(), txt(["v=1", "institution=school", "portal=https://evil.example.test"])],
    [srv(), txt(["v=2", "institution=school", `portal=${trusted.portalUrl}`])],
    [srv(), txt(["v=1", "institution=school", "institution=school", `portal=${trusted.portalUrl}`])],
    [srv(), txt(["v=1", "institution=school", `portal=${trusted.portalUrl}`, "ignored=\nsecret"])],
    [srv("127.0.0.1"), txt()], [srv(undefined, 0), txt()], [srv(undefined, 65536), txt()],
  ]) {
    const cache = new DiscoveryRecords([trusted]); cache.ingest({answers: [ptr()], additionals});
    assert.deepEqual(cache.servers(), []);
  }
});

test("expired and goodbye records stop being advertised as selectable servers", () => {
  let now = 1000;
  const cache = new DiscoveryRecords([trusted], () => now);
  cache.ingest(packet()); assert.equal(cache.servers().length, 1);
  now += 120001; assert.deepEqual(cache.servers(), []);
  cache.ingest(packet()); assert.equal(cache.servers().length, 1);
  cache.ingest({answers: [ptr(0)]}); assert.deepEqual(cache.servers(), []);
  cache.ingest(packet()); cache.ingest({answers: [srv(undefined, 20000, 0)]}); assert.deepEqual(cache.servers(), []);
  cache.ingest(packet()); cache.ingest({answers: [txt(undefined, 0)]}); assert.deepEqual(cache.servers(), []);
});

test("malformed or oversized multicast metadata cannot create usable endpoints", () => {
  for (const value of [null, [], "packet", {}, {answers: null}, {answers: Array(129).fill(ptr())},
    {answers: [ptr()], additionals: Array(129).fill(srv())},
    {answers: [ptr(), srv(), record(instance, "TXT", ["v=1", "institution=school", `portal=${trusted.portalUrl}`])]},
    {answers: [ptr(), srv(), record(instance, "TXT", [Buffer.alloc(256)])]},
    {answers: [ptr(), srv(), record(instance, "TXT", Array(9).fill(Buffer.from("v=1")))]},
    {answers: [{...ptr(), ttl: Infinity}, srv(), txt()]}, {answers: [{...ptr(), ttl: -1}, srv(), txt()]},
  ]) {
    const cache = new DiscoveryRecords([trusted]); assert.doesNotThrow(() => cache.ingest(value));
    assert.deepEqual(cache.servers(), []);
  }
});

test("discovery socket sends only service queries, returns bounded results and always closes on timeout", async () => {
  const socket = new Socket();
  const result = discoverServers([trusted], {socketFactory: () => socket, durationMs: 20});
  socket.emit("ready"); socket.emit("response", packet());
  assert.deepEqual(await result, [found]);
  assert.deepEqual(socket.queries[0], [{name: DISCOVERY_SERVICE, type: "PTR"}]);
  assert.equal(socket.destroyed, 1);
  const count = socket.queries.length; socket.emit("response", packet()); assert.equal(socket.queries.length, count);
});

test("abort closes an active discovery socket and pre-aborted calls never create one", async () => {
  const abort = new AbortController(); const socket = new Socket();
  const result = discoverServers([trusted], {socketFactory: () => socket, signal: abort.signal});
  abort.abort(); assert.deepEqual(await result, []); assert.equal(socket.destroyed, 1);
  let created = false;
  assert.deepEqual(await discoverServers([trusted], {socketFactory: () => {created = true; return socket;}, signal: abort.signal}), []);
  assert.equal(created, false);
});

test("socket startup/query errors are sanitized and resources are closed", async () => {
  for (const failure of ["factory", "event", "query"] as const) {
    const socket = new Socket();
    const result = discoverServers([trusted], {socketFactory: () => {
      if (failure === "factory") throw new Error("private-network-detail"); return socket;
    }});
    if (failure === "event") socket.emit("error", new Error("private-network-detail"));
    if (failure === "query") {socket.queryError = new Error("private-network-detail"); socket.emit("ready");}
    await assert.rejects(result, error => error instanceof Error && /ローカル|로컬 네트워크/.test(error.message) && !error.message.includes("private-network-detail"));
    assert.equal(socket.destroyed, failure === "factory" ? 0 : 1);
  }
});

test("a close failure after a socket bind error still settles the discovery request", async () => {
  const socket = new Socket(); socket.destroyError = new Error("ERR_SOCKET_DGRAM_NOT_RUNNING");
  const result = discoverServers([trusted], {socketFactory: () => socket, durationMs: 100});
  assert.doesNotThrow(() => socket.emit("error", new Error("EADDRINUSE")));
  await assert.rejects(result, /로컬 네트워크/); assert.equal(socket.destroyed, 1);
});

function controller(overrides: Partial<ConstructorParameters<typeof ServerDiscoveryController>[0]> = {}) {
  const applied: DiscoveredServer[] = [];
  let changes = 0;
  const value = new ServerDiscoveryController({profiles: async () => [trusted], assertAvailable: () => {},
    changed: () => {changes++;}, apply: async server => {applied.push(server);}, scan: async () => [found], verify: async () => {}, ...overrides});
  return {value, applied, changes: () => changes};
}

test("controller exposes discovery results but applies only an explicitly selected server after verification", async () => {
  const verified: TrustedDiscoveryServer[] = [];
  const {value, applied, changes} = controller({verify: async server => {verified.push(server);}});
  await value.search(); assert.equal(value.state.status, "complete"); assert.deepEqual(applied, []);
  await assert.rejects(() => value.select("unknown"));
  await value.select("school"); assert.deepEqual(verified, [found]); assert.deepEqual(applied, [found]);
  assert.ok(changes() >= 4); value.dispose();
});

test("controller prevents duplicate searches and ignores results after invalidation or disposal", async () => {
  const pending = deferred<DiscoveredServer[]>(); let scans = 0;
  const {value, applied} = controller({scan: async () => {scans++; return pending.promise;}});
  const first = value.search(); await value.search(); await Promise.resolve(); assert.equal(scans, 1);
  value.invalidate(); pending.resolve([found]); await first;
  assert.equal(value.state.status, "idle"); assert.deepEqual(value.state.servers, []); assert.deepEqual(applied, []);
  value.dispose(); await value.search(); assert.equal(scans, 1);
});

test("controller cancels discovery when shared-session authentication changes during the scan", async () => {
  const pending = deferred<DiscoveredServer[]>(); let available = true;
  const {value} = controller({assertAvailable: () => {if (!available) throw new Error("student already authenticated");}, scan: async () => pending.promise});
  const operation = value.search(); await Promise.resolve(); available = false; pending.resolve([found]); await operation;
  assert.equal(value.state.status, "unavailable"); assert.deepEqual(value.state.servers, []); value.dispose();
});

test("controller rechecks authentication and institutional trust after asynchronous HTTPS verification", async () => {
  for (const change of ["authentication", "profiles", "invalidate", "dispose"] as const) {
    const verification = deferred<void>(); let available = true; let profiles = [trusted];
    const {value, applied} = controller({profiles: async () => profiles, verify: async () => verification.promise,
      assertAvailable: () => {if (!available) throw new Error("session changed");}});
    await value.search(); const operation = value.select("school");
    if (change === "authentication") available = false;
    if (change === "profiles") profiles = [{...trusted, apiUrl: "https://replacement.example.test"}];
    if (change === "invalidate") value.invalidate();
    if (change === "dispose") value.dispose();
    verification.resolve(); await operation; assert.deepEqual(applied, [], change); value.dispose();
  }
});

test("controller rechecks the session after a delayed trust-file reload and suppresses duplicate selections", async () => {
  const reload = deferred<TrustedDiscoveryServer[]>(); let reads = 0; let available = true;
  const {value, applied} = controller({profiles: async () => ++reads === 1 ? [trusted] : reload.promise,
    assertAvailable: () => {if (!available) throw new Error("session changed");}});
  await value.search(); const selection = value.select("school"); await Promise.resolve();
  await value.select("school"); assert.equal(reads, 2);
  available = false; reload.resolve([trusted]); await selection; assert.deepEqual(applied, []); value.dispose();
});

test("controller surfaces verification failure without echoing credentials or raw server responses", async () => {
  const {value, applied} = controller({verify: async () => {throw new Error("private-cookie-or-response");}});
  await value.search(); await value.select("school"); assert.deepEqual(applied, []);
  assert.match(value.state.message!, /HTTPS 인증서/); assert.doesNotMatch(value.state.message!, /private-cookie-or-response/); value.dispose();
});
