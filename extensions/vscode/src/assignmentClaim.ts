import * as os from "node:os";

import * as vscode from "vscode";

import { ApiError, AutogradeClient, RequestCancelledError } from "./api";
import {
  type DeviceTokenPollOptions,
  OperationCancelledError,
  pollForDeviceToken,
} from "./deviceAuth";
import { isInsecureHttpPilotUrl, normalizeClaimCode } from "./helpers";
import type { AssignmentClaim, DeviceAuthorization, TokenResponse } from "./types";

const MAX_CLAIM_CODE_INPUT_LENGTH = 256;

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
  public constructor(
    private readonly client: AutogradeClient,
    private readonly extensionVersion: string,
    private readonly onRedeemed: (assignmentId?: string) => Promise<void>,
    private readonly tokenPoller: DeviceTokenPoller = pollForDeviceToken,
  ) {}

  public async redeem(): Promise<void> {
    const serviceBaseUrl = this.client.transport.getBaseUrl();
    if (isInsecureHttpPilotUrl(serviceBaseUrl)) {
      throw new Error(
        "과제 수령 코드는 암호화되지 않은 사설망 HTTP 서버로 보낼 수 없습니다. HTTPS 서버 주소를 설정한 뒤 다시 시도하세요.",
      );
    }

    let entered: string | undefined = await vscode.window.showInputBox({
      title: "수령 코드로 과제 받기",
      prompt: "Autograde 웹사이트에서 받은 과제 수령 코드(과제 키)를 입력하세요. 수령 코드는 이 기기에 저장되지 않습니다.",
      placeHolder: "AK1-XXXX-XXXX-XXXX",
      password: true,
      ignoreFocusOut: true,
      validateInput: claimCodeValidationMessage,
    });
    if (entered === undefined) {
      return;
    }

    let ephemeralClaimCode: string | undefined = normalizeClaimCode(entered);
    if (!ephemeralClaimCode) {
      throw new Error(claimCodeValidationMessage(entered) ?? "올바른 수령 코드를 입력하세요.");
    }
    entered = undefined;

    const deviceName = [os.hostname(), vscode.env.remoteName ?? process.platform]
      .filter(Boolean)
      .join(" / ");
    let prepared: PreparedAssignmentClaim;
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
            const authorization = await this.client.createDeviceAuthorization(
              deviceName,
              this.extensionVersion,
              controller.signal,
            );
            validatePendingDeviceAuthorization(authorization);
            if (this.client.transport.getBaseUrl() !== serviceBaseUrl) {
              throw new Error("수령 코드 확인 중 Autograde 서비스 주소가 변경되었습니다. 다시 시도하세요.");
            }
            try {
              const claim = await this.client.redeemAssignmentClaim(
                ephemeralClaimCode as string,
                authorization.device_code,
                controller.signal,
              );
              return { authorization, claim, recoveringFromLostResponse: false };
            } catch (error) {
              if (isUncertainRedemptionResponse(error)) {
                return { authorization, recoveringFromLostResponse: true };
              }
              throw error;
            }
          } catch (error) {
            if (error instanceof RequestCancelledError) {
              throw new vscode.CancellationError();
            }
            throw error;
          } finally {
            subscription.dispose();
          }
        },
      );
    } finally {
      // Do not retain the claim secret after its single network request.
      ephemeralClaimCode = undefined;
    }

    const tokens = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: prepared.recoveringFromLostResponse
          ? "기기 승인 상태 복구 중"
          : "과제용 로그인 연결 중",
        cancellable: true,
      },
      async (progress, cancellationToken) => {
        try {
          return await this.tokenPoller({
            deviceCode: prepared.authorization.device_code,
            expiresInSeconds: prepared.authorization.expires_in,
            initialIntervalSeconds:
              prepared.authorization.poll_interval ?? prepared.authorization.interval ?? 1,
            cancellationToken,
            exchange: (deviceCode, signal) => this.client.exchangeDeviceCode(deviceCode, signal),
            onProgress: (remainingSeconds) => {
              progress.report({
                message: prepared.recoveringFromLostResponse
                  ? `승인 응답 복구 중 (${remainingSeconds}초)`
                  : `token 발급 대기 중 (${remainingSeconds}초)`,
              });
            },
          });
        } catch (error) {
          if (error instanceof OperationCancelledError || error instanceof RequestCancelledError) {
            throw new vscode.CancellationError();
          }
          if (prepared.recoveringFromLostResponse) {
            throw new Error(
              "서버 응답이 중간에 끊겨 기기 승인 복구를 완료하지 못했습니다. 수령 코드를 다시 입력해 보세요.",
            );
          }
          throw error;
        }
      },
    );

    await this.client.tokens.storeSession(tokens);
    if (prepared.recoveringFromLostResponse) {
      void vscode.window.showInformationMessage(
        "서버 응답이 끊겼지만 과제 승인과 로그인을 복구했습니다. 다운로드할 과제를 선택하세요.",
      );
    }
    await this.onRedeemed(prepared.claim?.assignmentId);
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
    throw new Error("서비스가 유효하지 않은 기기 연결 응답을 반환했습니다.");
  }
}

function isUncertainRedemptionResponse(error: unknown): boolean {
  return error instanceof ApiError && error.status === 0 && new Set([
    "network_error",
    "request_timeout",
    "invalid_response",
  ]).has(error.code ?? "");
}
