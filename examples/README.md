# Examples

- [pattern-workshops/](pattern-workshops/README.md): C++17 옵저버·데코레이터 문제 상황, 학생 TODO 코드, 공개 검사와 AI 검토 프롬프트. 참고 정답은 별도 교수자 폴더.

- `assignments/`: 공개 가능한 최소 예제 과제와 채점 규칙
- `direct-bundle/`: GitHub 없는 starter 배포·직접 제출·파일럿 채점 예제
- `observer-java/`: Java로 구독·알림·해제를 구현하는 Observer 패턴 과제
- `observer-cpp/`: C++로 구독·알림·해제를 구현하는 Observer 패턴 과제
- `submissions/`: 향후 수동 제출 예제를 둘 빈 자리. 현재는 `.gitkeep`만 있으며 바로 제출할
  성공·실패 답안 파일은 제공하지 않음.

예제는 자동 테스트와 로컬 데모에서 재사용할 수 있도록 작고 결정적으로 유지합니다.

현재 이 디렉터리의 `roster.csv`는 기존 Git collection 형식 참고 자료이며 local
direct-bundle 파일럿 roster로 사용하지 않습니다. 별도로 만들 필요 없이 repository의
[`pilot/roster.csv`](../pilot/roster.csv)에 있는 `student_key,active,password` 20명 합성 예제를
사용합니다. 이 비밀번호는 로컬 시험 전용이며 외부 파일럿이나 실제 학생에게 사용하지 않습니다.
Roster 작성·import 절차는
[`docs/operations/direct-bundle-mvp.md`](../docs/operations/direct-bundle-mvp.md)를 따릅니다.

정답·대표 오답은 `examples/submissions/`에 존재한다고 가정하지 않습니다. Observer 과제의
자동 시험은 test 내부에서 합성 구현을 만들며, 수동 Extension 왕복에서는 과제 계약에 따라
검토자가 직접 작성한 합성 답안을 사용합니다.
