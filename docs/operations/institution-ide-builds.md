# 기관 배포용 IDE 후보 빌드

`.github/workflows/ide-build.yml`은 수동 release-candidate 빌드다. **Mac·Windows 네이티브·Linux
네이티브가 기본이며 Windows WSL2는 선택 사항**이다. GitHub Release·배포·저장소 push는 만들지
않는다. 산출물은 Actions 실행에 14일간 보관하고 접근 범위는 저장소 공개/비공개 설정을 따른다.
공개 저장소의 artifact를 비공개라고 설명하지 않는다. 다른 OS에서의 C# 컴파일 성공은 실제
Windows 실행 성공을 뜻하지 않는다.

## 대상과 버전

| 대상 | 빌드 환경 | 산출물 | 실행 방식 |
| --- | --- | --- | --- |
| macOS Apple Silicon | `macos-15`, ARM64 | `.app.zip` | Mac 앱 아이콘 |
| Windows 네이티브 기본 | `windows-2022`, x64 | `windows-native-x64.zip` | Windows EXE 아이콘, WSL 불필요 |
| Linux x64 | `ubuntu-22.04`, x64 | `linux-x64.tar.gz` | Linux 데스크톱 아이콘 |
| Windows WSL2 선택 | Linux x64 + Windows x64 | WSL2 payload `.tar.gz` + launcher `.zip` | Windows 아이콘이 WSL2/WSLg 편집기를 실행 |

Windows 네이티브는 관리자 설치 MSVC x64와 Windows SDK를 찾고 번들 Node·Electron을 사용한다.
학생이 Node를 설치하거나 시작 명령을 입력하지 않는다. 담당자가 전체 패키지를 설치한 다음
`Autograde IDE.exe`의 바로가기를 만든다. C17·C++17 사용자 범위 빌드 작업과 MSVC 터미널을
제공한다. 디버거·언어 기능은 교수자가 허용한 C/C++ 확장을 별도로 배포해야 한다. 기본 패키지에
상용 C/C++ 확장이나 디버거 UI가 포함되었다고 간주하지 않는다.

선택 WSL2는 별도 배포판·WSLg·비-root Linux 계정이 필요하다. 두 구성요소는 같은 빌드 실행에서
만든 것을 함께 사용한다. WSL launcher ZIP에는 컴파일된 EXE와 기존 설치 도구가 들어 있다.
기존 `Install-Shortcut.ps1`이 재컴파일·사전 점검을 수행하므로 포함된 C#·PowerShell·버전 파일도 보존한다.

`ide/code-oss/upstream.json`이 Code-OSS 저장소·tag·commit·Node 버전의 기준이다. 현재
Code-OSS `1.143.0`, commit `2d0b7506a36c1d6db9a466440131a8e01eab2289`, Node `24.18.0`이다.
확장과 EXE 버전은 `extensions/vscode/package.json`에서 읽는다. Assembly/FileVersion은 확장
버전에 `.0`을 붙이고 InformationalVersion은 확장 버전과 같다. 서버 호환·기관 주소·운영 정책은
별도의 기관 제품 프로필에서 승인한다.

GitHub의 [runner 표](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)에
따라 `macos-15`는 ARM64이고 Intel은 `macos-15-intel`이다. 실제 OS·architecture·Node도 검사한다.

## 실행과 검사

변경을 검토·커밋한 뒤 Actions의 **Institution IDE candidates**에서 ref를 수동 선택한다.
기본은 세 가지 native OS이며 필요할 때만 `include_wsl`을 선택한다. 토큰은 `contents: read`만
요청하고 checkout 자격증명을 보존하지 않는다. 로컬 candidate 빌드도 변경·미추적 파일이 없는
깨끗한 commit에서만 시작한다. 수정된 작업 폴더를 같은 commit의 정식 산출물로 기록하지 않는다.

1. 빌드 검증 시험과 예제 기관 제품 정의를 검사한다. 예제를 운영 승인본으로 변경하지 않는다.
2. OS별 새 경로에 고정 Code-OSS tag를 clone하고 실제 commit·`.nvmrc`를 확인한다.
3. 각 OS에서 의존성을 새로 설치한다. `node_modules`를 OS 간 전달하거나 캐시하지 않는다.
   Linux Electron native 모듈은 Ubuntu 22.04의 Clang 15로 빌드한다.
4. 교수자 배포 전용 설치 정책·제품 설정, client/extension 빌드, Electron 다운로드, overlay 검증과
   패키징을 수행한다. 패키징은 정책 런타임을 다시 컴파일하고 active native addon의 ELF/PE를 검사한다.
5. Mac 정책 smoke와 대상별 단위 시험·패키지 내부 CLI 실행을 수행한다. Windows에서는 실제
   launcher EXE를 컴파일하고 ACL·중복 실행·잠긴 파일 실패 시 recovery marker·job의 자식/손자
   종료·stale 복구·export 보존을 검사한다. MSVC C17/C++17 compile·SDK 링크·프로그램 실행도 검사한다.
6. 모든 기본 native job 성공 후 commit·버전·pin·SHA-256·파일 크기를 대조한다. 선택 WSL2의
   두 부분 중 하나라도 없으면 후보 묶음을 만들지 않는다.

Windows에서는 POSIX process-group·chmod 전용 시험을 실행하지 않고 Windows 패키지/부트스트랩,
플랫폼 관련 확장 시험 및 실제 native EXE 검사를 수행한다. 실제 통과한 이름을 build report에 기록한다.

Node heap은 최대 5 GiB이고 OS에 2 GiB를 남기며 native make 작업 수는 2다. upstream npm
wrapper의 8 GiB 고정값을 피하고 source typecheck→client compile(같은 incremental cache)→확장
compile→확장 media→Monaco typecheck를 순차 실행한다. 전체 검사를 유지하며 transpile-only로
대체하지 않는다. Go compiler 메모리 목표도 설정하지만 이는 절대 사용량 제한이 아니다. 최소 RAM
6 GiB·여유 공간 12 GiB를 검사하지만 전체 Code-OSS typecheck는 더 큰 메모리/디스크가 필요할 수
있다. 특히 에뮬레이션 환경의 메모리 부족을 성공으로 바꾸지 않는다. 실패 시 더 큰 native runner에서
다시 실행하며 typecheck를 생략한 파일을 검증 완료 후보로 집계하지 않는다.
수동 실행의 `macos_runner`·`linux_runner`·`windows_runner`에 기관에서 준비한 더 큰 native runner
label을 지정할 수 있다. 기본 label은 위 표와 같으며 Linux 대체 runner도 Ubuntu 22.04 x64와
동일한 빌드 전제를 충족해야 한다. 워크플로를 작성하는 것만으로 유료 runner를 예약하거나 실행하지 않는다.

공식 Node 배포본과 LICENSE를 사용한다. 로컬 명령도 네트워크로 소스·의존성·Electron을 받는다.
source/output은 기존 파일이 없는 새 경로여야 한다.

```sh
node scripts/ide/build-runtime.mjs --target macos-arm64 --source /tmp/code-oss-mac-new --output /tmp/mac-artifacts-new
node scripts/ide/build-runtime.mjs --target linux-x64 --source /tmp/code-oss-linux-new --output /tmp/linux-artifacts-new
# 선택 WSL2까지 만들려면 Linux 명령에 --include-wsl 추가
```

```powershell
node scripts/ide/build-runtime.mjs --target windows-x64 --source C:\Build\CodeOssNew --output C:\Build\NativeArtifactsNew
# 선택 WSL2 launcher만 빌드:
./scripts/ide/build-windows.ps1 -OutputDirectory C:\Build\WslLauncherNew
```

## 정리와 인수

Windows native는 POSIX 정리 코드를 재사용하지 않는다. SID에 제한한 NTFS ACL, exclusive file
lease, 소유 marker와 Windows job을 사용한다. Node를 suspended 상태로 만들고 job에 배정한 후
실행하며 breakaway를 허용하지 않는다. 자식 프로세스가 모두 멈춘 후 소유 세션만 지운다. 중단된
launch의 containment를 증명하지 못하거나 링크·권한·잠긴 파일 문제가 있으면 다음 실습을 막는다.
내보내기 폴더는 세션의 형제 경로이고 정리 대상이 아니다. 동일 Windows 계정의 모든 프로그램을
격리하는 보안 경계나 시험 감독 기능으로 설명하지 않는다. Windows 동작은
[Microsoft Job Objects 문서](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)를 따른다.

최종 `institution-ide-candidate-<run>-<attempt>` artifact에는 기본 세 native archive, 선택 WSL2
두 archive, 각 `.sha256`, `build-*.json`, `artifact-manifest.json`, `SHA256SUMS`가 있다.
manifest에는 크기·해시·버전·commit·실제 통과 검사와 선택 대상 관계를 기록한다.

```sh
sha256sum -c SHA256SUMS
# macOS: shasum -a 256 -c SHA256SUMS
```

해시는 파일 혼합·전송 오류를 감지하며 서명/공증을 대신하지 않는다. Mac은 ad-hoc 서명이고
Developer ID/공증 및 Windows Authenticode는 수행하지 않는다. 모든 산출물은 release-candidate,
`productionReady: false`다. 실제 Windows에서 실행하지 않았다면 Windows 자체 시험은 미실행이다.
기관 PC의 아이콘 실행·교수자 확장·인증·과제·제출·결과·C/C++ 작업·내보내기·정상/비정상 종료
정리와 서버 호환을 검증하고 그 증거를 기관 프로필에 연결한다.

설치 안내: [Windows 네이티브](../../ide/code-oss/windows/INSTALL-NATIVE-WINDOWS.md),
[Mac 앱](autograde-macos-app.md), [선택 Windows WSL2](autograde-windows-wsl-app.md).
