# 실습 2. 교내 카페 주문 조합 — Decorator

## 문제 상황

교내 카페 주문기에 아메리카노와 카페라테가 있습니다. 학생들이 우유, 샷, 휘핑크림을 자유롭게
추가할 수 있게 해 달라고 요청했습니다. `AmericanoWithMilkAndShot`처럼 조합마다 클래스를
만들면 옵션이 늘수록 클래스가 폭증합니다. 기본 음료 클래스에 모든 옵션 조건문을 넣어도
새 옵션을 추가할 때마다 기존 코드를 수정해야 합니다.

**음료를 다른 객체로 감싸서 기능을 누적**하는 데코레이터 패턴으로 해결하세요.
주문기는 어떤 조합이든 `Beverage` 하나로 취급할 수 있어야 합니다.

## 제공 코드와 작성 위치

`pattern.hpp`의 TODO 10개를 완성하세요. 제공 인터페이스와 소유권 구조를 유지합니다.

- `Beverage`: `description()`, `cost()` 공통 계약.
- `Americano`, `CafeLatte`: 기본 음료.
- `BeverageDecorator`: 다른 `Beverage`를 `unique_ptr`로 소유하는 추상 장식자.
- `Milk`, `Shot`, `WhippedCream`: 위임한 결과에 옵션 설명·가격을 더하는 장식자.

`main.cpp`와 `check.cpp`는 수정하지 않습니다. `pattern.hpp`의 미완성 메서드는 TODO 예외를 냅니다.

## 가격과 동작 계약

| 객체 | 가격 또는 추가 가격 | 설명 |
| --- | ---: | --- |
| Americano | 2,000원 | `Americano` |
| CafeLatte | 3,000원 | `Cafe Latte` |
| Milk | +500원 | 안쪽 설명 뒤에 ` + Milk` |
| Shot | +700원 | 안쪽 설명 뒤에 ` + Shot` |
| WhippedCream | +600원 | 안쪽 설명 뒤에 ` + Whipped Cream` |

1. 금액은 원 단위 정수입니다. 할인·세금·소수 금액은 다루지 않습니다.
2. 같은 옵션을 여러 번 감쌀 수 있습니다. 샷 두 번이면 1,400원이 추가됩니다.
3. 설명은 안쪽부터 바깥쪽으로 연결합니다. 감싸는 순서가 다르면 설명 순서는 달라도 가격은 같아야 합니다.
4. 옵션은 안쪽 객체의 `cost()`와 `description()`에 **위임**해야 합니다. 기본 음료 가격을
   옵션에 하드코딩하거나 `dynamic_cast`, 타입별 `if/switch`로 분기하지 마세요.
5. 새 기본 음료와 새 옵션은 기존 음료 클래스를 고치지 않고 추가할 수 있어야 합니다.
6. `nullptr`로 옵션을 만들면 `std::invalid_argument`가 발생합니다. 이 검사는 제공된 생성자에 있습니다.
7. 가상 소멸자와 `unique_ptr` 소유권을 유지합니다. 학생 코드에서 수동 `delete`는 필요하지 않습니다.

동일 옵션의 중복 금지, 옵션 제거, 사용자 문자열 주문 해석은 이번 실습 범위가 아닙니다.

## 실행 확인

Linux / macOS / WSL2에서 starter 폴더를 열고:

```bash
c++ -std=c++17 -Wall -Wextra main.cpp -o decorator-demo
./decorator-demo
c++ -std=c++17 -Wall -Wextra check.cpp -o decorator-check
./decorator-check
```

macOS에서 `memory` 같은 표준 헤더를 찾지 못하면 컴파일 환경 문제부터 확인하세요.
`xcrun --show-sdk-path`가 정상이고 해당 SDK에 `usr/include/c++/v1`이 있다면:

```bash
c++ -std=c++17 -Wall -Wextra -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1" main.cpp -o decorator-demo
./decorator-demo
c++ -std=c++17 -Wall -Wextra -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1" check.cpp -o decorator-check
./decorator-check
```

SDK 조회도 실패하면 개발 도구 환경을 먼저 정비해야 합니다. 표준 헤더 include를 지우지 마세요.

정상 시연 출력:

```text
Americano + Milk + Shot = 3200 KRW
Cafe Latte + Shot + Shot + Whipped Cream = 5000 KRW
```

검사를 모두 통과하면 `5/5 checks passed`, 종료 코드 0입니다. FAIL이 하나라도 있으면 종료 코드 1입니다.

VS2022/2026의 **Developer Command Prompt**에서는:

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:decorator-demo.exe
decorator-demo.exe
cl /nologo /std:c++17 /EHsc /W4 /utf-8 check.cpp /Fe:decorator-check.exe
decorator-check.exe
```

Visual Studio에서 폴더를 열어 `CMakeLists.txt`의 `decorator_demo`, `decorator_check`를
각각 실행해도 됩니다. main 함수가 있는 두 cpp를 한 실행 파일로 합치지 마세요.

## 제출과 프롬프트 확인

1. TODO를 채우고 시연·공개 검사를 실행합니다.
2. [검토 프롬프트](REVIEW_PROMPT.md)에 구현 전체와 실제 검사 결과를 붙입니다.
3. AI가 제시한 반례를 본인이 실행하고 필요한 부분을 수정합니다.
4. 최종 코드, 검사 결과, 아래 질문의 답을 제출합니다.

- 이 구조에서 상속과 객체 합성은 각각 어디에 사용되나요?
- Shot이 Americano의 구체 타입을 몰라도 되는 이유는 무엇인가요?
- `Milk(Shot(Americano))`와 `Shot(Milk(Americano))`에서 같고 다른 것은 무엇인가요?
- 새 `VanillaSyrup` 옵션을 추가할 때 변경해야 하는 코드는 어디인가요?
