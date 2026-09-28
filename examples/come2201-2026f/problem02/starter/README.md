# Week05 · Problem02: PC 조립 견적 서비스

- 교과목: COME2201
- 마감: **2026-09-30 13:50 (Asia/Seoul, KST)**
- 언어: C++17 / 채점 대상: main.cpp
- 설계 패턴: Factory Method, Composite, Facade

## 문제 상황과 학습 목표

PC 판매점은 CPU·메모리·디스크를 낱개로 팔기도 하고, 여러 부품을 패키지로 묶기도 합니다.
패키지에는 다른 패키지도 들어갑니다. 어떤 항목의 가격을 조회하든 같은 인터페이스를 사용하고,
명령 처리부가 생성 방법이나 내부 트리를 직접 다루지 않도록 견적 서비스를 만드세요.
실제 주문·결제가 아니라 메모리 안의 구성 트리를 조작하는 콘솔 프로그램입니다.

- Factory Method: 부품 생성을 구체 Creator 객체의 책임으로 분리합니다.
- Composite: 단일 부품과 그룹을 같은 Component::price()로 다룹니다.
- Facade: 명령 처리부가 QuoteFacade의 생성·연결·조회 기능만 사용하게 합니다.
- 순환·복수 부모를 막고 실패한 명령이 데이터를 바꾸지 않도록 설계합니다.

## 입력·출력 계약

첫 줄은 명령 수 N(1..100), 다음 N줄은 아래 명령입니다. 명령 이름과 토큰 수는 올바릅니다.
id는 영문 소문자·숫자·밑줄로 된 1..20자 문자열입니다. 종류 이름은 대문자이며 대소문자를 구분합니다.
부품과 그룹은 **같은 id 공간**을 사용합니다.

| 명령 | 의미 | 성공 출력 |
| --- | --- | --- |
| `PART id type` | 종류에 맞는 단일 부품 생성 | `CREATED id` |
| `GROUP id` | 자식이 없는 그룹 생성 | `CREATED id` |
| `LINK parent child` | child를 GROUP parent의 자식으로 연결 | `LINKED parent child` |
| `TOTAL id` | 그 항목에 포함된 전체 가격 조회 | `TOTAL 가격` |

| 부품 종류 | 가격 |
| --- | --- |
| CPU | 100 |
| RAM | 40 |
| DISK | 60 |

가격은 정수이며 통화 기호나 소수점을 출력하지 않습니다. 빈 그룹은 0입니다. 중첩 그룹은 모든 하위
부품 가격을 합산합니다. 부모에 연결된 뒤에도 부품·하위 그룹을 id로 직접 조회할 수 있습니다.
각 항목의 부모는 최대 하나입니다. 복사, 수량, 삭제, 연결 해제는 다루지 않습니다.

### 오류 검사 순서와 상태 보존

한 명령에서 여러 문제가 있어도 **아래 순서에서 첫 오류 한 줄만** 출력합니다.
오류가 나면 생성·연결을 수행하지 않고 다음 명령을 처리합니다.

| 명령 | 검사 순서 |
| --- | --- |
| PART | id 중복 → `ERROR duplicate`; 모르는 종류 → `ERROR type` |
| GROUP | id 중복 → `ERROR duplicate` |
| TOTAL | 없는 id → `ERROR missing` |
| LINK | parent 또는 child 없음 → `ERROR missing`; parent가 부품 → `ERROR parent`; 자기 연결 또는 순환 → `ERROR cycle`; child가 이미 연결됨 → `ERROR linked` |

같은 링크를 다시 요청해도 ERROR linked입니다. 자손 아래에 조상을 연결하는 것도 순환입니다.
잘못된 종류로 생성에 실패한 id는 등록되지 않습니다. 성공 출력과 오류 출력은 모두 명령당 한 줄입니다.

## 구현 단계

배포된 main.cpp의 인터페이스를 바탕으로 TODO와 필요한 보조 멤버를 구현하세요.

1. 단일 부품이 Component::price()를 통해 고정 가격을 돌려주도록 합니다.
2. CPUCreator, RAMCreator, DISKCreator가 각각 PartCreator::create()를 구현하게 합니다.
3. Group이 자식을 보관하고 공통 인터페이스를 통해 가격을 합산하게 합니다.
4. QuoteFacade에 id 등록소와 부모 관계 관리 책임을 둡니다. 연결 가능 여부를 모두 검사한 뒤 반영하세요.
5. 명령 처리부는 Facade를 호출하고, Facade가 생성·연결·조회와 오류를 일관되게 처리하게 합니다.
6. 실패한 연결 뒤에 TOTAL을 실행하여 기존 트리가 변하지 않았는지 확인합니다.

등록소만 있다고 Composite가 구현된 것은 아닙니다. 그룹의 합산이 실제 자식 객체에 위임되는지,
구체 Creator의 생성 메서드가 실제로 사용되는지 확인하세요.

## 공개 실행 예제

각 예제는 새 프로그램 실행으로 시작합니다.

### 예제 1 — 단일 그룹과 부품 직접 조회

입력
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

출력
```text
CREATED pc
CREATED cpu
CREATED ram
LINKED pc cpu
LINKED pc ram
TOTAL 140
TOTAL 100
```

pc의 합계는 100+40입니다. CPU가 그룹에 속해도 자신의 가격을 조회할 수 있습니다.

### 예제 2 — 중첩 패키지와 실패한 변경

입력
```text
13
GROUP set
TOTAL set
GROUP addon
PART store DISK
LINK addon store
LINK set addon
TOTAL set
LINK addon set
LINK set store
PART store GPU
PART video GPU
TOTAL addon
TOTAL video
```

출력
```text
CREATED set
TOTAL 0
CREATED addon
CREATED store
LINKED addon store
LINKED set addon
TOTAL 60
ERROR cycle
ERROR linked
ERROR duplicate
ERROR type
TOTAL 60
ERROR missing
```

set 안의 addon에 DISK 하나가 있으므로 두 그룹의 가격은 60입니다. 순환·복수 부모 요청은 모두 실패합니다.
이미 있는 store는 종류 오류보다 중복 오류가 먼저이며, video는 생성되지 않습니다.

## 빌드와 실행

Visual Studio 2022/2026 Community에서는 **C++를 사용한 데스크톱 개발** 도구를 준비하고,
CMakeLists.txt가 있는 이 폴더를 열어 lab 실행 대상을 빌드·실행합니다. 콘솔에는 예제 전체를 입력하세요.
예제를 input.txt에 저장했다면 Developer Command Prompt(CMD)에서 이 과제 폴더로 이동하여 실행할 수도 있습니다.

```bat
cl /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:lab.exe
lab.exe < input.txt
```

위 입력 전달은 CMD 기준이며 PowerShell 문법이 아닙니다. Linux/macOS/WSL에서는 C++17 컴파일러를 준비한 뒤 이 폴더에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o lab
./lab < input.txt
```

표준 입력만 읽고 표준 출력에는 규약의 결과만 쓰세요. 입력 안내문, 디버그 로그, 추가 메뉴,
종료 대기용 입력이나 system("pause")를 넣지 않습니다. 각 결과 줄 끝에는 줄바꿈을 출력합니다.

## 제출과 평가

채점 대상 구현은 과제 루트의 **main.cpp 하나에 완결**되어야 합니다. 다른 소스 파일·외부 라이브러리·
개인 PC의 절대 경로에 의존하지 마세요. CMakeLists.txt와 README는 로컬 안내용입니다.
확장은 과제 폴더를 묶어 전송하므로 개인 문서·비밀번호를 넣지 말고 .autograde 식별 정보를 유지하세요.
저장 → 해당 과제 선택 → 제출 → 새 접수번호 확인 → 현재 제출 결과 확인 순서로 진행합니다.

자동채점은 입력·출력, 경계값, 오류 처리의 **기능 100점**입니다. 예제 통과는 모든 경우의 통과를 뜻하지
않습니다. 패턴 사용 여부는 입출력만으로 증명할 수 없으므로 아래 구조 항목을 교수자가 별도 루브릭으로
검토합니다. 기능 만점이 구조 평가 완료나 최종 성적 확정을 의미하지는 않습니다.

- 구체 Creator의 factory method로 실제 부품을 생성하는가?
- 부품과 그룹이 같은 Component 인터페이스를 따르는가?
- 그룹이 자식에 재귀적으로 가격 계산을 위임하는가?
- Facade가 생성·연결·조회와 오류 검증을 맡고 main이 내부 구조에 의존하지 않는가?
- 순환·복수 부모를 차단하며 실패 시 기존 상태를 보존하는가?

## 자기 점검과 FAQ

- [ ] 빈 그룹, 독립 부품, 여러 단계 그룹의 가격을 손으로 계산해 비교했습니다.
- [ ] 부품과 그룹 사이의 id 중복도 확인했습니다.
- [ ] 자기 연결, 조상 연결, 이미 부모가 있는 자식 연결을 차단합니다.
- [ ] 오류 후 이어지는 명령이 실행되고 합계가 바뀌지 않습니다.

**같은 부품 id를 여러 패키지에서 공유해도 되나요?** 안 됩니다. 같은 종류가 여러 개 필요하면 서로 다른 id로 생성하세요.

**부품을 부모로 연결할 수 있나요?** 안 됩니다. 부모는 그룹이어야 합니다.

**Factory를 종류별 if 문으로만 구현해도 되나요?** Creator 선택 분기는 필요할 수 있지만 실제 생성은 Creator 메서드에
위임해야 합니다. 입출력 점수와 구조 평가는 구분됩니다.
