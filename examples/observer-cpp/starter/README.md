# 과제: Observer 패턴으로 상태 변경 알림 구현하기

`Subject`의 상태가 바뀌면 등록된 `Observer`에게 변경된 정수 상태를 알리는 기능을
완성하세요.

## 수정할 파일

`observer.cpp`의 TODO 부분을 구현합니다. `observer.hpp`에 선언된 공개 인터페이스는
채점 코드가 직접 사용하므로 이름과 함수 시그니처를 변경하지 마세요. `main.cpp`는
로컬 확인용이며 자유롭게 수정해도 채점에 사용되지 않습니다.

## 요구사항

1. `Subject`의 최초 상태는 `0`입니다.
2. `attach`는 유효한 Observer를 등록합니다.
3. `nullptr`와 이미 등록된 Observer는 무시합니다.
4. `setState`는 상태를 먼저 변경한 후, 현재 등록된 Observer를 등록 순서대로 한 번씩
   호출합니다.
5. `detach` 후에는 해당 Observer를 호출하지 않습니다. 등록되지 않은 Observer나
   `nullptr`를 해제해도 안전해야 합니다.
6. 알림 처리 중 `attach` 또는 `detach`가 호출되면 그 변경은 **다음** `setState`부터
   적용합니다. 현재 알림을 시작할 때 등록되어 있던 Observer는 이번 알림을 한 번씩
   받습니다.

`Subject`는 Observer를 소유하지 않습니다. 등록된 Observer는 `detach`되거나 Subject가
파괴될 때까지 살아 있어야 합니다.

## 로컬 실행 예시

```bash
c++ -std=c++17 -Wall -Wextra -pedantic observer.cpp main.cpp -o observer-demo
./observer-demo
```

완성된 예제는 다음을 출력합니다.

```text
display-a: 10
display-b: 10
display-b: 20
```

## 채점 기준 (10점)

- 컴파일 및 공개 인터페이스: 2점
- 상태 갱신 후 기본 알림: 2점
- 여러 Observer의 등록 순서와 반복 알림: 2점
- `nullptr` 및 중복 등록 방지: 1점
- 안전한 등록 해제: 1.5점
- 알림 중 구독 변경의 snapshot 동작: 1.5점
