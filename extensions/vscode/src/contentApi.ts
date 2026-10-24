import { ApiError, AutogradeClient, RequestCancelledError, type RequestOptions } from "./api";
import { parseContentContext, parseContentCatalog, parseContentManifest, type ContentContext,
  type ContentCatalog, type ContentManifest, type ContentFile, MAX_MANIFEST_BYTES, MAX_CONTENT_FILE_BYTES } from "./contentSync";

/** Course content uses the existing memory-only login, origin binding and bounded byte transport. */
export class ContentApi {
  public constructor(private readonly client: AutogradeClient) {}

  private async bytes(endpoint: string, init: RequestInit, options: RequestOptions): Promise<Uint8Array> {
    const origin = this.client.getBaseUrl(), identity = this.client.tokens.getSessionGeneration?.();
    const current = () => {
      if (options.signal?.aborted || origin !== this.client.getBaseUrl() || identity !== this.client.tokens.getSessionGeneration?.()) {
        throw new RequestCancelledError();
      }
    };
    let token = await this.client.tokens.getAccessToken();
    current();
    const request = async () => {
      current();
      const result = await this.client.transport.requestBytes(endpoint, init, token, {...options, expectedBaseUrl: origin});
      current();
      return result;
    };
    try { return await request(); }
    catch (error) {
      current();
      if (!(error instanceof ApiError) || error.status !== 401) throw error;
      token = await this.client.tokens.getAccessToken(true);
      return request();
    }
  }

  private async json(endpoint: string, signal?: AbortSignal, body?: unknown): Promise<unknown> {
    const bytes = await this.bytes(endpoint, {method: body === undefined ? "GET" : "POST", headers: {Accept: "application/json", "Content-Type": "application/json"},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})},
    {signal, maxResponseBytes: MAX_MANIFEST_BYTES, acceptedContentTypes: ["application/json"]});
    try { return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes)); }
    catch { throw new ApiError("콘텐츠 응답 형식이 올바르지 않습니다.", 0, "invalid_content_response"); }
  }

  public async context(signal?: AbortSignal): Promise<ContentContext> {
    try { return parseContentContext(await this.json("/v1/content/context", signal)); }
    catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApiError("현재 서버는 강의자료 변경분 동기화를 지원하지 않습니다. 교수자에게 서버 업데이트를 요청하세요.", 404, "content_unsupported");
      }
      throw error;
    }
  }

  public async catalog(context: ContentContext, signal?: AbortSignal): Promise<ContentCatalog> {
    return parseContentCatalog(await this.json("/v1/content/collections", signal), context);
  }

  public async manifest(releaseId: string, context: ContentContext, signal?: AbortSignal): Promise<ContentManifest> {
    if (!/^cr_[a-f0-9]{32}$/.test(releaseId)) throw new Error("콘텐츠 배포 식별자가 올바르지 않습니다.");
    const manifest = parseContentManifest(await this.json(`/v1/content/releases/${releaseId}/manifest`, signal), context);
    if (manifest.release_id !== releaseId) throw new Error("요청한 콘텐츠 배포본과 응답이 다릅니다.");
    return manifest;
  }

  public async object(manifest: ContentManifest, file: ContentFile, signal?: AbortSignal): Promise<Uint8Array> {
    if (!manifest.files.some(item => item.sha256 === file.sha256 && item.size_bytes === file.size_bytes)) {
      throw new Error("배포 목록에 없는 파일은 받을 수 없습니다.");
    }
    return this.bytes(`/v1/content/releases/${manifest.release_id}/objects/${file.sha256}`,
      {method: "GET", headers: {"X-Autograde-Manifest-SHA256": manifest.manifest_sha256}},
      {signal, timeoutMs: 90000, maxResponseBytes: Math.max(1, Math.min(MAX_CONTENT_FILE_BYTES, file.size_bytes)),
        acceptedContentTypes: ["application/octet-stream"]});
  }

  public async verify(manifest: ContentManifest, signal?: AbortSignal): Promise<void> {
    const response = await this.json(`/v1/content/releases/${manifest.release_id}/verify`, signal, {
      expected_manifest_id: manifest.manifest_id, manifest_sha256: manifest.manifest_sha256,
      generation: manifest.generation, policy_revision: manifest.policy_revision,
    });
    if (!response || typeof response !== "object" || Array.isArray(response) ||
        (response as Record<string, unknown>).verified !== true ||
        (response as Record<string, unknown>).manifest_id !== manifest.manifest_id) {
      throw new Error("콘텐츠 배포본의 최종 권한 검증에 실패했습니다. 기존 자료는 변경하지 않았습니다.");
    }
  }
}
