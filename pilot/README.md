# Local pilot inputs

- `course.csv`: loopback `127.0.0.1:18080`, `pilot-local`, worker 4개를 사용하는 0-env 설정
- `roster.csv`: 합성 학생 `s001`–`s020` 20명의 active roster

`course.csv`의 상대 `data_root=.data`는 이 파일이 있는 디렉터리 기준으로 해석되므로 상태와
credential은 `pilot/.data/`에 생성됩니다. State root는 config 디렉터리의 전용 하위 경로만
허용됩니다. 이 두 CSV에는 secret을 넣지 않습니다.

정확한 초기화·과제 등록·활성화·Extension 시험 명령은
[Local CSV 파일럿 실행 가이드](../docs/operations/direct-bundle-mvp.md)를 따릅니다.
공용 좌석에서는 서비스 URL만 지속하며 학생 token은 메모리 전용입니다. 학생은 VS Code의
Activity Bar에서 Autograde 아이콘을 열고 Assignments 영역의 **지금 로그인**과 과제를 펼쳤을
때 보이는 **과제 파일 다운로드**를 직접 사용합니다. 제목 표시줄과 과제 행의 아이콘도 같은
동작을 제공하며 Command Palette 명령은 사이드바 동작을 사용할 수 없을 때의 대체 경로입니다.
학생이 Sign In할 때마다 새 1회용 활성화 코드를 발급하고, 자리 이탈 전 Sign Out과
workspace/browser profile 정리를 수행합니다.

같은 신뢰 LAN의 다른 컴퓨터에서 단기 기능 시험을 할 때만
[`course.lan.example.csv`](course.lan.example.csv)를 local `.local` 파일로 복사하고
[신뢰 LAN 외부 접속 파일럿](../docs/operations/trusted-lan-pilot.md)을 따릅니다. 예제의 두 IP를
교수자 컴퓨터의 실제 사설 IPv4로 바꾸어야 하며 `0.0.0.0`은 접속 주소로 사용하지 않습니다.
이 모드는 HTTP credential과 제출물이 LAN에 노출될 수 있어 합성·사전 검토 코드 전용입니다.

`pilot-local`은 sandbox가 아니므로 합성·신뢰 코드에만 사용하며 실제 학생 채점에는
사용하지 않습니다.
