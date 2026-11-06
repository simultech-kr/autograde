# Windows IDE 시작 실패 진단

`Autograde Native Checks.exe`는 별도의 시험 폴더에서 권한, 복구, 프로세스 종료와 MSVC를
확인한다. 모두 PASS여도 실제 `%LOCALAPPDATA%\AutogradeNative` 폴더나 설치한 패키지의
파일·경로가 정상이라고 단정할 수 없다. 같은 Windows 계정에서 검사와 IDE를 실행한다.

진단 실행기는 공통 안내 대신 **실패 단계, 진단 코드, 예외 유형, HResult, 확인 가능한
Windows 오류 번호와 조치**를 표시한다. 최초 오류 뒤 정리에도 실패하면 두 오류를 순서대로
표시한다. 시작 실패와 종료 후 정리 실패를 모두 “새 실습을 시작하지 않았다”로 표시하지 않는다.

| 단계 | 확인 대상 |
| --- | --- |
| `package` | 실제 설치 폴더와 상위 경로의 링크 여부 |
| `package-node` | `runtime/node.exe` |
| `package-bootstrap` | `autograde/windows-native-bootstrap.mjs` |
| `package-manifest` | `windows-package.json` |
| `session-recovery` | 기존 작업 공간의 소유 표시·권한·이전 프로세스·복구 상태 |
| `session-create` | 새 세션과 고정 작업 경로 생성 |
| `toolchain` | MSVC와 Windows SDK 환경 |
| `process-create`, `runtime` | 프로세스 관리 준비와 실행 |
| `process-stop`, `session-cleanup` | 자식 프로세스 종료와 임시 파일 정리 |

진단 로그는 `%LOCALAPPDATA%\AutogradeNativeDiagnostics\launch-<고유값>.log`에 기록한다.
실패 창에 정확한 위치가 나온다. 현재 사용자 전용 권한으로 만들고 최근 10개를 유지하도록
정리한다. 학생 코드·학번·인증 정보·환경 변수·임의 예외 메시지·전체 파일 경로·스택은 기록하지
않는다. 고정된 오류 설명, 단계, 예외 유형, 오류 번호, 앱 버전과 UTC 시각만 남긴다.
로그 저장 자체가 실패해도 원래 진단은 화면에 표시한다.

## 전체 IDE를 다시 빌드하지 않고 진단 실행기 적용

수정 소스를 받은 **Autograde 저장소 루트**의 Windows PowerShell에서 진행한다.
IDE와 실행기 창을 먼저 정상 종료한다. 아래는 기존 압축 해제 패키지의 실행기 두 개와 대응하는
`autograde/windows-native-bootstrap.mjs`를 함께 교체하는 로컬 진단 절차다. 정책 marker와 환경 변수를
전달하는 계약이 바뀌었으므로 예전 bootstrap을 그대로 두지 않는다. 학생 작업 폴더는 변경하지 않는다.

이 절차만으로 새 정리 정책 기능 전체를 배포한 것으로 보지 않는다. IDE 내부의 정책별 종료 안내·
서버 마지막 저장·복구 안내까지 적용하려면 같은 소스 버전의 확장을 빌드하고 전체 Windows IDE
패키지를 다시 만든다. 운영 배포에서는 그 전체 패키지를 사용한다.

```powershell
$ErrorActionPreference = 'Stop'
$packagePath = (Resolve-Path -LiteralPath (Read-Host 'Autograde IDE.exe와 windows-package.json이 있는 폴더')).Path
$manifest = Get-Content -LiteralPath (Join-Path $packagePath 'windows-package.json') -Raw | ConvertFrom-Json
if ($manifest.platform -ne 'win32' -or $manifest.arch -ne 'x64') { throw 'Windows x64 패키지를 선택하세요.' }
$launcherBuild = Join-Path $env:TEMP ('autograde-launcher-' + [Guid]::NewGuid().ToString('N'))
& .\ide\code-oss\windows\Build-NativeLauncher.ps1 -OutputDirectory $launcherBuild -ExtensionVersion $manifest.extensionVersion -RunChecks
if (-not $?) { throw '실행기 빌드·검사가 실패했습니다. 기존 실행기는 교체하지 않습니다.' }

$launcherBackup = Join-Path (Split-Path -Parent $packagePath) ('autograde-launcher-backup-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $launcherBackup | Out-Null
$launcherFiles = @('Autograde IDE.exe', 'Autograde Native Checks.exe')
foreach ($name in $launcherFiles) { Copy-Item -LiteralPath (Join-Path $packagePath $name) -Destination (Join-Path $launcherBackup $name) }
Copy-Item -LiteralPath (Join-Path $packagePath 'autograde\windows-native-bootstrap.mjs') -Destination (Join-Path $launcherBackup 'windows-native-bootstrap.mjs')
foreach ($name in $launcherFiles) { Copy-Item -LiteralPath (Join-Path $launcherBuild $name) -Destination (Join-Path $packagePath $name) -Force }
Copy-Item -LiteralPath '.\ide\code-oss\windows-native-bootstrap.mjs' -Destination (Join-Path $packagePath 'autograde\windows-native-bootstrap.mjs') -Force
Write-Output "실행기 교체 완료. 이전 실행기: $launcherBackup"
```

이후 패키지 폴더의 `Autograde IDE.exe`를 다시 실행한다. 오류가 나타나면 **실패 단계, 진단 코드,
예외 유형, HResult, Windows 오류 번호**를 전달한다. 실제 원인을 확인하기 전에 소유 표시를
삭제하거나 작업 폴더 권한을 무조건 넓히지 않는다. 소유·프로세스 확인은 유지한다. 기본 정책은
`on_exit`로서 정상 종료 때 인증·IDE 상태와 학번별 코드·자료도 삭제한다.
서버에서 `next_start`를 확인한 실행만 코드·자료 삭제를 다음 실행으로 미룬다. 서버 정책을 확인하지 못하면
실행기 화면에 기본값 경고가 나온다. 기기 설정의 전용 ACL 설치는
[`INSTALL-NATIVE-WINDOWS.md`](../../ide/code-oss/windows/INSTALL-NATIVE-WINDOWS.md#담당자-정리-정책)를 따른다.

이 절차는 배포 ZIP과 체크섬을 갱신하지 않는다. 다른 PC에 배포할 최종 ZIP은 기존 패키징
절차로 다시 생성한다. 원래 실행기로 되돌릴 때는 IDE를 종료하고 백업의 두 파일을 원래
패키지 최상위에 복사하고 백업의 `windows-native-bootstrap.mjs`도 패키지 `autograde` 하위에 복구한다.

## 검증 범위

2026-11-04: 진단 문자열·단계 보존·이중 오류·비밀 정보 제외 등 30개 assertion을 macOS의
.NET 8에서 실제 실행했다. 실행기, Windows Native Checks와 별도 Diagnostic Checks를
C# 5 및 실제 .NET Framework 4.7.2 참조 API로 x64 컴파일했다.
Windows 권한·Job Object·실제 사용자 작업 폴더 정리는 이 환경에서 실행하지 않았다.
로그 권한·보관 개수·민감 정보 제외 검사는 Windows Native Checks에 포함되어
`Build-NativeLauncher.ps1 -RunChecks`에서 실행된다.

2026-11-05: 정책 형식·HTTPS origin·좌석/토큰 형식·주입한 요청 실패와 기본값·실행 중 스냅샷·
비밀 없는 경고를 확인하는 77개 assertion을 macOS .NET 8에서 실행했다. 실행기와 Windows Native /
Policy / Diagnostic Checks는 C# 5 및 실제 .NET Framework 4.7.2 참조 API로 x64 컴파일했다.
Windows Native Checks에는 기본 next-start 보존, 인증·캐시·빌드 삭제, 다음 실행의 새 식별자,
on-exit 전체 삭제, 정책 marker 불일치·링크·잠긴 파일 보호와 내보내기 보존 검사가 포함된다.
Windows ACL·Job Object·TLS 요청·실제 사용자 폴더 정리는 이 환경에서 실행하지 않았다.
