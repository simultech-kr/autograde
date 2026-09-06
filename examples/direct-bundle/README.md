# Direct bundle demo

GitHub 없이 starter를 내려받고 제출하는 local 파일럿 예제입니다. 합성·신뢰 코드로만
사용하며 실제 학생 코드 또는 공식 성적에는 사용하지 않습니다.

- `starter/`: 학생에게 배포되는 파일
- `assessment/`: 서버에만 두는 평가 코드
- `data/`: 서버가 평가 시 별도 workspace에 준비하는 공개 가능한 입력
- `runner/`: 향후 container 배포 계획을 위한 참고 Dockerfile; local 파일럿에서는 사용하지 않음

파일럿에서는 Docker image를 build하거나 registry에 push하지 않습니다. 전체 실행 절차와
local config/roster CSV schema는 `docs/operations/direct-bundle-mvp.md`를 참고하세요.
