# COME2201 · Week11 · Problem07
## 캠퍼스 지도 — Bridge, Flyweight
마감: **2026-11-11 13:50 (KST)**

캠퍼스 지도에는 같은 색상의 마커가 수천 개 표시됩니다. 마커별 좌표는 다르지만 색상 객체를 매번 만들 필요는 없습니다. 또한 지도 마커를 수정하지 않고 화면용 TEXT와 로그용 COMPACT 출력 방식을 교체해야 합니다.

`main.cpp`의 `StyleFactory`, `Renderer` 구현체, `MapMarker` TODO를 완성합니다. `MarkerStyle`은 불변 공유 상태(색상), 마커의 id/좌표는 외부 상태입니다. `MapMarker`가 `Renderer` 추상 인터페이스에 렌더링을 위임하도록 합니다. 제출 파일은 **main.cpp** 하나입니다.

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

## 공개 예제
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

## 실행 및 평가
VS2022/2026에서 이 폴더를 열고 CMake 빌드, 또는 Linux/macOS/WSL2에서 `c++ -std=c++17 main.cpp -o lab`. 시작 코드는 빌드되지만 TODO를 완성해야 동작합니다. 자동채점 100점은 출력·좌표·경계·공유 스타일 개수 계약을 검사합니다. **개수만 맞춘다고 실제 Flyweight를 구현한 것으로 인정되지는 않습니다.** 교수자가 아래 구조를 별도 검토합니다.

- [ ] Factory가 같은 색상 요청에 실제로 같은 불변 객체를 반환한다.
- [ ] 마커의 위치가 공유 스타일 객체 안에 들어 있지 않다.
- [ ] MapMarker가 구체 Renderer 타입을 분기하지 않고 추상 인터페이스에 위임한다.
- [ ] 같은 마커의 Renderer를 교체할 수 있고 두 출력 알고리즘이 분리되어 있다.
- [ ] shared_ptr 수명과 intrinsic/extrinsic state 구분을 설명할 수 있다.

공유 객체의 주소 확인은 로컬 디버거에서 하되 채점 출력에 주소나 디버그 문구를 넣지 마세요.
