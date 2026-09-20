# 루브릭 로컬 모듈 시험 자료

이 디렉터리는 [루브릭 CLI 1차 구현](../../docs/operations/rubric-local-mvp.md)의 실행 입력이다.
설계용 `design/rubrics/*.example.json`과 schema가 다르며 운영 학생 서비스 설정이 아니다.

- `observer.rubric.json`: C++ 옵저버 루브릭 3항목, 만점 100점.
- `hybrid.profile.json`: 규칙 + 수동 판정 취합을 선택하는 제한된 SES profile.
- `synthetic.evidence.json`: **실제 코드 없이 만든 합성 근거**. 예상 합계 82.5점.

해시·줄 범위·교수자 이름·검사 결과는 시험용 주장이다. 실제 소스/로그인/검사 실행을 증명하지 않는다.
엔진이 이 자료를 실제 학생 평가로 승격하거나 자동 공개하지 않는다.
직접 시험하려면 위 안내의 validate → compose → 새 전용 DB init → register → approve → evaluate를 따른다.
