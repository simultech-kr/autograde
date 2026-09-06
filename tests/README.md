# Tests

- `unit/`: domain/state/auth/HTTP/scheduler/CLI/workspace/grader의 빠른 결정적 테스트
- `integration/`: 실제 local Git repository와 HTTP server를 사용하는 collector/platform
  테스트 및 25명 direct-bundle 동시 제출·QR/password 과제 수령·assignment scope 검증
- `integration/test_observer_assignment_catalog.py`: Java/C++ 옵저버 과제의 비공개 등록과
  공개 전환 검증
- `integration/test_observer_*_assessment.py`: 컴파일·행동·부분점수와 채점 인프라 실패 검증
- `e2e/`: 향후 실제 container daemon과 GitHub test organization 종단 테스트
- `fixtures/`: 민감 정보가 없는 공용 fixture

전체 실행: `pytest -q`
