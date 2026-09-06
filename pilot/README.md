# Local pilot inputs

- `course.csv`: loopback `127.0.0.1:18080`, `pilot-local`, worker 4개를 사용하는 0-env 설정
- `course.https.example.csv`: 외부 HTTPS reverse proxy에서 QR 수령 흐름을 시험하기 위한
  secret 없는 예제. `.local` 파일로 복사하고 실제 domain으로 바꾸어 사용
- `roster.csv`: 합성 학생 `s001`–`s020` 20명의 active roster

`course.csv`의 상대 `data_root=.data`는 이 파일이 있는 디렉터리 기준으로 해석되므로 상태와
credential은 `pilot/.data/`에 생성됩니다. State root는 config 디렉터리의 전용 하위 경로만
허용됩니다. 이 두 CSV에는 secret을 넣지 않습니다.

정확한 초기화·과제 등록·활성화·Extension 시험 명령은
[Local CSV 파일럿 실행 가이드](../docs/operations/direct-bundle-mvp.md)를 따릅니다.
QR에서 학번과 Autograde 전용 비밀번호를 확인한 뒤 VS Code가 과제를 바로 받는 권장 외부
흐름은 [QR 과제 수령 파일럿 가이드](../docs/operations/qr-assignment-claim-pilot.md)를 따릅니다.
공용 좌석에서는 서비스 URL만 지속하며 학생 token은 메모리 전용입니다. 외부 HTTPS QR
흐름에서는 Activity Bar의 Autograde 아이콘을 열고 **수령 코드 입력 및 다운로드**를 사용합니다.
이 동작이 기기 연결, 과제 수락과 다운로드를 이어서 처리하므로 별도 **지금 로그인**은 필요하지
않습니다. **지금 로그인**과 교수자 발급 1회용 활성화 코드는 loopback·장애 대응용 호환
경로입니다. 어느 흐름이든 자리 이탈 전 Sign Out과 workspace/browser profile 정리를 수행하고,
Command Palette는 사이드바 동작을 사용할 수 없을 때만 대체 경로로 사용합니다.

같은 신뢰 LAN의 다른 컴퓨터에서 단기 기능 시험을 할 때만
[`course.lan.example.csv`](course.lan.example.csv)를 local `.local` 파일로 복사하고
[신뢰 LAN 외부 접속 파일럿](../docs/operations/trusted-lan-pilot.md)을 따릅니다. 예제의 두 IP를
교수자 컴퓨터의 실제 사설 IPv4로 바꾸어야 하며 `0.0.0.0`은 접속 주소로 사용하지 않습니다.
이 모드는 HTTP credential과 제출물이 LAN에 노출될 수 있어 합성·사전 검토 코드 전용입니다.

`pilot-local`은 sandbox가 아니므로 합성·신뢰 코드에만 사용하며 실제 학생 채점에는
사용하지 않습니다.
