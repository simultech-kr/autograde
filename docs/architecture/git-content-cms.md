# Git 기반 콘텐츠 관리·학생 로컬 동기화 설계

2026-10-24 · **목표 설계 + C0~C2 1차 구현. 구현 범위와 후속 범위를 아래에서 구분한다.**

Autograde를 채점 시스템과 함께 사용할 수 있는 수업 콘텐츠 관리 시스템(CMS)으로 확장한다.
교수자는 웹에서 자료를 관리하고, 학생 IDE는 본인에게 공개된 변경 파일만 전용 캐시에 받아
두었다가 원하는 시점에 자신의 폴더로 저장한다. SQLite와 모듈형 Python 서버를 유지한다.
학생별 폴더 분리는 현재 MVP, OS 계정·VM 격리는 설계만 한다는 기존 결정을 유지한다.

## 0. 이번 구현 범위

| 영역 | 구현 상태 |
| --- | --- |
| 서버 | `content_store.py`의 내부 bare Git·SHA-256 파일 객체·불변 release·SQLite migration 20 |
| 교수자 | 기존 웹의 `수업 → 콘텐츠`: 초안·대상·기간·파일 편집, 변경 검토, 즉시 공개, 이력, 배포 중단 |
| 학생 전송 | `/v1/content/context`, `/collections`, `/releases/{id}/manifest`, `/objects/{sha256}`, `/verify` |
| 학생 IDE | VS Code/Autograde IDE 0.5.9의 `강의자료 업데이트`: 수동 변경분 수신·해시 검증·snapshot·명시적 복사 |
| 보호 | 계정/수업별 캐시, 답안 폴더 제외, 인증 전환 취소, 링크/경로 검사, 공개 충돌·실패 시 이전 정상본 유지 |

작은 수업용 1차 구현은 **자료 모음 전체에 동일한 파일 트리**를 배포하고 전체 수업 또는 선택
학생에게 공개한다. 학생별 서로 다른 파일 variant와 기존 개인 PDF/ZIP의 CMS 이전은 아직
하지 않는다. 기존 개인 자료 서비스는 별도로 유지한다. `content.read` 독립 인증 전까지
학생 IDE는 현재 과제 수령 코드로 연결한 메모리 세션을 재사용한다.

이하 설계 중 미구현: 예약 공개, 버전 복구 버튼, 보관/GC, 서버 자기보고 수집, Next.js 콘텐츠
화면, 백그라운드 자동 수신, 오프라인 내보내기, chunk/Range 재개, 외부 Git 가져오기, SES
런타임 등록이다. 미래 시작 시각은 초안에 저장할 수 있지만 공개할 때 명확히 거절한다.
내보내기는 현재도 서버 연결·최종 권한 확인이 필요하다. Visual Studio VSIX 동기화는 후속이다.

초기 계약의 차이: manifest는 페이지형 대신 최대 1,000개 파일/512 KiB의 **한 번에 검증하는
전체 응답**이다. 파일 25 MiB·배포본 100 MiB·학생 캐시 512 MiB 한도, API 파일 전송 8개
동시 제한을 적용한다. IDE는 순차 파일 수신부터 시작한다. 공개본마다 대상/기간 정책을 함께
고정하고 변경도 새 공개본으로 처리한다. 콘텐츠 Git 객체 저장소와 단기 DB 트랜잭션만으로
공개하며, 미공개 orphan은 보존하고 동일 멱등 키로 재시도한다. 자동 GC는 제공하지 않는다.

[등록·수신 절차](../operations/student-material-distribution.md#git-콘텐츠-변경분-동기화-1차-mvp)를
참고한다. 운영 서버 적용·실제 학생 데이터 변경·자동 배포는 이번 작업에 포함하지 않는다.
구현 검증은 [2026-10-24 시험 기록](../operations/autograde-ide-poc.md#2026-10-24-git-cms-저장공개변경분-동기화-검증)에 남겼다.

## 1. 핵심 결정

| 영역 | 결정 |
| --- | --- |
| 서버 버전 관리 | 실제 서버 내부 Git 저장소로 자료 트리·설명·변경 이력·배포 원본을 버전 관리 |
| 파일 저장 | PDF/ZIP 등 바이너리는 비공개 SHA-256 객체 저장소(CAS)에 보관하고 Git에는 참조 기록 |
| 인가·공개 원장 | SQLite가 학생·수업·대상·기간·현재 공개 버전·감사 기록을 관리 |
| 학생 전송 | Git clone/pull 대신, 인증 HTTP로 허용된 manifest와 없는 파일 객체만 전송 |
| 로컬 반영 | 임시 수신 → 전체 검증 → 완성된 버전 일괄 활성화 → 학생의 명시적 내보내기 |
| 학생 답안 | 동기화 대상에서 제외. 자동 merge/reset/덮어쓰기/삭제 금지 |
| 관리 화면 | Next.js의 새 `콘텐츠` 기능으로 확장. Python 서비스가 모든 변경·인가를 판정 |
| 제품 구성 | Git 저장·동기화·내보내기·관리 UI를 계약으로 모듈화. SES 후보는 `contract_only` |

GitHub 계정·GitHub OAuth·GitHub App·학생별 원격 Git 저장소는 필요 없다. 서버의 자료 저장소와
Autograde 프로그램 자체의 소스 저장소도 분리한다. 자료 업데이트로 IDE 확장이나 실행 파일을
교체하는 자동 업그레이드 시스템을 만들지는 않는다.

Git은 내용 기반 객체·트리·커밋을 관리한다. 이 원리를 서버 버전 관리에 사용한다.
[Git 공식 객체 설명](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)
Git의 pack delta와 이 설계의 **파일 단위 변경분 전송**은 다르다. ZIP 안의 한 문장만 바뀌어도
ZIP 전체 해시가 바뀌면 MVP는 그 ZIP 전체를 다시 받는다. 바이트 단위 패치는 후속 최적화다.
[Git 공식 pack 설명](https://git-scm.com/book/en/v2/Git-Internals-Packfiles)

### 용어와 학생에게 표시할 의미

| 용어 | 실제 동작 | 화면 문구 |
| --- | --- | --- |
| 변경 확인 | 파일 목록·버전·크기만 조회 | 업데이트 확인 |
| 동기화/prefetch | 서버에서 학생 PC 캐시로 실제 파일 전송 | 변경분 받기 / 임시 수신 |
| 캐시 준비 | 검증된 전체 버전을 관리 영역에 보존 | 이 PC에 준비됨 |
| export | 캐시에서 선택한 일반 폴더로 독립 복사 | 내 폴더에 저장 |
| 열기 | 학생 요청으로 파일 위치 표시/허용 형식 열기 | 자료 열기 |

따라서 ‘나중에 다운로드’는 **나중에 내 폴더로 꺼내기**에 해당한다. 임시 수신도 이미 학생
PC로 내용을 전달한 것이다. 공개 전 시험지를 미리 평문으로 내려받고 버튼만 숨기는 방식은
사용하지 않는다. 기밀 시험 자료는 본 CMS의 일반 오프라인 배포 대상에서 제외한다.

## 2. 현재 기반과 신규 범위

| 현재 기반 | 그대로 활용할 계약 | 신규 개발할 부분 |
| --- | --- | --- |
| `student_materials.py` | 개인 PDF/ZIP·배정·기간·공개 후 수정·이전 파일 보존 | 여러 파일 트리·Git commit·CAS·불변 release |
| `student_material_api.py` | 인증 세션의 학생/수업 결정·해시 pin·권한 재검사 | manifest·누락 객체 전송·동기화 상태 API |
| `instructor_web.py` + `instructor_student_materials.py` | 교수자 수업 인가·CSRF·업로드 검증·revision 충돌 | 일관된 초안→변경 검토→공개 UX |
| IDE `studentMaterials.ts` / `studentMaterialsView.ts` | 해시/형식 검사·덮어쓰기 방지·세션 변경 취소 | 캐시·전체 snapshot 전환·내보내기·동기화 UI |
| `gitops.py` | bare 처리·불변 참조·checkout 없는 blob 읽기 등의 안전 기법 | 제출 수집과 분리한 콘텐츠 Git adapter |
| `web/src/features/registry.ts`, `instructor_web_api.py` | Next 화면 shell·인가된 조회 계약 | 콘텐츠 JSON 쓰기 API·업로드·Next CRUD |
| `management_ses.py` | 구성/의존성 검증 원칙 | CMS용 계약·v2 Model Base·런타임 Host 연계 |

기존 개인 자료 기능은 PDF/ZIP 원본 전체를 받는다. 신규 `강의자료 업데이트`에서만 파일
단위 캐시 동기화를 제공한다. Next 관리자는 현재 조회 중심이며 CMS CRUD는 기존 웹에서 제공한다.
현행 SES v1의 6개 관리 영역에 새 ID를 임의로 추가하거나 실행 가능하다고 선언하지 않는다.

## 3. 구성과 책임

```text
교수자 콘텐츠 관리자(Next.js)
  └─ Python 인증·수업 인가·콘텐츠 서비스
       ├─ content_authoring   초안·업로드·변경 검토
       ├─ content_git_store  서버 내부 Git commit/tree
       ├─ content_blob_store 비공개 파일 객체
       ├─ content_release    공개·기간·대상·철회 + SQLite
       └─ content_sync_api   학생별 manifest·객체·상태
                                 │ 인증된 HTTPS, 본인 범위만
                          Autograde IDE / 확장
                                 ├─ content_sync_client
                                 │    staging → objects → snapshots
                                 ├─ current pointer (완성 버전만)
                                 └─ 명시적 export → 학생 일반 폴더
                                      학생 답안 workspace와 독립
```

기능마다 서버를 만들지 않는다. Python은 유일한 변경 원장이고 Next.js는 화면을 제공한다.
파일 업로드/다운로드는 인증된 Python 경로로 직접 처리해 Node와 Python의 이중 버퍼링을 피한다.
Nest.js·PostgreSQL·Docker는 이 확장의 선행조건으로 추가하지 않는다.

### 서버의 Git 저장 범위

- 수업 개설(`course_key`, 학기·분반 포함)별 내부 저장소 하나를 기본으로 한다. 표시용
  `come2201` 이름이 같아도 다른 학기·분반 저장소/권한을 합치지 않는다.
- 설명·Markdown·작은 C/C++ 참고 소스는 Git tree에, PDF/ZIP은 CAS 참조 manifest에 저장한다.
  배포 시 모든 파일은 같은 SHA-256 객체 계약으로 제공한다. 텍스트의 Git OID와 전송용
  SHA-256은 별개 필드이며 서로 비교하지 않는다.
- Git에는 비밀번호·토큰·학생 명단 원문·성적·개인정보 감사 원장을 넣지 않는다. 개인 자료
  참조는 opaque ID만 쓰고 학생 매핑은 SQLite에 둔다. 개별 파일 본문도 비공개 CAS에 둔다.
- 공개할 후보는 서버 생성 commit과 보존 ref에 고정한다. 학생 목록은 Git HEAD가 아니라
  SQLite의 현재 공개 포인터만 읽는다. Git 과거 이력은 교수자 권한 화면에서만 제공한다.
- 서버가 관리하는 bare repo와 고정 인자 plumbing을 사용한다. 사용자 Git hooks, filters,
  submodule, 임의 `.git/config`, 외부 command를 실행하거나 제출 수집 서비스를 혼용하지 않는다.
- 교수자 외부 Git 저장소 가져오기는 후속 adapter다. 허용 호스트·수업 권한·읽기 전용 자격·
  정확한 commit·크기/시간 제한을 별도 검증하며 임의 URL fetch/자동 pull은 MVP에서 제외한다.

학생에게 raw Git 저장소를 clone시키지 않는다. partial clone/sparse checkout은 전송·작업
범위를 줄이는 수단이지 개인 자료 접근 제어가 아니기 때문이다. 본인 배포본에 없는 경로·
과거 blob·다른 학생 자료를 노출하지 않는 서버 projection을 만든다.
[Git clone 옵션](https://git-scm.com/docs/git-clone)

## 4. 콘텐츠 모델과 공개 규칙

`자료 모음(Collection) → 수정 초안(Draft) → 불변 공개 버전(Release) → 학생별 Manifest`로 나눈다.

| 설계 엔티티 | 주요 필드/책임 |
| --- | --- |
| `content_collections` | ID, 소유 course_key, 제목·분류, 현재 release와 별도 예약 후보, 편집 revision, 보관 상태 |
| `content_drafts` | 기준 release, draft revision, 파일 참조·변경 안내, 작성자, 검증 상태 |
| `content_releases` | release ID, 활성화 시 한 번 부여하는 단조 증가 generation, Git commit, root digest, 공개자·시각·사유 |
| `content_release_files` | release, 논리 경로, 파일 SHA-256, 크기·형식, audience/variant 참조 |
| `content_audiences` | 전체 수업/명시 학생 대상, 정책 revision, 유효 기간, 기존 수강 원장 참조 |
| `content_objects` | 저장 namespace, digest, 크기, 검증 상태·참조·보존 정보 |
| `content_publish_jobs` | 멱등 키·기준 revision·단계·lease·오류, Git/파일/DB 재조정 근거 |
| `content_delivery_events` | 서버 요청 기록 또는 클라이언트 자기보고, release·시각·문제 코드 |
| `content_legacy_links` | 기존 material/edition/file_revision ↔ 새 collection/release 매핑 |

위 표는 **목표 엔티티**다. 1차 구현은 migration 20의 `content_collections`, `content_draft_files`,
`content_releases`, `content_release_files`, `content_publish_jobs`, `content_audit`로 구성하고
대상·기간은 모음/공개본에 저장한다. 기존 migration 17~19나 제출·성적 테이블을 다시 작성하지 않는다.

### 대상·기간

- 자료 모음은 한 수업 개설에 속한다. 여러 분반에 같은 자료를 배포하려면 각 수업 권한을
  확인해 별도 release 참조를 생성한다. 타 분반의 학생 명단이나 자료가 합쳐지지 않게 한다.
- `전체 수업`은 현재 활성 수강생을 매 요청에 검사한다. `선택 학생`은 저장한 대상 집합과
  현재 활성 수강 상태의 교집합이다. 새 학생의 자동 편입 여부를 공개 화면에 명시한다.
- 공통 파일과 개인 variant를 조합하되, 같은 논리 경로가 충돌하면 명시한 개인 override만
  허용한다. 다른 학생의 파일명·해시·크기는 본인 manifest에 포함하지 않는다.
- 새 판의 내용은 불변이다. 기간·대상 변경도 새 정책 revision을 만들고 감사한다. 단순
  파일 교체는 기존 바이트 변경이 아니라 새 draft에서 새 release를 만드는 방식으로 통일한다.
- `opens_at <= server_now < closes_at`일 때만 신규 전송을 허용한다. 시각은 UTC 저장,
  관리/학생 화면은 Asia/Seoul 표기. 예약 후보는 현재 공개본과 분리하고 활성 시각 전에는
  기존 공개본을 유지한다. 모든 요청은 서버 시각 기준 기간 검사를 반드시 통과한다.
- 배포 중단은 즉시 신규 조회·전송 요청을 거절한다. 진행 중 전송은 감지 가능한 범위에서
  중단하고 최종 확인 실패 시 새 snapshot 활성화를 막는다. 이미 전송한 바이트나 TCP
  버퍼의 내용을 회수한다고 보장하지 않는다.
- 보관은 초안·중단·기간 종료 상태에서만 허용하고 기본 관리 목록에서 제외하되 이력을
  남긴다. 배포 중이면 먼저 중단하고 예약이 있으면 먼저 취소해야 한다. 보관 후 신규
  전송은 허용하지 않는다. 물리 삭제는 참조·보존·백업 검사와 별도 관리자 승인 후 GC로만 수행한다.

## 5. 초안 저장·공개·복구의 정합성

Git, 객체 파일, SQLite는 한 트랜잭션으로 묶이지 않는다. **검증된 파일을 먼저 준비하고,
SQLite의 공개 포인터를 마지막에 확정**한다.

1. 인증·수업 변경 권한·CSRF·예상 draft revision과 멱등 키를 검사하고 작업 원장을 만든다.
2. 격리된 staging에서 형식·경로·개수·크기·해시를 검사하고 CAS 객체를 배타적으로 저장한다.
   임의 코드나 ZIP을 실행하지 않는다.
3. Git tree/commit·서버 content-tree manifest·보존 ref를 준비하고 모든 참조 객체가 존재하는지 검증한다.
   큰 파일 작업·Git 실행 중 SQLite 쓰기 잠금을 유지하지 않는다.
4. 짧은 DB 트랜잭션에서 예상 collection/draft revision, 최신 교수자 권한·대상 정책을
   다시 검사한다. 즉시 공개는 새 release·generation·공개 포인터·감사·outbox를 함께
   확정한다. 예약 공개는 별도 후보·예약 기록만 저장하며 현재 공개 포인터를 바꾸지 않는다.
5. 학생 조회는 확정한 공개 포인터만 사용한다. 동시 수정은 한쪽에 409를 반환하고 변경
   검토를 다시 요구한다. 같은 멱등 키/같은 입력 재시도는 같은 공개 결과를 반환한다.
6. 재시작 시 `preparing/prepared/scheduled/published/cancelled/failed` 작업과 객체/ref를 재조정한다. DB 확정 전
   장애의 orphan은 미공개로 남기며 즉시 삭제하지 않는다. DB가 참조하는 파일 누락은 운영
   오류로 처리하고 다른 버전으로 조용히 대체하지 않는다.

사전 준비하는 서버 content-tree manifest는 파일 경로·객체 참조의 불변 원본이며 generation과
학생 subject를 포함하지 않는다. 학생 delivery manifest는 활성화 후 DB의 generation·정책
revision·인증된 subject를 결합하여 생성하고 그 projection을 해시한다. 예약 활성화를 위해
이미 준비된 Git commit이나 파일 manifest를 수정하지 않는다.

예약 활성화는 서버 요청 시 resolver 또는 선택한 scheduler가 수행한다. 미리 검증한 후보를
짧은 compare-and-swap 트랜잭션에서 다시 인가·기간·기준 revision 검사한 뒤, 그 시점의 다음 generation을
부여하고 현재 공개 포인터를 한 번만 전환한다. 파일 준비/Git 실행을 요청 경로에서 반복하지
않는다. 예약 이후 다른 판을 공개하거나 대상 정책을 바꾸면 기존 예약은 재검토가 필요하며
더 최신 판을 조용히 덮어쓰지 않는다. 실행이 늦어 종료 시각까지 지난 예약은 건너뛰고 운영
상태에 표시한다. scheduler 유무와 무관하게 각 전송 요청의 기간·인가 검사는 유지한다.

Git ref에도 예상 old OID 비교를 적용한다. 이것이 SQLite까지 원자화해 주지는 않는다.
[git-update-ref 공식 설명](https://git-scm.com/docs/git-update-ref)

복구는 과거 콘텐츠를 읽어 **새 generation으로 재공개**한다. 과거 공개 기록을 삭제하거나
force reset하지 않는다. 이전 대상·기간은 자동 복구하지 않고 현재 대상·기간을 다시 검토한다.
같은 내용으로 복구하면 기존 SHA-256 객체를 재사용할 수 있다.

## 6. 학생 로컬 공간과 동기화

기존 learner의 `workspace`와 형제인 `materials` 아래에 관리 캐시를 둔다. 아래 구조는
설계용이며 현재 launcher가 이 캐시 디렉터리를 생성하는 것은 아니다.

```text
learner-root/
├─ workspace/                         학생 답안·메모: 자동 변경 금지
└─ materials/
   ├─ .content/<origin>/<subject>/<course>/
   │  ├─ staging/<job>/               수신 중 .part, 동일 파일시스템
   │  ├─ objects/<sha256>             검증된 파일 객체
   │  ├─ snapshots/<manifest-id>/     불변 manifest·파일 인덱스
   │  ├─ current/<collection>.json    완성된 버전 포인터
   │  └─ journal/                    복구용 상태, 자격증명 없음
   └─ exports/<collection>/<version>/ 학생이 명시적으로 저장한 독립 복사본
```

- 서버 origin, 서버가 인증한 opaque subject, 수업을 모두 구분한다. 로컬 별칭/입력 학번은
  캐시 인가 근거가 아니다. 다른 서버·학생 사이의 캐시 공유는 MVP에서 금지한다.
- CAS와 exports/workspace 사이 hardlink는 사용하지 않는다. 독립 복사로 학생의 수정이
  캐시 원본을 바꾸지 않게 한다. 캐시의 읽기 전용 속성만 보안 격리로 믿지 않는다.
- OS의 자동 청소 대상 tmp 폴더가 아니라 전용 지속 캐시를 사용한다. 임시 staging만 따로
  정리하며, 현재 snapshot·진행 작업·학생이 보존 지정한 판은 자동 GC에서 제외한다.
- 초기 캐시 예산 제안은 학습자당 512MiB이며 부족하면 사전 경고한다. 대형 수신 전에 기존
  참조 객체와 새 파일·staging을 합산한다. 답안·내보낸 자료를 공간 확보 목적으로 삭제하지 않는다.
- POSIX 소유자 권한·링크 거절은 유지하되 동일 OS 계정의 학생끼리 보안 격리되었다고
  주장하지 않는다. 공용 좌석의 기밀성은 OS 계정/VM 단계에서 해결한다.

### 파일 단위 알고리즘

1. 메모리의 로그인 세대와 origin/subject/course를 고정하고 현재 카탈로그를 조회한다.
   인증/권한 검사 후에만 ETag/304를 반환한다. 304는 로그인을 생략할 근거가 아니다.
2. 정확한 release의 본인 manifest를 받고 구조·경로·개수·총 크기·digest를 검사한다.
   페이지 조회 시 동일 manifest snapshot에 고정하고 중복·누락·반복 cursor를 거절한다.
3. 경로가 아니라 SHA-256을 기준으로 캐시를 비교한다. 이름 변경은 기존 객체를 재사용하고
   변경/추가된 객체만 내려받는다. 기존 파일도 활성화 전 크기·해시를 확인한다.
4. 최대 2개 객체를 동시에 staging에 받는다(제안값). 매 요청은 같은 origin과 서버 인가를
   사용한다. 인증 전환·취소·크기 초과·해시 오류 시 중단하고 이전 정상본을 유지한다.
5. MVP는 실패한 파일 한 개를 처음부터 재전송한다. 이미 검증한 다른 객체는 재사용한다.
   Range/부분 chunk 재개, 바이너리 patch는 다음 단계로 분리한다.
6. 모든 객체와 manifest를 검증한 뒤 예상 manifest ID·digest·generation·policy revision을
   보내 서버에 현재 배포·대상·기간을 다시 확인한다. 새 판이
   공개되거나 정책이 바뀌면 409로 중단하고 새 manifest를 받는다. 같은 판으로 무한 재시도하지
   않고 최대 2회 후 ‘업데이트가 계속 변경됨’으로 수동 재시도를 안내한다.
7. 로그인 세대를 다시 검사하고 완성된 snapshot을 등록한다. 임시 포인터 파일 flush 후
   같은 디렉터리의 rename으로 current를 바꾼다. Windows 잠금 등 실패 시 이전 current를
   유지한다. 이는 최종 확인 시점의 일관성이지 이후 재공개를 영구 차단하는 잠금은 아니다.
8. 수신 완료를 로컬에 기록하고 온라인이면 최소 정보로 자기보고한다. 보고 전송 실패가
   로컬 검증 성공을 취소하지 않는다. 같은 이벤트의 재시도는 멱등 처리한다.
9. 학생이 `내 폴더에 저장`을 선택하면 새 버전 폴더에 독립 복사하고 다시 검사한다. 기존
   파일은 덮어쓰지 않고 다른 이름/폴더 선택을 안내한다. ZIP 자동 압축 해제·자동 실행은 없다.

파일 제거는 새 manifest에서 빠지는 것으로 표현한다. 이전 snapshot·exports·workspace를
즉시 삭제하지 않는다. 학생이 캐시를 직접 수정한 경우 해시 오류로 재수신 또는 중단한다.
자료를 코드로 활용하려면 별도 복사 동작을 사용하며 `.vscode`, tasks, Workspace Trust,
디버그 설정이나 실행 권한을 자동 활성화하지 않는다.

### 자동 갱신과 오프라인

- 기본은 IDE 활성·로그인 중 120초 간격(±20% jitter)으로 **메타데이터만 확인**한다.
  학생이 선택한 ‘이 수업 변경분 자동 수신’에서만 파일을 백그라운드로 받는다. 로그아웃·
  계정 전환 시 중단하고 다른 학생의 완료 알림도 표시하지 않는다.
- 오프라인은 `로컬 v3 / 마지막 확인 시각 / 서버 상태 확인 불가`로 표시한다. ‘최신’으로
  표시하지 않는다. 이미 준비된 일반 강의자료는 동일 로그인 세션과 허용된 오프라인 정책
  안에서 내보낼 수 있지만, 최초 수신·새 권한 판단은 온라인에서만 한다.
- 최종 온라인 확인은 `server_now`, `offline_until`을 반환한다. `offline_until`은 세션 만료,
  자료 종료(설정된 경우), 서버 정책상 최대 허용시간 중 가장 이른 시각이다. 현재 프로세스의
  단조 증가 경과시간으로 남은 시간을 줄이고 PC 시계 역행으로 연장하지 않는다. 절전 복귀나
  경과시간을 신뢰할 수 없는 경우 온라인 재확인 전에는 IDE 내보내기를 중단한다. 이미 확인한
  철회 상태는 오프라인 전환으로 해제하지 않는다. 이는 IDE 동작 제한이지 평문 파일 접근 통제가 아니다.
- 재시작 후에는 토큰을 복원하지 않는다. 개인 캐시를 IDE에서 다시 연결/내보내려면 재인증이
  필요하다. 재인증 없이 오프라인으로 이어갈 때는 이미 내보낸 파일을 사용한다. 완전한
  오프라인 개인 인증은 OS 계정/VM과 별도 설계한다.
- 기간 종료·수강 취소·배포 중단을 확인하면 새 수신·서비스 내 내보내기를 막는다. 하지만
  **이미 PC에 전달한 평문이나 내보낸 복사본을 원격 회수할 수는 없다.** 이는 DRM이 아니다.

## 7. 제안 API와 보안 계약

아래는 목표 API다. 이번에 구현한 경로는 0절에 명시했다. `reports`, Next 교수자 쓰기 API와
페이지 cursor는 후속이며 현재 요청할 수 없다.

| 대상 | 제안 경로 | 역할 |
| --- | --- | --- |
| IDE | `GET /v1/content/context` | 지원 계약/기능, 인증된 course·opaque subject, 제한값 |
| IDE | `GET /v1/content/collections?cursor=...` | 본인 자료 모음·현재 버전·기간·변경 안내, snapshot 페이지 |
| IDE | `GET /v1/content/releases/{release}/manifest?cursor=...` | 본인에게 필터된 불변 파일 목록·digest |
| IDE | `GET /v1/content/releases/{release}/objects/{sha256}` | 해당 manifest에 속한 본인 파일만 전달 |
| IDE | `POST /v1/content/releases/{release}/verify` | 예상 manifest ID·digest·generation·정책 revision pin 및 현재 인가·기간 확인 |
| IDE | `POST /v1/content/reports` | 허용된 상태·오류 코드의 자기보고, 멱등·rate limit |
| 교수자 | `/instructor/api/v1/courses/{course}/content/...` | 목록·초안·업로드·검증·공개·중단·보관·이력 |

manifest 계약 `autograde.content.manifest.v1`: `manifest_id`, `release_id`, `generation`,
`course_key`, `subject_scope`, `policy_revision`, `manifest_sha256`, `files[]`, `next_cursor`.
파일 항목은 `path`, `sha256`, `size_bytes`, `media_type`만 기본 제공하고 임의 외부 URL을 넣지
않는다. Git commit·다른 대상·비공개 이력·서버 파일 경로는 학생 응답에서 제외한다.
manifest digest는 정렬된 경로와 타입을 가진 canonical UTF-8 표현으로 정의하고 schema
버전을 포함한다. 페이지별 digest가 아니라 완성된 manifest 전체의 digest를 검증한다.
자기 참조인 `manifest_sha256`과 페이지별 `next_cursor`는 해시 입력에서 제외한다. 전체
`files[]`의 경로 정규화·정렬, 숫자 표현·키 순서·JSON 직렬화 규칙은 C0 계약과 공통 fixture로 고정한다.

`verify` 입력은 `expected_manifest_id`, `manifest_sha256`, `generation`, `policy_revision`이다.
네 값이 현재 인가된 개인 projection과 모두 일치할 때만 200과 오프라인 허용 종료 시각을
반환한다. 인가·기간 실패는 403, 여전히 권한이 있지만 공개본/정책이 달라졌으면 409다.
파일 내용이 같아도 대상·기간 변경을 이전 정책으로 승인하지 않는다.

- 서버 session으로 신원을 결정하고 모든 객체 요청에서 활성 수강·공개·기간·학생별 manifest
  소속을 확인한다. **해시를 안다는 사실은 접근 권한이 아니다.** 미인가 객체의 존재도 숨긴다.
- 공통 CAS는 수업 namespace, 개인 CAS는 수업+학생 namespace로 구분한다. 전 기관 또는
  타 학생 사이의 중복 제거·존재 조회 API는 MVP에서 제외한다.
- HTTPS·동일 origin·리다이렉트 거절·응답 한도·타임아웃·개인 응답 `no-store`를 적용한다.
  Nginx 공개 정적 경로나 Git HTTP endpoint로 우회 제공하지 않는다.
- 인증 만료 401, 권한/기간 403, 미배정 404, revision 변경 409, 용량 413, 과부하 429와
  Retry-After, 저장소 오류 503을 구분해 표시한다. 서버 원문/비밀정보를 오류에 포함하지 않는다.
- 경로는 상대경로만 허용한다. `..`, 절대경로·링크·장치·submodule·Windows 예약명/ADS·
  끝 공백/점·Unicode 정규화/대소문자 충돌·중복·과도한 길이를 서버와 클라이언트가 검사한다.
- 기본 형식은 기존 PDF/ZIP에 Markdown·텍스트·C/C++ 참고 소스·PNG/JPEG를 명시적으로
  추가하는 계획이다. 바이너리 실행 파일·매크로 문서·HTML 자동 실행은 허용하지 않는다.
  ZIP은 기본적으로 하나의 불투명 파일이며 서버 검사와 학생 수동 해제를 구분한다.
- 제안 초기 한도: 파일 25MiB, release 100MiB/1,000파일, manifest 페이지 100항목/256KiB
  이하, 서버 객체 전송 동시 8개. 실제 배포 전 부하 시험으로 조정하며 현행 API 한도를
  이 문서만으로 늘리지 않는다. MIME·해시 검증은 악성코드 검사를 대체하지 않는다.

### 자료 전용 인증

현재 IDE 자료 수령은 과제 수령 코드 로그인에 의존한다. CMS에서는 과제를 수락하지 않아도
자료를 이용할 수 있어야 한다. 기존 학생 웹 인증에서 **수업 자료 연결 코드**를 발급하고
`content.read` scope만 가진 메모리 세션을 만드는 계약을 후속 구현한다. 이 세션은 제출·채점
권한을 자동 부여하지 않는다. 학번/비밀번호를 IDE 디스크에 저장하거나 Git 인증으로 대체하지 않는다.
초기 서버/캐시 시범은 기존 시험 과제 세션을 재사용하되 독립 CMS 공개 전에 이 인증을 완료한다.

## 8. 관리자·학생 UI/UX

관리자 화면은 `수업·분반 → 콘텐츠`로 진입한다. 교수자에게 Git commit 명령을 요구하지 않고
`자료 모음 / 공개 버전 / 변경 내역`으로 표현한다. 기술 ID·해시는 진단 상세에만 둔다.

```text
콘텐츠                        [수업·분반 선택]   [새 자료 모음]
[자료명 검색] [상태] [공통/개인]
자료명       대상       공개 버전       상태       마지막 공개
Week04       전체       v3              배포 중     10/24
개인 피드백  20명       v2              배포 중     10/24

자료 상세: [파일] [대상·기간] [변경 검토] [버전·배포 현황]
현재 v3 / 작성 중인 초안
추가 1 · 변경 2 · 제외 0 · 그대로 7 / 예상 신규 수신 1.8MiB
[초안 저장]                         [변경 검토 후 공개]
```

1. 자료 모음의 용도·제목을 입력하고 파일을 올린다. 교수자 정답/채점 폴더를 자동 탐색해
   넣지 않는다. 업로드 목록에서 빠진 파일을 자동 삭제하지 않고 `제외`로 명시한다.
2. 수업 전체/선택 학생, 공통/개인 파일, 배포 기간을 정한다. 최종 대상 수와 비활성·미등록·
   분반 불일치 제외 사유, 개인 override 충돌을 검토한다.
3. 기존 공개본 대비 파일·대상·기간 변경과 학생 안내를 한 화면에서 검토한다. ‘초안 저장’은
   학생 공개가 아니다. 내용 검사·대상 검사 통과 후에만 `검토한 버전 공개`를 활성화한다.
4. 공개 중 실패하면 이전 버전이 유지된다. 성공하면 학생에게 새 버전 알림을 제공한다.
   수정은 `수정본 작성`, 복구는 `이 내용으로 복구 초안`에서 새 판으로 공개한다.
5. `배포 중단`과 `관리 목록에서 보관`을 구분한다. 배포 중에는 중단을, 예약이 있으면
   예약 취소를 먼저 안내한 후 보관을 허용한다. 보관 자료는 신규 수령할 수 없지만 이미
   내보낸 학생 파일은 삭제되지 않는다는 안내를 표시한다. 물리 삭제는 일반 교수자 버튼과 분리한다.

학생 화면은 자료별 기본 버튼 하나를 강조한다.

```text
Week04 참고자료                         업데이트 있음
서버 v4 · 이 PC v3 · 변경 2개 / 1.8MiB
“Observer 예제와 설명을 수정했습니다.”
[변경 내용]                            [변경분 받기]

수신·검증 후: 이 PC에 준비됨(v4)          [내 폴더에 저장]
보조 메뉴: 이전 내보내기 / 폴더 열기 / 자동 수신 설정
```

서버 배포 상태, 최신 비교 상태, 수신 상태, 내보내기 상태는 별도 축이다. `수신 중`, `검증 중`,
`준비됨`, `로그인 필요`, `용량 부족`, `오프라인·확인 불가`를 점수·제출 상태와 섞지 않는다.
작은 창에서 주 동작을 유지하고 세부 이력은 접는다. 테마 색 변수·키보드·320px/200% 확대를 검증한다.

### 교수자가 보는 수령 현황

`대상 학생 수 / 파일 요청 승인 / 캐시 저장 성공 보고 / 내보내기 성공 보고 / 오류·확인 오래됨`
을 구분하고 집계 시각·버전을 표시한다. 학생 1명이 여러 PC를 사용할 수 있으므로 상세는
설치별이며, 설치 ID는 앱이 만든 회전 가능한 난수이고 하드웨어 식별자를 수집하지 않는다.
서버 수신 시각과 클라이언트 보고 시각을 분리한다. 완료 이벤트의 위조/중복은 성적에 영향을 주지 않는다.

캐시 저장·내보내기는 **클라이언트 자기보고**다. 보고가 없으면 미수령으로 단정하지 않고
`확인 안 됨`으로 표시한다. 다운로드 요청/성공 보고를 열람·출석·학습 완료 증거로 사용하지 않는다.
파일 내용·개인 로컬 경로·브라우저 활동·스크린샷은 진단에 전송하지 않는다.

## 9. 모듈·SES/SPL 조합

| 목표 모듈 ID | 계약과 책임 | 의존성 |
| --- | --- | --- |
| `content.catalog` | 자료 모음·대상·초안 메타데이터 | 인증·수업 인가·SQLite·감사 |
| `content.authoring` | 업로드·검증·변경 검토 | catalog·artifact 검사 |
| `content.version.git` | Git commit/tree·이력·복구 원본 | 서버 승인 Git adapter |
| `content.objects.local_cas` | SHA-256 불변 객체·보존 참조 | 비공개 저장소·한도·백업 |
| `content.release` | 검증된 공개 포인터·기간·대상·철회 | 위 모듈·멱등 작업 원장 |
| `content.delivery.manifest` | 학생별 manifest·인가된 객체 전송 | release·content.read |
| `content.sync.file_cache` | 누락 파일 수신·snapshot 활성화 | delivery·학생별 로컬 공간 |
| `content.export.copy` | 독립 폴더 복사·덮어쓰기 방지 | 검증 snapshot·사용자 선택 |
| `content.receipts` | 요청·수신/내보내기 자기보고 | 감사·멱등·보존 정책 |
| `ui.content_admin` / `ui.content_student` | 웹/IDE 화면 기여 | 제공된 capability·범위 검사 |

SES 개념 구조(설계용, 현행 JSON에 입력 불가):

```text
CourseComposition [Aspect]
└─ LearningContent [Entity]
   └─ ContentVariant [Specialization]
      ├─ ContentDisabled
      ├─ LegacyPersonalFiles
      └─ GitManagedContent
         └─ ContentParts [Aspect]
            ├─ Catalog + Authoring + GitVersion + Release [필수 계약]
            ├─ ObjectStore → [S] LocalCAS | ManagedObjectStore(후속)
            ├─ Delivery → [S] ManualWebDownload | ManifestWithWebFallback
            ├─ ClientSync → [S] ManualFetch | OptInBackgroundCache
            └─ Audit + CopyExport [필수 계약]
```

`GitManagedContent`는 기존 자료 읽기·호환 adapter를 포함한다. 수업 내 이전 전후 자료가
공존하더라도 자료별 쓰기 원장은 하나이며, `LegacyPersonalFiles`를 별도 SES 대안으로
동시에 선택하지 않는다.

제약: background cache는 manifest 전송·학생별 캐시·크기 한도·세션 전환 취소가 모두 필요하다.
개인 콘텐츠는 본인 인가 없이 공통 cache로 대체할 수 없다. CMS가 꺼져도 제출/채점 원장은
계속 동작한다. CMS 실패가 과제 접수를 막지 않으며 데이터 내보내기 권리를 상품 옵션으로 숨기지 않는다.
서버 예약/GC 스캔은 향후 `scheduler.pyjevsim` 또는 수동 adapter를 선택하되 기간 인가는 매
요청에서 수행한다. IDE의 메타데이터 확인 timer와 서버 scheduler는 다른 구성요소다.

이 ID들은 `implementation_status=contract_only`, `runtime_ready=false`다. 현재 관리 SES
registry나 운영 CSV를 바꾸지 않는다. 구현·계약/보안 시험·제품 지원 행렬 확인 후에만 등록한다.
서명되지 않은 외부 플러그인을 업로드해 바로 실행하는 방식도 제공하지 않는다.

## 10. 단계별 구현 계획

| 단계 | 결과물 | 완료 조건 |
| --- | --- | --- |
| C0 계약 고정 | manifest·경로·객체·인가·상태·멱등성 schema와 합성 fixture | 타학생 비노출·상태/오류 계약 시험 |
| C1 서버 저장·공개 | Git adapter + LocalCAS + SQLite content_* + 기존 웹 진입점 | 충돌·중간 장애·복구·불변 공개본 시험 |
| C2 IDE 캐시·내보내기 | 수동 변경분 수신·snapshot 전환·copy export, 현행 과제 세션으로 시험 | 변경 객체만 전송·중단 복구·답안 보존 |
| C3 콘텐츠 관리자·독립 인증 | Next 콘텐츠 CRUD·공개·배포 현황 + 과제와 독립한 content.read 연결 | 자료-only 학생·교수자 권한·CSRF·실제 Nginx 시험 |
| C4 선택 자동 수신·운영 | opt-in 갱신·용량/보존·문제 진단·백업/복구·수업별 전환 | 20명 동시·공용 PC 전환·플랫폼별 인수 |
| 후속 | byte-range/chunk delta, 승인 원격 Git import, 저장소 대안, OS/VM 격리 | 개별 설계·검증 후 별도 활성화 |

작은 파일럿의 1차 목표는 C0~C2, 학생 대상 CMS 운영 목표는 C3~C4까지다. 교수자 기본 CRUD는
C1부터 기존 웹에 제공하고 C3에서 Next UI로 이행한다. Next 버튼만 먼저 노출하지 않는다.
공개 예약은 준비된 release의 활성 시점만 다루고 자동 GC는 기본 꺼 둔다.

기존 학생 웹 다운로드와 `/v1/materials`는 유지한다. 구형 확장은 기존 PDF/ZIP을 계속 받고,
신규 multi-file 콘텐츠는 인증된 웹에서 전체 묶음 받기를 제공하는 호환 adapter를 구현한다.
기능 협상 실패 시 숨긴 강제 업그레이드나 raw Git fallback 없이 웹 이용을 안내한다.

## 11. 기존 자료 이전·운영

1. SQLite·자료 폴더를 함께 백업하고 복구를 확인한다. Git 이력은 서비스 전체 백업이 아니다.
2. dry-run에서 기존 material/edition/file_revision·대상·해시·기간·요청 이력의 매핑을 검토한다.
3. 멱등 import로 content_*와 Git/CAS를 추가한다. 기존 파일·URL·이력은 삭제하거나 바꾸지 않는다.
4. 수업별 opt-in으로 새 읽기 경로를 비교한다. 한 자료의 수정 원장은 한 서비스만 담당한다.
   전환된 자료는 legacy 편집을 막거나 동일 CMS 서비스로 위임하여 이중 쓰기를 방지한다.
5. 신규 공개본을 시험 계정으로 비교한 뒤 전환한다. 돌아갈 때도 DB를 다운그레이드하지 않고
   기능을 비활성화하고 기존 읽기 경로/승인된 호환 공개본으로 복귀한다. 새 CMS 자료를 없던
   것으로 숨기는 롤백은 허용하지 않는다.

백업은 SQLite 기준점 + 참조 Git commits/refs + CAS 객체 + 설정·암호화 키(추가 시)를 일관되게
보존한다. GC는 활성/과거 보존 release·진행 작업·backup/legal hold 참조를 확인한다. 보존 기간
만료는 검토 후보일 뿐 자동 파괴 승인이 아니다. 교수자 보관과 물리 공간 회수를 구분한다.

CMS 객체 전송·Git 작업·보고서의 동시성을 제한해 채점 큐와 분리한다. 비밀 자료 저장소는
학생 프로그램을 실행하는 `pilot-local`과 같은 권한으로 안전하다고 가정하지 않는다.
OS 격리 미완료 파일럿에서는 비기밀 합성 자료/신뢰 참여자로 제한한다.

## 12. 인수 시험과 측정 기준

아래는 **구현 후 실행할 시험 계획**이며 기존 MVP의 통과 수에 합산하지 않는다.

| 시나리오 | 합격 조건 |
| --- | --- |
| 같은 release 2회 | 두 번째 파일 payload 0건. 메타데이터 통신은 별도 집계 |
| 10개 중 1개 수정 / 이름만 변경 | 수정 객체 1개만 전송 / 이름 변경은 manifest만 전송 |
| ZIP 내부 일부 수정 | ZIP 전체 재수신을 정확히 표시, byte delta로 오인하지 않음 |
| 전송 중 재공개·정책 변경 | 버전 혼합 없이 409·재조회, 이전 정상 snapshot 유지 |
| PC 종료·통신 끊김·디스크 부족·Windows 잠금 | staging 복구/정리 가능, 부분 버전 활성화 없음 |
| 캐시 변조·해시 불일치·악성 경로 | 파일 반영 차단, 학생 workspace 변경 0건 |
| export를 학생이 수정한 뒤 갱신 | 수정본·캐시 원본 각각 보존, hardlink 없음 |
| 학생/서버/수업 전환·토큰 만료 | 지연 응답·완료 알림 폐기, 다른 학생 캐시 목록 노출 없음 |
| 타인 SHA/manifest/release 추측 | 모든 객체·목록 요청 거절, 존재 정보 유출 없음 |
| 예약 후보 생성·활성 시각·늦은 실행 | 활성 전 기존 판 유지, CAS 1회 전환, 만료한 예약은 미공개 |
| 기한 경계·철회·수강 취소 | 신규 요청 거절, 진행 전송 가능한 범위 중단, 최종 verify 실패 시 활성화 금지 |
| 배포 중 보관·예약 보관 | 먼저 중단/취소 요구, 보관 뒤 신규 수령 불가, 기존 export 보존 |
| 오프라인 기한·시계 역행·절전 복귀 | 허용시간 연장 없음, 알려진 철회 유지, 온라인 재확인 필요 시 내보내기 중단 |
| 교수자 2명 동시 변경·공개 재시도 | revision 충돌 안내 또는 같은 멱등 결과, 이중 공개 없음 |
| Git 완료 후 DB 실패 / 공개 후 객체 누락 | 미공개 orphan 재조정 / 운영 오류, 조용한 버전 대체 없음 |
| 과거 내용으로 복구 | 새 generation, 현재 대상 재검토, 기존 이력 보존 |
| 자료-only 로그인 / 구형 IDE | 제출 권한 자동 부여 없음 / 웹 호환 경로 안내 |
| 수신 성공 보고 위조·누락·중복 | 열람·출석·점수에 영향 없음, 미확인/자기보고로 표시 |
| 20명 동시 다운로드+채점 | 제한된 큐·429 재시도, 메모리/잠금/채점 지연 측정, 타작업 고갈 없음 |
| 밝음·어두움·320px·200%·키보드 | 주 동작·상태·오류 안내가 잘리지 않음 |

성능은 총 파일 payload 바이트, 캐시 적중, manifest 왕복, p95 수신/검증 지연, DB 잠금 대기,
채점 접수 지연과 서버 최대 메모리를 분리해 기록한다. ‘증분이라 빨라짐’이라는 추정만으로
배포 승인하지 않는다. Windows/WSL2·Linux·macOS는 각각 파일 잠금·경로·권한·재시작을 인수한다.

## 관련 문서

- [현재 학생별 자료 배포](../operations/student-material-distribution.md)
- [전용 IDE·폴더 분리와 OS 격리 경계](autograde-ide.md)
- [Next.js 전환 계획](nextjs-migration-plan.md)
- [플러그인·SES](platform-plugin-ses.md), [SES 제품군](software-product-line.md)

설계 단계와 달리 이번에는 서버·IDE 코드 및 additive migration을 구현했다. 운영 DB·학생 파일·
운영 설정은 변경하지 않았으며, 운영 적용 전 백업·서버 업데이트·확장 갱신이 필요하다.
