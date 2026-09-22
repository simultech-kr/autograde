# COME2201 · Week14 · Problem10
## 실습 진행 보드 — Observer, Memento, Iterator, Facade
마감: **2026-12-02 13:50 (KST)**

튜터는 학생 실습 점수를 확인하고 변경 알림을 받습니다. 평가 시점의 보드를 저장했다가 복구할 수 있어야 하고, 일정 점수 이상인 학생만 내부 저장 구조에 직접 접근하지 않고 순회해야 합니다.

필수 패턴은 **Observer, Memento, Iterator, Facade**입니다. `main.cpp`에 제공된 Board/BoardFacade 골격을 유지하고 알림, 구독, 필터 순회, 저장·복구 TODO를 완성하세요. **main.cpp 한 파일**에 제출합니다. 알림은 출력 메시지를 추가하지 않고 구독자별 카운터를 갱신합니다.

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

## 공개 예제
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

## 실행 및 평가
VS2022/2026의 폴더 열기/CMake 빌드 또는 Linux/macOS/WSL2에서 `c++ -std=c++17 main.cpp -o lab`로 빌드하세요. 시작 코드는 컴파일되지만 TODO 때문에 올바른 결과를 주지 않습니다. 자동채점 100점은 구독/해지/재구독, 알림 횟수, 점수 경계, 빈 보드, 필터, 반복 복구와 순서를 검사합니다. 기능 점수와 별도로 교수자가 패턴 조합의 구조를 확인합니다.

- [ ] Board는 CounterObserver의 구체 타입을 모르고 공통 Observer에 알린다.
- [ ] 해지한 객체가 알림을 받지 않고 소유권/수명 관리가 안전하다.
- [ ] Memento는 상태를 캡슐화하며 스냅샷 관리자는 내부 표현을 수정하지 않는다.
- [ ] Iterator가 필터 순회와 진행 상태를 캡슐화하고 Facade가 보드 컨테이너를 직접 순회하지 않는다.
- [ ] Facade가 UI/명령 처리에 단순한 API를 제공한다.
- [ ] 복구 대상(보드)과 대상이 아닌 상태(구독/카운터)의 경계를 설명한다.
- [ ] 기능 확장 시 각 패턴의 책임과 결합 지점을 설명할 수 있다.

Iterator 사용 도중 보드를 변경하는 경우와 멀티스레드는 범위에서 제외합니다.
