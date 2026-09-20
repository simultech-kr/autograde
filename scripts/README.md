# Scripts

- `check_instructor_identity.cjs`: 개인 교수자·관리자 합성 화면 24개 크기/테마 조합과 담당 분반·수업 생성 컨트롤 검증.
  [실행 안내](../docs/operations/instructor-personal-auth.md#검증).

- `check_instructor_rubric.cjs`: 실제 루브릭 기준 입력·검토·상세·목록·오류 복구의 합성 HTML을 40개 화면/테마 조합으로 검사.
  [실행 안내](../docs/operations/rubric-web-mvp.md#시험).

- `check_connected_rubric.cjs`: 실제 제출 평가·결과·이력·제출 선택의 합성 HTML을 32개 화면/테마 조합으로 검사. [실행 안내](../docs/operations/rubric-connected-assessment.md#검증-방법).

- `check_instructor_browser.cjs`: 실제 교수자 HTML/CSP로 미저장 경고·케이스 편집·검증 자동 조회의 정상/실패 상태를 검사.
  [실행 안내](../docs/operations/instructor-ux-update.md#시험과-남은-단계).

- `check_instructor_ux.cjs`: 실제 서버가 렌더링한 교수자 개요·과제·입력 복구의 합성 화면 검사.
  [실행 안내](../docs/operations/instructor-ux-update.md#시험과-남은-단계).

- `check_instructor_wireframe.cjs`: 서버 연결 없는 교수자 화면 시안의 반응형·테마·상호작용 검사.
  [시안 범위·실행 안내](../docs/testing/instructor-ux-wireframe.md).

- `check_submission_timeline.cjs`: 합성 제출 이력·코드 비교 화면의 반응형·테마 검사.
  [실행 안내](../docs/operations/submission-timeline.md#재현-가능한-시험).

개발 환경 준비, 검사, 데이터 마이그레이션, 운영 작업 등 반복 가능한 보조 스크립트를 둡니다. 스크립트는 가능하면 여러 번 실행해도 안전하도록 작성합니다.

- `check_instructor_responsive.cjs`: 합성 HTML을 사용한 관리자 결과 화면의 Playwright 반응형 검사.
  [준비·실행 방법](../docs/operations/instructor-responsive-results.md#자동-검증)을 참고하세요.
