# COME2201 · Week13 · Problem09
## 카페 주문 조합기 — Factory, Decorator, Composite, Facade

마감: **2026-11-25 13:50 (KST)**

## 문제 상황과 학습 목표

학과 행사 카페는 기본 음료에 우유나 샷을 추가하고 여러 음료를 행사 세트로 판매합니다. 이미 판매 중인 상품을 바꾸지 않으면서 새 옵션 상품을 만들고, 세트 안에 다른 세트를 넣을 수 있어야 합니다. 각 조합마다 별도 상품 클래스를 만들면 종류가 빠르게 늘어나고, 명령 처리부가 모든 조합을 직접 계산하면 새 상품을 추가할 때 수정 범위가 커집니다.

이 과제는 생성 책임을 Factory에, 옵션 추가를 Decorator에, 전체와 부분의 동일한 취급을 Composite에, 외부 명령의 단순한 진입점을 Facade에 맡기는 연습입니다. **공통 인터페이스로 조합되는 객체 그래프**와 **변경하지 않고 재사용하는 상품**을 설계하고, 가격과 실제 음료 수가 서로 다른 집계임을 이해해야 합니다.

필수 패턴은 **Factory, Decorator, Composite, Facade**입니다. 제공된 `OrderFacade`와 명령 처리의 계약을 유지하면서 `DrinkFactory`, `Drink`, `ExtraDecorator`, `DrinkPack`의 TODO를 완성하세요. 단일 **main.cpp**에 구현합니다.

## 패턴별 책임과 구현 순서

| 구성 요소 | 맡아야 할 책임 |
|---|---|
| `MenuItem` | 모든 상품이 제공하는 `price()`·`units()` 공통 인터페이스 |
| `Drink` | 기본 음료 한 잔인 leaf의 가격과 음료 수 제공 |
| `DrinkFactory` | 음료 종류에 맞는 leaf 생성, 지원하지 않는 종류의 생성 거절 |
| `ExtraDecorator` | 다른 MenuItem 하나를 감싸 옵션 가격을 더하고 음료 수는 유지 |
| `DrinkPack` | 여러 MenuItem을 자식으로 보관하고 자식의 가격·음료 수 합산 |
| `OrderFacade` | id 등록, 오류 검사, 상품 생성·조회·주문을 단순한 API로 제공 |

1. 기본 음료의 가격과 음료 수를 구현하고 Factory의 두 지원 종류와 실패 결과를 확인합니다.
2. Decorator가 감싼 상품의 인터페이스를 이용하도록 합니다. 음료인지 세트인지 구체 타입을 판별해 별도 계산할 필요가 없습니다.
3. Composite가 자식에게 같은 인터페이스로 질의하도록 합니다. 중첩 세트의 내부 계산은 해당 자식에게 맡깁니다.
4. 합산 중간값과 반환값을 64비트 정수로 유지합니다. 작은 예제에서만 맞는 32비트 계산으로 제한하지 마세요.
5. 제공된 Facade의 검사 순서와 성공 후 등록 시점을 확인합니다. 객체 생성 실패가 메뉴에 이름만 남기지 않아야 합니다.
6. 기본 상품, 옵션 상품, 세트, 옵션을 붙인 세트 순서로 검증하고 원본을 다시 조회해 불변성을 확인합니다.

## 입력·출력 계약

N(1~100) 다음 N줄 명령. id와 종류는 공백 없는 영문·숫자·밑줄(1~30자). 등록한 상품은 불변이며 이미 존재하는 상품만 참조하므로 순환 참조는 없습니다. 유효한 구조의 중첩 깊이는 10 이하이며 가격/수량 합산은 64비트 정수를 사용합니다. 명령 문법은 정상입니다.

| 명령 | 규칙 |
|---|---|
| DRINK id tea/coffee | tea=3000원, coffee=4000원. OK 또는 ERROR kind |
| EXTRA id source milk/shot | source를 감싼 새 상품. milk=500원, shot=1000원 추가. OK 또는 ERROR missing / ERROR extra |
| PACK id k member1 ... memberK | 1~10개 구성원을 묶음. 가격과 음료 수를 재귀 합산. 같은 구성원을 여러 번 넣어도 됨. 성공 OK, 범위 밖 ERROR count, 없는 구성원 ERROR missing |
| QUOTE id | PRICE 금액 ITEMS 음료수 |
| ORDER id count | 1~100개 주문. TOTAL (상품가격 × count). 없는 id는 ERROR missing, 범위 밖은 ERROR count |

생성 명령은 공통으로 **새 id 중복을 가장 먼저 검사**하며 ERROR duplicate를 출력합니다. EXTRA는 원본 존재 확인 후 옵션 종류를 검사합니다. PACK은 k 범위(ERROR count)를 확인한 뒤 모든 구성원의 존재(ERROR missing)를 검사합니다. QUOTE/ORDER의 없는 id는 ERROR missing이며 ORDER는 그 뒤 수량 범위를 검사합니다. 실패한 생성은 id를 예약하거나 기존 상품을 바꾸지 않습니다.

옵션은 음료 수를 늘리지 않습니다. **세트에 옵션을 적용하면 세트 전체에 추가금 한 번**만 붙습니다. PACK의 k가 양수이면 오류 범위여도 해당 k개 토큰이 입력에 제공됩니다. 음수 또는 0이면 구성원 토큰은 없습니다. 각 명령의 성공 생성 출력은 OK이며, 모든 명령은 한 줄만 출력합니다.

`PACK`의 1과 10, `ORDER`의 1과 100은 유효합니다. ITEMS는 서로 다른 id 수가 아니라 **구성된 음료의 총 잔 수**입니다. 같은 상품을 두 번 포함하면 가격과 잔 수도 두 번 합산합니다. `ORDER`는 주문 금액만 계산하며 메뉴에서 상품을 제거하거나 재고를 차감하지 않습니다. 생성된 옵션 상품을 다시 감싸 옵션을 누적할 수 있지만 원본 가격은 바뀌지 않습니다.

가격은 원 단위 정수이며 소수점·통화 기호·천 단위 쉼표 없이 출력합니다. 출력의 `PRICE`, `ITEMS`, `TOTAL`, `ERROR`는 대문자로 유지하고 항목 사이만 한 칸씩 띄웁니다. 명령을 실패 처리하더라도 해당 명령의 입력 토큰은 모두 읽어 다음 명령이 어긋나지 않아야 합니다. 제공된 해석 흐름을 유지하세요.

## 공개 예제 1 — 음료와 옵션을 세트로 주문

입력:
```text
8
DRINK tea tea
DRINK coffee coffee
EXTRA latte coffee milk
PACK pair 2 tea latte
QUOTE pair
ORDER pair 3
QUOTE coffee
QUOTE latte
```
출력:
```text
OK
OK
OK
OK
PRICE 7500 ITEMS 2
TOTAL 22500
PRICE 4000 ITEMS 1
PRICE 4500 ITEMS 1
```

`latte`는 coffee 4000원에 milk 500원을 더한 4500원입니다. pair는 tea 3000원과 latte 4500원이므로 7500원·2잔이고, 세 세트의 금액은 22500원입니다. 원래 coffee는 여전히 4000원입니다.

## 공개 예제 2 — 세트 전체 옵션과 실패 후 재사용

입력:

```text
14
DRINK herb tea
DRINK roast coffee
PACK team 3 herb roast herb
EXTRA team_milk team milk
PACK meeting 2 team_milk roast
QUOTE team
QUOTE meeting
ORDER meeting 2
EXTRA retry missing milk
EXTRA retry herb honey
EXTRA retry herb shot
QUOTE retry
ORDER retry 0
QUOTE herb
```

출력:

```text
OK
OK
OK
OK
OK
PRICE 10000 ITEMS 3
PRICE 14500 ITEMS 4
TOTAL 29000
ERROR missing
ERROR extra
OK
PRICE 4000 ITEMS 1
ERROR count
PRICE 3000 ITEMS 1
```

team의 가격은 3000+4000+3000=10000원입니다. team_milk는 세트 전체에 500원만 더하므로 10500원·3잔입니다. 여기에 roast를 묶은 meeting은 14500원·4잔입니다. 실패한 두 번의 EXTRA는 `retry`를 예약하지 않으므로 세 번째 생성이 성공합니다.

## 빌드와 실행

**Visual Studio 2022/2026:** C++ 데스크톱 개발·CMake 도구가 있는 환경에서 **파일 → 열기 → 폴더**로 `main.cpp`와 `CMakeLists.txt`가 함께 있는 폴더를 엽니다. CMake 구성 후 `lab` 대상을 빌드·실행하고 예제 입력을 콘솔에 전달합니다. 같은 폴더의 **Developer Command Prompt**에서 다음과 같이 파일 입력으로 실행할 수도 있습니다. `<`는 일반 명령 프롬프트 문법입니다.

```bat
cl /nologo /std:c++17 /EHsc /utf-8 main.cpp /Fe:lab.exe
lab.exe < sample.in
```

**Linux/macOS/WSL2:** C++17 컴파일러가 설치된 터미널에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra main.cpp -o lab
./lab < sample.in
```

`sample.in`에는 예제 하나의 입력 블록만 저장합니다. 첫 줄의 명령 수까지 포함하고 출력 블록은 제외하세요. 예제마다 새로 실행하여 빈 메뉴에서 시작합니다. 시작 코드는 컴파일되지만 Factory의 빈 결과나 TODO 계산 때문에 아직 올바른 동작을 하지 않습니다.

## 제출과 평가

필요한 클래스와 함수를 모두 포함한 **main.cpp 한 파일**이 답안 구현물입니다. 제공된 README와 CMake 설정은 보조 자료로 유지하고 별도 답안 `.cpp`나 실행 파일·빌드 폴더를 넣지 않습니다. 모두 저장한 뒤 확장의 제출 확인창에서 과제명·파일 목록을 확인하세요. 재고 관리, 할인, 세금, 소수 가격 등 명세에 없는 기능을 출력에 추가하지 않습니다.

기능 자동채점은 총 **100점**이며 명령 계약, 중첩 구조, 가격·잔 수, 오류와 상태 보존을 확인합니다. 공개 예제 이외에도 공개 명세에 맞는 입력이 평가됩니다. **기능 100점은 네 패턴의 구조 평가를 대신하지 않습니다.** Factory의 생성 책임, Decorator의 위임, Composite의 동일 인터페이스, Facade의 경계와 불변성은 교수자가 별도로 코드와 설명을 검토합니다.

## 자기점검

- [ ] Factory가 기본 음료 생성을 캡슐화한다.
- [ ] Decorator가 공통 MenuItem을 감싸고 동작을 위임한다.
- [ ] Composite가 leaf/decorator/composite를 같은 인터페이스로 보관한다.
- [ ] Facade가 생성·조회·주문의 단순한 진입점을 제공한다.
- [ ] 기존 상품은 불변이며 추가 옵션이 원본 가격에 영향을 주지 않는다.
- [ ] 패턴 간 객체 관계와 새 음료/옵션을 추가하는 변경 지점을 설명한다.
- [ ] 같은 구성원의 반복 참조도 가격·음료 수에 각각 반영한다.
- [ ] 옵션을 붙여도 음료 수는 같고, 세트 옵션의 추가금은 한 번만 더한다.
- [ ] 합산·곱셈 과정에 32비트 중간 계산이 끼어들지 않는지 확인한다.

## 자주 묻는 질문

**우유를 세트에 추가하면 각 잔에 500원씩 붙나요?** 이 과제에서는 세트 전체에 500원을 한 번만 추가합니다. 실제 카페의 다른 정책을 추측해서 적용하지 마세요.

**같은 id가 세트에 두 번 나오면 중복 오류인가요?** 아닙니다. 생성할 새 id의 중복과 세트 구성원의 반복은 다릅니다. 구성원 반복은 허용합니다.

**옵션도 상품인데 ITEMS를 하나 늘려야 하나요?** ITEMS는 객체 수가 아니라 음료 잔 수입니다. Decorator 객체를 추가해도 감싼 상품의 잔 수를 유지합니다.

**ORDER 후 다시 QUOTE하면 값이 달라지나요?** 아닙니다. ORDER는 금액 조회 성격의 연산입니다. 등록된 상품은 불변이고 수량을 소진하지 않습니다.
