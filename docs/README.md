# Documentation

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
- [operations/observer-pattern-pilot.md](operations/observer-pattern-pilot.md): Java/C++ 옵저버
  패턴 과제를 등록하고 행동 기반 rubric, 학생 제출, dashboard와 오류 시나리오까지 검증하는
  교수자용 파일럿 절차
- `operations/go-live-checklist.md`: 합성·신뢰 코드 local 파일럿의 Go/No-Go와 복구 drill

`requirements/mvp.md`와 `qr-assignment-claim-pilot.md`가 현재 권장 파일럿 기준입니다.
`direct-bundle-mvp.md`의 활성화 코드 흐름은 loopback·호환 시험 기준입니다. `student-platform`
문서는 구현된 제출 vertical slice와 production용 repository/container 목표를 함께 설명합니다.
