# COME2201 · Week10 · Problem06
## 보고서 템플릿 제작소 — Builder, Prototype
마감: **2026-11-04 13:50 (Asia/Seoul, KST)**

컨설팅 팀은 제목·페이지 수를 정해 보고서를 만들고 기존 보고서를 복제해 고객별 섹션을 추가합니다. 복제본 수정이 원본까지 바꾸거나 유효하지 않은 페이지 수를 허용하면 안 됩니다.

`main.cpp`의 `Report`, `ReportBuilder` TODO를 완성하세요. Builder의 `withTitle`, `withPages`, `build`와 Prototype의 `clone`을 사용합니다. 제공된 명령 해석과 출력 흐름은 유지하세요. 모든 구현을 **main.cpp 한 파일**에 제출합니다.

## 입력과 출력
첫 줄은 명령 수 N(1~100), 다음 N줄은 아래 명령입니다. 식별자와 문자열은 공백 없는 영문·숫자·밑줄 토큰(1~30자)입니다. 문법과 명령어는 정상적으로 제공되며 범위 오류와 없는/중복 식별자는 처리해야 합니다.

| 명령 | 동작 및 출력 |
|---|---|
| BUILD id title pages | 1~500페이지 보고서 생성. 성공 OK, 같은 id는 ERROR duplicate, 범위 밖은 ERROR pages |
| CLONE source target | 제목·페이지·섹션을 독립 복제. 원본 없으면 ERROR missing, 대상 중복은 ERROR duplicate |
| TITLE id title | 제목 변경. 성공 OK, 없으면 ERROR missing |
| ADD id section | 섹션 뒤에 추가. 중복 섹션도 허용. 성공 OK, 없으면 ERROR missing |
| SHOW id | id\|제목\|페이지\|쉼표로 연결한 섹션. 섹션이 없으면 -, id가 없으면 ERROR missing |

BUILD는 중복 id 검사를 페이지 검사보다 먼저 합니다. CLONE은 원본 존재를 먼저 검사합니다. 실패 명령은 상태를 바꾸지 않습니다. 명령당 한 줄을 출력하고 안내 문구는 출력하지 않습니다.

## 공개 예제
입력:
```text
8
BUILD r1 Weekly 3
ADD r1 Intro
CLONE r1 r2
TITLE r2 Final
ADD r2 Result
SHOW r1
SHOW r2
SHOW missing
```
출력:
```text
OK
OK
OK
OK
OK
r1|Weekly|3|Intro
r2|Final|3|Intro,Result
ERROR missing
```

## 구현·검증 안내
Windows Visual Studio 2022/2026에서는 **폴더 열기**로 이 디렉터리를 열고 CMake 대상을 빌드합니다. Linux/macOS/WSL2에서는 `c++ -std=c++17 main.cpp -o lab`로 빌드합니다. 프로그램에 예제 입력을 표준 입력으로 전달하세요. 시작 코드는 컴파일되지만 TODO 때문에 정답 동작을 하지 않습니다.

자동채점은 표준 입력/출력 계약을 검증하며 총 100점입니다. 비공개 검사는 페이지 경계, 중복, 없는 식별자, 연쇄 복제, 섹션 독립성을 포함합니다. 출력만으로 디자인 패턴 준수를 증명할 수 없으므로 구조는 교수자가 별도로 확인합니다.

### 구조 확인 체크리스트
- [ ] Builder의 단계적 설정 뒤 build에서 유효성 검사와 객체 생성을 담당한다.
- [ ] 호출자는 clone을 통해 복제하며 내부 필드를 직접 조립하지 않는다.
- [ ] 복제본의 제목과 섹션을 바꿔도 원본/다른 복제본이 바뀌지 않는다.
- [ ] 소유권을 unique_ptr 또는 동등하게 안전한 방식으로 관리한다.
- [ ] Builder와 Prototype의 역할 및 일반 복사 생성자와의 관계를 설명할 수 있다.
