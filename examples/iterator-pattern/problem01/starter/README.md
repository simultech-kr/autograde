# Iterator 패턴 실습 제출 기록 순회

C++17로 실습 제출 기록을 순회하는 프로그램을 완성합니다. **Iterator 패턴**을 사용하여 기록을 저장하는 객체와 다음 기록을 선택하는 객체의 책임을 분리합니다. 사용자는 내부 배열이나 인덱스를 직접 다루지 않고 공통 인터페이스로 순회합니다.

주차와 제출 마감은 수업 서버의 일정을 따릅니다. 외부 라이브러리, 인터넷, 실제 학생 정보는 필요하지 않습니다. 예제의 `s01` 등은 가상의 제출 기록 ID입니다.

## 문제 상황

교수자가 제출 기록을 등록 순서대로 확인하려고 합니다. 한쪽에서는 전체 기록을 읽고, 다른 쪽에서는 기준 점수 이상의 기록만 읽습니다. 두 순회가 서로의 진행 위치를 바꾸면 안 됩니다. 순회 중 새로운 기록을 추가하거나 원본 목록을 비워도 이미 확인하던 기록을 계속 읽을 수 있어야 합니다.

이를 위해 두 순회 슬롯 `A`, `B`를 둡니다. 각 슬롯에 Iterator를 만들 때 현재 목록을 복사하여 보관합니다. 이 복사본을 **스냅샷**이라고 합니다. 이후에는 해당 스냅샷에서 기준 점수 이상의 기록만 등록 순서대로 반환합니다.

스냅샷은 이 과제에서 선택한 변경 처리 정책입니다. 모든 Iterator가 원본을 복사해야 한다는 뜻은 아닙니다. STL 반복자 전체 구현, 다중 스레드, 파일 저장은 요구하지 않습니다.

## 제공 파일과 수정 범위

| 파일 | 내용 |
| --- | --- |
| `main.cpp` | 추상 인터페이스, TODO 5개, 제공된 목록 관리와 명령 처리 |
| `HINTS.md` | 구현 순서와 평가 항목별 점검 방법 |
| `CMakeLists.txt` | C++17 빌드 설정 |
| `README.md` | 동작 규칙, 예제, 빌드와 제출 방법 |

`main.cpp`의 TODO 1~5를 구현합니다. 클래스 이름, public 함수 선언, 제공된 목록 관리·명령 처리·`main`은 유지합니다. 필요한 private 보조 함수는 추가할 수 있습니다. 예상 출력을 하드코딩하거나 명령 처리 코드에 별도의 순회 로직을 넣지 않습니다.

시작 코드는 컴파일되지만 미완성 함수가 `std::logic_error`를 발생시킵니다. 제공된 `main`은 이를 `ERROR incomplete`로 출력합니다. 이는 정답 출력이 아닙니다. 완성 후에는 아래 계약에 맞는 입력에서 이 문자열이 나오지 않아야 합니다.

## 클래스와 책임

| 구성 | 클래스 | 책임 |
| --- | --- | --- |
| 데이터 | `Submission` | 제출 ID `id`와 점수 `score` 보관 |
| Iterator 인터페이스 | `SubmissionIterator` | `hasNext`, `next`, `reset`의 공통 호출 제공 |
| 구체 Iterator | `ScoreIterator` | 스냅샷, 기준 점수, 현재 위치를 소유하고 조건에 맞는 기록 선택 |
| 목록 인터페이스 | `SubmissionCollection` | `createIterator(minScore)`로 Iterator 생성 |
| 구체 목록 | `SubmissionLog` | 원본 기록을 추가·삭제·조회하고 Iterator에 생성 시점 복사본 전달 |
| 클라이언트 | `LogApp` | 입력 검증, 슬롯 선택, 출력 형식 처리. 추상 Iterator를 통해 순회 |

`SubmissionLog`의 저장소는 private입니다. `LogApp`이 그 벡터를 꺼내 직접 순회하지 않습니다. 두 슬롯의 위치는 목록이나 전역 변수가 아니라 각 `ScoreIterator`가 보관합니다.

| 함수 | 계약 |
| --- | --- |
| `bool hasNext() const` | 반환할 기록이 남았는지만 확인. 위치를 바꾸지 않음 |
| `std::optional<Submission> next()` | 다음 기록 하나를 값으로 반환하고 다음 일치 위치로 이동. 없으면 `std::nullopt` |
| `void reset()` | 같은 스냅샷과 기준 점수를 유지하고 처음 일치 위치로 이동 |
| `createIterator(int minScore) const` | 새 Iterator를 생성하여 `std::unique_ptr<SubmissionIterator>`로 반환 |

`std::optional`은 값이 있는 경우와 없는 경우를 구분합니다. 점수 0인 기록도 유효한 값이므로 0을 종료 표시로 쓰지 않습니다. `std::unique_ptr`는 Iterator 객체의 소유권을 관리합니다. 지역 변수의 주소나 이미 사라진 기록의 참조를 반환하지 않습니다.

## 입력 명령과 출력

첫 줄은 명령 수 `N`이며 `1 ≤ N ≤ 200`입니다. 이후 정확히 `N`줄의 명령이 주어집니다. 명령마다 출력은 한 줄입니다. 단어 사이에는 공백 한 칸을 쓰며 입력 안내, 디버깅 메시지, 추가 빈 줄은 출력하지 않습니다.

| 명령 | 성공 시 의미와 출력 |
| --- | --- |
| `ADD id score` | 원본 목록 끝에 추가. `OK ADD` |
| `CLEAR` | 원본 목록의 모든 기록 삭제. `OK CLEAR` |
| `SIZE` | 현재 원본 목록 크기. `SIZE <개수>` |
| `OPEN slot min_score` | 선택 슬롯에 새 스냅샷 Iterator 생성. `OK OPEN` |
| `HAS slot` | 남은 일치 기록이 있으면 `HAS 1`, 없으면 `HAS 0` |
| `NEXT slot` | 다음 일치 기록이 있으면 `ITEM <id> <score>`, 없으면 `END` |
| `RESET slot` | 선택 Iterator의 같은 스냅샷을 처음부터 다시 순회. `OK RESET` |

명령 이름과 인수 개수, 정수 문법은 올바르게 주어집니다. 점수와 기준 점수 입력은 `-1000~1000`의 정수이며, 그중 **0 이상 100 이하**만 유효합니다. ID는 영문 대소문자·숫자·밑줄로 된 1~20자 문자열입니다. 슬롯 이름은 공백 없는 ASCII 문자열이며 `A`, `B`만 유효합니다. 모든 문자열은 대소문자를 구분합니다. 알 수 없는 명령어, 잘못된 인수 개수 등 문법 오류의 처리는 실습 범위가 아닙니다.

## 순회와 스냅샷 규칙

1. 프로그램 시작 시 원본 목록은 비어 있고 `A`, `B`는 모두 열리지 않았습니다. 새 실행은 다시 이 상태에서 시작합니다.
2. `ADD`의 ID는 현재 원본 목록 안에서만 유일합니다. `CLEAR` 후에는 같은 ID로 다른 점수의 기록을 추가할 수 있습니다.
3. `OPEN A 70`은 그 시점 원본을 복사하고 **점수 ≥ 70**인 기록만 선택합니다. 점수순·ID순 정렬이 아니라 원래 등록 순서를 유지합니다.
4. 처음 위치는 첫 번째 일치 기록입니다. `NEXT`는 일치 기록을 정확히 하나 반환하고 다음 일치 위치로 이동합니다. 사이의 불일치 기록은 건너뜁니다.
5. `HAS`는 조회입니다. 연속 호출해도 다음 `NEXT`의 결과가 달라지지 않습니다.
6. `A`, `B`의 스냅샷·기준 점수·위치는 독립적입니다. 한 슬롯의 순회·초기화·재생성이 다른 슬롯에 영향을 주지 않습니다.
7. `ADD`, `CLEAR`는 원본만 바꿉니다. 기존 Iterator의 스냅샷과 위치는 바뀌지 않습니다. `SIZE`도 원본 크기만 보여 줍니다.
8. `RESET`은 기존 스냅샷의 첫 일치 기록으로 돌아갑니다. 원본을 다시 읽거나 기준 점수를 0으로 바꾸지 않습니다.
9. 같은 슬롯에 다시 `OPEN`하면 그 슬롯의 이전 Iterator를 교체합니다. 현재 원본, 새 기준 점수, 새 시작 위치를 사용합니다.
10. 빈 원본 또는 일치 기록이 없는 원본도 `OPEN`할 수 있습니다. 이 경우 `HAS 0`, `END`이며 `RESET`은 성공합니다.
11. 종료 후 `NEXT`를 반복해도 계속 `END`입니다. 자동으로 처음으로 돌아가거나 마지막 기록을 다시 반환하지 않습니다.

## 오류 규칙

검증과 오류 문자열은 제공 코드에 들어 있습니다. Iterator를 연결한 뒤에도 아래 동작을 유지해야 합니다. 오류 명령은 원본 목록과 두 Iterator의 스냅샷·기준 점수·위치를 모두 보존합니다.

| 명령 | 검사 순서 | 오류 출력 |
| --- | --- | --- |
| `ADD` | 점수 범위 → 현재 원본의 ID 중복 | `ERROR score` 또는 `ERROR duplicate` |
| `OPEN` | 슬롯 이름 → 기준 점수 범위 | `ERROR slot` 또는 `ERROR score` |
| `HAS`, `NEXT`, `RESET` | 슬롯 이름 → 그 슬롯의 Iterator 존재 | `ERROR slot` 또는 `ERROR unopened` |

예를 들어 `OPEN C -1`은 `ERROR slot`입니다. 이미 `s01`이 있어도 `ADD s01 200`은 `ERROR score`입니다. 유효한 슬롯이라도 한 번도 열지 않았다면 `NEXT A`는 `END`가 아니라 `ERROR unopened`입니다. 반대로 빈 목록으로 정상 `OPEN`한 슬롯은 열린 상태이므로 `END`를 반환합니다. 실패한 `OPEN` 때문에 기존 슬롯을 비우면 안 됩니다.

## 예제 1 필터와 비소비 조회

입력:

```text
14
ADD s01 40
ADD s02 70
ADD s03 90
OPEN A 70
HAS A
HAS A
NEXT A
NEXT A
HAS A
NEXT A
NEXT A
RESET A
NEXT A
SIZE
```

출력:

```text
OK ADD
OK ADD
OK ADD
OK OPEN
HAS 1
HAS 1
ITEM s02 70
ITEM s03 90
HAS 0
END
END
OK RESET
ITEM s02 70
SIZE 3
```

기준 점수와 같은 70도 포함합니다. `HAS` 두 번은 기록을 소비하지 않습니다. 종료 후에도 `RESET`하면 같은 스냅샷의 `s02`부터 읽습니다.

## 예제 2 독립 순회와 원본 삭제

입력:

```text
20
ADD s01 50
ADD s02 80
OPEN A 0
OPEN B 70
NEXT A
ADD s03 95
CLEAR
SIZE
HAS B
NEXT B
NEXT B
NEXT A
NEXT A
ADD s01 100
OPEN B 90
NEXT B
RESET A
NEXT A
NEXT A
SIZE
```

출력:

```text
OK ADD
OK ADD
OK OPEN
OK OPEN
ITEM s01 50
OK ADD
OK CLEAR
SIZE 0
HAS 1
ITEM s02 80
END
ITEM s02 80
END
OK ADD
OK OPEN
ITEM s01 100
OK RESET
ITEM s01 50
ITEM s02 80
SIZE 1
```

처음 두 Iterator는 `s03` 추가 전에 생성되었으므로 `s03`을 포함하지 않습니다. 원본을 비워도 당시 기록은 남습니다. 새로 연 `B`는 `s01 100`을 읽지만, 기존 `A`는 초기화 후에도 예전 값 `s01 50`을 읽습니다.

## 예제 3 오류와 일치 기록 없음

입력:

```text
18
NEXT A
OPEN C -1
ADD s01 -1
ADD s01 60
ADD s01 200
ADD s01 90
OPEN A 60
OPEN A -1
HAS A
NEXT A
HAS A
OPEN B 100
HAS B
NEXT B
RESET B
HAS B
HAS a
SIZE
```

출력:

```text
ERROR unopened
ERROR slot
ERROR score
OK ADD
ERROR score
ERROR duplicate
OK OPEN
ERROR score
HAS 1
ITEM s01 60
HAS 0
OK OPEN
HAS 0
END
OK RESET
HAS 0
ERROR slot
SIZE 1
```

실패한 `OPEN A -1` 뒤에도 기존 `A`의 기준 점수 60과 위치가 유지됩니다. `B`는 정상적으로 열렸지만 100점인 기록이 없으므로 바로 종료 상태입니다.

## 구현 순서

1. `advanceToMatch`에서 현재 위치부터 점수가 기준 미만인 동안 건너뜁니다. 벡터 접근보다 범위 검사가 먼저입니다.
2. `hasNext`에서 위치가 스냅샷 범위 안에 있는지만 판단합니다. 앞 단계가 첫 일치 위치 또는 끝을 유지하도록 설계합니다.
3. `next`에서 종료 여부를 확인하고 현재 기록을 복사합니다. 위치를 한 칸 옮긴 뒤 다음 일치 기록을 찾습니다. 반환할 값과 다음 위치를 혼동하지 않습니다.
4. `reset`에서 위치를 0으로 바꾸고 첫 일치 기록을 다시 찾습니다. 스냅샷과 기준 점수는 바꾸지 않습니다.
5. `createIterator`에서 현재 목록의 값 복사본과 기준 점수로 새 `ScoreIterator`를 생성합니다. 추상 타입의 소유 포인터로 반환합니다.
6. 세 예제의 각 명령 뒤 원본 목록과 두 슬롯의 위치를 표로 적어 실제 출력과 비교합니다.

여러 TODO가 남아 있으면 하나를 고쳐도 점수가 바로 오르지 않을 수 있습니다. 모든 자동채점 항목이 Iterator를 생성하거나 사용합니다. 먼저 전체 생성·조회·반환 경로를 연결한 뒤 [HINTS.md](HINTS.md)의 평가 ID별 힌트로 확인합니다.

## 빌드와 실행

Linux, WSL2, macOS에서는 C++17 컴파일러를 사용합니다. 예제 입력 하나를 `input.txt`에 저장합니다.

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o submission
./submission < input.txt
```

Windows Visual Studio에서는 C++ 개발 도구가 설치된 **Developer Command Prompt**에서 실행합니다. 일반 PowerShell은 `cl` 경로나 `<` 입력 리디렉션이 다를 수 있으므로 이 예시는 개발자 명령 프롬프트 기준입니다.

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:submission.exe
submission.exe < input.txt
```

CMake를 지원하는 IDE에서는 받은 폴더를 열고 `submission` 대상을 빌드합니다. Visual Studio는 별도 `.sln` 없이 CMake 폴더로 열 수 있습니다.

```sh
cmake -S . -B build
cmake --build build --config Debug --target submission
```

일반적인 단일 구성 빌드의 실행 파일은 `build/submission`, Visual Studio Debug 구성은 `build\Debug\submission.exe`입니다. 직접 콘솔 프로젝트를 만든다면 C++17을 설정하고 `main.cpp` 하나를 빌드합니다.

## 평가 항목

기능 자동채점은 총 100점입니다. 각 항목의 출력 전체가 맞으면 배점 전부를 받으며, 한 항목 안에서의 부분 점수는 없습니다. 매 항목은 새로운 프로세스로 실행합니다.

| ID | 점수 | 평가 요소 |
| --- | ---: | --- |
| I1 | 10 | 등록 순서와 ID·점수 보존 |
| I2 | 10 | 기준 점수 이상 포함, 처음·중간·끝의 연속 불일치 기록 건너뛰기 |
| I3 | 10 | 반복 `HAS`가 위치를 바꾸지 않음 |
| I4 | 10 | `NEXT` 한 건씩 전진, 종료 후 반복 `END` |
| I5 | 10 | 두 슬롯의 서로 다른 필터와 진행 위치 독립, 한쪽 초기화 시 다른 쪽 유지 |
| I6 | 10 | `ADD` 후 기존 스냅샷 불변, 새 `OPEN`에만 추가 반영 |
| I7 | 10 | `CLEAR` 후에도 기존 순회 유지, ID 재사용 시 값 구분 |
| I8 | 10 | 빈 목록 및 일치 기록이 없는 목록의 안전한 순회 |
| I9 | 5 | `RESET`이 같은 스냅샷과 기준 점수로 돌아감 |
| I10 | 5 | 재생성한 슬롯만 새 목록·기준·위치로 교체 |
| I11 | 5 | 점수와 기준 점수의 0·100 경계 포함 |
| I12 | 5 | 오류 우선순위와 기존 원본·Iterator 상태 보존 |
| 합계 | 100 | 기능 평가 |

I1, I2, I3, I9는 공개 테스트입니다. 나머지는 입력과 예상 출력만 비공개이며 평가 기준은 위와 같습니다. 채점 상세 결과의 `평가 요소`는 확인 대상, 실패한 항목의 수정 힌트는 점검 방향을 알려 줍니다. 이 안내가 특정 소스 행의 오류를 확정하는 것은 아닙니다.

컴파일 실패 시 기능 항목은 **미검사**입니다. 컴파일 문제부터 해결하고 다시 제출합니다. 이 경우 실행되지 않은 기능에 대한 실패 힌트 대신 선행 오류 안내를 받습니다.

기능 100점은 Iterator 패턴 구조의 증명이 아닙니다. 교수자는 별도로 추상 인터페이스 사용, 목록의 캡슐화, Iterator별 상태 분리, 스냅샷 소유권, 클라이언트의 순회 위임을 코드에서 확인합니다. 본 패키지는 구조 검토 점수를 기능 100점에 자동 합산하지 않습니다.

## 제출과 수정

1. 받은 과제 폴더의 `main.cpp`를 수정하고 저장합니다.
2. 컴파일한 뒤 위 세 예제의 출력과 비교합니다.
3. IDE 확장에서 선택한 과제와 현재 폴더를 확인하고 제출합니다. 실행 파일과 빌드 폴더는 제출하지 않습니다.
4. 제출 접수 후 채점 완료를 기다립니다. 이번 제출 시각과 결과를 확인하고 이전 최고점과 구분합니다.
5. 실패 항목의 ID를 `HINTS.md`에서 찾아 점검하고 다시 제출합니다.

기존 실습을 이어서 할 때는 이미 받은 폴더를 엽니다. 다시 다운로드해야 한다면 기존 답안을 덮어쓰지 않도록 새 폴더에 받습니다.
