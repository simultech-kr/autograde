# COME2201 · Week12 · Problem08
## 실습 계획 편집기 — Memento, Visitor
마감: **2026-11-18 13:50 (KST)**

실습 계획 문서는 텍스트 블록과 배점 블록이 섞여 있습니다. 사용자는 이름을 붙여 상태를 저장하고 반복 복구합니다. 블록 클래스를 수정하지 않고 통계 산출과 출력 기능을 추가할 수 있어야 합니다.

`main.cpp`의 Visitor double dispatch, 문서 순회, 독립적인 상태 저장·복구 TODO를 완성하세요. `Document`가 Originator, `Document::Snapshot`이 Memento, main의 스냅샷 맵이 Caretaker입니다. Caretaker는 스냅샷 내부 데이터에 접근하지 않습니다. **main.cpp 한 파일**에 제출합니다.

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

## 공개 예제
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

## 실행과 평가
VS2022/2026에서 폴더를 열어 CMake 빌드, 또는 `c++ -std=c++17 main.cpp -o lab`(Linux/macOS/WSL2). 시작 코드는 컴파일되지만 아직 정답 동작을 하지 않습니다. 기능 자동채점 100점은 빈 문서, 배점 경계, 오류, 저장 중복, 반복 복구를 포함합니다. 구조적 패턴 준수는 자동 점수와 별개로 코드 확인합니다.

- [ ] Snapshot 내부 표현이 Caretaker에 노출되지 않는다.
- [ ] 저장과 복구 모두 독립 복제를 수행하여 반복 복구가 안정적이다.
- [ ] 각 Block의 accept가 해당 구체 타입의 visit을 호출한다.
- [ ] 통계와 출력 Visitor가 분리되어 있고 동작마다 새 Visitor로 집계한다.
- [ ] 타입명 문자열 비교나 dynamic_cast 연쇄로 Visitor를 대체하지 않는다.
- [ ] 새로운 연산과 새로운 블록 타입을 추가할 때 변경 범위 차이를 설명한다.
