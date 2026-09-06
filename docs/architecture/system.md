# 시스템 아키텍처

## 파일럿 실행 구조

파일럿의 입력은 환경변수가 아니라 명시적으로 선택한 pilot config CSV와 roster CSV입니다.
학생 source transport는 Git이 아닌 direct bundle입니다.

```text
pilot config CSV ─┐
                  ├──> instructor CLI ──────> SQLite + private local artifacts
roster CSV ───────┘             │
                                ├── starter/assessment/data bundle 등록
                                ├── 교과목/학생 상태와 전용 비밀번호 관리
                                └── 과제별 공개 QR 생성

학생 browser ── HTTPS QR page ──> password 확인 ──> 10분·1회용 수령 코드
                                                        │
VS Code Extension ── HTTP(S) (*) ───> Auth + Assignment API
       │                                      │
       ├── URL 설정 지속/token은 메모리 전용   ├── 최신 로그인 1-session
       ├── starter download                   ├── immutable receipt
       └── source bundle upload               └── bounded worker queue
                                                     │
                                                     v
                                      WorkspaceBuilder
                               submission / assessment / data
                                                     │
                                                     v
                                        pilot-local process grader
                                                     │
                                  SQLite result <────┘
                                      │          │
                             Extension result   Instructor dashboard
                                                (loopback/TLS profile)
```

(*) 기본값은 같은 장비의 loopback입니다. 외부 QR 파일럿은 built-in server의 loopback bind를
유지하고 앞단 HTTPS reverse proxy를 사용합니다. 별도 신뢰 LAN 시험에서는 server와 Extension이
각각 명시적으로 opt-in한 동안에만 실제 RFC 1918 interface의 단기 HTTP 접속을 허용하지만
비밀번호 기반 수령은 거부합니다.

`pilot-local process grader`는 구조상의 adapter 경계일 뿐 보안 sandbox가 아닙니다. API, SQLite,
assessment와 학생 프로그램이 같은 host 권한·network 환경에 있을 수 있습니다. 이 구조는 합성
제출물과 신뢰된 참여자를 이용한 기능 검증에만 사용합니다.

## 구성과 roster 경계

Pilot config는 course key, data root, public URL, listen address, port, network 접근 mode,
grading runtime과 worker 수를 하나의 `key,value` CSV로 고정합니다. 모든 운영 명령이 같은
파일을 명시하므로 terminal별 환경변수 drift를 피합니다. Parser는 상태 변경 전에 schema,
duplicate, type, URL, bind와 runtime 조합을 검증해야 합니다. Network 접근 mode의 기본값은
`disabled`이고 이때 HTTP는 loopback에만 bind합니다.

Roster는 별도 UTF-8 CSV입니다. 파일럿의 필수 identity는 `student_key`이며 `active`가 course
API admission을 결정하고 `password`가 초기 학생별 ASCII 숫자 6자리 credential을 제공합니다.
CLI는 전체 파일을 검증한 뒤 password를 hash해 SQLite에 넣고 원문을 출력하지 않습니다.
Config에는 학생 레코드를 넣지 않고 roster에는 server token, dashboard credential이나 채점
설정을 넣지 않습니다. 비밀번호 원문 때문에 roster 전체는 교수자 전용 credential이며 실제
학생 파일은 repository 밖 또는 ignore된 local path에 둡니다. CSV는 설정의 source이며
SQLite는 실행 중 인증·제출 원장입니다.

## Bundle과 상태 일관성

Starter, assessment, data와 submission은 canonical manifest를 포함한 deterministic `tar.gz`로
검증하고 SHA-256 content-addressed storage에 보관합니다. Student projection과 starter에는
assessment/data를 포함하지 않습니다. Server는 Extension과 독립적으로 path/type, Unicode와
case collision, size, entry count, content digest와 executable bit를 재검증합니다.

접수 성공 전에 source digest와 immutable receipt가 SQLite에 기록됩니다. Idempotency 범위는
server-side student, endpoint와 assignment이며 같은 body retry는 기존 receipt에 합쳐지고 같은
key의 다른 body는 거부됩니다. Worker 상태는 `accepted -> queued -> running -> graded ->
published`이며 infrastructure와 assessment failure를 틀린 답의 0점과 구분합니다.

WorkspaceBuilder는 submission, assessment와 data를 서로 다른 디렉터리로 materialize하고 각
digest를 검증합니다. 이 분리는 입력 provenance와 이전 실행 오염을 막기 위한 논리 경계입니다.
Local process가 다른 tree나 host를 읽지 못하게 하는 보안 경계는 아닙니다.

## 인증과 API 경계

권장 흐름에서 학생은 과제의 비밀 없는 QR로 HTTPS page에 접속하고, 학번과 교과목별
Autograde 전용 비밀번호를 확인해 10분·1회용 수령 코드를 받습니다. Extension은 새 pending
device authorization과 이 코드를 원자적으로 결합해 과제를 수락하고, 발급된 session을 해당
과제에만 제한합니다. 기존 교수자 발급 활성화 코드는 호환 login 경로로 남습니다. Extension은
서비스 URL만 machine scope 설정에 유지하고 수령 코드, access/refresh token과 audience는
저장하지 않습니다. 창 종료, Window Reload 또는 WSL 재연결 뒤에는 새 코드가 필요합니다.

Access/refresh token은 server-side session에 연결합니다. 새 로그인이 성공하면 같은
course/student의 이전 session을 교체하여 최신 session 하나만 유지하며, session은 최초 발급
후 4시간이 지나면 refresh 여부와 무관하게 만료됩니다. 모든 assignment, submission/result
조회에서 course, enrollment, session과 object ownership을 다시 검사합니다. 명시적 Sign Out은
현재 server session을 즉시 폐기합니다. 비정상 종료 시 Extension 메모리는 사라지지만 server
session은 절대 만료 또는 다음 로그인 교체까지 남을 수 있습니다. GitHub OAuth와 인증용
환경변수는 파일럿에 사용하지 않습니다.

Server는 기본적으로 loopback HTTP에만 bind합니다. 외부 HTTPS profile은 같은 loopback
listener 앞에서 TLS를 종료하고 공개 HTTPS origin을 service URL과 QR에 사용합니다. 평문 bind의
유일한 파일럿 예외인
`external_access_mode=insecure-http`는 `public_base_url`과 `listen`이 동일한 실제 RFC 1918
IPv4이고 Extension에서 `autograde.allowInsecureHttpPilot=true`를 선택했을 때만 동작합니다.
`0.0.0.0`, hostname, 공용·개방 LAN, router port forwarding과 인터넷 접속은 허용하지 않습니다.
이 예외는 암호화나 인증 강화가 아니며 합성·사전 검토 코드의 짧은 기능 시험에만 사용합니다.
정확한 시작·종료 통제는 [신뢰 LAN 외부 접속 파일럿](../operations/trusted-lan-pilot.md)을
따릅니다.

기본 loopback/TLS Dashboard는 학생 bearer token과 다른 instructor Basic credential로
보호합니다. `insecure-http` LAN mode에서는 Basic credential을 평문으로 받지 않도록
`/instructor`와 instructor API를 `404`로 비활성화하고, 결과는 교수자 CLI로만 확인합니다.

Browser를 열기 전에 Extension은 server가 반환한 verification URL의 origin을 설정된 service
origin과 대조하고 사용자에게 scheme/host/port를 표시합니다. Loopback HTTP도 같은 OS 계정의
다른 process나 공용 browser/profile을 격리하지 않으며, LAN HTTP는 활성화 코드, token,
제출물과 결과가 같은 network의 공격자에게 노출될 위험을 추가합니다. 메모리 token을 폐기해도
다운로드한 source/workspace와 browser·OS profile 흔적은 남으므로 별도 좌석 정리 절차가
필요합니다.

## PyJevSim과 Git 호환 경계

학생이 누른 direct submission은 즉시 bounded worker queue로 처리하므로 PyJevSim model을
통과하지 않습니다. PyJevSim `R_TIME`/`V_TIME`은 향후 instructor repository의 주기 collection과
schedule simulation에만 사용합니다.

Git 호환 모드를 선택하면 bare cache, explicit branch refspec, immutable exact-SHA ref와 tracked
file archive 경계를 추가합니다. GitHub App credential, repository preflight와 runner isolation은
학생 인증 및 direct-bundle 파일럿과 별도 구성입니다. Git `pull` 또는 persistent working tree는
사용하지 않습니다.

## Production 목표 구조

실제 학생 코드를 받기 전 채점 실행을 API/상태 서비스에서 분리합니다.

```text
API + immutable receipt
          │
          v
durable queue / isolated worker host
          │
          v
disposable Linux container or microVM
  ├── non-root, read-only root/mount
  ├── network disabled
  ├── capability/syscall 제한
  ├── CPU/memory/PID/time/output 상한
  └── job 종료 후 workspace 폐기
```

Docker/Podman은 가능한 production adapter이며 파일럿 완료 조건이 아닙니다. Container daemon
자체 권한과 host 경계를 별도로 보호해야 합니다. Confidential test에는 단일 container의
read-only mount만으로 부족하며, 학생 process가 assessment/data를 볼 수 없는 child sandbox나
microVM protocol이 필요합니다.

상세 API와 학생 흐름은 [학생 플랫폼 아키텍처](student-platform.md)를 참고합니다.
