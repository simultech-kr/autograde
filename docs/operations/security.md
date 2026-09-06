# 운영 및 보안 기준

## Local 파일럿 안전 경계

현재 direct-bundle 파일럿은 local config CSV와 roster CSV, 기본 loopback HTTP,
`grading_runtime=pilot-local`을 사용합니다. 별도 신뢰 LAN 기능 시험은 명시적
`external_access_mode=insecure-http` profile로만 허용합니다. 환경변수, GitHub credential,
Docker/Podman과 image registry는 파일럿 실행에 사용하지 않습니다.

`pilot-local`은 instructor assessment를 host process에서 실행합니다. Assessment가 학생
program을 실행하면 그 program도 host filesystem, 현재 OS 사용자 권한과 network에 접근할 수
있습니다. Workspace 디렉터리 분리, timeout, bounded output과 result schema validation은
격리가 아닙니다. 따라서 합성·사전 검토한 신뢰 코드만 허용하며 실제 학생 코드, 공식 성적,
비밀 평가 데이터, 공용·개방 LAN 또는 인터넷 서비스에는 No-Go입니다. 신뢰 LAN 예외 역시
이 코드·데이터 제한을 완화하지 않습니다.

Pilot config에는 공개 가능한 course/path/service endpoint/network mode/runtime 값만 저장하고
raw token, 활성화 코드, instructor password나 학생 레코드를 넣지 않습니다. Roster는 별도
CSV로 관리하고 필요한 `student_key,active`만 수집합니다. Environment file과 shell profile을
설정 source로 사용하지 않습니다.

아래 GitHub service 인증, TLS reverse proxy와 container 격리 항목은 해당 기능을 선택하는
production 배포 기준입니다. Local 파일럿의 완료 조건이 아닙니다.

## Production: GitHub service 인증

- 권장 방식은 organization에 설치한 GitHub App의 read-only installation token입니다.
- token을 clone URL, SQLite, Git config, log에 넣지 않습니다.
- `GIT_ASKPASS` 같은 프로세스 단위 credential 전달 방식을 사용하고 수집 후 환경을 폐기합니다.
- repository 권한은 `Contents: read`로 제한합니다.
- repository 생성·collaborator 관리처럼 write 권한이 필요한 후속 provisioning은 collection credential과 분리합니다.
- 내장 adapter는 mode `0600` App PEM으로 RS256 JWT를 만들고 one-hour installation token을
  만료 5분 전에 회전합니다. GitHub 응답 권한이 `Contents: read`가 아니면 fail closed합니다.
- ready assignment는 service 시작 전 numeric repository ID, canonical owner/name,
  private/archive/disabled 상태, HTTPS clone URL, target branch와 tree를 병렬 preflight합니다.
  실패 항목이 하나라도 있으면 service를 시작하지 않습니다.
- Git subprocess에는 환경 allowlist와 App provider overlay만 전달하여 OAuth client secret 등
  무관한 service 환경변수를 노출하지 않습니다.

## 학생 웹·Extension 인증

- 학교 SSO 대신 active course enrollment에 결합된 학생별 1회용 활성화 코드가
  기본 인증 경로입니다. GitHub numeric user ID/login은 repository 배정·표시
  metadata이며 활성화 코드 경로의 인증 근거가 아닙니다.
- 운영자는 `auth issue STUDENT_KEY --output PATH`로 덮어쓰지 않는 mode `0600`
  파일에 코드를 발급합니다. 자동화에서 보호된 stdout이 필요한 경우에만
  `--show-code`를 명시합니다. 코드는 인증된 LMS 등의 학생별 개별 채널로
  전달하고 공유 문서, repository, 수업 공용 키로 배포하지 않습니다.
- 활성화 코드는 매 Sign In마다 새로 발급·전달합니다. 이전에 소비한 코드는 다시 사용할 수
  없고, 아직 쓰지 않은 상태에서 재발급하면 이전 코드는 폐기됩니다. 학기 초에 한 코드를
  발급해 여러 좌석이나 여러 주에 반복 사용하는 방식은 지원하지 않습니다.
- 활성화 코드는 130-bit entropy, 기본 7일 만료, enrollment별 미사용 1개
  정책을 적용합니다. 재발급은 이전 미사용 코드를 폐기하고
  `auth revoke STUDENT_KEY`는 기존 session을 변경하지 않은 채 현재 미사용
  코드만 폐기합니다.
- 브라우저 `/activate`는 VS Code의 짧은 device user code와 학생 활성화 코드를
  함께 받습니다. server는 course, 만료, active student/enrollment를 검증하고
  활성화 코드 소비와 pending device 승인을 하나의 SQLite write transaction으로
  처리합니다. 오류·만료·재사용·타 course는 같은 공개 오류로 반환합니다.
- `/activate` GET은 검증한 pending authorization ID, course, user-code HMAC, CSRF를
  만료 있는 서명 cookie에 묶습니다. cookie는 `HttpOnly`, `SameSite=Lax`, HTTPS에서
  `Secure`를 사용합니다. 페이지는 HTML-escape한 device label을 보여주고 POST는
  cookie, CSRF, authorization ID, course, user-code HMAC을 모두 다시 검증합니다.
- Local CSV 파일럿 로그인에는 GitHub OAuth, GitHub App 또는 인증용 환경변수를 사용하지
  않습니다. Git repository production 호환 인증은 이 흐름과 별도 credential 영역입니다.
- Extension은 OAuth 2.0 Device Authorization을 참고한 1회용 user code와 outbound polling으로
  연결합니다. Local 파일럿 기본값은 같은 장비의 loopback HTTP입니다. 신뢰 LAN의 명시적
  `insecure-http` 시험도 callback port는 열지 않지만 전송 암호화를 제공하지 않습니다.
  Production은 HTTPS를 사용하며 WSL 또는 Windows host에 inbound callback port를 열지 않습니다.
- 기본값은 device code 5분, access token 15분, session 절대 수명 4시간입니다. Refresh token은
  사용할 때마다 rotation하지만 최초 session 발급 시 정한 절대 만료를 연장하지 않습니다.
- device code는 1회 사용 후 즉시 폐기하고 만료·거부·소비 상태를 영속화합니다. 짧은
  user code와 activation endpoint에는 HTTPS reverse proxy의 IP/account rate limit을
  반드시 적용합니다. device별 activation 오류는 기본 5회, 설정 가능한 최대
  20회로 제한하며 내장 server의 body/concurrency 상한은 인증 rate limit을 대신하지 않습니다.
- high-entropy device/refresh secret은 원문을 저장하지 않고, user code와 활성화
  코드는 서로 다른 용도의 server-secret keyed HMAC으로만 lookup·검증합니다.
- raw 활성화 코드, `device_code`, Authorization header, access/refresh token은 URL,
  query, cookie, application log, telemetry에 남기지 않습니다. 활성화 코드는
  `/activate/approve` POST body에만 있습니다. 짧은 `user_code`는 Extension이 여는
  `verification_uri_complete` 형태의 `/activate?user_code=...` query만 예외로 하되,
  application log/telemetry에 남기지 않고 reverse-proxy access log에서 `/activate`의
  query를 제외하거나 redaction합니다. `--show-code`는 의도적 secret output이므로
  terminal 기록·CI log·shell capture 접근 권한을 별도로 통제합니다.
- Extension은 server가 반환한 polling interval을 지키고 `slow_down` 응답에서 간격을
  늘립니다. 승인 전 polling을 API 일반 rate limit과 별도로 제한합니다.
- refresh token은 token family와 rotation을 기록하고 이미 사용된 token의 재사용이
  탐지되면 해당 family를 폐기합니다. 한 family의 rotation 기록은 기본 2,048회로
  hard cap되며 도달하면 family 전체를 폐기하고 `reauthentication_required`로 재연결을
  요구합니다.
- access token은 claim을 담지 않는 opaque random value이고 server는 keyed digest로 session을
  조회합니다. device grant의 원래 `course_key`는 token family와 session에도 고정됩니다.
  access 인증, refresh rotation, session 목록·폐기, assignment/submission/result 소유 조회는
  모두 명시적으로 같은 course를 필터링하고 현재 enrollment를 다시 확인합니다. 한 학생이
  여러 과목에 등록되어도 한 과목에서 발급한 credential은 다른 과목 API에서 사용할 수 없습니다.
- Extension은 서비스 base URL만 machine scope VS Code 설정에 유지합니다. Access/refresh token과
  audience는 Extension Host 메모리 전용이며 SecretStorage, `.vscode`, workspace state, `.env`,
  Git config, command line과 log에 저장하지 않습니다. 시작 시 구버전 SecretStorage token을
  삭제하고, 창 종료·Window Reload·Extension Host 재시작·WSL 재연결 뒤에는 다시 로그인합니다.
- Browser를 열기 전에 server가 반환한 verification URL 두 형태의 origin을 설정된 service
  origin과 정확히 대조합니다. Sign In 확인 창에도 scheme/host/port를 표시하며 학생은
  교수자가 안내한 origin과 다르면 활성화 코드를 입력하지 않습니다.
- 학생용 API는 자신의 device session 목록과 개별 폐기를 제공합니다. Extension의 Sign Out은
  server 폐기를 먼저 시도하고, 실패하면 경고와 사용자 확인 없이 local token을 제거하지
  않습니다. 사용자가 위험을 확인하고 local-only 로그아웃을 선택한 경우에만 메모리를
  지웁니다. 웹 session dashboard와 관리자 전체 폐기 UI는 후속 범위입니다. 목록은
  active/unexpired 항목 우선, 최신순으로 기본 50개까지만 반환합니다.
- API는 access-token digest가 연결된 server-side session의 active/revoked 상태를 매 요청
  확인합니다. session 폐기는 refresh token뿐 아니라 기존 access token도 즉시 무효화합니다.
- 새 device 로그인이 성공하면 같은 course/student의 기존 active session과 token family를
  같은 transaction에서 폐기하고 최신 로그인 하나만 유지합니다.
- course 수강 비활성화는 해당 course의 미사용 활성화 코드, session, token
  family, active refresh verifier와 승인된 device grant만 원자적으로 폐기합니다.
  재활성화해도 이전 credential은 되살아나지 않으며 다른
  course enrollment나 전역 학생 identity를 변경하지 않습니다. 전역 identity 차단은 별도
  관리자 권한으로 분리합니다. course CLI는 기존 `student_key`의 numeric GitHub identity가
  다르면 전역 row를 갱신하지 않고 거부합니다. schema v4 upgrade도 당시 전역 identity 또는
  course enrollment가 비활성인 기존 session/family/active refresh/승인 grant를 terminal
  상태로 바꿔 이후 재활성화로 legacy credential이 되살아나는 것을 막습니다.
- 학생별 장기 API key나 수업 전체 공유 key는 발급하지 않습니다.
- 기본 session admission 정책은 `(course, student)`별 최신 active session 1개, UTC 일일 신규
  발급 20개, 보존 history 1,000개입니다. 새 로그인 교체와 각 한도 검사는 device grant 소비와
  같은 SQLite write transaction에서 수행됩니다.
- 폐기되었거나 refresh가 만료된 session은 기본 30일이 지나고 submission 감사 기록이 참조하지
  않을 때만 session, token family, refresh verifier, 연결된 consumed device grant 순서로 GC합니다.
  독립된 denied/expired device grant도 생성 경로에서 짧게 보존한 뒤 삭제합니다. submission이
  참조하는 session은 자동 삭제하지 않으므로 history hard cap에 도달하면 운영자가 제출 보존
  정책을 검토하고, 필요한 용량을 확인한 뒤 `--max-retained-sessions-per-student`를 조정해야 합니다.
  현재 MVP에는 감사 참조 session을 자동 삭제하는 명령이 없습니다.
- 운영자는 분실 장치에 대해 `auth sessions list|revoke|reset --student-key`를 사용할 수 있습니다.
  reset은 해당 course/student session, family와 active refresh verifier를 한 transaction에서
  폐기하며 credential 원문을 출력하지 않습니다.

### 공용 좌석의 잔여 위험

정상적인 자리 이탈은 `Autograde: Sign Out`으로 server session을 폐기한 뒤 메모리 token을
지우는 것입니다. 통신 실패 시 사용자가 local-only 로그아웃을 선택하거나 Extension이
crash하면 local token은 사라지지만 server 폐기는 확인되지 않습니다. 이 경우 4시간 절대
만료 또는 다음 로그인에 의한 session 교체가 server-side 복구 경계입니다.

이 정책은 인증 credential만 다룹니다. 다운로드한 source/workspace와 `.autograde` marker,
VS Code 최근 폴더, browser history/cookie/autofill, clipboard, terminal 기록과 OS 사용자
profile은 다음 학생에게 남을 수 있습니다. 실습실은 학생별 OS 계정 또는 임시 profile을
우선 사용하고, 불가능하면 명시적 Sign Out 뒤 승인된 workspace/browser/profile 정리 절차를
별도로 적용해야 합니다.

Extension 자체의 latest/pending 제출 metadata는 disk에 지속하지 않으며 로그인 경계에서
과제 tree, Autograde Output과 diagnostics와 함께 지웁니다. 이전 버전의 관련 `globalState`도
시작 시 제거합니다. 다만 VS Code 최근 폴더와 Workspace Trust는 Extension 밖의 상태이고,
marker는 학생 소유권을 증명하지 않습니다. 같은 OS/WSL 계정으로 이전 학생의 신뢰된 workspace를
재사용하는 운영은 답안 및 임의 코드 실행 위험 때문에 Go 조건을 충족하지 않습니다.

Loopback HTTP는 신뢰된 같은 장비의 Autograde server와 Extension 사이 기본 파일럿
경계입니다. TLS나 OS 사용자 격리를 제공하지 않고 같은 host의 악성 process를 막지 않습니다.

신뢰 LAN 예외는 server의 `external_access_mode=insecure-http`와 Extension의
`autograde.allowInsecureHttpPilot=true`를 모두 켜고, public/listen에 동일한 실제 RFC 1918
IPv4를 지정한 경우에만 사용합니다. 이 mode의 HTTP에서는 활성화 코드, bearer token, 제출물과
결과를 같은 LAN의 공격자가 관찰·변조할 수 있습니다. Instructor Basic credential의 평문 전송을
막기 위해 `/instructor`와 instructor API는 이 mode에서 `404`로 비활성화합니다. Built-in
server에는 production용 proxy IP/account rate limit이 없고 device별 입력 실패 상한만 있으므로,
개인 hotspot/격리망, source-subnet firewall, 합성·사전 검토 코드와 짧은 실행 시간으로 범위를
제한합니다. 종료 전에 학생 Sign Out, 종료 후 session과 미사용 활성화 코드 폐기 및 firewall
회수를 수행합니다. 전체 절차는
[신뢰 LAN 외부 접속 파일럿](trusted-lan-pilot.md)을 따릅니다.

중앙 server, 공용·개방 LAN 또는 인터넷 주소가 필요하면 이 예외를 사용하지 않고
HTTPS·server identity·운영 인증, rate limit과 격리된 worker를 갖춘 별도 배포 검토로
전환합니다.

상세 흐름은 [학생 플랫폼 아키텍처](../architecture/student-platform.md),
[ADR 0003](../../design/adr/0003-wsl-extension-device-authorization.md)와 이를 공용 좌석 정책으로
개정한 [ADR 0005](../../design/adr/0005-shared-seat-ephemeral-auth.md)에 정의합니다.

## Extension trust 경계

- Extension과 workspace 파일은 학생이 수정할 수 있는 untrusted input입니다.
- Extension이 보낸 학생 ID, clone URL, local timestamp, local test 결과, runner 설정을
  신뢰하지 않습니다.
- 제출 API는 로그인 사용자, active enrollment, numeric repository ID, allowed branch의
  remote exact SHA와 server 접수 시각을 다시 검증합니다. optional PR head 검증은 현재
  MVP가 아니라 다음 단계입니다.
- assignment/submission/result ID 조회마다 대상 학생의 object ownership 또는 instructor
  역할을 검증하여 IDOR를 방지합니다.
- Workspace Trust가 없을 때 Git 실행, push, 제출을 차단합니다.
- GitHub App key, hidden assessment/data, grader credential은 Extension package나 학생
  repository에 포함하지 않습니다.
- raw grader output은 학생용 결과와 분리하고 bounded allowlist 및 best-effort secret pattern
  필터를 통과한 feedback만 공개합니다. 의미상 숨은 테스트 정보인지는 sanitizer가 판별할 수
  없으므로 trusted runner가 공개 가능한 문구만 생성해야 합니다.

## repository 접근 정책

- organization base permission은 `none`으로 설정합니다.
- 학생에게는 자신에게 할당된 private repository에만 `push`를 부여하고 peer repository
  접근을 허용하지 않습니다.
- instructor team에는 `maintain` 이상, collection App에는 `Contents: read`만 부여합니다.
- provisioning credential은 collection/Extension/grader credential과 분리합니다.
- 주기 reconciliation은 visibility, broad team grant, outside collaborator, 초대 상태와
  effective permission drift를 확인합니다.

## 학생 코드

- `pilot-local`은 합성·신뢰 코드만 host에서 실행합니다. 실제 학생 코드는 실행하지 않습니다.
- 파일럿 workspace의 source, assessment와 data 분리는 provenance 경계이지 접근 통제가 아닙니다.
- Production collection host에서는 학생 코드를 실행하지 않고 Git cache나 credential을 grading
  sandbox에 mount하지 않습니다.
- Production은 source, assessment, data를 별도 mount로 전달하고 assessment/data를 read-only로
  둡니다.
- assessment/data를 사용하기 전에 `workspace digest` 결과를 assignment에 등록합니다. digest가 없는 입력은 기본 거부하며 legacy override 사용 결과는 별도 감사 대상으로 취급합니다.
- Production sandbox grader는 digest-pinned image를 `--pull=never`, network 차단, non-root user,
  read-only root와 input mount, capability 제거, no-new-privileges, CPU/memory/process/time/output
  상한으로 Docker/Podman에서 실행합니다.
- container runtime daemon 자체는 강한 host 경계이므로 운영 고도화 시 grader를 collection
  service와 별도 host 또는 microVM trust domain으로 분리합니다. raw 결과는 feedback
  sanitizer를 통과한 뒤에만 학생 API에 게시합니다.
- 단일 container의 non-root UID는 세 mount를 모두 읽을 수 있습니다. read-only 권한은
  무결성을 보호할 뿐 악의적인 학생 코드로부터 assessment/data의 기밀성을 보호하지 않습니다.
  confidential test를 쓰려면 trusted runner가 학생 코드를 assessment/data가 보이지 않는
  별도 UID/process sandbox 또는 별도 container/microVM에서 실행해야 합니다.

## 장애 상태

- fetch 실패는 `failed`이며 이전 SHA로 대체하지 않습니다.
- 일부 학생만 실패하면 run은 `partial`입니다.
- force-push 감지는 실패가 아니라 별도 audit signal입니다. 과목 정책에 따라 수동 검토 또는 자동 실패로 해석합니다.
- 동일 scheduler는 한 supervisor instance만 실행하는 것을 권장합니다. 같은 run/repository의 중복 작업은 process lock과 멱등 키로 직렬화되지만, 여러 host에서 공유 filesystem lock을 사용할 때는 filesystem의 lock 보장을 별도로 검증해야 합니다.
- 학생 플랫폼의 processor와 recovery worker는 필수 `course_key`로 구성하고 assignment join에서
  같은 course만 조회합니다. 다른 course의 submission ID가 queue에 들어와도 상태를 바꾸지
  않습니다. 기본 배포는 course별 data root/service instance이며 shared database에서도 이
  application-level course predicate를 제거하지 않습니다.
- scheduled collection의 `partial`/`failed` slot은 audit 결과를 남기고 다음 시각으로 전진합니다. runner 종료 시 nonzero 상태를 경보에 연결하고 실패 학생은 새 manual run으로 재수집합니다.
- snapshot export는 file/blob 상한, LFS pointer/submodule 거부와 process-safe aggregate archive
  hard quota를 적용합니다. `artifacts gc`는 SQLite receipt reachability, 기본 dry-run과 24시간
  grace를 사용해 orphan archive/ref 및 terminal workspace만 정리합니다. fetch history는 이
  archive quota에 포함되지 않으므로 data root를 전용 filesystem/volume에 두고 quota와 경보를
  함께 설정합니다.
- `SIGTERM`/`SIGINT`는 새 event 생성을 멈추고 bounded queue를 drain합니다. Python thread는 강제 중단할 수 없으므로 Git/파일 I/O가 멈출 때를 대비해 supervisor의 process 강제 종료 시간(예: systemd `TimeoutStopSec`)을 설정합니다.
- 실행 중 schedule disable은 정상 종료로 처리합니다. interval, assignment, timezone, catch-up, cursor 변경은 runner를 재시작해 새 event 계획으로 반영합니다.

## 로컬 권한

- 파일럿은 개인 시험 계정에서 실행하고 그 계정의 중요한 credential·데이터를 제거하거나
  폐기 가능한 host를 사용합니다. 같은 UID에서 신뢰되지 않은 학생 코드를 실행하지 않습니다.
- Production은 전용 `autograde` service UID/GID로 실행합니다.
- data root와 managed directory는 `0700`, SQLite와 lock은 `0600`입니다.
- workspace의 submission은 `0700/0600`, assessment/data는 read-only `0500/0400`으로 생성합니다.
- Production collection host에서는 같은 UID로 학생 코드를 실행하지 않습니다. Grading container
  내부의 추가 UID 분리는 runner 계약이므로 confidential-test
  deployment의 필수 검증 항목입니다.

## 백업

실행 중인 SQLite database 파일만 복사하면 WAL에 남은 commit을 잃을 수 있습니다. 백업은 다음 중
하나로 일관되게 수행합니다.

- platform과 collector를 정상 중지한 뒤 `state.sqlite3`와 같은 디렉터리의 WAL/SHM 상태가
  정리된 것을 확인하고 data root 전체를 복사합니다.
- 서비스를 계속 운영해야 한다면 SQLite online backup API 또는 data root를 포함하는
  application-consistent filesystem/volume snapshot을 사용합니다. live `state.sqlite3` 파일만
  `cp`하지 않습니다.

복구 단위에는 platform의 `state.sqlite3`, `platform-auth-secret`, `snapshots/`, grading
`workspaces/` 또는 동일 content를 다시 구성할 수 있는 immutable assessment/data bundle을 함께
포함합니다. 인증 secret을 잃으면 기존 device/access/refresh credential을 검증할 수 없으므로
전체 session을 폐기하고 재연결해야 합니다. 미사용 활성화 코드의 HMAC도 더 이상
검증할 수 없으므로 모두 폐기하고 필요한 학생에게 재발급합니다. secret 백업은
database와 분리 암호화하고 접근을
제한합니다.

성적을 재현하려면 SQLite row와 source archive뿐 아니라 digest에 해당하는 assessment/data
content와 runner image도 실제로 보존해야 합니다. runner image는 digest-pinned registry에
retention 예외를 설정하거나 OCI archive로 export하고, 복구 훈련에서 `--pull=never`로 실행
가능한지 확인합니다. digest 문자열만 보관하는 것은 충분하지 않습니다. bare `cache/`는
재수집 최적화 자료지만 force-push 전 commit의 장기 보존에도 사용되므로 함께 백업을
권장합니다.

정기 복구 훈련에서는 격리 환경에 위 백업을 복원하고 database 무결성 검사, snapshot digest,
assessment/data digest, runner image digest를 확인한 뒤 대표 제출을 재채점합니다. platform과
collector가 서로 다른 data root를 쓰면 두 root를 같은 recovery point로 취급합니다.

스키마 migration은 database별 process lock으로 직렬화됩니다. v3의 중복 `student_key`처럼 자동 선택하면 데이터 의미가 바뀌는 충돌은 repository key 목록을 포함한 `SchemaVersionError`로 중단하므로, backup 후 중복 매핑을 명시적으로 정리하고 다시 시작합니다. v6 이전 collection job의 과거 평가 설정은 복원할 수 없어 API에서 `grading_inputs_pinned=false`와 unknown 값으로 보존되며 현재 assignment 값을 과거 기록으로 backfill하지 않습니다. 이 snapshot에 instructor 입력을 붙이는 작업은 기본 거부되고 명시적 `--allow-unpinned-grading-inputs`만 허용합니다. 기존 assignment는 `grading_config_review_required=true`로 표시되므로 `assignment list`로 확인한 뒤 canonical digest를 포함해 `assignment add`를 다시 실행해야 다음 collection이 허용됩니다. Nonterminal legacy grade job은 migration에서 cancel하며 claim/complete도 snapshot provenance를 다시 검증합니다. Read-only `schedule simulate`도 구형 row를 잘못 해석하지 않고 `init` migration을 먼저 요구합니다.

v6 이전 개발 버전에서 이미 게시한 workspace의 manifest 검증 표시는 신뢰하지 않습니다. 기존 디렉터리는 감사 이력으로 격리하고, review가 끝난 assignment의 새 collection을 사용하거나 legacy snapshot에는 새 `--workspace-key`와 명시적 override를 사용해 `verified=false` manifest로 다시 준비합니다.
