# Source

제품 코드는 `src/autograde/` Python package에 있습니다.

- `cli.py`: JSON-only CLI와 command routing
- `domain.py`: 불변 domain record와 상태 enum
- `state.py`: SQLite migration, 조회, 멱등 상태 전이
- `gitops.py`: bare fetch, exact-SHA ref와 source archive
- `events.py`, `scheduler.py`: PyJevSim due event와 worker bridge
- `service.py`: repository 병렬 수집 application service
- `roster.py`: 학교 roster CSV mapping
- `workspace.py`: submission/assessment/data 입력 준비
- `settings.py`: runtime 경로
- `platform_auth.py`: 학생 활성화 코드, opaque token과 device pairing 보안 primitive
- `platform_github.py`: 선택적 GitHub OAuth 호환 identity adapter
- `platform_state.py`, `platform_service.py`, `platform_http.py`: 학생 플랫폼 영속 상태와 HTTP API
- `platform_bundle.py`, `platform_bundle_worker.py`: 안전한 immutable bundle CAS와 4-worker
  direct 제출 채점
- `platform_pinner.py`, `platform_worker.py`: 접수 시점 exact-SHA snapshot과 durable grading 복구
- `platform_grader.py`: Docker/Podman 실행 제약과 public result sanitizer
- `platform_cli.py`: course-scoped 학생 플랫폼 운영 CLI

학생 코드는 host에서 실행하지 않고 `platform_grader.py`의 제한된 OCI container에서만
실행합니다. 현재 한 container에 assessment/data도 read-only mount하므로 confidential-test
격리는 trusted runner의 별도 child sandbox 계약으로 남습니다.

후속 기능의 경계는 `api/`(외부 API), `core/`(공용 domain service), `execution/`(sandbox orchestration), `graders/`(채점 adapter), `integrations/`(GitHub/LMS)로 예약해 두었습니다. 현재 실행 코드는 import 가능한 단일 `autograde` package에만 둡니다.
