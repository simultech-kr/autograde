# 기관 설치형·클라우드 설치형 배포

`scripts/deploy/`의 실행기는 새 설치를 준비하고 Python 서버·Next.js를 실행한다.
두 배포 유형 모두 **기관별 독립 설치와 SQLite**를 사용한다. 클라우드는 실행 위치가 Ubuntu VM인
구성이며, 여러 기관이 하나의 DB를 공유하는 SaaS 모드가 아니다.

## 지원 범위

| 실행기 | 기관 설치형 서버 | 클라우드 설치형 대상 |
|---|---|---|
| `ubuntu.sh` | Ubuntu + Linux Docker Engine | SSH로 접속하는 Ubuntu 서버 |
| `macos.sh` | macOS + Linux 컨테이너를 실행하는 Docker Desktop | SSH로 접속하는 Ubuntu 서버 |
| `windows.ps1` | WSL2의 Ubuntu + 해당 배포판에서 접근 가능한 Linux Docker daemon | WSL2에서 SSH로 접속하는 Ubuntu 서버 |

현재 서버는 POSIX 파일 잠금과 Linux 격리 채점 기능을 사용한다. Windows 네이티브 Python으로
실행하는 방식은 지원하지 않는다. Windows용 학생 IDE의 네이티브 지원 여부와 구분한다.
기관의 상시 운영 대상은 Ubuntu를 우선한다. Mac과 WSL2는 전용 로컬 평가 환경으로 사용할 수
있지만 절전·재부팅·WSL 네트워크·Docker Desktop 시작 조건을 포함한 현장 인수가 필요하다.

현재 실행기는 Python과 Next.js를 하나의 설치 OS 계정으로 관리한다. 서로 다른 프로세스이지만
OS 권한까지 격리한 구성은 아니다. 상용 운영에서는 별도 웹 서비스 계정과 파일 접근 정책을
적용하는 추가 강화·인수 작업이 필요하다. 설치 성공을 전체 제품의 보안 인수 완료로 표시하지 않는다.

스크립트는 다음을 자동 변경하지 않는다.

- 기존 설치 디렉터리, 운영 CSV, 학생 DB·자료·계정 및 공개 과제의 채점 이미지
- 학교 DHCP·DNS·방화벽·인증서 저장소, Windows 기능이나 PowerShell 실행 정책
- 클라우드 VM·네트워크·과금 리소스, SSH 호스트 키 신뢰, OS 사용자·자동 시작 서비스 등록

## 1. 실행 환경 준비

서버를 실행할 일반 OS 사용자와 같은 계정으로 설치한다. 설치 경로의 **상위 폴더**는 그 사용자가
쓸 수 있어야 한다. 설치 경로 자체는 아직 없어야 한다. 기존 운영 데이터를 새 설치 경로에
복사하거나 기존 경로를 `install_dir`로 지정하지 않는다.

기관 설치형 호스트와 클라우드 대상 Ubuntu 서버에는 다음이 필요하다.

- Python 3.10 이상과 `venv`, `pip`. Ubuntu는 `python3-venv` 패키지가 필요하다.
- Node.js 24와 npm. Ubuntu 기본 패키지의 Node 버전이 24인지 별도 확인한다.
- Git, Nginx, 설치 계정에서 사용할 수 있는 Linux Docker daemon
- 승인된 고정 채점 이미지의 실제 `repository@sha256:...` 값
- 웹·API의 서로 다른 HTTPS 주소와 두 주소를 포함하는 인증서·개인키
- Python/npm 의존성을 받을 수 있는 네트워크 또는 사전에 구성한 기관 패키지 미러

가상환경 생성부터 패키지 설치까지는 실행기가 수행한다. 학생 PC에 Python·Node를 설치하는
절차가 아니다. Docker와 인증서 접근 권한은 기관 운영자가 준비한다. Docker daemon 접근은
높은 권한이므로 임의의 학생 계정에 부여하지 않는다.

확인 예시:

```sh
python3 --version
node --version
npm --version
git --version
docker info
nginx -v
```

Windows에서는 WSL2 Ubuntu의 최초 사용자 설정을 마치고, 해당 배포판에서 위 명령을 확인한다.
설치 경로와 데이터는 `/home/...` 등 **WSL의 Linux 파일시스템**을 사용한다. `/mnt/c/...`를
운영 데이터 경로로 사용하지 않는다. 저장소와 프로필은 Windows에 두고 실행할 수 있다.

## 2. 설치 프로필 작성

설치 환경에 맞는 파일을 복사한다. `.local` 파일은 Git에 포함되지 않으며 내용은 JSON이다.

```sh
cp config/deployment/institution-ubuntu.example.json config/deployment/classroom.local
```

Mac은 `institution-macos.example.json`, Windows는 `institution-windows-wsl2.example.json`,
클라우드는 `cloud-ubuntu.example.json`을 사용한다. 다음 값은 실제 환경으로 바꾼다.

| 항목 | 설정 내용 |
|---|---|
| `installation_id` | 설치 식별자. 영문 소문자·숫자·하이픈 |
| `install_dir` | 새 설치의 절대 경로. 클라우드에서는 원격 Ubuntu 경로 |
| `web_url` / `api_url` | 서로 다른 HTTPS 주소. 이름이 다르면 둘 다 기본 443 사용 가능 |
| `runner_image` | 승인된 실제 이미지 digest. 예제의 `REPLACE_WITH_...`는 실행할 수 없음 |
| `tls_certificate` / `tls_key` | 서버 호스트에 준비한 인증서와 개인키의 절대 경로 |
| `allowed_cidrs` | 기관 설치형에서 접근을 허용할 네트워크. 클라우드에서 빈 목록이면 IP 제한 없음 |
| `course_keys` | Next.js 결과 화면에 허용할 실제 분반 키. 처음에는 빈 목록으로 시작 가능 |
| `worker_count` | 채점 worker 수, 1~16. 호스트의 CPU·메모리에 맞춰 선택 |
| `ssh_target` / `ssh_port` | 클라우드 배포에만 사용. 기존 SSH 사용자와 호스트, 포트 |

운영 비밀번호·SSH 개인키·DNS API 자격 증명·인증서 개인키 **내용**을 프로필에 넣지 않는다.
기존 `pilot-local`의 공개본과 제출 영수증을 격리 채점으로 몰래 변환하지 않는다. 기존 자료 이전은
[격리 채점 안내](isolated-instructor-authoring.md)의 별도 검증·전환 절차를 따른다.

## 3. 기관 설치형 실행

저장소 루트에서 먼저 `plan`을 실행해 경로·주소·배포 유형을 확인한다. `plan`은 배포하지 않는다.

Ubuntu:

```sh
bash scripts/deploy/ubuntu.sh plan --profile config/deployment/classroom.local
bash scripts/deploy/ubuntu.sh install --profile config/deployment/classroom.local
bash scripts/deploy/ubuntu.sh init-admin --profile config/deployment/classroom.local --username owner --name "기관 관리자"
bash scripts/deploy/ubuntu.sh start --profile config/deployment/classroom.local
bash scripts/deploy/ubuntu.sh status --profile config/deployment/classroom.local
```

macOS:

```sh
bash scripts/deploy/macos.sh plan --profile config/deployment/classroom.local
bash scripts/deploy/macos.sh install --profile config/deployment/classroom.local
bash scripts/deploy/macos.sh init-admin --profile config/deployment/classroom.local --username owner --name "기관 관리자"
bash scripts/deploy/macos.sh start --profile config/deployment/classroom.local
bash scripts/deploy/macos.sh status --profile config/deployment/classroom.local
```

Windows PowerShell:

```powershell
.\scripts\deploy\windows.ps1 -Command plan -Profile .\config\deployment\classroom.local -Distribution Ubuntu
.\scripts\deploy\windows.ps1 -Command install -Profile .\config\deployment\classroom.local -Distribution Ubuntu
.\scripts\deploy\windows.ps1 -Command init-admin -Profile .\config\deployment\classroom.local -Distribution Ubuntu -Username owner -Name "기관 관리자"
.\scripts\deploy\windows.ps1 -Command start -Profile .\config\deployment\classroom.local -Distribution Ubuntu
.\scripts\deploy\windows.ps1 -Command status -Profile .\config\deployment\classroom.local -Distribution Ubuntu
```

`-Distribution`은 `wsl.exe --list --verbose`에 표시된 Ubuntu 배포판 이름이다. WSL1 배포판은
거절한다. 실행 정책이 스크립트를 막으면 기관의 승인된 스크립트 배포·서명 정책을 따른다.
실행기는 실행 정책을 우회하거나 변경하지 않는다.

`install`은 새 설치 경로에 소스, Python 가상환경, 웹 빌드, 운영 CSV와 설정 파일을 준비한다.
`init-admin`의 비밀번호는 터미널에서 직접 입력한다. 기존 관리자 DB를 삭제하거나 다시
초기화하지 않는다. `start`는 등록된 설치의 Python·Next.js만 실행하고 내부 준비 상태를 확인한다.
프로세스를 실행했다는 것과 외부 HTTPS 접속 검증은 구분한다.

## 4. HTTPS·DNS 공개

설치 결과의 `config/nginx.conf`는 기존 업로드 한도, 실시간 교실 이벤트, 웹/API 구분,
Next.js 경로, Cookie·Origin 처리와 기관 허용 네트워크를 포함한다. 데이터 폴더를 정적 파일
경로로 노출하지 않는다. 인증서와 DNS는 먼저 실제 주소에 맞춰 준비해야 한다.

실행기의 `proxy-start`는 생성한 Nginx 설정을 검사한 뒤 그 프록시를 시작한다. 기본 443은
호스트에 따라 별도의 관리자 권한이 필요하다. **서버 설치·관리자 생성·앱 실행 전체를 root로
실행하지 않는다.** 필요한 경우 프록시 시작만 운영자가 승인한 권한으로 실행한다.

```sh
sudo python3 scripts/deploy/deploy.py proxy-start --profile config/deployment/classroom.local
```

Windows는 WSL Ubuntu 터미널에서 같은 방식으로 프록시 권한을 처리한다. 기존 Nginx가 이미
같은 포트를 사용한다면 두 번째 프록시를 시작하지 말고 생성 설정을 검토하여 기존 배포에
통합한다. 기존 설정을 자동 교체하지 않는다. 사용자 계정·경로·인증서 권한은 함께 검토한다.

기관 설치형에는 내부 DNS용 레코드 안내 파일이 생성된다. 학교 DNS에 등록하거나 독립
실습실 DNS에 적용한 뒤 DHCP를 통해 PC가 해당 DNS를 사용하도록 설정한다. 생성 파일만으로
PC의 DNS가 바뀌지는 않는다. `.local` 대신 소유 도메인의 내부용 하위 이름을 사용한다.

WSL2에서 실습실 다른 PC에 공개할 때는 Windows 호스트에서 WSL의 443까지 실제로 접근할 수
있는지 확인한다. WSL 네트워크 모드·Windows 방화벽·학교 정책은 배포 스크립트가 변경하지 않는다.
Windows 호스트 자체의 localhost 접근 성공만으로 실습실 LAN 접속 성공을 판단하지 않는다.

외부 주소 검증:

```sh
bash scripts/deploy/ubuntu.sh check --profile config/deployment/classroom.local
```

Mac은 `macos.sh check`, Windows는 `windows.ps1 -Command check`를 사용한다. HTTPS 검증을
끄지 않는다. 내부 CA를 사용하면 운영체제·Python·IDE·WSL의 실제 신뢰 저장소에 인증서를
배포한 뒤 시험한다. 교수자 `/instructor/login`에서 MFA를 등록하고 수업·학생·과제 등록을 진행한다.
[개인 교수자 인증 안내](instructor-personal-auth.md)를 따른다.

## 5. 클라우드 설치형 실행

먼저 기존 Ubuntu VM을 준비하고 일반 설치 계정, 동일한 전제 도구, 설치 상위 폴더의 쓰기 권한,
Docker 접근, TLS 인증서, DNS, 보안 그룹을 설정한다. 클라우드 생성·비용 발생 작업은 수행하지 않는다.
운영자는 해당 호스트의 SSH 키를 별도 신뢰 경로로 확인하고 SSH 접속을 준비한다. 호스트 키
검증을 끄거나 개인키를 소스·프로필에 복사하지 않는다.

자동 전송은 SSH 키 또는 에이전트 인증을 사용한다. `BatchMode=yes`로 비밀번호 대기 없이
실패를 반환하며, `StrictHostKeyChecking=yes`로 등록되지 않은 호스트를 거절한다.

```sh
cp config/deployment/cloud-ubuntu.example.json config/deployment/cloud.local
```

실제 값으로 수정한 뒤 실행한다.

```sh
# Ubuntu에서 배포
bash scripts/deploy/ubuntu.sh plan --profile config/deployment/cloud.local
bash scripts/deploy/ubuntu.sh cloud-deploy --profile config/deployment/cloud.local

# macOS에서 배포
bash scripts/deploy/macos.sh cloud-deploy --profile config/deployment/cloud.local
```

```powershell
# Windows에서 WSL2를 통해 배포
.\scripts\deploy\windows.ps1 -Command cloud-deploy -Profile .\config\deployment\cloud.local -Distribution Ubuntu
```

클라우드 명령은 배포 소스와 프로필을 원격 새 설치로 전달한 후 설치한다. 관리자 비밀번호를
전송하지 않고, 공개 서비스도 자동 시작하지 않는다. 출력된 원격 후속 명령을 사용하여 SSH로
`init-admin`, `start`, `proxy-start`, `check`를 진행한다. 원격에서 이 명령을 실행할 때 프로필의
`deployment`는 `cloud` 그대로 유지한다. `cloud-deploy`를 다시 실행해 기존 설치를 덮어쓰지 않는다.

## 6. 중지·백업·재시작

아래 예시의 백업 디렉터리는 설치의 데이터 폴더 밖에 미리 준비한 보호된 경로여야 한다.

```sh
bash scripts/deploy/ubuntu.sh stop --profile config/deployment/classroom.local
bash scripts/deploy/ubuntu.sh backup --profile config/deployment/classroom.local --destination /var/backups/autograde
bash scripts/deploy/ubuntu.sh start --profile config/deployment/classroom.local
```

Windows는 `-Command backup -Destination /home/운영자/autograde-backups`처럼 WSL 경로를 사용한다.
백업은 계획된 중지 시간에 진행하며 실제 복원 시험은 [기관 운영 안내](institution-operations.md)를
따른다. 자동 중지·무중단 업그레이드·백업 검증만으로 복원 성공을 보장하지 않는다.

`stop`은 이 설치가 관리하는 앱·Next.js 프로세스를 정상 종료한다. 다른 Python·Node·Nginx를
이름으로 검색하여 강제 종료하지 않는다. 운영 로그는 설치의 `logs/`에 남는다. OS 자동 시작은
생성된 서비스 예제를 검토하여 별도로 등록한다. Mac의 절전과 Docker Desktop, WSL 배포판의
시작 여부까지 OS 서비스 한 개로 보장된다고 가정하지 않는다.

## 배포 인수 기준

자동 회귀에서는 프로필 검증, 명령 인자 보존, 기존 설치 덮어쓰기 거절, 의존성·서비스 오류,
인증서 검증, 원격 명령 경계를 확인한다. 이는 아래 실제 장비 시험을 대체하지 않는다.

1. Ubuntu·Mac·Windows/WSL2 각각에서 신규 설치 → 개인 관리자 MFA → 수업 생성
2. 실습실 PC에서 DNS·HTTPS → 학생 로그인 → 수령·다운로드 → 제출 → 최신 점수 확인
3. 서버·Docker 재시작, 디스크 부족, HTTPS 인증서 오류, 백업·별도 호스트 복원
4. 공유 PC에서 종료 후 정리 및 다음 학생에게 이전 코드·로그인 정보가 보이지 않는지 확인
5. 설치 유형별 승인된 동시 제출 수와 복구 시간 측정

현재 Mac 개발 환경의 검증 결과를 실제 Ubuntu 서버나 Windows 실습실 인수 완료로 표시하지 않는다.

## 자동 검사와 실제 프록시 구문 검사

```sh
python3 -m pytest tests/unit/test_deployment_config.py tests/unit/test_deployment_launchers.py tests/unit/test_deployment_runner.py -q
```

2026-10-29 Mac 개발 환경에서 86개 통과, PowerShell 미설치에 따른 파서 검사 1개는 건너뛰었다.
실제 합성 프로세스의 시작·조회·종료와 주 프로세스 종료 후 하위 프로세스 정리, 다른 프로세스 보존,
긴 교수자 로그인 HTML 판정, 소스 복사 중 변경 감지, SSH 전달 파일 범위와 안전한 압축 해제를 포함한다.
Python/npm 실제 신규 설치 및 실제 SSH 서버 배포 전체를 이 자동 시험이 대신하지는 않는다.

별도로 공식 Nginx의 다음 고정 이미지에서 기관형 443·클라우드형 20010/20000 설정을
실제 `nginx -t`로 검사했다. 두 경우 모두 공백 포함 경로와 사용자·기본 그룹을 확인했다.
시험은 합성 인증서를 사용하며 호스트의 HTTPS 포트를 열거나 인증서 저장소를 바꾸지 않는다.

```sh
python3 scripts/deploy/check_nginx_config.py --image nginx@sha256:51047bea59fed88fbd1f52565d2f5fabcb82d311cf6c9e507a10ce74bb3bc3f0
```

이 검사는 해당 이미지를 로컬에 먼저 준비해야 하며, 실행 중 이미지를 자동으로 내려받지 않는다.
시험 컨테이너의 외부 네트워크는 차단한다. 실제 기관·클라우드 호스트에서 전체 설치와 학생 제출
흐름을 통과한 뒤 그 환경의 지원 상태를 확정한다.
