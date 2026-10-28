import type { ClassroomStudent, ClassroomSubmission, ConnectionState, ClassroomCommand, CommandKind } from "../../lib/contracts/classroom.ts";

export const commandLabels: Record<CommandKind, string> = { notice: "공지 전달", open_assignment: "과제 열기", sync_materials: "자료 동기화", check_environment: "환경 점검", save_submit: "저장·제출" };
export function commandStatusLabel(command: ClassroomCommand) {
  return command.status === "expired" && command.accepted_at ? "만료 · 처리 결과 미확인" : ({ queued: "요청됨 · IDE 수신 대기", received: "IDE 수신 · 학생 확인 대기", accepted: "학생 수락 · 처리 중", completed: "처리 완료", declined: "학생 거절", failed: "처리 실패", expired: "요청 만료" })[command.status];
}
export function commandResultLabel(command: ClassroomCommand) {
  if (command.status === "expired" && command.accepted_at) return "수락 이후 결과가 도착하지 않았습니다. 이미 실행된 작업이 취소되었다는 뜻은 아닙니다.";
  const labels: Record<string, string> = { notice_acknowledged: "학생이 공지를 확인했습니다.", assignment_opened: "학생 IDE에서 과제를 열었습니다.", materials_synced: "강의자료 변경 파일 동기화를 완료했습니다.", environment_checked: "환경 점검 결과를 수신했습니다.", submission_received: "서버가 공식 제출을 접수했습니다. 채점 완료와는 다릅니다.", student_declined: "학생이 요청을 거절했습니다.", cancelled: "학생이 작업을 취소했습니다.", action_failed: "작업을 완료하지 못했습니다.", scope_changed: "학생 계정·서버·과제 연결이 변경되었습니다.", workspace_unavailable: "과제 폴더를 확인할 수 없습니다.", unsupported: "현재 IDE가 이 요청을 지원하지 않습니다.", submission_unconfirmed: "공식 제출 접수 여부를 확인하지 못했습니다." };
  return command.result_code ? labels[command.result_code] || (command.status === "expired" ? "요청 유효 시간이 지났습니다." : "처리 결과를 확인하세요.") : "";
}
export function checkLabel(key: string, value: string) {
  const keys: Record<string, string> = { server: "서버 연결", workspace: "과제 폴더", compiler: "컴파일러", os: "운영체제", remote: "원격 환경" };
  const values: Record<string, string> = { ok: "정상", failed: "확인 실패", missing: "없음", untrusted: "신뢰되지 않은 폴더", available: "사용 가능", not_checked: "점검하지 않음", macos: "macOS", linux: "Linux", windows: "Windows", other: "기타", none: "로컬", wsl: "WSL", "ssh-remote": "SSH" };
  return `${keys[key] || key}: ${values[value] || value}`;
}

export const connectionLabels: Record<ConnectionState, string> = { online: "IDE 연결", stale: "신호 지연", offline: "연결 끊김", never_connected: "연결 기록 없음" };
const activityLabels: Record<string, string> = {
  workspace_opened: "과제 열기", file_saved: "실습 파일 저장", submission_started: "제출 시작",
  submission_finished: "제출 응답 확인", assignment_downloaded: "과제 다운로드", material_downloaded: "자료 다운로드",
  content_synced: "강의자료 동기화", session_started: "IDE 접속", session_ended: "IDE 연결 종료", heartbeat: "연결 신호",
};
export const activityLabel = (kind?: string) => kind ? activityLabels[kind] || "수업 활동" : "활동 기록 없음";
export function submissionLabel(student: ClassroomStudent) {
  return student.submission.state === "assignment_required" ? "과제 선택 필요" : student.submission.state === "not_assigned" ? "배정 대상 아님" : student.submission.submitted ? "공식 제출 접수" : "공식 미제출";
}
export function submissionScore(submission: ClassroomSubmission | null) {
  if (!submission) return "제출 없음";
  if (["graded", "published"].includes(submission.state) && submission.score !== null && submission.max_score !== null) return `${submission.score} / ${submission.max_score}점`;
  if (["failed", "rejected", "infra_failed", "assessment_failed"].includes(submission.state)) return "처리 오류 · 이력 확인";
  return "접수 완료 · 점수 미확정";
}
export function displayTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) + " KST" : "기록 없음";
}
export function stableStudents(students: ClassroomStudent[]) { return [...students].sort((a, b) => a.student_key.localeCompare(b.student_key, "en")); }

export type AttentionFilter = "all" | "attention" | "connection" | "never_connected" | "grading_error" | "grading_wait" | "command";
export function attentionReasons(student: ClassroomStudent, serverTime: string): string[] {
  const reasons: string[] = [];
  if (["stale", "offline"].includes(student.connection.state)) reasons.push("connection");
  const latest = student.submission.latest;
  if (latest && ["failed", "rejected", "infra_failed", "assessment_failed"].includes(latest.state)) reasons.push("grading_error");
  const waited = latest ? Date.parse(serverTime) - Date.parse(latest.received_at) : NaN;
  if (latest && ["received", "accepted", "queued", "running", "verifying", "pinning"].includes(latest.state) && waited >= 10 * 60 * 1000) reasons.push("grading_wait");
  if (student.command_attention) reasons.push("command");
  return reasons;
}
export function needsAttention(student: ClassroomStudent, filter: AttentionFilter, serverTime: string): boolean {
  if (filter === "all") return true;
  if (filter === "never_connected") return student.connection.state === "never_connected";
  const reasons = attentionReasons(student, serverTime);
  return filter === "attention" ? reasons.length > 0 : reasons.includes(filter);
}
