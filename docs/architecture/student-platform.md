# 학생 실습 플랫폼 아키텍처

> 상태: local CSV config/roster, GitHub 없는 direct bundle 배포·제출, OAuth-free device
> auth, Linux/macOS/WSL2 Extension, `pilot-local` grader와 instructor dashboard를 파일럿
> vertical slice로 사용합니다. Local grader는 격리가 아니며 신뢰된 코드만 허용합니다.
> Network 기본값은 loopback-only이고 신뢰 LAN HTTP는 합성·사전 검토 코드의 단기 기능
> 시험에만 명시적으로 여는 예외입니다.
> Container/microVM worker, Git exact-SHA 호환 모드, repository provisioner/reconciler,
> PR remote 검증과 confidential-test 이중 sandbox는 배포 단계 아키텍처입니다.

## 구성요소

```text
pilot config CSV + roster CSV ──> 교수자 CLI ── starter + assessment/data 등록 ──┐
                                               v
학생 브라우저 ─ 매 로그인 새 코드 ─> Autograde API ─> SQLite + immutable bundle CAS
                             ^   │                  │
                             │   │ 최신 1-session   │ accepted receipt
                             │   v                  v
                   VS Code Extension ─ 제출 ─> 4-worker grading queue
                   URL만 지속/token은 메모리 전용
                   Linux / macOS / WSL2                 │
                                                       v
                                      pilot-local process grader
                                                       │
                             Extension 결과 <──────────┴──> 교수자 dashboard
                                                        (loopback profile only)

배포 단계 호환 모드: 학생 private repository ─ exact SHA ─> isolated grading pipeline
```

논리 component는 초기 배포에서 하나의 modular service와 worker 집합으로 운영할 수
있습니다. component 경계는 credential, blocking I/O, 재시도, 감사 원장을 분리하기 위한
것이지 처음부터 network microservice로 나누기 위한 요구가 아닙니다.

파일럿에서는 모든 instructor 명령이 같은 local pilot config CSV를 명시하고 roster를 별도
CSV로 import합니다. Config는 course/data root, service URL/bind/port, network 접근 mode,
grading runtime과 worker 수를 고정하며 shell 환경변수에 의존하지 않습니다. 기본 network
mode는 loopback-only이고, 신뢰 LAN HTTP는 별도 명시적 mode입니다. CSV는 설정 source이고
SQLite는 실행 중 인증·제출·결과 원장입니다.

## 실행 환경과 Extension 경계

Extension은 학생 workspace 파일에 접근해야 하므로 `extensionKind: ["workspace"]`로
선언합니다. Windows에서는 VS Code Remote WSL host에서, Linux와 macOS에서는 local
workspace extension host에서 실행합니다. Git mode를 선택한 과제에서만 Git 실행 파일과
remote credential이 필요합니다.
[VS Code Remote Extension 문서](https://code.visualstudio.com/api/advanced-topics/remote-extensions)를
구현 기준으로 사용합니다.

Windows의 학생 workspace는 Linux toolchain과 같은 filesystem인 `~/courses/...` 아래에 둡니다.
`/mnt/c/...`는 기능상 허용할 수 있지만 기본 clone 위치로 제안하지 않습니다. Linux
도구를 사용하는 프로젝트는 WSL filesystem에 두는 것이 빠르다는
[Microsoft WSL 지침](https://learn.microsoft.com/en-us/windows/wsl/filesystems)을 따릅니다.
학생은 한 과제 root가 아니라 여러 과제 하위 폴더를 담는 전용 수업 폴더를 workspace root로
엽니다.

Extension의 책임은 다음으로 제한합니다.

- assignment와 delivery mode 조회
- bundle mode의 starter를 현재 수업 workspace 직접 하위에 안전하게 해제하고 같은
  Extension Host session에서 workspace 식별
- bundle mode의 일반 파일만 포함한 결정적 source bundle 생성·업로드
- Git mode의 Git/GitHub credential preflight, clone, branch/HEAD/push 상태 확인
- 제출 의사 확인과 제출 API 호출
- submission/grade 상태와 정제된 결과 표시

Extension은 학생이 수정할 수 있는 public client입니다. 로컬 manifest, Git remote 이름,
Extension version, local test 결과는 권한이나 점수의 근거가 아닙니다.

공용 좌석에서는 `autograde.serviceBaseUrl`만 machine scope로 지속합니다. Access/refresh token과
그 audience는 Extension Host 메모리에만 존재하므로 VS Code 종료, Window Reload, Extension
Host 재시작 또는 WSL 재연결 후에는 새 Sign In이 필요합니다. 이 정책은 다운로드한
source/workspace, `.autograde` marker, VS Code의 비인증 상태, browser 기록/cookie/autofill 또는
OS 사용자 profile을 삭제하지 않습니다. 인증 종료와 좌석 파일/profile 정리는 서로 다른
운영 통제입니다.

## Direct bundle 배포

기본 MVP의 release 좌표는 `(course_key, assignment_key, release_id)`이며 한 release를 모든
active enrollment가 공유합니다. 교수자는 starter, assessment와 선택적 data 디렉터리를
등록합니다. 파일럿의 `pilot-local` runtime은 runner image를 요구하지 않습니다. Server는 각
디렉터리를 결정적 manifest `tar.gz`로 만들고
archive SHA-256 content-addressed storage에 저장합니다. assessment/data는 학생 API나 starter에
포함하지 않고 채점 workspace에만 별도 read-only tree로 materialize합니다.

학생 다운로드와 제출은 다음 endpoint를 사용합니다.

```text
GET  /v1/assignments/{id}/starter
POST /v1/assignments/{id}/submissions   Content-Type: application/gzip
```

Extension은 absolute/parent/backslash 경로, `.git`, `.autograde`, link/device, 중복 및
Unicode/casefold 충돌을 거부합니다. server는 같은 검증을 다시 수행하고 manifest의 파일
digest·크기·executable bit를 payload와 대조합니다. 접수 성공 전에 source CAS 저장과 immutable
receipt 기록이 끝나며, 권한의 근거는 bearer session에서 해석한 student/course/enrollment와
server assignment ID다. workspace 안의 학생이 편집 가능한 metadata는 신뢰하지 않습니다.

Bundle 다운로드 대상은 현재 신뢰된 수업 workspace root의 새 직접 하위 폴더입니다. Extension은
다운로드 후 새 window를 열거나 workspace를 reload/add하지 않으므로 메모리 token이 있는
Extension Host가 유지됩니다. 안전한 최상위 README는 현재 editor preview로 열고, 없으면
과제 폴더를 OS explorer에 표시합니다. 제출 대상은 active file ancestor를 우선한 뒤 workspace
root와 직접 하위 marker에서 찾고, 여러 후보는 Quick Pick으로 결정합니다. 선택한 child root만
bundle로 만들고 marker의 assignment를 인증된 server 목록과 다시 대조합니다.

## Production: repository 배포

```text
instructor template + immutable release SHA
                    │
                    v
        repository provision plan
                    │ explicit apply
                    v
     private repo per course/assignment/student
                    │ student Git credential
                    v
              clone into WSL2
```

template repository는 배포 원본이고 학생이 clone하는 최종 target은 자신에게 할당된
private repository입니다. 학생별 repository key는 다음 논리 좌표를 사용합니다.

```text
(course_key, assignment_key, student_key)
```

owner/name은 표시와 URL 생성에 사용하고 canonical remote identity에는 GitHub numeric
repository ID를 사용합니다. Git compatibility implementation은 이 ID와 clone URL의 관계를
operator가 GitHub API로 확인한 뒤 등록한다고 신뢰하며, 원격 metadata를 재검증하는 provisioner는 아직
없습니다. 목표 provisioner는 이름이 충돌하거나 같은 이름의 기존 repository가 발견되어도
자동 adopt하지 않습니다.

Provisioner는 mutable template default branch를 그대로 복사하지 않습니다. assignment에
고정된 `template_repository_id`, `release_commit_sha`, `release_tree_sha/content_digest`에서
학생 repository를 materialize하고 초기 tree/digest를 다시 검증합니다. 다음 desired와
observed lifecycle이 일치한 뒤에만 Extension에 clone target으로 공개합니다.

```text
planned -> provisioning -> invitation_pending -> ready -> suspended -> archived
                         └───────────────────────────────> error
```

학생은 자기 repository에만 `push`, instructor team은 `maintain` 이상, read-only collection
App은 `Contents: read`를 갖습니다. organization base permission `none`, broad team grant,
초대 수락, visibility와 실제 effective permission을 주기적으로 reconcile하고 모든 mutation의
before/after와 operation key를 감사 원장에 남깁니다.

WSL의 clone/push에는 학생의 SSH key, Git Credential Manager 또는 `gh` credential을
사용합니다. 이 credential은 Autograde access/refresh token과 backend GitHub App
credential 어느 쪽으로도 교환하거나 복사하지 않습니다.

## 웹 활성화와 device authorization

### 흐름

```text
Operator                  Auth API              LMS/안전한 개별 채널
    │ 매 로그인 auth issue    │                       │
    │────────────────────────>│                       │
    │ 0600 file 또는 명시적 stdout│── 학생별 전달 ──────>│

Extension                 Auth API                 Browser /activate
    │ POST /device-authorizations │                       │
    │────────────────────────────>│                       │
    │ device_code, user_code, URL │                       │
    │<────────────────────────────│                       │
    │ open URL / show user_code   │──────────────────────>│
    │                             │  user_code + 활성화 코드
    │                             │<─ 원자적 1회 소비/승인 ─│
    │ poll token endpoint         │                       │
    │────────────────────────────>│                       │
    │ access + rotating refresh   │                       │
    │<────────────────────────────│                       │
    │ Extension Host 메모리에만 유지                     │
```

`device_code`는 high-entropy secret이며 Extension 밖으로 노출하지 않습니다.
`user_code`는 학생에게 보여주는 짧은 1회용 연결 코드입니다. 이와 별개인
학생 활성화 코드는 active course enrollment에 결합된 130-bit 1회용
credential입니다. 기본 만료는 7일이고 재발급하면 기존 미사용 코드를 폐기합니다.
브라우저는 두 코드를 함께 전송하며 server는 활성화 코드 소비와 device
승인을 하나의 transaction으로 처리합니다. 오류 반복은 device별 상한을 넘으면
거부됩니다. course·요청 시각 표시, deny action과 session dashboard는 UI 후속
작업입니다.

활성화 코드는 장기 학생 key가 아닙니다. 학생이 Sign In할 때마다 운영자가 새 코드를 발급해
개별 전달하고, 성공한 승인에서 즉시 소비합니다. 이전에 소비한 코드는 다음 자리나 다음
로그인에서 다시 사용할 수 없습니다. 새 로그인이 token 교환까지 완료되면 같은
course/student의 이전 active session과 token family를 원자적으로 폐기하고 최신 session만
유지합니다.

`GET /activate` 단계에서 server는 pending authorization ID, course, user-code HMAC,
CSRF nonce를 만료 있는 서명 값으로 묶어 `HttpOnly`, `SameSite=Lax` cookie에
저장하고 확인할 device label을 HTML-escape해 보여줍니다. `POST /activate/approve`는
cookie, CSRF, authorization ID, course와 user-code HMAC을 모두 다시
검증합니다. raw 활성화 secret은 POST body에만 있으며 URL, query, cookie에
넣지 않습니다. HTTPS 배포의 cookie에는 `Secure`도 추가합니다.

Local CSV 파일럿의 활성화 화면은 GitHub OAuth나 인증용 환경변수를 사용하지 않습니다.
Roster의 GitHub numeric user ID/login은 repository
배정·표시 metadata이며 활성화 코드 경로의 인증 근거가 아닙니다.
private repository를 fetch하는 backend은 활성화 코드, Autograde token, 학생
OAuth token을 사용하지 않습니다. built-in adapter가 read-only GitHub App
installation token을 회전하고 `GIT_ASKPASS`로 Git subprocess에만 전달합니다.
startup preflight는 App API의 numeric repository identity와 실제 allowed tree를 함께
검증합니다.

기본 활성화 코드와 device authorization 상태는 각각 다음과 같습니다.

```text
activation: issued -> consumed
                  ├─> revoked
                  └─> expired

device:     pending -> approved -> consumed
               ├────> denied
               └────> expired
```

Extension은 승인 전까지 server가 지정한 interval로 polling합니다. Browser를 열기 전에는
`verification_uri`와 `verification_uri_complete`의 origin이 설정된 service origin과 정확히
일치하는지 검사하고, Sign In 확인 화면에 그 origin을 표시합니다. 이 RFC 8628-style
device pairing은 outbound 요청만 사용하며 WSL 또는 Windows host에 inbound callback
port를 열지 않습니다. token endpoint는 `authorization_pending`, `slow_down`,
`access_denied`, `expired_token`을 구분하며 RFC 8628 polling 동작을 구현합니다.

### token 경계

- access token: 기본 15분의 opaque random bearer token
- server-side keyed digest 조회로 session ID와 course context를 해석하고 매 요청
  active/revoked 상태 확인
- refresh token: rotation과 token-family 재사용 감지, 최초 session 발급 후 4시간 절대 만료
  안에서만 사용 가능하며 rotation으로 절대 만료를 연장하지 않음
- device grant origin course를 token family/session에 함께 고정하고 access·refresh·owned-object
  lookup마다 동일 course를 명시적으로 비교
- `(course, student)`별 최신 active session 1개; 새 로그인 token 발급 transaction이 기존
  session과 family를 폐기; UTC 일일 발급 20, retained history 1,000 및 family별 refresh rotation 2,048
  hard cap; session 목록은 active 우선 최신 50개
- 폐기/만료 후 retention이 지난 unreferenced credential만 GC하며 submission audit FK는 보존
- device user code: 기본 5분, 1회 사용
- 학생 활성화 코드: 기본 7일, 130-bit, enrollment별 미사용 1개,
  device별 기본 5회 입력 실패 상한
- Extension token 저장: 없음. Access/refresh token과 audience는 Extension Host 메모리 전용;
  시작 시 구버전 SecretStorage token 삭제; service base URL 설정만 machine scope에 지속
- server 저장: high-entropy secret은 검증용 digest/token family로, user code와
  활성화 코드는 server secret 기반의 용도별 keyed HMAC으로만 저장
- 로그와 telemetry: raw 활성화 코드, Authorization header, device code, refresh token
  전체 redaction; verification URI의 user-code query는 reverse-proxy access log에서 제외/redaction

opaque token 자체에는 학생·session·scope claim을 넣지 않습니다. 각 course API 요청에서 현재 enrollment와
repository assignment, device session 상태를 확인하여 수강 취소·차단·session 폐기를
즉시 반영합니다.

정상적인 자리 이탈은 `Autograde: Sign Out`이 현재 server session을 폐기한 뒤 메모리를
지우는 흐름입니다. Server에 연결할 수 없을 때 사용자가 local-only 로그아웃을 선택하면
메모리는 지워지지만 server session은 4시간 절대 만료 또는 다음 로그인 교체까지 남을 수
있음을 경고합니다. Extension이 crash한 경우에도 같은 server-side 경계를 사용합니다.

파일럿의 기본 `http://127.0.0.1`/`localhost`는 신뢰된 동일 장비에서만 허용합니다.
Loopback이라는 사실만으로 같은 OS profile의 다른 process를 인증하지는 않습니다. 같은 신뢰
LAN에서 합성·사전 검토 코드의 기능만 잠깐 시험하는 경우에는 별도 config의
`external_access_mode=insecure-http`, 일치하는 실제 RFC 1918 public/listen 주소와 Extension의
`autograde.allowInsecureHttpPilot=true`를 모두 요구합니다. 이 mode는 매 로그인 전에 cleartext
위험을 다시 확인하며 인터넷, 공용·개방 LAN, `0.0.0.0`과 port forwarding을 거부합니다.
[신뢰 LAN 외부 접속 파일럿](../operations/trusted-lan-pilot.md)을 따르지 않고 주소만 바꾸는
구성은 허용하지 않습니다. 중앙 classroom server와 실제 운영은 HTTPS와 격리된 worker를
사용하는 배포 검토 대상입니다.

## submission protocol

Direct bundle contract는 다음과 같습니다.

```http
POST /v1/assignments/{assignment_id}/submissions
Authorization: Bearer <access-token>
Idempotency-Key: <client-generated-key>
Content-Type: application/gzip
Content-Length: <bounded-size>

<AUTOGRADE-BUNDLE manifest를 포함한 submission tar.gz>
```

server는 인증/수강/과제 window를 확인하고 upload를 compressed·expanded byte 수와 entry 수로
제한합니다. 검증된 archive digest와 크기, assessment/data digest, grading runtime,
rubric/version, result policy를 receipt에 고정한 뒤 `202 accepted`를 반환합니다. Production
runtime에서는 runner image digest도 고정합니다. 같은 학생·과제·source digest의
semantic retry는 기존 제출로 합쳐지고, 같은 idempotency key에 다른 valid bundle을 보내면
`409 Conflict`다.

Git exact-SHA 호환 contract는 다음과 같습니다.

```http
POST /v1/submissions
Authorization: Bearer <access-token>
Idempotency-Key: <client-generated-key>
Content-Type: application/json
```

```json
{
  "assignment_id": "asn_01K3...",
  "github_repository_id": 18273645,
  "pull_request_number": 1,
  "head_sha": "7d9fab31..."
}
```

두 mode 모두 `assignment_id`는 course와 immutable release를 server-side로 가리키는 opaque ID입니다.
`pull_request_number`는 과목이 PR review를 사용할 때만 보냅니다. 과제 policy는
`branch` 또는 `pull_request` submission mode와 allowed ref를 release 시점에 고정합니다.
server 처리 순서는 다음과 같습니다.

1. access session의 active 상태, 학생, active enrollment 확인
2. assignment release와 제출 window 확인
3. 할당된 GitHub numeric repository ID 확인
4. `branch` mode이면 configured allowed ref를, PR mode이면 assigned head repository,
   allowed head/base ref와 open/non-draft PR을 확인
5. repository lock 안에서 allowed ref를 fetch하고 fetched tip이 요청 SHA와 같은지 확인
6. exact object를 불변 local ref와 source snapshot으로 고정하고, 완료 시각을 deadline과
   다시 비교
7. scoped idempotency key/request hash, course/assignment/release, `received_at`,
   `accepted_at`을 request와 immutable receipt에 한 transaction으로 기록
8. `accepted`를 반환하고 grading event를 bounded in-process queue에 알림

Extension이 보낸 `student_key`, clone URL, score, local timestamp, runner 설정은 입력으로
받지 않습니다. HTTP 성공 응답은 이미 `accepted` receipt를 가진 제출입니다. remote ref가
요청 SHA가 아니거나 SHA를 마감 전 확보하지 못하면 durable grading request를 만들지 않고
안전한 API 오류로 다시 push/submit하도록 안내합니다.

Idempotency key는 `(server-side student, submission endpoint, assignment_id)` 범위이고 request
body hash와 함께 저장합니다. 같은 요청 replay는 기존 request 또는 receipt를 반환하며
같은 key에 다른 body를 보내면 `409 Conflict`입니다.

## 내부 채점과 결과

```text
submission request
        v
bundle digest validated (또는 Git remote SHA verified)
        v
immutable source pinned -> accepted receipt
        v
workspace: submission + assessment + data
        v
pilot-local process -> rubric evaluator
        v
feedback sanitizer -> result projection
        v
Extension polling / optional PR check
```

Bundle mode는 source CAS, Git mode는 기존 `GitCollector`의 bare cache/snapshot을 사용하며
둘 다 `WorkspaceBuilder`와 grader adapter 경계를 재사용합니다. 학생 플랫폼 상태는 mode별
request/receipt/result 원장에 기록하고 raw grader output은 저장·공개하지 않으며 bounded
public result projection만 영속화합니다.

`pilot-local` adapter는 host에서 assessment process를 실행합니다. Workspace 분리, timeout,
bounded output과 result schema 검증은 입력 provenance와 오작동 제한이며 host filesystem,
credential, network, UID 또는 kernel 격리가 아닙니다. 학생 program을 실행하는 assessment라면
그 program도 같은 안전 한계를 갖습니다. 따라서 실제 학생 코드에는 사용할 수 없습니다.

상태는 최소 다음을 구분합니다.

```text
accepted -> queued -> running -> graded -> published
   │                    ├─────> assessment_failed
   └──────────────────────────> infra_failed
```

새 HTTP 요청의 source/policy 오류는 receipt 생성 전에 4xx로 거부합니다. `rejected`는
이전 버전에서 접수됐거나 복구 중인 request를 위한 영속 상태이고, `infra_failed`는
queue·workspace·grader 운영 장애, `assessment_failed`는 instructor assessment 오류입니다.
운영·assessment 실패를 0점으로 변환하지 않습니다.

결과 API는 score, max score, rubric, 채점 SHA, 공개 가능한 diagnostics와 결과 상태만
반환합니다. 공개 정책은 `immediate`, `score_only`, `after_deadline`, `manual` 중 과제별로
고정합니다. `/submissions/{id}`와 `/result`는 매 요청마다 학생 object ownership 또는
instructor 역할을 확인하여 opaque ID를 아는 것만으로 다른 학생 결과를 읽지 못하게 합니다.

## API surface

```text
# Auth와 session
POST   /v1/device-authorizations
POST   /v1/device-authorizations/token
POST   /v1/tokens/refresh
DELETE /v1/sessions/current
GET    /v1/me/sessions
DELETE /v1/me/sessions/{session_id}

# 브라우저 device 승인
GET    /activate
POST   /activate/approve

# 학생 workflow
GET    /v1/me
GET    /v1/assignments
GET    /v1/assignments/{id}/starter
POST   /v1/assignments/{id}/submissions
GET    /v1/assignments/{id}/repository
POST   /v1/submissions
GET    /v1/submissions/{id}
GET    /v1/submissions/{id}/result

# 교수자 read-only dashboard
GET    /instructor
GET    /v1/instructor/dashboard
```

마지막 두 instructor endpoint는 기본 loopback/TLS profile에만 열립니다.
`external_access_mode=insecure-http`에서는 Basic credential을 평문으로 받지 않도록 둘 다
인증 prompt 없이 `404`를 반환하며 교수자는 운영 CLI로 결과를 조회합니다.

CLI fallback도 같은 학생 API와 protocol을 사용하여 Extension과 서로 다른 제출 판정
경로가 생기지 않게 합니다. 이 client는 local SQLite를 직접 조작하는 현재 운영 CLI와
별도 command surface와 credential을 사용합니다.

## PyJevSim 경계

현재 PyJevSim `R_TIME`/`V_TIME`은 기존 instructor CLI의 주기 repository collection
(`autograde schedule run/simulate`)에 사용합니다. 학생 플랫폼의 HTTP 제출은 event-driven
bounded worker와 SQLite recovery scan을 사용하며 PyJevSim model을 실행하지 않습니다.
repository reconciliation/deadline event를 플랫폼 PyJevSim stream으로 통합하는 것은 목표
설계입니다. 어느 쪽이든 HTTP, Git, DB transaction과 grading 실행은 DEVS transition 안에서
수행하지 않는다는 [ADR 0001](../../design/adr/0001-pyjevsim-scheduler.md)의 경계를 유지합니다.

## 구현 단계

1. student platform schema와 course/assignment/repository ownership — 구현됨
2. 학생별 1회용 활성화 코드, enrollment 확인, device authorization/session API — 구현됨
3. Linux/macOS/WSL2 Extension의 로그인·bundle 다운로드/제출·diagnostics — 구현됨
4. direct bundle API와 existing exact-SHA collection 연결 — bundle과 branch mode 구현됨
5. 파일럿 local-process grader와 result projection API — 파일럿 범위
6. instructor read-only dashboard — 구현됨
7. GitHub repository provisioning/reconciliation와 instructor release PR — 미구현
8. PR submission remote 검증, session 관리 UI, 학생용 CLI fallback — 미구현
9. Docker/Podman 또는 microVM worker, confidential-test child sandbox와 공식 성적 제출
   선택 — 배포 단계
