# COME2201 README 보강 및 공개본 갱신 기록

확인일: 2026-09-22, 한국 시간. 원격 상태는 17:25 KST 기준입니다.

## 변경 범위

[과제 목록과 README](../../examples/come2201-2026f/README.md)의 Problem01~10 설명을 보강했습니다.

- 실제 문제 상황과 학습 목표, 패턴별 책임과 구현 순서
- 명령·입출력 형식, 오류 검사 우선순위, 실패 후 상태 보존과 경계 조건
- 과제별 실행 예제 2쌍 및 결과 해설: 총 20쌍
- Visual Studio 2022/2026 Community, Linux/macOS/WSL2 빌드·실행 안내
- 단일 `main.cpp` 제출 범위, 자기 점검, FAQ
- 기능 자동채점 100점과 교수자의 패턴 구조 평가가 별개임을 명시

마감, 제목, 배점, 채점 시험, 스타터 C++ 코드, 정답·오답 코드는 변경하지 않았습니다.
비공개 시험 원문과 정답 구현은 README에 포함하지 않았습니다.
각 README는 서버 설명 제한인 20,000자 이내입니다.

## 로컬 시험

```sh
.venv/bin/python -m pytest -q \
  tests/integration/test_workshop_readme.py \
  tests/integration/test_workshop_catalog.py
```

최종 결과: **27 passed, 71.01초**.

- README의 20개 입력·출력 예제를 정답 프로그램의 실제 실행 결과와 정확 비교했습니다.
- 과제별 서로 다른 입력 형식(Problem03의 센서 입력, Problem05의 체인·요청 입력 포함)을 검사합니다.
- 10개 과제 등록·검증·공개, 마감, 배점, 중복 등록·변경 보존을 확인했습니다.
- 학생 로그인→수령 코드→수락→다운로드→정답 제출 100점→오답 재제출 0점 흐름을 확인했습니다.
- 재제출 대기 중 이전 점수를 현재 점수로 표시하지 않고, 이전 최고점 100점을 별도로 유지하는지 확인했습니다.
- 다운로드한 README가 전체 문제 설명과 동일하며, 정답·비공개 시험이 학생 자료에 없는지 확인했습니다.

시험은 별도 임시 저장소와 로컬 HTTP 서비스에서 실행했습니다. 운영 학생의 자격 정보나 제출물을 사용하지 않았습니다.
실제 Windows의 Visual Studio 또는 VS Code 확장을 통한 설치·다운로드 조작은 이번 시험 범위에 포함하지 않습니다.
원격 학생 수령 페이지는 로그인 화면까지 확인했지만, 기존 학생 로그인 세션이 만료되어 운영 계정의 수령·다운로드·제출을 새로 수행하지는 않았습니다.

## 원격 반영

대상: [COME2201 과제 관리](https://ai.cbchoi.info:20010/courses/come2201/instructor/assignments).

공개본을 직접 덮어쓰지 않고 **복제 초안 → README 전체를 문제 설명에 저장 → 서버 재검증 → 이전 공개본 숨김 → 새 공개본 공개** 순서로 처리했습니다.
기존 스타터·채점 코드는 복제해 유지했습니다. 서버는 검증·공개 시 문제 설명에서 학생용 README를 다시 생성하므로,
단순히 로컬 ZIP만 변경한 상태가 아닙니다. 각 개선본은 원격 서버에서 정답 **100점**, 오답 **0점**을 확인했습니다.

Problem02~10은 교체 직전에도 수락·제출 0건을 확인했습니다. 이전 9개 공개본은 삭제하지 않고 숨김 상태로 보존합니다.
교수자 전체 목록에는 이전 버전도 보이므로 과제 개수가 늘어 보일 수 있지만, 학생 목록에는 새 공개본만 표시됩니다.

| 문제 | 현재 운영 상태 | 개선 초안 | 새 공개본 ID |
| --- | --- | --- | --- |
| Problem01 | 기존 공개본 유지, 개선 초안 검증 통과 | `draft_BSVV3FKaW5pc2Y630eXpcgLV` | 미공개 |
| Problem02 | 개선본 공개 | `draft_YVKBP09lZEmxd7zHk5akMRoR` | `basn_3ttorn28O4rY59srcEA0p03T` |
| Problem03 | 개선본 공개 | `draft_O6aMY5tAKdxk5YKqRDNhIdqe` | `basn_8Hiph7b7DqEoL_3hODmWOHoh` |
| Problem04 | 개선본 공개 | `draft_eaWUIOf9YnCMIdJHPt62yP-n` | `basn_2oVEIhKKtNw57W7H5yS6CEIj` |
| Problem05 | 개선본 공개 | `draft_2A9tvKYPcQ9glPhiDqII2W2x` | `basn_kwbsL4Yr70YuwW5KScl--dk4` |
| Problem06 | 개선본 공개 | `draft_yjQuERS8zpiANQuMXA5Oy08g` | `basn_H2f-xGgFgp2ulAyeiyLV1wRn` |
| Problem07 | 개선본 공개 | `draft_ruIUtYVHIfw7wt0UyNQPWemi` | `basn_ivgfMerpdxuQSO88bBcsGHAK` |
| Problem08 | 개선본 공개 | `draft_KjiPMxpOLL2XhgL0oNCTql9L` | `basn_ZDi4Ac2AAgoYFubGE9S9Xh83` |
| Problem09 | 개선본 공개 | `draft_nr9V84maVNq_mnzxfVSs7wiI` | `basn_EF_-MyLAXmf0ypQHFHdiuDf6` |
| Problem10 | 개선본 공개 | `draft_x7E_GCZuhgFYNExTG8Pe5RKq` | `basn_wlxhX6Ak7cbX8abkSYNzCV8F` |

Problem01의 기존 공개본 `basn_P22dghkjbAGFcRadIidCSpgy`에는 수락 1명·제출 1건이 있습니다.
이를 보관하고 새 버전으로 교체하면 기존 수락본으로 추가 제출할 수 없으며 학생이 다시 수락해야 합니다.
당시에는 이 교체를 적용하지 않았습니다. 이전 점수가 새 과제로 자동 이관되는 것도 아닙니다.
이후 **같은 과제 ID를 유지하는 별도 설명 수정 기능**을 로컬 구현했습니다.
[업데이트 및 Problem01 적용 절차](../operations/assignment-document-revisions.md)에 따라
새 서버 배포 후 설명만 갱신할 수 있습니다. 기존 과제 숨김·새 과제 공개·재수락은 필요하지 않습니다.
이 기능의 운영 배포와 원격 Problem01 설명 갱신은 아직 수행하지 않았습니다.
기존 Hello World 과제와 학생·제출·점수 기록은 변경하지 않았습니다.

## 갱신 자료 내보내기

```sh
python -m autograde.workshop_catalog \
  --catalog examples/come2201-2026f \
  --export build/come2201-2026f-readme-update
```

10개 자료 내보내기 완료. 문제별 `starter.zip`은 학생용이며 `grading-private.zip` 및 `assignment.json`은 교수자 전용입니다.
카탈로그와 내보내기 자료는 Problem01의 개선 README도 포함하지만, 그 개선본은 아직 원격 학생 공개되지 않았습니다.
이 명령은 자료만 생성하며, 기존 운영 서버 갱신을 위해 등록기를 다시 실행해서는 안 됩니다.
