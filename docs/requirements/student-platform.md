# 학생 실습 플랫폼 요구사항

> 상태: local CSV config/roster → 학생 로그인 → starter 다운로드 → 직접 제출 →
> `pilot-local` 채점 → 결과 조회와 교수자 dashboard의 GitHub 없는 파일럿 vertical slice를
> 대상으로 합니다. Linux/macOS native와 Windows WSL2를 지원합니다. `pilot-local`은 격리된
> sandbox가 아니므로 합성·신뢰 코드만 허용합니다. Docker/Podman·microVM 배포, GitHub
> repository lifecycle, PR 제출 검증, confidential-test 격리와 공식 성적 반영은 후속 범위입니다.

## 전제

- 학생은 Linux/macOS의 local VS Code 또는 Windows의 WSL2 + VS Code WSL 환경을 사용합니다.
- Course, data root, service URL/listen/port, network 접근 mode, runtime과 worker 수는
  환경변수가 아니라 명시적으로 선택한 local pilot config CSV에서 읽습니다. 기본 network
  mode는 loopback-only입니다. Roster는 별도 UTF-8 CSV입니다.
- roster는 학교 `student_key`, 수강 `active` 상태와 학생별 ASCII 숫자 6자리 `password`를
  가집니다. Git 호환 모드를 쓰는 학생만 GitHub login과 numeric user ID를 함께 가집니다.
- 학교 SSO는 사용하지 않습니다. 권장 인증은 active course enrollment에 결합된 학생별
  Autograde 전용 비밀번호로 HTTPS 과제 페이지에서 10분·1회용 수령 코드를 발급하는
  방식입니다. 교수자 발급 1회용 활성화 코드는 호환 경로입니다. 파일럿 로그인에는 GitHub
  OAuth나 인증용 환경변수를 사용하지 않습니다.
- 공용 실습 장비에는 서비스 URL만 지속합니다. Access/refresh token과 token audience는
  Extension Host 메모리 전용이며 VS Code/WSL session을 넘어 복구하지 않습니다.
- 선택적으로 등록한 GitHub ID/login은 repository 배정·표시 metadata입니다. 활성화 코드
  경로에서 이 값을 학생 인증 증거로 사용하지 않습니다.
- 매주 실습에는 불변 `release_id`, starter bundle과 assessment/data digest가 있습니다.
  파일럿 runtime은 `pilot-local`이며 runner image를 요구하지 않습니다.
- 기본 배포 단위는 `(course_key, assignment_key, release_id)`이고 active enrollment가
  있는 학생만 authenticated starter를 받습니다. 학생별 repository는 필수가 아닙니다.
- 학생이 수행하는 제출 동작은 VS Code Extension의 `Autograde: Submit Assignment`입니다.
- 기본 source transport는 Extension이 만든 결정적 manifest `tar.gz` bundle이며 CAS의
  SHA-256가 제출과 채점의 원장입니다. Git 호환 모드에서는 GitHub의 pushed commit과
  서비스가 확정한 exact commit SHA가 원장입니다.

## 확정된 사용자 흐름

### 1. 학기 초 roster와 학생별 전용 비밀번호 등록

1. 관리자가 `student_key,active,password` UTF-8 roster를 준비하고 CLI import로 active
   enrollment와 초기 전용 비밀번호를 만듭니다. Active 행은 서로 다른 ASCII 숫자 6자리
   비밀번호를 가져야 하고 inactive 행은 비웁니다.
2. Git 호환 모드를 쓰는 경우에만 관리자가 GitHub login과 numeric user ID를 함께
   등록합니다.
3. 비밀번호 원문이 든 roster는 repository 밖 또는 ignore된 local path에 보관하고, 교수자만
   읽을 수 있게 보호합니다. POSIX에서는 현재 사용자 소유의 mode `0600` regular file만
   import합니다. Repository의 `pilot/roster.csv`는 합성 로컬 시험용이며 실제 학생에게
   사용하지 않습니다.
4. 관리자는 신원이 확인된 LMS 등의 개별 채널로 각 학생에게 자신의 비밀번호 하나만
   전달합니다. 전체 roster, 공유 course key, 공용 문서 또는 repository로 배포하지 않습니다.
5. 같은 roster 재-import는 기존 비밀번호를 변경하지 않습니다. 다른 값은 명시적
   `--replace-passwords`가 있어야 일괄 교체하고, 개별 분실은 `student password-set`으로
   처리합니다.
6. roster에 없거나 비활성인 사용자는 수령 코드나 활성화 코드를 발급·사용할 수 없고
   수업·과제·제출 API에 접근할 수 없습니다.

### 2. VS Code 연결

1. 학생은 교수자가 제시한 과제 QR을 휴대폰으로 열고 HTTPS service origin, 교과목과 과제를
   확인합니다. 주소나 인증서가 안내와 다르면 비밀번호를 입력하지 않습니다.
2. 학생은 웹 페이지에 자신의 학번과 Autograde 전용 비밀번호를 입력하고, 10분 동안 한 번만
   사용할 수 있는 과제 수령 코드를 발급받습니다.
3. Linux/macOS local 또는 Windows WSL filesystem의 전용 수업 폴더를 VS Code workspace
   root로 열고 신뢰합니다. Autograde 사이드바에서 서버 주소를 확인한 뒤 **수령 코드 입력 및
   다운로드**를 누르므로 Command Palette나 별도 callback port가 필요하지 않습니다.
4. Extension은 임시 device authorization을 만들고 같은 service origin으로 수령 코드를
   전송합니다. 서비스는 코드의 course·assignment·student·만료·1회용 상태와 pending device를
   원자적으로 검증하고 승인합니다.
5. Extension은 bounded polling으로 access/refresh token을 발급받고 수령한 과제 starter를
   바로 다운로드합니다. 이 session은 수락한 과제에만 scope됩니다.
6. Extension은 token을 현재 Extension Host 메모리에만 둡니다. Service URL 설정은
   machine scope로 유지되지만 access/refresh token과 audience는 SecretStorage, workspace,
   Git 설정, 환경변수와 로그에 저장하지 않습니다.
7. 다른 과제를 받을 때, 또는 VS Code 창 종료, `Developer: Reload Window`, Extension Host
   재시작이나 WSL 재연결 뒤에는 해당 과제 페이지에서 새 수령 코드를 발급받습니다.
8. 자리를 떠나기 전 `Autograde: Sign Out`으로 server session을 폐기합니다. 비정상 종료로
   명시적 폐기를 확인하지 못하면 server의 절대 만료 또는 다음 로그인 교체에 의존합니다.

교수자가 매 로그인마다 고엔트로피 활성화 코드를 별도 발급하고 학생이 `/activate`에 입력하는
기존 `Autograde: Sign In` device-pairing은 호환 경로로 유지합니다. 권장 과제 수령 흐름은
학생별 roster 비밀번호와 과제별 수령 코드를 사용합니다.

### 3. 매주 과제 배포

1. 강사는 starter, assessment와 선택적 data를 `release_id`와 함께 서버에 등록합니다.
2. 서버는 각 tree를 동일한 canonical 규칙으로 검증하고 immutable content-addressed
   bundle로 저장합니다. assessment/data는 starter 다운로드에 포함하지 않습니다.
3. 파일럿 설정의 grading runtime이 명시적으로 `pilot-local`인지 확인한 뒤 과제를
   `ready`로 공개합니다. 이 값은 합성·신뢰 코드만 허용하는 경고를 대체하지 않습니다.
4. Extension은 로그인한 학생에게 active course의 ready assignment만 보여주고,
   authenticated starter endpoint에서 bundle을 내려받습니다.
5. Extension은 path/type/digest/size, Unicode/casefold collision과 archive 한도를 검증한 뒤
   현재 신뢰된 수업 workspace root의 새 직접 하위 폴더에 안전하게 풉니다. Workspace가 없거나
   root 자체가 과제 marker를 가지면 수업 상위 폴더를 먼저 열도록 거부합니다.
6. Bundle 다운로드 후 새 창을 열거나 workspace를 reload/add하지 않습니다. 안전한 최상위
   README가 있으면 현재 editor preview로 열고, 없으면 OS 파일 탐색기에서 과제 폴더를
   표시하여 Extension Host의 메모리 session을 유지합니다.
7. 이미 공개된 release의 artifact와 채점 설정은 바꾸지 않고, 수정본은 새 `release_id`로
   등록합니다.

Git 호환 모드에서는 강사가 학생별 private repository와 권한을 별도로 준비합니다.
Extension은 서버가 저장한 numeric repository ID와 clone URL만 보여주고, Git/remote를
preflight하여 clone합니다. 학생 Git credential과 backend read-only GitHub App credential은
Autograde 학생 token과 분리합니다. built-in App preflight는 ready assignment의 numeric ID,
owner/name, private/archive/disabled 상태와 branch/tree를 검증합니다.

### 4. 제출

1. Extension은 active editor 파일의 ancestor를 우선하고, 이어서 workspace root와 직접 하위
   폴더에서 과제 marker 후보를 찾습니다. 여러 후보이면 Quick Pick으로 학생에게 선택받습니다.
   Marker는 과제 선택 힌트일 뿐이며 로그인 학생이 서버에서 다시 조회한 opaque assignment
   ID와 course/release 정보를 권한 근거로 사용합니다.
2. Extension은 `.git`, `.autograde`, dependency/build/cache와 특수 파일을 제외하고 regular
   file을 canonical manifest와 결정적 `tar.gz` bundle로 만듭니다.
3. 서버는 compressed/expanded size, entry count, path/type, digest, executable bit,
   course/enrollment/deadline을 다시 검증하고 CAS에 저장합니다.
4. idempotency key는 `(server-side student, endpoint, assignment ID)` 범위에서 body hash와
   함께 저장합니다. 같은 body의 재시도는 기존 receipt를 반환하고 다른 body에 같은 key를
   재사용하면 conflict를 반환합니다.
5. 성공 응답은 immutable source SHA-256와 receipt를 보존한 `accepted` 제출을 반환합니다.

Git 호환 모드에서 Extension은 repository, branch, HEAD SHA, uncommitted 변경과 remote push
상태를 확인합니다. 자동 commit이나 force-push를 하지 않으며, source archive 대신 numeric
repository ID, optional PR number와 exact HEAD SHA를 보냅니다. 과제의 submission mode가
`branch`이면 서비스가 고정한 allowed ref의 fetched tip이
   요청 SHA와 같아야 합니다. `pull_request`이면 assigned repository, allowed head ref,
   allowed base ref, open/non-draft 상태와 `head.sha`를 검증합니다.
6. server는 Git 요청 안에서 repository lock을 잡고 allowed ref를 fetch하여 tip과 요청
   SHA를 비교하고 exact object를 불변 ref/source snapshot으로 고정합니다.
7. 고정 완료 시각도 마감 전인지 확인한 뒤에만 request와 receipt를 한 transaction으로
   `accepted` 상태에 기록합니다. 성공 응답 전 branch가 바뀌거나 object를 마감 전 확보하지
   못하면 durable grading request 없이 제출을 거부합니다.
8. receipt는 `received_at`, `accepted_at`, course/assignment/release, exact SHA와 평가 입력
   provenance를 보존합니다.
PR을 사용하는 과목에서도 학생의 행위는 Extension 제출 버튼으로 통일합니다. PR은
review와 audit UI로 사용할 수 있지만, mutable PR 번호나 merge 결과가 아니라 검증된
`head_sha`가 채점 원본입니다.

### 5. 채점과 결과

1. 직접 제출은 검증된 bundle CAS digest를 source로 고정합니다. Git 호환 제출은
   `GIT_ASKPASS`로 제공한 read-only GitHub App installation token과 collection pipeline이
   exact SHA와 immutable source snapshot을 고정합니다. App token은 Git subprocess에만
   주입합니다.
2. `pilot-local` grader는 분리된 submission/assessment/data workspace를 host process에
   전달합니다. 이 디렉터리 분리는 provenance와 오염 방지를 위한 것이며 filesystem,
   process 또는 network 격리가 아닙니다.
3. 결과에는 source SHA, assessment/data digest, grading runtime과 rubric version을 함께
   기록합니다. Production runtime에서는 runner image digest도 기록합니다.
4. Extension은 결과 API를 polling하여 상태, 점수, rubric, 정제된 diagnostics를
   표시합니다.
5. trusted runner는 테스트/정답 의미, credential, 다른 학생 정보와 server 내부 경로를
   public JSON에 넣지 않아야 합니다. sanitizer는 bounded schema와 일부 secret pattern을
   강제하지만 의미 기반 declassification을 보장하지 않습니다.

초기 구현은 polling을 사용하며, push notification이나 streaming은 호환성을 깨지 않는
후속 개선으로 둡니다.

## 기능 요구사항

### 인증과 수강 권한

- `AUTH-01`: 권장 웹 인증은 active course enrollment의 `student_key`와 학생별 Autograde
  전용 비밀번호를 HTTPS 과제 페이지에서 확인하고, 과제별 10분·1회용 수령 코드를 발급합니다.
- `AUTH-02`: 서비스는 비밀번호 확인 결과와 수령 코드에서 학생·교과목·과제를 server-side로
  해석하며 Extension이 보낸 학생 식별자를 신뢰하지 않습니다.
- `AUTH-03`: device user code는 1회용이며 기본 5분 후 만료됩니다.
- `AUTH-04`: access token 기본 수명은 15분입니다.
- `AUTH-05`: refresh token은 사용할 때마다 rotation하지만 session의 최초 발급 시각을 기준으로
  한 4시간 절대 만료를 연장할 수 없습니다.
- `AUTH-06`: 만료, 거부, 이미 소비된 device authorization은 token으로 교환할 수 없습니다.
- `AUTH-07`: 학생은 API에서 자신의 device session을 확인하고 개별 폐기할 수 있습니다.
  웹 dashboard와 관리자 전체 폐기는 후속 UI 범위입니다.
- `AUTH-08`: 수강 비활성화는 발급된 token의 남은 수명과 무관하게 모든 course API에서
  즉시 반영하고 해당 course credential을 폐기합니다. 재활성화해도 이전 token은 되살아나지
  않으며 다른 course enrollment와 credential은 영향을 받지 않습니다.
- `AUTH-09`: 모든 API 요청은 opaque access-token digest에 연결된 server-side session이
  현재 활성인지 확인합니다.
  session 폐기는 refresh만 차단하는 것이 아니라 이미 발급된 access token도 즉시
  무효화합니다.
- `AUTH-10`: Extension polling은 server가 반환한 interval과 `authorization_pending`,
  `slow_down`, `access_denied`, `expired_token` 결과를 구분해 처리합니다.
- `AUTH-11`: device grant의 origin course는 token family/session에 고정하며 access, refresh,
  session 관리와 모든 학생 소유 object 조회는 동일 course를 명시적으로 필터링합니다.
- `AUTH-12`: 새 device 로그인이 성공하면 같은 course/student의 이전 active session과 token
  family를 같은 transaction에서 폐기하여 최신 로그인 하나만 유지합니다. UTC 일일 발급,
  보존 history와 family별 refresh rotation에는 원자적 hard cap을 적용합니다.
- `AUTH-13`: 폐기·만료된 credential history는 retention 이후에만, submission 감사 참조가
  없는 경우에만 GC하며 학생 session 목록은 active 우선으로 bounded 반환합니다.
- `AUTH-14`: 호환 로그인용 활성화 코드는 130-bit entropy를 갖고 기본 7일 후 만료합니다.
  enrollment당 미사용 코드는 하나만 허용하고 재발급 시 기존 코드를 폐기합니다. 각 Sign In은
  새 코드를 발급·전달·소비하는 단위이며 소비된 코드는 다음 로그인에 재사용하지 않습니다.
- `AUTH-15`: 운영 CLI는 호환 활성화 코드를 새 mode `0600` 파일에 덮어쓰기 없이
  기록하거나, 운영자가 명시적으로 선택한 경우에만 stdout에 한 번 표시합니다.
  raw 코드는 SQLite, URL, log에 저장하지 않고 인증된 LMS 등의 개별 채널로 전달합니다.
- `AUTH-16`: 호환 활성화 코드 소비와 pending device 승인은 하나의 원자적
  transaction으로 처리합니다. 재사용·만료·타 course·오류 코드는 같은 안전한
  공개 오류로 거부하고, device별 기본 5회 실패 후 해당 device authorization을 거부합니다.
- `AUTH-17`: Local CSV 파일럿의 권장 로그인은 GitHub OAuth, GitHub App과 인증용 환경변수 없이
  roster 비밀번호, 과제 수령 코드 및 device flow만으로 동작해야 합니다. 호환 활성화 코드와
  Git repository production 인증은 이 workflow와 별도 경로입니다.
- `AUTH-18`: 호환 `/activate` GET은 pending authorization ID, course, user-code HMAC, CSRF를
  만료 있는 서명 `HttpOnly`/`SameSite` cookie로 묶고 device label을 표시합니다.
  POST는 cookie와 모든 binding을 다시 검증해야 하며 raw 활성화 secret은 POST
  body 외의 URL, query, cookie에 넣지 않습니다.
- `AUTH-19`: Extension은 설정된 service URL만 machine scope로 지속하고 access/refresh token과
  audience는 메모리에서만 유지합니다. 시작할 때 구버전 SecretStorage credential을 삭제하며,
  window reload·Extension Host 종료·WSL 재연결 후에는 로그인 상태가 없어야 합니다.
- `AUTH-20`: 호환 Sign In에서 Extension은 browser를 열기 전에 verification URL의 origin이 설정된 service
  origin과 정확히 일치하는지 검사하고 사용자에게 그 origin을 표시해야 합니다.
- `AUTH-21`: `Autograde: Sign Out`은 server의 현재 session 폐기를 먼저 시도합니다. 통신 실패
  시 local memory만 지우는 선택은 server session이 절대 만료 또는 다음 로그인 교체까지
  남을 수 있음을 명시적으로 경고해야 합니다.
- `AUTH-22`: 학생 비밀번호는 정확히 ASCII 숫자 6자리이며 `scrypt$v2`로만 SQLite에 저장하고,
  원문은 보호된 roster 밖의 DB·URL·log·Dashboard·Extension 저장소에 남기지 않습니다.
- `AUTH-23`: 비밀번호 실패는 계정 존재 여부와 관계없이 같은 공개 오류와 hash 경로를 사용하고,
  enrollment별 5회 실패 시 5분 잠금 및 bounded hash concurrency를 적용합니다.
- `AUTH-24`: 과제 수령 코드는 학생·교과목·과제와 pending device에 결합되고 기본 10분 후
  만료되며, 한 번의 원자적 승인에서만 소비할 수 있습니다. 원문은 keyed digest로만 저장합니다.
- `AUTH-25`: 비밀번호 기반 발급과 수령 코드 교환은 HTTPS에서만 허용하며 같은 장비의 loopback
  HTTP만 개발 예외입니다. 신뢰 LAN HTTP opt-in은 이 흐름을 활성화하지 않습니다.
- `AUTH-26`: 수령 코드로 만든 session은 수락한 과제에만 scope되어 다른 과제의 목록,
  다운로드, 제출과 결과를 읽을 수 없습니다.

위 token 수명은 현재 MVP CLI의 고정값이고 session admission/retention과 호환 activation
입력 실패 상한은 `serve` 옵션으로 조정할 수 있습니다. 호환 활성화 코드 만료는
발급 명령에서 30일 이내로 선택할 수 있습니다. 4시간은 sliding idle timeout이 아니라
최초 session 발급 시각 기준 절대 상한이며 refresh가 갱신하지 않습니다.
public client의
refresh token은 rotation 또는 sender constraint를 적용해야 한다는
[OAuth 2.0 Security BCP](https://datatracker.ietf.org/doc/html/rfc9700)를 따릅니다.

### VS Code Extension

- `EXT-01`: Extension은 Linux/macOS에서 local extension host로, Windows에서는 WSL2
  remote extension host로 실행합니다. Windows native workspace 제출은 지원하지 않습니다.
- `EXT-02`: 지원 환경은 VS Code Desktop + Linux/macOS native 또는 WSL2이며 browser-only
  VS Code는 MVP 범위 밖입니다.
- `EXT-03`: Extension은 과제 목록, bundle 다운로드 또는 Git clone, workspace 연결, 제출,
  상태 새로고침과 결과 조회 명령을 제공합니다.
- `EXT-04`: Workspace Trust가 없을 때 과제 조회는 허용할 수 있지만 Git 실행, push,
  제출은 차단합니다.
- `EXT-05`: Extension package와 workspace에는 GitHub App key, hidden assessment,
  장기 API key를 포함하지 않습니다.
- `EXT-06`: Extension을 수정 가능한 untrusted client로 간주하고 모든 권한과 제출 입력을
  서버에서 재검증합니다.
- `EXT-07`: 공용 좌석에서 token 비지속성을 제공하더라도 다운로드한 source/workspace,
  `.autograde` marker와 browser/OS profile은 자동 삭제하지 않습니다. 학생·운영 문서는
  명시적 Sign Out과 별도의 workspace/browser/profile 정리를 요구해야 합니다. Extension의
  학생별 pending/latest 제출 metadata는 Extension Host 메모리로 제한하고 로그인 경계에서
  과제 tree, Output과 diagnostics와 함께 삭제해야 합니다.
- `EXT-08`: HTTP의 기본 허용 범위는 설정 origin과 동일 장비의 loopback server입니다. 별도
  신뢰 LAN 시험은 server config의 `external_access_mode=insecure-http`, public/listen의 동일한
  실제 RFC 1918 IPv4, Extension의 기본 `false`인 `autograde.allowInsecureHttpPilot=true`와
  로그인별 cleartext 위험 확인을 모두 요구합니다. `0.0.0.0`, hostname, 공용·개방 LAN,
  port forwarding과 인터넷 주소는 허용하지 않습니다. 상세 운영은
  [신뢰 LAN 외부 접속 파일럿](../operations/trusted-lan-pilot.md)을 따릅니다.
- `EXT-09`: Bundle starter는 현재 신뢰된 수업 workspace root의 직접 하위 폴더로 다운로드하며
  새 창, workspace reload 또는 workspace folder 추가를 일으키지 않아야 합니다. 같은
  Extension Host의 메모리 session으로 다운로드 직후 제출할 수 있어야 합니다.
- `EXT-10`: 제출 대상 탐색은 active file ancestor, workspace root와 직접 하위 폴더로
  제한하고 여러 marker가 있으면 사용자에게 선택받아야 합니다. 선택한 과제 root만 bundle로
  만들고 server assignment와 다시 대조해야 합니다.

### 저장소와 배포

- `BUNDLE-01`: 기본 배포·제출 transport는 manifest를 포함한 deterministic `tar.gz`이고,
  regular file과 directory만 허용합니다.
- `BUNDLE-02`: server와 Extension은 absolute/상위 경로, link/device, `.git`·`.autograde`,
  Windows 예약 이름/문자, trailing dot/space, Unicode NFC/casefold·file/directory collision을
  같은 정책으로 거부합니다.
- `BUNDLE-03`: 기본 상한은 compressed 25 MiB, expanded payload 100 MiB, entry 5,000개이며
  서버가 streaming upload와 extraction 양쪽에서 hard limit을 강제합니다.
- `BUNDLE-04`: starter, assessment, data와 submission은 SHA-256 content-addressed storage에
  immutable하게 보관하고 manifest와 실제 content/size/executable bit를 매번 대조합니다.
- `BUNDLE-05`: starter endpoint는 active enrollment를 인증하며 숨은 assessment/data의 경로,
  내용 또는 digest를 학생 assignment projection에 노출하지 않습니다.
- `BUNDLE-06`: 공개 release를 수정하지 않고 새 `release_id`를 등록합니다. Pilot readiness는
  artifact와 `pilot-local` 설정을 검증하고, production readiness는 이에 더해 exact-digest
  Linux runner image와 격리 policy를 검증합니다.

다음 `REPO-*` 요구사항은 파일럿 이후 Git 호환 모드에 적용합니다.

- `REPO-01`: instructor template과 학생별 private repository를 분리합니다.
- `REPO-02`: repository identity는 이름이 아니라 GitHub numeric repository ID를
  우선합니다.
- `REPO-03`: 저장소 생성, 초대, 권한 변경은 read-only collection credential과 분리된
  provisioning credential로 수행합니다.
- `REPO-04`: organization base permission은 `none`으로 두고 학생은 자신에게 할당된
  repository에만 `push`, instructor team은 `maintain` 이상, collection App은
  `Contents: read`를 갖습니다. peer 학생이나 broad student team에 의한 접근을
  reconciliation에서 탐지합니다.
- `REPO-05`: template 수정본은 학생 branch에 강제로 반영하지 않고 managed branch와
  PR을 사용합니다.
- `REPO-06`: roster 누락만으로 repository를 삭제하거나 권한을 자동 회수하지 않습니다.
- `REPO-07`: 학기 종료 기본 동작은 final exact-SHA collection 이후 archive이며 delete는
  초기 범위에서 지원하지 않습니다.
- `REPO-08`: lifecycle은 `planned -> provisioning -> invitation_pending -> ready ->
  suspended -> archived`와 `error`를 구분하고 desired/observed state, 멱등 operation,
  before/after audit를 보존합니다. Extension은 `ready` repository만 clone 대상으로
  표시합니다.
- `REPO-09`: provisioner는 pinned release commit/tree에서 학생 repository를 만들고
  initial tree 또는 content digest가 release와 일치하는지 검증한 뒤에만 `ready`로
  표시합니다. mutable template default branch를 암묵적으로 사용하지 않습니다.

### 제출과 평가

- `SUB-01`: 과제마다 `bundle` delivery mode 또는 Git의 `branch`/`pull_request` submission
  mode와 allowed ref를 불변 release policy로 고정합니다.
- `SUB-02`: remote SHA/snapshot 고정이 마감 전에 완료된 server 시각만 적시 제출로
  인정합니다.
- `SUB-03`: 현재 동기 admission에서는 `verifying`과 `pinning`이 HTTP 요청 안의 transient
  단계이며, exact object와 snapshot을 확보한 뒤 `accepted -> queued -> running -> graded ->
  published`를 영속화합니다. receipt 없는 실패 요청은 durable submission으로 만들지 않습니다.
- `SUB-04`: 학생 입력 오류인 `rejected`, 운영 장애인 `infra_failed`, instructor 평가
  입력 오류인 `assessment_failed`를 구분합니다.
- `SUB-05` (목표): 모든 제출 영수증을 보존하고 성적에 반영되는 제출을 별도로 표시합니다.
- `SUB-06`: receipt는 opaque assignment ID와 함께 course key, assignment key, immutable
  release ID를 보존하여 다른 과목의 같은 과제 이름과 혼동하지 않습니다.
- `RESULT-01`: 결과 공개 정책은 `immediate`, `score_only`, `after_deadline`, `manual`을
  지원합니다.
- `RESULT-02`: 결과 diagnostics는 채점 대상 assignment root 상대 경로와 행 번호를 사용하고,
  API의 `assignment_path`를 이용해 Extension에서 repository 경로로 안전하게 변환합니다.
  허용된 feedback만 학생에게 게시합니다.
- `RESULT-03`: submission/result ID 조회마다 로그인 학생의 object ownership 또는
  instructor 역할을 확인하며 active enrollment만으로 다른 학생 object 접근을 허용하지
  않습니다.
- `FALLBACK-01`: Extension 장애나 접근성 문제에 대비해 동일 API를 사용하는 CLI 제출과
  결과 조회 경로를 유지합니다.

## 현재 구현과 남은 범위

기존 collection schema와 별도로 학생 플랫폼 migration namespace가 course-scoped 학생,
enrollment, direct-bundle 및 학생별 Git assignment, device session, submission receipt와
result를 표현합니다. Git assignment-scoped repository의 기본 source path는 repository
root인 `.`이며 명시적 subpath도 허용합니다.

현재 구현된 항목은 다음과 같습니다.

- 학생별 1회용 활성화 코드 발급·폐기, 원자적 소비와 device authorization service
- 선택적 GitHub OAuth identity adapter와 호환 승인 화면
- 메모리 전용 Extension token, 4시간 절대 session, 최신 로그인에 의한 이전 session 교체,
  rotating refresh token, reuse detection, 즉시 session 폐기와 object ownership 확인
- local student 등록·CSV import와 instructor Basic-auth dashboard
- direct starter/assessment/data bundle 등록, authenticated 다운로드, direct submission과
  4-worker grading HTTP API
- Linux/macOS native 및 Windows WSL2 Extension의 로그인, 다운로드/clone, 제출과 결과 표시
- 25명 동시 threaded HTTP upload, SQLite admission, bundle CAS, 채점·소유권·dashboard regression
- allowed branch exact-SHA fetch hold, snapshot, durable recovery worker
- backend GitHub App installation token 발급·회전과 startup repository preflight
- LFS/submodule 거부, aggregate snapshot quota와 receipt 기반 artifact GC
- 파일럿 전용 local process grading과 public feedback sanitizer
- 기본 loopback HTTP 및 server/Extension 이중 opt-in의 신뢰 LAN 단기 HTTP 기능 시험

다음 항목은 별도 구현이 필요합니다.

- GitHub repository provisioning과 lifecycle/permission reconciliation
- instructor release를 managed branch와 PR로 동기화
- pull-request mode의 GitHub open/draft/head/base/reachability 검증
- 학생용 HTTP CLI fallback, LMS 연동과 이의 신청
- Docker/Podman 또는 microVM worker 격리, 성적에 반영할 제출 선택/표시와 confidential-test
  child sandbox

MVP direct-bundle CLI는 roster와 release를 operator가 등록합니다. Git 호환 모드에서는 이미
준비된 repository도 operator가 등록합니다. local SQLite를 직접 관리하는
`autograde-platform` 운영 CLI를 학생 제출 경로로 노출하지 않습니다.

`pilot-local`은 timeout, bounded output과 결과 schema 검증을 적용하더라도 host/network 강격리가
아닙니다. 실제 학생 코드, 비밀 평가 입력, 공식 성적, 공용·개방 LAN 또는 인터넷 접근
server에는 No-Go입니다. 신뢰 LAN 예외는 합성·사전 검토 코드의 단기 기능 시험만 허용하며,
문서·UI·로그에서 이를 sandbox, secure transport나 isolated grader로 표현해서는 안 됩니다.
