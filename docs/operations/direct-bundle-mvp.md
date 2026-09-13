# Local CSV 파일럿 실행 가이드

**2026-09-11 변경:** `bundle-add`는 초안만 등록합니다. 아래 다운로드 시험 전에
`bundle-check`와 `bundle-ready`를 완료하세요. [등록·검증·공개 절차](course-assignment-management.md).

> 이 문서의 교수자 발급 활성화 코드 흐름은 loopback·호환 시험용입니다. 학생이 QR에서
> 학번과 Autograde 전용 비밀번호를 확인하고 과제를 받는 권장 외부 흐름은
> [QR 과제 수령 파일럿 가이드](qr-assignment-claim-pilot.md)를 사용하세요.

이 가이드는 한 컴퓨터에서 교수자 등록 → VS Code 다운로드 → 직접 제출 → 결과 조회 →
교수자 dashboard까지 기능을 확인하는 파일럿 절차입니다. GitHub와 Docker/Podman을 사용하지
않으며, shell 환경변수도 선언하지 않습니다. 설정과 roster는 서로 다른 local CSV 파일로
관리합니다.

## 0. 사용 범위와 중단 기준

`grading_runtime=pilot-local`은 격리된 sandbox가 아닙니다. Instructor assessment가 host에서
실행되고 assessment가 학생 프로그램을 실행하면 그 프로그램 역시 같은 host 권한과 network
환경을 사용합니다. Process timeout과 결과 크기 제한은 강한 보안 경계가 아닙니다.

다음 조건을 모두 만족할 때만 이 가이드를 사용합니다.

- 합성 제출물 또는 담당자가 내용을 검토한 신뢰된 코드만 사용
- hidden data의 기밀성을 요구하지 않는 기능 시험
- 개인 개발 장비 또는 폐기 가능한 시험 장비의 loopback 접속
- 공식 성적과 무관한 파일럿 데이터

실제 학생의 임의 코드, 시험 문제, 비밀 test 또는 개인·연구 데이터가 하나라도 포함되면
**No-Go**입니다. 이 loopback 가이드에서 LAN/인터넷 공개도 No-Go입니다. 같은 신뢰 LAN에서
합성·사전 검토 코드로 기능만 확인하는 좁은 예외는 이 파일을 수정하지 않고 별도의
[신뢰 LAN 외부 접속 파일럿](trusted-lan-pilot.md)을 따라야 합니다. 그 범위를 벗어나면
파일럿을 중단하고 배포 단계의 HTTPS와 container/microVM 격리를 먼저 구현·검증합니다.

## 1. 설치

```bash
cd autograde
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Docker/Podman, image registry와 GitHub credential은 설치하거나 설정하지 않습니다. Extension을
repository에서 다시 빌드할 때만 Node.js 22 이상이 필요합니다.

## 2. Pilot config CSV 준비

Config CSV는 `key,value` 두 열을 사용합니다. 한 key는 정확히 한 번만 나타나야 하며 알 수 없는
key, 빈 필수 값, 중복 key, 잘못된 정수·URL·runtime은 시작 전에 거부되어야 합니다.

```csv
key,value
course_key,cse101-pilot
data_root,.data
public_base_url,http://127.0.0.1:18080
listen,127.0.0.1
port,18080
grading_runtime,pilot-local
bundle_worker_count,4
```

Repository의 바로 실행 가능한 예제는 `pilot/course.csv`입니다. 상대 `data_root`는 config
CSV가 있는 디렉터리를 기준으로 해석합니다. 따라서 예제의 `.data`는 현재 shell 위치와
무관하게 `pilot/.data`가 됩니다. 이 경로는 config 디렉터리의 전용 하위 디렉터리여야 하며
config 디렉터리 자체, 상위·외부 경로와 symlink는 거부됩니다. `public_base_url`의
host/port는 `listen`/`port`와 일치시킵니다. 이 기본 loopback profile에서는
`127.0.0.1` 또는 `localhost`의 HTTP만 허용하고 HTTPS·IPv6·전체 interface bind는
거부합니다. 신뢰 LAN 시험은 `pilot/course.csv`를 바꾸지 않고 별도 profile을 사용합니다.

Config CSV에는 token, 활성화 코드 또는 학생 개인정보를 넣지 않습니다. Version control에
포함할 때는 공개 가능한 값만 사용하고, 실제 파일럿의 data path와 port가 다르면 별도의 local
파일을 사용합니다.

## 3. Roster CSV 준비

Roster는 config와 별도인 UTF-8 CSV입니다. 학생별 초기 전용 비밀번호까지 함께 등록하는
파일럿 schema는 다음과 같습니다.

```csv
student_key,active,password
s001,true,042731
s002,true,816504
s003,false,
```

- `student_key`: 학교가 이미 검증한 수강생 식별자. 빈 값과 중복은 허용하지 않습니다.
- `active`: `true` 또는 `false`. 비활성 학생은 로그인·다운로드·제출할 수 없습니다.
- `password`: active 학생마다 필수인 서로 다른 ASCII 숫자 6자리. Inactive 행은 비웁니다.

Repository의 `pilot/roster.csv`에는 위 schema로 `s001`부터 `s020`까지 20명과 로컬 자동
시험용 합성 비밀번호가 들어 있습니다. 이 값은 외부 파일럿이나 실제 학생에게 사용하지 않습니다.
실제 roster는 repository 밖 또는 ignore된 local path에 만들고 교수자만 읽을 수 있게
보호합니다. POSIX에서는 현재 교수자가 소유한 mode `0600` regular file이어야 하고,
Windows에서도 공유 폴더를 피하고 교수자 계정만 읽도록 ACL을 제한합니다.

비밀번호 원문이 든 roster 전체를 학생에게 보내거나 terminal/log에 출력하지 않습니다. 신원을
확인한 개별 채널로 각 학생에게 자신의 비밀번호 하나만 전달합니다. 파일럿에는 이름, 이메일,
점수 같은 열을 추가하지 않습니다. GitHub 호환 모드를 별도로 시험할 때만
`github_user_id`와 `github_login`을 한 쌍으로 추가합니다. Import는 전체 파일과 기존 credential
상태를 먼저 검증한 뒤 학생·수강·비밀번호 변경을 하나의 database transaction으로 반영합니다.
도중 오류나 동시 credential 변경은 전체 import를 rollback합니다. 동일 비밀번호는 no-op이고,
기존 credential과 다른 값은 `--replace-passwords` 없이 적용 전에 거부됩니다.

## 4. 초기화와 roster 반영

모든 운영 명령에 같은 config 파일을 명시합니다. 다른 terminal을 열어도 환경변수를 다시
선언할 필요가 없고, 잘못된 course/data root로 명령을 실행할 위험을 줄일 수 있습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv init
chmod 600 pilot/roster.csv
.venv/bin/autograde-platform --pilot-config pilot/course.csv student import pilot/roster.csv
```

실제 학생 파일은 위 예제 경로 대신 보호된 local roster 경로를 지정합니다. 검토한 일괄
비밀번호 회전에만 `student import ROSTER --replace-passwords`를 사용합니다. 한 학생의 분실
대응은 대화형 `student password-set STUDENT_KEY`로 처리합니다. 비밀번호 교체는 해당 학생의
기존 수령 코드와 로그인 credential을 폐기합니다.

성공 JSON에서 course와 data root, roster 처리 건수를 확인합니다. `init`이 만든 다음 파일은
학생에게 보내거나 repository에 commit하지 않습니다.

- `<data_root>/platform-auth-secret`: 학생 credential 검증용 server secret
- `<data_root>/platform-instructor-token`: dashboard Basic-auth password

두 파일과 SQLite database는 다른 OS 사용자에게 공개되지 않는 권한이어야 합니다.

## 5. 과제 등록

파일럿의 과제는 starter, instructor assessment와 선택적 data로 구성합니다. Starter만 학생에게
전달됩니다. Assessment/data는 bundle digest로 고정되지만 `pilot-local`에서는 학생 프로그램과
같은 host에서 실행될 수 있으므로 기밀 정보는 넣지 않습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-add lab01 \
  --release-id lab01-v1 \
  --title 'Lab 01 - 두 배 출력' \
  --starter examples/direct-bundle/starter \
  --assessment examples/direct-bundle/assessment \
  --data examples/direct-bundle/data \
  --max-score 10 \
  --result-policy immediate

.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-list --ready-only
```

명령 결과의 opaque `assignment_id`를 기록합니다. 검토 후 공개하려면 등록 시 `--not-ready`를
사용하고, artifact와 설정을 확인한 뒤 `bundle-ready`로 공개합니다. 공개한 release를 덮어쓰지
않고 수정본에는 새 `release_id`를 부여합니다.

`pilot-local`에는 runner image나 container runtime 설정이 없습니다. Docker image를 만들거나
`--runner-image`, `--container-runtime`을 추가하지 않습니다.

Java와 C++의 옵저버 패턴 과제를 실제 교수자 등록 예제로 사용하려면
[옵저버 패턴 과제 파일럿](observer-pattern-pilot.md)의 비공개 등록 → digest 확인 → 공개 →
학생 제출 → rubric/dashboard 검증 절차를 따릅니다.

## 6. 서버 시작과 로그인별 활성화 코드 발급

```bash
mkdir -p pilot/activation-codes
chmod 700 pilot/activation-codes

.venv/bin/autograde-platform --pilot-config pilot/course.csv auth issue s001 \
  --output pilot/activation-codes/s001-login-01.txt

.venv/bin/autograde-platform --pilot-config pilot/course.csv serve
```

활성화 코드는 **학생이 Sign In할 때마다** 새 mode `0600` 파일로 발급하고, 해당 로그인
학생에게만 전달합니다. 한 번 승인에 사용한 코드는 소비되므로 다음 로그인에 다시 사용할 수
없습니다. 다음 로그인에는 `s001-login-02.txt`처럼 새 output 경로와 새 코드를 사용합니다.
재발급하면 아직 쓰지 않은 이전 코드는 폐기됩니다. 학기 초에 하나를 장기 배포하거나 URL,
terminal history, 공용 채팅, CSV 또는 repository에 넣지 않습니다.

학생에게 access/refresh token 문자열을 받아 입력시키지 않습니다. 학생이 입력하는 것은 새
활성화 코드이며, service token은 browser/device 승인 뒤 Extension 메모리에만 발급됩니다.

서버 시작 JSON에서 다음을 확인합니다.

- `course_key`가 config와 일치
- public URL과 bind 주소가 모두 `127.0.0.1:18080`
- grading runtime이 `pilot-local`
- bundle worker 수가 config와 일치

Port가 이미 사용 중이면 config CSV의 `public_base_url`과 `port`를 같은 값으로 함께 바꿉니다.
명령행에서 일부 값만 덮어써 서로 다른 설정을 만들지 않습니다.

## 7. VS Code Extension 시험

Repository에 포함된 VSIX를 설치하기 전에 checksum을 확인합니다.

```bash
cd extensions/vscode
npm ci
npm test
shasum -a 256 -c SHA256SUMS
code --install-extension autograde-vscode-0.1.2.vsix
```

Windows에서는 `.vsix`를 탐색기에서 더블클릭하지 않습니다. 이 동작은 파일 연결에 따라
Microsoft **Visual Studio VSIX Installer**를 열어 VS Code용 패키지에 잘못된 서명 오류를
낼 수 있습니다. PowerShell에서는 설치 전에 받은 파일의 hash를 배포본 `SHA256SUMS`와
비교합니다.

```powershell
Get-FileHash .\autograde-vscode-0.1.2.vsix -Algorithm SHA256
Get-Content .\SHA256SUMS
```

WSL2 사용자는 WSL terminal에서 수업 폴더를 `code .`로 연 다음, 왼쪽 아래 연결 표시가
`WSL`인 **Microsoft Visual Studio Code 창 안에서** Extensions 보기의 `...` →
`Install from VSIX...`를 실행합니다. `code`가 다른 제품을 가리키는지 확인하거나 Windows
측 설치를 재현할 때는 설치 유형에 맞는 Microsoft VS Code CLI의 전체 경로를 사용합니다.

```powershell
# 사용자별 설치
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --version
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.1.2.vsix --force

# 모든 사용자용 설치
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --version
& "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd" --install-extension .\autograde-vscode-0.1.2.vsix --force
```

`--force`는 기존 설치 갱신과 확인 prompt 처리용이지 서명 검증 우회 옵션이 아닙니다. 계속
실패하면 보안 설정을 바꾸지 말고 오류 창 제목, 전체 오류 문구와 코드, `--version` 출력,
Command Palette의 `Developer: Show Logs...` → `Shared` log를 수집합니다. 명령이 보이지
않으면 `Developer: Open Logs Folder`에서 Shared log를 찾습니다. 학교 관리 정책에 의한
차단이면 학생이 우회하지 않고 IT 관리자에게 `autograde.autograde-vscode` extension의
allowlist 등록을 요청합니다.

VS Code 설정 `autograde.serviceBaseUrl`을 config의 `public_base_url`과 동일하게 둡니다.
Linux/macOS에서는 파일럿 전용 **수업 폴더**를 local workspace root로 열고, Windows에서는
WSL2 filesystem의 수업 폴더를 `code .`로 열어 Extension이 WSL extension host에서 실행되는지
확인합니다. 현재 root 자체가 이전에 내려받은 과제 폴더가 아니라 여러 과제를 담을 상위
폴더여야 합니다. Workspace가 없거나 root에 `.autograde/assignment.json`이 있으면 올바른 수업
상위 폴더를 먼저 열라는 오류가 나야 합니다. Multi-root workspace에서는 다운로드할 수업
root를 Quick Pick으로 선택합니다. Windows native workspace는 지원하지 않습니다.

서비스 주소 설정은 장비에 남지만 학생 token은 남지 않습니다. Extension은 access/refresh
token과 service audience를 메모리에서만 유지하므로 VS Code 창 종료, `Developer: Reload
Window`, Extension Host 재시작 또는 WSL 재연결 뒤에는 `Autograde: Sign In`과 새 활성화 코드가
필요합니다. 구버전 Extension이 SecretStorage에 남긴 token은 시작 시 제거합니다.

학생 역할 시험 순서는 다음과 같습니다.

1. 위 수업 폴더를 workspace로 열고 Workspace Trust를 확인합니다.
2. 왼쪽 Activity Bar에서 **Autograde** 아이콘을 열고, **Assignments** 영역의
   **지금 로그인** 또는 제목 표시줄의 **로그인** 버튼을 누릅니다.
3. 확인 창에 표시된 service origin의 scheme, host와 port가 config 및 교수자 안내와 정확히
   같은지 확인한 뒤 그 origin의 브라우저 열기 동작을 선택합니다. 다르면 코드를 입력하지
   않고 중단합니다.
4. 열린 `/activate` 화면에서 VS Code 연결 코드와 **이번 로그인용으로 새로 받은** 학생
   활성화 코드를 입력합니다.
5. Autograde 사이드바에 자동으로 나타난 과제를 펼쳐 **과제 파일 다운로드**를 선택해 starter를
   받습니다. 과제 행 오른쪽의 다운로드 아이콘이나 제목 표시줄의 **과제 파일 다운로드**
   버튼도 사용할 수 있습니다. Bundle은 수업 폴더 바로 아래의 새 과제 하위 폴더에 설치됩니다.
6. 창이나 workspace를 바꾸지 않고 그 하위 폴더의 파일에서 신뢰된 파일럿 답안을 작성합니다.
   Git commit/push는 필요하지 않습니다.
7. 제출할 과제 파일을 editor에서 연 뒤 `Autograde: Submit Assignment`을 실행합니다. 여러
   과제가 후보이면 Quick Pick에서 정확한 과제를 선택합니다.
8. `Autograde: View Latest Result`에서 상태, 점수와 공개 diagnostics를 확인합니다.
9. 자리를 떠나기 전에 `Autograde: Sign Out`을 실행해 server session 폐기까지 완료합니다.

2번과 5번은 Command Palette 없이 수행하는 기본 경로입니다. 사이드바 버튼이 보이지 않으면
각각 `Autograde: Sign In`, `Autograde: Download or Clone Assignment` 명령을 대체 경로로
실행합니다.

Bundle 다운로드 뒤 Extension은 새 창을 열거나 폴더를 workspace에 추가하지 않습니다. 이로써
Extension Host reload 없이 같은 메모리 session을 유지합니다. Starter 최상위에 안전한
`README.md`, `README.txt` 또는 `README`가 있으면 현재 editor preview로 열고, 없으면 OS 파일
탐색기에서 새 과제 폴더만 표시합니다. 제출 시에는 active file의 상위 marker를 우선 찾고,
이어서 workspace root와 직접 하위 폴더의 marker를 찾습니다. 선택한 과제 root만 bundle로
만들며, marker 값은 인증된 server assignment와 다시 대조합니다.

Extension은 `.git`, `.autograde`, dependency/build/cache와 특수 파일을 제외합니다. 서버는
manifest, path, digest, size, enrollment와 deadline을 다시 검증합니다. Workspace marker와
Extension이 보낸 학생 식별자는 권한 근거가 아닙니다.

### 공용·순환 좌석 확인

새 로그인이 완료되면 같은 course/student의 이전 server session은 폐기되고 최신 session
하나만 남습니다. Session은 최초 로그인에서 4시간의 절대 만료 시각을 가지며 token refresh로
연장되지 않습니다. 따라서 이전 자리의 token이 아직 메모리에 있더라도 새 로그인 직후에는
API에서 거부되어야 합니다.

정상 흐름에서는 Sign Out을 먼저 실행합니다. 네트워크 단절로 server 폐기가 실패했을 때
Extension에서 local-only 로그아웃을 선택하거나 VS Code가 crash하면 메모리 token은 사라지지만
server session은 즉시 폐기됐다고 확인할 수 없습니다. 이때는 4시간 절대 만료 또는 다음
로그인의 session 교체에 의존합니다.

메모리 전용 token은 공용 PC 정리를 대신하지 않습니다. 다음 학생에게는 아래 항목이 남을 수
있으므로 학교 실습실 정책으로 별도 처리합니다.

- 내려받은 과제 source, build 결과와 `.autograde` workspace marker
- VS Code 최근 폴더와 Workspace Trust, 서비스 URL 설정
- Browser history, cookie/autofill 및 로그인된 browser profile
- 같은 OS 사용자 profile의 terminal history, clipboard와 파일

Autograde의 pending/latest 제출 metadata, Output과 diagnostics는 로그인 교체와 Sign Out에서
삭제되고 이전 버전의 `globalState`도 시작 시 정리됩니다. 그러나 workspace marker에는 학생
소유권이 없고 source는 자동 삭제하지 않으므로, 같은 OS/WSL 계정에서 신뢰된 이전 workspace를
다음 학생에게 넘기는 운영은 허용하지 않습니다.

이 파일럿의 loopback HTTP는 Autograde server와 Extension을 신뢰할 수 있는 같은 장비에서만
사용합니다. `127.0.0.1` 또는 `localhost`를 중앙 server/LAN 주소로 바꾸어 여러 좌석에서
접속하는 구성은 이 가이드의 범위가 아닙니다. 같은 신뢰 LAN의 합성·사전 검토 코드로 별도
단기 시험을 수행할 때는 이 설정을 수정하지 말고
[신뢰 LAN 외부 접속 파일럿](trusted-lan-pilot.md)의 분리된 config와 이중 opt-in 절차를
사용합니다.

## 8. 교수자 dashboard

`http://127.0.0.1:18080/instructor`를 열고 다음 credential을 사용합니다.

- 사용자 이름: `instructor`
- 암호: `pilot/.data/platform-instructor-token` 파일 내용

Dashboard에서 active roster × assignment의 다운로드, 제출, 최신 상태, 점수와 시각을
확인합니다. 학생 bearer token으로 instructor endpoint에 접근할 수 없어야 합니다.

다음 항목을 표본 점검합니다.

- 미제출 학생이 제출 완료로 표시되지 않음
- 동일 bundle 재시도가 중복 성적으로 늘지 않음
- 다른 학생이 submission/result ID를 알아도 조회할 수 없음
- assessment 오류는 0점이 아니라 `assessment_failed`로 구분됨
- 운영 오류는 `infra_failed`로 구분됨

## 9. 자동 시험과 파일럿 판정

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/integration/test_bundle_platform_25_load.py
.venv/bin/python -m pytest -q \
  tests/unit/test_platform_service.py::test_latest_login_replaces_abandoned_shared_seat_session \
  tests/unit/test_platform_service.py::test_refresh_rotation_cannot_extend_the_absolute_lab_session \
  tests/unit/test_platform_state.py::test_refresh_rotation_cannot_move_the_original_session_deadline

cd extensions/vscode
npm test
npm audit --omit=dev
shasum -a 256 -c SHA256SUMS
```

위 세 server 집중 시험은 이전 좌석 session 교체와 4시간 deadline 비연장을 검증합니다.
Extension test는 token 비지속, 구버전 SecretStorage 정리, Sign Out과 verification origin 차단을
검증합니다. 실제 source/browser/OS profile 정리는 자동 시험이 아니라 공용 좌석 수동 drill로
확인합니다.

25명 integration test는 HTTP upload, SQLite admission, bundle CAS, worker, result ownership과
dashboard 집계를 결정적 fake grader로 검증합니다. 이것은 20명 이상 상태 처리의 regression
gate이지 실제 host 격리나 production capacity 인증이 아닙니다. Docker smoke test는 이번
파일럿의 필수 시험에서 제외합니다.

자동 시험 후 [파일럿 Go/No-Go 체크리스트](go-live-checklist.md)의 수동 시나리오를 같은
config/roster로 실행하고 기록을 남깁니다.

## 10. 종료와 보존

서버는 `Ctrl-C`로 정상 종료하고 worker가 제한 시간 안에 drain되는지 확인합니다. 제출 원장과
결과를 보존하려면 config, roster, data root, activation-code 전달 기록을 접근 제한된 위치에
함께 보관합니다. 파일럿 데이터를 버릴 때는 exact data root를 확인한 후 조직의 보안 삭제
정책을 따릅니다.

공용 좌석을 반납할 때에는 학생이 Sign Out 성공을 확인하고, 운영자가 정한 방법으로 학생
workspace와 browser/OS profile 흔적을 정리합니다. 삭제 범위는 Autograde가 자동 결정하지
않으므로 다른 학생 파일이나 장비 공용 도구를 함께 지우지 않도록 실습실의 명시된 경로만
대상으로 합니다.

## 배포 단계에 남긴 계획

실제 수업 배포 전에는 다음을 별도 단계로 구현하고 검증합니다.

1. API/상태 서비스와 채점 worker의 OS 또는 host 분리
2. Disposable Docker/Podman container 또는 microVM 채점
3. Non-root, read-only root/mount, network-off, capability 제거
4. CPU, memory, PID, wall-clock, stdout/stderr와 전체 queue 상한
5. Immutable runner image digest와 배포 전 image provenance 검증
6. 학생 process에서 assessment/data를 숨기는 confidential-test child sandbox
7. TLS reverse proxy, rate limit, backup/restore, audit와 실제 대상 OS 20명 drill

이 계획이 구현·검증되기 전까지 `pilot-local`을 실제 학생 채점으로 확장하지 않습니다.
