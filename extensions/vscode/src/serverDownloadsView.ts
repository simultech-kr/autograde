import { randomBytes, randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { ApiError, type AutogradeClient } from "./api";
import { ContentApi } from "./contentApi";
import { shouldDiscardTokensAfterRefreshError } from "./authPolicy";
import { isAssignmentDownloadable } from "./helpers";
import { parseServerDownloadsAction, renderServerDownloads, type DownloadKind, type DownloadRow, type DownloadSection } from "./serverDownloads";
export { SERVER_DOWNLOADS_COMMAND } from "./serverDownloads";
export interface ServerDownloadsViewOptions {
  authenticated(): boolean;
  downloadAssignment(id: string): Promise<void>;
  downloadMaterial(id: string): Promise<"saved" | "cancelled" | "unavailable" | "failed">;
  downloadContent(id: string): Promise<"synced" | "cancelled" | "unavailable" | "failed">;
  connect(): Promise<void>;
  onAuthenticationRequired?(): void | Promise<void>;
  onActionError?(error: unknown): void;
}
interface Scope { readonly origin: string; readonly generation: number }
interface ActionRow { readonly kind: DownloadKind; readonly id: string; readonly available: boolean }
const same = (a: Scope | undefined, b: Scope | undefined) => a?.origin === b?.origin && a?.generation === b?.generation;
const when = (value: string | undefined) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"}) + " KST" : "기한 미설정";
const size = (bytes: number) => bytes < 1048576 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
const emptySections = (): DownloadSection[] => (["assignment", "material", "content"] as const).map(kind => ({kind, state: "loading", rows: []}));
function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인이 만료되었습니다. 새 수령 코드로 연결하세요.";
    if ([404, 405].includes(error.status)) return "서버에서 이 목록 기능을 사용할 수 없습니다. 교수자에게 자료 배포 설정과 서버 버전을 확인해 달라고 요청하세요.";
    if (error.status === 403) return "수강 권한 또는 배포 기간을 확인할 수 없습니다. 교수자에게 문의하세요.";
    if (error.status === 429) return "요청이 일시적으로 많습니다. 잠시 후 새로고침하세요.";
  }
  return "목록을 불러오지 못했습니다. 서버 연결을 확인하고 새로고침하세요.";
}
/** Session-memory catalogue; all writes delegate to existing checked download flows. */
export class ServerDownloadsView implements vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private readonly listeners: vscode.Disposable[] = [];
  private readonly content: ContentApi;
  private scope?: Scope;
  private revision = 0;
  private operation = 0;
  private controller?: AbortController;
  private rows = new Map<string, ActionRow>();
  private sections: DownloadSection[] = [];
  private busy = false;
  private status = "수령 코드 연결 후 목록을 확인할 수 있습니다.";
  private disposed = false;
  private nonce = randomUUID();
  constructor(private readonly client: AutogradeClient, private readonly options: ServerDownloadsViewOptions) { this.content = new ContentApi(client); }
  private currentScope(): Scope | undefined {
    try { const generation = this.client.tokens.getSessionGeneration?.();
      if (!this.options.authenticated() || !Number.isSafeInteger(generation)) return undefined;
      return {origin: this.client.getBaseUrl(), generation: generation!};
    } catch { return undefined; }
  }
  show(): void {
    if (this.disposed) return;
    if (this.panel) { this.panel.reveal(); this.reconcile(); return; }
    const panel = vscode.window.createWebviewPanel("autograde.serverDownloads", "서버 과제·자료", vscode.ViewColumn.One, {enableScripts: true, enableCommandUris: false, localResourceRoots: [], retainContextWhenHidden: false});
    this.panel = panel; this.nonce = randomUUID();
    this.listeners.push(panel.webview.onDidReceiveMessage(message => this.handle(message)));
    this.listeners.push(panel.onDidDispose(() => { if (this.panel !== panel) return; this.panel = undefined; this.clear(); for (const listener of this.listeners.splice(0)) listener.dispose(); }));
    this.reconcile();
  }
  reconcile(): void {
    if (this.disposed) return;
    if (!this.panel) { this.clear(); return; }
    const next = this.currentScope();
    if (!same(this.scope, next)) { this.clear(); this.scope = next; if (this.panel && next) { void this.load(next); return; } }
    this.render();
  }
  clear(): void {
    this.operation++; this.controller?.abort(); this.controller = undefined; this.scope = undefined; this.rows.clear(); this.sections = []; this.busy = false;
    this.status = "수령 코드 연결 후 목록을 확인할 수 있습니다."; this.render();
  }
  private render(): void {
    if (!this.panel || this.disposed) return;
    this.panel.webview.html = renderServerDownloads({revision: ++this.revision, authenticated: Boolean(this.scope), trusted: vscode.workspace.isTrusted, busy: this.busy, status: this.status, sections: this.sections}, this.nonce);
  }
  private row(kind: DownloadKind, id: string, value: Omit<DownloadRow, "key">, rows: Map<string, ActionRow>): DownloadRow {
    if (!id || id.length > 128 || /[\u0000-\u001f\u007f]/.test(id)) throw new Error("Invalid catalogue identifier");
    const key = `row-${randomBytes(16).toString("hex")}`; rows.set(key, {kind, id, available: value.available}); return {...value, description: value.description.length > 800 ? value.description.slice(0, 800) + "…" : value.description, key};
  }
  private async load(scope: Scope): Promise<void> {
    this.controller?.abort(); const controller = new AbortController(), operation = ++this.operation;
    this.controller = controller; this.scope = scope; this.rows.clear(); this.sections = emptySections(); this.busy = true;
    this.status = "서버에서 과제와 배포 자료 목록을 확인하고 있습니다."; this.render();
    const current = () => !this.disposed && this.panel && !controller.signal.aborted && operation === this.operation && same(scope, this.currentScope());
    const rows = new Map<string, ActionRow>();
    const result = await Promise.allSettled([
      Promise.resolve().then(async (): Promise<DownloadSection> => {
        const assignments = await this.client.getAssignments(); if (assignments.length > 1000) throw new Error("Assignment catalogue too large");
        return {kind: "assignment", state: "ready", rows: assignments.map(item => this.row("assignment", item.id, {title: item.title, metadata: `${item.courseLabel ?? "현재 수업"} · 마감 ${when(item.dueAt)}`, description: item.document?.changeNote ?? "과제 파일을 새 폴더에 다운로드하고 작업 영역으로 연결합니다.", available: isAssignmentDownloadable(item), unavailableReason: "과제 파일이 아직 준비되지 않았습니다. 교수자에게 확인하세요."}, rows))};
      }),
      Promise.resolve().then(async (): Promise<DownloadSection> => {
        const catalog = await this.client.getStudentMaterials(controller.signal);
        return {kind: "material", state: "ready", rows: catalog.materials.map(item => this.row("material", item.id, {title: item.title, metadata: `${catalog.courseKey} · ${item.format.toUpperCase()} · ${size(item.sizeBytes)} · 파일 v${item.fileRevision}${item.updated ? " · 수정됨" : ""}`, description: `${when(item.opensAt)} ~ ${when(item.closesAt)}${item.instructions ? `\n${item.instructions}` : ""}`, available: item.availability === "open", unavailableReason: item.availability === "scheduled" ? "배포 시작 전입니다." : "배포 기간이 종료되었습니다."}, rows))};
      }),
      Promise.resolve().then(async (): Promise<DownloadSection> => {
        const context = await this.content.context(controller.signal), catalog = await this.content.catalog(context, controller.signal);
        return {kind: "content", state: "ready", rows: catalog.collections.map(item => this.row("content", item.collection_id, {title: item.title, metadata: `${catalog.course_key} · 배포 v${item.generation}`, description: item.change_note || "공개된 최신 자료 모음입니다.", available: true}, rows))};
      }),
    ]);
    const sameOperation = () => {
      try { return !this.disposed && Boolean(this.panel) && !controller.signal.aborted
        && operation === this.operation && scope.origin === this.client.getBaseUrl(); }
      catch { return false; }
    };
    if (result.some(item => item.status === "rejected" && item.reason instanceof ApiError
      && (item.reason.status === 401 || shouldDiscardTokensAfterRefreshError(item.reason.status, item.reason.code))) && sameOperation()) {
      const expiredGeneration = this.client.tokens.getSessionGeneration?.();
      const noSession = !await this.client.tokens.hasSession().catch(() => true);
      if (sameOperation() && expiredGeneration === this.client.tokens.getSessionGeneration?.() && noSession) {
        this.clear(); // No await between the identity check and the authentication reset callback.
        const reset = this.options.onAuthenticationRequired?.();
        this.status = "로그인이 만료되었습니다. 새 수령 코드로 연결하세요."; this.render();
        await reset; return;
      }
    }
    if (!current()) { if (operation === this.operation) this.reconcile(); return; }
    const kinds = ["assignment", "material", "content"] as const;
    this.sections = result.map((item, index): DownloadSection => item.status === "fulfilled" ? item.value : {kind: kinds[index]!, state: "error", rows: [], error: errorText(item.reason)});
    const visible = new Set(this.sections.flatMap(section => section.rows.map(row => row.key)));
    this.rows = new Map([...rows].filter(([key]) => visible.has(key))); this.busy = false; this.controller = undefined;
    this.status = result.some(item => item.status === "rejected") ? "일부 목록을 불러오지 못했습니다. 확인된 항목은 다운로드할 수 있습니다." : "본인에게 공개된 최신 목록입니다. 받을 항목을 선택하세요."; this.render();
  }
  private async handle(message: unknown): Promise<void> {
    const panel = this.panel;
    if (this.disposed || !panel) return;
    if (!same(this.scope, this.currentScope())) { this.reconcile(); return; }
    const action = parseServerDownloadsAction(message);
    if (!action || action.revision !== this.revision || this.busy) return;
    if (action.action === "refresh") { if (this.scope) await this.load(this.scope); return; }
    if (action.action === "connect" && this.scope) return;
    const row = action.action === "download" ? this.rows.get(action.rowKey) : undefined;
    if (action.action === "download" && (!this.scope || !row?.available || !vscode.workspace.isTrusted)) { this.render(); return; }
    const scope = this.scope, operation = ++this.operation; this.busy = true;
    this.status = action.action === "connect" ? "수령 코드 연결을 진행합니다." : "선택한 항목의 다운로드를 진행합니다."; this.render();
    try {
      if (action.action === "connect") {
        await this.options.connect();
        if (operation === this.operation && !this.currentScope()) this.status = "연결하지 않았습니다. 수령 코드로 연결한 뒤 목록을 확인하세요.";
      }
      else if (row) {
        if (!same(scope, this.currentScope()) || !vscode.workspace.isTrusted) return;
        const outcome = row.kind === "assignment" ? await this.options.downloadAssignment(row.id) : row.kind === "material" ? await this.options.downloadMaterial(row.id) : await this.options.downloadContent(row.id);
        if (operation !== this.operation || !same(scope, this.currentScope())) return;
        this.status = outcome === "saved" || outcome === "synced" ? "자료 수신을 완료했습니다. 저장 위치는 IDE 알림에서 확인하세요." : outcome === "cancelled" ? "다운로드를 취소했습니다. 다시 선택할 수 있습니다." : outcome === "failed" || outcome === "unavailable" ? "자료를 받지 못했습니다. IDE 안내를 확인하고 목록을 새로고침하세요." : "과제 다운로드 절차를 마쳤습니다. IDE 알림과 열린 폴더를 확인하세요.";
      }
    } catch (error) {
      if (!this.disposed && this.panel === panel && operation === this.operation && same(scope, this.currentScope())) {
        this.options.onActionError?.(error);
        this.status = "작업을 완료하지 못했습니다. 서버 연결과 IDE 안내를 확인하고 다시 시도하세요.";
      }
    }
    finally { if (operation === this.operation) { this.busy = false; this.reconcile(); } }
  }
  dispose(): void {
    if (this.disposed) return;
    const panel = this.panel; this.panel = undefined; this.clear(); this.disposed = true;
    for (const listener of this.listeners.splice(0)) listener.dispose(); panel?.dispose();
  }
}
