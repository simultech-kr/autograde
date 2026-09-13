# Autograde VS Code Extension MVP

Linux, macOS 또는 Windows WSL2의 학생 workspace에서 과제를 내려받고 제출하며 Autograde
서비스의 채점 결과를 확인하는 workspace extension입니다. 서버가 직접 starter와 제출물을
보관하는 `bundle` 방식이 기본이며, 기존 GitHub exact-commit 방식도 호환합니다.

## 학생 흐름

0.4.1: 사이드바 **내가 수락한 실습**에는 현재 수령 코드로 수락한 과제만 표시합니다.
수락 전용 API를 사용하므로 서버부터 업데이트하세요. 이전 서버의 전체 과제 목록으로 우회하지 않습니다.
[학생·교수자 화면과 인증 범위](../../docs/operations/student-instructor-views.md).

0.4.0: 과제를 펼치면 **제출 기록 / 이전 코드 복원**이 표시됩니다. 서버 접수본 최대 100건의
결과를 조회하고 새 폴더로 복원합니다. [사용·시험 절차](../../docs/operations/submission-history-mvp.md).

0.3.0부터 독립 웹 파일럿을 지원합니다. 학생 웹 주소는 `https://<도메인>:20010`,
확장의 **서버 주소 설정**은 `https://<도메인>:20000`입니다. 같은 PC에서는 각각
`http://127.0.0.1:20010`, `http://127.0.0.1:20000`을 사용합니다.

1. 빈 수업 폴더를 만들고, Linux/macOS에서는 일반 VS Code window로, Windows에서는 Remote
   WSL2 window로 그 폴더를 엽니다. Workspace Trust를 확인합니다.
2. 담당자가 안내한 **HTTPS 20010번** Autograde 웹사이트에서 `come3105` 또는 `come2201`을
   선택하고 학번과 전용 비밀번호를 입력한 뒤 과제를 고릅니다. 웹사이트가 보여 주는 **과제 수령 코드**
   `AK1-XXXX-XXXX-XXXX`를 확인합니다. GitHub OAuth 계정 로그인은 필요하지 않습니다.
3. 왼쪽 Activity Bar의 **Autograde** 아이콘을 열고 **내가 수락한 실습** 영역의
   **수령 코드 입력 및 다운로드**를 누릅니다. 같은 기능의 열쇠 아이콘도 제목 표시줄에 항상
   표시되므로 Command Palette를 열 필요가 없습니다.
4. 가려진 입력창에 수령 코드를 직접 입력합니다. 대소문자, 하이픈 위치와 ASCII 공백은 자동
   보정하지만 `0`, `1`, `I`, `L`, `O`, `U`처럼 코드에 사용하지 않는 혼동 문자는 거부합니다.
   Extension은 브라우저를 열지 않고 임시 device authorization을 만든 뒤 코드를 그 device에
   연결하고, 기존의 bounded token polling으로 로그인 token을 받습니다.
5. 성공하면 수령한 과제가 사이드바에 표시되고 다운로드가 바로 시작됩니다. `bundle` 과제는
   starter를 현재 수업 폴더 바로 아래의 개별 과제 폴더로 안전하게 내려받고, GitHub 과제는
   repository를 clone합니다. 아직 준비 중이면 **과제 새로고침** 후 **과제 파일 다운로드**를
   누릅니다.
6. 문제를 해결합니다. `bundle` 과제에는 commit이나 GitHub push가 필요하지 않습니다.
7. 과제 폴더 안의 파일을 연 상태에서 `Autograde: Submit Assignment`을 실행하거나,
   Autograde 과제 목록에서 제출할 과제를 선택해 실행합니다.
8. `Autograde: View Latest Result`에서 상태, 점수, rubric과 공개 가능한 feedback을 봅니다.
9. 공용 실습 PC를 떠나기 전에 `Autograde: Sign Out`을 실행합니다.

수령 코드는 과제별·학생별·단기 1회용입니다. 다른 과제를 받을 때마다 웹사이트에서 새 코드를
받아 Extension 입력창에 직접 입력합니다. raw 코드와 보정된 코드는 설정, 파일,
`globalState`, `workspaceState`, `SecretStorage`, log 또는 controller field에 저장하지 않고
redeem 요청이 끝나는 즉시 메모리 참조도 지웁니다. 반환된 access/refresh token도 현재
Extension Host 메모리에만 있습니다. VS Code window를 닫거나 `Developer: Reload Window`를
실행하거나 WSL 연결을 다시 시작하면 token이 사라지므로 웹사이트에서 새 수령 코드를 발급받아
다시 시작합니다. 이미 소비된 코드는 재사용할 수 없습니다.

활성화 코드/device 로그인 버튼은 학생 사이드바에서 제거했습니다. 기존 Sign In 명령은
기존 단일 교과목 서버용 호환 기능이며 새 파일럿에서는 사용하지 않습니다.
수령 코드 흐름은 해당 과제에 scope된 새 session을 만들며,
기존 session이 있으면 현재 Extension Host가 새 token으로 전환합니다.

`bundle` 다운로드는 새 VS Code window를 열거나 현재 workspace를 교체하지 않습니다. 안전한
최상위 `README.md`, `README.txt` 또는 `README`가 있으면 현재 editor에서 미리 열고, 없으면
과제 폴더를 파일 탐색기에 표시합니다. 따라서 Sign In → 다운로드 → 풀이 → 제출을 하나의
Extension Host와 로그인 세션에서 진행할 수 있습니다. 현재 workspace 자체가 이미 과제
폴더라면 다음 과제를 그 안에 중첩하지 않고, 상위 수업 폴더를 workspace로 열도록 안내합니다.

`bundle` 과제는 선택된 개별 과제 폴더의 regular file만 결정론적 `tar.gz`로 만들고 직접 서버에
업로드합니다. archive에는 `autograde.bundle.v1` 형식의 `AUTOGRADE-BUNDLE.json`
manifest가 포함되며 서버가 각 파일의 경로·크기·SHA-256·실행 비트와 대조합니다. 이 이름은
학생 파일에 사용할 수 없습니다. 다음 디렉터리는 제출하지 않습니다.

- `.git`, `.autograde`
- `build`, `dist`, `out`, `target`, `node_modules`
- `.gradle`, `.venv`, `.pytest_cache`, `.mypy_cache`, `__pycache__`

symbolic link와 special file은 제출을 중단합니다. 파일과 디렉터리는 합계 5,000개,
한 파일과 전체 원본 파일은 100 MiB, 압축 archive는 25 MiB 이하입니다. 서버는 Extension을
신뢰하지 않고 같은 검증과 enrollment, deadline 검사를 독립적으로 수행해야 합니다.

GitHub 과제에서는 제출 전에 다음 조건을 모두 확인합니다.

- 신뢰된 workspace
- Git repository와 branch가 존재함
- tracked/untracked 변경이 모두 commit됨
- branch upstream과 원격 ref가 존재함
- 원격 ref의 SHA와 로컬 `HEAD`가 같음
- 현재 branch upstream이 과제의 `target_ref`와 같음
- 원격 URL이 서비스가 할당한 과제 repository와 일치함
- 서비스가 numeric GitHub repository ID를 제공함

서버는 repository ID, allowed ref, deadline과 exact SHA를 다시 검증해야 합니다.

## 사이드바와 명령

과제 수령과 다운로드는 Activity Bar의 Autograde 사이드바에서 바로 실행합니다. 로그인 여부와
관계없이 **Assignments** 영역의 **수령 코드 입력 및 다운로드** 또는 제목 표시줄의 열쇠
아이콘을 사용할 수 있습니다. 로그인 전에는 기존 device 로그인용 **지금 로그인** 링크와
제목 표시줄의 **로그인** 버튼도 표시됩니다. 로그인 후에는 과제를 펼쳐 **과제 파일 다운로드**를
선택할 수 있으며, 과제 행 오른쪽의 다운로드 아이콘과 제목 표시줄의 **과제 파일 다운로드**
버튼도 같은 기능을 제공합니다. 목록이 비어 있으면 **과제 새로고침**을 누릅니다. 수령 코드
입력과 모든 다운로드 동작은 신뢰한 workspace에서만 표시되거나 활성화됩니다.

VS Code가 시작되면 하단 상태 표시줄에 **Autograde 서버: 확인 중/연결됨/연결 끊김**이
표시됩니다. Extension은 로그인 token 없이 `/healthz`를 5초 timeout으로 즉시 확인하고 이후
약 30초(27~33초 무작위 간격)마다 다시 확인합니다. 표시를 가리키면 정확한 서버 origin과
최근 확인 시각이 나오며, 클릭하거나 사이드바의 **서버 연결 확인** 버튼을 누르면 즉시 다시
확인합니다. 이 표시는 서버 접속 가능 여부의 최근 확인 결과일 뿐 지속 연결이나 학생 로그인
여부를 뜻하지 않습니다. Remote WSL2 창에서는 Windows 브라우저가 아니라 WSL2 Extension
Host의 network와 인증서 신뢰 저장소를 기준으로 확인합니다.

사이드바 버튼이 보이지 않거나 키보드로 실행해야 할 때에는 Command Palette에서 아래 명령을
대체 경로로 사용할 수 있습니다.

- `Autograde: Sign In`
- `Autograde: Sign Out`
- `Autograde: Configure Service Address`
- `Autograde: Check Server Connection`
- `Autograde: Enter Assignment Claim Code and Download`
- `Autograde: Refresh Assignments`
- `Autograde: Download or Clone Assignment`
- `Autograde: Submit Assignment`
- `Autograde: View Latest Result`

기본 설정은 `autograde.serviceBaseUrl`입니다. 사이드바 제목의 **서버 주소 설정** 버튼에서
`203.0.113.10:20000`, `[2001:db8::10]:20000` 또는 완전한 URL을 직접 입력할 수 있습니다.
scheme을 생략하면 HTTPS로 정규화하며 loopback 주소만 로컬 개발용 HTTP로 정규화합니다.
IPv6 주소에 port를 붙일 때는 대괄호가 필수입니다. localhost 이외의 주소에는 HTTPS가
필수입니다. 서버 주소는 machine-scoped 설정으로 유지됩니다. 로그인 중 주소를 바꾸면 기존
서버 세션을 먼저 폐기하고 로컬 token과 채점 화면을 지우므로 이전 서비스의 token이 새 주소로
전송되지 않습니다. IP 주소를 HTTPS로 사용할 때에도 인증서가 그 IP 또는 접속 이름에 유효해야
하며 인증서 검사를 우회하는 기능은 제공하지 않습니다.
파일럿 기본 주소는 `http://127.0.0.1:18080`입니다. Sign In 확인창은 실제로 열 서버의
origin을 표시하며, 서버가 다른 origin의 인증 페이지를 반환하면 브라우저를 열지 않습니다.

기존 device 로그인은 같은 신뢰 LAN에서 짧게 진행하는 외부 HTTP 기능 시험만 예외입니다. 이때
`autograde.allowInsecureHttpPilot=true`를 별도로 설정해야 하며 표준 점 표기의 RFC 1918
사설 IPv4 주소만 허용됩니다. Hostname, 공인 IP, `0.0.0.0`과 legacy numeric IP 표기는
거부합니다. Extension은 로그인할 때마다 token과 제출물이 노출·변조될 수 있음을 modal로
다시 알리고, 사용자가 명시적으로 계속하기 전에는 인증 정보나 제출물을 보내지 않습니다.
상태 표시줄의 공개 `/healthz` 확인만 token 없이 수행됩니다. 이 opt-in은 암호화를 제공하지
않으므로 인터넷, 공용 Wi-Fi, 실제 성적에는 사용할 수 없습니다.
서버 설정과 전체 절차는 repository의
`docs/operations/trusted-lan-pilot.md`를 따릅니다.

학번·비밀번호로 수령 코드를 발급하는 웹 페이지와 Extension의 수령 코드 교환은 운영 환경에서
반드시 HTTPS를 사용합니다. `autograde.allowInsecureHttpPilot`을 켜도 RFC 1918 HTTP 서버에는
수령 코드를 입력하거나 전송하지 않습니다. 개발자 한 명의 같은 장비에서 사용하는
`localhost`, `127.0.0.1` 또는 `::1` loopback HTTP만 개발 예외입니다.

Access token, rotating refresh token과 token audience는 현재 Extension Host의 메모리에만
보관합니다. VS Code 설정, `globalState`, `workspaceState`, `SecretStorage` 또는 파일에는
저장하지 않습니다. 같은 VS Code 실행 중에는 refresh token을 메모리에서 회전해 긴 실습을
지원하지만 server session은 최초 로그인부터 최대 4시간이며 rotation으로 그 절대 만료를
연장하지 않습니다. Extension Host가 종료되면 복구하지 않습니다. 이전 버전이 `SecretStorage`에
저장한 v1 token은 Extension 시작 시 삭제합니다. token, Authorization header, device
secret과 Git stderr는 log 또는 Output Channel에 기록하지 않습니다.

최근 제출 ID와 전송 중인 제출의 idempotency key도 현재 Extension Host의 메모리에만 둡니다.
같은 실행 중의 timeout/일시 오류 재시도에는 같은 key를 사용하지만, Window Reload나 WSL
재연결 뒤에는 복구하지 않습니다. 이 경우 server가 로그인 학생과 동일 source digest 또는
Git commit을 기준으로 기존 제출에 합칩니다. 시작할 때 이전 Extension이 `globalState`에 남긴
제출 ID·재시도 metadata를 삭제하고, 로그인 교체와 Sign Out 때 과제 목록, 제출 metadata,
Autograde Output과 diagnostics를 모두 지웁니다.

학생 활성화 코드는 Extension 설정, 메모리 또는 `SecretStorage`에 저장하지 않습니다. 브라우저의
서비스 페이지에 한 번만 입력하며, 성공하면 즉시 소비됩니다. 9자리 `연결 코드`는 현재
VS Code 장치를 식별하는 짧은 만료 코드이고 학생 활성화 코드와 다른 값입니다.

과제 수령 코드는 매 과제마다 가려진 입력창에서 새로 받으며 입력값을 미리 채우지 않습니다.
Extension은 raw 코드와 보정된 코드를 설정, 상태 저장소, `SecretStorage`, clipboard 또는 log에
남기지 않습니다. 수령 코드로 받은 access/refresh token은 위와 같이 현재 Extension Host
메모리에만 있으며 다음 VS Code 실행으로 복구하지 않습니다.

## API contract

Extension은 현재 로그인·제출 흐름에서 다음 endpoint를 사용합니다. session 목록과 개별 폐기
endpoint도 server contract에 포함되지만 현재 Extension UI는 current-session sign-out만
호출하며 다른 device 관리는 후속 UI 범위입니다.

```text
GET    /healthz
POST   /v1/device-authorizations
POST   /v1/assignment-claims/redeem
POST   /v1/device-authorizations/token
POST   /v1/tokens/refresh
DELETE /v1/sessions/current
GET    /v1/me/sessions
DELETE /v1/me/sessions/{session_id}
GET    /v1/assignments
GET    /v1/assignments/{assignment_id}/starter
POST   /v1/assignments/{assignment_id}/submissions
POST   /v1/submissions
GET    /v1/submissions/{id}
GET    /v1/submissions/{id}/result
```

수령 코드 흐름은 먼저 `POST /v1/device-authorizations`로 pending device를 만들고, 브라우저를
열지 않은 채 다음 JSON으로 코드를 그 device에 연결합니다. 이 요청에는 bearer token을 보내지
않습니다.

```json
{
  "claim_code": "AK1-2345-6789-ABCD",
  "device_code": "<pending device code>"
}
```

성공 응답은 `course_key`, `assignment_id`, `delivery_mode`, `acceptance_id`를 포함하고 token은
포함하지 않습니다. Extension은 이어서 기존 `POST /v1/device-authorizations/token`을 bounded
polling해 일반 access/refresh token을 받은 뒤, `GET /v1/assignments`에서 응답의
`assignment_id`를 찾아 자동 다운로드합니다. Redeem 응답이 network error, timeout 또는
잘못된 응답 형식으로 유실되더라도 서버가 이미 코드를 소비하고 device를 승인했을 수 있으므로
같은 `device_code`로 token polling을 계속합니다. 복구 성공 시 과제 목록을 다시 읽어 학생이
다운로드 대상을 선택할 수 있게 하며, 명시적인 `assignment_claim_denied` 응답에는 polling하지
않습니다.

Device token endpoint의 오류 `authorization_pending`, `slow_down`, `access_denied`,
`expired_token`을 구분합니다. 일시적인 network 오류와 `429`/`502`/`503`/`504`는
`Retry-After`와 30초 상한 backoff를 적용해 연결 코드 만료 전까지 다시 시도합니다.
모든 제출 요청에는 임의의 `Idempotency-Key`를 보냅니다. `bundle` 제출은
`Content-Type: application/gzip`인 request body 자체가 제출물이며 URL의 assignment ID로
대상을 지정합니다. archive SHA-256은 재시도 식별을 위해 로컬에 계산하지만 권한 근거로
사용하지 않습니다. GitHub 제출은 JSON으로
`assignment_id`, `github_repository_id`, `head_sha`를 보냅니다. 서버는 allowed remote ref를
동기 확인·snapshot한 뒤 `accepted` 또는 기존 idempotent 제출을 반환하고, Extension은 이후
채점 상태를 제한적으로 polling합니다. 제출 요청은 90초 timeout과 최대 3회 재시도를 사용하며,
전송 전에 key를 현재 Extension Host 메모리에 기록하여 같은 실행 중 timeout과 일시
오류 재시도에는 같은 key를 재사용합니다. Extension 재시작·로그인 교체 후에는 복구하지
않고, 서버가 로그인 학생의 동일 source digest 또는 Git commit을 기존 제출로 병합합니다.
`submission_mode`가 `pull_request`이면 학생에게 양의 PR 번호를 입력받아
`pull_request_number`도 전송할 수 있도록 client contract를 준비했습니다. 현재 platform MVP
worker는 `branch` mode만 처리하며 PR의 open/non-draft와 head/base ref 검증은 다음 단계라서
operator는 pull-request assignment를 등록하지 않습니다.

`bundle` starter는 전체 archive 검증을 마친 뒤 생성된 임시 디렉터리에 풀고 최종 경로로
rename합니다. absolute path, `..`, backslash/Windows drive path, symlink, hardlink, device,
중복 경로, 대소문자·Unicode 정규화 충돌, Windows 예약 이름과 archive 내부의 `.git` 및
`.autograde`를 거부합니다. manifest도 archive 내용과 대조한 뒤 workspace에는 풀지 않습니다.
다운로드 후 생성되는 `.autograde/assignment.json`은 workspace와
과제를 연결하는 편의용 표시일 뿐입니다. 학생이 수정할 수 있으므로 접근 권한으로 사용하지
않으며, Extension은 인증된 `GET /v1/assignments` 목록에서 같은 과제를 다시 찾아야만
제출합니다. 제출할 root는 현재 파일에서 수업 workspace까지의 상위 폴더와 수업 workspace의
직접 자식 폴더에서 찾습니다. Marker의 server와 assignment ID가 현재 인증된 서버 응답과
일치해야 후보가 되며, 여러 후보가 있으면 실제 경로를 표시해 학생이 선택합니다. Bundle은
선택한 과제 root에서만 만들어지므로 수업 폴더의 다른 과제나 교수자 메모를 포함하지 않습니다.

GitHub clone은 신뢰된 workspace에서만 실행됩니다. Extension은 서버가 제공한
`clone_url`을 우선하고 없거나 올바르지 않으면 `ssh_url`을 사용합니다. URL은 shell 없이 Git
인자 배열로 전달하며, HTTP(S) userinfo credential, password, query 또는 fragment가 포함된
URL은 `.git/config`에 secret이 남지 않도록 거부합니다. SSH 인증은 OS의 SSH agent에
위임합니다. 선택한 부모 폴더 아래의 단일한 repository 이름만 사용합니다. 대상
경로가 이미 있으면 clone하지 않고 이전에 중단된 clone 폴더인지 확인하도록 안내합니다.
이 same-session 수업 폴더 흐름은 direct-bundle MVP에 적용됩니다. 기존 GitHub 호환 clone은
아직 clone된 repository를 새 window로 열기 때문에 새 Extension Host에서 Sign In을 다시 해야
합니다. Git root 탐색까지 포함한 같은-window Git 흐름은 후속 범위입니다.

이미 로그인한 상태에서 Sign In을 다시 실행하면 기존 서버 세션을 먼저 종료할지 확인합니다.
Sign Out은 `DELETE /v1/sessions/current`를 호출해 현재 서버 세션을 폐기한 다음 메모리 token을
지웁니다. 서버에 연결할 수 없으면 서버 세션이 남을 수 있음을 명시한 뒤, 사용자가 승인한
경우에만 현재 Extension Host의 메모리 token을 삭제합니다. 창을 닫는 것만으로는 서버 폐기가
확인되지 않으므로 공용 PC에서는 명시적으로 Sign Out을 실행해야 합니다.

Sign Out은 다운로드한 source/workspace, `.autograde` marker, VS Code 최근 폴더 또는
browser/OS profile을 삭제하지 않습니다. 공용 좌석에서는 학교가 지정한 경로와 profile 정리
절차를 별도로 수행합니다. 기본 Local HTTP 주소는 신뢰된 같은 장비의 loopback 파일럿에만
사용합니다. 별도의 신뢰 LAN HTTP 파일럿은 server와 Extension 양쪽 opt-in 및 전용 운영
가이드를 모두 적용한 경우에만 사용합니다.

`bundle` 제출 확인창은 현재 폴더가 본인의 작업물인지 다시 확인하도록 요구합니다. Marker는
service와 과제를 연결하는 편의 정보일 뿐 학생 소유권이나 보안 경계가 아닙니다. 같은 OS/WSL
계정에서 이전 학생이 신뢰한 workspace를 재사용하면 답안 노출뿐 아니라 Workspace Trust에 따른
코드 실행 위험도 있으므로, 실제 순환 좌석은 학생별 OS/WSL 계정 또는 매 실습 후 폐기되는
profile/workspace가 필요합니다.

과제 응답은 배열 또는 `{ "assignments": [...] }` envelope를 허용합니다. 각 과제에는
최소한 `id` 또는 `assignment_id`가 있어야 합니다. `bundle` 과제 예시는 다음과 같습니다.

```json
{
  "id": "asn_01K3...",
  "title": "Lab 03",
  "assignment_key": "lab03",
  "delivery_mode": "bundle",
  "starter_url": "/v1/assignments/asn_01K3.../starter",
  "starter": {
    "url": "/v1/assignments/asn_01K3.../starter",
    "sha256": "sha256:<64-hex>",
    "size_bytes": 12345
  },
  "status": "ready",
  "ready": true
}
```

`starter_url`은 생략할 수 있으며 이때 Extension은 assignment ID로 고정 endpoint를
구성합니다. 값이 있다면 현재 Autograde 서비스와 같은 origin의 동일 endpoint만 허용하므로
bearer token이 다른 호스트로 전달되지 않습니다. `starter.sha256`과 `size_bytes`가 제공되면
archive를 풀기 전에 다운로드 결과와 일치하는지 확인합니다.

GitHub 방식은 안전한 제출을 위해 다음 repository 정보가 필요합니다.
`assignment_path`가 `.`이 아니면 결과 diagnostic의 path는 해당 과제 root 상대 경로이며
Extension이 workspace 안의 실제 파일 경로로 변환합니다.

```json
{
  "id": "asn_01K3...",
  "title": "Lab 03",
  "assignment_path": "assignments/lab03",
  "repository": {
    "github_repository_id": 18273645,
    "full_name": "school-cse101/cse101-lab03-20261234",
    "clone_url": "https://github.com/school-cse101/cse101-lab03-20261234.git",
    "state": "ready"
  }
}
```

## 개발과 검증

Node.js 22 이상이 설치된 환경에서 실행합니다.

```bash
npm ci
npm test
shasum -a 256 -c SHA256SUMS
code --install-extension autograde-vscode-0.2.2.vsix
```

이 저장소의 `SHA256SUMS`는 현재 배포 후보 VSIX의 SHA-256을 고정합니다. 배포자는
`shasum -a 256 -c SHA256SUMS`가 성공한 같은 파일을 pilot 학생에게 전달합니다. Source를
수정해 새 release를 만들 때는 lockfile에 고정된 Microsoft `@vscode/vsce`로
`npm run package:vsix`를 실행한 다음 새 artifact hash로 release manifest를 갱신·재검증합니다.

### Windows/WSL2에서 배포 VSIX 설치

Windows 탐색기에서 `.vsix` 파일을 더블클릭하지 않습니다. 파일 연결에 따라 Microsoft
**Visual Studio VSIX Installer**가 열리면 VS Code용 패키지를 Visual Studio 확장으로 잘못
검사하여 서명 오류를 표시할 수 있습니다. 반드시 **Microsoft Visual Studio Code**에서
설치합니다.

먼저 PowerShell에서 받은 파일의 hash가 배포본 `SHA256SUMS`의 16진수 값과 같은지
(대소문자는 무관) 비교합니다.

```powershell
Get-FileHash .\autograde-vscode-0.2.2.vsix -Algorithm SHA256
Get-Content .\SHA256SUMS
```

WSL2 과제에서는 WSL terminal에서 수업 폴더를 `code .`로 열고, 왼쪽 아래 연결 표시가
`WSL`인 **같은 VS Code 창**에서 Extensions 보기의 `...` 메뉴 → `Install from VSIX...`를
선택합니다. `code` 명령이 다른 제품을 가리키는지 점검하거나 Windows 측 Microsoft VS Code
CLI로 설치를 재현해야 할 때는 설치 유형에 맞는 정확한 경로를 사용합니다.

```powershell
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --version
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.2.2.vsix --force

# 모든 사용자용으로 설치된 경우
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --version
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.2.2.vsix --force
```

`--force`는 기존 설치 갱신과 확인 prompt 처리용이며, 패키지 서명 검증을 끄거나 우회하는
옵션이 아닙니다. 설치가 계속 실패하면 보안 설정을 변경하지 말고 오류 창의 제목, 전체 오류
문구와 코드, 위 명령의 `--version` 출력, Command Palette의 `Developer: Show Logs...` →
`Shared` log를 함께 수집합니다. 해당 명령이 보이지 않으면 `Developer: Open Logs Folder`로
연 log 폴더에서 Shared log를 수집합니다. 학교 관리 PC에서 정책 차단이 확인되면 학생이
정책을 우회하지 말고 IT 관리자에게 extension ID `autograde.autograde-vscode`의 allowlist
등록을 요청합니다.

VS Code에서 이 폴더를 연 뒤 제공된 `Run Autograde Extension` 디버그 구성을 실행하면
Extension Development Host가 열립니다. 지원 기준은 Linux 및 macOS의 로컬 workspace와
Windows의 Remote WSL2 workspace입니다. Windows native workspace의 다운로드·제출은
차단합니다. 제한된 untrusted workspace에서는 로그인·과제·결과 조회만 허용하고 로컬 파일
검사, 다운로드와 제출은 차단합니다.
