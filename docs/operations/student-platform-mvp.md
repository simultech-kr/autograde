# Git repository/container 호환 모드 운영 참고

> 이 문서는 production 전환 시 검토할 기존 Git exact-SHA 및 container 경로의 참고 자료입니다.
> 현재 local CSV 파일럿에서는 이 절차, 환경변수, GitHub App/OAuth, Docker/Podman 또는 runner
> image를 사용하지 않습니다. 파일럿은
> [Local CSV 파일럿 실행 가이드](direct-bundle-mvp.md)를 따릅니다. 아래 절차를 실제 학생에게
> 적용하려면 별도의 production Go/No-Go와 격리 검증이 필요합니다.

이 문서는 한 과목 서비스 인스턴스에서 이미 생성된 학생별 private repository를 등록하고,
WSL2 VS Code Extension의 로그인 → clone → push → 제출 → 채점 → 결과 조회 vertical slice를
실행하는 절차입니다.

## 현재 범위

구현된 범위는 다음과 같습니다.

- roster 학생의 GitHub numeric user ID metadata와 course enrollment 등록
- 학생별 1회용 활성화 코드 발급·폐기와 OAuth-free device pairing
- 선택적 GitHub OAuth 호환 승인 경로
- access token 15분, rotating refresh token과 4시간 절대 session, 최신 로그인 교체
- 동시 refresh single-flight와 운영자 session list/revoke/reset 복구
- 학생별 assignment/repository API와 Extension clone
- built-in GitHub App installation token 회전과 repository startup preflight
- HTTP 접수 중 pushed branch의 exact SHA 확인, bare cache hold, immutable source snapshot
- LFS pointer·submodule 거부, aggregate snapshot hard quota와 artifact GC
- assessment/data digest 검증과 분리 workspace
- Docker/Podman sandbox 채점 및 공개 결과 정제
- Extension의 상태 polling, 점수·rubric·diagnostics 표시
- 기존 `autograde schedule run`의 PyJevSim 주기 collection

다음은 아직 자동화하지 않습니다.

- GitHub repository 생성, collaborator 초대와 권한 drift reconciliation
- instructor 변경을 학생 repository PR로 배포
- pull-request submission의 GitHub head/base/open/draft 검증
- LMS 업로드와 학생용 HTTP CLI fallback
- platform bulk roster import
- 성적에 반영할 제출을 별도로 선택하는 grade-selection UI
- 악의적인 학생 코드로부터 숨은 테스트를 격리하는 2단계 runner sandbox

따라서 MVP에서는 instructor가 GitHub organization에서 private repository와 학생 권한을
준비한 뒤 numeric repository ID와 clone URL을 등록합니다. 제출 mode는 `branch`만
사용합니다. 등록값과 GitHub 원격 identity/상태/tree의 대응은 ready preflight와
service startup에서 검증합니다.

## 1. 설치

```bash
cd autograde
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Docker 또는 Podman daemon이 실행 중이어야 하며 서비스 OS 계정이 선택한 runtime을 사용할
수 있어야 합니다. 운영 data root와 assessment/data는 학생 계정이 읽을 수 없는 별도
경로에 둡니다. GitHub App JWT 서명에는 OpenSSL CLI가 필요합니다.

```bash
export AUTOGRADE_COURSE_KEY=cse101-2026f
export AUTOGRADE_DATA_ROOT=/var/lib/autograde-cse101
autograde-platform init
```

`platform-auth-secret`은 최초 실행 때 32-byte mode `0600` 파일로 생성됩니다. 이 파일과
SQLite, snapshot, workspace를 같은 backup/restore 단위로 다룹니다.

## 2. 학생 등록과 활성화 코드

GitHub login과 numeric user ID는 repository 배정·표시 metadata입니다. 기본
활성화 코드 경로의 학생 인증 근거가 아닙니다. 현재 platform CLI는 roster를
한 명씩 등록하므로 instructor credential로 `gh api users/<login> --jq .id` 등을
실행해 repository metadata를 확인한 뒤 등록합니다. 자동 bulk resolution은 다음
단계입니다.

```bash
autograde-platform student add 20260001 \
  --github-user-id 12345678 \
  --github-login student-login
```

수강 취소 시 같은 명령에 `--inactive`를 사용하면 현재 `--course-key` enrollment만
비활성화되고, 그 course의 미사용 활성화 코드와 발급된 session/device grant가
폐기되어 재수강 처리 뒤에도 기존
token이 되살아나지 않습니다. 다른 course enrollment와 session, 전역 GitHub identity의
`platform_students.active`는 변경하지 않습니다. 전역 identity 차단은 course 운영 CLI와
분리된 관리자 작업이며 현재 MVP CLI에는 노출하지 않습니다. 이미 등록된 `student_key`의
numeric GitHub ID/login이 입력과 다르면 course CLI는 전역 identity를 덮어쓰지 않고
중단합니다. identity 교정·계정 변경도 별도 관리자 검증 작업으로 처리합니다.

Active enrollment 등록 후 학생이 **로그인할 때마다** 기본 7일 만료의 새 학생별 1회용
활성화 코드를 발급합니다. 권장 방식은 존재하는 private directory 아래에 새 mode `0600`
파일을 생성하는 것입니다. 기존 파일은 덮어쓰지 않습니다. 필요하면
`--expires-in-hours` 옵션으로 30일 이내의 만료를 지정합니다.

```bash
install -d -m 0700 /srv/autograde/activation-codes
autograde-platform auth issue 20260001 \
  --output /srv/autograde/activation-codes/20260001-login-01.txt
```

파일 write, permission 설정 또는 `fsync` 중 실패하면 명령은 방금 발급한
`activation_id`만 폐기하고 incomplete output 파일을 삭제한 뒤 실패로 종료합니다.
이전에 소비된 코드나 기존 session을 코드 발급만으로 폐기하지는 않습니다. 학생이 새
browser/device 승인을 완료하고 token을 발급받는 transaction에서 기존 session이 교체됩니다.

코드를 인증된 LMS 메시지 등의 학생별 개별 채널로 전달하고, 배포 파일은
기관 secret 보유 정책에 따라 접근을 제한하고 정리합니다. 공용 LMS 게시판,
repository, 공유 스프레드시트로 배포하지 않습니다. 보호된 자동화가 stdout을
필요로 할 때만 의도적으로 다음을 사용합니다.

```bash
autograde-platform auth issue 20260001 --show-code
```

`--show-code`는 JSON stdout에 raw secret을 한 번 노출하므로 terminal scrollback,
CI log, shell capture 권한을 확인합니다. 재발급은 이전 미사용 코드를 즉시
폐기합니다. 잘못 전달했거나 유출이 의심되면 다음 명령으로 현재 미사용
코드만 폐기합니다. 이 명령은 이미 발급된 session을 폐기하지 않습니다.

```bash
autograde-platform auth revoke 20260001
```

## 3. 과제와 grader 등록

assessment와 선택적 data tree는 등록 시 canonical SHA-256으로 고정됩니다. 이후 파일이
바뀌면 workspace 준비가 실패하고 학생에게 0점을 기록하지 않고 `assessment_failed`로
구분합니다.

`--assignment-path`는 `.` 또는 정규화가 필요 없는 repository 상대 POSIX 경로만 허용합니다.
절대 경로, `..`, 빈 segment, 역슬래시, control character, `.git` segment는 과제를 ready로
만들기 전에 등록 단계에서 거부됩니다.

등록 전에 service와 같은 runtime/context에 exact digest runner를 적재합니다. 다음 환경값을
`assignment add`/`ready`, 수동 처리와 `serve`에서 공통으로 사용합니다.

```bash
export AUTOGRADE_GRADING_RUNTIME=docker
export AUTOGRADE_RUNNER_IMAGE_INSPECT_TIMEOUT=15
export AUTOGRADE_RUNNER_IMAGE='ghcr.io/school/python-grader@sha256:<64-hex-digest>'
"$AUTOGRADE_GRADING_RUNTIME" pull "$AUTOGRADE_RUNNER_IMAGE"

autograde-platform assignment add lab01 20260001 \
  --release-id lab01-v1 \
  --github-repository-id 987654321 \
  --repository-owner school-org \
  --repository-name lab01-student-login \
  --clone-url https://github.com/school-org/lab01-student-login.git \
  --target-ref main \
  --assignment-path . \
  --assessment /srv/autograde/assessments/lab01 \
  --data /srv/autograde/data/lab01 \
  --runner-image "$AUTOGRADE_RUNNER_IMAGE" \
  --rubric-version v1 \
  --max-score 10 \
  --result-policy immediate \
  --due-at 2026-09-01T14:00:00+09:00
```

`assignment add`는 새 row를 먼저 `ready=false`로 stage하고 GitHub/tree, assessment/data
digest와 local runner image gate가 모두 성공한 뒤에만 공개합니다. 실패하면 명령은
nonzero이고 assignment는 hidden으로 남습니다.
GitHub App 설정이나 repository를 고친 뒤 동일 add 명령을 재실행하거나 다음 명령으로 다시
검증해 공개합니다. `ready`도 매번 해당 repository를 재검증하고 실패 시 hidden을 유지합니다.

```bash
autograde-platform assignment ready asn_...
```

20명 주차 assignment를 먼저 일괄 stage하려면 각 add에 `--not-ready`를 사용하고
`assignment preflight --jobs 4`로 hidden 항목까지 전체 검사한 뒤 학생별 `ready` 명령을
실행합니다. `--ready-only`는 이미 공개된 항목의 startup audit에 사용합니다.

worker는 network가 없는 실행 시점에 tag를 pull하지 않으며 `--pull=never`로 위에서 검증한
digest-pinned local image만 사용합니다.

grader image의 `ENTRYPOINT` 또는 `CMD`는 다음 read-only mount를 사용합니다. 서비스 계정과
같은 Docker/Podman daemon/context에 image를 미리 pull하고, 서비스 계정이 runtime socket을
사용할 수 있는지 확인합니다.

```text
/workspace/submission   학생 source
/workspace/assessment   instructor assessment
/workspace/data         선택적 instructor data
```

기본 실행 제약은 30초, 512 MiB, 1 CPU, 128 PID, stdout 128 KiB, stderr 32 KiB입니다.
root filesystem과 입력 mount는 read-only이고 network/capability는 없으며 `/tmp` 64 MiB만
쓸 수 있습니다. workdir은 `/workspace/submission`이고 arbitrary non-root numeric UID/GID로
실행되므로 image는 특정 사용자 이름이나 writable home에 의존하면 안 됩니다.

stdout에는 정확히 JSON object 하나를 출력해야 합니다. `max_score`는 0보다 크고 등록값과
정확히 같아야 합니다. runner는 학생 테스트 실패를 포착해 exit 0의 유효한 점수 JSON으로
표현해야 합니다. nonzero exit, timeout, output 상한 초과 또는 잘못된 JSON은 학생의 0점이
아니라 `assessment_failed`가 됩니다. stdout/stderr 원문은 학생 결과에 포함하지 않습니다.

```json
{
  "score": 8.5,
  "max_score": 10,
  "rubric": {
    "correctness": {
      "title": "Correctness",
      "score": 8.5,
      "max_score": 10,
      "feedback": "Edge case를 다시 확인하세요."
    }
  },
  "diagnostics": [
    {
      "path": "src/main.py",
      "line": 12,
      "severity": "warning",
      "message": "빈 입력을 처리하세요."
    }
  ]
}
```

service는 알 수 없는 field, 절대/상위 경로, 비정상 행 번호, secret 형태의 문자열과 크기
상한을 넘는 feedback을 제거하거나 거부합니다. 이 sanitizer는 형태·크기·일부 secret
pattern을 제한하는 방어 계층이지 숨은 테스트의 의미를 판별하는 declassifier가 아닙니다.
trusted runner가 학생에게 공개해도 되는 문구만 JSON에 넣어야 합니다.

현재 단일 grading container는 assessment/data를 학생 source와 함께 mount합니다. read-only는
변조를 막지만 같은 container에서 실행되는 악의적 학생 코드의 읽기를 막지 못합니다.
기밀 테스트가 필요한 과제는 trusted runner가 학생 프로그램을 assessment/data가 보이지 않는
별도 UID/process sandbox 또는 별도 container/microVM에서 실행하는 계약을 먼저 구현해야 합니다.

### private repository fetch credential

학생 활성화 코드, Autograde access/refresh token, 선택적 GitHub OAuth credential은
모두 Git fetch 권한이 아닙니다. HTTPS private repository에는 organization에 설치한
GitHub App을 사용합니다. App 권한은 `Contents: read`로 제한하고 course repository만
installation scope에 포함합니다.

```bash
install -o autograde -g autograde -m 0600 app.pem \
  /etc/autograde/cse101-github-app.pem
export AUTOGRADE_GITHUB_APP_ID=123456
export AUTOGRADE_GITHUB_APP_INSTALLATION_ID=78901234
export AUTOGRADE_GITHUB_APP_PRIVATE_KEY=/etc/autograde/cse101-github-app.pem
```

내장 adapter는 RS256 App JWT로 one-hour installation token을 요청하고 만료 5분 전에
회전합니다. 응답 권한이 `Contents: read`가 아니면 거부합니다. token은 URL, SQLite,
Git config, argument나 log에 넣지 않고 설치된 `autograde-git-askpass`를 통해 해당 Git
subprocess 환경에만 전달합니다. Git subprocess 환경은 allowlist이므로 OAuth client secret
등 다른 service credential을 상속하지 않습니다. 제공된 systemd 예제는
`ProtectHome=true`이며 GitHub.com clone URL을 HTTPS로 고정합니다.

등록 후 service를 시작하기 전에 전체 ready assignment를 점검합니다. GitHub API의
numeric ID/owner/name/private/archive/disabled 상태, clone URL, target branch와 과제 tree를
검사하며 LFS pointer, submodule, unsafe symlink, 파일/바이트 상한도 실제 snapshot과 같은
규칙으로 검증합니다. cache tracking ref 외에는 snapshot ref/archive를 남기지 않습니다.

```bash
autograde-platform assignment preflight --ready-only --jobs 4
```

하나라도 실패하면 명령과 service startup은 nonzero이며 항목별 stable error code를 JSON으로
반환합니다. 이 gate를 건너뛰는 serve 옵션은 없습니다.

## 4. 서비스와 웹 활성화 설정

기본 OAuth-free 배포에는 GitHub OAuth 환경변수가 필요하지 않습니다.
`AUTOGRADE_PUBLIC_BASE_URL`은 학생의 Windows 브라우저가 접근할 HTTPS origin으로
설정하고, 위의 collection GitHub App 변수는 별도로 제공합니다.

```bash
export AUTOGRADE_PUBLIC_BASE_URL=https://grade.example.edu

autograde-platform serve \
  --listen 127.0.0.1 \
  --port 8000 \
  --grading-runtime docker \
  --runner-image-inspect-timeout 15 \
  --git-timeout 60 \
  --repository-preflight-jobs 4 \
  --max-snapshot-files 10000 \
  --max-snapshot-bytes 1073741824 \
  --max-total-snapshot-bytes 53687091200 \
  --max-outstanding-per-student 3 \
  --max-daily-submissions-per-student 50 \
  --max-active-sessions-per-student 5 \
  --max-daily-session-issuances-per-student 20 \
  --max-retained-sessions-per-student 1000 \
  --max-refresh-rotations-per-session 2048 \
  --max-activation-attempts 5 \
  --session-list-limit 50 \
  --auth-history-retention-seconds 2592000
```

같은 `data-root`와 course 조합에서는 `serve` 또는 수동 `submission process` 하나만 실행할
수 있습니다. 프로세스 수명 동안 유지되는 non-blocking file lock을 먼저 획득한 뒤 orphan
container를 정리하므로, 중복 기동은 grader를 만들거나 다른 worker의 container를 건드리기
전에 즉시 실패합니다. 프로세스가 종료되면 kernel이 lock을 해제하므로 lock 파일 자체를
삭제하지 않습니다.

내장 server는 TLS를 종료하지 않습니다. 운영에서는 loopback에 bind하고 HTTPS reverse
proxy를 둡니다. `/activate`와 API 응답에 `no-store`, frame 차단, content type 보호 등
security header가 적용됩니다.

`GET /activate`의 유효한 연결 코드 화면은 pending authorization ID, course,
user-code HMAC, CSRF를 만료 있는 서명 `HttpOnly`/`SameSite=Lax` cookie로 묶고
확인할 device label을 표시합니다. `POST /activate/approve`는 서명 cookie와 모든
binding을 다시 검증합니다. raw 활성화 코드는 POST body에만 있고 URL/query/
cookie에 넣지 않습니다. `user_code`만 Extension의 verification URI query에 포함될 수
있으므로 reverse proxy access log에서 `/activate` query를 제외하거나 redaction합니다.

제출 응답 전에 Git fetch와 snapshot을 완료하므로 reverse proxy의 upstream timeout은
`--git-timeout`과 예상 archive 시간을 포함하도록 설정합니다. 기본 Git command 상한은
60초이며, 값을 늘릴 때는 동시성·filesystem quota와 장애 시 대기 시간도 함께 검토합니다.

reverse proxy에는 인증 endpoint별 rate limit과 요청 동시성 상한을 반드시 둡니다. 최소한
`POST /v1/device-authorizations`, token polling, `/activate`를 서로 분리해 IP와
account 기준으로 제한합니다. GitHub OAuth를 선택한 배포는 start/callback에도 별도
rate limit을 적용합니다. 초과 시 `429`와 `Retry-After`를 반환합니다.
내장 server의 body/concurrency 상한과 DB의 outstanding-device 상한은 이 외부 rate limit을
대체하지 않습니다.

device grant는 생성한 course에 결합되고 access/refresh token과 session도 그 course 범위를
상속합니다. 다른 course 서비스에서 같은 학생의 token을 제시해도 access, refresh, session,
assignment/submission/result 조회가 거부됩니다. 신규 로그인이 성공하면 같은 course/student의
기존 session/family/active refresh를 같은 transaction에서 폐기해 최신 session 하나만
남깁니다. Session 절대 수명은 최초 발급 후 4시간이며 refresh rotation으로 연장되지 않습니다.
`--max-active-sessions-per-student 5`는 기존 admission safety cap으로 유지되지만 로그인 교체
정책이 먼저 적용됩니다. UTC 일일 20개, 보존 history 1,000개가 기본 상한이며 admission은
원자적으로 처리됩니다.
목록은 active 우선 최신 50개로 제한됩니다. 폐기·만료 후 30일이 지나고 submission에서
참조하지 않는 credential history만 안전하게 GC합니다. 감사 참조 때문에 history 상한에
도달했다면 자동 삭제하지 말고 제출 보존 정책과 저장 용량을 검토한 후 retained 상한을
명시적으로 조정합니다.

학생이 장치를 잃거나 Extension credential이 꼬였을 때 raw token 없이 운영 CLI에서
해당 학생의 이 course session을 확인·폐기할 수 있습니다. reset은 session, token family와
active refresh verifier를 한 transaction으로 폐기하고 다른 학생·course에는 영향을 주지 않습니다.

```bash
autograde-platform auth sessions list --student-key 20260001
autograde-platform auth sessions revoke ses_... --student-key 20260001
autograde-platform auth sessions reset --student-key 20260001
```

학생 제출은 같은 assignment/repository/SHA의 semantic duplicate를 기존 receipt로 합치고,
기본적으로 학생별 동시 처리 3건과 UTC 일일 admission 시도 50건으로 제한합니다. push되지
않은 SHA처럼 pin에 실패한 시도도 Git 남용을 막기 위해 일일 한도를 소비합니다. 수업 규모에
맞게 위 CLI 값을 조정하되 reverse proxy rate limit과 filesystem quota는 유지합니다.

source archive 전체 quota에 도달하면 새 snapshot은 ref를 만들기 전에 실패합니다. SQLite
receipt에서 참조하지 않고 24시간 grace가 지난 archive/ref 및 terminal workspace는 기존
instructor CLI에서 기본 dry-run으로 점검한 뒤 삭제합니다.

```bash
autograde artifacts gc --quota-bytes 53687091200
autograde artifacts gc --quota-bytes 53687091200 --apply
```

GitHub OAuth가 필요한 기존 배포와의 호환성을 위해 client ID와 secret을 둘 다
설정하면 `/activate`에 선택 로그인 경로가 추가됩니다. callback URL은 다음과
정확히 맞추고 secret은 CLI argument나 repository에 넣지 않습니다.

```text
https://grade.example.edu/oauth/github/callback
```

```bash
export AUTOGRADE_GITHUB_CLIENT_ID='<oauth-client-id>'
export AUTOGRADE_GITHUB_CLIENT_SECRET='<secret>'
```

`auth approve USER_CODE --github-user-id ID`는 운영자가 표시된 device code를 직접
승인하는 recovery-only local 명령입니다. 학생에게 제공하는 기본 네트워크
인증 경로로 사용하지 않습니다.

## 5. VS Code Extension

```bash
cd extensions/vscode
npm ci
npm test
npm run package:vsix
shasum -a 256 -c SHA256SUMS
code --install-extension autograde-vscode-0.1.2.vsix
```

Node.js 22 이상이 필요합니다. `package:vsix`는 lockfile에 고정된 Microsoft `vsce`로 compile과
package를 수행합니다. `SHA256SUMS` 검증을 통과한 VSIX를 pilot 학생에게 같은
파일로 전달합니다. 개발 중에는 VS Code에서 이 폴더를 열고 `Run Autograde Extension` 구성을
실행할 수도 있습니다. 학생은 WSL window에서 VSIX를 설치하고
`autograde.serviceBaseUrl`을 서비스 HTTPS URL로 설정한 후 다음 명령을 사용합니다.

Windows 학생은 `.vsix`를 더블클릭하지 않습니다. 파일 연결로 Microsoft **Visual Studio
VSIX Installer**가 열리면 VS Code 확장이 아닌 Visual Studio 확장으로 검사되어 서명 오류가
날 수 있습니다. 먼저 PowerShell에서 받은 파일의 hash를 배포본 `SHA256SUMS`와 비교합니다.

```powershell
Get-FileHash .\autograde-vscode-0.1.2.vsix -Algorithm SHA256
Get-Content .\SHA256SUMS
```

WSL terminal에서 수업 폴더를 `code .`로 열고, 왼쪽 아래 연결 표시가 `WSL`인 **Microsoft
Visual Studio Code 창**에서 Extensions 보기의 `...` → `Install from VSIX...`로 설치합니다.
Windows 측 CLI 확인이 필요하면 설치 유형에 맞는 정확한 Microsoft VS Code 경로를 사용합니다.

```powershell
# 사용자별 설치
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --version
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.1.2.vsix --force

# 모든 사용자용 설치
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --version
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.1.2.vsix --force
```

여기서 `--force`는 기존 설치 갱신과 확인 prompt 처리용이며 서명 검증을 끄거나 우회하지
않습니다. 계속 실패하면 보안 설정을 바꾸지 말고 오류 창 제목, 전체 오류 문구와 코드,
`--version` 출력, Command Palette의 `Developer: Show Logs...` → `Shared` log를 수집합니다.
명령이 없으면 `Developer: Open Logs Folder`에서 Shared log를 찾습니다. 관리되는 학교 PC의
정책 차단이면 학생이 우회하지 말고 IT 관리자에게 extension ID
`autograde.autograde-vscode`의 allowlist 등록을 요청합니다.

1. 왼쪽 Activity Bar의 **Autograde** 아이콘을 열고 **Assignments** 영역의
   **지금 로그인** 또는 제목 표시줄의 **로그인** 선택
2. 과제 목록에서 받을 과제를 펼쳐 **과제 파일 다운로드** 선택
3. 과제 수정, commit, `git push`
4. `Autograde: Submit Assignment`
5. `Autograde: View Latest Result`
6. 자리를 떠나기 전 `Autograde: Sign Out`

과제 행 오른쪽의 다운로드 아이콘과 **Assignments** 제목 표시줄의 **과제 파일 다운로드**
버튼도 같은 동작을 실행합니다. 로그인과 다운로드는 Command Palette 없이 수행하며, 사이드바
동작을 사용할 수 없을 때만 `Autograde: Sign In`과
`Autograde: Download or Clone Assignment` 명령을 대체 경로로 사용합니다.

`Autograde: Sign In`은 5분 만료의 연결 코드를 만들고 Windows 브라우저의
`/activate`를 엽니다. 학생은 표시된 연결 코드와 LMS 등으로 개별 전달받은
이번 로그인용 새 활성화 코드를 함께 입력합니다. Browser를 열기 전 확인 창에 표시된
service origin이 운영자가 안내한 값과 같은지 확인합니다. Server가 활성화 코드를 1회 소비하고 device를
승인하면 Extension은 기존 token endpoint polling으로 access/refresh token을 받습니다.
활성화 오류를 기본 5회 반복하면 해당 device authorization이 거부되므로 Sign In을
다시 시작합니다.

Extension은 service URL만 machine scope에 유지하고 access/refresh token과 audience는
Extension Host 메모리에만 둡니다. 창 종료, Window Reload 또는 WSL 재연결 후에는 새 코드로
다시 로그인합니다. Sign Out 실패나 crash에서는 server session이 4시간 절대 만료 또는 다음
로그인 교체까지 남을 수 있습니다. Source/workspace와 browser/OS profile은 별도 공용 좌석
정리 정책의 대상입니다.

Extension은 dirty tree, detached HEAD, upstream 부재, push되지 않은 HEAD, 할당 URL 불일치,
untrusted workspace를 제출 전에 차단합니다. 서버는 이 검사를 신뢰하지 않고 session,
enrollment, assignment ownership, numeric repository ID, allowed branch와 fetched SHA를 다시
검증합니다.

제출 client timeout은 server의 60초 Git timeout보다 긴 90초입니다. timeout,
`429`, `502`, `503`, `504`와 malformed success는 최대 3회만 재시도하고 `Retry-After`를
최대 30초까지 적용합니다. idempotency key는 첫 요청 전에 현재 Extension Host 메모리에
기록하므로 같은 실행 중 timeout/일시 장애 재시도에는 같은 key를
재사용합니다. 로그인 교체·Sign Out·Extension restart 뒤에는 복구하지 않으며, server가 학생별
동일 source digest 또는 Git commit의 semantic duplicate를 기존 제출로 합칩니다. 시작 시에는
이전 Extension 버전이 `globalState`에 남긴 pending/latest 제출 metadata도 삭제합니다.

## 6. 상태와 장애 처리

정상 상태 흐름은 다음과 같습니다.

```text
accepted -> queued -> running -> graded -> published
```

- HTTP 제출이 `202`를 반환하기 전에 allowed branch tip과 요청 SHA를 비교하고 immutable
  snapshot/receipt를 만듭니다. push되지 않았거나 마감 뒤 관측된 SHA는 요청 자체를 거부합니다.
- 같은 학생·과제·repository·SHA의 재요청은 새 채점 작업 대신 기존 제출로 합쳐집니다.
- `rejected`: 이미 접수된 legacy/in-flight 학생 source가 정책을 위반함
- `infra_failed`: Git fetch, snapshot, workspace 또는 container runtime 장애
- `assessment_failed`: hidden assessment, digest 또는 grader 결과 문제; 0점으로 변환하지 않음

worker는 시작 시 non-terminal submission을 SQLite에서 복구합니다. container 실행 중 process가
중단된 `running` submission은 임의 재실행하지 않고 `worker_interrupted` infrastructure
failure로 종료하여 instructor assessment의 중복 side effect를 피합니다.

각 processor와 recovery worker는 실행 시 `--course-key`에 고정됩니다. shared database를
사용해도 해당 course의 assignment에 결합된 제출만 조회·처리하며, 다른 course ID가 queue에
들어오면 어떤 상태 전이도 하지 않습니다. 운영 단순성을 위해 기본 배포는 course별 data root와
service instance를 권장하지만, 실수로 공유된 database에서도 course 경계를 권한 경계로
유지합니다.

container에는 data-root/course에서 유도한 비밀이 아닌 service-instance label을 붙입니다.
service는 시작 시 같은 label의 orphan만 강제 제거하고, `SIGTERM`/`SIGINT`에서는 HTTP 접수를
중단한 뒤 worker와 현재 container를 정리합니다. runtime daemon에 남은 다른 Autograde
instance나 일반 container는 건드리지 않습니다. systemd `TimeoutStopSec`은 runtime 자체가
응답하지 않을 때의 최종 종료 경계입니다.

종료 시 server는 이미 accept한 client socket도 닫아 partial header/body를 보내는 연결이
handler slot을 계속 점유하지 못하게 합니다. socket 종료는 이미 synchronous exact-SHA 수집을
실행 중인 Python handler를 강제 취소하지 않습니다. 해당 handler가 snapshot과 receipt 기록을
완료했지만 worker 알림을 전달하지 못한 경우에는 다음 시작의 durable recovery가 처리하며,
응답을 받지 못한 client는 저장해 둔 같은 idempotency key로 안전하게 재시도합니다.

`after_deadline` 결과는 마감 뒤 durable recovery tick에서 자동 공개합니다. `manual` 결과만
operator가 공개합니다. `after_deadline` assignment에는 반드시 `--due-at`을 지정합니다.

```bash
autograde-platform submission publish sub_...
```

최근 제출을 수업 범위 안에서 찾고 한 건의 receipt/점수 상태를 확인할 수 있습니다. 목록은
기본 100건, 최대 500건으로 제한되며 두 명령 모두 credential, 내부 경로, rubric/diagnostics,
grader stdout/stderr 원문을 출력하지 않습니다.

```bash
autograde-platform submission list --student-key 20260001 --assignment-key lab01
autograde-platform submission list --state infra_failed --limit 50
autograde-platform submission show sub_...
```

reverse proxy 또는 service supervisor의 생존 확인에는 인증 없는 `GET /healthz`를 사용합니다.
응답은 `{"status":"ok"}`뿐이며 학생·수업·저장소 정보를 포함하지 않습니다. 이는 HTTP process
liveness 확인이고 container runtime이나 GitHub 준비 상태까지 보장하는 readiness 검사는 아닙니다.

예상치 못한 HTTP/worker 예외는 stderr에 한 줄 JSON event로 기록되어 systemd journal에서
조회할 수 있습니다. event에는 component, event name, 예외 type과 검증된 submission ID만
포함될 수 있고 예외 message, URL/query, token, 학생 코드 및 grader 출력은 기록하지 않습니다.

## 7. 검증

```bash
.venv/bin/python -m pytest -q
cd extensions/vscode && npm test
```

Python end-to-end test는 local bare Git repository와 실제 HTTP server를 사용해 OAuth 없이
활성화 코드 발급·웹 승인·device token 교환, assignment 조회, exact-SHA 제출,
snapshot/workspace, fake isolated grader 결과 게시까지
검증합니다. 별도의 25명 load gate는 실제 loopback HTTP server에 제출을 동시에 보내
25개의 exact-SHA archive/receipt가 생성되고 같은 idempotency key 재전송에서 snapshot이
늘지 않는지 확인합니다. 실제 Docker daemon을 요구하지 않는 unit test는 생성되는 container
argument와 sanitizer를 검증합니다.

코드 test만으로 GitHub organization, Linux container runtime과 VS Code Remote WSL을
대체하지 않습니다. 20명 이상 staging 배포의 필수 fault drill과 최종 판정은
[Go-live 체크리스트](go-live-checklist.md)를 따릅니다.
