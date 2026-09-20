# 전체 기능의 플러그인화와 SES 조합 설계

2026-09-18 · **설계안. 런타임 전환·DB 변경·운영 적용은 하지 않았다.**

성취도 평가를 포함한 모든 업무 기능을 교체 가능한 계약으로 분리하고,
System Entity Structure(SES, 체계요소구조)로 가능한 조합을 정의한다.
현재의 [관리 SES](management-ses.md)는 유지하고 전체 플랫폼 구성으로 확장한다.
[성취도 평가 상세](learning-achievement-plugin.md)와 [설계용 구성 예시](../../design/ses/platform-composition.example.json)를 함께 본다.

사업화 확장: [제공 루브릭 평가](rubric-evaluation-plugin.md)와 [SES 소프트웨어 제품군(SPL)](software-product-line.md)을 추가했다.
수업별 설정을 넘어 공통 자산·제품 template·고객 사용권·지원 조합·배포 lock으로 제품을 도출한다.

## 1. 결정 사항

1. **모든 업무 기능은 플러그인**, 인증·인가·원장 보호 등의 필수 기능도 고정 계약을 가진 필수 플러그인으로 표현한다.
2. 플러그인을 실행하고 권한을 강제하는 최소 Host Kernel은 제거하거나 수업 설정으로 교체할 수 없다.
   “모든 기능을 플러그인화”는 모든 함수를 별도 패키지로 나누거나 인증을 끌 수 있게 한다는 의미가 아니다.
3. 초기에는 **모듈형 단일 서버 + 제한된 작업자**로 구성한다. 기능마다 서버를 추가하는 마이크로서비스 전환은 하지 않는다.
4. 기존 구현은 Adapter로 감싸고 점진적으로 추출한다. 학생 API·제출 ID·수업 key·기존 결과를 유지한다.
5. 서버·명단 CSV와 SQLite를 유지한다. 중첩 조합만 별도 버전 있는 JSON으로 표현하며 환경변수를 추가하지 않는다.
6. 운영자가 설치·승인한 플러그인만 사용한다. 교수자는 설치된 대안과 허용된 설정을 조합한다.
7. 성취도 분석 실패가 제출 접수를 막거나 기존 점수를 바꾸지 않게 한다.
8. Canvas/LTI는 제외한다. 컨테이너 채점은 후속 대안이며 현재 파일럿에 강제 도입하지 않는다.

## 2. 현재와 목표의 차이

현재 `management_ses.py`는 관리 모듈 6종의 선택·파라미터·의존성을 검사하고 PES **미리보기**만 만든다.
`MODEL_BASE`는 실행 가능한 플러그인 레지스트리가 아니며 결과는 `runtime_ready=false`다.
현재 검증기는 수업당 정확히 6개 관리 모듈을 요구하므로 성취도/서버 모듈을 기존 JSON에 임의로 추가하면 거절된다.

이번 문서는 새로운 `autograde.platform.ses.v2` / `profile.v2` / `pes.v2` 계약의 목표를 정의한다.
설계 예시는 다른 schema ID를 사용하며 **기존 CLI나 pilot CSV에 입력하지 않는다.**
v1→v2 변환기, v2 검증기, 실행 Host, plugin SDK, 성취도 계산기와 화면은 후속 구현이다.
기존 서비스가 있다는 사실을 해당 플러그인의 구현·호환성 시험 완료로 표시하지 않는다.

1차 구현 진척: [독립 루브릭 모듈](../operations/rubric-local-mvp.md)은 로컬 operator 입력으로 동작한다.
전체 v2 대신 `autograde.rubric.ses.v1`의 고정 하위 모델을 제공하며 플랫폼 activation은 계속 false다.
후속 [웹 기준 카탈로그](../operations/rubric-web-mvp.md)는 별도 선택 설정으로 입력·검토·버전 조회만 연결한다.
공유 계정에서 평가 승인·근거 import·학생 공개를 열지 않으며 SES 제품 Host 활성화와 구분한다.
[개인 교수자 인증/분반 권한](../operations/instructor-personal-auth.md)을 고정된 선택 provider로 추가했다.
공유/개인 모드 선택과 서비스 인가 포트를 제공하되 전체 v2 Host나 평가 승인 활성화로 간주하지 않는다.

## 3. 플러그인 계층과 책임

| 계층 | 역할 | 교체 범위 |
| --- | --- | --- |
| Host Kernel | 승인된 registry, scope 강제, 계약 검사, 구성 활성화, 자원 제한, 실패 차단 | 배포 버전으로만 변경 |
| 필수 provider | 인증, 수업 권한, 저장소, 감사, 수업·수강, 제출 원장 | 호환 구현 선택 가능. 실행 프로필에서 비활성 불가 |
| 업무 plugin | 과제·자료 배포·채점·성취도·보고서·보관·지원 | 수업 목적에 따라 선택·조합 |
| UI contribution | 교수자/학생 메뉴·화면·작업 버튼 | 선택된 기능·권한·데이터 상태에 따라 노출 |
| Client adapter | VS Code, Visual Studio, 웹, CLI | 서버 계약과 버전 협상. 클라이언트 실행 파일은 별도 배포 |

Host는 인증된 Principal과 서버가 확인한 CourseScope를 주입한다. 요청 본문의 `course_key`나
클라이언트가 숨긴 버튼을 권한 근거로 사용하지 않는다. 수업 경계와 공개 정책은 모든 읽기·쓰기·파일 접근에 적용한다.
플러그인 장애 시 인증·인가를 우회하는 fallback은 없다. 쓰기 경로는 안전하게 거절하고 선택적 분석만 격리한다.

### 전체 기능 카탈로그와 기존 코드의 연결

아래 ID는 **목표 계약 ID**다. `기존 기반`은 재사용 후보이며 plugin 구현 상태가 아니다.

| 기능군 / 목표 ID | 기존 기반 | 선택·의존성과 범위 |
| --- | --- | --- |
| `identity.roster_password` | platform_auth, pilot_roster, enrollment credential | 학생 인증 필수, 수령 코드/세션 권한과 분리 |
| `identity.instructor_shared` / `identity.instructor_personal` | 공용 Basic 인증 / 개인별 인증 미구현 | 파일럿 대안 / 개인 판단·공동 운영 선행조건 |
| `authorization.course` | 기존 서비스의 수업·학생 검사 | 필수, 최소 권한을 낮출 수 없음 |
| `audit.events` | course_admin 감사·platform_events 운영 로그 | 필수, 기존 로그를 내구성 이벤트 원장으로 오인하지 않음 |
| `storage.sqlite` / `artifacts.local_digest` | platform_state, platform_bundle | 필수 기반, 마이그레이션·불변 원본 보장 |
| `course.catalog` / `enrollment.roster_csv` | course_admin, course_runtime, pilot_roster | 교과목 개설·학기·분반·명단·비활성·비밀번호 초기화 |
| `assignment.authoring` / `assignment.validation` | assignment_admin, instructor_upload, grader | 문제·starter·정답·오답·테스트·초안 검증 |
| `assignment.release` / `lifecycle.deadlines` | 공개/복제/숨김/연장, 마감 검사 | 검증한 버전만 공개. 별도 수락 기간·개인 예외는 추가 구현 |
| `delivery.bundle` / `claim.web_code` | platform_portal, platform_qr, platform_service | QR·수령 코드·수락·자료 다운로드 |
| `submission.receipt` | platform_service, platform_state | 필수 불변 접수·중복 방지·현재 제출 식별 |
| `submission.history` / `source.compare` | submission_review, digest 조회 | 제출 시각·버전·점수 증감·코드 비교 |
| `grading.queue` / `grading.io_cases` | course_runtime, platform_bundle_worker, grader | 접수와 실행 분리, 실행 결과→항목별 증거 |
| `runner.pilot_local` / `runner.isolated` | platform_pilot_exec / 후속 격리 실행기 | local은 신뢰 파일럿 전용, 일반 학생 코드 안전성 보장 아님 |
| `result.publication` | 최신/최고 결과, 공개 시점 정책 | 학생 자기 결과·교수자 전용 결과 분리 |
| `outcome.catalog` / `rubric.mapping` | 신규 | 학습목표·평가 항목·과제/목표 연결과 버전 |
| `rubric.catalog` / `rubric.binding` / `evaluation.rubric` | 신규 | 제공 루브릭 구조화·승인·실행 연결·항목 평가/취합. 성취도 없이 독립 사용 |
| `evaluator.deterministic` / `evaluator.instructor` | 기존 검사 기반 Adapter / 신규 | 규칙 자동 판정과 교수자 루브릭 판정 |
| `evaluator.ai_assist` | 후속 선택 대안·기본 off | 승인된 자료 범위의 제안만 생성, 교수자 승인 전 유효 점수 아님 |
| `achievement.criteria` / `achievement.hybrid` | 신규 | 자동 증거만 / 자동 증거 + 교수자 루브릭 확인 |
| `report.html_csv` / 후속 exporter | management SES 계약 | 성적/성취도 snapshot 소비. 파일 형식과 계산 분리 |
| `support.explainable_rules` / `inbox.instructor` | management SES 계약 | 지원 근거·검토·알림. 자동 제재·외부 연락 없음 |
| `archive.verified_bundle` / `restore.verified_bundle` | management SES 계약, 기존 GC 참고 | 무결성·참조·보관 금지 검사. 자동 삭제 기본 금지 |
| `scheduler.manual` / `scheduler.pyjevsim` | scheduler, management SES 계약 | 수동 요청 / 주기 tick·복구 스캔, 작업 실행기는 별도 |
| `operations.health` / `client.diagnostics` | readiness, events, download_diagnostics | 접속·다운로드·큐·용량 진단, 비밀정보 제외 |
| `operations.backup` / `enrollment.reset` | 운영 절차, course_reset | 운영자 승인·백업 필수, 수업 권한만으로 실행 불가 |
| `data.portability` | 신규 공통 계약 | 권한 있는 원본/평가 이력의 기본 내보내기. 고급 보고서 상품·사용권 만료와 분리 |
| `ui.instructor` / `ui.student_portal` | instructor_web, platform_portal, theme | MVC 화면 기여, 서비스 계약만 호출 |
| `client.vscode` / `client.visualstudio` / `client.cli` | 기존 확장·CLI | 능력 협상, 다운로드·접수·결과·업데이트 안내 |

삭제, 성적 확정, 학생 연락 등 영향이 큰 작업은 별도 capability와 확인 절차를 가진다.
예컨대 `archive`를 켰다고 `artifact.delete` 권한을 자동 부여하지 않는다.
업데이트 모듈은 서명/배포 채널·호환 버전을 관리하며 서버 설정만으로 IDE 확장을 원격 교체하지 않는다.
시험 통제·화면 채증은 이 학습/운영 구성에 포함하지 않는다.

## 4. SES 구성 공간

Entity=구성요소, Aspect=구성 분해, Specialization=대안 중 하나 선택,
MultiAspect=같은 템플릿의 수업/분반 반복으로 사용한다.
SES/Model Base의 구분과 이 관계들은 [Wismar CEA SES Toolbox의 모델링 설명](https://www.cea-wismar.de/tbx/SES_Tbx/examplesDoc/ses_tbx_index.html)을 참고했다.
아래는 Autograde용 제한된 dialect 설계이며 해당 도구의 파일 형식 호환·DEVS 실행 모델 생성을 주장하지 않는다.

```text
AutogradePlatform [E]
└─ PlatformComposition [A]
   ├─ HostKernel [E: mandatory]
   ├─ Infrastructure [E]
   │  └─ InfrastructureParts [A]
   │     ├─ Identity [E] → [S] SharedPilotIdentity | PersonalInstructorIdentity
   │     ├─ Storage [E] → SQLiteStorage [필수 provider]
   │     ├─ SecurityAndAudit [E: mandatory providers]
   │     └─ WorkerFleet [E: bounded grading / analysis workers]
   ├─ CourseFleet [E]
   │  └─ CourseInstances [MA: course_offerings]
   │     └─ CourseOffering [E: 내부 course_key, 학년도·학기·분반]
   │        └─ CourseComposition [A]
   │           ├─ CourseAndEnrollment [E: mandatory]
   │           ├─ AssignmentWorkflow [E: 작성·검증·공개]
   │           ├─ Delivery [E] → [S] BundleDelivery | DeliveryDisabled
   │           ├─ Submission [E: receipt·history·source comparison]
   │           ├─ GradingServices [E: queue·evidence producer·runner]
   │           ├─ Assessment [E] → [S] LegacyIoAssessment | RuleBasedRubric | InstructorRubric | HybridRubric | AiAssistedRubric
   │           ├─ Achievement [E] → [S] AchievementDisabled | CriteriaAchievement | HybridAchievement
   │           ├─ Reports [E] → [S] ReportsDisabled | HtmlCsvReports
   │           ├─ LearningSupport [E] → [S] SupportDisabled | ExplainableSupport
   │           ├─ Archive [E] → [S] ArchiveDisabled | VerifiedArchive
   │           ├─ Notifications [E] → [S] InboxDisabled | InstructorInbox
   │           └─ Schedule [E] → [S] ManualSchedule | PyJevSimSchedule
   └─ Presentation [E]
      └─ Channels [A]
         ├─ InstructorWeb [E]
         ├─ StudentWeb [E]
         └─ IdeClients [E: VS Code / Visual Studio adapter sets]
```

이 그림은 축약도다. 실제 모델은 entity/relation 교대, 유일한 template label,
provider 잎의 고정 `plugin_id`를 검증하며 도식의 묶음을 그대로 JSON 노드로 파싱하지 않는다.
서로 독립적으로 동시에 쓸 기능은 Aspect의 서로 다른 Entity로 둔다. Specialization은 toggle 목록이 아니라 단일 대안 선택이다.
선택하지 않는 업무 기능에는 명시적 Disabled 대안을 둔다. 필수 provider에는 Disabled가 없다.

같은 `come2201`이라도 개설 연도·학기·분반이 다르면 별도 `course_key`로 인스턴스화한다.
교과목 코드는 표시용 분류이며 이름이 같다는 이유로 자료나 권한을 병합하지 않는다.
과제별 루브릭·학습목표·마감은 속성/도메인 버전으로 관리한다. 학생마다 플러그인을 설치하지 않는다.

### 선택 제약과 커플링

| 선택 | 반드시 필요한 조건 / 차단 사례 |
| --- | --- |
| 일반 제출 수업 | 배포·수락·접수·채점·결과 provider 모두 필요. DeliveryDisabled는 읽기 전용 보관 수업에서만 허용 |
| 루브릭 평가 | catalog/binding/evaluation 필요. rules는 유효 검사 증거, manual/hybrid는 개인별 교수자 인증 필요 |
| AiAssistedRubric | rubric 평가·교수자 검토 + 명시적 기관 승인·허용 provider·전송/비용 정책 필요. 기본 off |
| CriteriaAchievement | outcome catalog, rubric mapping, 유효 criterion evidence, consistent snapshot 필요 |
| HybridAchievement | 위 조건 + manual rubric + 개인별 교수자 인증/수업 인가/감사 필요. 공유 계정이면 운영 활성화 거절 |
| 성취도 보고서 | AchievementSnapshot 계약 필요. 운영 현황 보고서만 쓰면 성취도 플러그인은 불필요 |
| ExplainableSupport | 지원 규칙이 요구하는 근거 capability 필요. 활동 근거 규칙과 성취도 근거 규칙을 구분 |
| VerifiedArchive | 마감/개인 예외/진행 작업/보관 금지/자료 참조·복원 검증 계약 필요 |
| 주기 보고서·분석·보관 | PyJevSimSchedule + durable jobs/outbox 필요. 스케줄러 off면 수동만 가능 |
| C/C++ Windows 과제 | Windows toolchain 가능 runner 필요. Linux runner로 조용히 대체 금지 |
| 학생 화면 | outcome 공개 정책 + 본인 scope 필수. 비공개 테스트·상담 기록은 기여 화면에서도 차단 |

커플링은 버전 있는 포트 연결이다. 예:
`grading.criteria_finalized → evidence.accept → achievement.recompute → achievement.snapshot_ready → report.render`.
`manual_rubric.revised`도 같은 증거 변환 경계를 거친다. 계산 결과를 입력 원장 이벤트처럼 다시 발행하지 않아 순환을 막는다.
루브릭은 `rubric.assessment_approved.v1 → evidence.accept`로 연결한다. AI proposal은 이 포트에 직접 연결할 수 없다.
필수 동기 의존성과 이벤트 흐름 DAG를 각각 검사한다. 허용한 재계산 트리거도 revision·중복 방지 키를 요구한다.

v2에서는 요청 시각의 제출/수락 마감 검사와 주기적인 마감 후 작업을 분리한다.
`ManualSchedule`이어도 필수 마감 검사는 매 요청에서 수행하며, 자동 보고서·자동 보관 예약은 하지 않는다.
v1의 `DeadlineWindows → PyJevSimScheduler` 제약을 변경한 것이 아니라 새 계약에서 책임을 분해하는 설계다.
v1 profile을 변환할 때 기존 주기 작업 의도를 보존하고 수동 일정으로 자동 축소하지 않는다.

## 5. 플러그인 계약과 Model Base

패키지 버전(`1.2.0`)과 계약 버전(`AchievementSnapshot.v1`)은 분리한다.
Model Base는 승인된 구현·정확한 버전·패키지 digest·manifest·설치 상태를 가진 registry로 발전시킨다.
설정에는 임의 import 경로, shell, Python 표현식, 템플릿 실행 코드를 허용하지 않는다.

```json
{
  "plugin_id": "achievement.hybrid",
  "package_version": "1.0.0",
  "host_api": "autograde.plugin.v1",
  "implementation_status": "contract_only",
  "scope": "course_offering",
  "requires": ["OutcomeMap.v1", "EvidenceSnapshot.v1", "InstructorDecision.v1"],
  "provides": ["AchievementSnapshot.v1"],
  "permissions": ["evidence.read", "outcome.read", "achievement.write_projection"],
  "subscriptions": ["grading.criteria_finalized.v1", "manual_rubric.revised.v1"],
  "settings_contract": "AchievementPolicy.v1",
  "execution": "analysis_worker",
  "ui_slots": ["instructor.course.achievement", "instructor.student.achievement"],
  "migrations": ["achievement.001"],
  "shutdown": "drain_then_stop"
}
```

이 예시는 계약 필드 설명용이다. 실제 설치 manifest는 공급자·서명/digest 검증정보·지원 버전 범위·
입출력 schema·timeout/메모리/동시 실행 상한·UI route·마이그레이션 체크섬까지 검증해야 한다.
`contract_only`는 미리보기에서만 선택 가능하며 활성화는 항상 거절한다.

Host 계약(의사 인터페이스): `describe`, `validate_settings`, `register_ports`, `health`, `drain`, `stop`.
업무 계약: `handle(command, context)`, `query(query, context)`, `consume(event, context)`.
Context는 actor, verified scope, correlation ID, snapshot cursor, 정책/PES hash, 제한된 repository·artifact handle을 제공한다.
Credential repository·전체 DB connection·전체 파일시스템 handle을 분석 플러그인에 제공하지 않는다.

**권한 manifest 자체는 샌드박스가 아니다.** 동일 Python 프로세스의 코드는 Host 메모리·권한에 접근할 수 있다.
1차는 검토·배포된 신뢰 플러그인만 허용한다. 분석을 별도 프로세스로 실행해도 OS 권한·파일 접근 제한 없이는
악성 코드 격리가 아니다. 제3자 임의 업로드/즉시 설치는 지원하지 않는다.

## 6. 구성 확정과 런타임 수명주기

`SES + 명시적 profile → pruning → PES → 계약/권한/설치 상태 검사 → 실행 계획 → 승인 → 활성화`

1. **정적 검사:** JSON schema·깊이·개수·중복·범위·알 수 없는 key 검사. 표현식 평가 금지.
2. **pruning:** specialization 선택과 course multi-aspect 인스턴스화. 단일 선택·누락 없는 필수 provider 검사.
3. **결합 검사:** scope, 포트 버전, 의존성, 순환, runner 자원, route/menu ID 충돌, 데이터 준비 상태 검사.
4. **영향 미리보기:** 이전 PES diff, 학생 UI 변화, 중지 기능, 재계산 대상, migration, 권한 증가, 예상 부하 표시.
5. **실행 고정:** 선택 plugin 버전·digest, 설정, source model/profile hash, 모든 정책 버전으로 lock manifest 생성.
6. **적용:** 운영자 승인·백업 확인 후 migration 수행, 구성 revision CAS와 effective_at으로 새 구성 활성화.
7. **전환:** 실행 중인 채점·분석은 접수 때 고정한 구성을 사용. 새 작업만 새 구성 사용. 강제 unload 금지.
8. **되돌리기:** 이전 조합의 새 구성 버전 생성. DB migration·공개 성적·발송·파일 삭제를 자동 역전하지 않음.

설치됨 / 선택됨 / 검증됨 / 활성 / 기능 저하 / 중지 / 제거는 서로 다른 상태다.
중지하면 새 작업 생성과 UI 진입은 멈추되 기존 증거·결과·감사를 삭제하지 않는다.
의존 플러그인이 있거나 진행 중 작업이 남아 있으면 제거를 거절한다.
registry lock과 schema 버전은 전역, 기능 설정은 수업별이다. 같은 프로세스에서 수업별 상충 라이브러리 버전을 로드하지 않는다.

v2 1차는 **완전한 profile**을 사용한다. 템플릿은 편집 시작점이지 런타임 암묵적 상속이 아니다.
후속 override는 플랫폼 최소 정책 → 수업 개설 → 과제의 허용된 필드만 제한한다.
저장소·identity provider·plugin package는 수업 override 불가. 마감/공개 정책 변경도 별도 도메인 승인 규칙을 통과해야 한다.

## 7. SQLite·이벤트·장애 경계

- 각 플러그인은 자신의 테이블/projection을 소유한다. 타 모듈 테이블 직접 UPDATE/DELETE 금지.
- 도메인 상태 변경과 outbox 삽입은 같은 SQLite 트랜잭션에서 Host UnitOfWork로 처리한다.
- 읽기는 명시된 projection/query 계약을 사용한다. 대형 분석·보고서는 기준 cursor가 있는 snapshot을 소비한다.
- 이벤트 공통 필드: event_id, event_type/version, course_key, subject_id, aggregate_id/revision,
  occurred_at, committed_sequence, correlation_id, policy_hash, payload_refs. 비밀정보·전체 소스는 넣지 않는다.
- at-least-once 전달, `(consumer_id, event_id)` 유일 키로 멱등 처리. 오래된 revision을 최신 결과에 덮어쓰지 않는다.
- 재계산 키는 `(course_key, scope, evidence_cursor, mapping_version, policy_hash, plugin_digest)`로 고정한다.
- 채점 큐와 분석 큐를 분리하고 분석 동시성은 초기 1개로 제한한다. 짧은 쓰기 트랜잭션, bounded busy retry,
  lease 만료 복구, dead-letter와 재실행 UI를 둔다. 장시간 컴파일/분석 중 DB 쓰기 잠금을 유지하지 않는다.
- 성취도 작업 실패는 마지막 snapshot + “갱신 실패/기준 시각”으로 표시한다. 오래된 수치를 실시간 수치처럼 보이지 않는다.
- 학생 접수에는 분석/report/archive 오류를 전파하지 않는다. 접수 필수 provider 실패는 503 등 명확한 실패로 반환하며
  불변 receipt를 저장하지 못했는데 접수 성공을 반환하지 않는다.

제안 테이블: `plugin_installations`, `plugin_schema_versions`, `composition_versions`, `composition_approvals`,
`plugin_jobs`, `plugin_event_inbox`, 공통 outbox와 audit. 성취도 테이블은 별도 상세 설계에 둔다.
마이그레이션은 전역 dependency 순서·체크섬·백업·복구 검사를 거치며 웹에서 설치 클릭 직후 실행하지 않는다.
SQLite 특성상 한 번에 쓰기 작업 하나가 진행되는 제약을 병렬 플러그인 수 증가로 해결했다고 보지 않는다.

## 8. MVC 및 확장 UI 조합

- **Model:** 플러그인별 domain/service/repository 계약. **Controller:** 인증·입력·명령 변환. **View:** 타입 있는 ViewModel만 표시.
- 교수자 공통 shell은 수업 문맥·테마·접근성·오류/진행 안내를 소유하고 plugin은 정해진 slot에 기여한다.
- 메뉴 등록은 `id, label, route, required_capability, order`로 검증한다. 성취도를 끄면 메뉴와 새 분석 요청이 함께 비활성화된다.
- 권한 없는 사용자에게 메뉴를 숨기는 것과 별도로 모든 endpoint에서 동일한 권한을 검사한다.
- 플러그인이 전달한 임의 HTML/JS를 실행하지 않는다. 배포에 포함된 신뢰 view만 허용하고 고정 script hash CSP를 재생성한다.
- UI는 다른 plugin의 DB 구조를 모르며 snapshot 상태·단위·기준 시각을 계약으로 받는다.
- 서버의 capability 응답은 `server_api_version`, `composition_version`, 허용 `capabilities`,
  `minimum_client_version`, 기능별 지원 상태를 제공한다. 비밀정보·내부 plugin 경로는 제공하지 않는다.
- VS Code/VS는 이미 설치된 지원 화면 중 허용 기능만 표시한다. 알 수 없는 필드는 안전하게 무시하되
  필수 제출 계약이 호환되지 않으면 이유와 업데이트 안내를 제공한다. 서버 검사를 우회하지 않는다.
- 기존 확장은 성취도 메뉴 없이 다운로드·제출·현재 점수 조회를 계속할 수 있어야 한다.
  성취도 결과를 기존 점수 필드에 끼워 넣지 않고 별도 endpoint/DTO로 추가한다.

## 9. 단계적 전환과 완료 기준

제품화 생산 순서와 사용권/지원 조합 gate는 [SPL 구현 순서](software-product-line.md#9-구현-순서와-현재-산출물)를 따른다.
첫 세로 기능은 제공 루브릭 등록→검증→평가→검토→공개이며, 이후 성취도와 결합한다.

| 단계 | 범위 | 완료 판정 |
| --- | --- | --- |
| P0 | v2 schema·계약·허용 registry·구성 검증기, 기존 기능을 감싼 Adapter | 기존 API 회귀 동일, 잘못된 조합·미구현 plugin 활성화 거절 |
| P1 | 개인별 교수자/수업 권한, outbox·snapshot, learning outcome·루브릭 증거 | 교차 수업 차단, 이벤트 재생 일치, 제출 원장 불변 |
| P2 | 성취도 플러그인·교수자 검토 화면·보고서 연결 | 손계산 사례 일치, 최신/최고/확정 분리, 기준/근거 추적 |
| P3 | 과제/배포/채점/결과/확장 Adapter의 명시적 registry 전환 | 25명 동시 합성 제출 + 분석 시 접수 손실 0, 기존 비교 기준 대비 지연 측정·승인 |
| P4 | 관리 SES 보관·지원·보고서와 통합, 조합 UI | 설정 diff·승인·예약·drain·복구 훈련 및 실패 격리 |

25명 시험의 지연 기준은 현재 동일 장비의 baseline을 먼저 기록한 뒤 고정한다. 처리량을 설계만으로 보장하지 않는다.
P1 이전에는 합성 자료로 성취도 미리보기만 허용한다. 공유 계정으로 개별 교수자 확정/상담 기록을 운영하지 않는다.
전면 재작성 대신 기능별 feature route/adapter를 순차 전환하고 각 단계에서 기존 경로와 결과를 비교한다.

필수 인수 시험: 필수 provider 제거, 알 수 없는 module, 계약 버전 충돌, 순환, 다른 수업 scope,
정책 하향 override, route 충돌, 임의 코드 설정, 미구현 plugin, 해시 위변조, migration 실패,
실행 중 비활성/롤백, 중복·역순 이벤트, 재채점과 분석 경합, DB busy·분석 timeout·queue 포화,
오래된 확장, 학생 비공개 정보 노출, 결과/증거 export 권한, 재시작 후 동일 PES 재현.

## 10. 이번 산출물의 검증 범위

설계 문서·구성 예시의 구조/참조, 기존 관리 SES 시험과 호환성만 확인한다.
새 플러그인 Host·새 권한 체계·성취도 수치·성능·운영 배포가 완료되었다는 의미는 아니다.

2026-09-18 설계 확인: 문서 상대 링크·JSON 예시 파싱·두 수업 key 중복 여부·안전 기본값·손계산 예시를 확인했다.
설계용 JSON이 기존 관리 SES 검증기에 운영 profile로 받아들여지지 않는 것도 확인했다.
기존 관리 SES·수업 관리·운영·스케줄러 회귀 시험은 **91 passed (2.61초)**다.
신규 성취도 계산기나 v2 실행기를 시험한 결과가 아니다.
