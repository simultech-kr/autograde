# Documentation

- [교수자 웹 관리 MVP 사용 안내](operations/instructor-web-mvp.md): 설정 활성화 → 수업·학생 등록 → 6단계 과제 등록·검증·공개. 2026-09-16 구현, 운영 배포는 별도.

- [교수자 웹 관리 시험 기록](testing/instructor-web-mvp.md): HTTP·실제 컴파일·25명 동시 제출·마이그레이션과 미실시 환경 구분.

- [학생 전체 초기화 실제 실행 절차](operations/course-student-reset.md): 서버 중지 → 영향 확인 → 자동 백업·come2201 초기화 → CLI 재등록. 과제·다른 수업·파일 보존.

- [수업별 학생 초기화·재등록 설계](architecture/course-student-reset-mvp.md): 초기화 위험 작업·백업·영향 확인·재등록 UX. 웹 기능은 설계 단계, 별도 오프라인 도구 제공.

- [교수자 과제 자료 제출·검증 UX](architecture/instructor-assignment-validation-ux.md): 문제·파일·채점 자료 준비부터 검증 결과 수정과 공개까지 6단계 안내. 교수자 화면의 기준 설계.

- [과제 제출·검증 단계별 UX](architecture/submission-validation-ux.md): 학생 제출 5단계, 접수/채점 구분·오류 복구·공통 화면 계약. 교수자 화면은 별도 6단계 설계 참조.

- [수업·학생 등록 웹 MVP 설계](architecture/course-student-web-mvp.md): MVC·공통 디자인, 학기/분반별 수업, 학생 개별/CSV 등록, 비밀번호 초기화·수강 관리와 기존 데이터 보존. 구현 범위는 위 사용 안내 참조.

- [교수자 웹 과제 등록 MVP 계획](architecture/instructor-assignment-web-mvp.md): 예제/직접 과제 등록·검증·공개, 업로드 보안과 운영 인수 기준. 로컬 구현·시험과 실제 HTTPS 운영 승인은 구분.

- [옵저버·데코레이터 실습 문제](../examples/pattern-workshops/README.md): C++17 문제 → 학생 코드 작성 → 실행 검사 → 프롬프트 검토.

- [학생·교수자 화면과 수락 과제](operations/student-instructor-views.md): 학생 웹/교수자 웹 분리, VS Code 0.4.1의 수락 전용 목록과 서버 권한 검사.

- [일반 학생 제출 배포 준비](operations/student-deployment-preparation.md): 격리 staging 모드, readiness, 공개 전 필수 통과 조건. 아직 일반 학생 공개는 No-Go.

- [과제 등록·검증·공개와 마감 연장](operations/course-assignment-management.md): 2026-09-11부터 등록 기본값은 초안. 모범답안·오답 검사, 감사 기록, DB 업데이트 주의사항.

- [제출 기록·코드 복원 MVP](operations/submission-history-mvp.md): 두 확장의 서버 접수 원본 조회·새 폴더 복원·학생별 격리와 직접 시험 절차.

- [C/C++ Hello World 실습](../examples/hello-world/README.md): Linux/WSL2 서버 등록, Windows/VS2022 로컬 시험과 배점.
- [Visual Studio 2022/2026 확장](../extensions/visualstudio/README.md): Windows VSIX 빌드·설치·학생 흐름과 미검증 항목.
- [Visual Studio 예외 점검](operations/visualstudio-exception-review.md): 인증·네트워크·제출·파일 처리의 수정 사항과 남은 현장 시험.
- [독립 웹 파일럿 실행](operations/independent-web-pilot.md): 현재 권장 절차. 학생 웹 20010,
  VS Code API 20000, `come3105`·`come2201` 학생 등록부터 서버 실행까지.
- [독립 웹 인증 MVP 설계](architecture/independent-web-auth-mvp.md): 구현 구조와 보안·운영 경계.
- `requirements/mvp.md`: local CSV 파일럿의 전제, 기능·안전 요구사항과 성공 기준
- `requirements/student-platform.md`: WSL2, 학생별 주차 repository, Extension 제출,
  device authorization과 결과 제공 목표 요구사항
- `architecture/system.md`: direct-bundle 파일럿 component와 production 격리 목표
- `architecture/student-platform.md`: 웹 인증, Linux/macOS/WSL2 Extension, direct bundle 및
  Git repository 배포, submission/grading/result API 아키텍처
- `operations/security.md`: credential, sandbox, 장애와 backup 기준
- `operations/student-platform-mvp.md`: Git repository/container 기반 production 호환 모드
  참고 절차; local 파일럿에서는 사용하지 않음
- `operations/direct-bundle-mvp.md`: 환경변수·GitHub·Docker 없이 local CSV와 Extension으로
  배포·제출·`pilot-local` 채점·dashboard와 공용 좌석의 로그인별 새 코드·메모리 전용 token을
  시험하는 절차
- [operations/qr-assignment-claim-pilot.md](operations/qr-assignment-claim-pilot.md): 외부 HTTPS
  QR 페이지에서 학번과 숫자 6자리 Autograde 전용 비밀번호로 10분·1회용 수령 코드를
  발급하고 VS Code로 과제를 자동 다운로드하는 권장 20~25명 파일럿과 사용성 평가 절차
- [operations/trusted-lan-pilot.md](operations/trusted-lan-pilot.md): 동일한 신뢰 LAN의 외부
  시험 PC에서 암호화되지 않은 HTTP로 기능 흐름만 단기 검증하고 즉시 session/code와 firewall
  허용을 폐기하는 명시적 opt-in 절차
- [operations/observer-pattern-pilot.md](operations/observer-pattern-pilot.md): 이전 Java/C++ 비교 예제 보존용. 옵저버
  패턴 과제를 등록하고 행동 기반 rubric, 학생 제출, dashboard와 오류 시나리오까지 검증하는
  교수자용 파일럿 절차
- `operations/go-live-checklist.md`: 합성·신뢰 코드 local 파일럿의 Go/No-Go와 복구 drill

- [테마별 가독성·제출 코드 확인](architecture/theme-and-submission-review.md): 웹 테마 자동 전환,
  Visual Studio 테마 연동, 교수자의 확인 필요 제출과 읽기 전용 코드/이력 열람.
- [VS2022/2026 확장 0.5.0·자동 업데이트](operations/visualstudio-updates.md): 제출 UX·테마 변경,
  Windows VSIX 빌드와 Marketplace 업데이트 배포 절차.
- [제출 접수·채점 확인 분리와 확장 UI 검토](architecture/visualstudio-submission-ux.md):
  구현 범위, 오류 처리, 화면 개선 우선순위와 Windows 현장 시험.
- [학생 채점 결과·교과목·분반 관리자](operations/results-and-course-admin.md):
  두 IDE 결과 요약 화면, 수정 필요 항목, 관리자 접속·분반 등록·집계 의미·인수 시험.

`requirements/mvp.md`와 `operations/independent-web-pilot.md`가 현재 파일럿 기준입니다.
`operations/qr-assignment-claim-pilot.md`는 기존 단일 교과목 실행 모드의 참고 문서입니다.
`direct-bundle-mvp.md`의 활성화 코드 흐름은 loopback·호환 시험 기준입니다. `student-platform`
문서는 구현된 제출 vertical slice와 production용 repository/container 목표를 함께 설명합니다.
