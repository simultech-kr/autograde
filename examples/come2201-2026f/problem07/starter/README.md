# COME2201 · Week11 · Problem07
## 캠퍼스 지도 — Bridge, Flyweight

마감: **2026-11-11 13:50 (KST)**

## 문제 상황과 학습 목표

캠퍼스 지도에는 강의실·출입구·시설을 나타내는 마커가 많이 표시됩니다. 같은 색상 마커가 늘어날 때 색상 정보를 담은 객체를 매번 생성하면 동일한 상태를 반복 저장하게 됩니다. 반대로 좌표까지 공유하면 마커 하나를 옮길 때 다른 마커가 같이 움직이는 문제가 생깁니다. 화면 표시용 `TEXT`와 기록용 `COMPACT` 출력도 마커 자체의 관리 기능과 분리해야 합니다.

목표는 Flyweight의 **공유해도 되는 불변 상태**와 **각 객체가 따로 가져야 하는 상태**를 구별하고, Bridge로 마커의 기능과 출력 알고리즘을 독립적으로 확장하는 것입니다. 실제 공유 객체를 사용하면서도 각 마커의 위치와 렌더러 선택은 독립적으로 유지해야 합니다.

`main.cpp`의 `StyleFactory`, `Renderer` 구현체, `MapMarker` TODO를 완성합니다. `MarkerStyle`은 불변 공유 상태(색상), 마커의 id/좌표는 외부 상태입니다. `MapMarker`가 `Renderer` 추상 인터페이스에 렌더링을 위임하도록 합니다. 제출 파일은 **main.cpp** 하나입니다.

## 패턴별 책임과 구현 순서

| 구성 요소 | 맡아야 할 책임 |
|---|---|
| `MarkerStyle` | 여러 마커가 공유하는 불변 색상 정보 |
| `StyleFactory` | 색상별 객체를 재사용하고 캐시의 실제 스타일 수 제공 |
| `Renderer`와 구현체 | 마커 정보를 받아 TEXT 또는 COMPACT 문자열 생성 |
| `MapMarker` | id·좌표·스타일 참조·렌더러 참조를 가지고 추상 Renderer에 그리기 위임 |
| 명령 처리부 | 중복·좌표 검사 후 생성, 사용자가 지정한 렌더러 선택 |

1. `StyleFactory::get()`의 처음 요청과 재요청을 구분합니다. 같은 색상의 재요청은 동일한 불변 객체를 얻어야 합니다.
2. `count()`는 마커 개수가 아니라 실제 공유 스타일 개수를 반환하도록 구현합니다.
3. 두 Renderer가 만드는 문자열을 각각 확인합니다. 좌표와 색상 상태를 Renderer가 소유하거나 변경할 필요는 없습니다.
4. `MapMarker::move()`는 자신의 좌표만 갱신하고, `setRenderer()`는 사용할 구현을 바꾸도록 합니다.
5. `draw()`에서는 구체 클래스별 출력 분기를 다시 만들지 말고 Renderer 인터페이스로 위임합니다. 아직 준비되지 않은 참조를 역참조하지 않도록 주의합니다.
6. 같은 색상의 마커 두 개를 만든 뒤 하나만 이동시키고, 같은 마커를 두 모드로 그려 상태와 표현이 분리되는지 확인합니다.

## 입력과 출력

첫 줄 N(1~100), 다음 N줄 명령. id와 color는 공백 없는 영문·숫자·밑줄 토큰(1~30자), 좌표 입력은 -10000~10000의 정수입니다.

| 명령 | 출력 및 규칙 |
|---|---|
| ADD id color x y | 좌표가 모두 -1000~1000이면 생성 후 OK. id 중복은 ERROR duplicate, 좌표 오류는 ERROR position |
| MOVE id x y | 이동 후 OK. 없는 id는 ERROR missing, 범위 밖은 ERROR position |
| DRAW id TEXT | TEXT id color x,y |
| DRAW id COMPACT | COMPACT[id;color;x;y] |
| DRAW id 다른모드 | ERROR renderer. id가 없으면 모드에 앞서 ERROR missing |
| COUNT | STYLES n — 성공적으로 생성된 마커가 사용하는 서로 다른 색상 수 |

ADD는 id 중복을 먼저 검사합니다. 실패한 ADD는 스타일 캐시에도 영향을 주면 안 됩니다. MOVE는 존재 여부를 먼저 검사하고 실패 시 좌표를 유지합니다. 색상은 변경하지 않고 캐시는 실행 중 유지됩니다. DRAW는 마커나 스타일을 복제하지 않습니다. 모든 명령은 한 줄씩 출력합니다.

좌표의 허용 범위는 x와 y **각각 -1000 이상 1000 이하**이며 양 끝도 유효합니다. 입력으로 주어지는 범위와 유효 좌표 범위는 다릅니다. `COUNT`는 시작할 때 `STYLES 0`이고, 마커 이동이나 렌더러 변경으로 증가하지 않습니다. 삭제·색상 변경 명령은 없습니다.

TEXT는 `TEXT`, id, color, `x,y` 사이만 한 칸씩 띄웁니다. COMPACT는 대괄호와 세미콜론을 쓰며 내부에 공백이 없습니다. 알 수 없는 모드는 `ERROR renderer`이고 기존 좌표·스타일을 바꾸지 않습니다. 명령 문법은 정상적으로 제공되므로 오류 문구를 임의로 추가하지 마세요.

## 공개 예제 1 — 같은 스타일, 다른 위치와 출력 방식

입력:
```text
8
ADD gate blue 1 2
ADD lab blue 5 6
COUNT
DRAW gate TEXT
DRAW lab COMPACT
MOVE gate -2 3
DRAW gate COMPACT
COUNT
```
출력:
```text
OK
OK
STYLES 1
TEXT gate blue 1,2
COMPACT[lab;blue;5;6]
OK
COMPACT[gate;blue;-2;3]
STYLES 1
```

`gate`와 `lab`은 blue 스타일을 공유하지만 좌표가 다릅니다. `gate`의 이동과 출력 방식 변경은 스타일 개수를 늘리지 않습니다.

## 공개 예제 2 — 생성·이동 실패가 남기는 상태

입력:

```text
12
COUNT
ADD pond green -1000 1000
ADD tower amber 1002 0
COUNT
ADD tower green 3 4
MOVE pond -1001 10
DRAW pond TEXT
DRAW tower SVG
DRAW absent SVG
ADD tower red 0 0
DRAW tower COMPACT
COUNT
```

출력:

```text
STYLES 0
OK
ERROR position
STYLES 1
OK
ERROR position
TEXT pond green -1000,1000
ERROR renderer
ERROR missing
ERROR duplicate
COMPACT[tower;green;3;4]
STYLES 1
```

실패한 amber 마커와 중복 id의 red 마커는 캐시에 색상을 남기지 않습니다. `pond`의 이동이 실패했으므로 처음의 경계 좌표가 그대로 출력됩니다. 없는 마커를 그릴 때는 모드보다 존재 여부를 먼저 판단합니다.

## 빌드와 실행

**Visual Studio 2022/2026:** C++ 데스크톱 개발·CMake 도구가 설치된 환경에서 **파일 → 열기 → 폴더**로 `main.cpp`와 `CMakeLists.txt`가 있는 폴더를 엽니다. CMake 구성 후 `lab` 대상을 빌드·실행하고 콘솔에 입력 블록 전체를 붙여 넣습니다. 같은 폴더의 **Developer Command Prompt**에서는 다음처럼 실행할 수도 있습니다. `<`는 일반 명령 프롬프트 문법입니다.

```bat
cl /nologo /std:c++17 /EHsc /utf-8 main.cpp /Fe:lab.exe
lab.exe < sample.in
```

**Linux/macOS/WSL2:** C++17 컴파일러가 설치된 터미널에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra main.cpp -o lab
./lab < sample.in
```

`sample.in`에는 예제 하나의 입력 블록만 저장합니다. 첫 줄 N도 포함하고, 설명·출력·두 번째 예제는 섞지 마세요. 시작 코드는 컴파일되지만 TODO 동작은 미완성입니다. `TODO` 출력이나 잘못된 `STYLES` 값은 구현 전 상태일 수 있습니다.

## 제출과 평가

답안 구현물은 **main.cpp 한 파일**입니다. 필요한 클래스를 이 파일에 모두 구현하고 제공된 명령 처리 계약을 유지하세요. README와 CMake 설정은 보조 자료이며, 별도 답안 소스나 실행 파일·빌드 폴더를 추가하지 않습니다. 모두 저장한 뒤 확장의 제출 확인창에서 선택 과제와 파일 목록을 확인합니다. 화면의 미저장 내용은 제출되지 않습니다.

자동채점 **100점**은 출력 형식·좌표·오류 후 상태·스타일 개수 등 공개 계약의 동작을 평가합니다. 위 예제만이 평가 입력의 전부는 아닙니다. **개수만 맞춘다고 실제 Flyweight를 구현한 것으로 인정되지는 않습니다.** 실제 객체 공유, Bridge 위임, 불변성과 소유권은 교수자가 별도로 코드와 설명을 검토합니다. 두 평가를 구분하세요.

## 자기점검

- [ ] Factory가 같은 색상 요청에 실제로 같은 불변 객체를 반환한다.
- [ ] 마커의 위치가 공유 스타일 객체 안에 들어 있지 않다.
- [ ] MapMarker가 구체 Renderer 타입을 분기하지 않고 추상 인터페이스에 위임한다.
- [ ] 같은 마커의 Renderer를 교체할 수 있고 두 출력 알고리즘이 분리되어 있다.
- [ ] shared_ptr 수명과 intrinsic/extrinsic state 구분을 설명할 수 있다.
- [ ] 실패한 ADD가 캐시에 항목을 만들지 않고 실패한 MOVE가 좌표를 바꾸지 않는다.
- [ ] 출력 모드가 달라도 같은 마커의 id·색상·좌표는 유지된다.

공유 객체의 주소 확인은 로컬 디버거에서 하되 채점 출력에 주소나 디버그 문구를 넣지 마세요.

## 자주 묻는 질문

**`COUNT`는 현재 마커 수인가요?** 아닙니다. 성공적으로 생성된 마커가 사용하는 서로 다른 색상 객체의 수입니다. 같은 색상 마커를 추가하면 증가하지 않습니다.

**좌표도 Style에 넣어 공유하면 안 되나요?** 좌표는 각 마커의 외부 상태입니다. 공통 색상을 사용하는 마커가 독립적으로 움직여야 합니다.

**모드 선택을 위한 모든 if문을 없애야 하나요?** 명령 처리부의 TEXT/COMPACT 선택은 제공된 흐름입니다. `MapMarker`가 구체 Renderer를 판별해 출력 알고리즘을 직접 구현하지 않는 것이 핵심입니다.

**`DRAW`할 때마다 같은 색상의 새 Style을 만들어도 출력은 같지 않나요?** 출력만 같을 수 있지만 실제 공유라는 구조 요구를 만족하지 못합니다. 캐시가 동일 객체를 반환하는지 별도로 확인하세요.
