# Hello World — C17 / Windows / VS2022 Community

`main.c`를 수정해 `Hello, World!` 한 줄과 줄바꿈을 출력하고 정상 종료하세요. 입력은 없습니다.
VS2022 Community에 C++를 사용한 데스크톱 개발을 설치하고 **파일 → 열기 → 폴더**로 이 폴더를
열어 CMake 프로젝트를 빌드합니다. 워크로드 이름은 C++이지만 이 과제는 **C 언어**로 컴파일합니다.
WSL2는 필요하지 않습니다.

x64 Native Tools Command Prompt for VS 2022에서도 시험할 수 있습니다.

```text
cl /nologo /TC /std:c17 main.c /Fe:hello.exe
hello.exe
```

컴파일 2점, 정상 종료 3점, 출력 일치 5점으로 총 10점입니다. CRLF 줄바꿈은 허용합니다.
수정·제출 대상은 `main.c`이며 C++ 코드나 `main.cpp`로 바꾸지 마세요.
이 과제는 개발 도구의 연결 확인용이며 Windows API 자체를 평가하지 않습니다.
교수자가 Windows에서 설치 시험을 마친 Visual Studio용 Autograde VSIX를 배포한 경우
도구 → Autograde 과제에서 코드를 입력하고 제출·결과를 확인합니다. VS Code용 VSIX와는 다릅니다.
원격 Windows 채점은 미구현이며, 현재 서버의 Hello World 채점은 POSIX 컴파일러를 사용합니다.
