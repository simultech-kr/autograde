# COME2201 · Week14 · Problem10
## 실습 진행 보드 — Observer, Memento, Iterator, Facade

마감: **2026-12-02 13:50 (KST)**

## 문제 상황과 학습 목표

튜터는 실습 참여자의 점수를 보드에 기록하고 변경이 몇 번 발생했는지 확인합니다. 다른 튜터가 나중에 구독을 시작하거나 먼저 구독을 해지할 수도 있습니다. 동시에 평가 시점의 보드 상태를 이름 붙여 저장해 두고, 여러 수정 뒤 그 시점으로 되돌리는 기능이 필요합니다. 점수 기준에 맞는 학생만 보여 주되 화면 역할의 코드는 보드의 내부 저장 구조에 직접 접근하지 않아야 합니다.

이 과제는 네 가지 패턴을 함께 사용하면서 **각 패턴이 관리하는 상태의 범위**를 구별하는 연습입니다. Observer는 현재 구독자에게 변경을 알리고, Memento는 보드 항목과 순서를 보관합니다. Iterator는 필터링과 순회 위치를 관리하며 Facade는 명령 처리에 단순한 API를 제공합니다. 저장 시점으로 돌아가는 것이 프로그램 전체 상태를 되감는 것과 다르다는 점을 설명할 수 있어야 합니다.

필수 패턴은 **Observer, Memento, Iterator, Facade**입니다. `main.cpp`에 제공된 Board/BoardFacade 골격을 유지하고 알림, 구독, 필터 순회, 저장·복구 TODO를 완성하세요. **main.cpp 한 파일**에 제출합니다. 알림은 출력 메시지를 추가하지 않고 구독자별 카운터를 갱신합니다.

## 패턴별 책임과 구현 순서

| 구성 요소 | 맡아야 할 책임 |
|---|---|
| `BoardObserver` | 보드가 구체 구독자 종류를 몰라도 호출할 수 있는 알림 인터페이스 |
| `CounterObserver` | 받은 변경 알림 횟수를 보관하고 조회 가능하게 제공 |
| `Board` | 점수 항목·삽입 순서를 관리하고 성공한 변경 뒤 현재 구독자에게 알림 |
| `Board::Snapshot` | 보드 항목과 순서만 보관하는 Memento |
| `ScoreIterator` | 기준 점수 미만을 건너뛰며 순회 위치·종료 상태를 관리 |
| `BoardFacade` | 이름별 구독자·스냅샷 관리, 보드 연산 연결, 조회 문자열 구성 |

1. `CounterObserver`의 한 번의 알림과 카운터 변화부터 확인합니다. 알림 자체에서 표준 출력은 하지 않습니다.
2. Board의 구독·해지·알림 전달을 구현합니다. 해지한 객체를 보드가 계속 보관해 알림을 보내지 않도록 참조와 수명을 점검합니다.
3. Iterator의 시작 위치, 기준 미달 항목 건너뛰기, 다음 항목 이동, 종료 판정을 각각 확인합니다. 종료 상태에서는 `next()`를 호출하지 않는 제공 사용법을 유지합니다.
4. 저장과 복구의 대상 필드를 정리합니다. 보드 행은 독립 저장하지만 구독자 목록과 카운터는 Snapshot에 넣지 않습니다.
5. 복구가 보드 전체와 순서를 교체한 다음 현재 구독자에게 한 번 알리도록 연결합니다. 여러 행이 바뀌더라도 행마다 알리는 연산은 아닙니다.
6. Facade와 main의 제공 계약을 유지하며 아래 예제에서 카운터를 명령 한 줄씩 손으로 계산해 봅니다. 이벤트 횟수와 현재 보드 항목 수를 혼동하지 마세요.

## 입력과 출력

첫 줄 N(1~100), 다음 N줄 명령. id와 이름은 공백 없는 영문·숫자·밑줄(1~30자), 점수 입력은 -1000~1000입니다. LIST의 기준값은 0~100으로 제공됩니다. 학생 id, 구독자 이름, 스냅샷 이름은 서로 다른 이름 공간입니다.

| 명령 | 규칙 및 출력 |
|---|---|
| ADD id score | 0~100점 학생 추가, OK. 중복은 ERROR duplicate, 범위 밖은 ERROR score |
| SCORE id score | 점수 갱신, OK. 없는 id는 ERROR missing, 범위 밖은 ERROR score |
| REMOVE id | 삭제 후 OK. 없으면 ERROR missing |
| WATCH name | 변경 카운터 0으로 구독 시작, OK. 중복은 ERROR duplicate |
| UNWATCH name | 구독 및 카운터 제거, OK. 없으면 ERROR missing |
| SNAP name | 보드 항목·순서를 독립 저장, OK. 중복은 ERROR duplicate |
| RESTORE name | 보드 항목·순서 복구, OK. 없으면 ERROR missing |
| LIST minimum | 점수가 기준 이상인 항목을 삽입 순서대로 id:score, 공백으로 연결. 없으면 NONE |
| COUNTS | 현재 구독자를 이름 오름차순으로 name=count, 공백으로 연결. 없으면 NONE |

성공한 **ADD, SCORE, REMOVE, RESTORE**만 현재 구독자에게 한 번씩 알립니다. 같은 점수로 SCORE하거나 빈/같은 상태로 RESTORE해도 한 번 알립니다. 오류, WATCH, UNWATCH, SNAP, 조회는 알리지 않습니다. 재구독은 0부터 시작합니다.

Snapshot은 보드 항목만 보관합니다. **구독자와 카운터는 복구 대상이 아니며**, 복구 시 현재 구독자에게 알립니다. 기존 항목 점수 변경은 순서를 유지하고 삭제 후 재추가는 맨 뒤입니다. ADD는 중복을 점수 범위보다 먼저, SCORE는 존재를 범위보다 먼저 검사합니다. 실패 명령은 상태를 바꾸지 않습니다. 각 명령은 정확히 한 줄만 출력합니다.

점수 0과 100은 유효합니다. `LIST minimum`은 **기준과 같은 점수도 포함**하며 점수순·id순 정렬을 하지 않습니다. `COUNTS`의 이름 오름차순은 구독 등록 순서와 다릅니다. 두 조회 모두 항목 사이만 한 칸 띄우고 앞뒤에 공백을 붙이지 마세요.

세 이름 공간은 독립적이므로 학생 id와 구독자·스냅샷 이름이 같은 토큰이어도 서로 중복 오류를 일으키지 않습니다. `SNAP`의 같은 이름은 덮어쓰기 없이 오류이고, `RESTORE`가 다른 스냅샷 이름을 지우지도 않습니다. 빈 보드도 저장·복구할 수 있습니다. WATCH 이전에 일어난 변경은 새 구독자의 카운터에 소급하지 않습니다.

## 공개 예제 1 — 변경 알림과 필터 조회

입력:
```text
12
WATCH tutor
ADD a 20
ADD b 80
SNAP v1
SCORE a 90
LIST 80
COUNTS
RESTORE v1
LIST 80
COUNTS
UNWATCH tutor
COUNTS
```
출력:
```text
OK
OK
OK
OK
OK
a:90 b:80
tutor=3
OK
b:80
tutor=4
OK
NONE
```

첫 COUNTS의 3회는 ADD 두 번과 SCORE 한 번입니다. SNAP과 LIST는 알리지 않습니다. RESTORE는 한 번의 변경으로 취급하므로 4회가 되고, UNWATCH 뒤에는 현재 구독자가 없어 NONE입니다.

## 공개 예제 2 — 같은 점수 변경과 현재 구독자 기준 복구

입력:

```text
19
WATCH beta
ADD s3 50
SNAP start
SCORE s3 50
WATCH alpha
RESTORE start
COUNTS
UNWATCH beta
REMOVE s3
WATCH beta
ADD s1 70
RESTORE start
LIST 0
SCORE none 101
SNAP start
COUNTS
RESTORE start
LIST 60
COUNTS
```

출력:

```text
OK
OK
OK
OK
OK
OK
alpha=1 beta=3
OK
OK
OK
OK
OK
s3:50
ERROR missing
ERROR duplicate
alpha=4 beta=2
OK
NONE
alpha=5 beta=3
```

`SCORE s3 50`은 값이 같아도 성공한 SCORE이므로 beta에 알립니다. alpha는 나중에 구독했으므로 첫 COUNTS에서 1회입니다. beta는 해지 뒤 재구독할 때 0으로 시작합니다. RESTORE는 `s3:50`만 되돌리고 두 구독자의 현재 카운터는 되돌리지 않습니다. 없는 학생의 SCORE와 중복 SNAP은 알리지 않습니다. 마지막 RESTORE는 보드가 이미 같아도 각 구독자에게 한 번씩 알립니다.

## 빌드와 실행

**Visual Studio 2022/2026:** C++ 데스크톱 개발·CMake 도구가 설치된 환경에서 **파일 → 열기 → 폴더**로 `main.cpp`와 `CMakeLists.txt`가 함께 있는 폴더를 엽니다. CMake 구성 후 `lab` 대상을 빌드·실행하고 예제 입력 전체를 콘솔에 전달하세요. 같은 폴더의 **Developer Command Prompt**에서는 다음 명령도 사용할 수 있습니다. `<`는 일반 명령 프롬프트 문법입니다.

```bat
cl /nologo /std:c++17 /EHsc /utf-8 main.cpp /Fe:lab.exe
lab.exe < sample.in
```

**Linux/macOS/WSL2:** C++17 컴파일러가 설치된 터미널에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra main.cpp -o lab
./lab < sample.in
```

`sample.in`에는 한 예제의 N과 명령들만 저장하고 출력·설명 문장은 넣지 않습니다. 예제마다 새 실행을 시작하므로 보드·구독자·스냅샷은 처음에 모두 비어 있습니다. 시작 코드는 컴파일되지만 TODO 때문에 조회가 비거나 카운터가 증가하지 않는 등 올바른 결과를 주지 않습니다.

## 제출과 평가

필요한 모든 클래스와 함수를 **main.cpp 한 파일**에 구현합니다. 제공된 README와 CMake 설정은 보조 자료이며, 추가 답안 소스·실행 파일·빌드 폴더를 넣지 않습니다. 모두 저장한 뒤 확장의 제출 확인창에서 과제명과 실제 제출 파일 목록을 확인하세요. 알림 로그, 입력 안내, 객체 주소처럼 계약에 없는 출력을 추가하면 출력 비교에 영향을 줍니다.

기능 자동채점은 총 **100점**이며 명령 결과·구독 상태·알림 횟수·복구·순서·경계의 공개 계약을 검사합니다. 예제 외의 적법한 명령 조합도 평가됩니다. **자동 100점과 네 패턴의 구조 확인은 별도**입니다. Observer의 추상화, Memento의 캡슐화, Iterator의 순회 책임, Facade의 경계와 객체 수명은 교수자가 코드와 설명으로 별도 확인합니다.

## 자기점검

- [ ] Board는 CounterObserver의 구체 타입을 모르고 공통 Observer에 알린다.
- [ ] 해지한 객체가 알림을 받지 않고 소유권/수명 관리가 안전하다.
- [ ] Memento는 상태를 캡슐화하며 스냅샷 관리자는 내부 표현을 수정하지 않는다.
- [ ] Iterator가 필터 순회와 진행 상태를 캡슐화하고 Facade가 보드 컨테이너를 직접 순회하지 않는다.
- [ ] Facade가 UI/명령 처리에 단순한 API를 제공한다.
- [ ] 복구 대상(보드)과 대상이 아닌 상태(구독/카운터)의 경계를 설명한다.
- [ ] 기능 확장 시 각 패턴의 책임과 결합 지점을 설명할 수 있다.
- [ ] 같은 값의 SCORE와 같은 상태의 RESTORE도 성공 시 각각 한 번 알린다.
- [ ] 오류·SNAP·조회·구독 변경 자체는 카운터를 증가시키지 않는다.
- [ ] 점수 갱신은 순서를 유지하고, 삭제 후 재추가는 맨 뒤에 놓인다.

Iterator 사용 도중 보드를 변경하는 경우와 멀티스레드는 범위에서 제외합니다.

## 자주 묻는 질문

**RESTORE하면 저장 당시 구독자도 돌아오나요?** 아니요. 스냅샷에는 보드 항목과 순서만 들어갑니다. 복구 시점에 구독 중인 객체가 알림을 받습니다.

**점수가 실제로 같으면 변경 알림을 생략해도 되나요?** 안 됩니다. 이 문제에서는 값의 차이보다 성공한 명령을 기준으로 알림을 셉니다. 성공한 SCORE와 RESTORE는 동일 상태라도 한 번 알립니다.

**COUNTS를 출력할 때 Board가 구독자의 이름을 알아야 하나요?** 보드의 책임은 공통 Observer에 알리는 것입니다. 이름별 관리와 출력은 제공된 Facade의 책임입니다.

**LIST를 Facade에서 직접 vector 순회로 작성해도 출력은 같지 않나요?** 출력만 같아도 Iterator의 구조 요구는 만족하지 못합니다. 필터와 진행 위치는 Iterator에 맡기고 Facade는 그 인터페이스를 사용해야 합니다.
