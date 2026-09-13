# 실습 1. 과제 마감 알림 게시판 — Observer

## 문제 상황

수업 서비스에서 과제 마감 30분 전 알림을 보내려고 합니다. 지금은 게시판이 VS Code 화면,
모바일 화면, 알림 집계 기능을 직접 호출합니다. 화면이 추가될 때마다 게시판 코드를 수정해야
하고, 로그아웃한 화면에도 알림이 전송되는 문제가 생깁니다.

`AssignmentBoard`는 **어떤 화면인지 몰라도 구독자에게 알림을 전달**해야 합니다.
학생은 인터페이스를 통해 구독·해지·일괄 알림을 구현합니다.

## 제공 코드와 작성 위치

`pattern.hpp`의 TODO 5개를 완성하세요. 공개 이름과 함수 인자는 바꾸지 않습니다.

- `Notice`: 과제명과 마감까지 남은 분.
- `Observer`: `update(const Notice&)`라는 공통 계약.
- `InboxObserver`: 받은 메시지를 순서대로 저장.
- `CountObserver`: 받은 알림 횟수 집계.
- `AssignmentBoard`: 구독자를 관리하고 `publish()` 때 알림 전달.

`main.cpp`, `check.cpp`는 수정하지 않습니다. `pattern.hpp` 외에 파일을 추가할 필요는 없습니다.
빈 코드는 컴파일되지만 TODO 예외를 내므로 아직 정답이 아닙니다.

## 필수 요구사항

1. `subscribe()`는 같은 객체를 두 번 등록해도 한 번만 구독합니다. `nullptr`는 무시합니다.
2. `unsubscribe()`는 해당 객체만 해지합니다. 없는 객체나 `nullptr`를 전달해도 안전해야 합니다.
3. `publish()`는 살아 있는 모든 구독자에게 등록 순서대로 한 번씩 전달합니다.
4. 게시판은 구독자의 수명을 소유하지 않습니다. 제공된 `weak_ptr` 저장소를 사용합니다.
   마지막 외부 `shared_ptr`가 사라진 구독자는 건너뛰어야 합니다.
5. 게시판은 `InboxObserver`, `CountObserver` 같은 구체 타입에 의존하지 않습니다.
6. 보관 메시지 형식은 정확히 `과제명 due in N min`입니다. 예: `lab03 due in 30 min`.

콜백 안에서 재구독·해지·재발행하는 경우, 멀티스레드, 콜백 예외의 개별 복구는 기본 실습 범위에서 제외합니다.
`shared_ptr`와 `weak_ptr`, 가상 함수의 기본 사용법을 먼저 복습하세요.

## 실행 확인

Linux / macOS / WSL2에서 starter 폴더를 열고 실행합니다.

```bash
c++ -std=c++17 -Wall -Wextra main.cpp -o observer-demo
./observer-demo
c++ -std=c++17 -Wall -Wextra check.cpp -o observer-check
./observer-check
```

macOS에서 `algorithm` 같은 표준 헤더를 찾지 못하면 컴파일 환경 문제부터 확인하세요.
`xcrun --show-sdk-path`가 정상이고 해당 SDK에 `usr/include/c++/v1`이 있다면:

```bash
c++ -std=c++17 -Wall -Wextra -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1" main.cpp -o observer-demo
./observer-demo
c++ -std=c++17 -Wall -Wextra -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1" check.cpp -o observer-check
./observer-check
```

SDK 조회도 실패하면 개발 도구 환경을 먼저 정비해야 합니다. 표준 헤더 include를 지우지 마세요.

정상 시연 출력:

```text
lab03 due in 30 min
lab03 due in 10 min
notifications=1
```

테스트는 모두 통과하면 마지막에 `4/4 checks passed`, 종료 코드 0을 출력합니다.
실패 시 FAIL 메시지와 종료 코드 1을 반환합니다. 테스트가 실행되었다는 사실만으로 통과한 것은 아닙니다.

VS2022/2026에서는 C++ 개발 도구를 설치한 후 **Developer Command Prompt**에서:

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:observer-demo.exe
observer-demo.exe
cl /nologo /std:c++17 /EHsc /W4 /utf-8 check.cpp /Fe:observer-check.exe
observer-check.exe
```

또는 Visual Studio의 **폴더 열기**로 이 폴더의 `CMakeLists.txt`를 열고 `observer_demo`와
`observer_check`를 각각 실행합니다. 두 cpp에는 각각 main이 있으므로 한 실행 파일로 합쳐 빌드하지 마세요.

## 제출과 프롬프트 확인

1. TODO를 채우고 공개 검사를 실행합니다.
2. [검토 프롬프트](REVIEW_PROMPT.md)에 `pattern.hpp` 전체와 실제 출력·종료 코드를 붙입니다.
3. AI의 지적을 직접 확인하고, 수정한 뒤 검사를 다시 실행합니다.
4. 최종 코드, 검사 결과, 아래 질문에 대한 본인 설명을 제출합니다.

- 게시판이 구체 화면 타입을 알면 어떤 변경 문제가 생기나요?
- `shared_ptr` 대신 `weak_ptr`로 보관한 이유는 무엇인가요?
- 단순히 main에서 두 화면을 직접 호출하는 것과 어떤 점이 다른가요?
