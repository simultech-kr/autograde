# 과제 다운로드 후 자동 열기 (0.5.1)

## 학생에게 보이는 동작

- Visual Studio 2022/2026: 저장할 상위 폴더 선택 → 새 고유 폴더에 다운로드 →
  그 위치를 Visual Studio의 폴더 작업 영역으로 자동 열기.
  자동으로 임의의 `.sln` 파일을 골라 로드하지는 않습니다.
- VS Code: 현재 수업 workspace 안에 다운로드 → 탐색기로 이동 → 과제 폴더 표시 →
  최상위 `main.cpp`, `main.c`, `README.md`, `README.txt`, `README` 중 첫 안전한 파일 열기.
  파일을 선택하면 해당 과제 폴더도 펼쳐집니다. 지정된 파일이 없으면 폴더만 표시합니다.
- VS Code는 새 창·단일 폴더에서 다중 루트 workspace로 전환하지 않습니다.
  Extension Host 재시작으로 메모리 전용 로그인 정보가 사라지는 일을 피합니다.
  WSL URI를 유지하며 Windows 탐색기를 따로 열지 않습니다.
- 이미 다운로드한 파일을 덮어쓰지 않습니다. 복원 기능·기존 Git clone 동작은 이번 변경에서 제외합니다.

## 실패·취소 처리

다운로드/압축 해제와 IDE 열기를 별도 단계로 처리합니다. IDE 열기 실패는 다운로드 실패로
취급하지 않으며 설치된 파일을 삭제하지 않습니다. Visual Studio에서는 표시된 경로와
**다운로드한 과제 폴더 열기** 버튼으로 재시도할 수 있습니다.
재시도할 때는 폴더의 서버·과제 식별자가 현재 선택 과제와 맞는지 확인합니다.
VS Code는 실패 안내에 표시된 경로를 현재 탐색기에서 직접 열 수 있습니다.

기존 편집 내용을 강제로 저장하거나 버리지 않으며 IDE의 저장·신뢰 확인창을 우회하지 않습니다.
확장이 명시적으로 빌드/실행을 시작하지는 않지만 Visual Studio의 CMake 자동 구성 등 IDE 자체
설정은 별도로 확인해야 합니다. 신뢰된 실습 자료만 내려받아 여세요.
VS Code의 자동 미리보기는 2 MiB 이하의 일반 파일만 대상으로 하며 심볼릭 링크를 제외합니다.

## 확인 절차

1. 확장을 0.5.1로 교체한 후 IDE를 재시작하고 수령 코드로 로그인합니다.
2. C++ 과제를 다운로드하여 저장한 위치가 탐색기/솔루션 탐색기에 표시되는지 확인합니다.
   VS Code에서는 `main.cpp`가 최상위에 있으면 편집기에 열려야 합니다.
3. C 과제는 `main.c`, 소스 없는 과제는 README, 둘 다 없는 과제는 폴더 표시를 확인합니다.
4. 한글·공백 경로에서 다운로드하고 파일 수정·저장·제출까지 이어서 확인합니다.
5. Visual Studio에서 기존 파일을 수정한 상태로 다른 과제를 다운로드합니다.
   저장 확인창에서 취소해 기존 편집 내용과 다운로드 파일이 모두 보존되는지,
   재시도 버튼으로 다운로드 없이 열 수 있는지 확인합니다.
6. VS Code WSL2에서 다운로드 후에도 WSL 연결·로그인이 유지되고 다른 과제 파일을
   섞지 않고 해당 과제만 제출되는지 확인합니다.

자동 검사에는 VS Code 파일 선택·경로/링크/크기 제한 시험, C# 공통 검사,
Python↔C# 연동 시험, IDE 호출 연결에 대한 소스 검사와 컴파일 검사가 포함됩니다.
**Windows IDE의 실제 저장·신뢰 확인창과 폴더 전환, WSL2 화면 조작은 현장 검증이 필요합니다.**
macOS의 compile-only 빌드는 설치 가능한 Visual Studio VSIX를 생성하지 않습니다.
Visual Studio 설치 파일은 Windows에서 [빌드 안내](../../extensions/visualstudio/DEVELOPMENT.md)에 따라 만드세요.

구현 참고: Visual Studio의
[IVsSolution7.OpenFolder](https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.shell.interop.ivssolution7.openfolder?view=visualstudiosdk-2022)
폴더 열기 API를 사용합니다.
