import {submissionState, type CourseData, type Receipt} from "./model.ts";

export type ScoreRow = {assignment_id: string; title: string; assignment_key: string; due_at?:string|null; receipt?: Receipt; missingIsUnknown: boolean};

/** Bound to the already authenticated course projection; no student selector. */
export function scoreRows(overview: CourseData["overview"]): ScoreRow[] {
  const latest = new Map(overview.latest.map(row => [row.assignment_id, row]));
  const rows: ScoreRow[] = overview.assignments.map(assignment => ({
    assignment_id: assignment.assignment_id, title: assignment.title, assignment_key: assignment.assignment_key, due_at:assignment.due_at,
    receipt: latest.get(assignment.assignment_id), missingIsUnknown: overview.latest_truncated && !latest.has(assignment.assignment_id),
  }));
  const seen = new Set(rows.map(row => row.assignment_id));
  for (const receipt of overview.latest) if (!seen.has(receipt.assignment_id)) {
    rows.push({assignment_id: receipt.assignment_id, title: receipt.title, assignment_key: receipt.assignment_key, receipt, missingIsUnknown: false});
    seen.add(receipt.assignment_id);
  }
  return rows;
}

export function scoreRowState(row: ScoreRow, now = Date.now()) {
  return row.missingIsUnknown ? {scored: false, tone: "info", stage: -1, label: "제출 기록 추가 확인 필요",
    hint: "조회 범위를 초과하여 제출 여부를 확정할 수 없습니다. IDE에서 해당 과제의 제출 이력을 확인하세요."} : !row.receipt && row.due_at && Date.parse(row.due_at) <= now
    ? {scored:false,tone:"warning",stage:-1,label:"미제출 · 마감됨",hint:"제출 마감이 지났습니다. 추가 제출이 필요한 경우 교수자에게 문의하세요."} : submissionState(row.receipt);
}

export function visibleCourseAssignments(overview: CourseData["overview"], filter: string, now: number) {
  const latest = new Map(overview.latest.map(receipt => [receipt.assignment_id, receipt]));
  return overview.assignments.map(assignment => {
    const receipt = latest.get(assignment.assignment_id);
    const state = scoreRowState({...assignment, receipt, missingIsUnknown: overview.latest_truncated && !receipt}, now);
    return {assignment, receipt, state};
  }).filter(({receipt, state}) => {
    switch (filter) {
      case "open": return !receipt && !overview.latest_truncated;
      case "pending": return Boolean(receipt) && state.tone === "info";
      case "attention": return state.tone === "warning" || state.tone === "error";
      case "completed": return state.scored;
      default: return true;
    }
  });
}
