# Design

- [루브릭 평가 설계](../docs/architecture/rubric-evaluation-plugin.md), [C++ 옵저버 루브릭 예시](rubrics/observer.rubric.example.json): 설계용이며 현행 평가기에 바로 등록할 수 없음.
- [SPL 사업화 설계](../docs/architecture/software-product-line.md), [초기 기능/제품 template 카탈로그 예시](ses/product-line.catalog.example.json): 실행/가격/사용권은 미확정.

- [전체 기능 플러그인·SES 설계](../docs/architecture/platform-plugin-ses.md) 및 [성취도 평가 설계](../docs/architecture/learning-achievement-plugin.md).
- [플랫폼 구성 설계 예시](ses/platform-composition.example.json): come2201/come3105의 목표 조합. **실행 불가**, 기존 관리 SES CLI·pilot CSV와 다른 설계용 schema.

- [교수자 UI/UX 단계 0 시안](prototypes/instructor-ux-wireframe.html): 수업·과제·학생 이력과 6단계 등록의 상호작용 미리보기. 가상 데이터 전용, 서버 미연결.
  [검토 순서와 자동 검사](../docs/testing/instructor-ux-wireframe.md).

- [과제 다운로드 troubleshooting](../docs/architecture/download-troubleshooting.md): 수락/파일 준비/IDE 열기 분리, 클라이언트 진단과 서버 상태 원장·교수자 화면 설계.

- [관리 기능과 SES 기반 사용자 정의](../docs/architecture/management-ses.md): 보관·보고서·학생 지원 모듈과 SES/MB/PES 경계.
- [SES 구조 모델](ses/management.ses.json), [구성 예시·실행 방법](../examples/management/README.md): 읽기 전용 검증·pruning 구현, 운영 부작용 없음.

- [수업별 학생 초기화·재등록 설계](../docs/architecture/course-student-reset-mvp.md): 위험 작업 UX, 수업 범위 원자성·인증 폐기·자료 보존·백업 복구.

- [교수자 과제 자료 제출·검증 UX](../docs/architecture/instructor-assignment-validation-ux.md): 준비 파일·서버 검사·오류 수정·학생 공개를 안내하는 교수자 전용 6단계.

- [과제 제출·검증 단계별 UX](../docs/architecture/submission-validation-ux.md): 두 확장의 제출 안내, 교수자 검증/공개와 상태별 다음 행동·사용성 시험.

- [수업·학생 등록 웹 MVP 설계](../docs/architecture/course-student-web-mvp.md): MVC + Service, 공통 디자인 시스템, 동적 수업 레지스트리와 학생·수강 정보 분리.

- [교수자 웹 과제 등록 MVP 계획](../docs/architecture/instructor-assignment-web-mvp.md): CLI 없는 교수자 등록 흐름, 공통 서비스·비동기 검증·안전한 업로드와 운영 배포 계획.
- [구현된 교수자 웹 관리 사용 안내](../docs/operations/instructor-web-mvp.md): 수업·학생 등록, 과제 등록 6단계, 설정 활성화와 파일럿 제약.

- [독립 웹 인증 MVP 설계](../docs/architecture/independent-web-auth-mvp.md): `come3105`,
  `come2201` 교과목 선택, 학생별 비밀번호 확인과 과제 수령 코드 발급의 구현 설계
- `adr/0001-pyjevsim-scheduler.md`: PyJevSim을 workflow clock으로 사용하는 결정
- `adr/0002-fetch-exact-sha.md`: pull 대신 bare fetch와 exact-SHA snapshot을 사용하는 결정
- `adr/0003-wsl-extension-device-authorization.md`: WSL workspace Extension, OAuth-free
  browser/device authorization과 exact-SHA 제출 경계
- `adr/0004-direct-bundle-delivery.md`: GitHub 없는 course-wide bundle 배포·제출과 local CSV,
  신뢰 코드 전용 `pilot-local`을 파일럿으로 사용하고 container를 production 계획에 두는 결정
- `adr/0005-shared-seat-ephemeral-auth.md`: 공용·순환 좌석에서 URL만 지속하고 token은 메모리
  전용으로 두며 로그인별 새 코드, 4시간 절대 session과 최신 로그인 교체를 적용하는 결정
- `diagrams/`: 후속 상세 diagram
- `prototypes/`: 폐기 가능한 설계 실험
