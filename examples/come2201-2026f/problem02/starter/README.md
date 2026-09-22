# Week05 · Problem02: PC 조립 견적 서비스
- 교과목: COME2201 / 마감: **2026-09-30 13:50 KST**
- C++17 / 제출: `main.cpp`
- 패턴: Factory Method, Composite, Facade

## 문제 상황
PC 부품 하나와 여러 부품을 묶는 패키지를 같은 방식으로 취급하는 견적 서비스를 만드세요. 패키지 안에는 다른 패키지도 들어갑니다. 사용자 명령 처리부는 부품 생성·구성 트리·검증을 직접 조작하지 않고 `QuoteFacade`만 사용해야 합니다.

`Component::price()`를 구현하는 Part/Group, `PartCreator::create()`를 구현하는 CPU/RAM/DISK 생성자, 외부 진입점 QuoteFacade를 완성하세요. Factory Method의 구체 생성자는 실제 Part 객체를 반환합니다.

## 규약
첫 줄 N(1..100), 다음 N줄에 명령 하나씩. id는 영문 소문자/숫자/밑줄 1..20자입니다. 문법과 토큰 수는 올바릅니다.

| 명령 | 의미 및 성공 출력 |
| --- | --- |
| `PART id CPU\|RAM\|DISK` | 부품 생성. 가격 CPU=100, RAM=40, DISK=60. `CREATED id` |
| `GROUP id` | 가격 0인 빈 패키지 생성. `CREATED id` |
| `LINK parent child` | child를 GROUP parent의 자식으로 연결. `LINKED parent child` |
| `TOTAL id` | 하위 부품 가격 합계. `TOTAL 가격` |

부품과 그룹의 id 공간은 같습니다. 각 항목은 부모를 최대 하나 가집니다. 연결 후에도 TOTAL로 모든 항목을 직접 조회할 수 있습니다. 항목 복사, 수량, 삭제는 다루지 않습니다.

오류는 상태를 바꾸지 않고 아래 한 줄을 출력합니다. 여러 원인이 있으면 기재 순서로 검사합니다.
- PART: id 중복 → `ERROR duplicate`; CPU/RAM/DISK 이외 종류 → `ERROR type`.
- GROUP: id 중복 → `ERROR duplicate`.
- TOTAL: 없는 id → `ERROR missing`.
- LINK: parent 또는 child 없음 → `ERROR missing`; parent가 부품 → `ERROR parent`; 자기 연결 또는 순환 → `ERROR cycle`; child에 이미 부모가 있음(동일 링크 재요청 포함) → `ERROR linked`.

### 예시
입력:
```text
7
GROUP pc
PART cpu CPU
PART ram RAM
LINK pc cpu
LINK pc ram
TOTAL pc
TOTAL cpu
```
출력:
```text
CREATED pc
CREATED cpu
CREATED ram
LINKED pc cpu
LINKED pc ram
TOTAL 140
TOTAL 100
```

## 작업과 평가
main.cpp의 TODO를 완성합니다. 출력 프롬프트/로그를 추가하지 마세요. VS에서는 폴더를 열어 CMake 프로젝트로, Linux/macOS/WSL에서는 `c++ -std=c++17 main.cpp -o lab`로 빌드합니다.

기능 자동채점은 100점이며 구조 루브릭은 별도 검토입니다.
- 구체 Creator의 factory method로 부품을 생성하는가?
- Leaf와 Group이 같은 Component 인터페이스를 따르고 Group이 자식에 재귀적으로 위임하는가?
- Facade가 생성/연결/합산 및 오류 검증을 일관되게 제공하는가?
- 실패한 연결이 트리를 바꾸지 않으며 순환과 복수 부모를 차단하는가?
