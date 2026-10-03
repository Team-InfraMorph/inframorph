# 설명과 구현을 검증하는 방법

## 확인 기준 {#baseline}

문서 revision 2는 feat/e-policy-lifecycle-ui의 현재 작업본을 기준으로 작성합니다. 기준 main은 05bd5b4이며 아직 commit하지 않은 정책 lifecycle 구현을 포함합니다. commit만으로 현재 구현을 재현할 수 있다고 주장하지 않습니다. review.json에 정책·규칙·구현 식별값과 revision 1 파일 해시, 규칙별 정확한 테스트 연결을 보존합니다.

판정 코드·프로필·템플릿·규칙 카탈로그는 변경하지 않습니다. 문서 revision 포인터만 갱신합니다. 문서 테스트는 기준 해시와 이전 문서 불변, 모든 내부 링크와 절·테스트 연결을 검사합니다.

## 재현 방법 {#run}

```sh
.venv/bin/python -m unittest tests.test_policy_documentation -v
.venv/bin/python scripts/check_policy_catalog.py
node --test control_plane/web/tests/*.test.mjs
```

기존 Python·AWS Adapter 회귀와 고정 redteam 입력도 함께 실행합니다. 테스트 파일의 존재만으로 통과라 하지 않으며 결과는 [검증 기록](verification.md#results)에 기재합니다. 검증용 모의 파서 장애는 실제 서비스 장애를 일으킨 검사가 아닙니다.

## 검증의 수준 {#levels}

규칙 단위 재현은 해당 함수의 판정 근거입니다. 전체 배포 중단은 GateWiring 테스트에서 Builder/Adapter 미호출까지 확인합니다. 실제 Local 배포 및 공개 접속 결과는 별도 실행 근거이며 이번 문서 수정 자체로 재배포하지 않습니다. AWS 실제 자원 상태를 이번 테스트로 관측했다고 주장하지 않습니다.

## 문서 리뷰 기준 {#review}

각 규칙의 입력·처리·판정·근거·조치·한계를 실제 코드에 대조합니다. 사유 코드가 같아도 호출 단계가 다르면 귀속을 확인합니다. 확인 불가·근거 부족은 보완 목록으로 남기며 자동 CI가 문장의 의미를 보장하지는 않습니다.
