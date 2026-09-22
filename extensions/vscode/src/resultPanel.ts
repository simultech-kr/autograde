import * as vscode from "vscode";
import { resultHtml } from "./resultSummary";
import type { Assignment, GradeResult } from "./types";

let panel: vscode.WebviewPanel | undefined;
let refresh: (() => Promise<void>) | undefined;
let pause: (() => void) | undefined;
let close: (() => void) | undefined;
export interface ResultPanelActions {
  readonly refresh?: () => Promise<void>;
  readonly pause?: () => void;
  readonly onClose?: () => void;
  readonly notice?: string;
  readonly reveal?: boolean;
}
export async function refreshDisplayedResult(): Promise<void> { await refresh?.(); }
export function pauseDisplayedResult(): void { pause?.(); }
export function clearResultPanel(): void {
  const onClose = close;
  refresh = undefined; pause = undefined; close = undefined;
  onClose?.();
  const previous = panel; panel = undefined; previous?.dispose();
}
export function showResultPanel(assignment: Assignment, result: GradeResult, receipt: string, context: string, actions: ResultPanelActions = {}): void {
  refresh = actions.refresh; pause = actions.pause; close = actions.onClose;
  if (!panel) {
    const current = vscode.window.createWebviewPanel("autograde.result", "Autograde 채점 결과", vscode.ViewColumn.Beside,
      { enableScripts: false, enableCommandUris: ["autograde.refreshDisplayedResult", "autograde.pauseDisplayedResult"],
        localResourceRoots: [], retainContextWhenHidden: false });
    panel = current;
    current.onDidDispose(() => { if (panel === current) clearResultPanel(); });
  }
  panel.webview.html = resultHtml(assignment, result, receipt, context, {
    canRefresh: Boolean(refresh), canPause: Boolean(pause), notice: actions.notice,
  });
  if (actions.reveal !== false) panel.reveal(vscode.ViewColumn.Beside, true);
}
