# Week09 · Problem05: 지원 요청 라우팅 체인
- 교과목: COME2201 / 마감: **2026-10-28 13:50 KST**
- C++17 / 제출: `main.cpp`
- 패턴: Chain of Responsibility

## 문제 상황
실습 지원 센터는 업무별 담당자와 긴급 담당자를 조합합니다. 요청은 체인의 앞에서 시작해 담당할 수 없는 노드가 다음 노드로 넘기며, **처음 처리한 노드에서 끝납니다**. 운영자가 순서를 바꾸면 같은 요청의 담당자가 달라질 수 있습니다. main에 모든 분기 규칙을 넣지 말고 Handler 객체를 입력 순서대로 연결하세요.

## 입력
1. 첫 정수 K(0..4): 체인 길이.
2. K개의 handler 키(공백/줄바꿈 구분). 중복 또는 알 수 없는 키가 있으면 `ERROR config` 한 줄 출력하고 종료.
3. 정수 Q(1..100): 요청 수.
4. Q개 줄의 `id category severity`.
   - id: 공백 없는 영문/숫자/밑줄 1..20자.
   - category: LOGIN, NETWORK, BILLING, OTHER. 다른 값은 해당 요청을 오류 처리.
   - severity: 정수. 유효 범위 1..5. 다른 정수는 해당 요청을 오류 처리.
문법/토큰 수는 올바릅니다. K=0이면 키를 읽지 않고 곧바로 Q를 읽습니다.

| handler 키 | 처리 조건 | 결과 |
| --- | --- | --- |
| AUTH | category=LOGIN (severity 무관) | AUTH_DESK |
| NETWORK | category=NETWORK (severity 무관) | NETWORK_DESK |
| BILLING | category=BILLING (severity 무관) | BILLING_DESK |
| ESCALATE | severity가 4 이상 (category 무관) | SENIOR |

입력 순서를 그대로 사용합니다. 끝까지 아무도 처리하지 않거나 체인이 비어 있으면 UNHANDLED입니다. 따라서 `ESCALATE AUTH`에서 LOGIN/5는 SENIOR, `AUTH ESCALATE`에서는 AUTH_DESK입니다.

## 출력
정상 요청은 `id: 결과`. category 오류는 `id: ERROR category`, severity 오류는 `id: ERROR severity`이며 다음 요청을 계속 처리합니다. 둘 다 잘못되면 category 오류가 우선입니다. 잘못된 요청은 체인에 전달하지 않습니다.

### 예시
입력:
```text
4
ESCALATE AUTH NETWORK BILLING
5
r1 LOGIN 1
r2 NETWORK 3
r3 BILLING 4
r4 OTHER 5
r5 OTHER 2
```
출력:
```text
r1: AUTH_DESK
r2: NETWORK_DESK
r3: SENIOR
r4: SENIOR
r5: UNHANDLED
```

## 작업과 평가
main.cpp의 TODO를 완성하세요. CMake 폴더를 Visual Studio로 열거나 `c++ -std=c++17 main.cpp -o lab`로 빌드합니다. 표준 출력에는 정해진 결과만 쓰세요.

기능 자동채점은 100점입니다. 패턴 구조는 별도 루브릭으로 확인합니다.
- 공통 Handler가 후속 handler를 소유/참조하고 실패 시 위임하는가?
- 구체 handler가 자신의 처리 조건과 결과만 책임지는가?
- 첫 성공 이후 다음 handler가 실행되지 않는가?
- 체인의 순서/일부 담당자 생략/빈 체인이 main의 업무 분기 수정 없이 처리되는가?
- 구성 오류와 요청 오류를 구분하고 요청 오류 후 다음 요청을 계속 처리하는가?
