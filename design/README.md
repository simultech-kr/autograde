# Design

- `adr/0001-pyjevsim-scheduler.md`: PyJevSim을 workflow clock으로 사용하는 결정
- `adr/0002-fetch-exact-sha.md`: pull 대신 bare fetch와 exact-SHA snapshot을 사용하는 결정
- `adr/0003-wsl-extension-device-authorization.md`: WSL workspace Extension, OAuth-free
  browser/device authorization과 exact-SHA 제출 경계
- `adr/0004-direct-bundle-delivery.md`: GitHub 없는 course-wide bundle 배포·제출과 local CSV,
  신뢰 코드 전용 `pilot-local`을 파일럿으로 사용하고 container를 production 계획에 두는 결정
- `adr/0005-shared-seat-ephemeral-auth.md`: 공용·순환 좌석에서 URL만 지속하고 token은 메모리
  전용으로 두며 로그인별 새 코드, 4시간 절대 session과 최신 로그인 교체를 적용하는 결정
- `diagrams/`: 후속 상세 diagram
- `prototypes/`: 폐기 가능한 설계 실험
