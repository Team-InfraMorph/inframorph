# 설명과 구현을 검증하는 방법

## 확인 기준 {#baseline}

정책 1.0.0의 규칙 18개를 구현·테스트·문서에 대조합니다. 입력·판정 조건·한계를 함께 확인하고, 관리용 구조 변경과 정책 규칙 변경을 구분합니다. 기준선의 식별값과 문서 파일 해시는 policy_gate/baselines/1.0.0.json에서 검사합니다.

## 재현 방법 {#run}

```sh
.venv/bin/python -m unittest tests.test_policy_documentation tests.test_policy_documentation_baseline tests.test_policy_documentation_contracts -v
.venv/bin/python scripts/check_policy_catalog.py --base origin/main
node --test control_plane/web/tests/*.test.mjs
```

실행 결과와 확인한 환경은 [검증 기록](verification.md#results)에 남깁니다. 테스트 파일의 존재만으로 통과라 하지 않으며, 모의 파서 장애를 실제 서비스 장애 재현으로 확대하지 않습니다.

## 검증의 수준 {#levels}

규칙 단위 재현은 해당 함수의 판정 근거입니다. 전체 배포 중단은 GateWiring 테스트에서 Builder/Adapter 미호출까지 확인합니다. 실제 Local 배포·공개 접속·AWS 자원 상태는 별도 실행 근거가 필요합니다.

## 문서 리뷰 기준 {#review}

규칙의 입력·처리·판정·근거·조치·한계를 실제 코드에 대조합니다. 사유 코드가 같아도 호출 단계가 다르면 귀속을 확인합니다. 근거 부족은 명시하며 자동 CI가 문장의 의미 전체를 보장하지는 않습니다. 과거 판정은 변경하지 않고 현재 문서는 정책 버전으로 조회합니다.

## 고정 corpus를 사용한 전체 회귀 {#corpus}

로컬 고정 checkout은 .local/main-review-corpus, CI 경로는 .local/redteam-corpus입니다. validation/redteam-source.json의 commit과 실제 checkout이 같은지 확인합니다.

```sh
INFRAMORPH_REDTEAM_CORPUS="$PWD/.local/main-review-corpus" .venv/bin/python -m unittest discover -s tests -v
```

기본 형제 redteam-repo가 다른 commit이면 corpus_revision_mismatch로 중단하는 것이 정상입니다. lock을 현재 브랜치에 맞춰 완화하지 않습니다.
