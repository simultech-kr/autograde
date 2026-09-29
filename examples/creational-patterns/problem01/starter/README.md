# 생성 패턴 실습 — 실습 결과 보고서 생성기

C++17로 출력 형식, 표시 테마, 보고서 일련번호를 분리한 프로그램을 완성합니다. 출력 형식에는 **Factory Method**, 테마에는 **Abstract Factory**, 프로세스에서 공유하는 일련번호에는 **Singleton**을 적용합니다.

이 안내문에는 주차 번호와 제출 마감이 지정되어 있지 않습니다. 별도 공지가 있으면 그 일정을 따릅니다.

## 작성 범위

`main.cpp`의 TODO 1~5 부분만 수정합니다. 제공된 클래스 이름, 함수 인터페이스, 고정된 `ReportCreator::render`와 `main`은 유지합니다. 이름이 생략된 매개변수에는 구현에 필요한 이름을 붙일 수 있습니다. 실행 파일이나 빌드 폴더는 제출하지 않습니다.

미완성 부분에는 빈 문자열, `nullptr`, `0` 반환 또는 `std::logic_error`가 들어 있습니다. 고정된 입력 처리는 `std::logic_error`를 잡아 `ERROR incomplete`를 출력합니다. 컴파일 성공이나 이 출력은 구현 완료를 뜻하지 않습니다. 완성한 프로그램의 정상 채점 입력에서는 `ERROR incomplete`가 나오면 안 됩니다.

| 구성 요소 | 역할과 구현할 동작 |
| --- | --- |
| `Report`, `TextReport`, `CsvReport` | 공통 인터페이스로 TEXT 또는 CSV 문자열을 생성합니다. |
| `ReportCreator::createReport`, `TextReportCreator`, `CsvReportCreator` | 구체 Creator가 생성할 `Report`를 결정합니다. |
| `ReportCreator::render` | `createReport`를 가상 호출하고 생성한 제품을 사용하는 제공된 공통 작업 흐름입니다. 수정하지 않습니다. |
| `TitlePart`, `PlainTitle`, `BracketTitle` | `format(name)`으로 제목을 테마에 맞게 만듭니다. |
| `ValuePart`, `PlainValue`, `BracketValue` | `format(value)`로 정수 값을 테마에 맞게 만듭니다. |
| `ThemeFactory`, `PlainThemeFactory`, `BracketThemeFactory` | `createTitle`, `createValue`로 같은 테마의 제품군을 만듭니다. |
| `Sequence` | `instance`, `next`, `count`로 하나의 일련번호 상태를 공유합니다. |
| `ReportService::make` | 형식·테마·값을 검증하고 Creator와 테마 제품군을 조합합니다. |

Factory Method에서는 `ReportCreator::render`가 구체 보고서 타입을 몰라도 동작해야 합니다. 형식 문자열에 따라 객체를 만드는 함수 하나만 작성하는 Simple Factory로 바꾸지 않습니다. `ReportService`에서 구체 Creator를 선택하는 분기는 사용할 수 있지만, 선택한 Creator의 가상 생성 메서드와 공통 흐름을 실제로 사용해야 합니다.

Abstract Factory에서는 한 요청의 제목 제품과 값 제품을 같은 `ThemeFactory`에서 얻습니다. Singleton은 비공개 생성자, 복사·이동 금지, 동일 인스턴스 반환을 유지합니다. 일반 전역 변수에 getter만 붙인 구조로 대체하지 않습니다.

## 입력

첫 줄은 명령 개수 `Q`이며 `1 ≤ Q ≤ 200`입니다. 이어서 명령이 `Q`줄 주어집니다.

| 명령 | 입력 예 | 의미 |
| --- | --- | --- |
| `PRINT FORMAT THEME NAME VALUE` | `PRINT TEXT PLAIN lab01 80` | 보고서를 생성합니다. 매 요청마다 새로운 `ReportService` 객체를 사용합니다. |
| `COUNT` | `COUNT` | 지금까지 성공한 보고서 생성 횟수를 조회합니다. |
| `SAME` | `SAME` | `Sequence::instance()`를 두 번 호출하여 같은 객체를 참조하는지 확인합니다. |

| 필드 | 입력 범위와 유효 조건 |
| --- | --- |
| `FORMAT` | 유효한 값은 대소문자를 구분하는 `TEXT`, `CSV`입니다. 그 밖의 값은 형식 오류입니다. |
| `THEME` | 유효한 값은 대소문자를 구분하는 `PLAIN`, `BRACKET`입니다. 그 밖의 값은 테마 오류입니다. |
| `NAME` | ASCII 영문자·숫자·밑줄로 구성된 1~20자 문자열입니다. `[A-Za-z0-9_]{1,20}`을 만족하는 입력만 주어집니다. |
| `VALUE` | 입력은 `-1000` 이상 `1000` 이하의 정수입니다. 보고서를 생성할 수 있는 값은 `0` 이상 `100` 이하입니다. |

`NAME`에는 공백이나 쉼표가 없으므로 CSV 따옴표 처리나 이스케이프 기능을 추가할 필요가 없습니다. 입력 파서 자체를 다시 작성하지 않습니다.

## 출력과 상태 규칙

모든 명령은 정확히 한 줄을 출력합니다. 설명 문구나 디버깅 출력을 추가하지 않습니다.

| 상황 | 출력 |
| --- | --- |
| 정상 `PRINT` | `<일련번호> <보고서 문자열>` |
| 잘못된 `FORMAT` | `ERROR format` |
| 잘못된 `THEME` | `ERROR theme` |
| 범위를 벗어난 `VALUE` | `ERROR value` |
| `COUNT` | `COUNT <현재 횟수>` |
| `SAME`에서 같은 객체를 참조함 | `SAME yes` |
| `SAME`에서 서로 다른 객체를 참조함 | `SAME no` — Singleton 구현이 잘못된 상태입니다. |

각 출력의 키워드 또는 일련번호 뒤에는 공백 한 칸을 둡니다. 아래 보고서 문자열에는 별도의 공백을 넣지 않습니다.

| 테마 | 제목 제품의 결과 | 값 제품의 결과 |
| --- | --- | --- |
| `PLAIN` | `NAME` 그대로 | 정수의 10진수 문자열 |
| `BRACKET` | `[NAME]` | `[정수의 10진수 문자열]` |

| 형식 | 보고서 문자열 | `NAME=lab01`, `VALUE=80` 예 |
| --- | --- | --- |
| `TEXT` | `title=<제목>;value=<값>` | PLAIN: `title=lab01;value=80` |
| `CSV` | `<제목>,<값>` | BRACKET: `[lab01],[80]` |

검증 순서는 **FORMAT → THEME → VALUE**입니다. 여러 필드가 잘못되었으면 가장 먼저 발견한 오류 하나만 출력합니다. 모든 검증을 통과한 `PRINT`만 일련번호를 한 번 증가시킵니다.

프로그램 시작 시 횟수는 `0`이고 첫 정상 보고서의 일련번호는 `1`입니다. 새 `ReportService`를 만들어도 같은 프로세스의 일련번호는 이어집니다. 오류, `COUNT`, `SAME`은 횟수를 바꾸지 않습니다. 새 프로세스로 실행하면 다시 `0`부터 시작합니다. 초기화 명령, 파일·네트워크 저장, 멀티스레드 처리는 실습 범위에 없습니다.

## 예제 1 — 형식과 테마 조합

입력:

```text
7
COUNT
SAME
PRINT TEXT PLAIN lab01 80
PRINT CSV PLAIN lab02 0
PRINT TEXT BRACKET lab03 100
PRINT CSV BRACKET lab04 45
COUNT
```

출력:

```text
COUNT 0
SAME yes
1 title=lab01;value=80
2 lab02,0
3 title=[lab03];value=[100]
4 [lab04],[45]
COUNT 4
```

## 예제 2 — 검증 순서와 오류 뒤의 일련번호

입력:

```text
9
PRINT JSON WRONG lab01 101
PRINT TEXT WRONG lab02 -1
PRINT CSV PLAIN lab03 101
COUNT
PRINT CSV BRACKET ok 0
SAME
PRINT TEXT PLAIN last 100
COUNT
PRINT TEXT PLAIN lab04 -1000
```

출력:

```text
ERROR format
ERROR theme
ERROR value
COUNT 0
1 [ok],[0]
SAME yes
2 title=last;value=100
COUNT 2
ERROR value
```

세 오류 모두 횟수를 증가시키지 않습니다. 형식과 테마가 함께 잘못된 첫 요청은 `ERROR format` 하나만 출력합니다.

## 구현 순서

1. `TextReport`와 `CsvReport`의 문자열 형식을 구현합니다. 대소문자, 세미콜론, 쉼표와 공백을 확인합니다.
2. 구체 Creator의 `createReport`를 구현합니다. 제공된 `ReportCreator::render`의 공통 흐름에서 가상 생성 메서드가 호출되는지 확인합니다.
3. 제목·값 제품 네 종류와 테마 Factory 두 종류를 구현합니다. PLAIN/BRACKET 제품을 요청 안에서 섞지 않습니다.
4. `Sequence`의 단일 인스턴스와 증가·조회 동작을 구현합니다. `COUNT`, `SAME`만 실행했을 때 횟수는 계속 `0`이어야 합니다.
5. `ReportService::make`에서 정해진 순서로 검증한 뒤 형식과 테마를 조합합니다. 오류 문구나 정상 결과 문자열을 반환하면 고정된 `main`이 출력합니다. 검증 실패 또는 보고서 생성 미완료 상태에서 `next`를 호출하지 않습니다.
6. 예제와 경계값 `0`, `100`, 범위 밖 값 `-1`, `101`을 확인합니다. 성공·오류·조회 명령을 섞어서 일련번호를 점검합니다.

항목별 점검 방법은 [HINTS.md](HINTS.md)에 있습니다. 완성 코드는 제공하지 않습니다.

## 빌드와 실행

`starter` 폴더를 작업 폴더로 사용합니다. 예제 입력을 `input.txt`에 저장하여 실행 결과와 위의 출력을 비교할 수 있습니다.

Linux / WSL / macOS의 C++17 컴파일러:

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o creational_patterns
./creational_patterns < input.txt
```

Windows의 Visual Studio **Developer Command Prompt**:

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:creational_patterns.exe
creational_patterns.exe < input.txt
```

CMake를 사용하는 IDE에서는 `starter` 폴더를 열고 `creational_patterns` 대상을 빌드·실행합니다. 제공된 설정은 C++17을 사용합니다. 명령줄 빌드는 다음과 같습니다.

```sh
cmake -S . -B build
cmake --build build --config Debug --target creational_patterns
```

실행 파일 위치는 선택한 CMake 생성기에 따라 다릅니다. 일반적인 단일 구성 빌드는 `build/creational_patterns`, Visual Studio Debug 구성은 `build\Debug\creational_patterns.exe`에 생성됩니다. IDE로 직접 프로젝트를 만들 때도 C++17을 선택하고 제공된 `main.cpp` 하나를 빌드합니다.

## 공개 평가 기준

자동 채점은 다음 기능 항목의 합계 100점입니다. 평가 기준은 해당 항목의 통과 여부와 관계없이 피드백에 표시되며, 디버깅 힌트는 실패한 항목에만 표시됩니다.

| ID | 점수 | 평가 기준 |
| --- | ---: | --- |
| F1 | 10 | TEXT 형식의 `title=...;value=...` 출력을 정확히 생성합니다. |
| F2 | 10 | CSV 형식의 `제목,값` 출력을 정확히 생성합니다. |
| A1 | 10 | PLAIN 테마의 제목과 값을 꾸밈없이 생성합니다. |
| A2 | 10 | BRACKET 테마의 제목과 값을 각각 대괄호로 감쌉니다. |
| A3 | 10 | 두 출력 형식과 두 테마의 조합을 서로 독립적으로 처리합니다. |
| S1 | 5 | `Sequence::instance()`의 두 호출이 같은 객체를 참조합니다. |
| S2 | 10 | 서로 다른 서비스 객체가 하나의 연속된 일련번호를 공유합니다. |
| E1 | 10 | 형식 오류를 검출하고 다른 오류보다 먼저 처리합니다. |
| E2 | 5 | 테마 오류를 검출하고 값 오류보다 먼저 처리합니다. |
| E3 | 10 | `0`과 `100`을 포함하여 허용 범위를 정확히 검사합니다. |
| E4 | 5 | 잘못된 요청이 성공 횟수와 다음 일련번호를 바꾸지 않습니다. |
| I1 | 5 | 생성·오류·조회 명령을 섞어도 출력과 상태가 일관됩니다. |
| 합계 | 100 | 기능 평가 |

공개 요구사항이 동작 계약의 전체이며, 모든 채점 입력이 예제로 공개되는 것은 아닙니다. 서버의 실제 실행 출력이 표시되는 것을 전제로 하지 말고 예제와 추가 입력을 로컬에서 비교합니다.

기능 점수 100점만으로 패턴 구조의 올바름이 증명되지는 않습니다. 소스 검토에서는 다음 구조를 별도로 확인합니다.

- Creator의 공통 작업 흐름이 가상 `createReport`를 호출하고 제품 인터페이스를 사용합니다.
- Abstract Factory가 한 요청에 필요한 제목·값 제품을 같은 테마 제품군으로 생성합니다.
- Singleton의 생성자가 비공개이고 복사·이동이 금지되어 있으며, 접근할 때마다 동일한 인스턴스를 반환합니다. 일반 전역 변수와 getter만으로 대신하지 않습니다.

제출 대상은 TODO를 완성한 `main.cpp`입니다.
