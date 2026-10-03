# Policy Gate

- 담당: E · 리뷰: D
- 기획서 구간: 5 · 8 · 이슈: #6 #9

`intent.json`과 `patch.diff`를 실행 전에 검사한다. 근거가 실제 코드에 있는지, 노출 범위, 패치 범위와 금지 패턴을 본다.


## 정식 정책 관리 1.0.0 (개발 중)

[기준선 점검](BASELINE_1_0_0.md), [버전별 문서](docs/1.0.0/revisions/1/overview.md), [구현·검증·점수표](../POLICY_LIFECYCLE_IMPLEMENTATION.md)를 참고한다. 기존 2.2.0 결과는 legacy로 보존한다. 정책 화면의 문서 버전 선택은 활성 실행 정책을 바꾸지 않는다.

규칙 추가·수정은 releases 카탈로그, docs 문서 revision, 실행 함수, 회귀 테스트, 변경 영향 명세를 함께 검토한다. `scripts/check_policy_catalog.py`가 CI에서 연결과 출시 불변성을 검사한다.
