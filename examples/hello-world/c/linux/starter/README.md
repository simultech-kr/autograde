# Hello World — C17 / Linux·WSL2

`main.c`를 수정해 `Hello, World!` 한 줄과 줄바꿈을 출력하고 정상 종료하세요. 입력은 없습니다.
Windows에서는 VS Code의 WSL 창, Linux에서는 일반 VS Code 창으로 과제 폴더를 엽니다.

```bash
gcc -std=c17 -Wall -Wextra main.c -o hello
./hello
```

컴파일 2점, 정상 종료 3점, 출력 일치 5점으로 총 10점입니다. 불필요한 안내 문구나 공백은 출력하지
마세요. 수정·제출 대상은 `main.c`입니다. 생성한 실행 파일은 제출 전에 제거합니다.
Autograde 확장에서 제출 후 결과를 확인하세요. C++ 코드나 `main.cpp`로 바꾸지 마세요.
