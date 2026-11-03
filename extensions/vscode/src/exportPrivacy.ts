/** A per-operation notice; never remembered as research consent or a preference. */
export const EXPORT_PRIVACY_TITLE = "내보낸 파일은 공용 PC에 남아 개인정보가 노출될 수 있습니다.";
export const EXPORT_PRIVACY_CONTINUE = "위험을 확인하고 내보내기";
export const EXPORT_PRIVACY_DETAIL = "내보낸 폴더·ZIP·PDF에는 학번, 이름, 작성 코드, 점수 또는 개인 배포 자료가 포함될 수 있습니다. " +
  "이 파일은 IDE를 종료하거나 로그인 정보와 실습 공간을 정리해도 삭제되지 않으며, 같은 PC를 사용하는 다른 사람이 열어볼 수 있습니다.\n\n" +
  "본인 USB 또는 접근이 제한된 암호화 저장소를 선택하세요. 공용 PC의 바탕화면·다운로드 폴더에 남기지 말고, 가져간 파일과 남은 복사본을 직접 확인하세요. " +
  "기존 파일을 자동으로 덮어쓰거나 삭제하지 않습니다. 이 확인은 연구·수업 개선 목적의 개인정보 활용 동의와 별개입니다.";

interface NoticeWindow {
  showWarningMessage(message: string, options: {modal: boolean; detail: string}, ...items: string[]): PromiseLike<string | undefined>;
}

export async function confirmExportPrivacy(window: NoticeWindow): Promise<boolean> {
  const decision = await window.showWarningMessage(EXPORT_PRIVACY_TITLE,
    {modal: true, detail: EXPORT_PRIVACY_DETAIL}, EXPORT_PRIVACY_CONTINUE, "취소");
  return decision === EXPORT_PRIVACY_CONTINUE;
}
