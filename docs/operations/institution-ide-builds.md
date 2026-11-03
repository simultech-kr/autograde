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
Code-OSS `1.143.0`, commit `2d0b7506a36c1d6db9a466440131a8e01eab2289`, 빌드·번들 Node `26.13.0`이다.
upstream 소스의 `.nvmrc`는 `24.18.0`이며 `sourceNode`로 별도 보존한다. 준비 도구가 검증된
설치 전 검사에 Node 26 호환 변경을 적용한다. Electron과 확장 호스트의 Node는 upstream
Electron에 포함된 별도 런타임이며 이 빌드 Node 버전을 따른다고 가정하지 않는다.
확장과 EXE 버전은 `extensions/vscode/package.json`에서 읽는다. Assembly/FileVersion은 확장
버전에 `.0`을 붙이고 InformationalVersion은 확장 버전과 같다. 서버 호환·기관 주소·운영 정책은
별도의 기관 제품 프로필에서 승인한다.

GitHub의 [runner 표](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)에
따라 `macos-15`는 ARM64이고 Intel은 `macos-15-intel`이다. 실제 OS·architecture·Node도 검사한다.

## 실행과 검사

담당자의 로컬 PC에서 아래 명령으로 빌드할 수 있으며 GitHub Actions는 필수가 아니다.
각 대상 OS·CPU에서 Node 26.13.0과 해당 플랫폼의 빌드 도구를 준비한다. 기본 candidate 빌드는
로컬 PC에서도 변경·미추적 파일이 없는 깨끗한 commit에서만 시작한다. 작업 중인 변경 내용을
포함해 빌드하려면 명시적으로 `--local`을 사용한다. 이 경우 현재 작업 트리의 소스를 사용하고
보고서에 `status: local-build`, `buildMode: local`과 실제 `sourceDirty`를 기록한다.
시험·고정 버전/commit 검증·해시 검증·플랫폼별 서명 검사는 그대로 수행한다.

Actions를 사용할 때는 **Institution IDE candidates**에서 ref를 수동 선택한다. 기본은 세 가지
native OS이며 필요할 때만 `include_wsl`을 선택한다. 토큰은 `contents: read`만 요청하고
checkout 자격증명을 보존하지 않는다.

1. 빌드 검증 시험과 예제 기관 제품 정의를 검사한다. 예제를 운영 승인본으로 변경하지 않는다.
2. 기본 새 빌드는 OS별 새 경로에 고정 Code-OSS tag를 clone하고 실제 commit·원본 `.nvmrc`를 확인하고 관리된 Node 호환 변경을 적용한다.
3. 첫 빌드는 각 OS에서 의존성을 새로 설치한다. `--resume`은 같은 PC의 성공 기록과
   산출물을 검증한 뒤 설치 결과를 재사용한다. `node_modules`를 OS 간 전달하지 않는다.
   Linux에서 Node 26을 실행하기 전에 `libatomic1`을 설치한다. Electron native 모듈은
   Ubuntu 22.04의 Clang 15와 upstream Electron 헤더로 빌드한다. Node 자체를 소스에서 빌드하는
   도구 요구사항을 Electron addon 빌드 요구사항과 혼동하지 않는다.
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

공식 Node 26.13.0 배포본과 LICENSE를 사용한다. Linux/WSL 학생 좌석에도 `libatomic1`
(Ubuntu/Debian) 또는 `libatomic`이 필요하다. Mac 최소 버전은 계속 13.5다.
[Node 26.13.0 플랫폼 요구사항](https://github.com/nodejs/node/blob/v26.13.0/BUILDING.md#platform-list)과
[공식 LTS 릴리스 안내](https://nodejs.org/en/blog/release/v26.13.0)를 기준으로 한다.
Node 버전 변경만으로 기존 Windows/Linux 인수 결과를 새 버전의 통과 결과로 기록하지 않는다. 로컬 명령도 네트워크로 소스·의존성·Electron을 받는다.
기본 실행은 새 source/output 경로를 요구한다. 실패한 빌드의 같은 경로를 이어 쓰려면 아래의
`--resume`을 명시한다.

### Windows 빌드 PC의 C++ 구성 요소

IDE를 소스에서 빌드하는 PC에는 **C++ 데스크톱 개발**, MSVC x64/x86 도구, Windows SDK와
**빌드에 사용하는 MSVC 버전의 x64/x86 Spectre 완화 라이브러리**가 필요하다.
`@vscode/native-watchdog`는 `SpectreMitigation=Spectre`를 요구한다. C++ 워크로드만
설치되어 있고 이 선택 구성 요소가 빠지면 `npm ci`에서 `MSB8040`이 발생한다.

오류에 `Visual Studio\2022\BuildTools`가 표시되는 경우:

1. **Visual Studio Installer → Visual Studio Build Tools 2022 → 수정**을 연다.
2. **개별 구성 요소**에서 `Spectre`를 검색한다.
3. **MSVC v143 - VS 2022 C++ x64/x86 Spectre-mitigated libs (Latest)**를 선택한다.
   특정 MSVC 버전을 지정한 빌드는 그 버전의 라이브러리를 선택한다.
   일반 최신 버전의 구성 요소 ID는 `Microsoft.VisualStudio.Component.VC.Runtimes.x86.x64.Spectre`다.
4. 설치를 완료하고 빌드 터미널을 새로 연다. 별도 Community/Professional 설치본이 아니라
   오류 경로에 표시된 Build Tools 설치본에 추가해야 한다.

다음은 해당 설치본의 **기본 MSVC 도구 버전**을 대상으로 하는 읽기 전용 사전 점검이다.
`spectreX64Present`가 `True`인지 확인한다. `libcmt.lib`의 존재를 확인하는 점검이므로
라이브러리 전체의 무결성이나 실제 네이티브 컴파일 검증을 대신하지 않는다.

```powershell
$vsRoot = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools'
$toolset = (Get-Content -LiteralPath (Join-Path $vsRoot 'VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt') -Raw).Trim()
$spectre = Join-Path $vsRoot "VC\Tools\MSVC\$toolset\lib\spectre\x64"
[pscustomobject]@{
  installation = $vsRoot
  toolset = $toolset
  spectreX64Path = $spectre
  spectreX64Present = (Test-Path -LiteralPath (Join-Path $spectre 'libcmt.lib') -PathType Leaf)
}
```

`VCToolsVersion`, `VCToolsInstallDir` 등으로 도구 버전을 별도 지정했다면 위 기본 버전이
아닌 실제 지정 버전의 같은 경로를 확인한다. 다른 설치본이나 오래된 도구 버전에 라이브러리가
있어도 현재 빌드의 누락을 해결하지 못한다. 생성된 `.vcxproj`의 Spectre 설정을 끄거나
`node_modules`를 수정하여 우회하지 않는다.

설치 후 실패한 빌드와 같은 source/output 경로에 `--resume`을 추가해 다시 실행한다.
작업 중인 Autograde 변경 내용을 사용했다면 `--local`도 유지한다. 이전 도구로 실패해 상태 파일이
없는 경우에도 고정 버전의 Code-OSS checkout과 빈 output을 검증하면 소스를 다시 clone하지
않고 `npm ci`와 필요한 검사를 다시 수행한다. 자세한 조건은 아래 재개 안내를 따른다.

근거: [Microsoft MSB8040 안내](https://learn.microsoft.com/en-us/visualstudio/msbuild/errors/msb8040?view=vs-2022),
[Visual Studio Build Tools 구성 요소 목록](https://learn.microsoft.com/en-us/visualstudio/install/workload-component-id-vs-build-tools?view=vs-2022&preserve-view=true).

### 플랫폼별 전체 빌드 명령

```sh
node scripts/ide/build-runtime.mjs --target macos-arm64 --source /tmp/code-oss-mac-new --output /tmp/mac-artifacts-new
node scripts/ide/build-runtime.mjs --target linux-x64 --source /tmp/code-oss-linux-new --output /tmp/linux-artifacts-new
# 선택 WSL2까지 만들려면 Linux 명령에 --include-wsl 추가
```

```powershell
# autograde 저장소 루트에서 실행한다. Node 출력은 v26.13.0이어야 한다.
node --version
Test-Path .\scripts\ide\build-runtime.mjs
node .\scripts\ide\build-runtime.mjs --target windows-x64 --source .\.cache\code-oss-windows-01 --output .\dist\windows-native-01
# 선택 WSL2 launcher만 빌드:
.\scripts\ide\build-windows.ps1 -OutputDirectory .\dist\wsl-launcher-01
```

`--source`는 Code-OSS 위치이고 `--output`은 배포 ZIP·해시·보고서의 출력 위치다.
둘 다 현재 디렉터리 기준으로 해석한다. 기본 실행은 이미 존재하는 경로를 거절한다.
새 빌드를 시작할 때는 `-02`처럼 새 경로를 쓰고, 실패한 빌드를 재개할 때는 같은 경로와
`--resume`을 사용한다. 기존 소스 폴더를 자동 삭제하지 않는다. Windows 설정·요구사항은
아래의 전용 설치 안내를 따른다.

`Candidate builds require a clean committed checkout` 오류가 나고 현재 변경 내용을 직접
시험하려는 경우, 저장소 루트에서 다음과 같이 실행한다. `git status --short`는 변경 내용을
확인하기 위한 명령이다. 빌드를 위해 변경 내용을 stash·삭제하거나 모두 한꺼번에 commit할 필요는 없다.

```powershell
git status --short
node .\scripts\ide\build-runtime.mjs --target windows-x64 --source .\.cache\code-oss-windows-26-local-01 --output .\dist\windows-node26-local-01 --local
```

`--local`은 현재 Autograde 작업 트리 사용 여부를 정하며, 기존 빌드 재개 여부를 정하는
`--resume`과 별개다. `--resume` 없는 로컬 빌드도 새 source/output 경로를 요구한다.
로컬 빌드의 압축 파일 이름에는 `Autograde-IDE-<version>-local-<target>`과 고유한
`-build-<id>`가 포함되며, 확장자는 `.zip` 또는 `.tar.gz`다. 정확한 파일명은 최종
`build-<target>.json`의 `artifacts`에 기록한다. 재패키징해도 이전 ZIP을 덮어쓰지 않는다.
작업 트리가 깨끗해도 `--local`로 만든 보고서는 `local-build`이고, 후보 묶음 생성기는 이를
거절한다. 기관 배포 후보가 필요하면 별도의 깨끗한 checkout에서 `--local` 없이 빌드한다.

### 실패한 빌드 재개

`--resume`은 output의 `.autograde-build-state.json`에 기록한 진행 상태를 읽는다.
성공한 단계는 입력과 실제 산출물이 모두 검증될 때만 재사용하며, 실패하거나 검증되지 않은
단계는 다시 실행한다. Autograde 소스가 바뀌면 영향을 받는 단계의 기록을 무효화한다.
PC 이름·대상 OS·architecture·Node 버전·upstream pin·빌드 모드가 이전 빌드와 다르면 재개를 거절하므로,
이 경우에는 새 source/output 경로로 빌드한다. 상태 파일을 편집해 검증을 우회하지 않는다.

예를 들어 Spectre 라이브러리를 설치한 뒤, 앞의 로컬 빌드를 같은 두 경로에서 재시도한다.
실제로 다른 output 경로를 사용했다면 아래에도 그 원래 경로를 넣는다.

```powershell
node .\scripts\ide\build-runtime.mjs --target windows-x64 --source .\.cache\code-oss-windows-26-local-01 --output .\dist\windows-node26-local-01 --local --resume
```

처음 실행할 때부터 `--resume`을 붙여도 된다. 아직 상태 파일이 없는 기존 소스는 고정
Code-OSS 저장소·commit·Node 호환 변경을 검증한 뒤 재사용한다. output이 없거나 비어 있으면
새 체크포인트를 만든다. output에 이전 결과물이 남아 있으면 **폴더 전체를 같은 디스크의
고유한 형제 백업 경로로 옮긴 뒤**, 원래 output 경로에 새 체크포인트를 만든다.
예를 들어 `dist/windows-node26-local-03`의 기존 내용은
`dist/.autograde-legacy-output-<고유값>/output`에 보존한다. 파일이나 내부 링크를 따라가거나
삭제하지 않으며, 백업은 자동 삭제하지 않는다. 위치는 콘솔과 체크포인트의 `legacyOutput.backup`에
기록한다. 이 예약된 백업 폴더는 후보 산출물 탐색에서 제외하고, 직접 최종화 입력으로 지정해도 거절한다.

`Cannot resume a nonempty output without a build checkpoint`는 이전 재개 도구의 제한이다.
수정본을 적용하고 같은 `--source`, `--output`, 빌드 모드로 `--resume`을 다시 실행하면 된다.
기록이 없으면 이전 성공 단계를 추측하지 않으므로 이번에는 의존성 설치와 검사를 다시 수행하지만,
소스 clone은 반복하지 않는다. 이후에는 새 체크포인트로 검증된 성공 단계를 재사용한다.
폴더 이동이 Windows의 파일 잠금 때문에 실패하면 그 폴더를 사용하는 프로그램을 닫고 재시도한다.
이동 직후 중단되어 원래 output이 없거나 비어 있어도 백업은 남고, 같은 명령으로 재개할 수 있다.
손상된 체크포인트나 다른 빌드의 기록은 자동 백업·초기화하지 않고 계속 오류로 처리한다.

재개 중에도 기존 Code-OSS 소스를 지우지 않는다. 실패한 `npm ci` 단계 자체를 재시도할 때는
npm이 그 단계의 의존성 폴더를 다시 설치한다. 패키징·패키지 실행 검사는 하나의 재시도 단위이며
output 옆의 고유한 임시 작업 폴더를 사용한다. 실패한 패키징 폴더와 이전 ZIP은 보존한다.
최종 보고서는 모든 단계가 성공한 뒤 생성하며 재개 전 보고서는 별도 이름으로 보존한다.
상태 파일의 `steps`에서 단계별 `running`·`failed`·`passed`와 실패 원인을 볼 수 있다.
재개 기록은 실제 대상 OS에서의 빌드·실행 검증을
대신하지 않으며, 이 변경의 실제 Windows 네이티브 빌드는 여기서 검증하지 않았다.

### Windows에서 확장 테스트가 실패하는 경우

`EPERM ... symlink`가 테스트용 임시 폴더에서 발생하면 파일 링크를 만드는 권한이 없는
경우다. 폴더 별칭 검사는 Windows junction을 사용하며, 파일 심볼릭 링크를 만들 수 없는
Windows 환경에서는 그 개별 테스트만 사유를 출력하고 건너뛴다. 하드 링크·경로 이탈·인증
표식 변조 검사는 별도로 계속 실행한다. 다른 오류는 건너뛰지 않는다.

`438 !== 384`는 Windows의 파일 모드 표시 `0666`을 POSIX 전용 `0600`과 비교한
테스트 문제다. Windows 접근 권한은 실행기가 설정한 ACL로 관리하므로 POSIX 모드 비교는
POSIX에서만 실행한다. 또한 NTFS의 큰 파일 ID에 `1`을 더하는 방식으로 변조 값을 만들면
JavaScript 숫자 정밀도 때문에 원래 값과 같아질 수 있어, 테스트는 확실히 다른 값을 사용한다.

수정본을 적용한 뒤 아래 명령으로 관련 테스트를 먼저 확인할 수 있다.

```powershell
npm --prefix .\extensions\vscode run compile
node --test .\extensions\vscode\out\test\filesystemFixtures.test.js .\extensions\vscode\out\test\learningExport.test.js .\extensions\vscode\out\test\managedExtensionsView.test.js .\extensions\vscode\out\test\sharedLearningSession.test.js
```

실패가 없으면 원래 source/output 경로에 `--resume`을 추가해 빌드를 다시 실행한다.
작업 중인 변경을 시험한다면 `--local`도 유지한다. 건너뛴 파일 심볼릭 링크
검사는 통과한 검사와 구분하여 기록하며, 최종 인수에는 링크 생성이 가능한 Windows 시험
환경의 결과도 포함한다. 일반 빌드 담당자에게 관리자 실행을 요구하지 않는다.

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
Developer ID/공증 및 Windows Authenticode는 수행하지 않는다. 기본 산출물은 `release-candidate`,
`--local` 산출물은 `local-build`이며 모두 `productionReady: false`다.
실제 Windows에서 실행하지 않았다면 Windows 자체 시험은 미실행이다.
기관 PC의 아이콘 실행·교수자 확장·인증·과제·제출·결과·C/C++ 작업·내보내기·정상/비정상 종료
정리와 서버 호환을 검증하고 그 증거를 기관 프로필에 연결한다.

설치 안내: [Windows 네이티브](../../ide/code-oss/windows/INSTALL-NATIVE-WINDOWS.md),
[Mac 앱](autograde-macos-app.md), [선택 Windows WSL2](autograde-windows-wsl-app.md).
