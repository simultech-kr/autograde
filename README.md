# Autograde

교수자가 등록한 실습을 학생이 VS Code Extension으로 내려받고, 제출한 결과를 로컬
서버에서 채점해 다시 보여주는 파일럿 MVP입니다. 기본 흐름에는 GitHub 계정, GitHub OAuth,
GitHub App, container registry가 필요하지 않습니다. Linux와 macOS는 native workspace,
Windows는 WSL2 workspace를 사용합니다.

이번 파일럿은 운영 설정용 shell 환경변수를 선언하지 않습니다. 운영자가 관리하는 local CSV
두 개를 입력으로 사용합니다.

- pilot config CSV: course, data directory, service URL/listen/port, network 접근 mode,
  채점 방식과 worker 수
- roster CSV: `student_key`와 수강 활성 상태

학생 인증은 active roster와 **로그인할 때마다 새로 발급하는** 학생별 1회용 활성화 코드에
기반합니다. VS Code에는 서비스 주소만 유지하고 access/refresh token은 Extension Host
메모리에만 둡니다. 따라서 창 종료, `Developer: Reload Window`, WSL 재연결 뒤에는 새 코드로
다시 로그인해야 합니다. Starter와 제출물은 결정적 bundle로 전달하므로 학생별 Git
repository도 필요하지 않습니다.

학생이 매번 입력하는 값은 access/refresh token 문자열이 아니라 교수자가 새로 발급한 1회용
활성화 코드입니다. Service token은 browser/device 승인 뒤 내부적으로 발급·회전합니다.

> **안전 경계:** `pilot-local` 채점은 assessment가 학생 프로그램을 host process로 실행하는
> 기능 검증용 경로입니다. Container, VM, 별도 host, network 차단 같은 강한 격리를 제공하지
> 않습니다. 따라서 합성 제출물과 신뢰된 참여자만 사용하는 non-confidential 파일럿에
> 한정합니다. 실제 학생의 임의 코드, 비밀 테스트, 공식 성적 또는 외부 공개 서비스에는
> **No-Go**입니다. Docker/Podman 또는 microVM 채점은 배포 단계 계획이며 이번 MVP 실행
> 범위가 아닙니다.

## 파일럿 범위

- local pilot config CSV 한 파일로 모든 운영 명령의 설정을 고정
- UTF-8 roster CSV 사전 검증 및 `student_key` 수강 등록
- GitHub 없는 starter bundle 다운로드와 direct submission
- 로그인마다 새로 발급·소비하는 학생별 1회용 활성화 코드와 browser/device authorization
- 공용 좌석을 위한 메모리 전용 Extension token과 최신 로그인 1-session 정책
- Linux/macOS native 및 Windows WSL2 VS Code Extension
- SQLite 제출 원장, 비동기 채점 worker, 결과 조회
- 교수자용 roster × assignment read-only dashboard
- 20명 이상을 가정한 동시 제출 regression test
- 기본 loopback HTTP와 명시적 이중 opt-in을 적용한 신뢰 LAN 단기 HTTP 기능 시험

다음은 파일럿 실행 범위가 아닙니다.

- Docker/Podman, image registry, Kubernetes 또는 microVM 배포
- 실제 학생의 신뢰할 수 없는 코드 실행과 confidential hidden test
- 인터넷 공개, 공용·개방 LAN 운영, TLS reverse proxy, 다중 서버와 고가용성
- GitHub repository 생성·권한 조정·PR 기반 제출
- LMS 성적 반영과 이의 신청 workflow

기존 Git exact-SHA 수집과 PyJevSim 주기 실행 코드는 호환 경로로 남아 있지만 direct-bundle
파일럿에서는 사용하지 않습니다. PyJevSim은 향후 instructor repository를 주기적으로
수집할 때만 사용합니다.

## 설치

Python 3.10 이상이 필요합니다. Extension을 직접 빌드할 때만 Node.js 22 이상이 필요합니다.
파일럿 실행에 Docker/Podman은 설치하지 않아도 됩니다.

```bash
cd autograde
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## CSV 기반 파일럿 시작

환경변수 대신 운영자가 만든 config CSV를 모든 명령에 명시합니다. Repository에는
[`pilot/course.csv`](pilot/course.csv)와 20명
[`pilot/roster.csv`](pilot/roster.csv)가 바로 실행 가능한 예제로 포함됩니다. 상대
`data_root`는 config 파일 디렉터리의 전용 하위 경로여야 하므로 `.data`는
`pilot/.data`가 됩니다. 전체 절차는
[local CSV 파일럿 실행 가이드](docs/operations/direct-bundle-mvp.md)에 있습니다.
Java와 C++로 같은 설계 개념을 출제하고 채점 결과까지 확인하는 예제는
[옵저버 패턴 과제 파일럿](docs/operations/observer-pattern-pilot.md)을 따릅니다.
같은 신뢰 LAN의 다른 컴퓨터에서 HTTP로 짧게 기능을 시험할 때만
[신뢰 LAN 외부 접속 파일럿](docs/operations/trusted-lan-pilot.md)의 별도 config와 위험 수락
절차를 따릅니다.

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

Roster는 별도 파일입니다.

```csv
student_key,active
s001,true
s002,true
```

명령 구조는 다음과 같습니다. 옵션의 최종 형태는 설치된 CLI의 `--help`를 기준으로 합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv init
.venv/bin/autograde-platform --pilot-config pilot/course.csv student import pilot/roster.csv

.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-add lab01 \
  --release-id lab01-v1 \
  --title 'Lab 01' \
  --starter examples/direct-bundle/starter \
  --assessment examples/direct-bundle/assessment \
  --data examples/direct-bundle/data \
  --max-score 10

mkdir -p pilot/activation-codes
chmod 700 pilot/activation-codes
.venv/bin/autograde-platform --pilot-config pilot/course.csv auth issue s001 \
  --output pilot/activation-codes/s001-login-01.txt
.venv/bin/autograde-platform --pilot-config pilot/course.csv serve
```

각 학생은 다음 사용자 흐름만 수행합니다.

1. 전용 수업 폴더를 VS Code workspace로 열고 신뢰
2. 왼쪽 Activity Bar에서 **Autograde** 아이콘을 열고 **Assignments** 영역의
   **지금 로그인** 또는 제목 표시줄의 **로그인** 클릭
3. 설정에 표시된 서비스 origin이 교수자가 안내한 값과 같은지 확인
4. 이번 로그인용으로 새로 전달받은 활성화 코드로 브라우저 연결 승인
5. 자동으로 나타난 과제를 펼쳐 **과제 파일 다운로드** 선택
6. 현재 창에 생성된 과제 하위 폴더에서 문제 해결
7. `Autograde: Submit Assignment`
8. `Autograde: View Latest Result`
9. 자리를 떠나기 전에 `Autograde: Sign Out` 실행

로그인과 과제 다운로드에는 Command Palette가 필요하지 않습니다. 사이드바 버튼이 보이지
않거나 키보드로 실행해야 할 때만 각각 `Autograde: Sign In`과
`Autograde: Download or Clone Assignment` 명령을 대체 경로로 사용합니다.
과제 행 오른쪽의 다운로드 아이콘과 **Assignments** 제목 표시줄의 **과제 파일 다운로드**
버튼도 같은 동작을 실행합니다.

Bundle 과제는 현재 수업 workspace 바로 아래에 내려받고 새 창을 열거나 workspace를
reload하지 않으므로 메모리 로그인 상태가 유지됩니다. 안전한 최상위 README가 있으면 editor
preview로 열고, 없으면 파일 탐색기에서 과제 폴더를 표시합니다.

서비스 주소는 다음 학생에게도 남을 수 있지만 credential은 남기지 않습니다. 명시적
로그아웃 없이 VS Code가 종료되면 메모리 token은 사라지지만 server session은 절대 수명
만료 또는 다음 로그인으로 교체될 때까지 남을 수 있습니다. 다운로드한 source/workspace와
브라우저·OS 사용자 profile은 별도 잔여물입니다. 공용 좌석에서는 로그아웃 후 학교의
workspace 삭제 및 browser/profile 정리 정책도 함께 수행합니다. 기본 Loopback HTTP는
신뢰된 동일 장비 파일럿 전용입니다. LAN 시험은 주소만 바꾸는 방식이 아니라 server의
`external_access_mode=insecure-http`와 Extension의
`autograde.allowInsecureHttpPilot=true`를 모두 켜고 로그인 때마다 별도 위험 확인을 거칩니다.
암호화가 생기는 것은 아니므로 합성·사전 검토 데이터에만 사용합니다.

기본 loopback profile의 Dashboard는 config의 `public_base_url` 뒤에 `/instructor`를 붙인
주소에서 확인합니다. 사용자 이름은 `instructor`, 암호는 config의 `data_root`에 생성된
`platform-instructor-token` 파일 내용입니다. `insecure-http` LAN profile에서는 Basic
credential을 평문으로 받지 않도록 dashboard endpoint가 비활성화되며 교수자 CLI로만 결과를
확인합니다.

## 검증

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/integration/test_bundle_platform_25_load.py

cd extensions/vscode
npm test
npm audit --omit=dev
shasum -a 256 -c SHA256SUMS
```

자동 시험의 fake grader와 `pilot-local`은 파일럿 기능을 검증하기 위한 것입니다. 이 시험의
성공은 임의 코드에 대한 host 격리, network 격리 또는 production 안전성을 증명하지 않습니다.
실제 파일럿 진입 판정은 [파일럿 Go/No-Go 체크리스트](docs/operations/go-live-checklist.md)를
따릅니다.

## 배포 단계 계획

실제 수업으로 확장할 때 채점기를 API 서버와 분리하고, disposable Linux Docker/Podman
worker 또는 microVM에서 실행합니다. 최소한 non-root user, read-only root filesystem,
network 차단, capability 제거, CPU/memory/process/time/output 상한, immutable runner digest,
job별 workspace 폐기를 적용합니다. Confidential test가 필요하면 학생 process가 assessment와
정답 데이터를 읽을 수 없는 별도 child sandbox가 필요합니다.

GitHub repository 호환 모드를 선택하는 경우에만 read-only GitHub App, exact-SHA fetch와
PyJevSim 주기 collection을 구성합니다. 이는 학생 인증과 별도 credential 영역입니다.

상세 설계는 [MVP 요구사항](docs/requirements/mvp.md),
[학생 플랫폼 요구사항](docs/requirements/student-platform.md),
[학생 플랫폼 아키텍처](docs/architecture/student-platform.md),
[보안 기준](docs/operations/security.md)에서 확인할 수 있습니다.
