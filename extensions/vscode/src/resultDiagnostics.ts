import * as path from "node:path";
import * as vscode from "vscode";
import { resolveAssignmentDiagnosticPath } from "./helpers";
import type { GradeResult } from "./types";

/** A receipt's source location, captured before submission; never the currently focused editor. */
export interface ResultDiagnosticTarget {
  readonly rootUri: string;
  readonly assignmentPath?: string;
}

export function createResultDiagnosticTarget(root: vscode.Uri, assignmentPath?: string): ResultDiagnosticTarget | undefined {
  if (root.scheme !== "file" && root.scheme !== "vscode-remote") return undefined;
  return Object.freeze({ rootUri: root.toString(), assignmentPath });
}

/** Session-only bounded lookup. A restarted client must not guess which local copy was submitted. */
export function rememberResultDiagnosticTarget(
  bindings: Map<string, ResultDiagnosticTarget>, receipt: string, target: ResultDiagnosticTarget | undefined,
): void {
  if (!target) return;
  bindings.delete(receipt);
  bindings.set(receipt, target);
  while (bindings.size > 100) bindings.delete(bindings.keys().next().value!);
}

export function publishResultDiagnostics(
  collection: vscode.DiagnosticCollection,
  result: GradeResult,
  target: ResultDiagnosticTarget | undefined,
  isCurrent: () => boolean,
): void {
  if (!isCurrent()) return;
  if (result.state !== "published" || !target) {
    collection.clear();
    return;
  }
  const root = vscode.Uri.parse(target.rootUri);
  if (root.scheme !== "file" && root.scheme !== "vscode-remote") {
    collection.clear();
    return;
  }
  const grouped = new Map<string, { uri: vscode.Uri; diagnostics: vscode.Diagnostic[] }>();
  for (const item of result.diagnostics) {
    if (!item.path) continue;
    const absolute = resolveAssignmentDiagnosticPath(root.fsPath, target.assignmentPath, item.path);
    if (!absolute) continue;
    // Preserve WSL/SSH authority instead of turning a remote path into a local file URI.
    const uri = vscode.Uri.joinPath(root, ...path.relative(root.fsPath, absolute).split(path.sep));
    const startLine = Math.max(0, (item.line ?? 1) - 1);
    const startColumn = Math.max(0, (item.column ?? 1) - 1);
    const endLine = Math.max(startLine, (item.endLine ?? item.line ?? 1) - 1);
    const endColumn = Math.max(startColumn + 1, (item.endColumn ?? item.column ?? 1));
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(startLine, startColumn, endLine, endColumn), item.message, diagnosticSeverity(item.severity),
    );
    diagnostic.source = "Autograde";
    const key = uri.toString();
    const group = grouped.get(key) ?? { uri, diagnostics: [] };
    group.diagnostics.push(diagnostic);
    grouped.set(key, group);
  }
  // A replaced result/session may not clear or overwrite the newer receipt's diagnostics.
  if (!isCurrent()) return;
  collection.clear();
  collection.set([...grouped.values()].map(group => [group.uri, group.diagnostics]));
}

function diagnosticSeverity(value?: string): vscode.DiagnosticSeverity {
  switch (value?.toLowerCase()) {
    case "error": return vscode.DiagnosticSeverity.Error;
    case "warning": case "warn": return vscode.DiagnosticSeverity.Warning;
    case "hint": return vscode.DiagnosticSeverity.Hint;
    default: return vscode.DiagnosticSeverity.Information;
  }
}
