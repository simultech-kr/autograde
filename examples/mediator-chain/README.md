# Mediator + Chain of Responsibility 실습 자료

C++17로 실습 문의를 담당자에게 전달하는 과제입니다. 학생 단말의 통신은 Mediator에 위임하고, 담당자 선택은 Chain of Responsibility에 위임합니다. 학생용 설명·힌트·TODO 코드, 교수자용 정답·오답, 14개 항목의 100점 채점 자료를 제공합니다.

교과목·주차·마감은 지정하지 않았습니다. 별도의 새 과제로 등록하는 자료이며, 내보내기만으로 기존 과제나 학생 제출 기록이 바뀌지 않습니다.

## 자료 구성

| 자료 | 목적 | 학생 배포 |
| --- | --- | --- |
| [학생 문제](problem01/starter/README.md) | 역할, 명령 계약, 세 예제, 빌드·제출, 평가 기준 | 포함 |
| [구현 힌트](problem01/starter/HINTS.md) | 구현 순서와 C1~M6의 로컬 점검 방법 | 포함 |
| `problem01/starter/main.cpp` | TODO 6개를 포함한 시작 코드 | 포함 |
| `problem01/starter/CMakeLists.txt` | C++17 빌드 설정 | 포함 |
| `problem01/instructor/` | 정답과 0점 검증용 오답 | 제외 |
| `problem01/assignment.json` | 테스트·배점과 학생 공개 평가 요소·힌트 | 제외 |
| [교수자 안내](instructor-guide.md) | 새 과제 등록, 채점 결과 해석, 구조 검토 | 제외 |

자동채점은 공개된 동작 계약을 평가합니다. 두 패턴의 클래스 구조는 별도 소스 검토 대상입니다. 비공개 입력을 사용하는 항목도 평가 요소와 수정 힌트는 학생에게 공개합니다.

## 웹 등록용 ZIP 생성

패키지가 설치된 Python 환경에서 `autograde` 폴더를 기준으로 실행합니다.

```sh
python -m autograde.workshop_catalog \
  --catalog examples/mediator-chain \
  --export build/mediator-chain-upload
```

`build/mediator-chain-upload/problem01/`에 다음 자료를 생성합니다.

- `starter.zip`: 학생 문제·힌트·시작 코드·CMake 설정
- `grading-private.zip`: `solution/main.cpp`, `negative/main.cpp`, `tests.json`
- `assignment.json`: 전체 학생 README를 설명에 반영한 교수자 입력 참고 자료

학생에게는 `starter.zip`을 배포합니다. 정답과 비공개 테스트가 있는 채점 ZIP이나 전체 카탈로그는 학생용 자료에 포함하지 않습니다.

교수자 웹의 **새 과제 등록**에서 별도 과제로 추가합니다. 기존 주차별 등록기는 고정 생성 키를 사용하므로 이 카탈로그에 `--config`, `--data-root`, `--publish` 등록 옵션을 사용하지 않습니다. 구체적인 업로드 순서는 [교수자 안내](instructor-guide.md)를 따릅니다.

## 로컬 검증

```sh
python -m pytest -q tests/integration/test_mediator_chain.py
```

검증 시 정답 100점, 시작 코드·대표 오답 0점, 학생 자료에서 비공개 파일 제외, 학생 README의 세 예제를 확인합니다. 실제 Windows Visual Studio 실행과 운영 서버 등록·공개는 로컬 시험과 별도로 확인합니다.

### 검증 기록 (2026-11-03)

macOS의 C++17 컴파일러와 임시 SQLite·로컬 HTTP 서버에서 확인했습니다.

- 신규 통합 테스트 32개와 기존 평가·교수자 피드백·채점 ZIP 준비 검사 46개 통과
- 정답 100/100, 시작 코드 0/100, 대표 오답 0/100
- README 입력·출력 3쌍, 독립 참조 모델과 무작위 혼합 명령, 명령 수 상한과 프로세스별 상태 초기화 확인
- 잘못된 연쇄 순서·누락, 전달 중복, 오프라인 우회, 실패 ID 선점, 상태 초기화 등 오류 구현 24종 검출
- 교수자 채점 ZIP 등록·검증·공개 → 시험 학생 수령·다운로드·제출 → 평가 요소·수정 힌트 확인
- 정답 제출 후 오답 재제출에서 최신 0점과 이전 최고 100점 구분 확인
- 학생 ZIP에 정답·오답·비공개 테스트가 포함되지 않으며, 비공개 채점 결과에 원문 입력·출력이 노출되지 않음 확인

```sh
python -m pytest -q \
  tests/integration/test_mediator_chain.py \
  tests/unit/test_assignment_evaluation.py \
  tests/integration/test_instructor_evaluation.py \
  tests/integration/test_grading_upload_readiness.py
```

Windows/MSVC에서의 실제 실행과 CMake 빌드는 이 검증에 포함되지 않았습니다. 운영 서버에는 자동으로 등록하지 않습니다.
