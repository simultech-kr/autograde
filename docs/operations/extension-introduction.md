# 확장 소개 페이지 관리·게시 안내

## 소개 원본

| 대상 | 학생용 소개 | 개발·운영 참고 |
| --- | --- | --- |
| VS Code | [README](../../extensions/vscode/README.md) | [DEVELOPMENT](../../extensions/vscode/DEVELOPMENT.md) |
| Visual Studio Community | [README](../../extensions/visualstudio/README.md) | [DEVELOPMENT](../../extensions/visualstudio/DEVELOPMENT.md) |

소개는 현재 0.5.1 구현 기준이다. 기존 빌드·API·보안 경계·시험 내용은 삭제하지 않고
같은 확장 디렉터리의 DEVELOPMENT.md로 분리했다.

소개 페이지에는 다음을 포함한다.

1. 어떤 수업에서 쓰는지, 확장만 설치하면 안 되는 이유.
2. Windows WSL2 / Linux / macOS / Visual Studio의 환경 차이.
3. 확장 설치와 웹/API 주소 구분.
4. 웹 학번·비밀번호 인증 → 과제 수령 코드 → 확장 로그인·다운로드.
5. 자동 폴더 열기와 IDE별 동작 차이.
6. 저장·파일 검토·제출 접수·채점 결과 확인.
7. 결과 공개 대기, 수정 필요, 자동채점 기준 충족의 의미.
8. 제출 기록·새 폴더 복원과 로컬 초안 백업의 차이.
9. 개인정보·공용 PC 로그아웃·자주 묻는 오류와 문의 방법.
10. 현재 지원 범위와 배포 전 남은 검증.

## 설치 화면과 Marketplace 반영

### VS Code

확장 루트 README.md는 확장 소개 원본이다. 패키징 시 포함되며 Marketplace 소개에도 사용한다.
저장소의 패키징 명령에는 README 상대 링크 변환에 필요한 GitHub 기준 URL이 설정되어 있다.
[공식 게시 문서](https://code.visualstudio.com/api/working-with-extensions/publishing-extension).

소개를 수정한 후 확장 디렉터리에서 `npm run package:vsix`를 실행하고 VSIX 안의
`extension/readme.md`에 변경 내용이 있는지 확인한다. 이미 설치한 학생 PC의 소개를 바꾸려면
업데이트된 패키지를 배포해야 하며 서버 재시작만으로는 변경되지 않는다.
공개된 버전을 업데이트할 때는 기존 버전 번호를 재사용하지 말고 새 버전으로 배포한다.

### Visual Studio

README.md를 Visual Studio Marketplace 상세 소개의 원본으로 사용한다.
VSIX manifest의 짧은 Description만 바꿔서는 긴 소개 전체가 자동 게시되지 않는다.
게시 담당자가 Marketplace의 기존 확장 항목에서 소개 내용을 등록·미리보기해야 한다.
[공식 게시 안내](https://learn.microsoft.com/en-us/visualstudio/extensibility/walkthrough-publishing-a-visual-studio-extension?view=vs-2022).

Marketplace 편집기에 내용을 옮길 때 README의 상대 링크는 공개 저장소의 절대 HTTPS 링크로
바꾼다. 예를 들어 DEVELOPMENT.md는 저장소 main 브랜치에 실제 반영한 후
`https://github.com/simultech-kr/autograde/blob/main/extensions/visualstudio/DEVELOPMENT.md`로 연결한다.
아직 push하지 않은 문서 링크나 접근할 수 없는 비공개 저장소 링크를 공개 페이지에 배포하지 않는다.

## 게시 전 확인

- README의 버전·버튼 이름·지원 환경과 실제 패키지가 일치하는지 확인한다.
- 설치 링크는 실제 게시 완료 후 추가한다. 존재하지 않는 Marketplace 항목·게시자 인증을 표시하지 않는다.
- 실제 IDE 캡처를 추가할 경우 샘플 과제만 사용하고 학번·비밀번호·수령 코드·토큰을 제외한다.
  이번 소개에는 실제 화면으로 오해할 수 있는 합성 스크린샷을 넣지 않았다.
- Windows 설치·폴더 전환·저장 확인창, WSL2 다운로드·제출을 현장에서 확인한다.
- Marketplace에 게시할 계정·라이선스·서명·지원 창구와 서버 개인정보 보관 정책은 운영자가 결정한다.
- VS Code 패키지의 현재 `UNLICENSED`와 LICENSE 파일 부재는 공개 배포 전에 검토한다.
  소개 문서 작성이 공개 배포 허가나 라이선스 부여를 대신하지 않는다.

이번 변경은 소개 원본·짧은 설명·문서 연결을 갱신한다. 실제 Marketplace 게시,
학교 서버 설정 변경, 학생 PC의 확장 설치·업데이트는 수행하지 않는다.
