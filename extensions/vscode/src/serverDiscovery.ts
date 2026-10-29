import mdns from "multicast-dns";
import { isIP } from "node:net";
import { get } from "node:https";

export const DISCOVER_SERVERS_COMMAND = "autograde.discoverServers";
export const DISCOVERY_SERVICE = "_autograde._tcp.local";
export interface TrustedDiscoveryServer {
  institutionId: string; displayName: string; apiUrl: string; portalUrl: string;
}
export interface DiscoveredServer extends TrustedDiscoveryServer { id: string }
export interface DiscoveryState {
  status: "idle" | "searching" | "complete" | "unavailable";
  message?: string; servers: readonly DiscoveredServer[];
}

export function discoveryOrigin(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048 || /[\s\u0000-\u001f\u007f\\]/.test(value)) throw new Error("기관 주소를 확인하세요.");
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash
    || isIP(url.hostname) || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(url.hostname)
    || ![url.origin, `${url.origin}/`].includes(value)) throw new Error("기관 주소는 HTTPS 도메인 주소여야 합니다.");
  return url.origin;
}

/** Trust comes from installed/machine settings, never from the network or a project. */
export function trustedDiscoveryServers(value: unknown): TrustedDiscoveryServer[] {
  if (!Array.isArray(value) || value.length > 32) throw new Error("기관 검색 설정을 확인하세요.");
  const ids = new Set<string>();
  return value.map(item => {
    if (!item || typeof item !== "object" || Array.isArray(item) || Object.keys(item).sort().join() !== "apiUrl,displayName,institutionId,portalUrl"
      || typeof item.institutionId !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(item.institutionId) || ids.has(item.institutionId)
      || typeof item.displayName !== "string" || !item.displayName.trim() || item.displayName.length > 100 || /[\u0000-\u001f\u007f]/.test(item.displayName)) throw new Error("기관 검색 설정을 확인하세요.");
    ids.add(item.institutionId);
    const apiUrl = discoveryOrigin(item.apiUrl), portalUrl = discoveryOrigin(item.portalUrl);
    if (apiUrl === portalUrl) throw new Error("API와 포털 주소를 구분하세요.");
    return {institutionId: item.institutionId, displayName: item.displayName, apiUrl, portalUrl};
  });
}

type RecordValue = { name: string; type: string; ttl: number; data: unknown };
type Instance = { ptr?: number; srv?: {expires: number; target: string; port: number}; txt?: {expires: number; institution: string; portal: string} };
const dnsName = (value: string): string => value.replace(/\.$/, "").toLowerCase();
const instanceName = (name: string): boolean => name.length <= 255 && name.endsWith(`.${DISCOVERY_SERVICE}`) && name.length > DISCOVERY_SERVICE.length + 1;

/** Bounded, short-lived DNS-SD cache. A/AAAA data never controls HTTPS resolution. */
export class DiscoveryRecords {
  private readonly instances = new Map<string, Instance>();
  public constructor(private readonly trusted: readonly TrustedDiscoveryServer[], private readonly now = Date.now) {}
  public ingest(packet: unknown): string[] {
    if (!packet || typeof packet !== "object") return [];
    const p = packet as {answers?: unknown; additionals?: unknown};
    if (!Array.isArray(p.answers) || p.answers.length > 128 || p.additionals !== undefined && (!Array.isArray(p.additionals) || p.additionals.length > 128)) return [];
    const queries: string[] = [];
    for (const raw of [...p.answers, ...(Array.isArray(p.additionals) ? p.additionals : [])]) {
      if (!raw || typeof raw !== "object") continue;
      const r = raw as RecordValue;
      if (typeof r.name !== "string" || !Number.isFinite(r.ttl) || r.ttl < 0) continue;
      let name = dnsName(r.name);
      if (r.type === "PTR") {
        if (name !== DISCOVERY_SERVICE || typeof r.data !== "string") continue;
        name = dnsName(r.data);
      } else if (r.type !== "SRV" && r.type !== "TXT") continue;
      if (!instanceName(name)) continue;
      const existing = this.instances.get(name);
      if (!existing && this.instances.size >= 64) continue;
      const entry = existing ?? {};
      this.instances.set(name, entry);
      const expires = this.now() + Math.min(r.ttl, 120) * 1000;
      if (r.type === "PTR") {
        const hadPtr = entry.ptr && entry.ptr > this.now();
        entry.ptr = expires;
        if (r.ttl > 0 && (!hadPtr || !entry.srv || !entry.txt)) queries.push(name);
      }
      if (r.type === "SRV") {
        delete entry.srv;
        if (r.ttl === 0 || !r.data || typeof r.data !== "object") continue;
        const srv = r.data as {target?: unknown; port?: unknown};
        if (typeof srv.target !== "string" || typeof srv.port !== "number" || !Number.isInteger(srv.port) || srv.port < 1 || srv.port > 65535) continue;
        const target = dnsName(srv.target);
        try { discoveryOrigin(`https://${target}${srv.port === 443 ? "" : `:${srv.port}`}`); } catch { continue; }
        entry.srv = {expires, target, port: srv.port};
      }
      if (r.type === "TXT") {
        delete entry.txt;
        if (r.ttl === 0 || !Array.isArray(r.data) || r.data.length > 8) continue;
        let size = 0;
        const fields = new Map<string, string>();
        for (const part of r.data) {
          if (!Buffer.isBuffer(part) || part.length > 255) { size = Infinity; break; }
          size += part.length;
          const text = part.toString("utf8"), equal = text.indexOf("="), key = text.slice(0, equal).toLowerCase();
          if (equal < 1 || fields.has(key) || /[\u0000-\u001f\u007f]/.test(text)) { size = Infinity; break; }
          fields.set(key, text.slice(equal + 1));
        }
        if (size > 1024 || fields.get("v") !== "1") continue;
        const institution = fields.get("institution"), portal = fields.get("portal");
        if (!institution || !portal) continue;
        try { entry.txt = {expires, institution, portal: discoveryOrigin(portal)}; } catch { /* Invalid hints are discarded. */ }
      }
    }
    return queries;
  }
  public servers(): DiscoveredServer[] {
    const found = new Map<string, DiscoveredServer>();
    for (const entry of this.instances.values()) {
      const {ptr, srv, txt} = entry, now = this.now();
      if (!ptr || ptr <= now || !srv || srv.expires <= now || !txt || txt.expires <= now) continue;
      const apiUrl = `https://${srv.target}${srv.port === 443 ? "" : `:${srv.port}`}`;
      const profile = this.trusted.find(item => item.institutionId === txt.institution && item.apiUrl === apiUrl && item.portalUrl === txt.portal);
      if (profile) found.set(profile.institutionId, {...profile, id: profile.institutionId});
    }
    return [...found.values()].sort((a, b) => a.displayName.localeCompare(b.displayName));
  }
}

export interface DiscoverySocket {
  on(event: string, listener: (...args: any[]) => void): unknown;
  query(questions: Array<{name: string; type: "PTR" | "SRV" | "TXT"}>, callback?: (error: Error | null) => void): void;
  destroy(callback?: () => void): void;
}
export async function discoverServers(trusted: readonly TrustedDiscoveryServer[], options: {
  durationMs?: number; signal?: AbortSignal; socketFactory?: () => DiscoverySocket;
} = {}): Promise<DiscoveredServer[]> {
  if (options.signal?.aborted) return [];
  const cache = new DiscoveryRecords(trusted);
  return new Promise((resolve, reject) => {
    let socket: DiscoverySocket | undefined, completed = false;
    let timer: ReturnType<typeof setTimeout> | undefined, repeat: ReturnType<typeof setTimeout> | undefined;
    const finish = (error?: Error): void => {
      if (completed) return;
      completed = true; clearTimeout(timer); clearTimeout(repeat); options.signal?.removeEventListener("abort", abort);
      let settled = false;
      const done = (): void => {
        if (settled) return;
        settled = true;
        if (error) reject(new Error("자동 검색을 시작하지 못했습니다. 로컬 네트워크 접근 권한을 확인하거나 서버 주소를 직접 입력하세요.")); else resolve(cache.servers());
      };
      try { if (socket) socket.destroy(done); else done(); } catch { done(); }
    };
    const abort = (): void => finish();
    options.signal?.addEventListener("abort", abort, {once: true});
    timer = setTimeout(() => finish(), Math.max(20, Math.min(options.durationMs ?? 3500, 10000)));
    try {
      socket = options.socketFactory?.() ?? mdns({type: "udp4", reuseAddr: true, bind: "0.0.0.0"});
      socket.on("error", (error: Error) => finish(error));
      // Malformed unrelated multicast traffic is ignored, never logged or displayed.
      socket.on("warning", () => {});
      const query = (): void => { if (!completed) socket?.query([{name: DISCOVERY_SERVICE, type: "PTR"}], error => { if (error) finish(error); }); };
      socket.on("ready", () => { query(); if (!completed) repeat = setTimeout(query, 1000); });
      let queryBudget = 128;
      const lastQuery = new Map<string, number>();
      socket.on("response", (packet: unknown) => {
        if (completed) return;
        for (const name of cache.ingest(packet)) {
          if (queryBudget < 1 || Date.now() - (lastQuery.get(name) ?? 0) < 500) continue;
          queryBudget--; lastQuery.set(name, Date.now());
          socket?.query([{name, type: "SRV"}, {name, type: "TXT"}]);
        }
      });
    } catch (error) { finish(error instanceof Error ? error : new Error("discovery failed")); }
  });
}

/** Verify the trusted canonical address without student tokens, cookies or redirects. */
export function verifyDiscoveredServer(server: TrustedDiscoveryServer): Promise<void> {
  const origin = discoveryOrigin(server.apiUrl);
  return new Promise((resolve, reject) => {
    const request = get(`${origin}/healthz`, {rejectUnauthorized: true, headers: {Accept: "application/json"}}, response => {
      const chunks: Buffer[] = []; let size = 0;
      if (response.statusCode !== 200) { response.resume(); reject(new Error("서버의 HTTPS 상태 확인에 실패했습니다.")); return; }
      response.on("data", (chunk: Buffer) => { size += chunk.length; if (size > 1024) request.destroy(new Error("Invalid health response")); else chunks.push(chunk); });
      response.on("error", reject);
      response.on("end", () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (value.status !== "ok") throw new Error("Invalid health response");
          resolve();
        } catch { reject(new Error("Autograde 서버 상태를 확인하지 못했습니다.")); }
      });
    });
    const timeout = setTimeout(() => request.destroy(new Error("서버 연결 확인 시간이 초과되었습니다.")), 5000);
    request.on("close", () => clearTimeout(timeout));
    request.on("error", () => reject(new Error("HTTPS 인증서 또는 서버 연결을 확인하지 못했습니다. 주소를 적용하지 않았습니다.")));
  });
}

export class ServerDiscoveryController {
  public state: DiscoveryState = {status: "idle", servers: []};
  private abort?: AbortController;
  private generation = 0;
  private selecting = false;
  private disposed = false;
  public constructor(private readonly options: {
    profiles(): Promise<TrustedDiscoveryServer[]>;
    assertAvailable(): void;
    changed(): void;
    apply(server: DiscoveredServer): Promise<void>;
    scan?: typeof discoverServers;
    verify?: typeof verifyDiscoveredServer;
  }) {}
  public async search(): Promise<void> {
    if (this.disposed || this.abort || this.selecting) return;
    this.options.assertAvailable();
    const generation = ++this.generation, abort = new AbortController(); this.abort = abort;
    this.state = {status: "searching", servers: [], message: "같은 네트워크의 수업 서버를 찾고 있습니다."}; this.options.changed();
    try {
      const profiles = await this.options.profiles();
      if (abort.signal.aborted) return;
      if (!profiles.length) throw new Error("등록된 기관이 없습니다.");
      const servers = await (this.options.scan ?? discoverServers)(profiles, {signal: abort.signal});
      if (this.disposed || generation !== this.generation) return;
      this.options.assertAvailable();
      this.state = {status: "complete", servers, message: servers.length
        ? "등록된 기관 주소와 일치하는 서버입니다. 선택하면 HTTPS 연결을 확인합니다."
        : "등록된 수업 서버를 찾지 못했습니다. 같은 네트워크의 안내 프로그램을 확인하거나 서버 주소를 직접 입력하세요."};
    } catch {
      if (!this.disposed && generation === this.generation) this.state = {status: "unavailable", servers: [], message: "자동 검색을 완료하지 못했습니다. 기관 설정·로컬 네트워크 접근 권한을 확인하거나 서버 주소를 직접 입력하세요."};
    } finally { if (this.abort === abort) this.abort = undefined; this.options.changed(); }
  }
  public async select(id: string): Promise<void> {
    if (this.disposed || this.abort || this.selecting) return;
    this.options.assertAvailable();
    const server = this.state.servers.find(item => item.id === id), generation = this.generation;
    if (!server) throw new Error("검색 결과에서 서버를 다시 선택하세요.");
    this.selecting = true;
    this.state = {...this.state, message: "선택한 서버의 HTTPS 연결을 확인하고 있습니다."}; this.options.changed();
    try {
      await (this.options.verify ?? verifyDiscoveredServer)(server);
      this.options.assertAvailable();
      if (this.disposed || generation !== this.generation) return;
      const profiles = await this.options.profiles();
      this.options.assertAvailable();
      if (this.disposed || generation !== this.generation) return;
      if (!profiles.some(p => p.institutionId === id && p.apiUrl === server.apiUrl && p.portalUrl === server.portalUrl)) throw new Error("기관 설정이 변경되었습니다. 다시 검색하세요.");
      await this.options.apply(server);
      if (!this.disposed && generation === this.generation) this.state = {...this.state, message: `${server.displayName}에 연결했습니다. 학생 포털에서 수령 코드를 받아 연결하세요.`};
    } catch {
      if (!this.disposed && generation === this.generation) this.state = {...this.state, message: "서버를 적용하지 못했습니다. HTTPS 인증서·연결 상태와 기관 설정을 확인한 뒤 다시 선택하세요."};
    } finally { this.selecting = false; this.options.changed(); }
  }
  public invalidate(): void {
    this.generation++; this.abort?.abort(); this.state = {status: "idle", servers: []}; this.options.changed();
  }
  public dispose(): void { this.disposed = true; this.generation++; this.abort?.abort(); }
}
