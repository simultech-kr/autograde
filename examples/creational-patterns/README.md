# Factory Method · Abstract Factory · Singleton 실습

C++17 단일 파일 과제입니다. 기존 COME2201 주차별 공개 과제와 별도 자료이며, 기존 과제·제출·점수를 변경하지 않습니다. 주차와 마감은 지정하지 않았습니다. 운영 서버 등록·학생 공개는 수행하지 않았습니다.

- [학생용 문제](problem01/starter/README.md)
- [항목별 힌트](problem01/starter/HINTS.md)
- [교수자 등록·평가 안내](instructor-guide.md)
- [채점 설정](problem01/assignment.json) — 교수자 전용

## 자료 구분

| 경로 | 내용 | 학생 배포 |
| --- | --- | --- |
| `problem01/starter/` | 문제, 힌트, TODO 코드, CMake 설정 | 포함 |
| `problem01/instructor/` | 정답·0점 검증 코드 | 제외 |
| `problem01/assignment.json` | 12개 테스트, 100점 배점, 평가 요소, 수정 힌트 | 제외 |

프로그램은 요청한 출력 형식과 테마로 보고서를 만들고, 하나의 Singleton에서 성공 건수와 일련번호를 관리합니다. Factory는 Factory Method를 뜻하며 Simple Factory와 구분합니다.

자동채점은 기능 100점입니다. 항목마다 학생 공개 `evaluation`과 실패 시 공개 `hint`를 제공합니다. 비공개 시험의 입력·예상 출력은 노출하지 않습니다. 패턴의 구조 준수 여부는 교수자가 제출 코드를 별도로 확인합니다.

## 웹 업로드 파일 생성

프로젝트의 `autograde` 폴더에서 실행합니다.

```sh
python -m autograde.workshop_catalog \
  --catalog examples/creational-patterns \
  --export build/creational-patterns-upload
```

`build/creational-patterns-upload/problem01/`에 생성되는 파일:

- `starter.zip`: 학생 배포용
- `grading-private.zip`: 교수자 전용 정답·오답·테스트·평가 요소
- `assignment.json`: 문제 설명을 포함한 교수자 입력 참고 자료

내보내기는 DB를 변경하지 않습니다. 이 자료는 **교수자 웹에서 신규 초안으로 등록**합니다. 기존 주차별 카탈로그용 `--config`/`--data-root` 등록 명령은 사용하지 않습니다. 해당 등록기의 고정 생성 키와 이 자료의 `problem01` 이름이 기존 과제와 충돌할 수 있습니다.

저장소 전체나 `grading-private.zip`을 학생에게 제공하지 않습니다. 학생에게는 `starter.zip`만 배포합니다.

## 검증 실행

```sh
python -m pytest -q tests/integration/test_creational_patterns.py
```

시험은 임시 저장소와 합성 학생을 사용합니다. 정상 코드, 미완성 코드, 0점 코드, 대표 오답, 웹용 자료 등록·검증, 학생 수령·다운로드·제출·결과 조회를 확인합니다. 로컬 실행은 보안 격리가 아니며, 이 시험에 검토하지 않은 학생 코드를 추가하지 않습니다. 실제 Windows IDE와 CMake 실행 여부는 별도로 확인해야 합니다.

### 검증 기록 — 2026-09-29

- 실습·평가 요소 관련 자동 테스트 57개 통과: 실습 14개, 평가 요소 기능 43개
- 교수자 편집 화면 브라우저 시나리오 35개 통과: 평가 요소 입력·복사, 반응형 배치, 입력 보호 등
- 정답 100점, 미완성 코드·검증용 오답 0점 확인
- 대표 오류 8종에서 대응 평가 요소와 수정 힌트 확인
- README 예제 2개, 200개 명령 처리와 새 프로세스의 일련번호 초기화 확인
- 임시 수업에서 자료 등록·검증·공개, 학생 수령·다운로드·제출·재제출 확인
- 재제출 0점 결과와 이전 최고점 100점 분리, 비공개 자료 미노출 확인

전체 관련 테스트 재실행:

```sh
python -m pytest -q tests/integration/test_creational_patterns.py \
  tests/unit/test_assignment_evaluation.py \
  tests/integration/test_instructor_evaluation.py
```

검증 환경은 macOS의 C++17 컴파일러와 로컬 임시 서버입니다. 운영 서버 등록·배포나 실제 Windows Visual Studio 실행을 검증한 기록은 아닙니다.
