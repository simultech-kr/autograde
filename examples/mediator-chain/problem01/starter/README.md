# Mediator · Chain of Responsibility — 실습실 지원 요청 중계기

C++17로 학생 문의를 담당자에게 전달하는 프로그램을 완성합니다. **Mediator**는 단말 사이의 통신과 전달 상태를 조정하고, **Chain of Responsibility**는 문의를 받을 담당자를 순서대로 선택합니다.

## 문제 상황

학생 단말 `A`, `B`가 실습 문의를 보냅니다. 담당자는 튜터 `TUTOR`, 환경 담당 `TECH`, 교수자 `PROF`입니다. 각 학생 단말이 모든 담당자를 직접 알게 하지 않고 공통 Mediator에 전송을 요청합니다. Mediator는 Handler 연쇄에 담당자 선택을 맡긴 뒤 선택한 담당자에게 문의를 전달합니다.

같은 문의라도 연쇄 순서에 따라 담당자가 달라질 수 있습니다. 기본 모드는 긴급 문의를 먼저 확인하고, DOMAIN 모드는 담당 분야를 먼저 확인합니다. 이 과제는 표준 입력·출력으로만 동작하며 실제 네트워크나 파일 저장은 구현하지 않습니다.

## 제공 파일과 수정 범위

| 파일 | 내용 |
| --- | --- |
| `main.cpp` | 클래스, TODO 6개, 제공된 파서·명령 처리·조회 |
| `HINTS.md` | 구현 순서와 평가 ID별 로컬 점검 |
| `CMakeLists.txt` | C++17 빌드 설정과 `labhelpdesk` 대상 |
| `README.md` | 동작 계약, 예제, 빌드·제출, 평가 기준 |

`main.cpp`의 TODO 1~6을 구현합니다. 클래스 이름과 public 함수 선언, 제공된 파서·명령 처리·`main`은 유지합니다. 필요한 private 보조 함수는 추가할 수 있습니다. 명령 처리에 별도 배분 로직을 넣거나 예상 출력을 하드코딩하지 않습니다.

시작 코드는 컴파일 가능한 미완성 코드입니다. 연결되지 않은 `SEND`는 `UNHANDLED`를 반환할 수 있으므로, 컴파일 성공만으로 기능이 완성된 것은 아닙니다.

## 클래스와 책임

| 구성 | 클래스 | 책임 |
| --- | --- | --- |
| 문의 데이터 | `Request` | `id`, `sender`, `kind`, `priority` 보관 |
| Mediator 인터페이스 | `ILabMediator` | 문의 전송의 공통 인터페이스 |
| 학생 단말 | `StudentTerminal` | 자신의 A/B ID로 문의를 만들고 Mediator에 위임 |
| 담당자 단말 | `StaffTerminal` | 온라인 상태와 받은 문의의 수신함 보관 |
| 연쇄 공통 구현 | `Handler` | 방문 기록, 수락 여부 확인, 다음 Handler에 위임 |
| 구체 Handler | `PriorityHandler`, `KindHandler` | 우선순위 또는 문의 종류에 대한 수락 조건 |
| 구체 Mediator | `LabMediator` | 연쇄 소유, 담당자 선택 요청, 전달·중복·통계 관리 |
| 클라이언트 | `main`, `executeLine` | 객체 생성과 수명 관리, 명령 처리, 조회 결과 출력 |

`Handler::handle(request, trace)`는 담당자 이름을 담은 `std::optional<std::string>`을 반환합니다. 처리자가 없으면 `std::nullopt`입니다. **담당자를 선택했다는 사실과 전달에 성공했다는 사실은 다릅니다.** Handler는 담당자의 온라인 상태를 검사하거나 수신함에 문의를 넣지 않습니다.

`LabMediator`는 연쇄의 머리를, 각 Handler는 다음 Handler를 `std::unique_ptr`로 소유합니다. 연쇄를 교체하면 이전 연쇄의 수명도 끝납니다. `StudentTerminal`은 `ILabMediator&`를 빌려 사용하며 Mediator를 소유하지 않습니다. 제공된 `main`의 객체 선언 순서를 유지하여 Mediator가 학생 단말보다 오래 존재하도록 합니다.

## 입력과 명령

첫 줄은 명령 수 `N`이며 `1 ≤ N ≤ 200`입니다. 이후 정확히 `N`개의 명령을 한 줄씩 입력합니다. 채점 입력은 문법과 인수 범위가 모두 유효합니다.

- ID: `1~10000`의 정수
- 학생 단말: `A`, `B`
- 문의 종류: `CODE`, `ENV`, `GRADE`, `OTHER`
- 우선순위: `1`, `2`, `3`이며 3이 가장 높음
- 담당자: `TUTOR`, `TECH`, `PROF`
- 모드: `PRIORITY`, `DOMAIN`, `EMPTY`

문자열은 대소문자를 구분합니다. 제공 파서는 명령 줄의 잘못된 문법이나 범위에 `ERROR input`을 출력하며, 이 오류 입력 처리는 채점하지 않습니다. 단어 사이 공백 한 칸을 사용하고 입력 안내, 디버깅 메시지, 추가 빈 줄은 출력하지 않습니다.

| 명령 | 출력과 의미 |
| --- | --- |
| `SEND id sender kind priority` | 아래 전송 규칙에 따라 `SENT`, `OFFLINE`, `UNHANDLED`, `DUPLICATE` 중 하나 출력 |
| `MODE mode` | 연쇄 교체 후 `OK MODE` |
| `ONLINE role 0` | 해당 담당자를 오프라인으로 설정 후 `OK ONLINE` |
| `ONLINE role 1` | 해당 담당자를 온라인으로 설정 후 `OK ONLINE` |
| `TRACE` | 최근 SEND의 방문 기록. 없으면 `TRACE -`, 있으면 `TRACE PRIORITY>CODE`와 같은 형식 |
| `INBOX role` | `INBOX <role> <개수>`를 출력한 뒤 각 문의를 `ITEM <id> <sender> <kind> <priority>`로 한 줄씩 출력 |
| `STATUS` | `STATUS A <개수> B <개수> TUTOR <개수> TECH <개수> PROF <개수>` |

`STATUS`의 A/B는 해당 학생이 **성공적으로 보낸 문의 수**이고, TUTOR/TECH/PROF는 각 수신함 크기입니다. `INBOX`는 도착 순서를 유지하며, 수신함이 비어 있으면 머리 줄만 출력합니다. `TRACE`, `INBOX`, `STATUS`는 상태를 바꾸지 않는 조회입니다.

## 연쇄와 전달 규칙

프로그램 시작 시 모드는 `PRIORITY`, 모든 담당자는 온라인입니다. 수신함, 발신자별 성공 횟수, 성공 ID 집합, trace는 모두 비어 있습니다. 테스트마다 새 프로세스로 시작합니다.

| Handler 이름 | 수락 조건 | 선택 담당자 |
| --- | --- | --- |
| `PRIORITY` | `priority == 3`이고 `kind != OTHER` | `PROF` |
| `CODE` | `kind == CODE` | `TUTOR` |
| `ENV` | `kind == ENV` | `TECH` |
| `GRADE` | `kind == GRADE` | `PROF` |

| 모드 | 연쇄 순서 |
| --- | --- |
| `PRIORITY` | `PRIORITY → CODE → ENV → GRADE` |
| `DOMAIN` | `CODE → ENV → GRADE → PRIORITY` |
| `EMPTY` | Handler가 없는 빈 연쇄 |

각 `SEND`는 다음 순서를 따릅니다.

1. 직전 trace를 비웁니다.
2. ID가 과거에 성공한 적이 있으면 `DUPLICATE <id>`를 출력하고 종료합니다. 학생·문의 종류·담당자가 달라도 중복입니다. Handler를 방문하지 않으므로 trace는 비어 있습니다.
3. 머리부터 Handler를 방문합니다. 각 Handler는 자신의 이름을 trace에 기록한 뒤 수락 조건을 검사합니다. **처음 수락한 Handler에서 멈춥니다.**
4. 아무 Handler도 수락하지 않으면 `UNHANDLED <id>`를 출력합니다. EMPTY에서는 Handler 방문도 없습니다.
5. 선택한 담당자가 오프라인이면 `OFFLINE <id> <role>`을 출력합니다. 연쇄 탐색을 다시 시작하거나 다음 Handler로 넘어가지 않습니다.
6. 온라인이면 해당 수신함 끝에 문의를 추가하고, 발신자의 성공 횟수를 1 늘리고, 성공 ID를 기록한 뒤 `SENT <id> <role>`을 출력합니다.

실패한 전송은 수신함·성공 횟수·성공 ID 집합을 바꾸지 않습니다. 따라서 `OFFLINE` 또는 `UNHANDLED`였던 ID는 나중에 다시 사용할 수 있습니다. 성공한 ID는 모드를 바꿔도 다시 사용할 수 없습니다.

`MODE`는 연쇄를 교체하고 trace를 비웁니다. 기존 수신함·성공 ID·성공 횟수·온라인 상태는 보존합니다. `ONLINE`은 해당 담당자의 온라인 상태만 바꾸며 trace를 보존합니다. 예를 들어 PRIORITY 모드의 `CODE 3`은 PROF를 선택합니다. PROF가 오프라인이어도 TUTOR로 우회하지 않습니다. DOMAIN 모드의 같은 문의는 CODE Handler가 먼저 수락하여 TUTOR를 선택합니다.

## 예제 1 담당 분야와 수신함

입력:

```text
9
SEND 1 A CODE 1
TRACE
SEND 2 B ENV 2
SEND 3 A GRADE 1
INBOX TUTOR
INBOX TECH
INBOX PROF
STATUS
TRACE
```

출력:

```text
SENT 1 TUTOR
TRACE PRIORITY>CODE
SENT 2 TECH
SENT 3 PROF
INBOX TUTOR 1
ITEM 1 A CODE 1
INBOX TECH 1
ITEM 2 B ENV 2
INBOX PROF 1
ITEM 3 A GRADE 1
STATUS A 2 B 1 TUTOR 1 TECH 1 PROF 1
TRACE PRIORITY>CODE>ENV>GRADE
```

조회는 trace를 바꾸지 않습니다. 마지막 trace는 세 번째 SEND가 방문한 Handler 목록입니다.

## 예제 2 연쇄 순서와 처리자 없음

입력:

```text
11
SEND 10 A CODE 3
TRACE
MODE DOMAIN
TRACE
SEND 11 B CODE 3
TRACE
SEND 12 A OTHER 3
TRACE
MODE EMPTY
SEND 13 B ENV 2
TRACE
```

출력:

```text
SENT 10 PROF
TRACE PRIORITY
OK MODE
TRACE -
SENT 11 TUTOR
TRACE CODE
UNHANDLED 12
TRACE CODE>ENV>GRADE>PRIORITY
OK MODE
UNHANDLED 13
TRACE -
```

OTHER는 우선순위 3이어도 어느 Handler도 수락하지 않습니다. EMPTY는 검사할 Handler 자체가 없는 상태입니다.

## 예제 3 실패 재시도와 전역 중복

입력:

```text
14
ONLINE PROF 0
SEND 20 A CODE 3
TRACE
ONLINE PROF 1
SEND 20 B CODE 3
SEND 20 A ENV 1
TRACE
MODE EMPTY
SEND 21 A GRADE 1
MODE DOMAIN
SEND 21 A ENV 1
INBOX PROF
INBOX TECH
STATUS
```

출력:

```text
OK ONLINE
OFFLINE 20 PROF
TRACE PRIORITY
OK ONLINE
SENT 20 PROF
DUPLICATE 20
TRACE -
OK MODE
UNHANDLED 21
OK MODE
SENT 21 TECH
INBOX PROF 1
ITEM 20 B CODE 3
INBOX TECH 1
ITEM 21 A ENV 1
STATUS A 1 B 1 TUTOR 0 TECH 1 PROF 1
```

실패한 ID 20과 21은 재시도할 수 있습니다. ID 20이 성공한 뒤에는 다른 학생과 다른 종류로 보내도 중복입니다. 모드를 바꿔도 기존 수신함은 유지됩니다.

## 구현 순서

1. **TODO 1 — `Handler::handle`**: 방문 이름을 기록하고, 수락하면 담당자를 반환하고, 거절하면 다음 Handler에 위임합니다. 다음 객체도 없으면 `std::nullopt`입니다.
2. **TODO 2 — `PriorityHandler::accepts`**: 우선순위 3이면서 OTHER가 아닌지 검사합니다.
3. **TODO 3 — `KindHandler::accepts`**: 요청 종류와 해당 Handler의 종류가 같은지 검사합니다. 두 `accepts`에서 온라인 상태는 검사하지 않습니다.
4. **TODO 4 — `buildChain`**: 각 모드의 순서대로 Handler를 연결합니다. EMPTY의 머리는 빈 소유 포인터입니다.
5. **TODO 5 — `StudentTerminal::send`**: 자신의 `name_`을 `Request.sender`에 넣고 전달받은 요청 ID를 보존한 `Request`를 만들어 `ILabMediator::send`에 전달합니다.
6. **TODO 6 — `LabMediator::send`**: trace 초기화→전역 중복 확인→담당자 선택→온라인 확인→성공 상태 갱신 순서를 연결합니다.

세 예제를 확인한 뒤 [HINTS.md](HINTS.md)의 평가 ID별 점검을 진행합니다. 미완성 TODO가 여러 개면 하나를 고쳐도 점수가 바로 오르지 않을 수 있습니다.

## 빌드와 실행

예제 입력 하나를 `input.txt`에 저장합니다. 프로그램은 첫 줄의 명령 수를 포함한 표준 입력을 읽습니다.

**Visual Studio 2022/2026**에서는 C++ 데스크톱 개발 도구와 CMake 도구를 설치하고 받은 폴더를 **폴더 열기**로 엽니다. `CMakeLists.txt`를 인식한 뒤 `labhelpdesk` 대상을 빌드합니다. 별도 `.sln`은 필요하지 않습니다. 해당 Visual Studio의 **Developer Command Prompt**에서 직접 빌드·실행할 수도 있습니다.

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:labhelpdesk.exe
labhelpdesk.exe < input.txt
```

`<` 입력 리디렉션을 쓰는 Windows 명령은 cmd/Developer Command Prompt 기준입니다. 일반 PowerShell에서 그대로 실행하지 않습니다.

**VS Code, macOS, WSL2, Linux**에서는 설치된 C++17 컴파일러로 빌드합니다. VS Code는 편집기이므로 C++ 컴파일러를 별도로 설치해야 합니다. macOS·WSL2·Linux 터미널의 예시는 다음과 같습니다.

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o labhelpdesk
./labhelpdesk < input.txt
```

Windows의 VS Code에서도 MSVC 개발자 환경과 위 `cl` 명령을 사용할 수 있습니다. Windows에 `g++`가 설치되어 있다고 가정하지 않습니다.

모든 환경에서 CMake를 사용할 수 있습니다.

```sh
cmake -S . -B build
cmake --build build --config Debug --target labhelpdesk
```

일반적인 단일 구성 빌드의 실행 파일은 `build/labhelpdesk`, Visual Studio Debug 구성은 `build\Debug\labhelpdesk.exe`입니다. 해당 실행 파일에 `input.txt`를 표준 입력으로 전달합니다. IDE에서 직접 콘솔 프로젝트를 만들면 C++17을 선택하고 `main.cpp` 하나를 빌드합니다.

## 평가 항목

기능 자동채점은 총 100점입니다. 각 항목의 전체 출력이 맞으면 배점 전부를 받으며, 항목 안의 부분 점수는 없습니다.

| ID | 점수 | 평가 요소 |
| --- | ---: | --- |
| C1 | 10 | 일반 CODE 문의를 TUTOR로 선택 |
| C2 | 10 | 일반 ENV 문의를 TECH로 선택 |
| C3 | 5 | 일반 GRADE 문의를 PROF로 선택 |
| C4 | 10 | PRIORITY 모드에서 우선순위 3이며 OTHER가 아닌 문의를 PROF로 선택 |
| C5 | 5 | 방문 순서 기록, 첫 일치 뒤 즉시 종료 |
| C6 | 10 | DOMAIN과 PRIORITY의 연쇄 순서 변경 |
| C7 | 5 | OTHER를 처리자 없음으로 판단 |
| C8 | 5 | EMPTY의 안전한 처리와 정상 모드 복원 |
| M1 | 5 | A/B 발신자 ID 보존 |
| M2 | 10 | 담당자별 수신함 분리, 도착 순서와 문의 값 보존 |
| M3 | 10 | 오프라인 전달 실패, 다른 담당자로 우회하지 않음 |
| M4 | 5 | 실패 ID 재시도, 성공할 때만 ID·통계·수신함 갱신 |
| M5 | 5 | 성공한 ID를 전역에서 중복 차단, 중복 SEND의 빈 trace |
| M6 | 5 | 모드 변경 뒤 수신함·성공 ID·통계·온라인 상태 보존 |
| 합계 | 100 | 기능 평가 |

C1, C4, C6, M1의 입력과 예상 출력은 공개합니다. 나머지 항목도 위 규칙만 검사하며 평가 요소와 실패 시 수정 힌트는 공개합니다. 힌트는 점검 방향이며 특정 행의 오류를 확정하는 진단은 아닙니다.

컴파일 실패 시 기능 항목은 **미검사**입니다. 컴파일 오류부터 수정하고 다시 제출합니다. 기능 100점이 두 패턴의 구조를 증명하지는 않습니다. 교수자는 인터페이스 위임, 연쇄 연결과 소유권, 선택·전달 책임 분리, 참조 수명 등을 별도로 소스 검토합니다. 구조 검토 점수는 기능 100점에 자동 합산하지 않습니다.

## 제출과 수정

1. 받은 과제 폴더의 `main.cpp`를 수정하고 저장합니다.
2. 컴파일한 뒤 세 예제의 출력과 비교합니다.
3. IDE 확장에서 선택한 과제와 현재 폴더를 확인하고 소스를 제출합니다. 실행 파일·빌드 폴더는 제출하지 않습니다.
4. 채점 완료 후 이번 제출의 결과를 확인합니다. 이전 최고점과 이번 점수는 다를 수 있습니다.
5. 실패한 평가 ID를 `HINTS.md`에서 찾아 수정하고 다시 제출합니다.

주차와 마감은 수업 서버의 일정을 따릅니다. 이어서 실습할 때는 기존 폴더를 열고, 다시 다운로드해야 한다면 기존 답안을 덮어쓰지 않도록 새 폴더에 받습니다.
