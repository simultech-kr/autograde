import * as vscode from "vscode";
import { resultHtml } from "./resultSummary";
import type { Assignment, GradeResult } from "./types";

let panel: vscode.WebviewPanel | undefined;
export function clearResultPanel(): void {
  const previous = panel; panel = undefined; previous?.dispose();
}
export function showResultPanel(assignment: Assignment, result: GradeResult, receipt: string, context: string): void {
  if (!panel) {
    const current = vscode.window.createWebviewPanel("autograde.result", "Autograde 채점 결과", vscode.ViewColumn.Beside,
      { enableScripts: false, localResourceRoots: [], retainContextWhenHidden: false });
    panel = current;
    current.onDidDispose(() => { if (panel === current) panel = undefined; });
  }
  panel.webview.html = resultHtml(assignment, result, receipt, context);
  panel.reveal(vscode.ViewColumn.Beside, true);
}
