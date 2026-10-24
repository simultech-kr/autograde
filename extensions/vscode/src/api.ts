import {
  isInsecureHttpPilotUrl,
  normalizeAssignments,
  normalizeClaimCode,
  normalizeGradeResult,
  normalizeServiceBaseUrl,
  normalizeSubmission,
} from "./helpers";
import { shouldDiscardTokensAfterRefreshError } from "./authPolicy";
import type { ClassroomEvent, ClassroomScope } from "./classroomActivity";
import { parseClassroomCommands, type ClassroomCommand, type ClassroomCommandContext, type ClassroomCommandResponse } from "./classroomCommands";
import { parseAssignmentDocument } from "./assignmentDocument";
import { parseSubmissionHistory, verifySubmissionSource, type SubmissionVersion } from "./submissionHistory";
import { parseAcceptedAssignments, type AcceptedAssignmentEntry } from "./acceptedAssignmentCatalog";
import { MAX_STUDENT_MATERIAL_BYTES, MAX_STUDENT_MATERIALS, parseStudentMaterials, verifyStudentMaterial,
  type StudentMaterial, type StudentMaterialsCatalog } from "./studentMaterials";
import type {
  Assignment,
  AssignmentClaim,
  DeviceAuthorization,
  GradeResult,
  SubmissionSummary,
  TokenResponse,
} from "./types";

export const DEFAULT_REQUEST_TIMEOUT_MS = 20_000;
export const SUBMISSION_REQUEST_TIMEOUT_MS = 90_000;
export const STARTER_REQUEST_TIMEOUT_MS = 90_000;
export const MAX_STARTER_RESPONSE_BYTES = 25 * 1024 * 1024;
const MAX_RETRY_DELAY_MS = 30_000;
const SUBMISSION_MAX_ATTEMPTS = 3;

export interface RequestOptions {
  readonly timeoutMs?: number;
  readonly signal?: AbortSignal;
  readonly maxResponseBytes?: number;
  readonly acceptedContentTypes?: readonly string[];
  /** Bind an authenticated request to the origin for which its token was obtained. */
  readonly expectedBaseUrl?: string;
}

export interface ApiTransport {
  getBaseUrl(): string;
  request<T>(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options?: RequestOptions,
  ): Promise<T>;
  requestBytes(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options?: RequestOptions,
  ): Promise<Uint8Array>;
}

export interface LegacySecretStore {
  delete(key: string): Thenable<void>;
}

export interface SessionTokenStore {
  getSessionGeneration?(): number;
  hasSession(): Promise<boolean>;
  storeSession(tokens: TokenResponse, expectedBaseUrl: string): Promise<void>;
  getAccessToken(forceRefresh?: boolean): Promise<string>;
  clear(): Promise<void>;
}

export class ApiError extends Error {
  public constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
    public readonly retryAfterMs?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class RequestCancelledError extends Error {
  public constructor() {
    super("Autograde 서비스 요청이 취소되었습니다.");
    this.name = "RequestCancelledError";
  }
}

export class HttpTransport implements ApiTransport {
  public constructor(
    private readonly configuredBaseUrl: () => string,
    private readonly fetchImplementation: typeof fetch = globalThis.fetch.bind(globalThis),
    private readonly allowInsecureHttpPilot: () => boolean = () => false,
  ) {}

  public getBaseUrl(): string {
    return normalizeServiceBaseUrl(
      this.configuredBaseUrl(),
      this.allowInsecureHttpPilot(),
    );
  }

  public async request<T>(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options: RequestOptions = {},
  ): Promise<T> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new TypeError("request timeout must be a positive finite number");
    }
    const baseUrl = this.getBaseUrl();
    if (options.expectedBaseUrl !== undefined && options.expectedBaseUrl !== baseUrl) {
      throw new ApiError("Autograde 서비스 주소가 변경되었습니다. 다시 로그인하세요.", 401, "login_required");
    }
    const requestUrl = `${baseUrl}${endpoint}`;
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const abortFromCaller = (): void => controller.abort(options.signal?.reason);
    if (options.signal?.aborted) {
      abortFromCaller();
    } else {
      options.signal?.addEventListener("abort", abortFromCaller, { once: true });
    }
    const headers = new Headers(init.headers);
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/json");
    }
    if (init.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (bearerToken) {
      headers.set("Authorization", `Bearer ${bearerToken}`);
    }

    try {
      const response = await this.fetchImplementation(requestUrl, {
        ...init,
        headers,
        redirect: "error",
        signal: controller.signal,
      });

      let payload: unknown;
      let hasJsonPayload = response.status === 204;
      if (response.status !== 204) {
        try {
          payload = await response.json();
          hasJsonPayload = true;
        } catch (error) {
          if (controller.signal.aborted) {
            throw error;
          }
          payload = undefined;
        }
      }

      const retryAfterMs = parseRetryAfter(response.headers.get("Retry-After"));

      if (!response.ok) {
        const errorRecord =
          typeof payload === "object" && payload !== null
            ? payload as Record<string, unknown>
            : undefined;
        const nestedError =
          typeof errorRecord?.error === "object" && errorRecord.error !== null
            ? errorRecord.error as Record<string, unknown>
            : undefined;
        const code =
          typeof errorRecord?.error === "string"
            ? errorRecord.error
            : typeof errorRecord?.code === "string"
              ? errorRecord.code
              : typeof nestedError?.code === "string"
                ? nestedError.code
                : undefined;
        const serverMessage =
          typeof errorRecord?.error_description === "string"
            ? errorRecord.error_description
            : typeof errorRecord?.message === "string"
              ? errorRecord.message
              : typeof nestedError?.message === "string"
                ? nestedError.message
                : undefined;
        throw new ApiError(
          serverMessage ?? `Autograde 요청이 실패했습니다 (${response.status}).`,
          response.status,
          code,
          retryAfterMs,
        );
      }
      if (!hasJsonPayload) {
        throw new ApiError(
          "Autograde 서비스가 JSON이 아닌 성공 응답을 반환했습니다.",
          0,
          "invalid_response",
        );
      }
      return payload as T;
    } catch (error) {
      if (error instanceof ApiError || error instanceof RequestCancelledError) {
        throw error;
      }
      if (controller.signal.aborted) {
        if (timedOut) {
          throw new ApiError("Autograde 서비스 요청 시간이 초과되었습니다.", 0, "request_timeout");
        }
        throw new RequestCancelledError();
      }
      throw new ApiError("Autograde 서비스에 연결할 수 없습니다.", 0, "network_error");
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abortFromCaller);
    }
  }

  public async requestBytes(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options: RequestOptions = {},
  ): Promise<Uint8Array> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    const maxResponseBytes = options.maxResponseBytes ?? 64 * 1024 * 1024;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new TypeError("request timeout must be a positive finite number");
    }
    if (!Number.isSafeInteger(maxResponseBytes) || maxResponseBytes <= 0) {
      throw new TypeError("max response bytes must be a positive safe integer");
    }
    const baseUrl = this.getBaseUrl();
    if (options.expectedBaseUrl !== undefined && options.expectedBaseUrl !== baseUrl) {
      throw new ApiError("Autograde 서비스 주소가 변경되었습니다. 다시 로그인하세요.", 401, "login_required");
    }
    const requestUrl = `${baseUrl}${endpoint}`;
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const abortFromCaller = (): void => controller.abort(options.signal?.reason);
    if (options.signal?.aborted) {
      abortFromCaller();
    } else {
      options.signal?.addEventListener("abort", abortFromCaller, { once: true });
    }
    const headers = new Headers(init.headers);
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/octet-stream");
    }
    if (init.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/octet-stream");
    }
    if (bearerToken) {
      headers.set("Authorization", `Bearer ${bearerToken}`);
    }

    try {
      const response = await this.fetchImplementation(requestUrl, {
        ...init,
        headers,
        redirect: "error",
        signal: controller.signal,
      });
      const contentLength = response.headers.get("Content-Length");
      if (contentLength && /^[0-9]+$/.test(contentLength) && Number(contentLength) > maxResponseBytes) {
        await response.body?.cancel().catch(() => undefined);
        throw new ApiError("Autograde 응답이 허용된 크기를 초과했습니다.", 0, "response_too_large");
      }
      if (!response.ok) {
        const errorBytes = await readLimitedBody(response, Math.min(maxResponseBytes, 1024 * 1024));
        const payload = parseJsonBytes(errorBytes);
        throw apiErrorFromPayload(response, payload);
      }
      const accepted = options.acceptedContentTypes?.map((value) => value.toLowerCase());
      const contentType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
      if (accepted && (!contentType || !accepted.includes(contentType))) {
        await response.body?.cancel().catch(() => undefined);
        throw new ApiError("Autograde 서비스가 예상한 파일 형식이 아닌 응답을 반환했습니다.", 0, "invalid_response");
      }
      // Keep timeout/cancellation alive until the entire body arrives, not just its headers.
      return await readLimitedBody(response, maxResponseBytes);
    } catch (error) {
      if (error instanceof ApiError || error instanceof RequestCancelledError) {
        throw error;
      }
      if (controller.signal.aborted) {
        if (timedOut) {
          throw new ApiError("Autograde 서비스 요청 시간이 초과되었습니다.", 0, "request_timeout");
        }
        throw new RequestCancelledError();
      }
      throw new ApiError("Autograde 서비스에 연결할 수 없습니다.", 0, "network_error");
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abortFromCaller);
    }
  }
}

async function readLimitedBody(response: Response, maximumBytes: number): Promise<Uint8Array> {
  if (!response.body) {
    return new Uint8Array();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      total += value.byteLength;
      if (total > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        throw new ApiError("Autograde 응답이 허용된 크기를 초과했습니다.", 0, "response_too_large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function parseJsonBytes(bytes: Uint8Array): unknown {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    return undefined;
  }
}

function apiErrorFromPayload(response: Response, payload: unknown): ApiError {
  const errorRecord =
    typeof payload === "object" && payload !== null
      ? payload as Record<string, unknown>
      : undefined;
  const nestedError =
    typeof errorRecord?.error === "object" && errorRecord.error !== null
      ? errorRecord.error as Record<string, unknown>
      : undefined;
  const code =
    typeof errorRecord?.error === "string"
      ? errorRecord.error
      : typeof errorRecord?.code === "string"
        ? errorRecord.code
        : typeof nestedError?.code === "string"
          ? nestedError.code
          : undefined;
  const serverMessage =
    typeof errorRecord?.error_description === "string"
      ? errorRecord.error_description
      : typeof errorRecord?.message === "string"
        ? errorRecord.message
        : typeof nestedError?.message === "string"
          ? nestedError.message
          : undefined;
  return new ApiError(
    serverMessage ?? `Autograde 요청이 실패했습니다 (${response.status}).`,
    response.status,
    code,
    parseRetryAfter(response.headers.get("Retry-After")),
  );
}

export function parseRetryAfter(value: string | null, now = Date.now()): number | undefined {
  const normalized = value?.trim();
  if (!normalized) {
    return undefined;
  }
  if (/^[0-9]+(?:\.[0-9]+)?$/.test(normalized)) {
    const seconds = Number(normalized);
    return Number.isFinite(seconds) ? Math.max(0, Math.ceil(seconds * 1000)) : undefined;
  }
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - now) : undefined;
}

export function isRetryableApiError(error: unknown): error is ApiError {
  return error instanceof ApiError && (
    error.status === 0 && (
      error.code === "request_timeout" ||
      error.code === "network_error" ||
      error.code === "invalid_response"
    ) ||
    error.status === 429 ||
    error.status === 502 ||
    error.status === 503 ||
    error.status === 504
  );
}

export function retryDelayMs(error: ApiError, retryIndex: number): number {
  const retryAfter = error.retryAfterMs;
  if (retryAfter !== undefined) {
    return Math.min(MAX_RETRY_DELAY_MS, Math.max(0, retryAfter));
  }
  const exponent = Math.max(0, Math.min(8, retryIndex));
  return Math.min(MAX_RETRY_DELAY_MS, 500 * (2 ** exponent));
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

const ACCESS_TOKEN_KEY = "autograde.auth.accessToken.v1";
const REFRESH_TOKEN_KEY = "autograde.auth.refreshToken.v1";
const ACCESS_EXPIRES_KEY = "autograde.auth.accessExpiresAt.v1";
const TOKEN_AUDIENCE_KEY = "autograde.auth.serviceBaseUrl.v1";

/** Remove credentials persisted by extension versions released before session-only authentication. */
export async function clearLegacyPersistedTokens(secrets: LegacySecretStore): Promise<void> {
  await Promise.all([
    secrets.delete(ACCESS_TOKEN_KEY),
    secrets.delete(REFRESH_TOKEN_KEY),
    secrets.delete(ACCESS_EXPIRES_KEY),
    secrets.delete(TOKEN_AUDIENCE_KEY),
  ]);
}

export class TokenManager {
  /** Identity boundary for UI work; routine token rotation preserves the session. */
  public getSessionGeneration(): number { return this.identityGeneration; }
  private accessToken: string | undefined;
  private refreshToken: string | undefined;
  private accessExpiresAt = 0;
  private tokenAudience: string | undefined;
  private sessionGeneration = 0;
  private identityGeneration = 0;
  private refreshOperation: { readonly generation: number; readonly promise: Promise<string> } | undefined;

  public constructor(private readonly transport: ApiTransport) {}

  public async hasSession(): Promise<boolean> {
    this.clearIfAudienceChanged();
    return this.refreshToken !== undefined;
  }

  public async storeSession(tokens: TokenResponse, expectedBaseUrl: string): Promise<void> {
    const currentBaseUrl = this.transport.getBaseUrl();
    if (currentBaseUrl !== expectedBaseUrl) {
      this.clearMemory();
      throw new ApiError(
        "Autograde 서비스 주소가 변경되었습니다. 다시 로그인하세요.",
        401,
        "login_required",
      );
    }
    // The comparison and replacement are synchronous, so a configuration
    // event cannot interleave and bind credentials from one origin to another.
    this.replaceSession(tokens, expectedBaseUrl);
    this.identityGeneration += 1;
  }

  public async getAccessToken(forceRefresh = false): Promise<string> {
    this.clearIfAudienceChanged();
    if (
      !forceRefresh &&
      this.accessToken &&
      Number.isFinite(this.accessExpiresAt) &&
      this.accessExpiresAt > Date.now() + 30_000
    ) {
      return this.accessToken;
    }
    return this.refresh();
  }

  public async clear(): Promise<void> {
    this.clearMemory();
  }

  private async refresh(): Promise<string> {
    const generation = this.sessionGeneration;
    if (!this.refreshOperation || this.refreshOperation.generation !== generation) {
      const promise = this.performRefresh(generation).finally(() => {
        if (this.refreshOperation?.promise === promise) {
          this.refreshOperation = undefined;
        }
      });
      this.refreshOperation = { generation, promise };
    }
    return this.refreshOperation.promise;
  }

  private async performRefresh(generation: number): Promise<string> {
    const refreshToken = this.refreshToken;
    const audience = this.tokenAudience;
    if (!refreshToken || !audience) {
      throw new ApiError("Autograde 로그인이 필요합니다.", 401, "login_required");
    }
    try {
      if (audience !== this.transport.getBaseUrl()) {
        this.clearMemory();
        throw new ApiError("Autograde 로그인이 필요합니다.", 401, "login_required");
      }
      const response = await this.transport.request<TokenResponse>("/v1/tokens/refresh", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refreshToken }),
      }, undefined, { expectedBaseUrl: audience });
      if (
        generation !== this.sessionGeneration ||
        refreshToken !== this.refreshToken ||
        audience !== this.transport.getBaseUrl()
      ) {
        throw new ApiError("Autograde 로그인이 필요합니다.", 401, "login_required");
      }
      this.replaceSession(response, audience);
      return response.access_token;
    } catch (error) {
      if (
        error instanceof ApiError &&
        generation === this.sessionGeneration &&
        shouldDiscardTokensAfterRefreshError(error.status, error.code)
      ) {
        this.clearMemory();
      }
      throw error;
    }
  }

  private clearIfAudienceChanged(): void {
    if (
      (this.accessToken !== undefined || this.refreshToken !== undefined) &&
      this.tokenAudience !== this.transport.getBaseUrl()
    ) {
      this.clearMemory();
    }
  }

  private replaceSession(tokens: TokenResponse, audience: string): void {
    validateTokenResponse(tokens);
    this.sessionGeneration += 1;
    this.accessToken = tokens.access_token;
    this.refreshToken = tokens.refresh_token;
    this.accessExpiresAt = Date.now() + tokens.expires_in * 1000;
    this.tokenAudience = audience;
  }

  private clearMemory(): void {
    this.sessionGeneration += 1;
    this.identityGeneration += 1;
    this.accessToken = undefined;
    this.refreshToken = undefined;
    this.accessExpiresAt = 0;
    this.tokenAudience = undefined;
  }
}

function validateTokenResponse(tokens: TokenResponse): void {
  if (
    !tokens ||
    typeof tokens.access_token !== "string" ||
    !tokens.access_token ||
    typeof tokens.refresh_token !== "string" ||
    !tokens.refresh_token ||
    typeof tokens.expires_in !== "number" ||
    tokens.expires_in <= 0 ||
    (tokens.token_type !== undefined && tokens.token_type.toLowerCase() !== "bearer")
  ) {
    throw new ApiError("서비스가 유효하지 않은 token 응답을 반환했습니다.", 0);
  }
}

export class AutogradeClient {
  public constructor(
    public readonly transport: ApiTransport,
    public readonly tokens: SessionTokenStore,
    private readonly retrySleep: (milliseconds: number) => Promise<void> = delay,
  ) {}

  private checkClassroomCommandScope(context: ClassroomCommandContext, signal: AbortSignal): void {
    if (signal.aborted) throw new RequestCancelledError();
    if (context.baseUrl !== this.getBaseUrl() || context.generation !== this.tokens.getSessionGeneration?.()) {
      throw new ApiError("교수자 요청 세션이 변경되었습니다.", 401, "login_required");
    }
    if (!/^[A-Za-z0-9_-]{8,96}$/.test(context.clientSessionId)) throw new Error("수업 연결 정보가 올바르지 않습니다.");
  }
  public async pollClassroomCommands(context: ClassroomCommandContext, signal: AbortSignal): Promise<readonly ClassroomCommand[]> {
    this.checkClassroomCommandScope(context, signal);
    // Server timestamps precede response transfer. Subtract the whole request
    // duration conservatively instead of extending a request by network latency.
    const startedAt = Date.now();
    const value = await this.authorizedRequest<unknown>("/v1/classroom/commands/poll", {
      method: "POST", body: JSON.stringify({client_session_id: context.clientSessionId}),
    }, {timeoutMs: 5000, signal, expectedBaseUrl: context.baseUrl});
    this.checkClassroomCommandScope(context, signal);
    return parseClassroomCommands(value, startedAt);
  }
  public async respondClassroomCommand(id: string, response: ClassroomCommandResponse, context: ClassroomCommandContext, signal: AbortSignal): Promise<void> {
    this.checkClassroomCommandScope(context, signal);
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) throw new Error("교수자 요청 식별자가 올바르지 않습니다.");
    const checks = response.result?.checks;
    const result = response.result ? {
      ...(response.result.submission_id ? {submission_id: response.result.submission_id} : {}),
      ...(checks ? {checks: {server: checks.server, workspace: checks.workspace, compiler: checks.compiler, os: checks.os, remote: checks.remote}} : {}),
    } : undefined;
    const ack = await this.authorizedRequest<{command?: {command_id?: string; status?: string; result_code?: string | null}; duplicate?: boolean}>(`/v1/classroom/commands/${encodeURIComponent(id)}/responses`, {
      method: "POST", body: JSON.stringify({client_session_id: context.clientSessionId, status: response.status,
        ...(response.result_code ? {result_code: response.result_code} : {}), ...(result ? {result} : {})}),
    }, {timeoutMs: 5000, signal, expectedBaseUrl: context.baseUrl});
    this.checkClassroomCommandScope(context, signal);
    if (ack?.command?.command_id !== id || ack.command.status !== response.status || typeof ack.duplicate !== "boolean"
      || (response.result_code && ack.command.result_code !== response.result_code)) {
      throw new Error("교수자 요청 처리 결과를 서버가 확인하지 못했습니다.");
    }
  }

  /** Optional classroom presence is isolated from submission retries and bound to its original identity. */
  public async reportClassroomEvent(event: ClassroomEvent, scope: ClassroomScope, signal: AbortSignal): Promise<void> {
    const checkScope = (): void => {
      if (signal.aborted) throw new RequestCancelledError();
      if (scope.baseUrl !== this.transport.getBaseUrl() || scope.generation !== this.tokens.getSessionGeneration?.()
        || scope.assignmentId !== event.assignment_id) {
        throw new ApiError("수업 상태 공유 세션이 변경되었습니다.", 401, "login_required");
      }
    };
    checkScope();
    // Explicit field projection prevents file names, paths, contents, and student IDs
    // from ever accidentally being serialized from an expanded client-side object.
    const body = JSON.stringify({
      client_session_id: event.client_session_id, sequence: event.sequence, kind: event.kind,
      assignment_id: event.assignment_id,
      environment: { os: event.environment.os, ide: event.environment.ide,
        ...(event.environment.remote_name ? { remote_name: event.environment.remote_name } : {}) },
    });
    await this.authorizedRequest<unknown>("/v1/classroom/events", { method: "POST", body }, {
      timeoutMs: 5_000, signal, expectedBaseUrl: scope.baseUrl,
    });
    checkScope();
  }

  public async createDeviceAuthorization(
    deviceName: string,
    extensionVersion: string,
    signal?: AbortSignal,
    expectedBaseUrl?: string,
    claimCode?: string,
  ): Promise<DeviceAuthorization> {
    if (claimCode !== undefined) {
      const baseUrl = this.transport.getBaseUrl();
      if (expectedBaseUrl !== undefined && baseUrl !== expectedBaseUrl) {
        throw new ApiError("Autograde 서비스 주소가 변경되었습니다.", 401, "login_required");
      }
      if (isInsecureHttpPilotUrl(baseUrl)) {
        throw new ApiError("과제 수령 코드는 HTTPS 서버에서만 사용할 수 있습니다.", 0, "insecure_claim_redemption");
      }
      claimCode = normalizeClaimCode(claimCode);
      if (!claimCode) {
        throw new ApiError("수령 코드 형식이 올바르지 않습니다.", 0, "invalid_claim_code");
      }
      expectedBaseUrl = baseUrl;
    }
    return this.transport.request<DeviceAuthorization>("/v1/device-authorizations", {
      method: "POST",
      body: JSON.stringify({
        client: "vscode-extension",
        extension_version: extensionVersion,
        device_name: deviceName,
        ...(claimCode ? { claim_code: claimCode } : {}),
      }),
    }, undefined, { signal, expectedBaseUrl });
  }

  public exchangeDeviceCode(
    deviceCode: string,
    signal?: AbortSignal,
    expectedBaseUrl?: string,
  ): Promise<TokenResponse> {
    return this.transport.request<TokenResponse>("/v1/device-authorizations/token", {
      method: "POST",
      body: JSON.stringify({ device_code: deviceCode }),
    }, undefined, { signal, expectedBaseUrl });
  }

  /**
   * Redeem one opaque, single-use assignment claim code.
   *
   * This request is intentionally unauthenticated: the claim code approves an
   * already-pending device authorization. Tokens are obtained separately from
   * the existing bounded device-token exchange. The code is sent only in the
   * JSON body so it cannot leak through a URL, referrer, or Authorization header.
   */
  public async redeemAssignmentClaim(
    claimCode: string,
    deviceCode: string,
    signal?: AbortSignal,
    expectedBaseUrl?: string,
  ): Promise<AssignmentClaim> {
    const serviceBaseUrl = this.transport.getBaseUrl();
    if (expectedBaseUrl !== undefined && expectedBaseUrl !== serviceBaseUrl) {
      throw new ApiError(
        "Autograde 서비스 주소가 변경되었습니다. 수령 코드를 다시 입력하세요.",
        401,
        "login_required",
      );
    }
    if (isInsecureHttpPilotUrl(serviceBaseUrl)) {
      throw new ApiError(
        "과제 수령 코드는 HTTPS 서버에서만 사용할 수 있습니다.",
        0,
        "insecure_claim_redemption",
      );
    }
    const normalizedClaimCode = normalizeClaimCode(claimCode);
    if (!normalizedClaimCode) {
      throw new ApiError("수령 코드 형식이 올바르지 않습니다.", 0, "invalid_claim_code");
    }
    const payload = await this.transport.request<unknown>("/v1/assignment-claims/redeem", {
      method: "POST",
      body: JSON.stringify({
        claim_code: normalizedClaimCode,
        device_code: deviceCode,
      }),
    }, undefined, { signal, expectedBaseUrl });
    return normalizeAssignmentClaim(payload);
  }

  public async getAssignments(): Promise<Assignment[]> {
    // Do not fall back to the course catalog on an older server.
    try {
      const payload = await this.authorizedRequest<unknown>("/v1/accepted-assignments", { method: "GET" });
      return normalizeAssignments(payload);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApiError("서버가 수락 과제 전용 목록을 지원하지 않습니다. 교수자에게 서버 업데이트를 요청하세요.",
          404, "server_upgrade_required");
      }
      throw error;
    }
  }

  public getBaseUrl(): string { return this.transport.getBaseUrl(); }

  public async getStudentMaterials(signal?: AbortSignal): Promise<StudentMaterialsCatalog> {
    const checkSession = this.sessionGuard();
    const materials: StudentMaterial[] = [];
    const identifiers = new Set<string>();
    const cursors = new Set<string>();
    let courseKey: string | undefined;
    let cursor: string | undefined;
    const invalid = () => new ApiError("배포 자료 목록의 페이지가 일치하지 않습니다. 목록을 다시 열어 주세요.", 0, "invalid_response");
    const tooMany = () => new ApiError(`배포 자료가 ${MAX_STUDENT_MATERIALS}개를 초과했습니다. 교수자에게 종료 자료 정리를 요청하세요.`,
      0, "material_catalog_limit");
    try {
      while (true) {
        checkSession();
        if (signal?.aborted) throw new RequestCancelledError();
        const endpoint = `/v1/materials${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`;
        const page = parseStudentMaterials(await this.authorizedRequest<unknown>(endpoint, {method: "GET"}, {signal}));
        checkSession();
        if (signal?.aborted) throw new RequestCancelledError();
        if (courseKey !== undefined && courseKey !== page.courseKey) throw invalid();
        courseKey = page.courseKey;
        if (materials.length + page.materials.length > MAX_STUDENT_MATERIALS) throw tooMany();
        for (const material of page.materials) {
          if (identifiers.has(material.id)) throw invalid();
          identifiers.add(material.id);
          materials.push(material);
        }
        if (page.nextCursor === null) return {courseKey, materials};
        if (!page.materials.length || cursors.has(page.nextCursor)) throw invalid();
        if (materials.length >= MAX_STUDENT_MATERIALS) throw tooMany();
        cursors.add(page.nextCursor);
        cursor = page.nextCursor;
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApiError("현재 서버는 IDE 자료 배포를 지원하지 않습니다. 학생 웹을 이용하거나 서버 업데이트를 요청하세요.",
          404, "materials_unsupported");
      }
      throw error;
    }
  }

  public async getStudentMaterialFile(material: StudentMaterial, signal?: AbortSignal): Promise<Uint8Array> {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(material.id) || !/^[a-f0-9]{64}$/.test(material.sha256) ||
        !["pdf", "zip"].includes(material.format) || !Number.isSafeInteger(material.sizeBytes) ||
        material.sizeBytes <= 0 || material.sizeBytes > MAX_STUDENT_MATERIAL_BYTES) {
      throw new ApiError("자료 파일 정보가 올바르지 않습니다. 목록을 새로고침하세요.", 0, "invalid_response");
    }
    const contentType = material.format === "pdf" ? "application/pdf" : "application/zip";
    const bytes = await this.authorizedBytesRequest(`/v1/materials/${encodeURIComponent(material.id)}/download`,
      {method: "GET", headers: {Accept: contentType, "X-Autograde-Expected-SHA256": material.sha256}},
      {timeoutMs: STARTER_REQUEST_TIMEOUT_MS, maxResponseBytes: MAX_STUDENT_MATERIAL_BYTES,
        acceptedContentTypes: [contentType], signal});
    verifyStudentMaterial(bytes, material);
    return bytes;
  }

  public async getAcceptedAssignmentCatalog(): Promise<AcceptedAssignmentEntry[]> {
    try {
      return parseAcceptedAssignments(await this.authorizedRequest<unknown>("/v1/accepted-assignments/catalog", { method: "GET" }));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApiError("서버가 수락 이력을 지원하지 않습니다. 서버 업데이트가 필요합니다. 현재 과제의 열기·다운로드·제출은 계속 사용할 수 있습니다.", 404, "server_upgrade_required");
      }
      throw error;
    }
  }

  public async getAssignmentDocument(assignmentId: string) {
    try {
      return parseAssignmentDocument(await this.authorizedRequest<unknown>(
        `/v1/assignments/${encodeURIComponent(assignmentId)}/document`, {method: "GET"},
      ), assignmentId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return undefined;
      throw error;
    }
  }

  public async getSubmissionHistory(assignmentId: string) {
    return parseSubmissionHistory(await this.authorizedRequest<unknown>(
      `/v1/assignments/${encodeURIComponent(assignmentId)}/history`, { method: "GET" },
    ), assignmentId);
  }

  public async getSubmissionSource(version: SubmissionVersion, signal?: AbortSignal): Promise<Uint8Array> {
    const bytes = await this.authorizedBytesRequest(
      `/v1/submissions/${encodeURIComponent(version.id)}/source`,
      { method: "GET", headers: { Accept: "application/gzip" } },
      { timeoutMs: STARTER_REQUEST_TIMEOUT_MS, maxResponseBytes: MAX_STARTER_RESPONSE_BYTES,
        acceptedContentTypes: ["application/gzip", "application/x-gzip", "application/octet-stream"], signal },
    );
    verifySubmissionSource(bytes, version);
    return bytes;
  }

  public async getStarter(
    assignmentId: string,
    starterUrl?: string,
    signal?: AbortSignal,
  ): Promise<Uint8Array> {
    const endpoint = validatedStarterEndpoint(
      this.transport.getBaseUrl(),
      assignmentId,
      starterUrl,
    );
    return this.authorizedBytesRequest(endpoint, {
      method: "GET",
      headers: { Accept: "application/gzip" },
    }, {
      timeoutMs: STARTER_REQUEST_TIMEOUT_MS,
      maxResponseBytes: MAX_STARTER_RESPONSE_BYTES,
      acceptedContentTypes: ["application/gzip", "application/x-gzip", "application/octet-stream"],
      signal,
    });
  }

  public async submit(
    assignmentId: string,
    githubRepositoryId: number,
    headSha: string,
    idempotencyKey: string,
    pullRequestNumber?: number,
  ): Promise<SubmissionSummary> {
    const checkSession = this.sessionGuard();
    const requestBody: Record<string, unknown> = {
      assignment_id: assignmentId,
      github_repository_id: githubRepositoryId,
      head_sha: headSha,
    };
    if (pullRequestNumber !== undefined) {
      requestBody.pull_request_number = pullRequestNumber;
    }
    for (let attempt = 0; attempt < SUBMISSION_MAX_ATTEMPTS; attempt += 1) {
      checkSession();
      try {
        const payload = await this.authorizedRequest<unknown>("/v1/submissions", {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(requestBody),
        }, { timeoutMs: SUBMISSION_REQUEST_TIMEOUT_MS });
        const submission = normalizeSubmission(payload);
        if (!submission) {
          throw new ApiError("서비스가 제출 식별자를 반환하지 않았습니다.", 0, "invalid_response");
        }
        return submission;
      } catch (error) {
        checkSession();
        if (!isRetryableApiError(error) || attempt + 1 >= SUBMISSION_MAX_ATTEMPTS) {
          throw error;
        }
        await this.retrySleep(retryDelayMs(error, attempt));
      }
    }
    throw new Error("unreachable submission retry state");
  }

  public async submitBundle(
    assignmentId: string,
    archive: Uint8Array,
    idempotencyKey: string,
  ): Promise<SubmissionSummary> {
    const checkSession = this.sessionGuard();
    const endpoint = `/v1/assignments/${encodeURIComponent(assignmentId)}/submissions`;
    const body = ownedArrayBuffer(archive);
    for (let attempt = 0; attempt < SUBMISSION_MAX_ATTEMPTS; attempt += 1) {
      checkSession();
      try {
        const payload = await this.authorizedRequest<unknown>(endpoint, {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/gzip",
            "Idempotency-Key": idempotencyKey,
          },
          body,
        }, { timeoutMs: SUBMISSION_REQUEST_TIMEOUT_MS });
        const submission = normalizeSubmission(payload);
        if (!submission) {
          throw new ApiError("서비스가 제출 식별자를 반환하지 않았습니다.", 0, "invalid_response");
        }
        return submission;
      } catch (error) {
        checkSession();
        if (!isRetryableApiError(error) || attempt + 1 >= SUBMISSION_MAX_ATTEMPTS) {
          throw error;
        }
        await this.retrySleep(retryDelayMs(error, attempt));
      }
    }
    throw new Error("unreachable bundle submission retry state");
  }

  public async getSubmission(submissionId: string, signal?: AbortSignal): Promise<SubmissionSummary> {
    const payload = await this.authorizedRequest<unknown>(
      `/v1/submissions/${encodeURIComponent(submissionId)}`,
      { method: "GET" },
      { signal },
    );
    const submission = normalizeSubmission(payload);
    if (!submission) {
      throw new ApiError("서비스가 제출 상태를 반환하지 않았습니다.", 0);
    }
    return submission;
  }

  public async getResult(submissionId: string, signal?: AbortSignal): Promise<GradeResult> {
    const payload = await this.authorizedRequest<unknown>(
      `/v1/submissions/${encodeURIComponent(submissionId)}/result`,
      { method: "GET" },
      { signal },
    );
    const result = normalizeGradeResult(payload);
    if (!result) {
      throw new ApiError("서비스가 채점 결과를 반환하지 않았습니다.", 0);
    }
    return result;
  }

  public async revokeCurrentSession(): Promise<void> {
    const checkSession = this.sessionGuard();
    await this.authorizedRequest<unknown>("/v1/sessions/current", { method: "DELETE" });
    checkSession();
    await this.tokens.clear();
  }

  private sessionGuard(): () => void {
    const origin = this.transport.getBaseUrl();
    const identity = this.tokens.getSessionGeneration?.();
    return () => {
      if (origin !== this.transport.getBaseUrl() || identity !== this.tokens.getSessionGeneration?.()) {
        throw new ApiError("Autograde 로그인 또는 서버가 변경되었습니다. 현재 과제에서 작업을 다시 실행하세요.", 401, "login_required");
      }
    };
  }

  private async authorizedRequest<T>(
    endpoint: string,
    init: RequestInit,
    options?: RequestOptions,
  ): Promise<T> {
    const expectedBaseUrl = this.transport.getBaseUrl();
    const checkSession = this.sessionGuard();
    let accessToken = await this.tokens.getAccessToken();
    checkSession();
    try {
      const response = await this.transport.request<T>(endpoint, init, accessToken, {
        ...options,
        expectedBaseUrl,
      });
      checkSession();
      return response;
    } catch (error) {
      checkSession();
      if (!(error instanceof ApiError) || error.status !== 401) {
        throw error;
      }
      if (this.transport.getBaseUrl() !== expectedBaseUrl) {
        throw new ApiError("Autograde 서비스 주소가 변경되었습니다. 다시 로그인하세요.", 401, "login_required");
      }
      accessToken = await this.tokens.getAccessToken(true);
      checkSession();
      const response = await this.transport.request<T>(endpoint, init, accessToken, {
        ...options,
        expectedBaseUrl,
      });
      checkSession();
      return response;
    }
  }

  private async authorizedBytesRequest(
    endpoint: string,
    init: RequestInit,
    options?: RequestOptions,
  ): Promise<Uint8Array> {
    const expectedBaseUrl = this.transport.getBaseUrl();
    const checkSession = this.sessionGuard();
    let accessToken = await this.tokens.getAccessToken();
    checkSession();
    try {
      const response = await this.transport.requestBytes(endpoint, init, accessToken, {
        ...options,
        expectedBaseUrl,
      });
      checkSession();
      return response;
    } catch (error) {
      checkSession();
      if (!(error instanceof ApiError) || error.status !== 401) {
        throw error;
      }
      if (this.transport.getBaseUrl() !== expectedBaseUrl) {
        throw new ApiError("Autograde 서비스 주소가 변경되었습니다. 다시 로그인하세요.", 401, "login_required");
      }
      accessToken = await this.tokens.getAccessToken(true);
      checkSession();
      const response = await this.transport.requestBytes(endpoint, init, accessToken, {
        ...options,
        expectedBaseUrl,
      });
      checkSession();
      return response;
    }
  }
}

function normalizeAssignmentClaim(payload: unknown): AssignmentClaim {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new ApiError("서비스가 유효하지 않은 수령 코드 교환 응답을 반환했습니다.", 0, "invalid_response");
  }
  const raw = payload as Record<string, unknown>;
  const assignmentId = raw.assignment_id;
  const courseKey = raw.course_key;
  const deliveryMode = raw.delivery_mode;
  const acceptanceId = raw.acceptance_id;
  if (
    typeof assignmentId !== "string" || !assignmentId ||
    typeof courseKey !== "string" || !courseKey ||
    typeof deliveryMode !== "string" || !deliveryMode ||
    typeof acceptanceId !== "string" || !acceptanceId
  ) {
    throw new ApiError("서비스가 유효하지 않은 수령 코드 교환 응답을 반환했습니다.", 0, "invalid_response");
  }
  return {
    assignmentId,
    courseKey,
    deliveryMode,
    acceptanceId,
  };
}

function validatedStarterEndpoint(
  serviceBaseUrl: string,
  assignmentId: string,
  advertisedUrl?: string,
): string {
  const endpoint = `/v1/assignments/${encodeURIComponent(assignmentId)}/starter`;
  if (!advertisedUrl) {
    return endpoint;
  }
  const expected = new URL(`${serviceBaseUrl}${endpoint}`);
  let advertised: URL;
  try {
    advertised = new URL(advertisedUrl, `${serviceBaseUrl}/`);
  } catch {
    throw new ApiError("서비스가 유효하지 않은 starter URL을 반환했습니다.", 0, "invalid_response");
  }
  if (
    advertised.origin !== expected.origin ||
    advertised.pathname !== expected.pathname ||
    advertised.username || advertised.password || advertised.hash
  ) {
    throw new ApiError("서비스가 허용되지 않은 starter URL을 반환했습니다.", 0, "invalid_response");
  }
  return `${endpoint}${advertised.search}`;
}

function ownedArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}
