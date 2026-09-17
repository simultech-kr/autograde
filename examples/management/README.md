# SES 기반 수업 운영 구성 미리보기

실제 학생 데이터나 서버 설정을 변경하지 않는 구성 예시다. 모든 모듈은 **contract_only**이며
보관·압축·보고서·학생 분석·알림·스케줄러를 실제로 실행하지 않는다.

## 파일

- [구조 모델](../../design/ses/management.ses.json): Entity / Aspect / Specialization / MultiAspect와 module_id.
- [수업별 선택](course-profiles.json): come2201, come3105의 variant와 attribute 예시.
- [전체 설계](../../docs/architecture/management-ses.md): 개선 우선순위, 보관 안전장치, 보고서 지표, 학습 지원과 구현 순서.

| 예시 수업 | 보관 | 보고서 | 지원 분석 | 실행 일정 |
| --- | --- | --- | --- | --- |
| come2201 | 제출·이의신청·결과 확정 후 7일 지연, 보관 후 180일 검토 | HTML + CSV, 최신 접수본 기준 | 최소 유효 평가 3개, 2회 연속 미제출/낮은 점수 60% 예시 | PyJevSim 300초 tick 계약 |
| come3105 | 사용 안 함 | CSV, 교수자 확정 점수 기준 | 사용 안 함 | 수동 |

과목 코드는 예시일 뿐 실제 DB에 존재하는지 확인하거나 그 수업에 적용하지 않는다.
보존·학습 기준값은 운영자가 검토할 초깃값이며 검증된 교육 효과나 의무 보존 기간이 아니다.

## 실행

프로젝트 루트에서 기존 Python 환경으로 실행한다.

    python -m autograde.management_ses --model design/ses/management.ses.json --profile examples/management/course-profiles.json

출력은 JSON이며 ok: true, runtime_ready: false, side_effects: none,
수업별 선택 모듈, specialization을 제거하고 수업 반복을 펼친 PES 트리,
모델 해시와 구성 해시를 포함한다. 오류면 stderr JSON과 종료 코드 2를 반환한다.
적용/삭제 옵션은 없다. 운영용 환경변수·DB·네트워크 연결도 필요 없다.

## 사용자 정의

1. 예시 profile을 별도 파일로 복사한다. 실제 비밀번호·학생명단을 넣지 않는다.
2. course_key와 selections를 수정한다. 각 specialization에서 정확히 하나를 선택한다.
3. 선택 모듈에 필요한 parameters만 제공한다. 미사용 모듈 파라미터는 제거한다.
4. 위 명령의 --profile에 새 파일을 지정해 검증한다.
5. runtime_ready=false인 계획을 운영 설정으로 오인하지 않는다. 실제 적용기는 후속 구현이다.

예: come2201의 분석을 끄려면 support_mode를 SupportDisabled,
notification_mode를 NotificationsDisabled로 바꾸고 support.rules.v1 파라미터를 제거한다.
보고서까지 끄려면 report_mode를 ReportsDisabled로 바꾸고 reports.html_csv.v1 파라미터도 제거한다.
보관을 쓰면서 scheduler_mode만 ManualScheduler로 바꾸면 의존성 오류가 난다.

새 구현 모듈은 Model Base 등록·입출력 계약·SES 대안·파라미터 검증·의존성·테스트를 함께 추가한다.
설정 문자열로 임의의 Python 모듈을 import하거나 함수를 실행할 수는 없다.

## 검사

    python -m pytest -q tests/unit/test_management_ses.py

이번 시험은 구성 검사다. 실제 압축·복원·보고서 수치·학습 지원 정확도·동시 제출 성능은 시험하지 않는다.

2026-09-17 로컬 검증: 신규 SES 구성 검사 36개와 기존 수업 관리·운영·스케줄러 시험을 합쳐
91개 통과. 두 수업의 CLI 미리보기, 문서 파싱·상대 링크도 확인했다.

    python -m pytest -q tests/unit/test_management_ses.py tests/unit/test_course_admin.py tests/unit/test_course_operations.py tests/unit/test_scheduler.py
