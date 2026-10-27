# Autograde IDE — Linux 운영자 설치 안내

이 패키지는 기관 공용 실습용 Linux 데스크톱 앱입니다. Node와 편집기 실행 파일이 포함되므로 학생 PC에 Node·npm이나 저장소 체크아웃이 필요하지 않습니다. Windows용 WSL2 배포본과 별도이며 macOS에서 만든 실행 파일로 대체할 수 없습니다.

## 설치 조건

- x64 또는 arm64 Linux와 동일 아키텍처로 만든 배포본을 사용합니다. 빌드한 배포판보다 오래된 glibc 환경은 지원한다고 가정하지 않습니다. 실제 배포 대상 배포판에서 수용 검증이 필요합니다.
- 일반 사용자로 로그인한 로컬 X11 또는 Wayland 데스크톱이 필요합니다. SSH X11 전달, root 실행, WSL 환경은 이 실행기에서 거절합니다.
- Electron용 시스템 라이브러리와 `/usr/bin/ldd`, `/usr/bin/unshare`가 필요합니다. Ubuntu 계열에서는 배포판에 맞는 GTK 3, NSS, ALSA, GBM, X11/ATK 라이브러리 등을 운영자가 준비합니다. 실행기는 라이브러리를 자동 설치하지 않습니다.
- Chromium이 사용할 수 있는 사용자 네임스페이스 또는 관리자가 올바르게 설치한 root 소유 sandbox helper가 필요합니다. AppArmor 등 호스트 정책 때문에 추가 운영자 설정이 필요할 수 있습니다. 패키징은 setuid 권한을 추가하거나 보안 정책을 변경하지 않습니다. `--no-sandbox`는 지원하지 않습니다.
- 앱과 임시 세션 경로는 로컬 Linux 파일 시스템이어야 합니다. 현재 ext 계열, btrfs, XFS, tmpfs, overlayfs, F2FS, ZFS를 확인합니다. NFS·SMB·Windows 공유 폴더는 거절합니다.
- 시작 실패를 대화상자로 표시하려면 `zenity` 또는 `kdialog`를 설치합니다. 없으면 아래 `--check` 명령의 JSON 오류를 확인합니다.

## 설치

압축 파일과 함께 제공된 SHA-256을 확인하고, **새 비어 있는 로컬 설치 경로**에 압축을 풉니다. 기존 설치 위에 덮어쓰지 않습니다. 기관 관리자가 `/opt` 아래에 읽기 전용으로 배치하거나, 실습 사용자 소유의 고정된 앱 폴더를 사용할 수 있습니다. 설치 후 폴더를 옮기면 아이콘 경로도 다시 등록해야 합니다.

실습용 일반 사용자로 다음을 실행합니다. 경로에 공백과 한글을 사용할 수 있으며 반드시 따옴표를 유지합니다. 데스크톱 표준의 제약 때문에 실행 파일 경로의 `%`, `=`와 제어 문자는 거절합니다.

```sh
"/opt/Autograde IDE/bin/autograde-ide" --check
"/opt/Autograde IDE/bin/install-autograde-desktop"
```

두 번째 명령은 현재 사용자의 `$XDG_DATA_HOME/applications` 또는 `~/.local/share/applications`에 `autograde-ide.desktop`을 새로 만듭니다. 기존 파일이나 연결된 폴더는 덮어쓰지 않습니다. 앱 메뉴의 **Autograde IDE**로 실행합니다. 화면 바탕에 아이콘을 추가하려면 실제 바탕화면 폴더를 직접 지정합니다.

```sh
"/opt/Autograde IDE/bin/install-autograde-desktop" \
  --applications-dir "$HOME/.local/share/applications" \
  --desktop-dir "$HOME/Desktop"
```

앱 메뉴와 바탕화면 아이콘을 동시에 설치하려면 위 명령을 최초 등록 시 사용합니다. 이미 등록했다면 운영자가 이전 `.desktop` 파일을 확인하여 별도 보관한 뒤 다시 실행합니다. 데스크톱 환경이 요구하는 “실행 허용/신뢰” 승인은 사용자가 직접 처리합니다. 설치기는 자동으로 신뢰 속성을 변경하지 않습니다.

## 실습 종료와 개인정보

각 실행은 `/tmp/ag-linux-lab-<uid>/current/workspace`의 빈 작업 폴더와 새 전용 세션에서 시작합니다. 과제 코드, 교재, 사용자 프로필, 확장 저장소, 캐시, 임시 파일, HOME과 터미널 기록은 세션에 둡니다. 정상 종료와 편집기 충돌 후에는 프로세스가 종료된 것을 확인하고 해당 세션만 정리합니다. 이전 실행의 소유권·프로세스 상태를 확인할 수 없으면 새 실습을 열지 않습니다.

학생은 종료 전에 필요한 결과를 **명시적으로 내보내기**해야 합니다. 세션 밖으로 내보낸 ZIP 등은 삭제하지 않습니다. 강제 전원 종료 뒤에는 다음 실행이 안전하게 확인할 수 있는 중단 세션만 복구 정리합니다. `/tmp` 청소 정책이 실행 중 파일을 지우지 않도록 기관이 운영해야 합니다.

같은 운영체제 계정의 악의적인 사용자에 대한 보안 격리나 디스크의 복구 불가능한 삭제를 제공하는 것은 아닙니다. DBus·그래픽 세션은 현재 로그인 사용자의 데스크톱과 연결됩니다. 학생별 OS 계정 또는 OS 차원의 격리가 필요한 기관은 별도로 구성해야 합니다.

설치본은 일반적인 확장 설치를 막고 Autograde 교수자 승인 경로를 사용하는 편집기 정책을 포함합니다. 빌드 시 정책 소스를 검증하고 다시 컴파일합니다. 배포 전 교수자 승인 확장 설치와 우회 설치 차단을 실제 대상 Linux에서 확인합니다.

## 빌드와 수용 확인

같은 아키텍처의 Linux에서 고정된 upstream, 의존성, Autograde 오버레이와 편집기·내장 확장을 빌드한 뒤 실행합니다.

```sh
node ide/code-oss/package-linux.mjs \
  --source /absolute/path/to/prepared-code-oss \
  --node /absolute/path/to/linux-node/bin/node \
  --output /absolute/path/to/new-autograde-linux
```

출력은 앱 폴더, `.tar.gz`, `.tar.gz.sha256`입니다. 번들 Node는 `ide/code-oss/upstream.json`의 버전과 일치해야 합니다. macOS/Windows 호스트, 다른 아키텍처의 Node·Electron·네이티브 모듈, 미컴파일 정책/확장, 기존 출력 경로는 거절합니다.

배포 대상에서 `--check` 성공, 메뉴 실행, 과제 열기·빌드·제출, 승인 확장 설치, 일반 VSIX/CLI 설치 차단, 종료 후 세션 삭제, 다음 학생의 빈 상태, 내보낸 파일 보존을 확인합니다. `--check`는 실제 전체 GUI 수용 검증을 대체하지 않습니다.

30초 주기 작업 저장·복원은 [공용 작업 폴더 안내](../../../docs/operations/shared-workspace-checkpoints.md)를 참고합니다.
