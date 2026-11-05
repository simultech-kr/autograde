import { sharedSessionCleanupNotice, type SharedSessionCleanupMode } from "./sharedLearningSession";

/** Explicit failure handling for the supported shared-session exit command.
 * A process kill/window close still follows the launcher's mandatory cleanup policy.
 */
export async function resolveUnsavedSharedExit(options: {
  cleanupMode?: SharedSessionCleanupMode;
  evidence(): string;
  retry(): Promise<boolean>;
  exportWork(): Promise<void>;
  choose(message: string, detail: string, ...choices: string[]): Promise<string | undefined>;
}): Promise<boolean> {
  for (;;) {
    const action = await options.choose("최신 작업의 서버 저장을 확인하지 못했습니다.",
      `${options.evidence()}\n${sharedSessionCleanupNotice(options.cleanupMode)} 서버에 없는 변경은 정리 후 복원할 수 없습니다. 비정상 종료 후에도 다음 실행에서 남은 파일을 정리합니다. 내보내기를 마치고 저장된 파일을 확인한 뒤 다시 종료하세요.`,
      "서버 저장 재시도", "먼저 내보내기", "미저장 변경 폐기 선택");
    if (action === "서버 저장 재시도") { if (await options.retry()) return true; continue; }
    if (action === "먼저 내보내기") { await options.exportWork(); return false; }
    if (action !== "미저장 변경 폐기 선택") return false;
    const confirmation = await options.choose(options.cleanupMode === "next_start"
      ? "다음 IDE 실행 시 서버에 없는 변경을 복구할 수 없게 삭제합니다." : "서버에 없는 변경을 복구할 수 없게 삭제합니다.",
      `${options.evidence()}\n${sharedSessionCleanupNotice(options.cleanupMode)} 이 선택은 서버 저장이나 내보내기가 아닙니다. 미저장 실습 코드를 폐기하고 이 자리를 다음 학생에게 넘길 때만 선택하세요.`,
      "미저장 변경을 폐기하고 종료");
    return confirmation === "미저장 변경을 폐기하고 종료";
  }
}
