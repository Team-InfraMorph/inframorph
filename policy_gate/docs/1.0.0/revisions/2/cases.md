# 실패와 해결의 검증 사례

## 자동 재현 사례 {#reproduced}

| 사례 | 최소 변경 | 확인할 결과 | 후속 영향·수정 |
|---|---|---|---|
| CASE-01 근거 파일·줄 | 유효 Intent의 인용을 없는 파일/범위 밖으로 변경 | I-001 BLOCK, 뒤 규칙 NOT_RUN | 실제 소스 인용으로 재분석 |
| CASE-02 DB 불일치·누락 | sqlite 소스에서 engine=postgresql 또는 DB 요구 삭제 | I-003 BLOCK; I-004/I-002 NOT_RUN | 실제 datasource와 일치하는 분석 재생성 |
| CASE-03 무관한 패치 | 허용 파일에 admin:true 추가, manifest/diff는 일치 | P-004 BLOCK | 승인 변환만 남겨 전체 패치 재검사 |
| CASE-04 파서 장애 | Node 파서 호출에 OSError 모의 | P-006 ERROR, 후속 NOT_RUN | 환경 복구 후 동일 입력 검사 |
| CASE-05 변조 | 승인 해시 이후 bundle 내용 변경 | P-001 BLOCK; build 실행 호출 없음 | 동일 소스로 bundle 재생성 |
| CASE-06 저장 누락 경계 | Intent에서 persistent_files 삭제 | I-004 제외, 실제 경로 G-002 BLOCK | 누락을 숨기지 않고 분석 복원 |
| CASE-07 제외와 부수 호출 | 변경 없는 JS 검사에서 템플릿 파서 장애 모의 | P-004 ERROR 가능 | 공통 전제와 제외 의미 보완 대상으로 관리 |
| CASE-08 읽기 불가 | 존재하지 않는 snapshot 경로 | I-000 BLOCK unreadable_source | 환경·자료를 먼저 확인; 앱 악성 확정 안 함 |

CASE-01/02/04/06/07/08은 tests.test_policy_documentation.DocumentedScenarios에서 고정 분기와 ledger를 확인합니다. CASE-03은 RuleTests.test_arbitrary_behavior_inside_allowed_file_is_blocked 및 GateWiringTests.test_consistent_but_forbidden_patch_stops_before_builder_and_adapter에 연결합니다. CASE-05는 GateTests.test_tampered_content·test_rejected_gate_never_invokes_docker에 연결합니다. 규칙별 문서 하단에는 전체 Python 테스트 경로를 제공합니다.

## 입력과 판정 예시 {#example}

DB 사례의 최소 변경은 다음과 같습니다. source_revision과 근거 파일은 유지합니다.

```json
{"원본 datasource": "sqlite", "분석 state.engine": "postgresql"}
```

validate_intent 단위 호출에서는 I-000/I-001/I-006 뒤 I-003이 db_provider_mismatch로 차단합니다. 실제 runtime 준비에서는 G-002가 더 먼저 intent_source_mismatch로 차단할 수 있습니다. 단위 테스트의 사유를 실제 첫 실패 코드라고 단정하지 않습니다.

## redteam과의 관계 {#redteam}

현재 고정 corpus revision은 488833528c3c9a389d0af05606c9bb1cdeaa1c99입니다. 39개 입력의 호스트 통합 검증은 inframorph의 verify_e_redteam.py에서 실행합니다. corpus 자체 검증과 실제 호스트 실행 결과를 구분합니다. 특수 corpus 변환은 테스트 프로필이며 실제 배포 허용 조건으로 확장하지 않습니다.

## 설명용 사례 {#illustrative}

각 규칙 표의 경계·미지원 설명 중 회귀에 직접 연결되지 않은 항목은 코드 대조에 근거한 설명이며 실행 재현 완료로 표시하지 않습니다. 모든 조합을 테스트했다고 주장하지 않습니다. [보완 목록](improvements.md#backlog)에 부족한 검증을 남깁니다.
