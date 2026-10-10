# Command Adapter Facade 실습

C++17로 강의실 조명과 스피커를 제어하는 프로그램을 완성합니다. 서로 다른 장비 API를 공통 인터페이스로 바꾸는 **Adapter**, 요청과 되돌리기를 객체로 표현하는 **Command**, 장비 제어 절차를 간단한 기능으로 제공하는 **Facade**를 함께 사용합니다. Adaptor는 Adapter의 다른 표기이며, 이 실습에서는 Adapter로 통일합니다.

주차와 마감은 이 자료에 지정하지 않았습니다. 수업 서버에 표시된 일정을 따릅니다. 실제 장비, 인터넷 연결, 외부 라이브러리는 필요하지 않습니다.

## 문제 상황

기존 강의실에는 조명과 스피커가 설치되어 있습니다. 조명은 `0~10` 단계, 스피커는 `0~20` 단위로 작동하며 함수 이름도 다릅니다. 사용자는 두 장비 모두 `0~100` 수준으로 조절하고 싶습니다. 또한 학습 장면이나 영상 장면을 한 번에 적용하고, 마지막 작업을 취소할 수 있어야 합니다.

입력 처리 코드에서 장비별 단위 변환, 이전 값 보관, 여러 장비 설정을 모두 처리하지 마세요. 다음 책임으로 분리합니다.

| 패턴 | 이 문제에서 해결할 일 | 핵심 클래스 |
| --- | --- | --- |
| Adapter | 기존 장비 API와 단위를 공통 `LevelDevice` 인터페이스로 변환 | `LampAdapter`, `SpeakerAdapter` |
| Command | 설정할 대상과 값, 실행 직전 값을 보관하고 실행과 취소 제공 | `Command`, `SetLevelCommand`, `SceneCommand` |
| Facade | 장비 선택·입력 검증·장면 구성을 감추고 간단한 요청 제공 | `RoomFacade` |

Adapter는 장비 하나의 인터페이스를 변환합니다. Facade는 여러 객체를 사용해야 하는 절차를 간단하게 만듭니다. Command는 그 절차의 실행과 취소를 기록 가능한 객체로 표현합니다. 같은 기능을 세 클래스에 중복 구현하지 않습니다.

## 제공 파일과 수정 범위

| 파일 | 용도 |
| --- | --- |
| `main.cpp` | 제공된 클래스와 TODO 구현, 고정 입력 처리 |
| `README.md` | 동작 계약, 예제, 구현 순서, 실행·제출 안내 |
| `HINTS.md` | 평가 ID별 단계적 힌트와 점검 순서 |
| `CMakeLists.txt` | C++17 빌드 설정 |

`main.cpp`의 TODO 1~5를 완성합니다. 제공된 클래스 이름, public 함수 선언, `LegacyLamp`, `LegacySpeaker`, `main`을 유지합니다. 필요한 private 멤버와 보조 함수는 추가할 수 있습니다. 제공된 필드 이름이 채점의 전제는 아니지만, 장비를 복사하거나 참조 수명을 깨뜨리면 안 됩니다.

미완성 함수는 `std::logic_error`를 발생시킵니다. 제공된 `main`은 이를 잡아 `ERROR incomplete` 한 줄을 출력합니다. 시작 코드는 컴파일될 수 있지만, 컴파일 성공은 과제 완성을 뜻하지 않습니다. 완성 후 아래 입력 계약의 명령에서 `ERROR incomplete`가 나오면 안 됩니다.

## 클래스별 계약

| 클래스 | 해야 할 일 |
| --- | --- |
| `LegacyLamp` | 제공된 `writeStep`와 `readStep`으로 실제 조명 단계 보관. 수정하지 않음 |
| `LegacySpeaker` | 제공된 `changeVolume`과 `volume`으로 실제 스피커 단위 보관. 수정하지 않음 |
| `LevelDevice` | `setLevel(int)`와 `level() const`를 제공하는 공통 추상 인터페이스 |
| `LampAdapter` | 조명에 대한 쓰기와 읽기 양방향 단위 변환 |
| `SpeakerAdapter` | 스피커에 대한 쓰기와 읽기 양방향 단위 변환 |
| `SetLevelCommand` | `execute()` 때 이전 값을 읽고 새 값을 설정. `undo()` 때 저장한 값 복원 |
| `SceneCommand` | 두 설정 명령을 조명→스피커 순서로 실행. 취소는 역순. 하나의 작업으로 취급 |
| `CommandHistory` | 명령 실행 후 이력에 보관. 가장 최근 명령을 취소하고 이력에서 제거 |
| `RoomFacade` | 검증·장비 선택·장면 조합을 담당하며 모든 변경을 명령 이력에 위임 |

장비 객체는 프로그램 실행 동안 하나씩 존재합니다. Adapter는 그 장비를 참조합니다. Command는 공통 `LevelDevice`를 참조하고 구체 장비 종류를 판단하지 않습니다. 소유하는 명령은 `std::unique_ptr<Command>`로 관리하며 이력에 넣을 때 `std::move`를 사용할 수 있습니다.

## 장비 단위 변환

외부에 보이는 값은 모두 정수 백분율입니다. 허용 값은 **0 이상 100 이하이며 10의 배수**입니다. 두 장비에 같은 허용 범위를 적용합니다.

| 장비 이름 | 내부 장비 | 설정할 때 | 조회할 때 | 외부 값 60의 내부 값 |
| --- | --- | --- | --- | ---: |
| `LIGHT` | `LegacyLamp` | 백분율 ÷ 10 | 단계 × 10 | 6 |
| `SOUND` | `LegacySpeaker` | 백분율 ÷ 5 | 단위 × 5 | 12 |

스피커 하드웨어가 5 단위의 백분율을 표현할 수 있어도, 이 프로그램의 공개 입력 규칙은 10의 배수만 허용합니다. `SET SOUND 35`는 오류입니다. 값을 반올림하거나 범위 안으로 보정하지 않습니다.

Adapter에 백분율을 별도로 저장하고 기존 장비를 갱신하지 않는 방식은 허용하지 않습니다. `level()`은 기존 장비에서 읽은 값을 변환해야 합니다. `RAW` 명령으로 실제 장비 값도 확인합니다.

## 입력 명령

첫 줄에는 명령 개수 `Q`가 주어지며 `1 ≤ Q ≤ 200`입니다. 이후 `Q`줄에 다음 명령이 주어집니다. 키워드는 대소문자를 구분합니다.

| 명령 | 의미 |
| --- | --- |
| `SET DEVICE LEVEL` | 지정 장비의 값을 변경 |
| `SCENE NAME` | 두 장비에 장면을 적용 |
| `UNDO` | 가장 최근의 성공한 변경 하나를 취소 |
| `STATUS` | 두 장비의 백분율 조회 |
| `RAW` | 두 기존 장비의 내부 단위 조회 |
| `HISTORY` | 현재 취소할 수 있는 작업 수 조회 |

명령어와 인수 개수, 정수 문법은 올바르게 주어집니다. `LEVEL` 입력 범위는 `-1000~1000`이며 그 안에서 유효 범위를 검사합니다. `DEVICE`와 `NAME`은 공백 없는 ASCII 문자열이며 미지원 이름도 입력됩니다. 문자열 파서, 잘못된 정수 문법, 알 수 없는 명령어 자체의 처리는 실습 범위가 아닙니다.

### 장면 설정

| 이름 | 조명 백분율 | 스피커 백분율 |
| --- | ---: | ---: |
| `STUDY` | 80 | 20 |
| `MOVIE` | 20 | 60 |
| `OFF` | 0 | 0 |

`RoomFacade::scene`은 장면 이름으로 두 값을 결정하고, `SceneCommand` 하나를 실행합니다. 내부 설정 두 개를 각각 이력에 넣지 않습니다. `OFF`는 두 값을 0으로 설정하는 작업이지, 이력을 지우는 명령이 아닙니다.

## 출력과 상태 규칙

명령 하나당 정확히 한 줄을 출력합니다. 디버깅 메시지, 입력 안내, 빈 줄은 추가하지 않습니다. 오류나 성공 문자열은 Facade에서 반환하고 제공된 `main`이 출력합니다.

| 상황 | 출력 |
| --- | --- |
| 정상 `SET` | `OK SET` |
| 정상 `SCENE` | `OK SCENE` |
| 취소할 작업이 있는 `UNDO` | `OK UNDO` |
| 취소할 작업이 없는 `UNDO` | `EMPTY` |
| 지원하지 않는 장비 | `ERROR device` |
| 범위 밖 또는 10의 배수가 아닌 값 | `ERROR level` |
| 지원하지 않는 장면 | `ERROR scene` |
| `STATUS` | `STATE light=<백분율> sound=<백분율>` |
| `RAW` | `RAW lamp=<단계> speaker=<단위>` |
| `HISTORY` | `HISTORY <개수>` |

공백은 표에 나온 위치에 한 칸씩 사용합니다. 정수에는 소수점이나 `%`를 붙이지 않습니다.

1. 시작 시 두 장비 값과 이력 개수는 모두 0입니다. 새 프로세스로 실행하면 다시 0입니다.
2. `SET`은 **장비 이름 → 값** 순서로 검사합니다. `SET TV 135`는 `ERROR device` 하나만 출력합니다.
3. 검증 오류는 두 장비와 이력을 바꾸지 않습니다. 검증을 완료하기 전에 명령을 실행하지 않습니다.
4. 성공한 `SET`과 `SCENE`은 각각 이력을 정확히 한 개 늘립니다. **현재 값과 같은 값으로 설정해도 이력을 남깁니다.**
5. `UNDO`는 가장 최근 작업부터 취소합니다. 취소한 작업은 이력에서 제거되며 다시 실행하는 REDO 기능은 없습니다.
6. 이전 값은 명령의 `execute()` 시점에 저장합니다. 취소 시 0이나 반대 장면으로 바꾸는 것이 아니라, 실행 직전의 실제 값으로 복원합니다.
7. 장면 취소는 두 장비를 함께 원래 값으로 돌립니다. 그 전의 이력은 유지합니다.
8. `STATUS`, `RAW`, `HISTORY`, 빈 이력의 `UNDO`는 상태와 이력을 바꾸지 않습니다.

파일 저장, 네트워크, 스레드, 장비 고장, 예외 발생 중의 트랜잭션 복구는 실습 범위가 아닙니다. 완성된 코드에서 유효한 장비 설정은 실패하지 않는다고 가정합니다.

## 예제 1 장비 변환과 단일 취소

입력:

```text
10
STATUS
SET LIGHT 80
SET SOUND 30
STATUS
RAW
HISTORY
UNDO
STATUS
RAW
HISTORY
```

출력:

```text
STATE light=0 sound=0
OK SET
OK SET
STATE light=80 sound=30
RAW lamp=8 speaker=6
HISTORY 2
OK UNDO
STATE light=80 sound=0
RAW lamp=8 speaker=0
HISTORY 1
```

스피커 30은 내부 단위 6입니다. 마지막 작업은 스피커 설정이므로 취소해도 조명 80은 유지됩니다.

## 예제 2 장면 하나를 작업 하나로 취소

입력:

```text
14
SET LIGHT 50
SET SOUND 40
SCENE MOVIE
STATUS
RAW
HISTORY
UNDO
STATUS
HISTORY
SCENE OFF
STATUS
UNDO
STATUS
HISTORY
```

출력:

```text
OK SET
OK SET
OK SCENE
STATE light=20 sound=60
RAW lamp=2 speaker=12
HISTORY 3
OK UNDO
STATE light=50 sound=40
HISTORY 2
OK SCENE
STATE light=0 sound=0
OK UNDO
STATE light=50 sound=40
HISTORY 2
```

`MOVIE` 취소는 `STUDY`로 이동하는 동작이 아닙니다. 바로 전의 50과 40으로 복원합니다. `OFF`도 취소할 수 있습니다.

## 예제 3 오류와 같은 값 설정

입력:

```text
17
UNDO
SET TV 135
SET LIGHT -10
SET SOUND 35
SCENE PARTY
HISTORY
SET LIGHT 0
HISTORY
UNDO
UNDO
SCENE STUDY
STATUS
SCENE STUDY
HISTORY
UNDO
STATUS
HISTORY
```

출력:

```text
EMPTY
ERROR device
ERROR level
ERROR level
ERROR scene
HISTORY 0
OK SET
HISTORY 1
OK UNDO
EMPTY
OK SCENE
STATE light=80 sound=20
OK SCENE
HISTORY 2
OK UNDO
STATE light=80 sound=20
HISTORY 1
```

마지막 취소는 두 번째 `STUDY`만 제거합니다. 첫 번째 `STUDY`의 상태와 이력은 남습니다.

## 구현 순서와 기본 힌트

1. **TODO 1 Adapter:** 쓰기는 백분율을 내부 단위로 바꾸고, 읽기는 내부 단위를 백분율로 바꿉니다. 두 Adapter는 같은 `LevelDevice`를 구현하지만 변환 비율과 호출하는 기존 API가 다릅니다.
2. **TODO 2 단일 Command:** `execute()`에서 먼저 `level()`을 읽어 저장한 뒤 목표 값을 씁니다. `undo()`는 저장한 값을 씁니다. 이전 값을 저장하기 전에 새 값을 쓰면 취소가 동작하지 않습니다.
3. **TODO 3 장면 Command:** 두 개의 `SetLevelCommand`를 멤버로 구성합니다. 실행은 조명→스피커, 취소는 스피커→조명 순서로 위임합니다. 기존 장비 API를 여기서 직접 호출하지 않습니다.
4. **TODO 4 이력:** `run`은 실행을 완료한 명령의 소유권을 이력에 옮깁니다. `undoLast`는 비었는지 먼저 확인하고 마지막 명령을 취소한 다음 제거합니다. 제거 전에 객체가 살아 있어야 합니다.
5. **TODO 5 Facade:** 먼저 입력을 검사합니다. `SET`은 하나의 설정 명령, `SCENE`은 하나의 장면 명령을 구성해 이력에 전달합니다. Facade가 별도의 이전 값 목록을 관리하지 않습니다.
6. **통합 확인:** 예제 순서대로 비교한 뒤 0, 100, -10, 110, 35와 같은 값으로 범위를 확인합니다. 한 기능이 통과해도 장면·취소·오류를 섞으면 상태 관리 오류가 드러날 수 있습니다.

여러 TODO가 남아 있으면 하나를 고쳐도 점수가 바로 오르지 않을 수 있습니다. 예를 들어 Adapter가 완성되어도 Facade와 명령 실행이 미완성이면 `SET` 경로가 끝까지 실행되지 않습니다. 디버거로 각 단계의 호출과 값을 확인합니다. 세부 힌트는 [HINTS.md](HINTS.md)에 평가 ID 순서로 정리되어 있습니다.

## 빌드와 실행

Linux, WSL2, macOS에서는 C++17 컴파일러를 사용합니다. 예제 입력 하나를 `input.txt`에 저장하고 다음과 같이 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o room_control
./room_control < input.txt
```

Windows Visual Studio의 **Developer Command Prompt**에서는 다음과 같이 실행합니다. 일반 PowerShell에는 `cl` 경로가 없을 수 있으므로 개발자 명령 프롬프트를 사용합니다.

```bat
cl /nologo /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:room_control.exe
room_control.exe < input.txt
```

CMake를 사용하는 IDE는 받은 폴더를 열고 `room_control` 대상을 빌드합니다.

```sh
cmake -S . -B build
cmake --build build --config Debug --target room_control
```

일반적인 단일 구성 빌드의 실행 파일은 `build/room_control`, Visual Studio Debug 구성은 `build\Debug\room_control.exe`입니다. 직접 만든 콘솔 프로젝트에서는 C++17을 선택하고 제공된 `main.cpp` 하나를 빌드합니다.

## 평가 항목과 결과 해석

기능 자동채점은 총 100점입니다. 각 항목은 통과하면 해당 점수 전부, 실패하면 0점입니다. 한 항목 안의 일부 명령만 맞은 경우 별도 부분 점수는 없습니다. 매 테스트는 새 프로세스에서 시작합니다.

| ID | 점수 | 평가 요소 |
| --- | ---: | --- |
| A1 | 10 | 조명 백분율과 내부 단계의 양방향 변환 |
| A2 | 10 | 스피커 백분율과 내부 단위의 양방향 변환 |
| A3 | 5 | 두 장비에서 0과 100을 포함한 경계 처리 |
| C1 | 10 | 단일 설정을 실행 직전 값으로 복원 |
| C2 | 10 | 여러 명령의 마지막 실행부터 취소하고 이력 제거 |
| C3 | 5 | 빈 이력에서 안전하게 `EMPTY` 반환 |
| C4 | 5 | 같은 값의 설정도 하나의 성공 이력으로 기록 |
| F1 | 10 | STUDY, MOVIE, OFF의 장비 값 구성 |
| F2 | 10 | 장면 하나를 이력 하나로 기록하고 두 장비를 함께 복원 |
| F3 | 5 | 장면과 개별 설정을 섞은 뒤 이전 상태 복원 |
| E1 | 5 | 미지원 장비 검출과 장비 우선 검증 |
| E2 | 5 | 범위 밖·10의 배수가 아닌 값 거절과 상태 보존 |
| E3 | 5 | 미지원 장면 거절과 상태 보존 |
| I1 | 5 | 조회·장면·설정·취소가 섞여도 상태와 이력 일관성 유지 |
| 합계 | 100 | 기능 평가 |

채점 결과에는 `평가 요소`와 항목별 점수가 표시됩니다. 실행되었으나 실패한 항목에는 점검할 클래스·상태·순서를 설명하는 수정 힌트가 붙습니다. A1, A2, C1, F1은 공개 테스트이며 그 밖의 테스트 입력은 비공개입니다. 비공개 테스트도 위 요구사항만 검사하며, 평가 요소와 힌트는 공개합니다.

컴파일 오류가 있으면 먼저 컴파일 안내를 해결합니다. 이 경우 기능 항목은 **미검사**이며, 개별 구현이 틀렸다는 뜻이 아닙니다. 실행 실패용 세부 힌트 대신 선행 컴파일 문제 안내가 표시될 수 있습니다. 자동채점은 실패 현상과 점검 방향을 제공하지만 소스 원인을 확정하지 않습니다.

기능 100점만으로 디자인 패턴의 구조가 증명되지는 않습니다. 교수자는 다음을 별도로 확인할 수 있습니다.

- Adapter가 기존 장비를 감싸고 공통 인터페이스와 단위를 변환하는가.
- Command가 대상·요청·이전 상태를 보관하고 실행과 취소를 제공하는가.
- 이력이 구체 장비를 몰라도 `Command` 인터페이스로 동작하는가.
- Facade가 단순한 호출을 제공하며 내부 장비 제어를 명령 객체에 위임하는가.
- 고정 입력 처리와 기존 장비를 우회하거나 예상 출력을 하드코딩하지 않았는가.

## 제출과 재시도

1. 받은 과제 폴더의 `main.cpp`를 수정하고 저장합니다.
2. 로컬에서 컴파일하고 세 예제의 출력과 비교합니다.
3. IDE 확장에서 선택한 과제와 현재 폴더가 일치하는지 확인한 뒤 제출합니다. 실행 파일과 빌드 폴더는 제출 대상이 아닙니다.
4. 제출 접수와 채점 완료를 구분합니다. 이번 제출의 처리 완료 후 상세 결과를 엽니다.
5. 실패 항목의 ID를 `HINTS.md`에서 찾아 수정하고 다시 제출합니다. 이전 최고점과 이번 제출 점수를 구분해서 확인합니다.

이전에 받은 폴더로 이어서 실습할 수 있습니다. 파일을 다시 받는 경우 기존 답안을 덮어쓰지 않도록 새 폴더를 사용합니다. 다른 과제의 `main.cpp`를 제출하지 않습니다.
