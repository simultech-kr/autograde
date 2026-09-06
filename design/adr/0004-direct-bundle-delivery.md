# ADR 0004: GitHub 없는 직접 번들 배포·제출을 기본 MVP로 사용

- 상태: Accepted
- 날짜: 2026-09-02
- 개정: 2026-09-03 — local CSV와 `pilot-local`을 파일럿 경로로 채택하고 container는
  production 계획으로 이동

## 배경

수업의 기본 흐름은 교수자가 주차별 실습을 등록하고, 인증된 학생이 VS Code에서 내려받아
해결한 뒤 같은 Extension으로 제출하며, 로컬 Autograde 서버가 숨은 평가 입력을 주입해
채점하고 결과를 돌려주는 것이다. 이 흐름 자체에는 학생별 GitHub repository, GitHub OAuth,
GitHub App이 필수적이지 않다. 특히 20명 안팎의 한 강좌 MVP에서는 GitHub organization 권한,
repository provisioning, 학생 credential 문제를 동시에 운영하면 실패 지점이 불필요하게
늘어난다.

## 결정

직접 bundle delivery를 기본 MVP transport로 추가한다. 기존 Git exact-SHA 방식은 호환
모드로 유지한다.

1. 파일럿은 shell 환경변수 대신 local config CSV와 별도 roster CSV를 사용한다. 교수자는
   starter, assessment와 선택적 data 디렉터리를 course-wide assignment release로 등록한다.
2. 각 디렉터리는 manifest가 포함된 결정적 `tar.gz` bundle이 되며 전체 archive SHA-256으로
   content-addressed storage에 원자적으로 보관한다.
3. 학생은 기존 OAuth-free 활성화 코드/device authorization으로 로그인한다. 학생 계정에는
   GitHub identity가 없어도 된다.
4. Extension은 starter bundle을 받아 새 디렉터리에 안전하게 해제한다. Windows에서는 WSL2
   remote workspace를 사용하고 Linux/macOS에서는 native workspace를 사용한다.
5. 제출 시 Extension은 workspace의 일반 파일만 결정적 submission bundle로 만들고 bearer
   token과 idempotency key로 업로드한다. server가 식별한 학생·course·assignment만 권한의
   근거로 사용한다.
6. HTTP 성공 전에 bundle 검증, immutable CAS 보관, request와 grading-input receipt 기록을
   완료한다. worker는 기본 4개 thread로 bounded queue와 SQLite recovery scan을 공유한다.
7. 파일럿 grader는 submission, assessment, data를 분리된 workspace로 준비하고 trusted
   assessment를 host process에서 실행한다. 이 경로는 host/network 격리가 아니므로 합성·신뢰
   코드에만 사용한다. 결과는 기존 bounded public rubric/diagnostics contract로만 공개한다.
8. 교수자 dashboard는 별도 mode-0600 local token을 Basic-auth password로 사용하며 roster ×
   assignment의 다운로드·제출·상태·점수를 읽기 전용으로 제공한다.

## Bundle 경계

- kind는 `starter`, `submission`, `assessment`, `data` 중 하나다.
- absolute path, `..`, backslash, `.git`, `.autograde`, link/device/FIFO, 중복 경로와
  Unicode/casefold 충돌을 거부한다.
- manifest의 path, byte size, executable bit, file SHA-256과 archive payload를 모두 비교한다.
- server 기본 public upload 상한은 compressed 25 MiB, expanded 100 MiB, payload entry
  5,000개다. Extension은 이 상한 이하에서 bundle을 생성해야 한다.
- 접수 receipt에는 source archive digest/size와 assessment/data tree digest, grading runtime,
  rubric, 공개 정책을 고정한다. Production runtime은 runner image digest도 고정한다.

## 결과

장점은 GitHub 장애나 학생 Git 설정 없이도 주차별 배포→제출→채점→결과 조회를 한 로컬
서비스에서 시험할 수 있고, 한 과제 release를 학생별로 중복 등록하지 않아도 된다는 것이다.
25명 동시 제출은 bounded HTTP 요청과 4-worker queue로 처리한다.

대신 Git commit history와 PR review는 직접 bundle 제출의 provenance가 아니다. 제출 원본은
server receipt의 archive SHA-256이다. Git 기반 수업은 기존 exact-SHA 모드를 선택한다.
`pilot-local`은 학생 프로그램을 host 권한과 network 환경에서 실행할 수 있으므로 sandbox가
아니다. 이 파일럿은 합성·신뢰 코드의 non-confidential 기능 시험에만 사용하며 실제 학생 코드,
공식 성적이나 고위험 시험에는 No-Go다. 실제 배포에는 API와 worker를 분리한 disposable
Docker/Podman container 또는 microVM이 필요하고, confidential test는 학생 process에서
assessment/data가 보이지 않는 추가 경계를 요구한다.

PyJevSim은 기존 instructor repository의 주기 collection/synchronization에 계속 사용한다.
학생이 누른 제출은 즉시 응답해야 하므로 event-driven queue로 처리하며, DEVS transition 안에서
HTTP·DB·grading 실행을 수행하지 않는 ADR 0001의 경계를 유지한다.
