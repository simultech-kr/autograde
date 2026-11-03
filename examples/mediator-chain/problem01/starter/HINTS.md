# Mediator + Chain of Responsibility 구현 힌트

README의 계약과 평가 ID를 기준으로 점검합니다. 아래 로컬 재현은 직접 입력을 만들기 위한 예입니다. 첫 줄에 실제 명령 개수를 적고, 명령을 한 줄에 하나씩 입력합니다. 실패 원인을 특정 소스 행으로 단정하지 말고 전송 경로와 상태를 확인합니다.

## 먼저 연결할 흐름

전송은 `StudentTerminal → ILabMediator → LabMediator → Handler 연쇄 → 선택한 StaffTerminal` 순서입니다. 단말은 자신의 ID를 붙여 전송을 위임하고, 연쇄는 담당자만 선택하며, Mediator는 실제 전달 성공 여부를 결정합니다.

`Handler::handle`은 **자신의 이름 기록 → 수락 검사 → 수락하면 담당자 반환 → 거절하면 다음 Handler 호출 → 다음도 없으면 빈 optional** 순서입니다. 다른 Handler가 반환한 결과도 그대로 호출자에게 돌려줘야 합니다. 반환값을 버리면 뒤쪽 Handler가 수락해도 처리자 없음으로 끝납니다.

`std::optional<std::string>`은 담당자 이름이 있는 상태와 결과가 없는 상태를 구분합니다. 빈 optional을 확인하기 전에 역참조하지 않습니다. 담당자 이름이 있다는 것은 선택 성공이며, 온라인 전달 성공까지 뜻하지는 않습니다.

`buildChain`의 연결 방향을 확인합니다. 앞에 노드를 붙이는 방법이면 마지막 Handler부터 만들어야 최종 방문 순서가 맞습니다. `std::unique_ptr`는 복사하지 않고 소유권을 이동합니다. 원시 포인터를 보조 참조로 사용하더라도 같은 객체를 두 소유 포인터에 넣거나 지역 객체의 주소를 반환하지 않습니다.

`LabMediator::send`는 먼저 trace를 비운 뒤 성공 ID를 검사합니다. 성공 상태를 기록하는 지점은 온라인 담당자의 수신함에 문의를 전달하는 경로 한 곳입니다. 실패 분기에서 ID를 예약하거나 성공 횟수를 증가시키지 않습니다.

## C1 일반 CODE 선택

1. 기본 모드에서 우선순위 1 또는 2인 CODE 문의를 보냅니다. 결과는 TUTOR이며 trace는 `PRIORITY>CODE`입니다.
2. `KindHandler::accepts`가 자신에게 설정된 종류와 `request.kind`를 비교하는지 확인합니다. 모든 KindHandler가 CODE만 검사하면 뒤쪽 분야도 잘못 동작합니다.
3. CODE가 수락했는데 UNHANDLED이면 `handle`의 반환 전파와 Mediator의 결과 사용을 확인합니다.

## C2 일반 ENV 선택

1. `SEND 42 B ENV 2` 뒤 `TRACE`, `INBOX TECH`를 조회합니다. 담당자는 TECH, 방문 순서는 `PRIORITY>CODE>ENV`입니다.
2. CODE에서 거절한 뒤 다음 Handler로 같은 요청과 같은 trace를 전달하는지 확인합니다.
3. ENV Handler의 종류와 담당자 설정을 구분합니다. 검사할 문자열은 ENV이고 반환할 문자열은 TECH입니다.

## C3 일반 GRADE 선택

1. 우선순위 1인 GRADE를 전송합니다. 기본 모드에서 네 Handler를 거쳐 PROF가 선택되어야 합니다.
2. 연쇄 끝의 GRADE Handler가 연결되어 있는지 확인합니다. 마지막 Handler에도 수락 검사는 필요합니다.
3. `next_`가 없다는 이유로 현재 Handler의 수락 검사까지 생략하지 않습니다. 끝 노드도 수락하면 값을 반환합니다.

## C4 우선순위 3 선택

1. 기본 모드에서 서로 다른 ID로 CODE 3, ENV 3, GRADE 3을 보냅니다. 모두 PROF가 받으며 각 trace에는 PRIORITY 하나만 남습니다.
2. 우선순위 조건은 정확히 `priority == 3`이고 OTHER 제외 조건도 함께 필요합니다.
3. 일반 분야 Handler에서 우선순위를 따로 제한하지 않습니다. DOMAIN 모드에서는 CODE 3도 CODE Handler가 먼저 수락해야 합니다.

## C5 방문 기록과 첫 일치

1. 기본 모드에서 CODE 1 뒤 TRACE, GRADE 1 뒤 TRACE를 비교합니다. 각각 `PRIORITY>CODE`, `PRIORITY>CODE>ENV>GRADE`입니다.
2. 방문 이름은 수락 검사 전에 추가합니다. 거절한 Handler도 방문한 것이므로 trace에 포함됩니다.
3. 수락 후 다음 Handler를 호출하지 않습니다. trace를 만들기 위해 연쇄 전체를 별도로 순회하지도 않습니다. 두 번 TRACE를 조회해도 같은 결과여야 합니다. 중간에 ONLINE을 실행해도 기존 방문 기록은 유지합니다.

## C6 모드에 따른 연쇄 순서

1. PRIORITY에서 CODE 3을 보내고, DOMAIN으로 바꾼 뒤 새 ID로 같은 종류·우선순위를 보냅니다. 담당자는 PROF에서 TUTOR로 달라집니다.
2. DOMAIN은 `CODE→ENV→GRADE→PRIORITY`이며 조건 자체를 변경하는 명령이 아닙니다. OTHER 3을 보내면 네 Handler를 모두 방문하고 UNHANDLED입니다. 마지막 PRIORITY를 생략하지 않습니다.
3. PRIORITY로 복원한 뒤 새 ID의 ENV 3이 다시 PROF로 가는지 확인합니다. 매 모드 변경은 연쇄를 새 순서로 교체하고 trace를 비웁니다.

## C7 OTHER 처리자 없음

1. 기본 모드에서 OTHER 1과 OTHER 3을 서로 다른 ID로 보냅니다. 모두 UNHANDLED이고 네 Handler의 방문이 기록됩니다.
2. `PriorityHandler`는 우선순위만 확인하면 안 됩니다. OTHER를 제외해야 합니다.
3. 마지막에서 거절하면 `std::nullopt`를 반환합니다. 기본 담당자 PROF를 임의로 선택하지 않습니다. STATUS와 수신함도 증가하지 않아야 합니다.

## C8 빈 연쇄와 복원

1. `MODE EMPTY` 후 CODE 1을 보냅니다. UNHANDLED이고 TRACE는 `-`입니다.
2. 빈 머리를 역참조하지 않습니다. Handler가 없는 상태는 정상 모드 중 하나입니다.
3. `MODE PRIORITY` 뒤 같은 ID를 다시 보내면 TUTOR에게 성공할 수 있습니다. 모드 전환이 한 번만 되도록 구현하거나 EMPTY가 영구 상태가 되지 않게 합니다.

## M1 학생 단말의 발신자 ID

1. A와 B에서 서로 다른 ID의 CODE 1 문의를 하나씩 보냅니다. TUTOR 수신함의 ITEM에 A, B가 각각 보존되고 STATUS는 A 1, B 1이어야 합니다.
2. `StudentTerminal::send`는 자신의 `name_`을 `Request.sender`에 넣고 전달받은 숫자 요청 ID를 `Request.id`에 보존합니다. 항상 A를 쓰거나 마지막으로 읽은 학생 문자열을 공유하지 않습니다.
3. 단말은 요청을 만들고 Mediator에 위임합니다. 직접 수신함을 수정하면 중복·오프라인·통계 경로가 서로 달라질 수 있습니다.

## M2 수신함 분리와 순서

1. ID 90의 CODE, ID 12의 ENV, ID 40의 CODE를 순서대로 보냅니다. TUTOR에는 90, 40 순서로 두 건, TECH에는 12 한 건이 있어야 합니다.
2. 수신함에 `Request` 전체를 값으로 보관하여 ID·학생·종류·우선순위가 함께 유지되게 합니다. ID순 정렬이나 전체 담당자에게 복제는 하지 않습니다.
3. INBOX를 두 번 조회하고 STATUS를 확인합니다. 조회가 기록을 제거하거나 횟수를 늘리지 않아야 합니다. 선택하지 않은 담당자의 수신함도 확인합니다.

## M3 오프라인과 우회 금지

1. 기본 모드에서 PROF만 오프라인으로 만든 뒤 CODE 3을 보냅니다. 결과는 OFFLINE PROF이고 trace는 PRIORITY 하나입니다. TUTOR가 온라인이어도 전달하지 않습니다.
2. 온라인 검사를 Handler의 수락 조건에 넣으면 연쇄가 다음 Handler로 넘어갑니다. Handler는 조건만 판단하고 Mediator가 선택 이후 온라인을 검사합니다.
3. 해당 담당자를 다시 온라인으로 바꾸어 같은 ID로 성공하는지 확인합니다. ONLINE 명령 자체는 직전 trace를 비우지 않습니다.

## M4 실패한 ID 재시도와 통계

1. TUTOR를 오프라인으로 하고 CODE 1을 보낸 뒤 STATUS와 INBOX를 확인합니다. 실패 전과 값이 같아야 합니다.
2. TUTOR를 온라인으로 바꾸어 같은 ID를 재전송합니다. 이번에는 한 번 성공하고 통계·수신함도 한 번만 증가합니다.
3. OTHER로 실패한 ID를 CODE로 다시 보내는 경우도 확인합니다. ID 기록을 SEND 시작 시점에 넣으면 재시도를 잘못 중복 처리합니다. UNHANDLED와 OFFLINE 모두 성공 상태를 갱신하지 않습니다.

## M5 전역 중복과 빈 trace

1. A에서 CODE 문의 하나를 성공시킨 뒤 같은 ID를 B의 ENV 문의로 보냅니다. 두 번째 결과는 DUPLICATE이며 다른 담당자도 받지 않습니다.
2. 중복 집합을 학생별 또는 담당자별로 나누지 않습니다. 한 번 성공한 ID는 전체 프로그램에서 사용한 ID입니다.
3. 중복 SEND도 trace를 먼저 비웁니다. 성공 직후 TRACE가 있었더라도 중복 SEND 뒤에는 `TRACE -`입니다. 중복 확인이 연쇄 탐색보다 먼저인지 확인합니다.

## M6 모드 변경과 기존 상태

1. 문의를 하나 성공시키고 한 담당자를 오프라인으로 설정한 뒤 DOMAIN, EMPTY, PRIORITY로 모드를 바꿉니다. 기존 수신함과 STATUS는 유지되어야 합니다.
2. 성공했던 ID를 다시 보내면 여전히 DUPLICATE입니다. 모드 변경에서 성공 ID 집합을 초기화하지 않습니다.
3. 오프라인으로 설정했던 담당자에게 새 ID를 보내면 여전히 OFFLINE입니다. `MODE`는 연쇄와 trace만 바꾸며 `LabMediator`나 담당자 전체를 새로 생성하는 명령이 아닙니다.
4. MODE 직후 TRACE는 비어 있어야 합니다. 이후 ONLINE, INBOX, STATUS, TRACE는 그 기록을 임의로 바꾸지 않습니다.

## 실패 결과를 읽는 순서

1. 컴파일 실패는 먼저 해결합니다. 기능 **미검사**는 실행 결과의 실패와 다릅니다.
2. 대부분 UNHANDLED이면 TODO의 공통 연결을 확인합니다. 단말 위임, 머리 연결, 수락 결과 반환 중 하나가 빠져도 여러 항목이 함께 실패합니다.
3. C 계열 위주로 실패하면 수락 조건·방문 순서·모드 연결을, M 계열 위주로 실패하면 전송 이후 상태와 실패 분기의 변경 여부를 확인합니다.
4. 한 명령의 응답뿐 아니라 그 뒤 TRACE·INBOX·STATUS도 비교합니다. 성공 응답이 맞아도 상태가 틀릴 수 있습니다.
5. 기능을 통과한 뒤 단말의 Mediator 위임, Handler의 다음 객체 위임, 선택과 전달의 책임 분리, 소유권과 참조 수명을 소스에서 확인합니다.
