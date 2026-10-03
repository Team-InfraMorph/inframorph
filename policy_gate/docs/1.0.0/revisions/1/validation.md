# 검증과 알려진 한계

## 규칙 점검

각 규칙은 실제 검사 함수, 문서, 정상/위반 회귀 테스트에 연결됩니다. 필수 규칙 미실행은 통과할 수 없습니다. 파서·입력·정책 자료가 없으면 확인 불가로 남깁니다.

## 검증 명령

`.venv/bin/python scripts/check_policy_catalog.py`

`.venv/bin/python -m unittest discover -s tests`

`node --test control_plane/web/tests/*.test.mjs`

## 한계

지원 프로필은 제한되어 있습니다. Prisma 검사는 전체 언어 AST가 아닌 구조 토큰 비교이며 JS AST 검사는 임의 코드의 사업 의미 증명이 아닙니다. 이전 결과에는 없는 세부 규칙을 복원하지 않습니다. 재검사는 저장된 배포 입력을 검사하며 클라우드의 현재 drift나 새로운 네트워크 상태를 관측하지 않습니다.
