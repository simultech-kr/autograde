# Autograde

교수자가 등록한 실습을 학생이 VS Code Extension으로 내려받고, 제출한 결과를 로컬
서버에서 채점해 다시 보여주는 파일럿 MVP입니다. 기본 흐름에는 GitHub 계정, GitHub OAuth,
GitHub App, container registry가 필요하지 않습니다. Linux와 macOS는 native workspace,
Windows에서 Linux 실습을 할 때는 WSL2 workspace를 사용합니다.

실습 기본 언어는 **C17·C++17**이며 Java/JDK는 필요하지 않습니다. Windows 시스템 프로그래밍은
VS2022 Community, Linux 시스템 프로그래밍은 VS Code/Linux·WSL2를 목표로 합니다.
VS2022/VS2026용 [별도 Visual Studio 확장](extensions/visualstudio/README.md)의 소스와 빌드 절차를
제공합니다. C# 컴파일·서버 통신은 검증했으며 Windows VSIX 패키징·설치 시험은 남아 있습니다.
Windows 원격 채점 작업자는 후속 구현입니다.

VS Code 0.4.0 / Visual Studio 0.2.0 소스에는 [제출 기록·이전 코드 복원](docs/operations/submission-history-mvp.md)이
추가되었습니다. 서버 접수본을 학생별로 조회하고 현재 작업을 덮어쓰지 않는 새 폴더에 복원합니다.

## 현재 권장 실행 흐름

웹 시작 화면 `/`에서 **교수자 포털**(`/instructor`)과 **학생 포털**(`/student`)을 선택합니다.
교수자는 담당 수업 카드에서 과제·학생·실습 교실·평가·자료로 이동하고, 학생은 교과목 인증 후
내 수업에서 과제 수령·본인 제출 현황·개인 자료를 확인합니다. 최신 제출 점수와 이전 공개 최고점은
구분하여 표시합니다. [포털 운영 안내](docs/operations/role-portals.md),
[정보구조·고도화 설계](docs/architecture/role-portals.md)를 참고하세요.

학생은 `/student/register`에서 **학번·본인 비밀번호·교과목**으로 가입합니다. 교수자가 **가입·수강 명단**에
올린 학번 목록과 일치하면 수업에 자동 연결되며 수동 승인은 필요하지 않습니다. 이후 `/student/login`에서
로그인하여 `/student/account`의 본인 수업으로 이동합니다. 명단 CSV에는 비밀번호를 넣지 않습니다.
[회원가입·명단 연결 안내](docs/operations/student-membership.md)를 확인하세요.
교과목 정보는 SQLite에 저장하며 **수업 설정 → 삭제 범위 확인**에서 보관을 거치지 않고 영구 삭제할 수 있습니다.
[교과목 등록·수정·영구 삭제](docs/operations/course-permanent-deletion.md)를 참고하세요.

소규모 수업 개념연구용 **Autograde IDE**는 Code-OSS에 기존 VS Code 확장을 내장하는 방식으로
진행합니다. SQLite·서버 API는 유지하며, PostgreSQL은 향후 Docker 기반 운영 단계에서 검토합니다.
[설계와 지원 경계](docs/architecture/autograde-ide.md), [개발용 준비·실행·검증](docs/operations/autograde-ide-poc.md)을 확인하세요.
1단계는 학습 흐름 통합이며 AI 강제 차단이나 학생 배포용 서명 설치 파일을 제공하는 단계가 아닙니다.

Mac IDE **0.5.18**은 선택형 **원격 Linux C/C++/Python 실습**을 제공합니다. 학생 PC에 컴파일러·Python·
Docker를 설치하지 않고 현재 코드를 서버에 저장한 뒤 실행하고, 실습 종료 시 원격 공간을
정리합니다. 기관 서버에는 별도 실행 관리자와 승인 이미지 설정이 필요하며 SQLite와 공식 채점은
유지합니다. Python은 진입 파일 1개와 프로젝트 모듈·표준 라이브러리를 지원하며 추가 패키지 설치는 제공하지 않습니다.
Java·디버거는 후속입니다. 운영 서버에 자동 적용되지 않으므로
[원격 실습 설치·사용·검증 안내](docs/operations/remote-learning-runtime.md)를 확인하세요.

교수자 웹의 **학생별 자료 배포**에서 `학번.pdf` 또는 `학번.zip`을 개별·일괄 등록할 수 있습니다.
학생은 학번·회원 비밀번호 또는 기존 수업 전용 비밀번호로 로그인하여 자신에게 배정된 파일만 받습니다.
성적과 무관한 학생별 자료는 공개 후에도 안내·기간·파일·배정 대상을 바로 수정할 수 있으며, 교체 전 파일과 요청 이력은 보존합니다. 여러 변경을 검토한 뒤 한 번에 공개하려면 **수정본 만들기**도 사용할 수 있습니다.
[자료 준비·배포·수령·서버 반영 절차](docs/operations/student-material-distribution.md)를 확인하세요.

[Git 기반 콘텐츠 관리·학생 로컬 동기화](docs/architecture/git-content-cms.md)의 1차 구현은
기존 교수자 웹 `수업 → 콘텐츠`에서 자료를 즉시 공개하고, VS Code/Autograde IDE 0.5.9의
`강의자료 업데이트`로 변경된 파일만 캐시에 받은 뒤 별도 폴더에 저장합니다. 기존 개인 PDF/ZIP
배포와 답안 파일은 그대로 유지합니다. 예약·자동 수신·Next.js 콘텐츠 관리는 후속입니다.

교수자 **실습 교실**은 학생 카드·상세 팝업으로 IDE 연결, 최근 활동, 공식 제출·채점 상태를
실시간 조회합니다. 학생별 **IDE 요청**에서 공지·과제 열기·자료 동기화·환경 점검·저장 제출을
요청하고 학생 확인 및 처리 결과를 볼 수 있습니다. 학생 수락 전 실행하지 않으며,
기존 CMS와 Next.js 및 VS Code/Autograde IDE **0.5.11**에서 사용할 수 있습니다.
[실습 교실 사용·서버 적용·상태 해석](docs/operations/classroom-live.md)을 확인하세요.

VS Code/Autograde IDE **0.5.12**의 **내 학습내용 내보내기**에서 작성 중인 코드·이전 제출본·
강의자료를 골라 폴더나 ZIP으로 보관할 수 있습니다. 전용 실행기의 공용 실습 모드는 매번 새
작업 공간을 만들고 앱 종료 후 정리하며, 공간 밖에 내보낸 파일은 유지합니다.
[내보내기·공용 좌석 실행·종료 절차](docs/operations/student-learning-export.md)를 확인하세요.

관리자 조회 화면은 선택적으로 **Next.js `/app`**에서 이용할 수 있습니다. 교과목·분반 선택,
제출 현황과 이번 점수·이전 최고점 조회를 제공하며 과제·학생 변경은 기존 관리 화면을 유지합니다.
기본 비활성화 상태이고 `git pull` 외에 별도 웹 빌드와 서버 설정이 필요합니다.
[관리자 파일럿 Git 업데이트·활성화·복귀 안내](docs/operations/nextjs-pilot.md)를 따릅니다.

학생 웹·교수자 관리 화면을 구분하고, VS Code **0.4.1**부터 `내가 수락한 실습`에는 현재 수령
코드로 수락한 과제만 표시합니다. [화면별 범위와 업데이트 안내](docs/operations/student-instructor-views.md).

일반 학생 제출을 위한 [배포 준비 1차](docs/operations/student-deployment-preparation.md)를 추가했습니다.
명시적인 `--isolated` 모드로 HTTPS·OCI staging을 실행하고 `/readyz`로 DB·저장소·작업자 상태를 확인합니다.
기존 파일럿 기본값은 유지하며 **실제 격리·성적 무결성·인증 검증 전 일반 학생 공개는 아직 No-Go**입니다.

과제 등록은 이제 **초안 → 모범답안·오답 검증 → 명시적 공개** 순서입니다.
기존 과제의 제출 ID를 유지하는 마감 연장도 제공합니다.
[업데이트·교수자 명령 안내](docs/operations/course-assignment-management.md)를 먼저 확인하세요.

이미 제출된 과제도 **설명 수정·이력**에서 안내문을 보완할 수 있습니다.
같은 과제 ID와 기존 점수를 유지하고, 학생에게는 별도 최신 설명을 제공합니다.
[설명 버전·서버/확장 업데이트 안내](docs/operations/assignment-document-revisions.md).

**학생 웹 20010 → 회원가입·로그인 → 교과목 명단 자동 연결 → 수령 코드 발급 →
VS Code API 20000 → 다운로드·제출·결과 확인**입니다. 교과목은 `come3105`, `come2201`입니다.
[독립 웹 파일럿 실행 안내](docs/operations/independent-web-pilot.md)에 설치, 명단·과제 등록,
두 포트 동시 실행과 외부 HTTPS 설정을 정리했습니다. VS Code 확장은 0.3.0 이상을 사용합니다.
아래 기존 단일 교과목 실행 절차와 활성화 로그인은 호환 모드 참고용입니다.

첫 서버 실행은 설정 CSV 옆의 `student_roster.csv`에서 명단을 한 번 초기화합니다.
[Hello World 과제](examples/hello-world/README.md)로 Linux/WSL2 제출·채점과
Windows/VS2022·VS2026의 로컬 빌드를 시험할 수 있습니다. Windows 원격 채점은 후속 구현입니다.

이번 파일럿은 운영 설정용 shell 환경변수를 선언하지 않습니다. 운영자가 관리하는 local CSV
서버 설정은 로컬 CSV를 사용하고 교과목·학생 계정·수강 연결은 SQLite에서 관리합니다.

- pilot config CSV: course, data directory, service URL/listen/port, network 접근 mode,
  채점 방식과 worker 수
- 새 회원 흐름의 수강 명단 CSV: 교수자 웹에서 `student_key`를 등록하며 비밀번호는 불필요
- 기존 roster CSV 가져오기: `student_key`, 수강 활성 상태와 학생별 숫자 6자리 전용 비밀번호를 사용하는 호환 경로

새 학생은 본인이 정한 8~128자 회원 비밀번호로 인증하고 교수자가 등록한 수강 명단과 연결된
교과목만 이용합니다. 기존 6자리 수업 비밀번호도 유지하며 기존 학생의 회원 계정 연결은 수업 인증 후 진행합니다.
학생은 10분 동안 한 번만 쓸 수 있는 과제 수령 코드를 VS Code에 입력합니다. 공유 링크에는 공개 URL만 사용합니다.
기존 파일럿 roster의 `password` 열만 초기 비밀번호 원문을 담는 의도된 예외이며, URL·log·명령
출력에는 비밀번호나 수령 코드를 남기지 않습니다. 학교 포털 비밀번호를 이 서비스에 입력해서는
안 됩니다.

VS Code에는 서비스 주소만 유지하고 access/refresh token은 Extension Host 메모리에만 둡니다.
따라서 창 종료, `Developer: Reload Window`, WSL 재연결 뒤에는 새 수령 코드로 다시 연결해야
합니다. Starter와 제출물은 결정적 bundle로 전달하므로 학생별 Git repository도 필요하지
않습니다. 기존 교수자 발급 1회용 활성화 코드 login은 장애 대응용 호환 경로로 남아 있습니다.

> **안전 경계:** `pilot-local` 채점은 assessment가 학생 프로그램을 host process로 실행하는
> 기능 검증용 경로입니다. Container, VM, 별도 host, network 차단 같은 강한 격리를 제공하지
> 않습니다. 따라서 합성 제출물과 신뢰된 참여자만 사용하는 non-confidential 파일럿에
> 한정합니다. 실제 학생의 임의 코드, 비밀 테스트, 공식 성적 또는 외부 공개 서비스에는
> **No-Go**입니다. Docker/Podman 또는 microVM 채점은 배포 단계 계획이며 이번 MVP 실행
> 범위가 아닙니다.

## 파일럿 범위

- local pilot config CSV 한 파일로 모든 운영 명령의 설정을 고정
- SQLite 교과목·회원 계정, 비밀번호 없는 수강 명단 CSV와 자동 수강 연결
- 교과목 삭제 범위 확인·키 입력 후 영구 삭제, 재시작 시 삭제 교과목 재생성 방지
- 보호된 UTF-8 roster CSV 사전 검증 및 학생별 6자리 비밀번호와 `student_key` 수강 등록
- 교과목별/학생별 집계 조회와 학생별 전용 비밀번호 설정·재설정
- GitHub 없는 starter bundle 다운로드와 direct submission
- 비밀 없는 과제 접속 링크, 비밀번호 확인과 10분·1회용 과제 수령 코드
- 공용 좌석을 위한 메모리 전용 Extension token과 최신 로그인 1-session 정책
- Linux/macOS native 및 Windows WSL2 VS Code Extension
- VS Code 시작부터 약 30초마다 갱신되는 서버 도달 가능 상태 표시
- SQLite 제출 원장, 비동기 채점 worker, 결과 조회
- 교수자용 roster × assignment read-only dashboard
- 20명 이상을 가정한 동시 제출 regression test
- 기본 loopback HTTP, 외부 HTTPS reverse proxy와 명시적 이중 opt-in의 신뢰 LAN HTTP 시험

다음은 파일럿 실행 범위가 아닙니다.

- Docker/Podman, image registry, Kubernetes 또는 microVM 배포
- 실제 학생의 신뢰할 수 없는 코드 실행과 confidential hidden test
- 격리 없는 production 인터넷 운영, 공용·개방 LAN, 다중 서버와 고가용성
- GitHub repository 생성·권한 조정·PR 기반 제출
- LMS 성적 반영과 이의 신청 workflow

기존 Git exact-SHA 수집과 PyJevSim 주기 실행 코드는 호환 경로로 남아 있지만 direct-bundle
파일럿에서는 사용하지 않습니다. PyJevSim은 향후 instructor repository를 주기적으로
수집할 때만 사용합니다.

## 설치

Python 3.10 이상이 필요합니다. Extension을 직접 빌드할 때는 Node.js 22 이상,
선택적 Next.js 관리자 웹을 빌드·실행할 때는 Node.js 24 LTS가 필요합니다.
파일럿 실행에 Docker/Podman은 설치하지 않아도 됩니다.

```bash
cd autograde
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## CSV 기반 파일럿 시작

환경변수 대신 운영자가 만든 config CSV를 모든 명령에 명시합니다. Repository에는
[`pilot/course.csv`](pilot/course.csv)와 20명
[`pilot/roster.csv`](pilot/roster.csv)가 바로 실행 가능한 예제로 포함됩니다. 이 파일의 학번과
비밀번호는 로컬 자동 시험만을 위한 합성 값이므로 외부 파일럿이나 실제 학생에게 사용하지
않습니다. 실제 roster는 repository 밖 또는 ignore된 local path에 두고 교수자만 읽을 수 있게
보호합니다. 상대
`data_root`는 config 파일 디렉터리의 전용 하위 경로여야 하므로 `.data`는
`pilot/.data`가 됩니다. 전체 절차는
[local CSV 파일럿 실행 가이드](docs/operations/direct-bundle-mvp.md)에 있습니다.
[C/C++ Hello World](examples/hello-world/README.md)로 시작하고, 설계 패턴 실습은
[C++ 옵저버 과제](examples/observer-cpp/README.md)를 사용합니다.
같은 신뢰 LAN의 다른 컴퓨터에서 HTTP로 짧게 기능을 시험할 때만
[신뢰 LAN 외부 접속 파일럿](docs/operations/trusted-lan-pilot.md)의 별도 config와 위험 수락
절차를 따릅니다.
웹 접속 링크와 전용 비밀번호를 사용하는 권장 외부 HTTPS 흐름은
[웹 과제 수령 파일럿 가이드](docs/operations/web-assignment-claim-pilot.md)를 따릅니다.

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
student_key,active,password
s001,true,042731
s002,false,
```

`password`는 active 학생마다 서로 다른 ASCII 숫자 6자리여야 하고, inactive 행은 비워야
합니다. 비밀번호 원문이 들어 있는 roster 전체를 학생에게 배포하지 말고, 신원을 확인한 개별
채널로 각 학생에게 자신의 비밀번호 하나만 전달합니다. POSIX에서는 파일 소유자가 현재
교수자이고 mode가 정확히 `0600`이어야 import할 수 있습니다.

명령 구조는 다음과 같습니다. 옵션의 최종 형태는 설치된 CLI의 `--help`를 기준으로 합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv init
chmod 600 pilot/roster.csv
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

같은 roster를 다시 import하면 동일한 비밀번호는 변경하지 않습니다. 기존 DB와 CSV의 비밀번호가
다르면 기본적으로 안전하게 실패하며, 의도적으로 전체 변경을 검토한 경우에만 import 명령에
`--replace-passwords`를 추가합니다. 개별 분실 대응은 `student password-set STUDENT_KEY`를
사용합니다.

외부 HTTPS 파일럿의 각 학생은 다음 사용자 흐름만 수행합니다.

1. 브라우저에서 과제 접속 링크를 열고 HTTPS domain·교과목·과제를 확인
2. 회원가입·로그인 후 수강 명단과 연결된 수업에서 10분·1회용 수령 코드 발급. 기존 6자리 수업 비밀번호도 사용 가능
3. 전용 수업 폴더를 VS Code workspace로 열고 신뢰
4. 설정의 서비스 origin이 교수자가 안내한 값과 같은지 확인
5. 하단의 **Autograde 서버: 연결됨** 표시를 확인
6. 왼쪽 **Autograde** 아이콘에서 **수령 코드 입력 및 다운로드**를 눌러 코드 입력
7. 현재 창에 생성된 과제 하위 폴더에서 문제 해결
8. 과제 행에서 **제출** 후 **채점 결과 보기**
9. 자리를 떠나기 전에 **로그아웃** 실행

수령과 다운로드에는 Command Palette가 필요하지 않습니다. 사이드바 버튼이 보이지 않거나
키보드로 실행해야 할 때만 `Autograde: Enter Assignment Claim Code and Download` 명령을
대체 경로로 사용합니다. 기존 `Autograde: Sign In`은 호환 login 경로입니다.
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
.venv/bin/python -m pytest -q tests/integration/test_assignment_claim_25_load.py

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
