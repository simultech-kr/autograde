import * as vscode from "vscode";

import { isAssignmentDownloadable, isBundleAssignment } from "./helpers";
import type { Assignment } from "./types";

export class AssignmentTreeItem extends vscode.TreeItem {
  public constructor(public readonly assignment: Assignment) {
    super(assignment.title, vscode.TreeItemCollapsibleState.Collapsed);
    this.id = assignment.id;
    this.description = describeAssignment(assignment);
    this.tooltip = buildTooltip(assignment);
    this.contextValue = assignment.latestSubmission
      ? "autograde.assignmentWithSubmission"
      : "autograde.assignment";
    this.iconPath = new vscode.ThemeIcon(iconForStatus(assignment.latestSubmission?.state ?? assignment.status));
  }
}

class DetailTreeItem extends vscode.TreeItem {
  public constructor(label: string, description?: string, icon?: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    if (icon) {
      this.iconPath = new vscode.ThemeIcon(icon);
    }
  }
}

export class AssignmentsTreeProvider implements vscode.TreeDataProvider<AssignmentTreeItem | DetailTreeItem> {
  private readonly changed = new vscode.EventEmitter<AssignmentTreeItem | DetailTreeItem | undefined>();
  public readonly onDidChangeTreeData = this.changed.event;
  private assignments: readonly Assignment[] = [];

  public setAssignments(assignments: readonly Assignment[]): void {
    this.assignments = assignments;
    this.changed.fire(undefined);
  }

  public clear(): void {
    this.setAssignments([]);
  }

  public getAssignments(): readonly Assignment[] {
    return this.assignments;
  }

  public getTreeItem(element: AssignmentTreeItem | DetailTreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(element?: AssignmentTreeItem | DetailTreeItem): Array<AssignmentTreeItem | DetailTreeItem> {
    if (!element) {
      return this.assignments.map((assignment) => new AssignmentTreeItem(assignment));
    }
    if (!(element instanceof AssignmentTreeItem)) {
      return [];
    }

    const assignment = element.assignment;
    const details: DetailTreeItem[] = [];
    if (isAssignmentDownloadable(assignment)) {
      const downloadItem = new DetailTreeItem(
        "과제 파일 다운로드",
        "클릭하여 현재 수업 폴더에 받기",
        "cloud-download",
      );
      downloadItem.command = {
        command: "autograde.cloneAssignment",
        title: "과제 파일 다운로드",
        arguments: [element],
      };
      details.push(downloadItem);
    }
    if (assignment.courseLabel) {
      details.push(new DetailTreeItem("수업", assignment.courseLabel, "book"));
    }
    if (assignment.dueAt) {
      details.push(new DetailTreeItem("마감", formatDate(assignment.dueAt), "clock"));
    }
    if (assignment.repository?.fullName) {
      details.push(new DetailTreeItem("Repository", assignment.repository.fullName, "repo"));
    } else if (isBundleAssignment(assignment)) {
      details.push(new DetailTreeItem("배포 방식", "서버 starter bundle", "cloud-download"));
    }
    if (assignment.latestSubmission) {
      const submission = assignment.latestSubmission;
      const score = formatScore(submission.score, submission.maxScore);
      details.push(new DetailTreeItem("최근 제출", score ? `${submission.state} · ${score}` : submission.state, "history"));
      const resultItem = new DetailTreeItem("채점 결과 보기", undefined, "output");
      resultItem.command = {
        command: "autograde.viewLatestResult",
        title: "View Latest Result",
        arguments: [element],
      };
      details.push(resultItem);
    }
    return details;
  }
}

function describeAssignment(assignment: Assignment): string | undefined {
  const submission = assignment.latestSubmission;
  if (submission) {
    return formatScore(submission.score, submission.maxScore) ?? submission.state;
  }
  return assignment.status;
}

function buildTooltip(assignment: Assignment): vscode.MarkdownString {
  const lines = [`**${assignment.title}**`];
  if (assignment.courseLabel) {
    lines.push(`수업: ${assignment.courseLabel}`);
  }
  if (assignment.dueAt) {
    lines.push(`마감: ${formatDate(assignment.dueAt)}`);
  }
  if (assignment.repository?.fullName) {
    lines.push(`Repository: ${assignment.repository.fullName}`);
  } else if (isBundleAssignment(assignment)) {
    lines.push("배포: 서버 starter bundle");
  }
  return new vscode.MarkdownString(lines.join("  \n"));
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatScore(score?: number, maxScore?: number): string | undefined {
  return score === undefined ? undefined : maxScore === undefined ? String(score) : `${score} / ${maxScore}`;
}

function iconForStatus(status?: string): string {
  switch (status?.toLowerCase()) {
    case "published":
    case "graded":
      return "pass-filled";
    case "running":
    case "queued":
    case "accepted":
    case "pinning":
    case "verifying":
    case "received":
      return "loading~spin";
    case "rejected":
    case "infra_failed":
    case "assessment_failed":
      return "error";
    default:
      return "notebook";
  }
}
