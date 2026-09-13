# C/C++ Hello World 파일럿 과제

두 환경의 첫 빌드·제출 확인용 C17·C++17 과제다. Java/JDK는 사용하지 않는다.
입력 없이 `Hello, World!` 한 줄과 줄바꿈을
표준 출력에 쓰고 정상 종료한다. Windows 전용 API·Linux 시스템 호출은 아직 평가하지 않는다.

- `c/windows/starter`, `c/linux/starter`: C17의 `main.c` 과제
- `windows/starter`, `linux/starter`: C++17의 `main.cpp` 과제
- `c/solution`, `solution`: 각각 C·C++ 교수자 정답(학생 starter에 포함하지 않음)
- `assessment/grade.py`: C/C++ 공용, POSIX 및 Windows MSVC의 신뢰 코드 전용 채점 스크립트
- `c/data`, `data`: 각각 C·C++의 공개 예상 출력과 교수자 언어 설정

교수자가 등록한 데이터의 `language.json`으로 C(`c`) 또는 C++(`cpp`)를 고정한다.
학생 제출물의 설정으로 언어를 변경할 수 없다. 기존 C++ 과제 호환을 위해 언어 파일이 없으면
C++로 처리하며 잘못된 교수자 설정은 채점 환경 오류로 보고한다.

컴파일 2점 + 정상 종료 3점 + 정확한 출력 5점 = **10점**이다. CRLF와 LF만 동등하게 취급하며
불필요한 공백·추가 출력은 오답이다. Starter는 출력이 없는 미완성 코드이므로 5점, 정답은
10점이다. 컴파일 실패 0점, 실행 실패·시간 초과는 컴파일 점수 2점만 받는다. 도구 설치 오류는
학생 0점으로 처리하지 않고 채점 환경 오류(exit 78)로 보고한다.

## Linux/WSL2 서버 등록

**등록 기본값은 초안입니다.** 아래 등록 후 반환된 ID에 대해 `bundle-check`와 `bundle-ready`가
필요합니다. [전체 순서](../../docs/operations/course-assignment-management.md)를 따르세요.
검증에는 C는 `c/solution`, C++는 `solution`을 모범답안으로, 해당 Linux starter를
오답 예제로 지정하며 두 언어 모두 `--negative-score 5`를 사용합니다.

저장소 루트에서 실행한다. 아래 `come3105`는 등록 예시일 뿐 교과목의 운영체제를 고정하지
않는다. `come2201`에 등록하려면 해당 config를 사용한다. 서버에는 C17·C++17 컴파일러가 필요하다.
먼저 C 과제를 등록한다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-add hello-world-c \
  --release-id hello-world-c-v1 --title 'Hello World - C17' \
  --starter examples/hello-world/c/linux/starter \
  --assessment examples/hello-world/assessment --data examples/hello-world/c/data \
  --rubric-version hello-world-c-v1 --max-score 10 --result-policy immediate
```

C++ 과제도 별도로 등록할 수 있다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-add hello-world-linux \
  --release-id hello-world-linux-v1 --title 'Hello World - Linux/WSL2' \
  --starter examples/hello-world/linux/starter \
  --assessment examples/hello-world/assessment --data examples/hello-world/data \
  --rubric-version hello-world-v1 --max-score 10 --result-policy immediate

.venv/bin/autograde-pilot --config pilot/come3105.csv
```

최초 실행 전 `pilot/student_roster.csv`에 실제 명단과 전용 비밀번호를 입력하고 `chmod 600`을
적용한다. [명단 초기화와 웹 실행 안내](../../docs/operations/independent-web-pilot.md)를 따른다.
학생은 20010번 웹에서 코드 발급 후 20000번 API를 사용하는 VS Code 확장으로 과제를 받는다.

교수자 사전 시험(C):

```bash
.venv/bin/python examples/hello-world/assessment/grade.py --submission examples/hello-world/c/solution --data examples/hello-world/c/data
.venv/bin/python examples/hello-world/assessment/grade.py --submission examples/hello-world/c/linux/starter --data examples/hello-world/c/data
```

교수자 사전 시험(C++):

```bash
.venv/bin/python examples/hello-world/assessment/grade.py --submission examples/hello-world/solution
.venv/bin/python examples/hello-world/assessment/grade.py --submission examples/hello-world/linux/starter
```

각각 JSON의 `score`가 10, 5인지 확인한다. 점수 JSON이 아니라 exit 78이 나오면 컴파일러와
채점 데이터 설치를 확인한다. macOS 개발 검증에서는 SDK의 C++ 헤더 경로를 확인해 사용한다.
macOS 검증을 Linux 시스템 호출의 호환성 검증으로 보지는 않는다.

## Windows / VS2022 Community 사전 시험

Windows 채점용 PC 또는 VM을 별도로 사용할 수 있다는 운영 전제를 채택한다.
VS2022 Community에서 **C++를 사용한 데스크톱 개발** 및 CMake 도구·Windows SDK를 설치하고,
Python 3.10 이상도 준비한다. [Microsoft의 CMake 폴더 열기 안내](https://learn.microsoft.com/en-us/cpp/build/get-started-linux-cmake?view=msvc-170).

학생 개발은 C의 `c/windows/starter` 또는 C++의 `windows/starter` 폴더를 VS2022에서 연다.
C는 `/TC /std:c17`, C++는 `/TP /std:c++17`로 빌드한다.
[Microsoft 언어 표준 옵션 안내](https://learn.microsoft.com/en-us/cpp/build/reference/std-specify-language-standard-version?view=msvc-170).
교수자 채점 시험은
**x64 Native Tools Command Prompt for VS 2022**에서 저장소 루트로 이동한 뒤 실행한다.
서버 패키지 설치나 환경변수 수동 선언 없이 이 스크립트만 실행할 수 있다.

```text
py -3 examples\hello-world\assessment\grade.py --submission examples\hello-world\c\solution --data examples\hello-world\c\data
py -3 examples\hello-world\assessment\grade.py --submission examples\hello-world\c\windows\starter --data examples\hello-world\c\data
py -3 examples\hello-world\assessment\grade.py --submission examples\hello-world\solution
py -3 examples\hello-world\assessment\grade.py --submission examples\hello-world\windows\starter
```

예상 점수는 각 언어의 정답 10점, starter 5점이다. `cl.exe`를 찾지 못하면 VS2022 개발자 명령 프롬프트인지 확인한다.
채점 스크립트의 시간·출력 제한과 프로세스 정리는 최선 노력이며 격리 환경이 아니다.
시험 장비에서 사전 검토한 코드만 사용한다. 학생이 수정한 CMake나 임의 빌드 스크립트는
채점 과정에서 실행하지 않으며 `main.c` 또는 `main.cpp`를 정해진 컴파일 명령으로 빌드한다.

**현재 완료 범위:** Windows starter와 로컬 채점 명령, [VS2022/VS2026 확장 소스](../../extensions/visualstudio/README.md)를
제공한다. 확장 Windows VSIX 패키징·설치는 미검증이며 Windows 원격 작업자·운영체제별 작업 배정은
아직 미구현이다. Windows 과제를 현재 POSIX
채점기에 등록해 통과하더라도 MSVC/Windows에서 검증된 것으로 간주하면 안 된다.
이번 개발 환경은 macOS이므로 Windows/MSVC 실행 시험은 Windows 장비에서 별도로 수행한다.

## 자동 시험

```bash
.venv/bin/python -m pytest -q tests/integration/test_hello_world_assessment.py tests/unit/test_pilot_roster.py
```

Windows에서는 전체 서버가 아닌 이 과제의 독립 시험만 실행한다. pytest가 설치되어 있어야 한다.

```text
py -3 -m pytest -q tests\integration\test_hello_world_assessment.py
```

두 언어의 정답, CRLF, 오답, 컴파일 오류, 비정상 종료, 무한 반복, 출력 과다와 도구 부재를 확인한다.
C 과제에 C++ 코드를 제출하거나 학생이 언어 설정을 변조하는 경우도 확인한다.
Windows 전용 시험은 Windows/MSVC가 없으면 명시적으로 건너뛴다.
