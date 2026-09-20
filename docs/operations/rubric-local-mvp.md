# 루브릭 모듈 1차 구현: 로컬 시험

2026-09-18 · 기존 학생 서비스와 **분리된 CLI/SQLite 모듈**이다.
루브릭을 등록·승인하고 가져온 자동 검사 결과/수동 판정을 항목별 점수로 계산할 수 있다.
별도 후속 [교수자 웹 카탈로그](rubric-web-mvp.md)는 기준 등록·검토·조회만 제공한다.
실제 제출 원장/runner 연결, 학생 결과 공개, 개인별 인증, 전체 제품군 Host는 아직 연결하지 않았다.
여기서의 미연결은 **로컬 평가 CLI** 기준이다. 별도의 [개인 교수자 인증 모듈](instructor-personal-auth.md)은
웹 카탈로그·수업 관리에 선택적으로 연결했으며, 로컬 평가 근거를 검증된 서버 근거로 바꾸지는 않는다.

## 무엇을 시험할 수 있나

| 기능 | 현재 동작 |
| --- | --- |
| 루브릭 입력 | 엄격한 `autograde.rubric.v1` JSON. 문장/문서 자동 변환은 미구현 |
| 등록·승인 | 별도 SQLite에 저장, 같은 버전 변경 거절, 예상 digest를 지정하여 승인 |
| 자동 항목 | 가져온 passed/failed 결과의 개수를 승인한 수준에 매핑. 학생 코드/컴파일은 실행하지 않음 |
| 수동 항목 | 입력된 수준·검토자·사유·제출 digest·파일/줄 근거를 검사하고 취합 |
| 미평가/오류 | 미검토·검사 대기·인프라 실패는 총점 null. 부분 합계와 분모를 별도로 반환 |
| 재시도/이력 | 같은 요청 키·같은 입력은 같은 평가. 변경 입력은 새 키, 이전 평가 보존 |
| 제출본 고정 | 같은 submission ID의 과제·source digest·파일 참조 변경 거절 |
| 검사 실행 고정 | 동일 submission/rubric/grading_run ID의 테스트 결과 변경 거절. 새 실행 ID 필요 |
| SES | 루브릭 하위 모델의 규칙/수동/혼합 specialization을 선택한 PES 미리보기와 구성 해시 |

모든 실행은 `offline_operator`다. **JSON의 검사 결과·검토자·파일 해시는 운영자가 제공한 미검증 주장**이다.
파일이 실제 해당 코드인지, 교수자가 실제 로그인했는지, 검사 프로그램이 실행되었는지는 검증하지 않는다.
`--actor`는 감사 메타데이터이지 인증이 아니다. 이 CLI를 학생에게 노출하거나 업로드 API로 감싸면 안 된다.
로컬 DB/입력 파일에 접근할 수 있는 운영자만 시험한다. 개인별 교수자 인증과 서버 원장 조회 adapter가 평가 흐름에 연결되기 전
운영 성적의 권위 있는 근거로 사용하지 않는다. 실제 교육 성적을 자동 변경하는 기능은 없다.

평가 결과에는 `evidence_provenance=operator_supplied_unverified`, `publication_allowed=false`,
`gradebook_updated=false`가 명시된다. 평가 상태 `evaluated`는 가져온 자료의 계산 완료이지 교수자 신원 확인이나 공개 승인이 아니다.

## 예제 파일

- [루브릭](../../examples/rubric/observer.rubric.json): 자동 40점 + 교수자 항목 35/25점.
- [혼합 평가 profile](../../examples/rubric/hybrid.profile.json): come2201, HybridRubric.
- [합성 평가 근거](../../examples/rubric/synthetic.evidence.json): 두 검사 통과, 수동 수준 0.5/1.

합성 근거의 `aaaa…`/`bbbb…` 해시와 파일 줄수는 **시험 placeholder**이며 실제 학생 코드와 연결되지 않는다.
예상 값은 40 + 17.5 + 25 = 82.5점이다. 이 예제로 옵저버 패턴 코드 검사의 타당성을 검증했다고 보지 않는다.
`design/rubrics/`의 설계용 JSON은 실행 입력으로 받지 않는다. 반드시 `examples/rubric/`의 별도 runtime 형식을 사용한다.

## 실행 절차

저장소 루트의 기존 Python 환경에서 실행한다. 명령에 환경변수를 선언하거나 Docker를 사용할 필요가 없다.
Windows도 같은 `python -m ...` 명령을 사용할 수 있지만 이번 실제 시험은 macOS에서 수행했다.
설치 후에는 `autograde-rubric` 진입점도 제공된다. 기존 환경에서 추가된 명령이 보이지 않으면
`python -m autograde.rubric_cli`를 그대로 사용하거나 유지보수 절차에 따라 패키지를 다시 설치한다.

### 1. 형식과 구성 확인 — 데이터 변경 없음

```sh
python -m autograde.rubric_cli validate --rubric examples/rubric/observer.rubric.json
python -m autograde.rubric_cli compose --profile examples/rubric/hybrid.profile.json
```

validate는 정상화된 루브릭과 digest를 반환한다.
compose는 `local_evaluation_ready=true`, `runtime_ready=false`, `server_connected=false`다.
전체 Autograde 제품군 배포 기능이 아닌 **고정된 루브릭 하위 SES 모델**이다.
`RuleBasedRubric`, `InstructorRubric`, `HybridRubric`만 선택 가능하며 AI/임의 plugin 경로는 거절한다.

### 2. 새 전용 DB 초기화

```sh
mkdir .data-rubric-demo
python -m autograde.rubric_cli init --database .data-rubric-demo/rubric.sqlite3
```

이미 있는 DB에는 init이 실패한다. 재실행 시 파일을 삭제하지 말고 기존 DB를 계속 사용하거나 새로운 시험 경로를 선택한다.
운영 서버 DB를 지정하지 않는다. 별도 application/schema marker가 없는 DB에는 읽기/쓰기 요청을 거절한다.
없는 DB를 조회/등록한다고 자동 생성하지 않는다. 새 파일은 생성 시 가능한 플랫폼에서 소유자 전용 권한을 요청한다.
DB 초기화 중 오류로 불완전 파일이 남으면 새 시험 경로를 사용하고 원본을 먼저 점검한다.

### 3. 등록 후 정확한 버전 승인

```sh
python -m autograde.rubric_cli register --database .data-rubric-demo/rubric.sqlite3 --rubric examples/rubric/observer.rubric.json --actor pilot_instructor
```

출력의 `result.digest` 값을 아래 `등록결과의_digest`에 복사한다. placeholder 자체를 입력하지 않는다.

```sh
python -m autograde.rubric_cli approve --database .data-rubric-demo/rubric.sqlite3 --course come2201 --rubric-id observer_cpp_review --version 1 --actor pilot_instructor --digest 등록결과의_digest
```

digest가 다르면 승인되지 않는다. 루브릭은 등록 시점부터 버전별 불변이며 변경은 새 version으로 등록한다.
승인은 구조화 기준에 대한 **로컬 운영자의 확인 기록**이다. 실제 예제 소스 실행 검증이나 학교 권한 인증을 대신하지 않는다.
예제 루브릭을 변경했다면 evidence의 rubric_digest도 새 validate 결과에 맞추고 새 버전을 승인한다.

### 4. 합성 제출 근거 평가

```sh
python -m autograde.rubric_cli evaluate --database .data-rubric-demo/rubric.sqlite3 --course come2201 --rubric-id observer_cpp_review --version 1 --actor pilot_instructor --profile examples/rubric/hybrid.profile.json --evidence examples/rubric/synthetic.evidence.json --request-key demo_assessment_01
```

정상 결과: `status=evaluated`, `total="82.5"`, `display_total="82.50"`, `maximum_points="100"`.
출력의 assessment_id로 정확히 그 평가를 조회한다.

```sh
python -m autograde.rubric_cli result --database .data-rubric-demo/rubric.sqlite3 --course come2201 --assessment-id 평가결과의_assessment_id
```

같은 평가 명령을 그대로 반복하면 같은 assessment_id를 반환한다. 리뷰를 바꾸면 새 request-key가 필요하다.
테스트 결과가 바뀌면 evidence의 grading_run_id도 새 ID로 바꿔야 한다.
서로 다른 제출본을 합치지 않으려면 새 source에는 새 submission_id를 사용한다.
현재 CLI는 자동 “최신/최고점” 선택이 아니라 평가 ID별 조회다. 운영 서비스의 최신/최고 결과 API는 변경하지 않았다.

## 오류 시험

예제 파일은 복사본을 만들고 수정한다. 실제 학생 자료·운영 DB로 시험하지 않는다.

- 수동 review 1개를 빼면 `review_required`, total null, 부분 합계만 반환.
- `level_id=not_met`이면 유효한 0점으로 계산. 미평가와 다름.
- 검사 결과를 infrastructure_error로 바꾸면 blocked이며 총점을 0으로 만들지 않음.
- rubric_digest, source_digest, 파일 digest, 줄 범위 또는 course가 틀리면 평가 저장 거절.
- 같은 rubric version의 배점을 바꾸면 등록 충돌. 새 version 필요.
- 다른 입력으로 같은 request-key를 쓰면 충돌. 이전 평가는 그대로 유지.
- Hybrid 루브릭을 RuleBasedRubric profile로 평가하면 필수 evaluator 누락으로 거절.
- N/A 자동 재배분·선택적 항목·AI·설계용 profile·임의 코드 규칙은 미지원 오류.

오류 시 종료 코드 2와 JSON 오류를 반환한다. 파일 내용·원시 SQLite 오류·자격증명을 오류 출력에 그대로 넣지 않는다.
일반 도움말/명령 형식 오류는 Python argparse 형식으로 표시된다.

## 구현 구조와 다음 단계

- `rubric_engine.py`: 엄격한 루브릭/근거 계약, 고정된 evaluator registry, 정밀 십진수 계산.
- `rubric_store.py`: 전용 SQLite, 버전/승인 digest, 멱등 평가·제출/검사 실행 고정·로컬 감사.
- `rubric_composition.py`: 제한된 SES 모델의 선택/pruning·PES 해시. 상용 Host/사용권 검증과 구별.
- `rubric_cli.py`: JSON 출력·명시적 경로의 로컬 시험 진입점.

웹 기준 카탈로그와 선택적인 개인 인증/분반 권한은 후속 구현으로 추가했다. 다음은 서버가 신뢰하는 실제 제출/검사 adapter →
동결 과제 binding과 예제 실행 검증 → 교수자 판정·승인 결과 공개 → 성취도/제품군 Host 연결 순서다.
현재 독립 저장소는 향후 서버 DB migration과 동일한 schema라고 가정하지 않는다. 명시적 전환 도구와 검증이 필요하다.

## 검증

```sh
python -m pytest -q tests/unit/test_rubric_engine.py tests/integration/test_rubric_local_workflow.py tests/unit/test_management_ses.py tests/unit/test_product_design_assets.py
```

신규 엔진/로컬 workflow 68개 + 기존 SES/설계 예시 49개 = **117개 통과**.
별도 DB와 실제 CLI 왕복, 동시 중복 요청 25개, 잘못된 입력/버전/근거·권한 범위 인자·기존 DB 보호를 검사했다.
동시 중복 요청 시험은 **학생 25명 채점 성능 시험이 아니다**. 웹 로그인·Windows runner·실제 코드 평가·상용 운영 시험도 아니다.

전체 회귀 시험은 **1125 passed, 19 skipped (267.19초)**.
생략은 Windows/MSVC 2개, 작동하는 JDK 없음 5개, PATH에 .NET 8 SDK 없음 12개다.
전체 시험 실행 중 추가한 caller 입력 복사 보완 후에도 관련 117개 시험을 다시 통과했다.
