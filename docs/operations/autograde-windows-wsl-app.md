# Windows WSL2 실습실 앱 설치와 실행

학생은 Windows 바탕 화면의 **Autograde IDE (WSL2)** 아이콘으로 실습을 시작한다.
Windows 실행기가 지정한 WSL2 배포판과 일반 Linux 사용자로 편집기를 열고, 편집기 종료 후
임시 실습 파일 정리가 끝날 때까지 기다린다. 학생에게 Node.js 설치나 터미널 명령을 요구하지 않는다.
Node와 편집기는 Linux 배포 묶음에 포함되며, C/C++ 컴파일러와 Linux 시스템 라이브러리는
실습실 담당자가 미리 준비한다. 채점 서버의 Python·SQLite 구성은 바뀌지 않는다.

이 구성은 WSLg로 Linux 편집기 창을 Windows에 표시한다. Microsoft Remote WSL 확장을
설치하거나 기존 Windows VS Code에 원격 접속하는 방식이 아니다. WSLg는 Windows 11 또는
Windows 10 빌드 19044 이상에서 지원한다. 실습실 기준 환경은 Windows 11·WSL2·WSLg로
정한다. [Microsoft WSL GUI 앱 안내](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps)

## 학생 이용 순서

1. **Autograde IDE (WSL2)** 아이콘을 더블클릭한다. 준비 창 다음에 새 학습 공간이 열린다.
2. 제한 모드가 표시되면 이번 실습 폴더만 신뢰한다. 홈 디렉터리나 다른 폴더까지 범위를 넓히지 않는다.
3. 수업 서버 주소를 확인하고 학생 웹에서 발급한 수령 코드로 과제를 받는다.
4. 이전 제출본을 이어서 작업하려면 제출 이력에서 본인의 코드를 복원한다.
5. 가져갈 코드·제출본·자료는 **내 학습내용 내보내기**로 세션 밖에 저장한다.
6. **공용 실습 종료**를 선택하고 편집기 종료와 정리 완료를 기다린다.

Windows 폴더로 내보낼 때는 Linux 파일 선택 창에서 본인의 Windows 저장 위치를 선택한다.
예를 들어 `C:\Users\student\Downloads`는 일반적인 WSL 자동 마운트 설정에서
`/mnt/c/Users/student/Downloads`로 접근한다. 사용자 이름과 드라이브 경로는 해당 PC에 맞춘다.
USB도 먼저 Windows에서 사용할 수 있는지 확인한다. 코드 작업 폴더 자체를 `/mnt/c`로 옮기지는 않는다.

**미제출·미내보내기 코드도 실습 종료 시 삭제된다.** 로컬 저장은 서버 제출이나 백업을 뜻하지 않는다.
서버에 접수된 제출 이력·점수와 세션 밖에 명시적으로 내보낸 ZIP·폴더는 유지된다.
실습을 끝내기 전에 필요한 내용의 제출·내보내기 완료를 확인한다.

아이콘을 다시 눌러도 동시에 다른 학생의 세션을 만들지 않는다. 기존 실습이 실행 중이라는 안내가
나오면 작업 표시줄에서 해당 IDE를 사용한다. 다음 학생은 이전 학생의 **공용 실습 종료** 후 시작한다.

## 담당자가 한 번 준비할 내용

WSL 배포판과 바로가기는 **실제 실습에 사용할 Windows 계정**에서 준비한다. 별도 교수자 계정에만
설치된 WSL 배포판은 학생의 Windows 계정에서 그대로 사용할 수 없다. 같은 좌석에서 하나의 Windows
계정을 공유하더라도 매 실행의 과제·자료·IDE 프로필은 새로 만든다.

- WSL2와 WSLg, GPU 드라이버를 준비한다. WSL1은 지원하지 않는다.
- `Ubuntu-22.04` 또는 `Ubuntu-24.04` 등의 배포판과 일반 사용자 계정을 정한다. `root`로 실행하지 않는다.
- 해당 Linux 아키텍처에 맞게 편집기를 빌드한다. Windows x64용과 ARM64용 Linux 바이너리를 섞지 않는다.
- Git·C/C++ 컴파일러·편집기 시스템 라이브러리를 준비하고 실제 GUI 실행을 확인한다.
- Linux 사용자 네임스페이스 또는 관리자가 설치한 Chromium 실행 보호 구성을 준비한다.

배포판 이름·버전 확인과 WSL 설치·갱신은 [Microsoft WSL 명령 안내](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)를 따른다.
실행기와 설치 스크립트는 WSL 배포판 설치·삭제·변환이나 Windows 정책 변경을 자동 수행하지 않는다.

처음 준비하는 Windows 11 좌석에서는 담당자가 관리자 PowerShell에서 다음을 실행한다.
이미 `Ubuntu-24.04`를 사용하는 좌석은 재설치하지 않고 `wsl --list --verbose`로 먼저 확인한다.

```powershell
wsl --install -d Ubuntu-24.04
```

재부팅 후 실제 실습 Windows 계정에서 Ubuntu를 열고 일반 Linux 사용자(아래 예: `student`)를
만든다. 다음 결과에서 배포판의 `VERSION`이 `2`인지 확인한다. WSLg가 없는 기존 설치는 실습을
모두 종료한 유지보수 시간에 관리자 권한으로 `wsl --update`하고 재부팅한다.
[Microsoft WSL 설치 안내](https://learn.microsoft.com/en-us/windows/wsl/install)

```powershell
wsl --version
wsl --list --verbose
wsl -d Ubuntu-24.04 -u student
```

아래 Linux 빌드는 마지막 명령으로 연 Ubuntu 터미널에서 진행한다. `/mnt/c`의 Windows 저장소
대신 Linux 홈 아래에 별도 저장소를 준비한다. 기존 저장소가 있으면 그 위치에서 갱신한다.

```sh
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/simultech-kr/autograde.git
cd autograde
```

Git이 없으면 먼저 배포판 패키지 관리자로 설치한다. 이후 학생 좌석에는 완성된 Linux 배포
묶음만 복사할 수 있으며, 모든 좌석에서 Code-OSS를 다시 빌드할 필요는 없다.

## Linux 배포 묶음 제작

이 단계는 담당자가 Linux 또는 WSL2 빌드 환경에서 수행한다. Mac에서 만든 `.app`, Node,
Electron 또는 `node_modules`를 복사해서 사용하지 않는다. [Code-OSS 소스 준비와 빌드](autograde-ide-poc.md)를
해당 Linux 환경에서 완료하고, 고정 Node 버전 `24.18.0`을 사용한다.

현재 Git 저장소와 Windows 설치 도구 ZIP에는 완성된 Linux 편집기·Node 바이너리가 포함되어
있지 않다. 처음 한 번은 위 빌드 안내의 **소스 준비 → 내장 확장 준비 → Code-OSS 빌드**를
완료해야 한다. 담당자의 빌드 환경에는 Node·npm이 필요하지만 완성된 묶음으로 실행하는 학생은
별도로 설치하지 않는다. `--node`는 공식 Linux Node 배포 압축을 푼 `bin/node`처럼 상위
디렉터리에 `LICENSE`도 있는 경로를 사용한다. `/usr/bin/node`만 복사하는 방식은 지원하지 않는다.

저장소 루트에서 실행한다. `--node`에는 함께 배포할 Linux Node 실행 파일을 지정한다.

```sh
node ide/code-oss/manage.mjs verify --source .cache/code-oss
node ide/code-oss/package-wsl.mjs \
  --source .cache/code-oss \
  --node /path/to/node-v24.18.0-linux-x64/bin/node \
  --output build/wsl/autograde-ide-linux-x64
```

출력은 실행 폴더, `.tar.gz` 압축 파일, `.tar.gz.sha256` 확인 파일이다. ARM64 빌드에서는
Node 경로와 출력 이름을 `arm64`에 맞춘다. 패키징 도구는 Linux Node·Electron의 아키텍처와
준비된 내장 확장을 확인하며, 기존 출력은 덮어쓰지 않는다. 원본 Git 저장소·학생 데이터·서버
DB·인증 파일은 배포 묶음에 포함하지 않는다.

수신한 압축 파일은 체크섬을 확인한 뒤 **새 Linux 설치 폴더**에 푼다. 아래는 Linux 사용자
`student`의 홈 아래에 설치하는 예다. `release-01`은 배포 식별자이며 갱신할 때 새 이름을 쓴다.

```sh
sha256sum -c autograde-ide-linux-x64.tar.gz.sha256
mkdir -p "$HOME/.local/share/autograde-ide/releases/release-01"
tar -xzf autograde-ide-linux-x64.tar.gz \
  -C "$HOME/.local/share/autograde-ide/releases/release-01" --no-same-owner
"$HOME/.local/share/autograde-ide/releases/release-01/autograde-ide-linux-x64/bin/autograde-ide" --check
```

`--check`는 WSL2·일반 사용자·WSLg 소켓·Linux 설치 위치·편집기 실행 보호 조건을 확인한다.
`ready` 결과는 사전 조건 확인이며, 편집기 창과 과제 제출 시험을 대신하지 않는다.
Linux 설치·실습 폴더를 Windows 공유 드라이브나 네트워크 공유 폴더에 두면 실행을 거절한다.

Ubuntu의 AppArmor 또는 사용자 네임스페이스 정책 때문에 실행 보호 검사가 실패하면 담당자가
배포판 정책과 Chromium 실행 구성을 확인한다. 필요하다면 루트가 소유하고 일반 사용자가 수정할
수 없는 설치 경로에서 검증된 `chrome-sandbox` helper를 관리한다. 패키징 도구는 setuid 권한을
자동으로 부여하지 않는다. `--no-sandbox` 추가나 전역 보안 정책 해제로 우회하지 않는다.

## Windows 바로가기 설치

Autograde 저장소의 `ide/code-oss/windows` 폴더를 Windows에 준비한다. 아래 명령은
실습실 담당자가 실습 Windows 계정의 PowerShell에서 한 번 실행한다. 학생에게 안내하는 명령이 아니다.
Windows에서 Git 저장소를 갱신했다면 저장소 루트에서 실행한다. 별도 Setup-Kit ZIP을 사용하면
압축을 푼 폴더에서 스크립트 경로를 `.\windows\Install-Shortcut.ps1`로 바꾼다.

```powershell
.\ide\code-oss\windows\Install-Shortcut.ps1 `
  -Distribution 'Ubuntu-24.04' `
  -User 'student' `
  -LinuxInstallRoot '/home/student/.local/share/autograde-ide/releases/release-01/autograde-ide-linux-x64'
```

배포판 이름·사용자·Linux 설치 경로는 앞서 검증한 값과 정확하게 맞춘다. 설치 스크립트는
Windows에 제공되는 .NET Framework 컴파일러로 GUI 실행기와 자체 시험 프로그램을 만들고,
시험과 Linux 사전 검사가 성공한 뒤 바탕 화면 바로가기를 만든다. 학생 PC에 Node·npm·.NET SDK는
필요하지 않다. PowerShell 실행이 학교 정책으로 차단되면 승인된 서명·설치 절차를 사용한다.
스크립트가 전역 실행 정책을 완화하지 않는다.

Windows 실행 파일과 설정은 다음 경로의 새 릴리스 폴더에 설치된다.

```text
%LOCALAPPDATA%\Autograde\WSLLauncher\release-<날짜>-<식별자>\
  Autograde IDE.exe
  launcher.json
```

`launcher.json`에는 배포판·일반 사용자·설치 경로만 저장하며 학생 비밀번호나 수령 코드를 넣지 않는다.
바로가기는 `Autograde IDE (WSL2).lnk`이고, 실행 시 터미널 창을 열지 않는다. Linux 실행기는
학생 PC의 전역 Node 대신 배포 폴더 안의 Node를 사용한다.

## 갱신과 종료 후 정리

학생 실습과 정리가 끝난 뒤 새 Linux 릴리스 폴더를 설치하고 `--check`를 확인한다. Windows
새 PowerShell 창에서 설치 스크립트에 새 `-LinuxInstallRoot`를 전달한다. 기존에 설치한 Autograde 소유 바로가기를
갱신하려면 `-ReplaceOwnedShortcut`을 추가한다. 임의의 같은 이름 바로가기는 덮어쓰지 않는다.
이전 실행 파일·Linux 설치 폴더는 담당자가 복구 가능성을 확인한 뒤 별도로 관리한다.

정상 종료에서는 실제 Linux 편집기 프로세스가 멈춘 뒤 해당 실행기가 만든 코드·자료·프로필과
임시 캐시를 제거한다. Windows 실행기만 닫거나 작업 관리자에서 강제 종료하는 것을 정상 종료로
안내하지 않는다. WSL 전체 종료·Windows 전원 차단 시에는 즉시 삭제를 보장할 수 없다.
다음 시작에서 이전 프로세스·소유 표식을 확인하여 멈춘 세션만 정리하고, 상태가 불확실하면
새 실습을 열지 않는다. 해당 좌석을 다음 학생에게 넘기기 전에 담당자가 확인한다.

같은 Linux 계정의 다른 프로그램이 실행 중인 파일을 읽는 것을 막는 OS 격리는 아니다.
세션 밖에 학생이 임의로 저장한 파일도 자동 삭제 범위에 포함하지 않는다.

실습 프로세스의 `HOME`, 셸 기록과 사용자 설정·캐시 경로도 이번 세션 안으로 지정한다.
따라서 공유 Linux 계정의 기존 홈 설정이나 홈 아래의 개발 도구 설정을 그대로 불러오지 않는다.
담당자는 수업에 필요한 컴파일러·도구를 시스템 경로나 관리되는 `/opt` 경로 등에 준비하고
새 실습에서 사용할 수 있는지 확인한다.

## 오류 확인

| 표시되는 상황 | 확인할 내용 |
| --- | --- |
| WSL을 찾을 수 없음 | 해당 Windows 계정에 WSL2와 배포판이 설치됐는지 확인한다. |
| WSL1 또는 root 실행 | 바로가기의 배포판과 일반 사용자 설정을 확인한다. |
| WSLg 연결 실패 | Windows·WSL 버전, GPU 드라이버와 그래픽 앱 실행 상태를 확인한다. |
| 앱 버전·프로세서 불일치 | 해당 배포판 아키텍처의 Linux 배포 묶음을 다시 준비한다. |
| Linux 설치 위치 오류 | 앱과 실습 경로를 Windows 공유 드라이브 밖으로 옮겨 설치한다. |
| Chromium 실행 보호 오류 | 배포판의 네임스페이스·AppArmor 또는 관리된 helper를 확인한다. |
| 이전 실습 실행 중 | 기존 IDE에서 공용 실습 종료 후 다시 시작한다. |
| 정리 완료 미확인 | 새 학생을 받지 않고 남은 프로세스·권한·세션 소유 표식을 확인한다. |

## 실습실 인수 시험

현재 개발 환경은 Mac이다. 실행기 전체 자동 시험 **200개**가 통과했으며, 이 중 새 WSL 실행기·
패키징 시험은 **53개**다. WSL 사전 조건은 모의 입력으로 검증했고, 임시 파일·실제 POSIX 자식
프로세스의 종료 후 정리·압축 풀기·패키지 이동 시험을 수행했다. 실제 WSLg 실행을 시험한 결과는 아니다.

Windows 실행 인자 전달·설정·상태 응답에 대한 **17개 자체 시험**은 설치 과정에서 Windows의
실제 프로세스를 사용하여 실행하도록 제공한다. 현재 Mac에서는 Windows 실행 파일을 컴파일하거나
이 시험을 실행하지 않았다. 실제 Linux 바이너리 배포 묶음 제작과 Windows·WSLg 창 표시·
좌석별 배포는 대상 환경에서 확인해야 한다.

Windows 설치는 임시 빌드 폴더에서 시험과 WSL 사전 검사를 끝낸 뒤에만 설치 경로와 바로가기를
만든다. 검사 실패 시에는 진단용 폴더를 안내하며, 같은 최종 경로로 다시 시도할 수 있다.

1. Windows 설치 스크립트의 자체 시험과 Linux `--check`가 성공하는지 확인한다.
2. 학생 계정에서 아이콘만 눌러 새 학습 홈이 열리는지 확인한다.
3. 아이콘을 반복 클릭해도 동시에 학생 세션이 두 개 생기지 않는지 확인한다.
4. 시험 계정으로 과제 수령·다운로드·수정·제출·채점 결과까지 확인한다.
5. 코드와 자료를 ZIP·폴더로 내보내고 내용이 맞는지 확인한다.
6. 공용 실습 종료 후 작업 폴더·프로필이 제거되고 내보낸 복사본은 남는지 확인한다.
7. 다시 실행하여 이전 코드·인증·자료가 보이지 않는지 확인한다.
8. IDE 강제 종료, Windows 실행기 강제 종료, WSL 종료를 각각 시험한다. 안전한 복구 또는
   다음 실습 중단 여부를 확인하며 운영 학생 자료로 시험하지 않는다.

### 종료 후 삭제를 실제 경로로 확인

아래는 담당자용 확인 명령이다. 학생에게 터미널 사용을 요구하지 않는다. 시험용 학생으로 IDE를
실행하고 과제 폴더에 `cleanup-check.txt`를 만들어 저장한다. 자료도 한 개 받고, **내 학습내용
내보내기**로 Windows 다운로드 폴더 등에 시험 ZIP을 저장한다. 내보낸 ZIP 안에 시험 파일이
있는지 먼저 확인한다.

IDE가 열려 있는 동안 별도 Windows PowerShell에서 아래를 실행해 이번 세션 경로를 기록한다.
배포판과 사용자 이름은 설치 때 사용한 값으로 바꾼다.

```powershell
$distro = 'Ubuntu-24.04'
$linuxUser = 'student'
$uidOutput = & wsl.exe -d $distro -u $linuxUser --exec /usr/bin/id -u
if ($LASTEXITCODE -ne 0) { throw 'Linux 사용자 확인 실패' }
$linuxUid = ($uidOutput | Out-String).Trim()
if ($linuxUid -notmatch '^\d+$') { throw 'Linux 사용자 번호 확인 실패' }
$labRoot = "/tmp/ag-wsl-lab-$linuxUid"
$sessionOutput = & wsl.exe -d $distro -u $linuxUser --exec /usr/bin/find $labRoot -mindepth 1 -maxdepth 1 -type d -name 'e-*'
if ($LASTEXITCODE -ne 0) { throw '실습 폴더 확인 실패' }
$sessionPaths = @($sessionOutput | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($sessionPaths.Count -ne 1) { throw '실행 중인 시험 세션이 정확히 한 개여야 합니다' }
$testSession = $sessionPaths[0]
Write-Output "종료 후 없어져야 할 폴더: $testSession"
```

IDE의 **공용 실습 종료**로 종료하고 실행기의 정리가 끝날 때까지 기다린다. 다시 IDE를 열기
전에, 같은 PowerShell 창에서 다음을 실행한다. 이 명령은 파일을 직접 삭제하지 않는다.

```powershell
& wsl.exe -d $distro -u $linuxUser --exec /usr/bin/test ! -e $testSession
if ($LASTEXITCODE -ne 0) { throw '시험 세션이 남아 있거나 확인에 실패했습니다' }
& wsl.exe -d $distro -u $linuxUser --exec /usr/bin/test ! -e "$labRoot/.autograde-shared-lock.json"
if ($LASTEXITCODE -ne 0) { throw '실행 잠금이 남아 있거나 확인에 실패했습니다' }
Write-Output '확인 완료: 시험 세션과 실행 잠금이 없습니다'
```

`$labRoot` 자체는 빈 폴더로 남아도 정상이다. 시험 세션 아래의 `workspace`, `materials`,
`r`, `home`, 캐시·셸 기록이 함께 없어져야 하며, Windows에 내보낸 ZIP과 서버의 제출 이력은
그대로 남아야 한다. 아이콘으로 다시 실행하여 이전 시험 파일과 로그인 정보가 없는 새 학습
공간이 열리는지도 확인한다. 종료 후 폴더가 남았다고 수동 삭제로 시험을 통과시키지 않는다.
실행기 오류와 남은 프로세스를 확인하고 해당 좌석의 다음 학생 사용을 보류한다.
