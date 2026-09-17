import { randomUUID } from "node:crypto";
import { ApiError, RequestCancelledError } from "./api";

export type DownloadStage = "preflight" | "requesting" | "receiving" | "verifying" | "installing" | "files_ready" | "opening";
export class DownloadFailure extends Error {
  constructor(public readonly code: string) { super(code); }
}
export const stageLabels: Record<DownloadStage, string> = {
  preflight: "저장 위치 확인", requesting: "서버 요청·파일 수신", receiving: "파일 수신",
  verifying: "파일 무결성 검증", installing: "파일 저장", files_ready: "파일 준비 완료", opening: "IDE 폴더 열기",
};
export function classifyDownloadError(error: unknown, stage: DownloadStage, cancelled = false): string {
  if (cancelled || error instanceof RequestCancelledError) return "AG-DL-USER-CANCELLED";
  if (stage === "opening") return "AG-DL-OPEN-WORKSPACE";
  if (error instanceof DownloadFailure) return error.code;
  if (error instanceof ApiError) {
    if (error.status === 401) return "AG-DL-AUTH-EXPIRED";
    if (error.status === 403) return "AG-DL-ACCESS-DENIED";
    if (error.status === 429) return "AG-DL-HTTP-RATE-LIMIT";
    if (error.status >= 500) return "AG-DL-HTTP-SERVER";
    if (error.code === "request_timeout") return "AG-DL-NETWORK-TIMEOUT";
    if (error.code === "invalid_response") return "AG-DL-RESPONSE-TYPE";
    if (error.code === "response_too_large") return "AG-DL-INTEGRITY-SIZE";
    if (error.code === "network_error") return "AG-DL-NETWORK-UNKNOWN";
  }
  const native = error && typeof error === "object" && "code" in error ? String(error.code) : "";
  const known: Record<string,string> = { EACCES:"LOCAL-PERMISSION", EPERM:"LOCAL-PERMISSION", ENOSPC:"LOCAL-SPACE",
    EEXIST:"LOCAL-EXISTS", ENAMETOOLONG:"LOCAL-PATH", ENOENT:"LOCAL-PATH", EIO:"LOCAL-IO" };
  return "AG-DL-" + (known[native] ?? "UNKNOWN");
}
export function downloadGuidance(code?: string): string {
  switch (code) {
    case "AG-DL-LOCAL-PERMISSION": return "이 위치에 저장할 권한이 없습니다. 쓰기 가능한 다른 폴더를 선택하세요. 다시 수락할 필요는 없습니다.";
    case "AG-DL-LOCAL-SPACE": return "디스크 여유 공간을 확보한 뒤 다시 받으세요. 기존 코드는 보존하세요.";
    case "AG-DL-LOCAL-EXISTS": return "같은 이름의 폴더가 있습니다. 기존 과제를 열거나 다른 위치를 선택하세요. 기존 코드는 덮어쓰지 않습니다.";
    case "AG-DL-AUTH-EXPIRED": return "로그인이 만료됐거나 바뀌었습니다. 새 수령 코드로 로그인하세요. 기존 파일은 남습니다.";
    case "AG-DL-OPEN-WORKSPACE": return "다운로드는 완료됐습니다. 재다운로드하지 말고 현재 수업 폴더의 과제 파일을 여세요.";
    case "AG-DL-USER-CANCELLED": return "작업을 취소했습니다. 필요할 때 다시 시도하세요.";
    case "AG-DL-INTEGRITY-HASH": case "AG-DL-INTEGRITY-SIZE": return "받은 파일과 과제 정보가 다릅니다. 과제 새로고침 후 재시도하거나 문의번호를 알려 주세요.";
    case "AG-DL-RESPONSE-TYPE": return "과제 파일 대신 다른 응답을 받았습니다. API 주소와 서버 프록시 설정을 확인하세요.";
    case "AG-DL-ACCESS-DENIED": return "교수자에게 수강 상태와 과제 접근 권한을 확인해 달라고 요청하세요.";
    case "AG-DL-NETWORK-TIMEOUT": case "AG-DL-NETWORK-UNKNOWN": return "API 주소와 네트워크 연결을 확인하고 다시 시도하세요. 인증서 검증을 해제하지 마세요.";
    case "AG-DL-HTTP-RATE-LIMIT": case "AG-DL-HTTP-SERVER": return "서버가 요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요.";
    case undefined: return "수락·파일 준비·IDE 열기는 별도 단계입니다.";
    default: return "실패 단계와 문의번호를 교수자에게 알려 주세요. 기존 코드는 덮어쓰지 마세요.";
  }
}
export class DownloadDiagnostic {
  readonly attemptId = randomUUID();
  stage: DownloadStage = "preflight";
  outcome = "in_progress";
  openOutcome = "not_attempted";
  code?: string;
  httpStatus?: number;
  delivery = "전달 전";
  private sequence = 0;
  fail(error: unknown, cancelled = false): void {
    this.code = classifyDownloadError(error, this.stage, cancelled);
    if (error instanceof ApiError && error.status >= 100 && error.status <= 599) this.httpStatus = error.status;
    if (this.stage === "opening") { this.outcome = "succeeded"; this.openOutcome = this.code === "AG-DL-USER-CANCELLED" ? "open_cancelled" : "open_failed"; }
    else this.outcome = this.code === "AG-DL-USER-CANCELLED" ? "cancelled" : "failed";
  }
  payload(version: string, platform: string, remote?: string): object {
    return { schema_version:1, attempt_id:this.attemptId, seq:this.sequence++, stage:this.stage,
      outcome:this.outcome, open_outcome:this.openOutcome, error_code:this.code, http_status:this.httpStatus,
      ide:"vscode", extension_version:version, os:platform === "win32" ? "windows" : platform === "darwin" ? "macos" : platform === "linux" ? "linux" : "other",
      remote_kind:!remote ? "none" : remote === "wsl" ? "wsl" : remote === "ssh-remote" ? "ssh" : "other" };
  }
  get summary(): string {
    return this.outcome === "succeeded" ? "파일 준비 완료" + (this.openOutcome === "open_failed" ? " · IDE 열기 실패" : "") : this.outcome === "cancelled" ? "다운로드 취소" : this.outcome === "failed" ? "다운로드 실패" : "다운로드 진행 중";
  }
  get text(): string {
    return `${this.summary}\n단계: ${stageLabels[this.stage]}\n오류 코드: ${this.code ?? "없음"}\n문의번호: ${this.attemptId}\nHTTP: ${this.httpStatus ?? "응답 없음/해당 없음"}\n진단: ${this.delivery}\n\n${downloadGuidance(this.code)}`;
  }
}
