# 설명과 구현을 검증하는 방법

## 확인 기준 {#baseline}

문서 revision 3은 main fa84467을 통합한 구현 commit 138e6ca 위에서 작성했습니다. 판정 실행 코드는 이 commit과 같고, 문서·검증·기준선 파일은 이번 로컬 변경입니다. revision 2의 05bd5b4 및 미커밋 표기는 당시 작성 기준으로 보존합니다. 현재 기준은 review.json과 policy_gate/baselines/1.0.0.json에 기록합니다.

이번 설명 보완에서 판정 코드·프로필·템플릿·규칙 카탈로그는 변경하지 않습니다. 기본 문서 포인터만 3으로 갱신합니다. 문서 검사는 과거 revision 보존, 내부 링크·절·테스트 연결과 실제 입력 예시를 확인합니다. 기준선 검사는 정책 식별값과 문서 바이트가 보존되는지 확인합니다.

## 재현 방법 {#run}

```sh
.venv/bin/python -m unittest tests.test_policy_documentation tests.test_policy_documentation_baseline -v
.venv/bin/python scripts/check_policy_catalog.py
node --test control_plane/web/tests/*.test.mjs
```

기존 Python·AWS Adapter 회귀와 고정 redteam 입력도 함께 실행합니다. 테스트 파일의 존재만으로 통과라 하지 않으며 결과는 [검증 기록](verification.md#results)에 기재합니다. 검증용 모의 파서 장애는 실제 서비스 장애를 일으킨 검사가 아닙니다.

## 검증의 수준 {#levels}

규칙 단위 재현은 해당 함수의 판정 근거입니다. 전체 배포 중단은 GateWiring 테스트에서 Builder/Adapter 미호출까지 확인합니다. 실제 Local 배포 및 공개 접속 결과는 별도 실행 근거이며 이번 문서 수정 자체로 재배포하지 않습니다. AWS 실제 자원 상태를 이번 테스트로 관측했다고 주장하지 않습니다.

## 문서 리뷰 기준 {#review}

각 규칙의 입력·처리·판정·근거·조치·한계를 실제 코드에 대조합니다. 사유 코드가 같아도 호출 단계가 다르면 귀속을 확인합니다. 확인 불가·근거 부족은 보완 목록으로 남기며 자동 CI가 문장의 의미를 보장하지는 않습니다.
