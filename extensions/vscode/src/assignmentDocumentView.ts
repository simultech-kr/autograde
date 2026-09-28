import { randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { AssignmentDocumentState, documentText } from "./assignmentDocument";
import type { Assignment, AssignmentDocument } from "./types";

export const ASSIGNMENT_DOCUMENT_SCHEME = "autograde-assignment-document";
const CLEARED = "이 과제 설명의 표시가 종료되었습니다. 현재 수락한 과제에서 설명 보기를 다시 선택하세요.";

export class AssignmentDocumentView implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  public readonly onDidChange = this.changed.event;
  private readonly contents = new Map<string, {uri: vscode.Uri; text: string}>();

  public constructor(
    private readonly state: AssignmentDocumentState,
    private readonly fetchDocument: (assignmentId: string) => Promise<AssignmentDocument | undefined>,
    private readonly isAccepted: (assignmentId: string) => boolean,
    private readonly notifyRead: () => void,
  ) {}

  public provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString())?.text ?? CLEARED;
  }

  public select(assignmentId: string | undefined): void {
    if (this.state.select(assignmentId)) this.redact();
  }

  public reconcileAccepted(): void {
    const selected = this.state.selected();
    if (selected && !this.isAccepted(selected)) this.select(undefined);
  }

  public async show(assignment: Assignment): Promise<void> {
    if (!this.isAccepted(assignment.id)) return;
    this.select(assignment.id);
    const scope = this.state.begin(assignment.id);
    let document: AssignmentDocument | undefined;
    try { document = await this.fetchDocument(assignment.id); }
    catch (error) { if (scope.isCurrent() && this.isAccepted(assignment.id)) throw error; else return; }
    if (!scope.isCurrent() || !this.isAccepted(assignment.id)) return;
    if (!document) {
      void vscode.window.showInformationMessage("현재 서버 또는 과제에서 설명 보기를 제공하지 않습니다. 과제를 새로고침하거나 다운로드한 README를 확인하세요.");
      return;
    }
    const uri = vscode.Uri.from({scheme: ASSIGNMENT_DOCUMENT_SCHEME, path: `/${randomUUID()}/과제-설명-v${document.revision}.txt`});
    this.redact();
    this.contents.set(uri.toString(), {uri, text: documentText(assignment.title, document)});
    try {
      const opened = await vscode.workspace.openTextDocument(uri);
      if (!scope.isCurrent() || !this.isAccepted(assignment.id)) { this.remove(uri); return; }
      // Plain text deliberately avoids Markdown previews, command links and HTML.
      const plain = await vscode.languages.setTextDocumentLanguage(opened, "plaintext");
      if (!scope.isCurrent() || !this.isAccepted(assignment.id)) { this.remove(uri); return; }
      await vscode.window.showTextDocument(plain, {preview: true, preserveFocus: false, viewColumn: vscode.ViewColumn.Beside});
      if (this.state.markRead(document, scope)) this.notifyRead();
      else this.remove(uri);
    } catch (error) {
      this.remove(uri);
      if (scope.isCurrent()) throw error;
    }
  }

  private redact(): void {
    const uris = [...this.contents.values()].map(item => item.uri);
    this.contents.clear();
    for (const uri of uris) this.changed.fire(uri);
  }

  private remove(uri: vscode.Uri): void {
    if (this.contents.delete(uri.toString())) this.changed.fire(uri);
  }

  public clear(): void { this.state.clear(); this.redact(); }
  public dispose(): void { this.clear(); this.changed.dispose(); }
}
