import * as vscode from "vscode";

import type { AutogradeClient } from "./api";
import { normalizeServiceAddressInput } from "./helpers";

const DEFAULT_SERVICE_ADDRESS = "http://127.0.0.1:20000";
const CHANGE_ADDRESS_ACTION = "로그아웃하고 주소 변경";

/** Prompts for, validates, and machine-locally stores one service origin. */
export class ServiceAddressController {
  public constructor(
    private readonly client: AutogradeClient,
    private readonly endCurrentSession: () => Promise<boolean>,
    private readonly onAddressChanged: () => Promise<void>,
  ) {}

  public async configure(): Promise<void> {
    const configuration = vscode.workspace.getConfiguration("autograde");
    const currentValue = configuration.get<string>("serviceBaseUrl", DEFAULT_SERVICE_ADDRESS);
    const allowInsecureHttpPilot = configuration.get<boolean>("allowInsecureHttpPilot", false);
    const entered = await vscode.window.showInputBox({
      title: "Autograde 서버 주소 설정",
      prompt:
        "HTTPS 주소 또는 IP와 port를 입력하세요. scheme을 생략하면 HTTPS를 사용하며 IPv6 port는 [주소]:port 형식입니다.",
      placeHolder: "203.0.113.10:20000",
      value: currentValue,
      ignoreFocusOut: true,
      validateInput: (value) => serviceAddressValidationMessage(value, allowInsecureHttpPilot),
    });
    if (entered === undefined) {
      return;
    }

    const normalized = normalizeServiceAddressInput(entered, allowInsecureHttpPilot);
    let currentNormalized: string | undefined;
    try {
      currentNormalized = normalizeServiceAddressInput(currentValue, allowInsecureHttpPilot);
    } catch {
      // An invalid value entered through Settings must remain repairable here.
    }
    if (normalized === currentNormalized) {
      void vscode.window.showInformationMessage(`Autograde 서버 주소가 이미 ${normalized}(으)로 설정되어 있습니다.`);
      return;
    }

    if (await this.client.tokens.hasSession()) {
      const action = await vscode.window.showWarningMessage(
        "서버 주소를 변경하면 현재 Autograde 로그인이 종료됩니다.",
        {
          modal: true,
          detail:
            `현재 서버: ${currentNormalized ?? currentValue}\n새 서버: ${normalized}\n` +
            "기존 서버 세션을 먼저 폐기한 뒤 주소를 변경합니다.",
        },
        CHANGE_ADDRESS_ACTION,
      );
      if (action !== CHANGE_ADDRESS_ACTION) {
        return;
      }
      if (!(await this.endCurrentSession())) {
        return;
      }
    }

    // Clear before updating configuration so an old token cannot race to the
    // newly selected origin. No credential or claim code is persisted here.
    await this.client.tokens.clear();
    await configuration.update(
      "serviceBaseUrl",
      normalized,
      vscode.ConfigurationTarget.Global,
    );
    await this.onAddressChanged();
    void vscode.window.showInformationMessage(
      `Autograde 서버 주소를 ${normalized}(으)로 설정했습니다.`,
    );
  }
}

export function serviceAddressValidationMessage(
  value: string,
  allowInsecureHttpPilot = false,
): string | undefined {
  try {
    normalizeServiceAddressInput(value, allowInsecureHttpPilot);
    return undefined;
  } catch (error) {
    return error instanceof Error ? error.message : "Autograde 서비스 주소가 올바르지 않습니다.";
  }
}
