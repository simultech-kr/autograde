# Iterator 실습 자료

C++17로 실습 제출 기록을 순회하는 과제입니다. 학생은 점수 필터를 가진 Iterator, 독립적인 두 순회 위치, 생성 시점 스냅샷, 종료와 재순회를 구현합니다. 학생용 설명·힌트·TODO 코드와 교수자 정답·오답, 12개 항목의 100점 채점 자료를 제공합니다.

교과목·주차·마감은 지정하지 않았습니다. 기존 주차별 과제와 별도의 자료이며, 생성·내보내기만으로 기존 과제나 학생 제출 기록이 바뀌지 않습니다.

## 자료 구성

| 자료 | 목적 | 학생 배포 |
| --- | --- | --- |
| [학생 문제](problem01/starter/README.md) | 문제 상황, 클래스 계약, 명령·오류 규칙, 세 예제, 실행·제출, 평가 기준 | 포함 |
| [단계별 힌트](problem01/starter/HINTS.md) | 공통 구현 원리와 I1~I12 점검 방법 | 포함 |
| `problem01/starter/main.cpp` | 컴파일 가능한 TODO 5개 | 포함 |
| `problem01/starter/CMakeLists.txt` | C++17 빌드 설정 | 포함 |
| `problem01/instructor/` | 정답과 0점 검증용 오답 | 제외 |
| `problem01/assignment.json` | 테스트·배점과 학생 공개 평가 요소·힌트 | 제외 |
| [교수자 안내](instructor-guide.md) | 등록·검증·구조 검토 기준 | 제외 |

자동채점은 공개된 동작 계약을 평가합니다. Iterator 패턴 구조는 교수자 소스 검토로 분리합니다. 비공개 입력을 쓰는 항목도 평가 기준과 수정 힌트는 학생에게 공개합니다.

## 웹 등록용 ZIP 생성

패키지가 설치된 Python 환경에서 `autograde` 폴더를 기준으로 실행합니다.

```sh
python -m autograde.workshop_catalog \
  --catalog examples/iterator-pattern \
  --export build/iterator-pattern-upload
```

`build/iterator-pattern-upload/problem01/`에 다음 파일을 만듭니다.

- `starter.zip`: 학생 문제·힌트·시작 코드·CMake 설정
- `grading-private.zip`: `solution/main.cpp`, `negative/main.cpp`, `tests.json`
- `assignment.json`: 전체 학생 README를 설명에 반영한 교수자 입력 참고 자료

내보내기는 데이터베이스를 변경하지 않습니다. 학생 배포는 `starter.zip`으로 한정합니다. 정답·비공개 테스트가 있는 채점 ZIP이나 카탈로그 전체를 학생용 저장소에 올리지 않습니다.

교수자 웹의 **새 과제 등록**에서 별도 과제로 추가합니다. 기존 주차별 등록기는 고정 생성 키를 사용하므로 이 `problem01` 카탈로그에 `--config`, `--data-root`, `--publish` 등록 옵션을 사용하지 않습니다. [교수자 안내](instructor-guide.md)의 ZIP 업로드 절차를 따릅니다.

## 자동 검증

```sh
python -m pytest -q tests/integration/test_iterator_pattern.py
```

과제 메타데이터, 학생 ZIP의 비공개 자료 제외, 정답·시작 코드·오답 점수, 설명의 예제, 대표 구현 오류, 컴파일 오류, 합성 계정의 등록·수령·제출을 검사합니다. 임시 저장소만 사용하며 실제 수업 데이터를 초기화하거나 원격 서버에 등록하지 않습니다.

로컬 시험은 코드 보안 격리가 아닙니다. 이 검토된 예제만 실행하며 임의의 학생 코드를 섞지 않습니다. macOS 시험과 Windows Visual Studio 실행 검증은 구분합니다.

## 검증 기록

2026년 10월 13일 macOS 로컬 환경에서 과제 전용 시험 24개와 기존 평가 요소·힌트 관련 회귀시험 43개, 총 67개를 통과했습니다.

- 정답 100점, 시작 코드·대표 오답 0점과 컴파일 성공
- README의 입력·출력 3쌍, 최대 200개 명령, 새 프로세스의 초기 상태
- 대표 오류 변형 시험 17건에서 해당 평가 항목 실패와 수정 힌트 연결
- 같은 점수의 서로 다른 ID, 중간·끝의 연속 미달 기록, 다른 슬롯에 영향을 주지 않는 초기화
- 컴파일 실패 시 기능 미검사·컴파일 안내와 평가 요소 표시
- 학생 ZIP 및 학생 다운로드에서 정답·오답·비공개 테스트 제외
- 임시 서버의 교수자 자료 등록·검증·공개와 합성 학생의 수령·다운로드·제출
- 100점 후 0점 재제출에서 이번 점수 0점과 이전 최고점 100점 분리

서버·확장 코드는 변경하지 않았습니다. 실제 Windows Visual Studio 실행과 CMake 빌드는 별도 확인이 필요합니다. 운영 서버 등록·공개, 교과목·마감 지정은 수행하지 않았습니다.

관련 시험을 함께 실행하려면 다음 명령을 사용합니다.

```sh
python -m pytest -q tests/integration/test_iterator_pattern.py \
  tests/unit/test_assignment_evaluation.py \
  tests/integration/test_instructor_evaluation.py
```
