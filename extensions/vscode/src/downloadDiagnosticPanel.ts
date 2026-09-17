import * as vscode from "vscode";
import type { DownloadDiagnostic } from "./downloadDiagnostic";
let panel: vscode.WebviewPanel | undefined;
let latest: DownloadDiagnostic | undefined;
export function clearDownloadDiagnostic(): void { latest = undefined; const old = panel; panel = undefined; old?.dispose(); }
export function rememberDownloadDiagnostic(value: DownloadDiagnostic): void { latest = value; }
export async function showDownloadDiagnostic(): Promise<void> {
  if (!latest) { void vscode.window.showInformationMessage("이번 로그인에서 기록한 다운로드 진단이 없습니다."); return; }
  const diagnostic = latest;
  if (!panel) {
    const created = vscode.window.createWebviewPanel("autograde.downloadDiagnostic", "Autograde 다운로드 진단", vscode.ViewColumn.Beside,
      { enableScripts:false, localResourceRoots:[], retainContextWhenHidden:false });
    panel = created; created.onDidDispose(() => { if (panel === created) panel = undefined; });
  }
  const escaped = diagnostic.text.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]!));
  panel.webview.html = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);background:var(--vscode-editor-background);padding:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}</style></head><body><h2>다운로드 진단</h2><pre>${escaped}</pre><p>이 화면은 해당 시도의 기록입니다. 로그아웃하면 지워집니다.</p></body></html>`;
  panel.reveal(vscode.ViewColumn.Beside, true);
  const selected = await vscode.window.showInformationMessage("문의번호와 정제된 진단 정보를 복사할 수 있습니다.", "진단 정보 복사");
  if (selected && latest === diagnostic) await vscode.env.clipboard.writeText(diagnostic.text);
}
