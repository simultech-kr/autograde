# C++ Observer pattern pilot assignment

교수자가 등록하고 학생이 VS Code 확장으로 내려받아 제출하는 C++17 예제 과제입니다.
GitHub, Docker, CMake은 필요하지 않습니다.

- `starter/`: 학생에게 배포되는 문제와 시작 코드
- `assessment/`: 서버에만 보관하며 제출물에 주입되는 행동 기반 채점기와 테스트 harness
- `data/`: 채점 workspace에 별도로 주입되는 공개 시나리오 설정

채점기는 학생의 소스 문자열을 검색하지 않습니다. `AUTOGRADE_DATA_DIR`에 별도로 주입된
공개 시나리오 설정을 검증하고, 학생의 `observer.cpp`와 서버의 비공개 테스트 harness를
함께 컴파일한 뒤 실제 객체 등록·통지·해제 동작을 실행하여 평가합니다. 과제 등록 시
만점은 `10`으로 지정해야 합니다.

## 실행 요구사항

- Python 3.10 이상
- `c++`, `g++`, `clang++` 중 하나
- C++17을 지원하는 컴파일러

이 예제는 신뢰할 수 있는 합성 코드로 로컬 파일럿을 검증하기 위한 것입니다. 현재
`pilot-local` 채점기는 보안 sandbox가 아니므로 검증되지 않은 코드를 운영 호스트에서
실행하면 안 됩니다.
