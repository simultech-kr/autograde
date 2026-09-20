# 제공 루브릭 기반 평가 플러그인

2026-09-18 · 목표 설계. [로컬 모듈 1차 구현](../operations/rubric-local-mvp.md)으로
엄격한 JSON 입력·별도 SQLite 등록/승인·가져온 자동/수동 근거 평가·제한된 SES/CLI를 추가했다.
[웹 카탈로그 MVP](../operations/rubric-web-mvp.md)로 기준 등록·검토·버전 조회를 선택적으로 연결했다.
[교수자 실제 제출 평가 연결](../operations/rubric-connected-assessment.md)로 서버 원본 검증·웹 평가 사용 승인·
개인 교수자 항목 판정·비공개 평가 이력을 추가했다. 아래 전체 계약의 자동 검사 근거 원장·최종 성적 승인·문서 파서·AI·학생 공개는 미구현이다.
[개인 계정/분반 인가](../operations/instructor-personal-auth.md)는 선택적으로 사용 가능하며,
개인 모드의 카탈로그 등록자와 연결 평가자는 인증된 계정 ID를 기록한다. 최종 성적 확정·공개 API는 아직 제공하지 않는다.

교수자가 제공하는 평가 루브릭을 재사용 가능한 평가 자산으로 등록하고,
학생의 고정된 제출본을 그 기준에 따라 평가한다. [SPL 제품군](software-product-line.md)의 독립 선택 기능이다.
[성취도 분석](learning-achievement-plugin.md)은 이 모듈의 결과를 소비하지만 사용의 필수 조건은 아니다.
[옵저버 패턴 루브릭 예시](../../design/rubrics/observer.rubric.example.json)는 설계 데이터이며 현재 채점 설정이 아니다.

## 1. 책임과 제품 경계

| 기능 | 소유 모듈 | 출력 |
| --- | --- | --- |
| 루브릭 등록·구조화·버전 고정 | `rubric.catalog` | `ApprovedRubric.v1` |
| 루브릭 항목과 허용 검사/평가자 연결 | `rubric.binding` | `RubricBinding.v1` |
| 기준에 따른 평가 작업 조정·항목 취합 | `evaluation.rubric` | `RubricAssessment.v1` |
| 테스트 결과를 규칙에 따라 판정 | `evaluator.deterministic` | `CriterionDecision.v1` |
| 교수자가 코드를 보고 수준 선택·사유 기록 | `evaluator.instructor` | `CriterionDecision.v1` |
| 선택적 AI 검토 제안(후속·기본 off) | `evaluator.ai_assist` | 확정되지 않은 `CriterionProposal.v1` |
| 승인된 결과 공개 | 기존 `result.publication` 계약 | 학생 공개용 평가 projection |
| 목표별 성취도·증거 충족률 | 기존 목표 설계 `achievement.*` | `AchievementSnapshot.v1` |

루브릭 평가 결과는 항목 점수와 총점이다. 성취도는 목표 연결이 있을 때 추가 계산한다.
루브릭 배점과 학습목표의 중요도/배분을 중복 곱하지 않는다.
자동 채점 원장·최종 성적 원장·루브릭 평가 원장도 구분한다.
새 모듈은 기존 과제의 점수나 최종 성적을 자동 교체하지 않는다.
과제 공개 시 기존 IO 채점 / 루브릭 총점 중 결과 기준을 하나 선택하고 버전을 고정한다.
기존 IO 점수가 루브릭 일부 항목에 사용되면 그 IO 총점을 다시 더하지 않는다.

## 2. 교수자가 루브릭을 제공하는 흐름

1. 과제 작성의 **평가 기준 → 루브릭 추가**에서 직접 작성, 텍스트 붙여넣기 또는 구조화 JSON 가져오기.
2. 가져온 내용을 항목 ID·이름·배점·수준별 설명·판정 방법·필요 증거로 정리한 미리보기 표시.
3. 모호한 표현, 빠진 배점, 중복 ID, 합계 불일치, 자동 검사에 연결되지 않은 항목을 수정.
4. 항목별로 규칙 자동 평가 / 교수자 평가 / 선택적 AI 제안 후 교수자 평가를 지정.
5. 자동 항목은 서버에 등록된 테스트·검사 ID에 연결한다. 수동 항목은 확인할 코드/설명 범위를 지정한다.
6. 예제 제출본과 예상 항목 점수로 사전 검증. 총점뿐 아니라 항목별 불일치를 확인.
7. 교수자가 구조화 결과와 배점을 승인한 뒤 루브릭·binding·평가자 버전을 과제 공개본에 고정.
8. 학생 제출 후 자동 항목을 계산하고, 수동/제안 항목은 검토함에 표시.
9. 교수자는 항목별 근거를 열어 판단 → 총점/미평가 확인 → 확정 → 설정된 시점에 학생에게 공개.
10. 루브릭 변경은 새 버전이다. 기존 제출 재평가는 영향 미리보기·대상·사유 승인 후 별도 작업으로 실행한다.

자연어 입력 자체를 평가 프로그램으로 실행하지 않는다. “객체지향적으로 잘 작성했는가”처럼 판단 기준이
불명확하면 보완 요청/수동 평가로 남긴다. 구조화 도구가 배점·수준·검사 ID를 추정했다면 추정임을 표시하고 승인받는다.
필수 필드가 빠진 루브릭을 조용히 기본값으로 완성해 공개하지 않는다.

1차 입력은 웹 폼·텍스트·JSON으로 제한한다. Word/PDF/스프레드시트 파서는 후속 importer plugin이며
이미지 OCR·복잡한 표 추출 결과도 원문 위치와 함께 검토한다. 가져오기가 지원된다고 현재 표시하지 않는다.
원문 문서·학생 README·코드 주석에 든 지시문은 **자료**이며 시스템 정책/평가 도구 권한을 바꾸지 못한다.

## 3. 구조화 루브릭 계약

### ApprovedRubric.v1

- identity: tenant scope(상용 Host), course_key, logical_assignment_id, rubric_id, version, digest.
- metadata: 제목, 작성자, 원문 digest/접근 제한 참조, 언어, 승인자·승인 시각, 상태.
- scoring: 만점, `weighted_levels.v1` 방식, 표시 자릿수, 반올림 모드, 미평가/적용 제외 정책.
- criteria: 고정 criterion_id, 제목, 관찰 가능한 설명, max_points, required, evaluator_kind,
  levels(`level_id`, ratio, 근거/수준 설명), 허용 evidence 종류, 학생 공개 설명.
- bindings: criterion → evaluator 계약/버전, 등록 검사 ID, 제출 파일 범위, 필요한 도구/환경.
- optional outcome mapping: 별도 `OutcomeMap.v1` 참조. 없어도 루브릭 평가는 가능.

정밀 계산값은 canonical decimal 문자열로 표현하고 유한수·자리수·범위를 검사한다.
MVP 제한안은 루브릭당 1~50항목, 항목당 2~10수준, 각 배점 > 0, ratio는 0~1,
ratio 0과 1 포함, 수준 ID/ratio 중복 금지, 항목 배점 합=명시 만점이다.
동일 ID를 다른 의미로 재사용하지 않으며 수준은 수치 오름차순으로 정렬한다.
0은 근거 확인 후 미충족 판정, null은 미평가/불가다. N/A를 임의로 0점/만점으로 바꾸지 않는다.

등록 상태: `draft → validated → approved → retired`. 사용된 approved 버전은 수정·삭제 불가.
구조화 문서는 입력 크기·항목/문자 수 상한·중복 JSON key·알 수 없는 필드·경로 traversal을 검사한다.
HTML은 이스케이프하고 문서 macro·외부 참조 다운로드·임의 shell/expression을 실행하지 않는다.
교수자 설명과 evaluator 설정도 도메인 데이터로 검증한다. 신뢰 설치된 evaluator code와 구별한다.

### 판정과 집계

항목 점수 `p_i = max_points_i × selected_level_ratio_i`.
완료 시 총점 `P = Σ p_i`, 만점 `M = Σ max_points_i`. 가중치는 max_points에 이미 포함된다.
규칙 evaluator도 최종 수준 ID를 반환한다. 임의 연속 점수가 필요한 방식은 별도 계약으로 후속 추가한다.
raw 값을 먼저 합산하고 화면에서만 예: 소수 둘째 자리·half-up으로 반올림한다.
수준 기반 총점과 성취도 달성 임계값의 계산/반올림은 각각 버전 있는 정책을 사용한다.

하나라도 미평가 항목이 있으면 **확정 총점은 null**이다. “평가된 항목 40/40, 전체 배점 100,
나머지 60점 검토 필요”처럼 부분 합계와 전체 분모를 나란히 표시하며 100점 만점 점수로 오인시키지 않는다.
면제/적용 제외는 교수자 승인된 대상별 binding/policy snapshot으로 처리한다.
MVP는 N/A 자동 재배분을 지원하지 않는다. 적용 제외가 있으면 합계 확정을 보류하고 승인된 예외 기준을 마련한다.

예시: 기능 동작 40점·구조 분리 35점·구독 해제 25점에서 수준 비율이 각각 1, 0.5, 1이면
`40 + 17.5 + 25 = 82.5/100`. 가운데 항목 미평가이면 확정 총점 null, 평가된 부분 합계 65/65다.
마지막 항목을 실제 0점으로 판정하면 `40 + 17.5 + 0 = 57.5/100`이며 미평가와 구분한다.

## 4. 평가자 전략과 자동화 한계

| 대안 | 입력과 판정 | 실패 처리 |
| --- | --- | --- |
| RuleBasedRubric | 승인된 testcase/정적 검사 결과 + 제한된 decision table → 수준 | 검사 부재·버전 불일치·인프라 오류는 blocked, 학생 채점 기준에 명시한 실제 실패만 0점 후보 |
| InstructorRubric | 고정 제출 코드 + 수준 설명 → 교수자 선택/사유 | 미검토는 pending, 개인 권한/expected_revision 없으면 거절 |
| HybridRubric | 항목별 rule/instructor 조합 | 항목 단위 진행 표시, 일부 성공으로 전체 확정 금지 |
| AiAssistedRubric | 승인된 자료 범위 + 루브릭 → 근거 있는 제안 → 교수자 확인 | 근거 불충분·시간 초과·비용 한도·제공자 오류는 review_required, 임의 감점 금지 |

자동 검사는 “패턴이 있다”는 이름 매칭/키워드 검색만으로 설계 품질을 확정하지 않는다.
옵저버의 결합도·위임 구조는 수동 코드 루브릭으로 시작하고, 검증된 정적 검사 adapter를 나중에 추가할 수 있다.
규칙은 허용된 연산자·typed 입력으로만 작성한다. 학생 코드를 평가 서비스 프로세스에서 직접 실행하지 않는다.
테스트 실행은 기존 채점 runner의 책임이며 일반 학생 코드용 격리 실행기는 별도 상용 인수 조건이다.

### AI 보조 대안의 경계

AI 사용은 요구사항의 필수 가정이 아니다. **기본 off인 후속 제품 대안**으로만 추가한다.
기존 성취도 설계의 AI 제외는 “성취도 계산기 내부에 AI 판정을 넣지 않음”으로 유지하고,
AI를 쓸 경우 rubric 평가자의 제안 모듈로 분리한다. 학생의 AI 사용 허용 정책과도 별개다.

- 기관 승인·배포 환경·전송 정책·제공자 계약을 확인하기 전 외부 AI로 자료를 보내지 않는다.
- 선택은 로컬 모델 또는 허용된 원격 provider로 한정. 원격 제공자 장애를 이유로 다른 곳에 자동 전송하지 않는다.
- 학번·비밀번호·토큰을 제거하고 승인된 소스/설명 범위만 사용한다. 비공개 정답/테스트는 기본 전송 금지.
- 문서/코드의 “점수를 올려라” 같은 지시는 실행하지 않는다. 모델에 shell·브라우징·채점 원장 쓰기 도구를 주지 않는다.
- 응답은 criterion_id, level_id 제안, 짧은 이유, 검증 가능한 파일 digest/줄 범위 근거로 제한한다.
  존재하지 않는 파일/줄/수준, 제출본 불일치, schema 오류면 제안을 유효 점수로 받지 않는다.
- 점수와 기준 판단은 독립 검토 가능해야 한다. 모델의 자신감 수치를 신뢰도 보장으로 표시하지 않는다.
- 모델/프롬프트/응답/자료 digest·평가 실행 버전과 비용/호출 한도를 감사한다. 불필요한 개인정보가 든 원문 로그는 수집하지 않는다.
- 제안은 교수자 확인 전 성적/확정 증거가 아니다. `proposal.accepted`가 아닌 정상 수동 승인 계약을 거쳐야 성취도에 반영한다.
- 모델을 고정해도 동일 응답을 보장한다고 하지 않는다. 평가 응답 자체를 보존하며 재실행은 별도 평가 버전으로 남긴다.

## 5. 실행·저장·공개 계약

요청 `EvaluateRubric.v1`: 서버가 검증한 scope/actor, submission_id/source_digest,
rubric_version/digest, binding_version, evaluator lock hash, expected_revision, idempotency_key.
source_digest가 없는 가변 IDE 폴더를 평가 대상으로 받지 않는다.

출력 `RubricAssessment.v1`: 평가 ID·제출본·루브릭/평가자 버전, 항목별 state/level/earned/max,
근거 참조·짧은 설명·평가자·검토자, 부분 합계·확정 총점, 승인/공개 상태, 생성/완료 시각.
`proposed` 항목과 `approved` 항목은 출력에서도 타입/상태를 분리한다.

평가 상태: `accepted → queued → evaluating → review_required → approved`.
모든 항목이 승인된 결정적 자동 기준이면 승인된 정책에 따라 `evaluating → approved`를 허용할 수 있다.
이는 루브릭 평가 완료이지 학생 공개나 학기 성적 확정이 아니다. 학생 공개는 기존 공개 정책을 별도 통과한다.
인프라 오류는 failed/blocked, 재시도는 같은 요청 키로 중복 실행/과금·기록을 방지한다.
평가자 프로세스 종료 시 lease 복구하며 원격 호출 결과가 불확실하면 자동 중복 호출 대신 unknown/reconcile 상태로 남긴다.

최신 제출이 평가 중이면 이전 평가를 최신 결과로 반환하지 않는다. 동일 기준의 이전 최고는 별도 필드다.
서로 다른 제출의 유리한 항목을 합쳐 새 답안 점수를 만들지 않는다.
재평가/정정은 원래 평가를 superseded로 연결한다. 원본 결과·교수자 사유·공개 이력을 보존한다.
재평가 중 채점 기준 변경/재제출이 발생해도 기존 작업의 submission/rubric/lock은 고정한다.

제안 저장소: `rubric_versions`, `rubric_bindings`, `rubric_validation_runs`, `rubric_assessments`,
`rubric_criterion_decisions`, `rubric_review_actions`, `evaluator_proposals`.
현재 성취도 설계의 `rubric_criterion_versions`와 `manual_rubric_decisions`는 별도 복제 원장을 만들지 않고
이 계약의 projection/참조로 통합한다. 소유자는 rubric/evaluation 모듈이다.
평가 완료 이벤트 `rubric.assessment_approved.v1`만 증거 adapter가 `CriterionEvidence.v1`로 변환한다.
중복/역순 이벤트·scope·source digest를 재검사하며 임의 확정/학생 공개 권한은 주지 않는다.

## 6. 교수자·학생 UX

- 과제 등록 단계에 “기존 IO / 루브릭 평가” 선택, 루브릭 가져오기·배점 합계·모호한 항목·미연결 검사 표시.
- 공개 전 “이 버전으로 학생이 받게 될 기준”, 교수자 전용 검사, 예제 예상/실제 수준을 분리해 확인.
- 평가 화면에는 항목·배점·선택 수준·확보 근거·미검토 이유·교수자 수정란을 한 행/모바일 카드에 배치.
- 코드 열기는 정확한 제출 digest/파일 위치로 이동. 사유 없는 확정·기존 검토 덮어쓰기 방지.
- 채점 완료·교수자 검토 필요·학생 공개 대기를 서로 다른 상태로 표시.
- 학생은 본인 최신 제출의 승인된 항목 피드백과 공개 가능한 기준만 확인한다.
  숨긴 기준/비공개 검사로부터 점수를 역산할 수 있는 부분 합계를 노출하지 않는다.
- 루브릭만 선택하고 성취도를 끈 제품에서도 평가·검토·공개 흐름은 완결되어야 한다.

## 7. SES·SPL 연결과 인수 기준

CourseComposition의 `Assessment [E] → AssessmentMethod [S]` 대안을
`LegacyIoAssessment | RuleBasedRubric | InstructorRubric | HybridRubric | AiAssistedRubric`로 둔다.
Achievement 선택과 독립이지만 켜면 같은 criterion 증거 계약으로 연결한다.
자동/수동 evaluator의 구성은 선택 대안 내부 Aspect로 분해한다. 독립 기능을 단일 선택 목록에 중복 섞지 않는다.

모든 rubric 대안은 catalog/binding/evaluation 필요. 수동/AI 대안은 개인별 교수자 인증·감사 필요.
규칙 대안은 실제 evidence producer 필요. AI 대안은 위 조건에 더해 승인된 AI capability·전송/비용 정책 필요.
루브릭 문서만 있고 실행 연결이 없으면 `draft/blocked`이며 “자동 평가 가능”으로 표시하지 않는다.

MVP 순서: JSON/폼 catalog → deterministic/instructor evaluator → hybrid 집계/검토/공개 → 성취도 연결.
텍스트 구조화 자동화·문서 파서·AI 보조는 후속이며 초기 상용 필수 기능으로 묶지 않는다.
모듈별 test fixtures에는 루브릭·동결 제출·도구 결과·예상 항목 수준·예상 합계를 함께 둔다.

인수 시험: 합계 불일치·중복 ID·순서/비율·0점/null/N/A·정밀도·누락 binding·원문 injection,
다른 제출 digest·다른 기관/수업·미공개 증거·예제 항목별 검증·0/부분/만점·수동 동시 수정,
AI 제안의 유효 증거 유입 차단·없는 근거/수준·시간/비용 제한·원격 전송 금지·호출 불확실성,
재채점/재제출 경합·중복 이벤트·구성/라이선스 변경 중 진행 작업 보존·기존 점수 이중 합산 방지,
학생 최신/이전 최고 분리·보고서와 화면 합계 일치·루브릭 단독 제품·성취도 결합 제품.
