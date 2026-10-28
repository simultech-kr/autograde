import * as os from "node:os";

import * as vscode from "vscode";

import { ApiError, AutogradeClient, RequestCancelledError } from "./api";
import { AssignmentWorkspaceError, assignmentWorkspaceErrorMessage } from "./assignmentWorkspace";
import {
  type DeviceTokenPollOptions,
  OperationCancelledError,
  pollForDeviceToken,
} from "./deviceAuth";
import { isInsecureHttpPilotUrl, normalizeClaimCode } from "./helpers";
import type { AssignmentClaim, DeviceAuthorization, TokenResponse } from "./types";

const MAX_CLAIM_CODE_INPUT_LENGTH = 256;
type ClaimStage = "device" | "redeem" | "session" | "assignment";
const CLAIM_STAGE_LABELS: Record<ClaimStage, string> = {
  device: "기기 연결 준비 (/v1/device-authorizations)",
  redeem: "수령 코드 확인 (/v1/assignment-claims/redeem)",
  session: "로그인 연결 (/v1/device-authorizations/token)",
  assignment: "수락 목록 확인 · 과제 다운로드",
};
// Never echo arbitrary server strings, even if they look like an error-code identifier.
const SAFE_CLAIM_ERROR_CODES = new Set([
  "internal_error", "service_unavailable", "not_found", "invalid_request", "invalid_response",
  "network_error", "request_timeout", "response_too_large", "rate_limit_exceeded", "rate_limited",
  "too_many_requests", "login_required", "invalid_grant", "access_denied", "expired_token",
  "authorization_pending", "slow_down", "assignment_claim_denied", "assignment_claim_unavailable",
  "invalid_claim_code", "insecure_claim_redemption", "server_upgrade_required", "permission_denied",
]);

export { normalizeClaimCode } from "./helpers";

type DeviceTokenPoller = (options: DeviceTokenPollOptions) => Promise<TokenResponse>;

interface PreparedAssignmentClaim {
  readonly authorization: DeviceAuthorization;
  readonly claim?: AssignmentClaim;
  readonly recoveringFromLostResponse: boolean;
}

/**
 * Handles one-shot assignment claim-code entry and session bootstrap UI.
 *
 * Claim codes never become controller fields. The normalized code exists in
 * one local variable only until the redemption request settles, then that
 * reference is cleared before the resulting session is installed.
 */
export class AssignmentClaimController {
  private redeemInProgress = false;

  public constructor(
    private readonly client: AutogradeClient,
    private readonly extensionVersion: string,
    private readonly onRedeemed: (assignmentId?: string) => Promise<void>,
    private readonly tokenPoller: DeviceTokenPoller = pollForDeviceToken,
  ) {}

  public async redeem(entered?: string): Promise<boolean> {
    if (this.redeemInProgress) {
      void vscode.window.showInformationMessage("과제 수령을 이미 진행하고 있습니다. 현재 수령 코드 입력창 또는 진행 알림을 먼저 확인하세요.");
      return false;
    }
    this.redeemInProgress = true;
    try {
      const operation = this.redeemOnce(entered);
      entered = undefined;
      return await operation;
    } finally {
      this.redeemInProgress = false;
    }
  }

  private async redeemOnce(entered?: string): Promise<boolean> {
    const serviceBaseUrl = this.client.transport.getBaseUrl();
    let sessionGeneration = this.client.tokens.getSessionGeneration?.();
    const ensureCurrentSession = (): void => {
      if (this.client.transport.getBaseUrl() !== serviceBaseUrl ||
        this.client.tokens.getSessionGeneration?.() !== sessionGeneration) {
        throw new ApiError("authentication context changed", 401, "login_required");
      }
    };
    if (isInsecureHttpPilotUrl(serviceBaseUrl)) {
      throw new Error(
        "과제 수령 코드는 암호화되지 않은 사설망 HTTP 서버로 보낼 수 없습니다. HTTPS 서버 주소를 설정한 뒤 다시 시도하세요.",
      );
    }

    if (entered === undefined) entered = await vscode.window.showInputBox({
      title: "수령 코드로 과제 받기",
      prompt:
        `접속할 서버: ${new URL(serviceBaseUrl).origin}\n` +
        "학생 포털에서 받은 과제 수령 코드를 입력하세요. 웹 비밀번호는 입력하지 않습니다.",
      placeHolder: "AK1-XXXX-XXXX-XXXX",
      password: true,
      ignoreFocusOut: true,
      validateInput: claimCodeValidationMessage,
    });
    if (entered === undefined) {
      return false;
    }

    const validation = claimCodeValidationMessage(entered);
    if (validation) { entered = undefined; throw new Error(validation); }
    let ephemeralClaimCode: string | undefined = normalizeClaimCode(entered);
    if (!ephemeralClaimCode) {
      throw new Error(claimCodeValidationMessage(entered) ?? "올바른 수령 코드를 입력하세요.");
    }
    entered = undefined;

    const deviceName = [os.hostname(), vscode.env.remoteName ?? process.platform]
      .filter(Boolean)
      .join(" / ");
    let prepared: PreparedAssignmentClaim;
    let stage: ClaimStage = "device";
    try {
      prepared = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "수령 코드 확인 및 기기 연결 준비 중",
          cancellable: true,
        },
        async (_progress, cancellationToken) => {
          const controller = new AbortController();
          const subscription = cancellationToken.onCancellationRequested(() => controller.abort());
          if (cancellationToken.isCancellationRequested) {
            controller.abort();
          }
          try {
            throwIfClaimCancelled(cancellationToken);
            ensureCurrentSession();
            const authorization = await this.client.createDeviceAuthorization(
              deviceName,
              this.extensionVersion,
              controller.signal,
              serviceBaseUrl,
              ephemeralClaimCode,
            );
            throwIfClaimCancelled(cancellationToken);
            ensureCurrentSession();
            validatePendingDeviceAuthorization(authorization);
            stage = "redeem";
            try {
              const claim = await this.client.redeemAssignmentClaim(
                ephemeralClaimCode as string,
                authorization.device_code,
                controller.signal,
                serviceBaseUrl,
              );
              throwIfClaimCancelled(cancellationToken);
              ensureCurrentSession();
              return { authorization, claim, recoveringFromLostResponse: false };
            } catch (error) {
              throwIfClaimCancelled(cancellationToken);
              ensureCurrentSession();
              if (isUncertainRedemptionResponse(error)) {
                return { authorization, recoveringFromLostResponse: true };
              }
              throw error;
            }
          } finally {
            subscription.dispose();
          }
        },
      );
    } catch (error) {
      throw claimStageError(stage, error);
    } finally {
      // Do not retain the claim secret after its single network request.
      ephemeralClaimCode = undefined;
    }

    try {
      ensureCurrentSession();
      const tokens = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: prepared.recoveringFromLostResponse
            ? "기기 승인 상태 복구 중"
            : "과제용 로그인 연결 중",
          cancellable: true,
        },
        async (progress, cancellationToken) => {
          throwIfClaimCancelled(cancellationToken);
          const tokenResponse = await this.tokenPoller({
            deviceCode: prepared.authorization.device_code,
            expiresInSeconds: prepared.authorization.expires_in,
            initialIntervalSeconds:
              prepared.authorization.poll_interval ?? prepared.authorization.interval ?? 1,
            cancellationToken,
            exchange: (deviceCode, signal) => {
              ensureCurrentSession();
              return this.client.exchangeDeviceCode(deviceCode, signal, serviceBaseUrl);
            },
            onProgress: (remainingSeconds) => {
              progress.report({
                message: prepared.recoveringFromLostResponse
                  ? `승인 응답 복구 중 (${remainingSeconds}초)`
                  : `token 발급 대기 중 (${remainingSeconds}초)`,
              });
            },
          });
          throwIfClaimCancelled(cancellationToken);
          return tokenResponse;
        },
      );

      ensureCurrentSession();
      // TokenManager replaces credentials and advances identity synchronously. Capture only
      // that own transition before yielding, then reject logout/replacement during the await.
      const installation = this.client.tokens.storeSession(tokens, serviceBaseUrl);
      sessionGeneration = this.client.tokens.getSessionGeneration?.();
      await installation;
      ensureCurrentSession();
    } catch (error) {
      throw claimStageError("session", error, prepared.recoveringFromLostResponse);
    }
    if (prepared.recoveringFromLostResponse) {
      void vscode.window.showInformationMessage(
        "서버 응답이 끊겼지만 과제 승인과 로그인을 복구했습니다. 다운로드할 과제를 선택하세요.",
      );
    }
    try {
      ensureCurrentSession();
      await this.onRedeemed(prepared.claim?.assignmentId);
    } catch (error) {
      throw claimStageError("assignment", error);
    }
    return true;
  }
}

export function claimCodeValidationMessage(value: string): string | undefined {
  if (!value.trim()) {
    return "수령 코드를 입력하세요.";
  }
  if (value.length > MAX_CLAIM_CODE_INPUT_LENGTH) {
    return "수령 코드가 너무 깁니다.";
  }
  if (!normalizeClaimCode(value)) {
    return "수령 코드는 AK1-XXXX-XXXX-XXXX 형식입니다. 0, 1, I, L, O, U는 사용하지 않습니다.";
  }
  return undefined;
}

function validatePendingDeviceAuthorization(value: DeviceAuthorization): void {
  if (
    !value ||
    typeof value.device_code !== "string" || !value.device_code ||
    typeof value.expires_in !== "number" || !Number.isFinite(value.expires_in) || value.expires_in <= 0 ||
    (value.poll_interval !== undefined && (
      typeof value.poll_interval !== "number" ||
      !Number.isFinite(value.poll_interval) ||
      value.poll_interval <= 0
    )) ||
    (value.interval !== undefined && (
      typeof value.interval !== "number" ||
      !Number.isFinite(value.interval) ||
      value.interval <= 0
    ))
  ) {
    throw new ApiError("invalid device authorization response", 0, "invalid_response");
  }
}

function isUncertainRedemptionResponse(error: unknown): boolean {
  return error instanceof ApiError && error.status === 0 && new Set([
    "network_error",
    "request_timeout",
    "invalid_response",
  ]).has(error.code ?? "");
}

function throwIfClaimCancelled(token: { readonly isCancellationRequested: boolean }): void {
  if (token.isCancellationRequested) throw new vscode.CancellationError();
}

function claimStageError(stage: ClaimStage, error: unknown, recovering = false): Error {
  if (error instanceof vscode.CancellationError || error instanceof RequestCancelledError || error instanceof OperationCancelledError) {
    return new vscode.CancellationError();
  }
  if (stage === "assignment" && error instanceof AssignmentWorkspaceError) {
    // Typed local failures have a fixed code-to-guidance map; never trust their mutable message.
    return new Error(`과제 연결 중단 · 기존 과제 폴더 확인\n${assignmentWorkspaceErrorMessage(error.code)}`);
  }
  const apiError = error instanceof ApiError;
  const status = apiError && Number.isInteger(error.status) && error.status >= 100 && error.status <= 599 ? error.status : 0;
  const code = apiError && error.code && SAFE_CLAIM_ERROR_CODES.has(error.code) ? error.code : "unclassified_error";
  const technical = apiError
    ? `\n${status ? `HTTP ${status}` : "HTTP 응답 없음/확인 불가"} · 오류 코드: ${code}`
    : "";
  const actions: Record<ClaimStage, string> = {
    device: status === 401 || code === "invalid_grant" || code === "invalid_claim_code" || code === "expired_token" || code === "assignment_claim_denied"
      ? "이 수령 코드로 연결할 수 없습니다. 학생 웹에서 새 코드를 발급받아 다시 입력하세요. 반복되면 교수자에게 교과목·과제 공개 상태와 서버 로그 확인을 요청하세요."
      : "서버 연결 확인 후 다시 시도하세요. 반복되면 교수자에게 기기 연결 준비 단계의 서버 로그 확인을 요청하세요.",
    redeem: "학생 웹에서 해당 과제의 새 수령 코드를 받아 다시 시도하세요. 계속 실패하면 교수자에게 과제 공개·수강 상태를 확인해 달라고 요청하세요.",
    session: "수령 코드가 이미 사용되었을 수 있습니다. 학생 웹에서 새 코드를 받아 다시 연결하세요. 반복되면 교수자에게 로그인 연결 단계의 서버 로그 확인을 요청하세요.",
    assignment: status === 401 || code === "login_required"
      ? "수령 코드 확인 후 로그인이 만료되거나 변경되었습니다. 학생 웹에서 새 코드를 받아 다시 연결하세요. 기존 과제 파일은 삭제하지 마세요."
      : "수령 코드 확인은 완료되었습니다. ‘과제 새로고침’ 후 수락 목록에서 ‘과제 파일 다운로드’를 다시 실행하세요. 기존 과제 파일은 삭제하지 마세요.",
  };
  const recovery = recovering ? "\n서버 응답이 끊긴 뒤 기기 승인 복구를 완료하지 못했습니다." : "";
  const message = `과제 수령 중단 · ${CLAIM_STAGE_LABELS[stage]}${technical}${recovery}\n${actions[stage]}`;
  // No raw message, payload, cause, device label, student identity or credential is retained.
  return apiError ? new ApiError(message, status, code) : new Error(message);
}
