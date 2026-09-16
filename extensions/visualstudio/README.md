# Autograde for Visual Studio — VS2022 / VS2026

C/C++ 수업의 과제 수령·다운로드·제출·결과 조회용 **Windows x64 파일럿 확장**입니다.
VS Code용 확장과는 별도 VSIX이지만 같은 서버/API와 학생 계정을 사용합니다.

## 현재 상태

- 0.5.0: **학생 결과 카드**. 상태·큰 점수·총점 달성률·수정 필요 항목·다음 할 일을 먼저 표시하고
  전체 진단은 접어서 제공합니다. 최신/과거 제출을 구분합니다.
  [결과 화면·교과목/분반 관리자·현장 시험](../../docs/operations/results-and-course-admin.md).
- 0.4.0: **제출 접수와 백그라운드 채점 확인 분리**. 접수 후 작업 버튼을 즉시 다시 활성화하며
  마지막 접수의 과제명·접수번호·채점 상태를 별도 표시합니다.
  [동작 범위·UI 개선 검토·현장 시험](../../docs/architecture/visualstudio-submission-ux.md).
- 0.3.0: **Visual Studio 테마 연동**. 입력창·버튼·과제/이력 선택·수령 코드 마스킹 창의
  색상을 동적 자원으로 연결하고, 기존 VSIX ID를 유지하여 업데이트 가능하게 버전을 올렸습니다.
  [설치·자동 업데이트 배포 안내](../../docs/operations/visualstudio-updates.md).
- 0.2.0: **제출 기록 조회 / 선택 기록의 결과 확인 / 새 폴더 복원** 구현.
  [사용·시험 절차](../../docs/operations/submission-history-mvp.md). 서버 접수본만 보관하며 로컬 초안 백업은 제외합니다.

- [예외 상황 점검·수정 내역](../../docs/operations/visualstudio-exception-review.md): 인증 경합, 중복 제출, 잘못된 응답과 취소 처리.
- 확장 창, 수령 코드 로그인, 과제 목록·다운로드, 제출 파일 미리보기, 제출·결과 조회 구현.
- 메모리 전용 인증, 토큰 갱신·로그아웃, 30초 연결 확인, 요청 취소·시간 제한 구현.
- C# 공통 모듈과 WPF/VSSDK 코드 컴파일 확인. 서버와의 교차 언어 자동 시험 제공.
- **Windows에서 VSIX 패키징·설치·실제 창 조작은 아직 검증하지 않았습니다.** 현재 개발 환경은 macOS입니다.
- Marketplace 게시·서명·자동 업데이트 채널 개설은 아직 하지 않았습니다. Marketplace에 게시하고
  학생 PC의 자동 업데이트 설정을 활성화하는 권장 절차를 문서화했습니다. 설치 검증 전 일괄 배포는 보류합니다.
- Windows 원격 채점 작업자는 이번 확장과 별개이며 아직 미구현입니다. `windows.h`, Win32 API 과제를
  POSIX 서버에 보내 Windows 채점을 대신할 수 없습니다. 현재 서버 리허설은 이식 가능한 C/C++로 한정합니다.

VS2026은 17.x API를 지원합니다. Microsoft가 안내한 공통 설치 범위 `[17.0,)`와 `amd64`를
사용하고, VS2022 17.0 SDK의 안정 API를 참조합니다. 이는 호환성 대상 선언이지 두 IDE에서의
실행 검증을 뜻하지 않습니다. [Microsoft 호환성 모델](https://learn.microsoft.com/en-us/visualstudio/extensibility/migration/extension-compatibility?view=visualstudio).
이번 패키지는 x64 Community를 대상으로 하며 ARM64와 다른 에디션은 별도 검증 대상입니다.

## 1. Windows에서 빌드

교수자/개발자 PC에 다음을 설치합니다. 학생 PC에는 확장 개발 도구가 필요 없습니다.

- 최신 업데이트가 적용된 Visual Studio 2022 또는 2026 Community x64
- Visual Studio Installer의 **Visual Studio 확장 개발** 워크로드
- **C++를 사용한 데스크톱 개발** 워크로드(C/C++ 실습용)
- .NET Framework 4.7.2 targeting pack와 .NET 8 SDK(공통 모듈 시험용)

저장소를 받은 뒤 PowerShell에서 실행합니다. 운영용 환경변수나 GitHub OAuth는 필요 없습니다.

```powershell
cd autograde\extensions\visualstudio
.\build.ps1
```

스크립트는 설치된 VS의 MSBuild를 찾아 공통 시험 → 확장 빌드 → VSIX의 필수 파일 검사를 합니다.
실행 정책 때문에 막히면 학교의 승인된 PowerShell 정책에 따라 실행합니다. 정책을 영구 해제하지 않습니다.
출력 파일:

```text
Autograde.VisualStudio\bin\Release\net472\Autograde.VisualStudio.vsix
```

VSIX에는 `Autograde.VisualStudio.dll`, 등록용 `.pkgdef`, `Autograde.Core.dll`, `Newtonsoft.Json.dll`이
포함되어야 합니다. 스크립트가 출력하는 SHA-256으로 전달 파일이 같은지 확인할 수 있습니다.
`AutogradeCompileOnly=true` 빌드는 컴파일 점검 전용이며 **설치 가능한 확장이 아닙니다**.

### `VSIX dependency missing: Newtonsoft.Json.dll` 오류

VSSDK는 `Newtonsoft.Json.dll`을 기본 VSIX 제외 목록에 넣습니다. 따라서 DLL이 빌드 폴더에
있어도 설치 파일에서 누락될 수 있습니다. `ForceIncludeInVSIX="true"`만으로 해결되지 않는
환경에서는 자동 참조 포함에 의존하지 않고 복원된 NuGet DLL을 명시적인 VSIX Content로 넣습니다.
확장 프로젝트에는 Core와 같은 버전의 `PackageReference`를 **하나만** 두고
`GeneratePathProperty="true"`를 지정합니다. 이어서 `$(PkgNewtonsoft_Json)/lib/net45/Newtonsoft.Json.dll`을
`Content`로 포함하고 `Link=Newtonsoft.Json.dll`, `IncludeInVSIX=true`로 설정합니다.
net45 어셈블리는 이 확장의 net472 대상과 호환됩니다. 이전 `ForceIncludeInVSIX` 항목은
중복 포함되지 않도록 교체하며, 사용자 PC의 절대 NuGet 경로나 임의 DLL 다운로드는 사용하지 않습니다.
수정된 `Autograde.VisualStudio.csproj`를 Windows 체크아웃에 반영한 뒤 `./build.ps1`을 다시
실행하세요. 스크립트가 Rebuild와 최종 ZIP 내용 검사를 수행합니다. `build.ps1`의 누락 검사를
삭제하거나 Visual Studio 설치 폴더에서 임의 버전의 DLL을 복사하지 마세요.
이 변경의 실제 Windows VSIX 생성·설치 확인은 별도로 필요합니다.

## 2. 설치와 학생 사용

1. VS2022/VS2026을 모두 종료합니다. 위 **Visual Studio용** VSIX를 실행해 대상 IDE를 선택합니다.
   기존 `extensions/vscode/*.vsix`는 Visual Studio에 설치할 수 없습니다.
2. IDE를 다시 실행하고 **도구 → Autograde 과제**를 엽니다. 이후 이 창의 버튼으로 사용합니다.
3. API 주소에 `https://<도메인>:20000`을 입력하고 **주소 적용 / 연결 확인**을 누릅니다.
   IP 주소도 입력할 수 있지만 외부 HTTPS IP에는 해당 IP를 포함하는 유효한 인증서가 필요합니다.
   같은 PC 테스트만 `http://127.0.0.1:20000`을 허용합니다. 외부 HTTP·인증서 검증 해제는 지원하지 않습니다.
4. 브라우저에서 `https://<도메인>:20010`에 접속해 교과목과 학번·전용 비밀번호를 입력하고 과제를 선택합니다.
5. 발급된 `AK1-XXXX-XXXX-XXXX`를 확장에 입력하고 **코드로 로그인**을 누릅니다.
   숫자 6자리 비밀번호 자체를 확장에 입력하는 것이 아닙니다.
6. **선택 과제 다운로드**에서 저장할 상위 폴더를 선택합니다. 새 고유 폴더를 만들며 기존 작업은 덮어쓰지 않습니다.
7. VS의 **파일 → 열기 → 폴더**로 받은 폴더를 엽니다. 확장이 CMake/빌드 스크립트를 자동 실행하지 않습니다.
8. 문제를 해결하고 **모두 저장(Ctrl+Shift+S)**합니다. **파일 확인 후 제출**에서 파일 목록·과제·경로를 확인해 승인합니다.
9. 접수번호가 표시되면 다른 작업을 할 수 있습니다. 마지막 접수의 채점 상태는 별도 영역에서
   최대 2분간 5초 간격으로 확인합니다. 자동 확인 종료 후에는 해당 과제를 선택하고
   **최신 결과 확인** 또는 **제출 기록 조회 → 선택 기록의 결과 확인**으로 조회합니다.
   조회 장애나 로그아웃은 서버에 접수된 제출·채점을 취소하지 않습니다.
10. 공용 자리에서 떠나기 전 **로그아웃 / 자리 비우기**를 누릅니다.

주소만 `%LOCALAPPDATA%\AutogradeVS\service.txt`에 저장됩니다. 수령 코드·access/refresh token·비밀번호는
저장하지 않습니다. IDE 종료 후에는 새 코드가 필요합니다. **도구 창을 숨기는 것만으로 로그아웃되지 않습니다.**
다운로드 폴더와 학생 소스는 자동 삭제하지 않으므로 학교 PC 정리 절차를 따릅니다.
오프라인 로그아웃도 메모리 인증은 지우지만 서버 세션 폐기는 성공하지 않을 수 있습니다.

서버 주소 변경 시 기존 세션의 폐기를 시도하고 메모리 인증을 지웁니다. 수령·교환 중 통신이 끊겨
코드가 이미 소비되었으면 웹에서 재발급합니다. 요청 취소는 서버에 접수된 제출을 철회하지 않습니다.
제출 응답 유실 시 우선 최신 결과를 조회하세요. 같은 세션에서 같은 파일을 재시도하면 같은 요청 키를
사용합니다. 같은 세션에서 같은 내용의 제출이 이미 성공했다면 기존 접수 기록을 재사용하고
새 제출을 만들지 않습니다. 키·접수 기록은 합계 128개까지만 메모리에 유지합니다.
IDE 재시작 또는 로그아웃 후에는 재시도 키도 복원하지 않습니다.
인증 갱신 응답이 유실되면 소비된 토큰을 재사용하지 않고 새 코드 로그인을 요청합니다.

## 3. C/C++ 제출 범위와 안전 경계

- 파일 형식: `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, `.inl`, `.rc`, `.txt`, `.md`, `.cmake`.
  `CMakeLists.txt`도 포함됩니다. `.sln`, `.vcxproj`, `.exe`, `.dll`, `.obj`, `.pdb`, CSV와 임의 바이너리는 제외합니다.
  채점 계약은 소스 중심이어야 합니다. 추가 데이터·프로젝트 파일을 요구하는 과제는 이 파일럿 범위를 먼저 확장해야 합니다.
- `.vs`, `.git`, `.autograde`, `build`, `out`, `bin`, `obj`, `Debug`, `Release`, `x64`, `x86`, `arm64` 및 숨김 항목 제외.
  소스 폴더에 이 이름을 쓰면 제출에서 빠지므로 사용하지 않습니다. 제출 직전 목록을 반드시 확인합니다.
- 최대 5000개 탐색 항목, 소스+manifest 100 MiB, 압축 25 MiB. 깊은 폴더와 비정규 Unicode 이름은 거부합니다.
- SHA-256·manifest 검증, 경로 탈출·심볼릭 링크·junction·Windows 장치명·대소문자 충돌 방어.
  Windows 제출 파일의 hard link도 거부합니다. 공유 폴더에서 다른 프로세스가 동시에 파일을 교체하는 상황을
  완전히 격리하는 보안 도구는 아니므로 학생 자신의 로컬 작업 폴더를 사용합니다.
- 현재 서버의 `pilot-local`은 sandbox가 아닙니다. 사전 검토한 신뢰 코드만 시험합니다.

## 4. Hello World 등록

서버의 저장소 루트에서 Windows용 C starter를 등록합니다. C++는 아래 경로의 `c/windows/starter`를
`windows/starter`로, `c/data`를 `data`로 바꾸고 별도 assignment/release/rubric 이름을 사용합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-add hello-world-c-vs \
  --release-id hello-world-c-vs-v1 --title 'Hello World - C17 / Visual Studio' \
  --starter examples/hello-world/c/windows/starter \
  --assessment examples/hello-world/assessment --data examples/hello-world/c/data \
  --rubric-version hello-world-c-vs-v1 --max-score 10 --result-policy immediate
.venv/bin/autograde-pilot --config pilot/come3105.csv
```

서버는 여전히 Linux/macOS에서 실행합니다. 외부 학생 PC라면 위 localhost 기본 설정 대신
[HTTPS 서버 안내](../../docs/operations/independent-web-pilot.md)의 도메인 설정을 사용합니다.
최초 명단 초기화와 웹 20010/API 20000 구분은 동일합니다. Hello World C 정답은 10점,
아무것도 출력하지 않는 starter는 5점입니다. Windows 전용 API 호환성을 확인하는 과제가 아닙니다.

## 5. 검증

공통 로직 시험은 Windows/Linux/macOS의 .NET 8 SDK에서 실행할 수 있습니다.

```text
dotnet run --project extensions/visualstudio/Autograde.Checks/Autograde.Checks.csproj
```

서버 저장소 루트에서 .NET SDK가 PATH에 있는 경우 다음으로 실제 포털 연동도 검증합니다.
명단·코드·DB는 시험용 임시 데이터만 사용합니다. 마지막 시험은 POSIX 서버에서 C17을 컴파일합니다.

```bash
.venv/bin/python -m pytest -q tests/integration/test_visualstudio_extension.py
```

점검 범위: 주소 검증, 메모리 세션·갱신·오프라인 로그아웃, 재시도 요청 키, 결과 대기/공개,
C#→Python 제출, Python→C# starter(한글·빈 폴더), 악성 archive 거부, 실제 웹 코드→C# 인증→
다운로드→C Hello World 제출→서버 컴파일·10점→결과→로그아웃.

2026-09-10 최초 구현의 macOS 검증 기록: 공통 점검 55개 통과, 확장 전용 Python 시험 13개 통과,
WPF/VSSDK C# compile-only 빌드 경고·오류 0개. 전체 서버 회귀 실행은 748개 통과·7개 건너뜀
(Windows/MSVC 2개, 기존 Java/JDK 5개)이었으며, 이후 추가한 VSIX 등록 정적 점검은
위 확장 전용 13개 실행에 포함했습니다. 이 기록은 Windows 설치 시험을 대체하지 않습니다.

이후 예외 점검 수정에서는 공통 점검 73개(신규 예외 18개 포함), 확장·포털 시험 20개가 통과했습니다.
최종 compile-only 빌드도 경고·오류 0개이며, 상세 범위는 위 예외 점검 문서를 참고하세요.

배포 전에는 **VS2022와 VS2026 각각** 다음 수동 시험을 통과하고 버전·결과를 기록합니다.

| 항목 | VS2022 x64 | VS2026 x64 |
| --- | --- | --- |
| VSIX 설치·재시작·도구 메뉴/창 열기 | 미검증 | 미검증 |
| HTTPS 주소 적용·코드 로그인·다운로드 | 미검증 | 미검증 |
| C/C++ 로컬 MSVC 빌드 | 미검증 | 미검증 |
| 저장 후 제출·점수·항목별 피드백 | 미검증 | 미검증 |
| 통신 단절·다시 조회·재시도·취소 | 미검증 | 미검증 |
| 주소/과제 전환·로그아웃·IDE 재시작 | 미검증 | 미검증 |
| 창 숨기기/복원·확장 제거 | 미검증 | 미검증 |
| 이전 버전 → 0.5.0 설치·버전 표시 | 미검증 | 미검증 |
| 결과 카드·수정 필요 항목·과거/최신 구분·진단 펼치기 | 미검증 | 미검증 |
| 접수 직후 버튼 활성화·백그라운드 조회·학생 전환 시 응답 격리 | 미검증 | 미검증 |
| 밝음/어두움/파랑/고대비·선택·비활성·포커스 | 미검증 | 미검증 |

서명/설치 정책으로 거부되는 경우 조직의 정책과 VSIX 설치 로그를 확인합니다. 검증을 전역으로
끄는 방식으로 배포하지 않습니다. Marketplace 게시와 업데이트 채널은 두 버전 설치 검증 후 별도 진행합니다.
