# 옵저버 패턴 Java/C++ 과제 파일럿

이 문서는 교수자가 동일한 소프트웨어 설계 주제를 Java와 C++ 과제로 등록하고, 학생 역할로
다운로드·제출한 뒤 자동 채점 결과와 dashboard를 확인하는 파일럿 절차입니다. 모든 명령은
`pilot/course.csv`를 직접 지정합니다. Shell 환경변수, GitHub, Docker/Podman은 사용하지
않습니다.

> **안전 경계:** `pilot-local`은 보안 sandbox가 아닙니다. 자동 시험이 test 내부에서 구성하는
> 합성 구현 또는 담당자가 직접 작성·검토한 합성 코드만 실행합니다. `examples/submissions/`에
> 바로 제출할 정답·오답 fixture가 포함되어 있다고 가정하지 않습니다. 실제 학생의 임의 코드,
> confidential hidden test, 공식 성적, LAN/인터넷 공개 서비스에는 사용하지 않습니다.

## 1. 학습 목표와 과제 계약

두 과제의 공통 목표는 다음과 같습니다.

- Subject와 Observer 사이의 결합도를 낮추는 인터페이스를 설계한다.
- Observer 등록, 중복 등록 방지, 해제와 변경 통지를 구현한다.
- 여러 Observer가 있을 때 콜백 횟수와 전달 값이 정확하도록 만든다.
- 소스 형태가 아니라 실행 시 동작으로 설계 계약을 검증한다.

Java 과제는 날씨 관측 예제로 구성됩니다. 구현 대상은
`WeatherStation`과 `CurrentConditionsDisplay`이며, 불변 `WeatherSnapshot`을 Observer에
전달합니다. Starter는 `examples/observer-java/starter`, 채점기는
`examples/observer-java/assessment`, 입력 데이터는
`examples/observer-java/data/scenarios.csv`에 있습니다.

C++ 과제는 범용 `Subject`/`Observer` 예제로 구성됩니다. `Subject::attach`, `detach`,
`setState`, `state`와 `Observer::update(int)` 계약을 구현합니다. `nullptr`과 중복 등록을
무시하고 등록 순서대로 알리며, 통지 도중 구독 목록이 바뀌면 그 변경은 다음 `setState`부터
반영해야 합니다. Starter는 `examples/observer-cpp/starter`, 채점기는
`examples/observer-cpp/assessment`, 공개 입력은
`examples/observer-cpp/data/public-scenarios.json`에 있습니다.

채점기는 학생 소스의 특정 문자열을 찾지 않습니다. 학생 코드와 행동 harness를 함께
컴파일하고, 인스턴스 등록·상태 변경·콜백 횟수와 값·해제 동작을 실행해서 판정합니다.
Assessment와 data는 starter에는 포함되지 않지만 `pilot-local`에서 학생 process와 격리되지
않으므로 confidential 자료로 취급하면 안 됩니다.

## 2. 도구 확인

Autograde 자체에는 Python 3.10 이상이 필요합니다. Java 과제는 표준 JDK 11 이상에서
동작하며 파일럿 장비에는 JDK 17 이상을 권장합니다. C++ 과제는 CMake 없이 C++17 compiler
하나만 필요합니다.

경로가 존재하는지만 확인하면 macOS의 Java stub을 실제 JDK로 잘못 판단할 수 있습니다.
아래 명령이 각각 종료 코드 0으로 끝나고 버전을 출력해야 합니다.

```bash
python3 --version
javac -version
java -version
c++ --version
```

`c++`이 없다면 `g++ --version` 또는 `clang++ --version`으로 사용할 compiler를 확인합니다.
2026-09-04 기준 이 저장소를 작성한 host에는 `/usr/bin/java`와 `/usr/bin/javac` 경로만 있고
실행 가능한 JDK가 없습니다. 따라서 이 host에서 Java compiler 의존 시험은 **skip**이 정상이며,
Java 수동 제출 시험은 JDK를 설치한 뒤 진행해야 합니다. 경로만 존재하거나 version 명령이
실패하는 상태에서 Java 과제를 Go로 판정하지 않습니다.

## 3. 파일럿 초기화

저장소 root에서 한 번 실행합니다. 이미 같은 course를 초기화하고 roster를 가져왔다면
멱등하게 다시 실행해도 됩니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv init
chmod 600 pilot/roster.csv
.venv/bin/autograde-platform --pilot-config pilot/course.csv student import pilot/roster.csv
```

이 절차는 `pilot/course.csv`의 `grading_runtime=pilot-local`과 loopback 주소를 그대로
사용합니다. 별도의 설정 환경변수나 credential을 선언하지 않습니다.

## 4. 교수자 과제 등록

처음에는 `--not-ready`로 등록합니다. 이 상태에서는 학생 과제 목록에 노출되지 않으므로
starter, assessment/data digest, 배점과 release를 확인한 다음 공개할 수 있습니다.

### Java

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-add observer-java \
  --release-id observer-java-v1 \
  --title 'Observer Pattern - Java' \
  --starter examples/observer-java/starter \
  --assessment examples/observer-java/assessment \
  --data examples/observer-java/data \
  --rubric-version observer-java-v1 \
  --max-score 10 \
  --result-policy immediate \
  --not-ready
```

### C++

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-add observer-cpp \
  --release-id observer-cpp-v1 \
  --title 'Observer Pattern - C++' \
  --starter examples/observer-cpp/starter \
  --assessment examples/observer-cpp/assessment \
  --data examples/observer-cpp/data \
  --rubric-version observer-cpp-v1 \
  --max-score 10 \
  --result-policy immediate \
  --not-ready
```

등록 결과 JSON의 `assignment_id`, `release_id`, `starter_digest`, `assessment_digest`,
`dataset_digest`, `max_score`, `ready=false`를 기록합니다. 두 과제를 함께 확인하려면 다음을
실행합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-list --include-inactive
```

검토를 마친 뒤 아래 자리표시자를 목록에서 확인한 실제 ID로 바꾸어 공개합니다. ID를 shell
변수로 선언할 필요는 없습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-ready <JAVA_ASSIGNMENT_ID>
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-ready <CPP_ASSIGNMENT_ID>
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-list --ready-only
```

등록한 release의 파일을 덮어쓰지 않습니다. 과제나 rubric을 수정하면
`observer-java-v2`/`observer-cpp-v2`처럼 새 `release_id`와 새 rubric version으로 등록합니다.
파일럿에서는 `--runner-image` 또는 container runtime 옵션을 추가하지 않습니다.

## 5. 학생 다운로드와 제출

활성화 코드를 아직 발급하지 않았다면 한 명의 시험 계정에 발급하고 서버를 시작합니다.

```bash
mkdir -p pilot/activation-codes
chmod 700 pilot/activation-codes
.venv/bin/autograde-platform --pilot-config pilot/course.csv auth issue s001 \
  --output pilot/activation-codes/s001-observer-login-01.txt
.venv/bin/autograde-platform --pilot-config pilot/course.csv serve
```

학생 역할에서는 Linux/macOS의 local VS Code 또는 Windows의 WSL2 VS Code를 사용합니다.

1. 두 과제를 함께 담을 전용 수업 폴더를 VS Code workspace root로 열고 신뢰합니다. 과제
   starter 자체를 root로 먼저 열지 않습니다.
2. 왼쪽 Activity Bar의 **Autograde** 아이콘을 열고 **Assignments** 영역의
   **지금 로그인** 또는 제목 표시줄의 **로그인**을 누릅니다. 확인 창의 service origin이
   `http://127.0.0.1:18080`과 같은지 확인합니다.
3. 브라우저에 VS Code 연결 코드와 이번 로그인용 `s001` 활성화 코드를 입력합니다. 이
   활성화 코드는 한 번 소비되며 다음 로그인에 재사용하지 않습니다.
4. 자동으로 표시된 `Observer Pattern - Java`를 펼쳐 **과제 파일 다운로드**를 선택합니다.
5. `Observer Pattern - C++`도 펼쳐 **과제 파일 다운로드**를 선택하여 같은 수업 폴더 바로
   아래에 내려받습니다. 각 과제 행 오른쪽의 다운로드 아이콘도 같은 동작입니다.
6. 창이나 workspace를 바꾸지 않고 두 하위 폴더의 README와 공개 계약을 따라 구현합니다.
7. 제출할 과제의 파일을 editor에서 연 뒤 `Autograde: Submit Assignment`을 실행합니다. 후보가
   여러 개이면 Quick Pick에서 Java 또는 C++ 과제를 명시적으로 선택합니다.
8. `Autograde: View Latest Result`에서 총점, rubric 항목과 공개 diagnostics를 확인합니다.
9. 자리를 떠나기 전에 `Autograde: Sign Out`으로 server session 폐기를 확인합니다.

로그인과 두 과제 다운로드는 Command Palette 없이 수행합니다. 사이드바 버튼이 보이지 않을
때만 `Autograde: Sign In`과 `Autograde: Download or Clone Assignment` 명령을 대체 경로로
사용합니다.

Direct bundle 제출이므로 Git repository 생성, commit, push 또는 Pull Request가 필요하지
않습니다. 두 과제는 같은 수업 workspace의 서로 다른 직접 하위 폴더에 내려받으며 각각의
`.autograde/assignment.json`을 사용합니다. 다운로드 후 새 창이나 workspace reload가 발생하지
않아 현재 메모리 로그인으로 이어서 제출할 수 있어야 합니다.

다른 좌석 또는 VS Code/WSL 재시작 뒤 이어서 시험할 때에는 교수자가
`s001-observer-login-02.txt` 같은 새 경로로 새 활성화 코드를 발급합니다. 서비스 URL 설정만
지속되고 access/refresh token은 Extension Host 메모리에만 있으므로 기존 로그인은 복구되지
않습니다. 새 로그인 성공 시 같은 학생의 이전 server session은 폐기되고 최신 session 하나만
남습니다. Session은 최초 발급 후 4시간의 절대 만료를 가지며 refresh로 연장되지 않습니다.

공용 장비에서는 Sign Out과 별도로 두 Observer 과제 workspace/source, build 결과,
`.autograde` marker와 browser/OS profile 흔적을 실습실 정책에 따라 정리합니다. Loopback HTTP
시험은 server와 Extension이 같은 신뢰 장비에 있을 때만 수행합니다.

## 6. 채점 방식과 배점

Java는 표준 `javac`/`java`로 제출 소스와 학생 starter에 포함되지 않은 행동 harness를
컴파일·실행합니다. `scenarios.csv`의 입력을 주입해 최신 측정값과 notification을
검증합니다.

| Java 항목 | 점수 | 검증 내용 |
|---|---:|---|
| 컴파일 및 공개 API | 2.0 | 요구된 type과 method로 전체 코드가 컴파일됨 |
| 기본 알림과 최신 측정값 | 3.0 | 측정 변경 시 snapshot 값과 station 최신 값이 정확함 |
| 중복 등록 방지와 해제 | 2.0 | 같은 Observer는 한 번만 알림을 받고 해제 뒤에는 받지 않음 |
| 복수 Observer 통지 | 1.5 | 등록된 여러 Observer가 각각 정확히 통지받음 |
| Display 상태 반영 | 1.5 | `CurrentConditionsDisplay`가 받은 최신 snapshot을 반영함 |
| **합계** | **10.0** | |

C++은 사용 가능한 `c++`, `g++`, `clang++` 중 하나로 C++17 제출 소스와
`observer_behavior_test.cpp`를 직접 컴파일하고 각 행동 시나리오를 별도로 실행합니다.

| C++ 항목 | 점수 | 검증 내용 |
|---|---:|---|
| 컴파일 및 공개 API | 2.0 | 요구된 C++17 type과 method로 전체 코드가 컴파일됨 |
| 상태 갱신과 기본 통지 | 2.0 | `setState`가 상태를 저장하고 Observer에 정확한 값을 전달함 |
| 복수 Observer와 순서 | 2.0 | 모든 Observer가 등록 순서대로 통지받음 |
| 중복·null 등록 방지 | 1.0 | 중복 pointer와 `nullptr`을 무시함 |
| 해제 | 1.5 | `detach` 이후 대상 Observer를 통지하지 않음 |
| 통지 중 변경 snapshot | 1.5 | callback 도중 attach/detach한 변경은 다음 통지부터 반영됨 |
| **합계** | **10.0** | |

Compiler/JDK가 없거나 실행 불가능하면 학생에게 0점 결과를 게시하지 않습니다. Assessment가
nonzero로 종료하고 platform이 제출을 `assessment_failed`로 기록해야 합니다. Assessment
harness가 없거나 서버 data가 누락·손상된 경우도 같은 운영 실패입니다. Toolchain의 구체적
진단은 집중 시험 또는 서버 log에서 확인하며 학생 오답으로 합산하지 않습니다.

반면 학생 소스 누락이나 compile error는 제출물 오류이므로 0점 결과와 source diagnostic을
게시합니다. 행동 항목 하나만 실패하면 나머지 통과 항목은 부분점수로 보존합니다. Assessment
예외, timeout/crash 또는 잘못된 결과 형식은 임의의 0점이 아니라 `assessment_failed`로
구분합니다.

## 7. 교수자 결과 확인

Dashboard는 `http://127.0.0.1:18080/instructor`에서 엽니다.

- 사용자 이름: `instructor`
- 암호: `pilot/.data/platform-instructor-token` 파일 내용

학생 × 과제 행에서 다운로드 수, 제출 수, 최신 상태, `score / 10`, 최근 제출 시각을
확인합니다. CLI로 특정 과제의 제출 상태와 opaque 제출 ID를 확인할 수도 있습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv submission list \
  --assignment-key observer-java
.venv/bin/autograde-platform --pilot-config pilot/course.csv submission list \
  --assignment-key observer-cpp
.venv/bin/autograde-platform --pilot-config pilot/course.csv submission show <SUBMISSION_ID>
```

Dashboard는 운영 현황과 총점을 보여줍니다. 항목별 rubric과 diagnostics는 시험 학생 계정의
`Autograde: View Latest Result`에서 확인합니다. `result-policy=immediate`이므로 정상 채점된
결과는 별도 publish 명령 없이 학생에게 공개되어야 합니다.

## 8. 검증 시나리오

자동 시험과 한 명의 수동 Extension 왕복을 모두 통과해야 과제 workflow를 Go로 판정합니다.

| 시나리오 | Java 기대 결과 | C++ 기대 결과 | 확인 위치 |
|---|---|---|---|
| 정상 구현 | 10 / 10 | 10 / 10 | 집중 자동 시험 + Extension |
| 컴파일 오류 | compile/API 점수 없음, source diagnostic | compile/API 점수 없음, source diagnostic | rubric/result |
| 중복 Observer 등록 | 중복 방지 항목 감점 | 중복·null 항목 감점 | rubric/result |
| Observer 해제 누락 | 해제 항목 감점 | 해제 항목 감점 | rubric/result |
| 복수 Observer 누락 | 복수 통지 항목 감점 | 복수·순서 항목 감점 | rubric/result |
| 통지 중 목록 직접 순회 | 해당 없음 | snapshot 항목 감점 | C++ rubric/result |
| Compiler/JDK 없음 | compiler 의존 pytest skip, live grade는 `assessment_failed`·점수 없음 | 동일 | pytest/server log/dashboard |
| Assessment harness 누락 | `assessment_failed`, 점수 없음 | 동일 | 집중 시험/dashboard |
| 서버 data 누락·손상 | `assessment_failed`, 점수 없음 | 동일 | 집중 시험/dashboard |
| 동일 제출 재시도 | 제출 중복으로 점수가 늘지 않음 | 동일 | dashboard |
| Assessment 내부 오류 | `assessment_failed`, 임의 0점 금지 | 동일 | dashboard/submission |
| 같은 학생 새 좌석 로그인 | 이전 session 거부, 최신 session만 유효 | 동일 | Extension/API/session list |
| VS Code/WSL 재시작 | URL은 유지, 로그인은 유지되지 않음 | 동일 | Extension Sign In 화면 |

집중 자동 시험은 다음과 같이 실행합니다.

```bash
.venv/bin/python -m pytest -q tests/integration/test_observer_assignment_catalog.py
.venv/bin/python -m pytest -q tests/integration/test_observer_java_assessment.py
.venv/bin/python -m pytest -q tests/integration/test_observer_cpp_assessment.py
```

2026-09-04 현재 host에서 위 세 파일을 함께 실행한 결과는 `9 passed, 5 skipped`입니다.
등록 catalog, C++ 정상·부분점수·컴파일 오류·인프라 실패, Java JDK 부재 실패 정책은
통과했습니다. Skip 5개는 모두 동작 가능한 JDK가 필요한 Java 행동 시험입니다. 따라서 현재
장비에서는 **과제 등록과 C++ 채점은 확인됨**, **Java 행동 채점은 JDK 설치 전 No-Go**로
기록합니다.

Catalog 시험은 두 과제가 CSV 파일럿 CLI로 등록 가능하고 digest·release·배점이 기대값과
일치하는지 확인합니다. JDK probe가 실패한 장비에서는 Java의 compiler 의존 test가 skip되어야
하며, JDK 없이 실행되는 별도 시험은 live grader가 점수를 만들지 않고 nonzero로 실패하는지
확인합니다. Skip을 성공적인 Java 채점 검증으로 세지 않습니다. C++ compiler가 준비된 현재
host에서는 C++ test가 내부에서 구성하는 정상·부분점수·컴파일 오류 구현을 모두 실행해야
합니다. 그 다음 전체 회귀
시험과 Extension 시험을 실행합니다.

```bash
.venv/bin/python -m pytest -q

cd extensions/vscode
npm test
shasum -a 256 -c SHA256SUMS
```

마지막으로 `s001`로 두 starter를 실제 내려받아, 과제 계약에 따라 담당자가 직접 작성·검토한
정상 구현과 대표 오답을 각각 제출합니다. Repository에는 이 수동 제출용 답안 파일을 제공하지
않습니다.
다음 증거를 시험 기록에 남깁니다.

- 과제 등록 JSON과 두 immutable digest
- `bundle-list --ready-only` 결과
- toolchain version 출력 또는 명시적인 skip 사유
- 학생 최신 결과의 총점·rubric·diagnostics
- Dashboard의 학생·과제별 상태와 점수
- Sign Out 뒤 이전 token 거부 및 다음 로그인에 새 활성화 코드를 사용한 기록
- 좌석 교대 전 Observer workspace와 browser/OS profile 정리 확인
- 서버 종료 후 남은 채점 process와 임시 workspace가 없다는 확인

자동 시험 통과는 `pilot-local`의 격리 안전성을 증명하지 않습니다. 실제 학생 20명 대상
시험은 [파일럿 Go/No-Go 체크리스트](go-live-checklist.md)의 별도 수동 drill을 적용합니다.
