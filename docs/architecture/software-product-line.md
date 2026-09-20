# SES 기반 Autograde 소프트웨어 제품군(SPL) 사업화 설계

2026-09-18 · **제품 아키텍처 설계. 상품 출시·가격·계약·SLA·보안 인증이 확정된 상태가 아니다.**

목표는 고객별 서버를 복사·수정하는 방식이 아니라 공통 자산에서 검증 가능한 제품 변형을 생성하는 것이다.
SPL을 공통 관리 기능과 핵심 자산, 정해진 생산 방식으로 제품군을 만드는 접근으로 다루는
[CMU SEI 설명](https://www.sei.cmu.edu/library/software-product-lines-collection/)을 참고했다.
공통 자산 개발·제품 생성·제품군 운영을 구분하는 [SEI 과정](https://insights.sei.cmu.edu/library/introduction-to-software-product-lines-course/)과
SES의 구조·선택·분해를 결합한 **Autograde 자체 설계**이며 SES 설정만으로 상용 제품이 완성된다고 보지 않는다.

[전체 plugin 계약](platform-plugin-ses.md), [루브릭 평가](rubric-evaluation-plugin.md),
[성취도 평가](learning-achievement-plugin.md)가 이 제품군의 공통 자산/변이점이 된다.
[기능→plugin·제품 template 예시](../../design/ses/product-line.catalog.example.json)는 초기 부분 카탈로그다.
실행 SES나 라이선스 파일이 아니며, 후속 feature 목록과 현재 예시 범위를 구분한다.

## 1. 제품군 범위와 자산

대상 제품군은 프로그래밍 실습 과제 배포·접수·평가·학습목표 분석·수업 운영 소프트웨어다.
현재 C/C++·VS Code·Visual Studio·Windows/Linux 환경을 우선한다. 다른 언어·일반 문서 평가는
plugin으로 확장할 수 있는 경계만 두고 지원 완료로 판매하지 않는다. Canvas/LTI는 범위 밖이다.

공통 자산에는 소스뿐 아니라 다음이 포함된다.

- Host Kernel, plugin SDK·입출력 계약·버전 호환표·허용 registry.
- SES 모델, 기능 카탈로그, 의미 제약, pruning/검증기, 제품 profile, 배포 lock.
- 문제·루브릭·모범/오답 예제의 schema와 검증 도구. 고객의 실제 문제/학생 소스는 공통 자산으로 수집하지 않음.
- 공통 MVC shell·반응형/접근성 요소·두 IDE adapter·업데이트 프로토콜.
- 자동 시험·합성 데이터·지원 구성 행렬·부하 기준·취약점/의존성 점검·복구 도구.
- 설치/업데이트·백업·마이그레이션·운영 설명서·고객지원 진단 형식.

학생 인증·기관/수업 경계·감사·안전한 실행·백업/복원·자기 데이터 내보내기 등 기본 안전 기능은
상위 상품에서만 확보되는 선택 옵션으로 만들지 않는다. 보고서 형식/분석 깊이 등 부가 기능과 구별한다.

## 2. 변이점: 기술 기능과 판매 상품을 분리

| 구분 | 의미 | 예 |
| --- | --- | --- |
| Feature | 고객이 선택하는 업무 능력 | 루브릭 평가, 성취도, 지원 분석 |
| Plugin | Feature를 구현하는 계약/코드 단위 | rubric.catalog + evaluation.rubric + evaluator.instructor |
| SES variant | 유효 구조 대안 | HybridRubric, CriteriaAchievement |
| Product template | 제품 조합의 시작점 | Practice / Rubric / Insight (가칭) |
| Entitlement | 계약상 사용 가능한 Feature·용량 범위 | 기관별 허용 기능 집합·유효기간 |
| Deployment | 실제 서비스 인스턴스와 기술 환경 | 기관 전용 설치·관리형 전용 설치 |
| Course profile | 허용된 제품 안에서 수업별 선택 | 동일 기관의 분반별 평가 방식 |
| Rubric/assignment policy | 공개 버전에 고정하는 교육 설정 | 항목·배점·기한·피드백 공개 |

Feature 1개가 여러 plugin을 요구할 수 있고 하나의 plugin이 여러 Feature에 기여할 수도 있다.
상품 이름을 코드에서 분기 조건으로 사용하지 않는다. `edition == 'Pro'`가 아니라 검증된 capability를 검사한다.
**선택됨 ∩ 설치/호환됨 ∩ 사용권 허용됨 ∩ 사용자 권한 있음 ∩ 준비 상태 충족**일 때만 작업이 가능하다.
SES 유효성, 사용권, 사용자 인가는 서로 대체하지 않는다.

### 가칭 제품 템플릿 — 가격표/확정 출시 목록이 아님

| 템플릿 | 주요 기능 | 운영 전제 |
| --- | --- | --- |
| Practice | 수업/명단·자료 배포·불변 접수·IO 채점·결과·이력·기본 내보내기 | 개인별 관리 인증·격리 실행·백업·고객지원 기본선 |
| Rubric | Practice + 제공 루브릭·항목 평가·교수자 검토·근거 공개 | 승인 루브릭/검사 연결·기준 검증·판정 감사 |
| Insight | Rubric + 목표 성취도·보고서·설명 가능한 지원 분석 | 목표 매핑·일관 snapshot·분모 검증·정정 흐름 |

위 세 템플릿은 출발점이다. 루브릭 없이 자동 증거 기반 성취도만 쓰는 조합도 지원 정책과 의존성에 맞으면 가능하다.
보관/복원, 고급 exporter, AI 보조 등은 독립 옵션이다. AI는 별도 승인·비용/자료 전송 정책이 필요한 후속 대안이다.
고객 요구가 기술적으로 유효하더라도 시험/지원 범위에 없으면 `valid_but_unsupported`로 표시하고 판매 승인과 구별한다.

## 3. SES로 제품을 도출하는 구조

```text
AutogradeProductLine [Entity]
└─ ProductFamily [Aspect]
   ├─ MandatoryPlatform [Entity: 공통 Host·보안·데이터 계약]
   ├─ Deployment [Entity]
   │  └─ DeploymentVariant [Specialization]
   │     ├─ InstitutionDedicated [Entity]
   │     └─ ManagedDedicated [Entity]
   ├─ Assessment [Entity]
   │  └─ AssessmentVariant [Specialization]
   │     ├─ LegacyIoAssessment [Entity]
   │     ├─ RuleBasedRubric [Entity]
   │     ├─ InstructorRubric [Entity]
   │     ├─ HybridRubric [Entity]
   │     └─ AiAssistedRubric [Entity: optional/future]
   ├─ Analytics [Entity: 성취도·보고서·지원의 독립 대안 분해]
   ├─ Management [Entity: 보관·알림·일정의 독립 대안 분해]
   ├─ ClientChannels [Entity: 웹·VS Code·Visual Studio]
   └─ CourseFleet [Entity]
      └─ CourseReplication [MultiAspect: 허용된 개설/분반]
         └─ CourseInstance [Entity: 제품 범위 안의 선택·속성]
```

축약 개념도이며 노드 종류/잎의 provider를 생략한 부분은 실제 SES schema에서 명시한다.
제품 수준 Assessment는 허용된 평가 능력 범위를 정하고, 수업 profile은 그 범위 안에서 선택한다.
제품에 존재하지 않는 기능을 수업 profile로 주입할 수 없다. 학생별 면제/기한 예외는 기능 복제 대신 도메인 자료로 표현한다.
여러 평가 방식을 함께 허용하는 제품은 CapabilitySet의 Aspect로 각각 제공하고, 수업의 단일 평가 기본 방식은 Specialization으로 선택한다.

**단일 기준 원천:** feature ID와 SES decision/variant·plugin capability·제약의 매핑을 버전 있는
카탈로그에서 관리한다. UI 체크박스·가격/상품 목록·배포 스크립트에 별도 의존성 규칙을 복제하지 않는다.
SES는 구조와 결합을 담당하고 capability/semantic constraints가 기술 제약을 담당한다.
계약 사용권은 별도 서명 데이터로 검사한다. 현재 SES v1을 범용 feature constraint solver라고 주장하지 않는다.

### 변이점 확정 시점

| 시점 | 결정할 것 | 이후 변경 |
| --- | --- | --- |
| 개발 | plugin API, 입력/출력 schema, 제품군 불변 제약 | 호환성 검토·SDK major version |
| 빌드/배포 | 승인 package 버전·digest, 저장소/runner·OS, 배포 형태 | 시험한 release와 migration 필요 |
| 기관 구성 | 허용 capability·사용권·기본 정책·네트워크 전송 | 승인·구성 diff·새 버전 |
| 수업 개설 | 평가/성취도/보고서·일정 선택 | 영향 검토·시점 예약·진행 작업 버전 고정 |
| 과제 공개 | 루브릭·배점·검사·제출/공개 기준 | 새 버전 + 명시적 재평가/재공개 |
| 요청 실행 | 인증된 사용자·대상·서버 접수 시각 | 클라이언트가 고정 정책을 override할 수 없음 |

## 4. 제품 생산 파이프라인

1. 요구/견적 단계: 고객이 필요한 기능·배포/OS·데이터 위치·규모·오프라인 조건을 구조화한다.
2. 상품 template에서 **완전한 product profile**을 만든다. 수정 필드와 선택 근거를 기록한다.
3. SES pruning으로 PES 후보를 생성하고 필수 기능·제약·capability/계약·지원 조합을 검사한다.
4. 설치 registry와 entitlement를 대조한다. 미구현·미승인 plugin, 필수 전제 누락은 배포 후보에서 제외한다.
5. 코드/의존성·migration·OS/toolchain·UI/API 호환성을 잠근 product lock을 만든다.
6. 선택 조합의 기능·보안·격리·데이터 이동·성능·복구·업데이트 시험을 수행한다.
7. 서명된 배포 묶음, 구성/설명서, SBOM·라이선스 고지 자료, 지원 가능 목록, 시험 증빙을 함께 생성한다.
8. 고객 자료를 포함하지 않은 깨끗한 설치를 수행하고 고객 관리자가 실제 domain/TLS/백업/계정을 설정한다.
9. 설치 후 smoke/복구 확인을 통과하면 배포 상태를 supported로 등록한다. 배포만으로 검수 통과를 선언하지 않는다.
10. 제품군 공통 변경은 고객 구성에 영향 분석 → staging 검증 → 지원 채널별 업데이트로 전달한다.

제품 lock에 포함할 것: product_family/release, template ID, SES/catalog/profile/PES hash,
Host·plugin·dependency 버전/digest, runner/toolchain, DB schema/migration 체크섬,
server/client 계약 범위, 시험 증빙 ID, 지원 매트릭스 revision, 승인자·배포 식별자.
고객 비밀번호·학생 명단·라이선스 서명 개인키는 넣지 않는다. 비밀정보는 별도 보호 저장소 참조로 주입한다.

같은 승인 입력이면 같은 PES/lock이 도출되어야 한다. 이것이 곧 빌드 바이너리의 bit-for-bit 재현이나
AI 판정 재현을 뜻하지 않는다. 빌드는 도구·의존성까지 별도 관리하고 AI 응답은 평가 기록으로 보존한다.

## 5. 기관별 데이터와 배포 전략

초기 상용 후보는 **기관별 전용 배포 + SQLite**를 우선 검증한다. 운영 장비·학생 규모·백업 조건을 확인하고
처리량·가용성을 실측한 범위만 지원한다. 현재 파일럿 성능을 모든 고객/규모에 일반화하지 않는다.
기관별 DB·artifact 경로·암호화/서명 key·백업·운영 권한을 분리하고 하나의 pilot data root를 여러 고객이 공유하지 않는다.

동일 인프라에서 여러 기관을 서비스하는 형태는 후속 별도 변이점이다. 도입하려면 tenant_id를
수업/수강/과제/접수/평가/작업/감사/캐시/파일·내보내기까지 일관되게 적용한다.
현재 course scope만으로 기관 간 격리가 완성되었다고 표시하지 않는다.
인증된 서버 문맥으로 tenant를 결정하며 임의 request tenant_id/Host header만으로 접근권한을 부여하지 않는다.
기관 이동·데이터 합치기는 import/export 승인 작업이며 이름이 같은 학번/수업을 자동 병합하지 않는다.

현재 `pilot-local`은 신뢰 코드 기능 시험용이다. 불특정 학생이 제출하는 코드의 실행 격리·네트워크/파일 접근 제한·
CPU/메모리/시간 quota 검증은 상용 배포 gate다. 컨테이너만 설치했다고 이 gate가 자동 통과하지 않으며,
격리 방식의 기술 선택과 현장 시험은 별도 구현 단계다. 파일럿에서는 기존 계획대로 Docker를 강제 도입하지 않는다.

## 6. 사용권·확장 배포·업그레이드

- entitlement는 기능 사용권이며 성적·학생 접근권한 데이터와 분리한다. 추가 요금/가격은 설계에서 확정하지 않는다.
- 초기 전용 설치는 오프라인 검증 가능한 서명된 entitlement 파일을 고려한다. 검증 key rotation·기간·시간 역행·
  재발급 정책을 명시하고 사용권 확인 때문에 학생 자료를 외부로 전송하지 않는다.
- 만료/다운그레이드 시 새 부가 작업 생성을 제한할 수 있지만 접수된 작업·이미 구매한 기간의 평가 근거·원본 자료를
  삭제하거나 점수를 바꾸지 않는다. 기존 자료 조회/기본 내보내기·지원 복구 경로를 유지한다.
- 진행 중 평가·보고서는 접수 당시 구성으로 drain한다. 수업 중 필수 제출/인증을 갑자기 끄는 정책은 허용하지 않는다.
  유예/종료 시점은 계약과 구현 정책을 명시적으로 합의해야 하며 숨은 원격 중지 장치는 넣지 않는다.
- plugin 등록은 패키지 출처·서명/digest·정적/계약 시험·보안 검토를 통과해야 한다. 교수자 파일 업로드는 plugin 설치가 아니다.
- VS/VS Code 확장은 가능한 공통 배포본을 유지하고 서버 capability로 지원 UI를 선택한다.
  서버가 확장에 임의 코드를 내려 실행시키지 않는다. client 최소 버전·rollback·기존 제출 계약을 지원 행렬에 포함한다.
- 단일 코드라인의 보안/오류 수정은 모든 영향 제품에 전파한다. 고객별 영구 코드 fork를 기본 운영 방식으로 두지 않는다.

## 7. 커스터마이징과 제품군 거버넌스

사용자 정의 수준을 ①설정/브랜딩·루브릭 ②검증된 plugin 조합 ③새 plugin/API 확장으로 구분한다.
①은 범위 검사·preview, ②는 지원 조합 시험, ③은 SDK/보안/유지보수 검토가 필요하다.
기본 로고·색/문구를 바꿔도 접근성·경고·평가 근거·보안 표시를 제거할 수 없다.

제품군 책임자: 범위/Feature/지원 template 승인. 핵심 자산 책임자: API·공통 품질·migration.
plugin 책임자: 계약·테스트·복구·권한·의존성. 고객 배포 책임자: 제품 도출·현장 검증.
교수자는 교육 기준을, 기관 관리자는 사용자·자료 정책을 승인한다. 개발자가 운영 학생 성적을 묵시적으로 승인하지 않는다.

신규 기능은 제안 → 고객 공통성/변이성 검토 → feature ID/계약/SES 제약 → 검증 fixture →
공통 자산 등록 → 지원 조합 시험 → 배포 문서 → 정식 지원으로 승격한다.
고객 전용 plugin도 전체 DB 쓰기나 Kernel 우회 권한을 받지 않는다.
지원 종료는 대체/데이터 변환/내보내기/기한을 미리 제공한다. 과거 루브릭·plugin lock·평가 기록의 열람/재현 정책을 보존한다.

SBOM·오픈소스 의존성 고지/재배포 조건·문제/루브릭 저작권·고객 자료 이용 범위는 출시 체크리스트로 관리한다.
현재 의존성의 상용 배포 조건을 검토 완료했다고 주장하지 않으며, 실제 계약·개인정보·사용권 문구는 별도 검토 대상이다.
이 문서는 법률/규제 준수 승인서가 아니다.

## 8. 검증 및 출시 gate

| gate | 통과 증거 |
| --- | --- |
| 구성 유효성 | 지원 template 정례 compile, 필수 provider 제거·의존성·권한·상충 조합 거절 |
| 평가 타당성 | 승인 루브릭별 0/부분/만점·미평가·오류 예제와 항목별 기대값, 교수자 사전 검토 |
| 안전·데이터 분리 | 기관/수업/학생 경계, 비공개 테스트, 격리 runner·quota·감사·secret 점검 |
| 신뢰성 | 접수 멱등성, 최신/최고 분리, 재채점/재시작, 백업·복원·migration 실패 복구 |
| 조합/클라이언트 | 템플릿×대상 OS/runner×client/계약 버전 시험, 지원하지 않는 조합은 명시 거절 |
| 서비스 품질 | 지정 장비의 25명 이상 합성 동시 제출 + 분석/보고서 부하, 누락 0·지연/용량 실측 |
| 운영/지원 | 설치·업데이트·진단 묶음·가명 처리·지원 종료/내보내기 절차·고객 검수 |
| 상품화 | 지원 범위·가격/용량·업데이트/유지보수 조건·의존성/콘텐츠 권리·보안 문서 검토 |

상호작용이 적은 옵션은 pairwise 검사를 활용하되 보안·평가·데이터 손실 관련 조합은 전체 필수 시나리오를 실행한다.
지원 template은 매 릴리스 시험하고 invalid 조합도 회귀 시험한다. 시험 통과 목록과 “구성은 가능하지만 미지원” 목록을 구분한다.
제품군 운영 지표는 지원 조합 수, 공통 자산 재사용, 고객별 예외 코드량, 업데이트 소요, 구성 결함·지원 문의로 관찰한다.
매출·시장성·교육 효과를 설계만으로 추정하거나 보장하지 않는다.

## 9. 구현 순서와 현재 산출물

1. **계약 확정:** rubric schema·evaluator port, feature→SES/plugin 매핑, 제품 template과 인수 fixture.
2. **공통 기반:** 승인 registry·v2 pruning/검증기·lock·개인별 인증·감사/outbox·기존 서비스 Adapter.
3. **첫 세로 기능:** 제공 루브릭 등록→예제 검증→고정 제출 평가→교수자 검토→결과 공개.
4. **제품 도출 검증:** Practice/Rubric template으로 같은 코드에서 두 설치를 생성하고 계약·업데이트를 비교.
5. **분석 제품:** 승인 평가→성취도→보고서·지원 분석을 연결하고 Insight 조합 시험.
6. **상용 gate:** 기관 전용 배포, 격리 실행·복구·용량 검증, 지원·사용권·유지보수 조건 확정 후 고객 파일럿.
7. **후속:** AI 보조·문서 importer·대규모 저장소/다기관 운영 등은 별도 변이점으로 검증 후 편입.

이번에는 설계·설계용 예시와 기존 관리 SES 호환성 확인만 수행한다.
후속 [로컬 루브릭 모듈 1차](../operations/rubric-local-mvp.md)에서 offline evaluator와 별도 승인/평가 DB,
고정된 루브릭 SES 미리보기를 구현했다. 전체 Host·라이선스 검증기·제품 생성기·상용 배포는 미구현이다.
후속 [교수자 웹 카탈로그](../operations/rubric-web-mvp.md)는 기준 등록·검토·버전 조회를 독립 adapter로 연결했다.
CSV 선택 설정은 MVP 호스트의 임시 연결이며 SES로 제품 전체를 생성하거나 평가·공개 권한을 활성화하지 않는다.
[교수자 개인 인증](../operations/instructor-personal-auth.md)은 공통 기반의 선택 provider로 연결했다.
담당 분반 인가·세션 revision과 등록자 ID를 제공하며, 제품 사용권이나 평가 승인 정책을 대신하지 않는다.

### 설계 예시 검사

[자동 검사](../../tests/unit/test_product_design_assets.py)는 예시 JSON의 명시적 design-only 상태,
기존 관리 profile로의 적용 거절, 항목/수준/규칙 참조·배점 합계·부분 합계, 제품 feature 의존성/순환,
템플릿의 필수 기능·의존성 충족, 수업의 rubric 전제, 문서 링크를 확인한다.
실제 학생 답안 평가, 문서 이해·추출 품질, 신규 Host의 권한 강제나 상용 제품 검증은 아니다.

```sh
python -m pytest -q tests/unit/test_product_design_assets.py tests/unit/test_management_ses.py tests/unit/test_course_admin.py tests/unit/test_course_operations.py tests/unit/test_scheduler.py
```

2026-09-18: 신규 설계 예시 검사 13개 + 기존 관련 회귀 91개 = **104개 통과**.
