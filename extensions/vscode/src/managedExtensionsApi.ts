import { ApiError, AutogradeClient, RequestCancelledError, type RequestOptions } from "./api";
import type { ClassroomCommandContext } from "./classroomCommands";
import { parseExtensionCatalog, type ExtensionDistributionClient, type ExtensionRelease, type ExtensionReport } from "./managedExtensions";

/** Same-origin, memory-only credentials; neither arbitrary URLs nor Marketplace downloads. */
export class ManagedExtensionsApi implements ExtensionDistributionClient {
  constructor(private readonly client: AutogradeClient) {}
  private async bytes(endpoint: string, body: unknown | undefined, scope: ClassroomCommandContext, options: RequestOptions): Promise<Uint8Array> {
    const guard = () => {
      if (options.signal?.aborted || this.client.getBaseUrl() !== scope.baseUrl
        || this.client.tokens.getSessionGeneration?.() !== scope.generation) throw new RequestCancelledError();
    };
    guard();
    let token = await this.client.tokens.getAccessToken(); guard();
    const request = async () => {
      guard();
      const bytes = await this.client.transport.requestBytes(endpoint,
        {method: body === undefined ? "GET" : "POST", ...(body === undefined ? {} : {body: JSON.stringify(body), headers: {"Content-Type": "application/json"}})},
        token, {...options, expectedBaseUrl: scope.baseUrl});
      guard(); return bytes;
    };
    try {return await request();}
    catch (error) {
      guard(); if (!(error instanceof ApiError) || error.status !== 401) throw error;
      token = await this.client.tokens.getAccessToken(true); guard(); return request();
    }
  }
  private async json(endpoint: string, body: unknown | undefined, scope: ClassroomCommandContext, signal: AbortSignal): Promise<unknown> {
    const bytes = await this.bytes(endpoint, body, scope, {signal, maxResponseBytes: 1024 * 1024, acceptedContentTypes: ["application/json"]});
    try {return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));}
    catch {throw new ApiError("확장 관리 응답 형식을 확인할 수 없습니다.", 0, "invalid_extension_response");}
  }
  public async catalog(scope: ClassroomCommandContext, signal: AbortSignal): Promise<readonly ExtensionRelease[]> {
    return parseExtensionCatalog(await this.json("/v1/ide/extensions", undefined, scope, signal));
  }
  public download(release: ExtensionRelease, scope: ClassroomCommandContext, signal: AbortSignal): Promise<Uint8Array> {
    if (!/^er_[a-f0-9]{32}$/.test(release.release_id)) throw new Error("Invalid release identity");
    return this.bytes(`/v1/ide/extensions/${release.release_id}/package`, undefined, scope,
      {signal, timeoutMs: 120_000, maxResponseBytes: release.size_bytes, acceptedContentTypes: ["application/octet-stream"]});
  }
  public async report(release: ExtensionRelease, report: ExtensionReport, scope: ClassroomCommandContext, signal: AbortSignal): Promise<void> {
    if (!/^er_[a-f0-9]{32}$/.test(release.release_id)) throw new Error("Invalid release identity");
    const value = await this.json(`/v1/ide/extensions/${release.release_id}/status`, {
      client_session_id: scope.clientSessionId, ...report, platform: process.platform, architecture: process.arch,
    }, scope, signal);
    if (!value || typeof value !== "object" || (value as Record<string, unknown>).accepted !== true
      || (value as Record<string, unknown>).status !== report.status) throw new ApiError("설치 상태 접수를 확인할 수 없습니다.", 0, "invalid_extension_response");
  }
}
