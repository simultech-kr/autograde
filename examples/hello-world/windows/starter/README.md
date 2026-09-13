# Hello World — Windows / Visual Studio 2022 Community

`main.cpp`를 수정하여 표준 출력에 아래 한 줄과 줄바꿈을 출력하세요. 입력은 없습니다.

```text
Hello, World!
```

1. Visual Studio Installer에서 **C++를 사용한 데스크톱 개발**을 설치합니다.
2. Visual Studio 2022 Community의 **파일 → 열기 → 폴더**에서 이 폴더를 엽니다.
3. CMake 구성이 끝나면 `hello` 대상을 빌드·실행합니다. WSL2는 필요하지 않습니다.
4. `main.cpp` 소스가 제출 대상입니다. 실행 파일이나 빌드 출력은 제출하지 않습니다.

대체 시험: x64 Native Tools Command Prompt for VS 2022에서
`cl /nologo /EHsc /std:c++17 main.cpp /Fe:hello.exe`로 빌드한 뒤 `hello.exe`를 실행합니다.

배점: 컴파일 2점, 정상 종료 3점, 정확한 출력 5점. 합계 10점입니다.
추가 안내 문구나 공백은 출력하지 않습니다. Windows의 CRLF 줄바꿈은 허용합니다.
이 과제는 기본 도구 연결 확인용이며 Windows API 자체를 평가하지 않습니다.

교수자가 Windows 설치 시험을 마친 Visual Studio용 Autograde VSIX를 배포하면
도구 → Autograde 과제에서 다운로드·제출·결과를 확인합니다. VS Code용 VSIX를 설치하지 마세요.
원격 Windows 채점은 미구현이며 현재 서버의 Hello World 채점은 POSIX 컴파일러를 사용합니다.
