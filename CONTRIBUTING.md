# 협업 규칙

## 최초 설정

Git과 Python 3가 필요합니다. 클론한 저장소마다 한 번 실행하세요.

```sh
sh scripts/setup-hooks.sh
```

훅은 클론만으로 자동 활성화되지 않습니다. 설치 프로그램은 기존 활성 훅을 덮어쓰지 않습니다.

## 작업 브랜치

`main`에서 최신 내용을 받은 뒤 `feat/<역할>-<기능>` 브랜치를 만듭니다.
역할과 기능은 영문 소문자·숫자로 작성하고, 기능의 단어는 하이픈으로 구분합니다.
기본 설정이나 문서 수정도 같은 형식을 사용합니다.

```sh
git switch main
git pull --ff-only origin main
git switch -c feat/devops-repo-guards
```

예: `feat/backend-user-api`, `feat/frontend-login-page`, `feat/devops-ci-setup`.
로컬 훅은 브랜치 형식, main 직접 커밋·푸시, 커밋에 포함된 공백 오류·충돌 마커를 검사합니다.
브랜치 삭제는 main을 제외하고 허용합니다.

## PR 작성과 병합

`.github/PULL_REQUEST_TEMPLATE.md`의 네 제목과 순서를 유지하세요.

- Related Issues: 실제 이슈 번호(`#123`)나 이슈 URL을 작성합니다. 관련 이슈가 없으면 `없음`을 입력합니다.
- Overview: 변경 목적과 개요를 작성합니다.
- Key Changes: 실제 주요 변경 사항을 작성합니다.
- Notes: 제목은 유지하고, 내용은 선택적으로 작성합니다. 테스트 결과나 미완료 사항을 공유할 수 있습니다.
- HTML 주석은 작성 내용으로 인정하지 않습니다. 예시 문구를 그대로 남기면 검사에 실패합니다.

CI의 `repo-policy` 검사는 PR 생성·본문 수정·커밋 추가·재오픈·리뷰 준비 전환 시 실행됩니다.
브랜치 형식이나 본문이 맞지 않으면 실패합니다. 초안 PR도 검사 대상입니다.
자동 검사는 형식과 최소 입력 여부를 확인하며, 내용의 적절성은 리뷰어가 판단합니다.

로컬에서 PR 본문 파일을 미리 검사할 수도 있습니다.

```sh
python3 scripts/repo_policy.py --branch feat/devops-repo-guards --body-file /tmp/pr-body.md
python3 -m unittest discover -s tests -p 'test_repo_policy.py' -v
```

GitHub main Ruleset에는 `repo-policy`를 필수 상태 검사로 등록해야 병합이 차단됩니다.
PR과 다른 리뷰어 1명의 승인, 리뷰 대화 해결이 필요하며, 변경 커밋 추가 시 재승인을 받습니다.
main 삭제·강제 푸시는 금지합니다. 로컬 훅은 우회 가능하므로 최종 강제는 GitHub 규칙이 담당합니다.
검사 스크립트·워크플로 변경도 PR 리뷰에서 확인하세요.
