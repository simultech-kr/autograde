# Java Observer Pattern pilot assignment

소프트웨어 설계의 Observer Pattern을 실제 객체 협력으로 확인하는 Java 파일럿
과제입니다. 문자열 검색이나 클래스 이름의 존재 여부가 아니라, 서버의 테스트
harness가 observer 등록, 상태 변경, 알림, 중복 등록, 해제를 직접 실행해 평가합니다.

- `starter/`: 학생에게 배포할 Java 소스와 과제 설명
- `assessment/`: 서버에서만 사용하는 `grade.py`와 행동 테스트 harness
- `data/`: 채점 시 서버가 별도 주입하는 측정 시나리오

## 교수자 등록 예시

저장소 루트에서 다음과 같이 등록합니다. 파일럿 설정은 `pilot/course.csv`를
사용하며 환경변수나 Docker가 필요하지 않습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.csv assignment bundle-add observer-java \
  --assignment-id basn_observer_java \
  --release-id observer-java-v1 \
  --title "Observer Pattern - Java" \
  --starter examples/observer-java/starter \
  --assessment examples/observer-java/assessment \
  --data examples/observer-java/data \
  --max-score 10
```

채점 호스트에는 `javac`와 `java`가 모두 동작하는 JDK 11 이상이 필요합니다.
평가기는 먼저 작은 프로그램을 컴파일하고 실행하여 도구 체인을 확인합니다.
macOS의 실행 안내용 `/usr/bin/java` stub처럼 파일만 존재하고 JDK가 없는 경우도
`jdk_unavailable` 채점 인프라 실패로 처리하며 학생에게 0점을 게시하지 않습니다.
마찬가지로 서버 측 `scenarios.csv`가 없거나 손상된 경우에도 성적 대신 채점 실패로
기록하여 교수자가 환경을 수정한 뒤 다시 채점할 수 있게 합니다.

이 평가는 파일럿용 로컬 프로세스에서 학생 코드를 실행합니다. 신뢰 가능한 합성
코드로만 시험하고 공식 성적이나 검증되지 않은 학생 코드에는 사용하지 마세요.
