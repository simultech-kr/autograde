import {submissionState, type CourseData, type Receipt} from "./model.ts";

export type ScoreRow = {assignment_id: string; title: string; assignment_key: string; receipt?: Receipt; missingIsUnknown: boolean};

/** Bound to the already authenticated course projection; no student selector. */
export function scoreRows(overview: CourseData["overview"]): ScoreRow[] {
  const latest = new Map(overview.latest.map(row => [row.assignment_id, row]));
  const rows: ScoreRow[] = overview.assignments.map(assignment => ({
    assignment_id: assignment.assignment_id, title: assignment.title, assignment_key: assignment.assignment_key,
    receipt: latest.get(assignment.assignment_id), missingIsUnknown: overview.latest_truncated && !latest.has(assignment.assignment_id),
  }));
  const seen = new Set(rows.map(row => row.assignment_id));
  for (const receipt of overview.latest) if (!seen.has(receipt.assignment_id)) {
    rows.push({assignment_id: receipt.assignment_id, title: receipt.title, assignment_key: receipt.assignment_key, receipt, missingIsUnknown: false});
    seen.add(receipt.assignment_id);
  }
  return rows;
}

export function scoreRowState(row: ScoreRow) {
  return row.missingIsUnknown ? {scored: false, tone: "info", stage: -1, label: "제출 기록 추가 확인 필요",
    hint: "조회 범위를 초과하여 제출 여부를 확정할 수 없습니다. IDE에서 해당 과제의 제출 이력을 확인하세요."} : submissionState(row.receipt);
}
