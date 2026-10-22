import type { Bootstrap, Results, ResultRow } from "../src/lib/contracts/instructor.ts";

// Explicitly synthetic examples, used only by tests and the /app/preview wireframe.
const meta = { contract_version: "1" as const, request_id: "synthetic-preview", server_time: "2026-10-10T04:50:00Z" };
export const sampleBootstrap: Bootstrap = { ...meta,
  principal: { id: "synthetic-instructor", display_name: "시안 교수자", mode: "personal" }, csrf: "synthetic-not-a-token",
  courses: [{ course_key: "come2201", name: "소프트웨어 설계", code: "COME2201", section: "01분반", status: "active" }], capabilities: ["results.read"] };
const base: ResultRow = {
  row_key: "come2201:sample-001:observer", student_key: "SAMPLE-001", student_name: "가상 학생 1",
  assignment_id: "observer", assignment_key: "Week04-Problem01", release_id: "synthetic-release", title: "Observer · Decorator 패턴 구현",
  assignment_ready: true, submission_id: "sample-submission-2", received_at: "2026-10-10T04:48:00Z", state: "graded", status_label: "채점 완료",
  score: 0, max_score: 100, previous_best: { submission_id: "sample-submission-1", received_at: "2026-10-10T04:30:00Z", score: 100, max_score: 100 },
  submission_count: 2, detail_url: null, attention: true,
};
export const sampleResults: Results = { ...meta, course_key: "come2201", version: "a".repeat(64), page: 1, page_size: 20, total: 4,
  summary: { scope: "course", active_students: 4, submitted_students: 3, submitted: 3, unsubmitted: 1, pending: 1, completed: 1, attention: 2, submission_attempts: 4 },
  assignments: [{ assignment_id: "observer", assignment_key: "Week04-Problem01", release_id: "synthetic-release", title: base.title, ready: true, students: 4, submitted: 3, unsubmitted: 1 }],
  rows: [base,
    { ...base, row_key: "come2201:sample-002:observer", student_key: "SAMPLE-002", student_name: "가상 학생 2", state: "queued", status_label: "채점 대기", score: null, max_score: null, previous_best: null, submission_count: 1, attention: false },
    { ...base, row_key: "come2201:sample-003:observer", student_key: "SAMPLE-003", student_name: "가상 학생 3", state: "failed", status_label: "컴파일 오류", score: null, max_score: null, previous_best: null, attention: true, submission_count: 1 },
    { ...base, row_key: "come2201:sample-004:observer", student_key: "SAMPLE-004", student_name: "가상 학생 4", state: "unsubmitted", status_label: "미제출", score: null, max_score: null, previous_best: null, submission_id: null, received_at: null, submission_count: 0, attention: false },
  ] };
