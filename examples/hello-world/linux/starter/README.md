# Hello World — Linux / WSL2

`main.cpp`를 수정하여 표준 출력에 아래 한 줄과 줄바꿈을 출력하세요. 입력은 없습니다.

```text
Hello, World!
```

Windows에서는 VS Code의 WSL 창에서 과제 폴더를 엽니다. Linux에서는 일반 창을 사용합니다.

```bash
g++ -std=c++17 -Wall -Wextra main.cpp -o hello
./hello
```

배점: 컴파일 2점, 정상 종료 3점, 정확한 출력 5점. 합계 10점입니다.
추가 안내 문구나 공백은 출력하지 않습니다. 제출에는 소스만 필요합니다.
생성한 `hello` 실행 파일은 제출 전에 제거하세요. Autograde 확장에서 제출 후 결과를 확인합니다.
