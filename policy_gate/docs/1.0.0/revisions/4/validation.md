# 설명과 구현을 검증하는 방법

## 확인 기준 {#baseline}

문서 revision 4는 로컬 commit 544ae0c의 revision 3 위에서 작성했습니다. 판정 구현은 main fa84467을 통합한 138e6ca와 같고 이번 변경은 설명·연결·검증 예시입니다. review.json에 이전 revision 1·2·3의 해시를 보존합니다.

기본 문서 포인터만 4로 갱신합니다. 판정 코드·프로필·템플릿·규칙 카탈로그와 해시 계산 방식은 바꾸지 않습니다. [고정 검사의 정확한 범위](versioning.md#baseline)를 함께 읽으세요. 기존 기준선 파일이 현재 문서 전체를 고정한다고 해석하지 않습니다.

## 재현 방법 {#run}

```sh
.venv/bin/python -m unittest tests.test_policy_documentation tests.test_policy_documentation_baseline tests.test_policy_documentation_r4 -v
.venv/bin/python scripts/check_policy_catalog.py --base 544ae0c
node --test control_plane/web/tests/*.test.mjs
```

관련 Python 회귀를 실행하고, 이번 실행과 이전 기준선의 AWS·redteam·UI 결과를 [검증 기록](verification.md#results)에서 구분합니다. 테스트 파일의 존재만으로 통과라 하지 않으며 결과는 [검증 기록](verification.md#results)에 기재합니다. 검증용 모의 파서 장애는 실제 서비스 장애를 일으킨 검사가 아닙니다.

## 검증의 수준 {#levels}

규칙 단위 재현은 해당 함수의 판정 근거입니다. 전체 배포 중단은 GateWiring 테스트에서 Builder/Adapter 미호출까지 확인합니다. 실제 Local 배포 및 공개 접속 결과는 별도 실행 근거이며 이번 문서 수정 자체로 재배포하지 않습니다. AWS 실제 자원 상태를 이번 테스트로 관측했다고 주장하지 않습니다.

## 문서 리뷰 기준 {#review}

각 규칙의 입력·처리·판정·근거·조치·한계를 실제 코드에 대조합니다. 사유 코드가 같아도 호출 단계가 다르면 귀속을 확인합니다. 확인 불가·근거 부족은 보완 목록으로 남기며 자동 CI가 문장의 의미를 보장하지는 않습니다.

## 고정 corpus를 사용한 전체 회귀 {#corpus}

이 작업의 로컬 고정 checkout은 .local/main-review-corpus이며 HEAD는 488833528c3c9a389d0af05606c9bb1cdeaa1c99입니다. CI는 .local/redteam-corpus를 사용합니다. 경로 이름이 아니라 validation/redteam-source.json의 고정 revision과 실제 checkout이 같은지 확인합니다. 다른 환경에서는 준비한 고정 checkout 경로를 지정하세요.

```sh
INFRAMORPH_REDTEAM_CORPUS="$PWD/.local/main-review-corpus" .venv/bin/python -m unittest discover -s tests -v
```

기본 형제 redteam-repo가 다른 revision이면 corpus_revision_mismatch로 중단하는 것이 정상입니다. lock을 현재 브랜치에 맞춰 완화하지 않습니다.
