import * as os from "node:os";

import * as vscode from "vscode";

import { AutogradeClient } from "./api";
import {
  type DeviceTokenPollOptions,
  OperationCancelledError,
  pollForDeviceToken,
} from "./deviceAuth";
import { isInsecureHttpPilotUrl } from "./helpers";
import type { TokenResponse } from "./types";

const INSECURE_HTTP_CONTINUE_ACTION = "위험을 이해하고 계속";
type DeviceTokenPoller = (options: DeviceTokenPollOptions) => Promise<TokenResponse>;

export class AuthenticationController {
  public constructor(
    private readonly client: AutogradeClient,
    private readonly extensionVersion: string,
    private readonly onAuthenticationChanged: (authenticated: boolean) => void,
    private readonly tokenPoller: DeviceTokenPoller = pollForDeviceToken,
  ) {}

  public async signIn(): Promise<void> {
    const serviceBaseUrl = this.client.transport.getBaseUrl();
    if (isInsecureHttpPilotUrl(serviceBaseUrl)) {
      const proceed = await vscode.window.showWarningMessage(
        "암호화되지 않은 외부 파일럿 서버에 연결하려고 합니다.",
        {
          modal: true,
          detail:
            `접속할 서버: ${new URL(serviceBaseUrl).origin}\n` +
            "같은 네트워크의 다른 사람이 활성화 코드, token 또는 제출 내용을 보거나 바꿀 수 있습니다. " +
            "교수자가 관리하는 신뢰된 사설 LAN에서 진행하는 짧은 파일럿일 때만 계속하세요.",
        },
        INSECURE_HTTP_CONTINUE_ACTION,
      );
      if (proceed !== INSECURE_HTTP_CONTINUE_ACTION) {
        return;
      }
    }

    if (await this.client.tokens.hasSession()) {
      const replace = await vscode.window.showWarningMessage(
        "이 VS Code에는 이미 Autograde 로그인이 있습니다.",
        {
          modal: true,
          detail: "기존 서버 세션을 종료한 뒤 새 계정으로 다시 로그인합니다.",
        },
        "기존 세션 종료 후 다시 로그인",
      );
      if (replace !== "기존 세션 종료 후 다시 로그인") {
        return;
      }
      if (!(await this.endExistingSessionForReplacement())) {
        return;
      }
    }

    const serviceOrigin = new URL(serviceBaseUrl).origin;
    const deviceName = [os.hostname(), vscode.env.remoteName ?? process.platform]
      .filter(Boolean)
      .join(" / ");
    const authorization = await this.client.createDeviceAuthorization(
      deviceName,
      this.extensionVersion,
      undefined,
      serviceBaseUrl,
    );
    validateDeviceAuthorization(authorization);
    if (this.client.transport.getBaseUrl() !== serviceBaseUrl) {
      throw new Error("로그인 중 Autograde 서비스 주소가 변경되었습니다. 다시 시도하세요.");
    }
    validateVerificationUrl(authorization.verification_uri, serviceBaseUrl);
    if (authorization.verification_uri_complete !== undefined) {
      validateVerificationUrl(authorization.verification_uri_complete, serviceBaseUrl);
    }
    const verificationUrl = authorization.verification_uri_complete ?? authorization.verification_uri;
    const openBrowserAction = `${new URL(serviceBaseUrl).host}에서 로그인`;

    const action = await vscode.window.showInformationMessage(
      "Autograde 웹사이트에 학생 활성화 코드를 입력해 VS Code를 연결하세요.",
      {
        modal: true,
        detail: `접속할 서버: ${serviceOrigin}\n연결 코드: ${authorization.user_code}\n코드는 잠시 후 만료됩니다.`,
      },
      openBrowserAction,
      "코드 복사",
    );
    if (!action) {
      return;
    }
    if (action === "코드 복사") {
      await vscode.env.clipboard.writeText(authorization.user_code);
      const openAfterCopy = await vscode.window.showInformationMessage(
        "연결 코드를 복사했습니다. 표시된 Autograde 서버에서 로그인을 계속할까요?",
        {
          modal: true,
          detail: `접속할 서버: ${serviceOrigin}\n연결 코드: ${authorization.user_code}`,
        },
        openBrowserAction,
      );
      if (openAfterCopy !== openBrowserAction) {
        return;
      }
    } else if (action !== openBrowserAction) {
      return;
    }

    const opened = await vscode.env.openExternal(vscode.Uri.parse(verificationUrl));
    if (!opened) {
      throw new Error("인증 페이지를 열 수 없습니다.");
    }

    let tokens: TokenResponse;
    try {
      tokens = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `Autograde 연결 코드 ${authorization.user_code}`,
          cancellable: true,
        },
        async (progress, cancellationToken) => this.tokenPoller({
          deviceCode: authorization.device_code,
          expiresInSeconds: authorization.expires_in,
          initialIntervalSeconds: authorization.poll_interval ?? authorization.interval ?? 5,
          cancellationToken,
          exchange: (deviceCode, signal) => this.client.exchangeDeviceCode(
            deviceCode,
            signal,
            serviceBaseUrl,
          ),
          onProgress: (remainingSeconds) => {
            progress.report({ message: `승인 대기 중 (${remainingSeconds}초)` });
          },
        }),
      );
    } catch (error) {
      if (error instanceof OperationCancelledError) {
        throw new vscode.CancellationError();
      }
      throw error;
    }

    if (this.client.transport.getBaseUrl() !== serviceBaseUrl) {
      throw new Error("로그인 중 Autograde 서비스 주소가 변경되었습니다. 다시 시도하세요.");
    }
    await this.client.tokens.storeSession(tokens, serviceBaseUrl);
    this.onAuthenticationChanged(true);
    void vscode.window.showInformationMessage("Autograde에 연결되었습니다.");
  }

  public async signOut(): Promise<boolean> {
    if (!(await this.client.tokens.hasSession())) {
      this.onAuthenticationChanged(false);
      void vscode.window.showInformationMessage(
        "현재 Autograde 로그인 세션이 없습니다. 남아 있던 채점 화면을 지웠습니다.",
      );
      return true;
    }
    try {
      await this.client.revokeCurrentSession();
    } catch {
      const localOnly = await vscode.window.showWarningMessage(
        "서버에서 현재 세션을 종료할 수 없습니다.",
        {
          modal: true,
          detail: "이 기기의 token만 삭제할 수 있습니다. 서버 세션은 만료되거나 별도 장치 관리에서 폐기할 때까지 남을 수 있습니다.",
        },
        "이 기기에서만 로그아웃",
      );
      if (localOnly !== "이 기기에서만 로그아웃") {
        return false;
      }
      await this.client.tokens.clear();
      this.onAuthenticationChanged(false);
      void vscode.window.showWarningMessage(
        "이 기기의 로그인 정보와 채점 화면은 지웠지만 서버 세션 폐기는 확인하지 못했습니다. 공용 PC의 작업 파일은 자동 삭제되지 않습니다.",
      );
      return true;
    }
    this.onAuthenticationChanged(false);
    void vscode.window.showInformationMessage(
      "Autograde에서 로그아웃하고 채점 화면을 지웠습니다. 공용 PC의 작업 파일은 자동 삭제되지 않습니다.",
    );
    return true;
  }

  private async endExistingSessionForReplacement(): Promise<boolean> {
    try {
      await this.client.revokeCurrentSession();
      this.onAuthenticationChanged(false);
      return true;
    } catch {
      const reset = await vscode.window.showWarningMessage(
        "기존 서버 세션을 종료할 수 없습니다.",
        {
          modal: true,
          detail: "로컬 token을 삭제하고 다시 로그인할 수 있지만 기존 서버 세션은 남을 수 있습니다.",
        },
        "로컬 로그인 삭제 후 계속",
      );
      if (reset !== "로컬 로그인 삭제 후 계속") {
        return false;
      }
      await this.client.tokens.clear();
      this.onAuthenticationChanged(false);
      return true;
    }
  }
}

export function validateVerificationUrl(value: string, serviceBaseUrl: string): void {
  let parsed: URL;
  let service: URL;
  try {
    parsed = new URL(value);
    service = new URL(serviceBaseUrl);
  } catch {
    throw new Error("서비스가 올바르지 않은 인증 URL을 반환했습니다.");
  }
  const localHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
  const allowedHttp = parsed.protocol === "http:" && (
    localHosts.has(parsed.hostname.toLowerCase()) ||
    (isInsecureHttpPilotUrl(serviceBaseUrl) && isInsecureHttpPilotUrl(value))
  );
  if (
    parsed.username ||
    parsed.password ||
    (parsed.protocol !== "https:" && !allowedHttp) ||
    parsed.origin !== service.origin
  ) {
    throw new Error("인증 페이지 주소가 설정된 Autograde 서버와 일치하지 않습니다.");
  }
}

function validateDeviceAuthorization(value: {
  device_code: string;
  user_code: string;
  verification_uri: string;
  expires_in: number;
}): void {
  if (
    !value ||
    typeof value.device_code !== "string" ||
    !value.device_code ||
    typeof value.user_code !== "string" ||
    !value.user_code ||
    typeof value.verification_uri !== "string" ||
    !value.verification_uri ||
    typeof value.expires_in !== "number" ||
    value.expires_in <= 0
  ) {
    throw new Error("서비스가 유효하지 않은 device authorization을 반환했습니다.");
  }
}
