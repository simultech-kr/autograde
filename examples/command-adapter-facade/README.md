# Command Adapter Facade 실습 자료

C++17 강의실 장비 제어 과제입니다. 학생은 기존 조명·스피커를 공통 인터페이스로 연결하고, 설정·취소 명령과 장면 제어를 구현합니다. 문제 설명, 시작 코드, 단계별 힌트, 교수자 정답·오답과 14개 채점 항목을 포함합니다.

기존 과제와 별도 자료입니다. 주차·교과목·마감은 지정하지 않았으며 등록 시 결정합니다. 기존 과제나 학생 제출 기록을 변경하지 않습니다.

## 학생 자료와 교수자 자료

| 자료 | 내용 | 학생 배포 |
| --- | --- | --- |
| [학생 README](problem01/starter/README.md) | 문제 상황, 인터페이스, 세 예제, 빌드·제출 방법, 100점 기준 | 포함 |
| [평가 항목별 힌트](problem01/starter/HINTS.md) | 14개 ID별 단계적 점검 방법 | 포함 |
| `problem01/starter/main.cpp` | 컴파일 가능한 TODO 1~5 시작 코드 | 포함 |
| `problem01/starter/CMakeLists.txt` | C++17 빌드 설정 | 포함 |
| `problem01/instructor/` | 정답 및 0점 검증용 오답 | 제외 |
| `problem01/assignment.json` | 테스트 입력·예상 출력·배점·학생 공개 평가 요소와 힌트 | 제외 |
| [교수자 안내](instructor-guide.md) | 신규 등록, 검증, 패턴 구조 검토, 피드백 운영 | 제외 |

자동 점수는 기능 동작을 평가합니다. 패턴의 구조 준수 여부는 교수자 코드 검토로 분리합니다. 입력이 비공개인 항목도 평가 요소와 수정 힌트는 학생에게 공개됩니다.

## 웹 등록용 ZIP 생성

프로젝트 `autograde` 폴더에서 패키지가 설치된 Python 환경으로 실행합니다.

```sh
python -m autograde.workshop_catalog \
  --catalog examples/command-adapter-facade \
  --export build/command-adapter-facade-upload
```

`build/command-adapter-facade-upload/problem01/`에 다음 자료가 생성됩니다.

- `starter.zip`: 학생 README, HINTS, 시작 코드, CMake 설정
- `grading-private.zip`: `solution/main.cpp`, `negative/main.cpp`, `tests.json`
- `assignment.json`: 전체 학생 README를 문제 설명에 반영한 교수자 참고 자료

내보내기는 DB를 변경하지 않습니다. 학생에게는 `starter.zip`만 제공합니다. 정답과 테스트가 포함된 `grading-private.zip`이나 이 카탈로그 전체를 학생에게 배포하지 않습니다.

이 자료는 교수자 웹의 **새 과제 등록**으로 추가합니다. 기존 주차별 등록기가 사용하는 고정 생성 키와 `problem01`이 충돌할 수 있으므로 이 카탈로그에는 `--config`, `--data-root`, `--publish` 등록 옵션을 사용하지 않습니다. 기존 과제에 덮어쓰는 대신 별도의 신규 과제를 만듭니다.

## 자동 검증

```sh
python -m pytest -q tests/integration/test_command_adapter_facade.py
```

정답·시작 코드·오답 점수, README 예제, 대표 구현 오류, 컴파일 오류, 비공개 자료 분리, 교수자 검증과 학생 제출 결과를 검사합니다. 시험은 임시 저장소와 합성 계정을 사용합니다. 실제 수업 데이터를 초기화하거나 원격 서버에 등록하지 않습니다.

로컬 채점 시험은 보안 격리가 아닙니다. 검토한 이 자료만 실행하며 임의의 학생 코드를 섞지 않습니다. 자동 시험의 성공과 실제 Windows Visual Studio 설치·실행, 원격 운영 서버의 공개 여부는 별개입니다.

## 검증 기록

2026년 10월 7일 macOS 로컬 환경에서 다음을 확인했습니다.

- 과제 전용 자동 시험 22개 통과: 정답 100점, 시작 코드·오답 0점, README 예제 3개, 200개 명령, 새 프로세스 초기화
- 대표 구현 오류 15종에서 해당 평가 항목 실패와 수정 힌트 확인
- 컴파일 실패 시 기능 항목 미검사와 평가 요소 안내 확인
- 임시 서버에서 교수자 자료 등록·검증·공개, 학생 수령·다운로드·제출·결과 전달 확인
- 100점 뒤 0점 재제출에서 이번 점수 0점과 이전 최고점 100점 분리 확인
- 기존 평가 요소·힌트 관련 회귀시험 43개 통과

총 65개 시험 통과입니다. 기존 채점기의 macOS SDK 탐색을 사용했으며 이 과제를 위해 서버 코드는 변경하지 않았습니다. Windows IDE와 CMake 실행은 별도 확인이 필요합니다. 원격 운영 서버 등록·공개는 수행하지 않았습니다.

관련 시험 재실행:

```sh
python -m pytest -q tests/integration/test_command_adapter_facade.py \
  tests/unit/test_assignment_evaluation.py \
  tests/integration/test_instructor_evaluation.py
```
