# COME2201 · 2026년 2학기 설계 패턴 실습

C++17 단일 파일 실습 10개입니다. 학생은 `main.cpp`의 TODO를 완성해 IDE 확장으로 제출합니다. 각 과제는 상황 설명, 입출력 계약, 공개 예제, 컴파일 가능한 미완성 코드, 교수자용 정답·오답과 비공개 시험을 제공합니다.

학생 개발 환경은 Visual Studio의 폴더/CMake 프로젝트를 기본으로 하며 VS Code·WSL2·Linux·macOS에서도 같은 표준 C++17 코드를 사용할 수 있습니다. Windows 전용 API나 다중 소스 파일 빌드 과제가 아닙니다.

**원격 운영 상태:** 2026-09-22 15:51 KST, `ai.cbchoi.info:20010`의 COME2201에 10개 모두 웹으로 등록·서버 검증·공개했습니다. [공개본과 재시험 기록](../../docs/testing/come2201-2026f.md#원격-배포현장-확인)을 확인하세요. 아래 CLI는 새 서버용이며, 이미 웹 등록된 이 수업에 다시 실행하면 별도 과제가 중복 생성될 수 있습니다.

## 일정

마감은 모두 **한국 시간(Asia/Seoul, UTC+09:00) 13:50**입니다. 수령 시작은 지정하지 않았으므로 학생 공개 후 즉시 수령할 수 있습니다. 매주 시작일에만 보이게 하려면 공개 전에 수령 시작 시각을 별도로 정해야 합니다.

| 주차 | 문제 | 주제·상황 | 마감 |
| --- | --- | --- | --- |
| Week04 | [Problem01](problem01/starter/README.md) | Observer, Decorator · 센서 구독과 메시지 장식 | 2026-09-23 13:50 |
| Week05 | [Problem02](problem02/starter/README.md) | Factory Method, Composite, Facade · PC 구성 견적 | 2026-09-30 13:50 |
| Week06 | [Problem03](problem03/starter/README.md) | Adapter(Adaptor), Template Method · 레거시 센서 보고서 | 2026-10-07 13:50 |
| Week07 | [Problem04](problem04/starter/README.md) | Iterator, Mediator · 팀 메시지 센터 | 2026-10-14 13:50 |
| Week09 | [Problem05](problem05/starter/README.md) | Chain of Responsibility · 지원 요청 라우팅 | 2026-10-28 13:50 |
| Week10 | [Problem06](problem06/starter/README.md) | Builder, Prototype · 보고서 템플릿 제작소 | 2026-11-04 13:50 |
| Week11 | [Problem07](problem07/starter/README.md) | Bridge, Flyweight · 캠퍼스 지도 | 2026-11-11 13:50 |
| Week12 | [Problem08](problem08/starter/README.md) | Memento, Visitor · 실습 계획 편집기 | 2026-11-18 13:50 |
| Week13 | [Problem09](problem09/starter/README.md) | Factory, Decorator, Composite, Facade 조합 · 카페 주문 | 2026-11-25 13:50 |
| Week14 | [Problem10](problem10/starter/README.md) | Observer, Memento, Iterator, Facade 조합 · 실습 진행 보드 | 2026-12-02 13:50 |

## 학생 자료와 교수자 자료

각 문제 폴더는 다음 역할로 나뉩니다.

| 경로 | 역할 | 학생 배포 |
| --- | --- | --- |
| `starter/main.cpp` | TODO가 남아 있는 컴파일 가능한 시작 코드 | 포함 |
| `starter/README.md` | 문제·예제·제출·평가 안내 | 포함 |
| `starter/CMakeLists.txt` | VS 폴더 열기와 C++17 빌드 | 포함 |
| `instructor/solution.cpp` | 만점 기준 구현 | 제외 |
| `instructor/negative.cpp` | 컴파일은 되지만 출력하지 않는 0점 검증 코드 | 제외 |
| `assignment.json` | 설정, 마감, 전체 공개·비공개 시험 | 제외 |

이 카탈로그 전체는 교수자용 원본입니다. 학생에게 저장소 전체 또는 `assignment.json`을 전달하지 마세요. 아래 내보내기 기능은 학생용 `starter.zip`과 교수자용 `grading-private.zip`을 분리합니다.

기능 자동채점은 과제별 **100점**입니다. 전체 57개 시험이 정상 동작, 순서, 경계, 잘못된 요청, 실패 후 상태 보존 등을 확인합니다. 표준 입출력 정답만으로 설계 패턴 사용을 증명할 수 없으므로, 각 README의 패턴 구조 체크리스트를 교수자가 제출 코드와 함께 별도로 검토합니다. 이 체크리스트는 자동으로 기능 점수에 합산되지 않습니다.

## 웹 화면에서 등록하기

저장소의 `autograde` 폴더에서 프로젝트가 설치된 Python 환경으로 실행합니다. 환경변수 설정은 필요하지 않습니다.

```sh
python -m autograde.workshop_catalog \
  --catalog examples/come2201-2026f \
  --export /tmp/come2201-2026f-web-upload
```

이 명령은 DB를 변경하지 않습니다. 출력 폴더 아래 문제별로 `starter.zip`, `grading-private.zip`, `assignment.json`이 생성됩니다. 내보내기 폴더는 교수자만 접근할 수 있는 위치를 사용하세요. 같은 출력 경로로 재실행하면 그 경로의 내보내기 파일을 갱신합니다.

1. 교수자 웹에서 **COME2201 → 과제 관리 → 직접 만들기**로 새 초안을 만듭니다.
2. **문제·일정**에서 해당 문제의 제목, README 전체 설명, C++17, 학생 개발 환경, 한국 시간 마감을 입력하고 저장합니다. `assignment.json`은 입력 참고 자료이며 웹 JSON 일괄 업로드 기능은 아닙니다.
3. **학생 배포 자료**에서 해당 문제의 `starter.zip`을 등록합니다.
4. **교수자 채점 자료**에서 `grading-private.zip`을 선택하고 **채점 템플릿 한 번에 저장**을 누릅니다. 정답·오답·시험·배점을 함께 저장합니다.
5. 준비 현황에서 학생/채점 자료 등록과 100점 만점을 확인하고 **현재 저장 버전 검증 시작**을 누릅니다. 정답 100점, 오답 0점이어야 합니다.
6. 최신 저장 버전 검증이 성공하면 마감을 확인하고 **학생 공개**를 진행합니다.

과제 저장 후 화면이 갱신되므로 영역 하나씩 저장하세요. 서버는 공개 README를 문제 설명에서 생성하므로 **문제 설명에 README 전체 내용을 넣어야** 상세 요구사항이 학생에게 전달됩니다. 카탈로그 등록기는 이 작업을 자동으로 수행합니다.

이미 작성한 실습을 올리는 경우 `기본 템플릿을 서버에 저장` 대신 해당 실습의 `starter.zip`을 업로드하세요. 기본 템플릿 저장은 이 카탈로그의 TODO 코드를 대신 등록하는 기능이 아닙니다.

## 서버 설정 CSV로 일괄 등록하기

등록기는 서버의 과제 작성 서비스를 사용하므로 생성한 초안·공개본을 동일한 교수자 웹에서 확인할 수 있습니다. `--config`는 **현재 서버가 실제로 사용 중인 설정 CSV**를 지정합니다. 아래 `pilot/portal.https.local`은 운영 문서의 예시 경로이므로 실제 파일명이 다르면 바꿉니다.

먼저 진행 중인 학생 제출·교수자 검증을 확인하고 서버를 정상 종료합니다. 설정 CSV, 해당 `data_root` 전체(SQLite와 bundle 등), 필요한 자격 파일을 접근 제한된 별도 위치에 백업한 뒤 실행합니다. DB 파일만 복사하면 과제 파일까지 함께 복구할 수 없습니다.

등록 대상 `come2201` 수업이 준비되어 있어야 하며, 설정의 `grading_runtime`은 `pilot-local`이어야 합니다. 표시 이름 COME2201과 URL/내부 수업 키 `come2201`을 구분하세요. 다른 분반 키를 대상으로 할 때는 `--course`를 실제 키로 변경합니다.

초안을 등록하고 정답·오답을 검증하되 학생에게 아직 공개하지 않으려면:

```sh
python -m autograde.workshop_catalog \
  --catalog examples/come2201-2026f \
  --config pilot/portal.https.local \
  --course come2201 \
  --validate
```

검토한 자료를 전체 검증 후 공개하려면:

```sh
python -m autograde.workshop_catalog \
  --catalog examples/come2201-2026f \
  --config pilot/portal.https.local \
  --course come2201 \
  --validate --publish
```

`--validate`는 교수자 정답·오답 코드를 로컬에서 컴파일하고 실행합니다. `--publish`는 `--validate`와 함께만 사용할 수 있습니다. 이번 실행의 모든 문제 검증이 통과하기 전에는 새 학생 공개를 시작하지 않습니다. 검증 실패 시 등록된 초안은 보존되며 오류를 확인할 수 있습니다. 공개는 개별 과제별로 처리하므로 최종 출력의 상태를 확인하세요.

문제별 진행 JSON이 출력되고 마지막 JSON의 `ok`, `count`, `assignments`에서 결과를 확인할 수 있습니다. `due_at`은 UTC로 출력될 수 있으며 한국 시간 13:50은 UTC 04:50입니다. 완료 후 **원래 서버 실행 명령과 같은 설정 CSV**로 서버를 재시작하고 교수자 과제 목록을 확인합니다.

운영 서버와 분리한 시험 저장소를 사용할 때는 `--config` 대신 `--data-root /절대경로/시험데이터`를 지정합니다. `--export`, `--config`, `--data-root` 중 하나만 사용할 수 있습니다. 시험 저장소에도 대상 수업이 준비되어 있어야 합니다.

### 재실행 시 보존 규칙

- 문제별 고정 생성 키로 같은 초안을 찾아 중복 등록을 방지합니다.
- 등록된 내용이 같으면 같은 초안을 사용하고 누락된 자료 등록을 이어갈 수 있습니다.
- 교수자가 제목·설명·일정·시험 또는 업로드 파일을 바꿨다면 충돌로 중단합니다. 기존 수정을 덮어쓰는 옵션은 없습니다.
- 삭제한 초안을 다시 만들거나 공개본·학생·제출·점수를 초기화하지 않습니다.
- 이미 공개한 과제의 숨김 상태나 연장된 마감을 원본 카탈로그 값으로 되돌리지 않습니다.
- 실행 중 서버와 충돌하는 잠금 또는 기존 대기/실행 중 검증 작업이 있으면 실행을 거절합니다.
- 마감이 지난 과제의 새 공개는 서버 규칙에 따라 거절됩니다. 마감을 변경하려면 교수자 화면에서 의도한 새 일정을 설정하고 재검증합니다.

운영 서버 접속 및 업로드 설정은 [교수자 웹 관리 안내](../../docs/operations/instructor-web-mvp.md)를, 검증 범위와 후속 현장 확인은 [COME2201 시험 기록](../../docs/testing/come2201-2026f.md)을 참고하세요.
