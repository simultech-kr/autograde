# COME2201 · Week13 · Problem09
## 카페 주문 조합기 — Factory, Decorator, Composite, Facade
마감: **2026-11-25 13:50 (KST)**

학과 행사 카페에서 기본 음료, 추가 옵션, 세트 묶음을 조합합니다. 기존 상품을 수정하지 않고 옵션 상품을 만들고 세트 안에 다른 세트를 넣을 수 있어야 합니다. 화면 코드는 가격 계산과 객체 생성의 내부 구조를 몰라도 주문할 수 있어야 합니다.

필수 패턴은 **Factory, Decorator, Composite, Facade**입니다. 제공된 `OrderFacade`와 명령 처리의 계약을 유지하면서 `DrinkFactory`, `Drink`, `ExtraDecorator`, `DrinkPack`의 TODO를 완성하세요. 단일 **main.cpp**에 구현합니다.

## 입력·출력 계약
N(1~100) 다음 N줄 명령. id와 종류는 공백 없는 영문·숫자·밑줄(1~30자). 등록한 상품은 불변이며 이미 존재하는 상품만 참조하므로 순환 참조는 없습니다. 유효한 구조의 중첩 깊이는 10 이하이며 가격/수량 합산은 64비트 정수를 사용합니다. 명령 문법은 정상입니다.

| 명령 | 규칙 |
|---|---|
| DRINK id tea/coffee | tea=3000원, coffee=4000원. OK 또는 ERROR kind |
| EXTRA id source milk/shot | source를 감싼 새 상품. milk=500원, shot=1000원 추가. OK 또는 ERROR missing / ERROR extra |
| PACK id k member1 ... memberK | 1~10개 구성원을 묶음. 가격과 음료 수를 재귀 합산. 같은 구성원을 여러 번 넣어도 됨 |
| QUOTE id | PRICE 금액 ITEMS 음료수 |
| ORDER id count | 1~100개 주문. TOTAL (상품가격 × count) |

생성 명령은 공통으로 **새 id 중복을 가장 먼저 검사**하며 ERROR duplicate를 출력합니다. EXTRA는 원본 존재 확인 후 옵션 종류를 검사합니다. PACK은 k 범위(ERROR count)를 확인한 뒤 모든 구성원의 존재(ERROR missing)를 검사합니다. QUOTE/ORDER의 없는 id는 ERROR missing이며 ORDER는 그 뒤 수량 범위를 검사합니다. 실패한 생성은 id를 예약하거나 기존 상품을 바꾸지 않습니다.

옵션은 음료 수를 늘리지 않습니다. **세트에 옵션을 적용하면 세트 전체에 추가금 한 번**만 붙습니다. PACK의 k가 양수이면 오류 범위여도 해당 k개 토큰이 입력에 제공됩니다. 음수 또는 0이면 구성원 토큰은 없습니다. 각 명령의 성공 생성 출력은 OK이며, 모든 명령은 한 줄만 출력합니다.

## 공개 예제
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

## 실행·제출·평가
VS2022/2026의 폴더 열기/CMake 빌드 또는 Linux/macOS/WSL2의 `c++ -std=c++17 main.cpp -o lab`를 사용합니다. 시작 코드는 컴파일되지만 TODO 동작은 미완성입니다. 기능 100점은 중첩 구조, 중복 구성원, 옵션 누적, 오류와 개수 경계를 검사합니다. 다음 패턴 구조는 교수자가 별도로 코드 검토합니다.

- [ ] Factory가 기본 음료 생성을 캡슐화한다.
- [ ] Decorator가 공통 MenuItem을 감싸고 동작을 위임한다.
- [ ] Composite가 leaf/decorator/composite를 같은 인터페이스로 보관한다.
- [ ] Facade가 생성·조회·주문의 단순한 진입점을 제공한다.
- [ ] 기존 상품은 불변이며 추가 옵션이 원본 가격에 영향을 주지 않는다.
- [ ] 패턴 간 객체 관계와 새 음료/옵션을 추가하는 변경 지점을 설명한다.
