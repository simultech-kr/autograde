# COME2201 · Week12 · Problem08
## 실습 계획 편집기 — Memento, Visitor

마감: **2026-11-18 13:50 (KST)**

## 문제 상황과 학습 목표

조교는 텍스트 설명과 배점 항목을 순서대로 배치해 실습 계획을 만듭니다. 초안을 저장한 뒤 항목을 추가했다가 이전 상태로 돌아갈 수 있어야 합니다. 저장 상태가 현재 문서와 같은 객체를 공유한다면, 나중에 문서를 수정했을 때 과거 상태도 달라져 복구가 무의미해질 수 있습니다.

문서에는 종류가 다른 블록이 섞여 있습니다. 통계 집계와 화면 출력을 각 블록에 계속 추가하기보다는, 연산을 별도 Visitor로 분리하려고 합니다. 목표는 Memento로 **복구 대상과 내부 표현을 캡슐화**하고, Visitor의 **double dispatch로 구체 블록에 맞는 연산을 선택**하는 것입니다.

`main.cpp`의 Visitor double dispatch, 문서 순회, 독립적인 상태 저장·복구 TODO를 완성하세요. `Document`가 Originator, `Document::Snapshot`이 Memento, main의 스냅샷 맵이 Caretaker입니다. Caretaker는 스냅샷 내부 데이터에 접근하지 않습니다. **main.cpp 한 파일**에 제출합니다.

## 패턴별 책임과 구현 순서

| 구성 요소 | 맡아야 할 책임 |
|---|---|
| `TextBlock`, `TaskBlock` | 각자의 값을 보관하고 `accept()`로 자신에 맞는 방문 연산 연결 |
| `StatsVisitor` | 텍스트 수·문자 수·배점 항목 수·배점 합을 한 번의 순회에서 집계 |
| `PrintVisitor` | 블록 순서대로 문자열을 만들고 구분자·빈 문서 형식 처리 |
| `Document` | 블록 순서를 소유하며 방문·저장·복구 수행 |
| `Document::Snapshot` | 저장 당시 블록 상태를 외부 수정으로부터 보호 |
| `main`의 스냅샷 맵 | 이름의 중복·존재 여부와 스냅샷 수명만 관리 |

1. `TextBlock`과 `TaskBlock`의 `accept()`가 구체 타입에 맞는 Visitor 연산에 도달하도록 연결합니다. 호출 과정에서 정적 타입과 동적 타입이 각각 어떤 역할을 하는지 설명해 보세요.
2. 문서가 각 블록을 저장 순서대로 방문하게 하고, 출력 Visitor부터 확인합니다. 블록 사이에만 `|`를 넣습니다.
3. 통계 Visitor의 종류별 누적 대상을 정리합니다. 조회마다 새로운 Visitor를 사용하는 제공 흐름을 유지해 이전 조회 결과가 누적되지 않게 합니다.
4. 저장할 때 모든 블록의 현재 값과 순서가 독립적으로 남는지 확인합니다. 제공된 `clone()` 인터페이스의 용도를 살펴보세요.
5. 복구는 현재 문서 전체를 저장 당시 상태로 바꾸어야 합니다. 스냅샷의 블록을 꺼내 써서 저장 상태를 비우거나 현재 문서와 공유하지 않도록 합니다.
6. 같은 스냅샷을 두 번 복구하고, 그 사이 현재 문서를 수정해도 두 번째 복구 결과가 처음과 같은지 확인합니다.

## 입력과 출력

N(1~100) 다음 N줄의 명령. 텍스트와 이름은 공백 없는 ASCII 영문·숫자·밑줄(1~30자)입니다. 명령 문법은 정상이며 점수 입력은 -1000~1000입니다.

| 명령 | 규칙과 출력 |
|---|---|
| TEXT word | 텍스트 블록을 뒤에 추가, OK |
| TASK points | 0~100의 배점 블록을 뒤에 추가, OK. 범위 밖은 ERROR points |
| SAVE name | 현재 상태의 독립 스냅샷 저장, OK. 같은 이름이면 ERROR duplicate |
| RESTORE name | 저장 상태로 문서 전체 교체, OK. 이름이 없으면 ERROR missing |
| PRINT | 순서대로 TEXT:word 또는 TASK:points를 \|로 연결. 빈 문서는 EMPTY |
| STATS | TEXTS n CHARS c TASKS m POINTS p |

CHARS는 텍스트 블록의 ASCII 문자 수 합, POINTS는 배점 블록의 값 합입니다. SAVE/RESTORE 시 다른 스냅샷 목록을 변경하지 않습니다. 복구 후 문서에 추가해도 저장 상태는 유지되어야 합니다. 실패 명령은 상태를 바꾸지 않으며 모든 명령은 정확히 한 줄을 출력합니다.

0점과 100점은 유효합니다. `TASK 0`도 배점 블록 한 개이므로 TASKS는 증가합니다. 0~100 제한은 **블록 하나의 값**에 적용되며 POINTS 합계를 100으로 제한하지 않습니다. 숫자·밑줄도 텍스트의 문자 수에 포함하지만 `TEXT:` 같은 출력용 접두어나 블록 구분자는 CHARS에 포함하지 않습니다.

빈 문서는 `PRINT`에 `EMPTY`, `STATS`에 `TEXTS 0 CHARS 0 TASKS 0 POINTS 0`을 출력합니다. 빈 문서 저장도 가능하며 `RESTORE`는 덧붙이기가 아닌 전체 교체입니다. 중복 이름의 `SAVE`는 기존 스냅샷을 덮어쓰지 않고, 없는 이름의 `RESTORE`는 현재 문서를 비우지 않습니다. `PRINT`에는 구분자 앞뒤 공백이나 맨 끝 `|`를 붙이지 않습니다.

## 공개 예제 1 — 저장한 시점의 문서로 돌아가기

입력:
```text
10
TEXT Plan
TASK 30
SAVE first
TEXT Done
TASK 20
STATS
RESTORE first
PRINT
STATS
RESTORE absent
```
출력:
```text
OK
OK
OK
OK
OK
TEXTS 2 CHARS 8 TASKS 2 POINTS 50
OK
TEXT:Plan|TASK:30
TEXTS 1 CHARS 4 TASKS 1 POINTS 30
ERROR missing
```

복구 전에는 `Plan`과 `Done`의 문자 수가 합계 8이고 배점 합계가 50입니다. `first`를 복구하면 저장 이후 추가된 두 블록은 현재 문서에서 사라집니다.

## 공개 예제 2 — 빈 상태, 중복 저장, 반복 복구

입력:

```text
14
SAVE blank
TASK 45
TEXT Check2
SAVE draft
TASK -7
RESTORE blank
PRINT
RESTORE draft
TEXT Fix
STATS
SAVE draft
RESTORE draft
PRINT
STATS
```

출력:

```text
OK
OK
OK
OK
ERROR points
OK
EMPTY
OK
OK
TEXTS 2 CHARS 9 TASKS 1 POINTS 45
ERROR duplicate
OK
TASK:45|TEXT:Check2
TEXTS 1 CHARS 6 TASKS 1 POINTS 45
```

`Check2`는 6자, `Fix`는 3자입니다. 중복 저장이 실패하므로 `draft`에는 `Fix`가 포함되지 않습니다. `blank`로 복구한 뒤에도 `draft`라는 저장 항목 자체는 남아 있습니다.

## 빌드와 실행

**Visual Studio 2022/2026:** C++ 데스크톱 개발·CMake 도구를 사용합니다. **파일 → 열기 → 폴더**로 `main.cpp`와 `CMakeLists.txt`가 함께 있는 폴더를 열고, CMake 구성이 끝나면 `lab` 대상을 빌드·실행합니다. 콘솔에 예제 입력 전체를 붙여 넣거나 같은 폴더의 **Developer Command Prompt**에서 아래처럼 실행하세요. `<`는 일반 명령 프롬프트의 입력 연결 문법입니다.

```bat
cl /nologo /std:c++17 /EHsc /utf-8 main.cpp /Fe:lab.exe
lab.exe < sample.in
```

**Linux/macOS/WSL2:** C++17 컴파일러가 있는 터미널에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra main.cpp -o lab
./lab < sample.in
```

`sample.in`에는 한 예제의 N과 명령들만 저장합니다. 출력 블록을 입력에 섞지 마세요. 예제마다 프로그램을 새로 실행하면 빈 문서와 빈 스냅샷 목록에서 시작합니다. 시작 코드는 컴파일되지만 TODO를 완성하기 전에는 방문·저장·복구 결과가 올바르지 않습니다.

## 제출과 평가

필요한 클래스를 모두 포함한 **main.cpp 한 파일**이 답안 구현물입니다. 제공된 README와 CMake 설정은 보조 자료로 두고, 추가 답안 소스·실행 파일·빌드 폴더를 넣지 않습니다. 모두 저장한 뒤 확장에서 선택 과제와 제출 파일 목록을 확인하세요. 실행 중 설명 문구·객체 주소·디버그 로그를 표준 출력에 추가하지 마세요.

기능 자동채점은 총 **100점**이며 저장·복구, 방문 결과, 경계와 오류 후 상태 등 공개된 계약을 검사합니다. 공개 예제 이외의 명령 조합도 평가됩니다. **자동채점 100점과 Memento·Visitor 구조 검토는 별개**입니다. 캡슐화, 독립 복제, double dispatch, 연산별 Visitor 분리와 변경 범위에 대한 설명은 교수자가 별도로 확인합니다.

## 자기점검

- [ ] Snapshot 내부 표현이 Caretaker에 노출되지 않는다.
- [ ] 저장과 복구 모두 독립 복제를 수행하여 반복 복구가 안정적이다.
- [ ] 각 Block의 accept가 해당 구체 타입의 visit을 호출한다.
- [ ] 통계와 출력 Visitor가 분리되어 있고 동작마다 새 Visitor로 집계한다.
- [ ] 타입명 문자열 비교나 dynamic_cast 연쇄로 Visitor를 대체하지 않는다.
- [ ] 새로운 연산과 새로운 블록 타입을 추가할 때 변경 범위 차이를 설명한다.
- [ ] `STATS`를 연속 호출해도 집계가 누적되지 않고, 문서 순서가 PRINT에 그대로 나타난다.
- [ ] 실패한 저장·복구·배점 추가가 현재 문서나 기존 스냅샷을 변경하지 않는다.

## 자주 묻는 질문

**RESTORE하면 나중에 만든 다른 스냅샷 이름도 사라지나요?** 아니요. 복구 대상은 문서의 블록 목록이며 스냅샷 이름 목록은 별도로 유지됩니다.

**저장한 블록의 포인터를 현재 문서로 옮기면 되나요?** 저장 상태를 소모하면 같은 스냅샷의 반복 복구가 불가능해집니다. 저장 상태는 유지하고 현재 문서가 독립적인 블록을 가져야 합니다.

**공백이나 한글의 길이도 계산해야 하나요?** 이 과제의 입력 텍스트는 공백 없는 ASCII 토큰입니다. 이 계약 안에서 문자 수를 계산하며 임의로 다국어 입력 규칙을 추가하지 않습니다.

**모든 블록이 공통 인터페이스인데 종류를 어떻게 구분하나요?** 각 구체 블록의 `accept()`와 Visitor의 오버로드가 협력하는 것이 학습 목표입니다. 종류 문자열이나 강제 형변환 분기로 대체하지 마세요.
