# Visual Studio 확장 0.5.0: 학생 결과·제출 UX·테마와 업데이트 배포

2026-09-16. **Visual Studio 2022/2026 Community x64용 VSIX** 안내다.
VS Code 확장은 별도 제품/패키지/Marketplace 항목이며 이 문서의 설치 파일을 사용할 수 없다.

## 이번 변경과 설치

- 현재 **0.5.0**. 테마·접수 분리에 더해 결과 카드를 추가했으며 기존 VSIX ID와 패키지 GUID 유지.
  [학생 결과 화면·관리자 안내](results-and-course-admin.md).
- 제출 접수 직후 작업 버튼 활성화, 별도 영역에서 백그라운드 채점 확인.
  [상세 동작·UI 검토·시험 절차](../architecture/visualstudio-submission-ux.md).
- 도구 창, 설명, 주소/결과 입력창, 버튼은 Visual Studio 동적 색상/스타일 사용.
- 과제/제출 이력은 선택 활성·비활성·포커스·비활성화 상태의 글자/배경을 함께 적용.
- 수령 코드는 PasswordBox를 유지하고 마스킹·지우기 동작을 보존. 일반 텍스트로 바꾸지 않는다.
- 고정 RGB나 현재 테마의 색상 복사 대신 동적 자원 참조를 사용하여 실행 중 테마 변경에 대응.
- 제목에 실제 어셈블리 버전을 표시한다. 학생 인증·서버 프로토콜은 변경하지 않았다.

Windows 개발 PC에서 수정된 소스를 받은 후:

```powershell
cd autograde\extensions\visualstudio
.\build.ps1
```

생성 파일:

```text
Autograde.VisualStudio\bin\Release\net472\Autograde.VisualStudio.vsix
```

기존 0.2.0이 설치된 시험 PC에서 Visual Studio를 종료하고 새 VSIX를 실행한다.
대상 IDE를 확인하여 업데이트한 뒤 **도구 → Autograde 과제**에서 버전 0.5.0을 확인한다.
다른 VS 인스턴스를 사용하는 경우 인스턴스별 설치 상태를 확인한다. 서버 재시작만으로는
학생 PC의 확장이 바뀌지 않는다. 서명/설치 정책을 끄지 말고 학교 정책에 맞춰 검증한다.

`build.ps1`은 필수 DLL뿐 아니라 최종 VSIX의 ID·버전이 소스 manifest와 같은지도 검사한다.
macOS의 compile-only DLL은 설치용 VSIX가 아니므로 배포하지 않는다.

## 권장: Marketplace 자동 업데이트

Community 학생 PC의 지속적인 배포에는 **Visual Studio Marketplace**를 권장한다.
VS가 새 확장 버전을 확인·설치하며 적용에는 IDE 종료/재시작이 필요할 수 있다.
파일을 웹사이트에 올리는 것만으로 이 기능이 활성화되지는 않는다.
[Microsoft 자동 업데이트 안내](https://learn.microsoft.com/en-us/visualstudio/ide/finding-and-using-visual-studio-extensions?view=vs-2022).

### 교수자/개발자: 최초 설정

1. Windows에서 빌드하고 아래 설치·테마 점검을 완료한다.
2. Marketplace에 Microsoft 계정으로 로그인하고 게시자 계정을 준비한다.
3. 새 확장 종류는 **Visual Studio**를 선택한다. Visual Studio Code가 아니다.
4. VSIX, 설명, 지원 버전, 사용 안내 등 배포 자료를 등록하고 공개 여부를 결정한다.
5. 시험 PC에서 Marketplace 설치/업데이트와 서버 연결을 확인한 뒤 학생들에게 안내한다.

이미 등록한 확장이 있다면 새 항목을 만들지 않고 기존 항목을 업데이트한다.
게시자 계정과 공개 배포 승인은 운영자가 결정한다. 이번 작업에서 실제 게시하지 않았다.
[Microsoft 게시 절차](https://learn.microsoft.com/en-us/visualstudio/extensibility/walkthrough-publishing-a-visual-studio-extension?view=vs-2022).

### 학생: 한 번 설정

1. **확장 → 확장 관리**에서 해당 확장을 설치한다.
2. **도구 → 옵션 → 환경 → 확장**에서 업데이트 확인과 자동 설치가 활성화되어 있는지 확인한다.
   버전에 따라 `모든 설정` 아래에 있거나 메뉴 명칭이 다를 수 있다.
3. 확장 상세의 **이 확장 자동 업데이트**도 확인한다. 학교의 관리 정책이 우선한다.
4. 업데이트 대기 안내가 있으면 작업을 저장하고 IDE를 종료/재시작한다.

자동 업데이트는 즉시 원격 강제 교체가 아니다. 오프라인 상태, 사용자 설정, 학교 정책,
호환성에 따라 늦어지거나 차단될 수 있다.
[Microsoft 설정 안내](https://learn.microsoft.com/en-us/visualstudio/ide/configure-extension-update-options?view=visualstudio).

### 다음 릴리스

- 동일한 VSIX ID `Autograde.VisualStudio.74db5571-a3ad-4451-a5f4-e8cc28d20536`를 유지한다.
- manifest, 프로젝트 Version, InstalledProductRegistration, 인증 요청의 extension_version을 함께 증가시킨다.
- 시험을 통과한 새 VSIX를 기존 Marketplace 항목에 업로드한다.
- 0.2.0 수동 설치본 → 게시 버전 전환은 시험 PC에서 먼저 확인한다. 업데이트가 탐지되지
  않으면 VS를 재시작하고 확장 관리의 업데이트/호환 상태 및 설치 인스턴스를 확인한다.
- 문제가 생기면 이전 소스로 **더 높은 수정 버전**을 만들어 배포한다. 자동 다운그레이드를 가정하지 않는다.

## 대안: 자체 서버/비공개 갤러리

Visual Studio에는 VSIX와 Atom 피드를 제공하는 private gallery 방식도 있다.
단순 다운로드 링크와는 다르며 PC에 갤러리 등록이 필요하다.
[Microsoft 비공개 갤러리 안내](https://learn.microsoft.com/en-us/visualstudio/extensibility/private-galleries?view=visualstudio).

현재 설정 문서는 이를 **Enterprise feature**로 설명하므로 Community 전체에서 사용할 수
있다고 가정하지 않는다. 학교의 정확한 IDE 버전/에디션에서 등록·업데이트를 먼저 검증한다.
가능하면 검증된 HTTPS 주소의 정적 배포 경로를 별도로 정하고 VSIX와 피드를 함께 관리한다.
학생 비밀번호/수령 코드는 업데이트 피드에 넣지 않는다. 이번 작업에서는 서버 경로나
피드를 만들지 않았고, 자체 다운로드·자동 실행/설치 코드도 추가하지 않았다.

## 시험 기록과 Windows 인수 점검

macOS에서 .NET SDK 8.0.425로 WPF/VSSDK compile-only 빌드: 경고 0, 오류 0.
확장 관련 Python/C# 연동 시험: **19개 통과**. 이 중 테마/버전/이벤트 결선 확인은 소스 계약 검사이며
실제 WPF 렌더링 시험이 아니다. Windows VSIX 생성·설치·Marketplace 업데이트는 미실시다.
공통 C# 자체 점검도 **117개 통과**했다. 이 점검은 연동 시험에서도 호출되므로 개수를 합산하지 않는다.

VS2022/VS2026 각각 다음을 확인하고 배포한다:

- 이전 버전 위에 0.5.0 설치 후 창에 표시되는 버전과 기존 서버 주소 보존.
- 창을 연 채 밝음/어두움/파랑 전환. Windows 고대비와 사용자 지정 테마도 점검.
- 주소, 비밀번호 마스킹/커서, 읽기 전용 폴더/결과, 과제·이력의 선택 활성/비활성 가독성.
- Tab 이동·포커스, 마우스 hover, 연결/제출 중 비활성 상태, 취소 버튼.
- 다운로드·제출·결과·이력·로그아웃이 이전과 동일하게 동작하는지 확인.
- 시험용 새 버전 탐지·다운로드·재시작 후 반영. 업데이트 서버 불통 중에도 과제 작업 유지.

이번 작업은 게시 계정 생성, 서명, 원격 업로드, 학생 PC 설정 변경을 수행하지 않았다.
