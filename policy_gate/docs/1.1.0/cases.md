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

## CASE-01: 잘못된 근거를 고치고 다시 확인하기 {#evidence-walkthrough}

상황: v1 snapshot은 그대로인데 web workload가 없는 파일을 근거로 제출했습니다. 정상 입력 전체는 [I-000 입력 예시](rules/I-000.md#example)에 있습니다. 아래는 바꾼 필드만 발췌한 것입니다.

```json
{"workloads[0].evidence": ["src/missing.js:1"]}
```

1. 당시 실행의 source_revision과 snapshot이 같은 자료인지 먼저 확인합니다. 기록에 없는 파일을 임의로 만들어 넣지 않습니다.
2. Intent 검사 단위에서는 I-000이 통과하고 I-001이 `evidence_file_missing`으로 BLOCK입니다. I-006부터 뒤의 규칙은 NOT_RUN입니다. I-001의 references에는 제출한 인용 목록이 남지만 기대한 코드 내용 전체는 없습니다.
3. 실제 전체 준비에서는 G-002가 먼저 `non_source_evidence`로 차단합니다. 허용된 소스 파일의 줄이 범위 밖이거나 비어 있으면 `invalid_source_evidence`입니다. 그러면 Intent 검사 실행 자체가 없을 수 있습니다. 단위 검사 결과를 실제 첫 실패로 바꿔 읽지 않습니다.
4. 원본 `src/server.js`의 22·87줄이 HTTP 동작의 근거인지 확인하고 분석 생성 입력·출력을 고칩니다. 복원한 인용은 다음과 같습니다.

```json
{"workloads[0].evidence": ["src/server.js:22", "src/server.js:87"]}
```

5. 분석 수정이 필요하므로 [일반 배포 진입점](troubleshooting.md#actions)으로 새 분석 경로를 실행합니다. 저장된 context나 과거 판정 행은 수정하지 않습니다. replay라면 잘못된 저장 응답을 반복 재생하지 않는지도 확인합니다.
6. 같은 v1 자료를 사용한 회귀에서는 G-002와 Intent 전체가 PASS이고 모든 필수 규칙이 완료됩니다. 이는 분석 검사 해결이며 빌드·접속 성공까지 뜻하지 않습니다.

존재하는 줄이더라도 의미상 잘못된 인용일 수 있습니다. 줄 존재와 요구의 관련성 검사를 따로 확인해야 합니다.

## CASE-02: 원본 DB와 배포 DB를 혼동한 분석 {#db-walkthrough}

상황: 원본 스키마의 datasource는 sqlite인데 분석이 배포 목표와 혼동해 postgresql을 주장했습니다. 동일 v1 snapshot에서 DB 항목만 변경합니다.

```json
{"state[0].engine": "postgresql", "state[0].evidence": ["prisma/schema.prisma:6"]}
```

1. `prisma/schema.prisma`의 원본 datasource를 확인합니다. Plan의 목표 DB와 원본 DB를 비교하는 검사가 아님을 먼저 구분합니다.
2. Intent 단위 검사에서 I-000·I-001·I-006은 통과하고 I-003이 `db_provider_mismatch`로 BLOCK입니다. I-004·I-002는 NOT_RUN입니다. 실제 남는 근거의 발췌는 다음과 같습니다.

```json
{"path": "prisma/schema.prisma", "observed": "sqlite", "claimed": ["postgresql"]}
```

3. 실제 전체 준비는 G-002가 먼저 `intent_source_mismatch`로 멈출 수 있습니다. 어느 실패든 공통 준비가 완료되지 않아 대상 worker가 시작하지 않습니다.
4. 소스가 맞다면 분석을 sqlite로 복원합니다. 테스트에서는 아래 필드 변경 후 G-002·Intent·Local Plan 검사를 수행하고 승인된 bundle을 생성해 패치·빌드 입력 검사까지 실행합니다.

```json
{"state[0].engine": "sqlite"}
```

5. 새 Intent의 DB는 sqlite, Local Plan의 DB는 postgres 서비스, 변환된 Prisma provider는 postgresql이어야 합니다. 패치의 P-003과 빌드 입력 X-001까지 PASS여도 실제 DB 데이터 이전이나 서비스 실행을 검증한 것은 아닙니다.
6. 실제 운영 조치는 [새 분석을 만드는 일반 배포](troubleshooting.md#actions)입니다. 정책 재검사는 저장된 잘못된 Intent를 고치지 않습니다. 원본 앱 자체를 바꿔야 하는 상황이라면 새 commit과 지원 소스 범위부터 확인합니다.

회귀의 성공 기준은 오류 코드가 사라지는 것뿐 아니라, 원본과 분석의 일치 및 승인된 DB 변환이 모두 유지되는 것입니다.

## CASE-03: 허용 파일 안의 무관한 코드 변경 {#patch-walkthrough}

상황: v1의 승인된 저장소 변환 bundle에서 `src/storage.js`에 다음 코드를 덧붙였습니다. 재현용 bundle의 manifest 해시와 diff도 변경 내용에 맞춰 구성하여 P-001/P-002만으로 차단되는 사례와 구분합니다. 이 조작은 검사 검증용이며 운영 해결 절차가 아닙니다.

```js
module.exports.admin = true;
```

1. 검사 기록의 원본 commit·대상과 코드 diff를 함께 확인합니다. 파일 경로가 허용 목록에 있다는 이유만으로 기능 변경이 허용되는 것은 아닙니다.
2. P-001·P-006·P-007·P-002·P-003 이후 P-004가 `patch_behavior_changed`로 BLOCK입니다. P-005는 NOT_RUN입니다. 근거는 changed_paths와 실패 path이며 AST의 어느 노드가 다른지까지 기록하지는 않습니다.
3. 이 패치를 승인된 결과물로 반환할 수 없으므로 다음 빌드·실행으로 넘기지 않습니다. 단위 재현은 패치 판정을 확인하고, 기존 GateWiring 회귀는 Builder·Adapter가 호출되지 않는 중단 경계를 확인합니다.
4. 생성기 출력에서 승인된 저장 변환 외 코드를 제거하고 같은 snapshot·Plan으로 새 bundle을 생성합니다. 과거 실패 bundle과 판정은 덮어쓰지 않습니다. manifest만 맞추는 조치는 이 실패를 해결하지 못합니다.
5. 재생성한 bundle을 P-001부터 다시 검사합니다. 회귀에서는 P-004와 패치 전체가 PASS이며 X-001도 통과합니다. 실제 운영에서는 새 정상 배포의 빌드 직전 검사·이미지·실행 결과도 확인합니다.

무관한 기능이 업무상 필요해도 배포 변환에 몰래 섞지 않습니다. 원본 앱 변경과 현재 지원 프로필 적합성을 별도로 검토합니다.

## 구현·검증 근거 {#references}

세 사례는 `tests.test_policy_documentation_baseline.WalkthroughTests`의 `test_missing_evidence_then_repair`, `test_db_analysis_then_approved_transform`, `test_unrelated_patch_then_regenerate`로 연결합니다. 보존된 정상 v1 fixture와 임시 bundle을 사용하며 실제 모델 호출·Docker 실행·원격 배포를 수행하지 않습니다.

Local 선행 실패의 원격 명령 미호출, 온프레미스 자료 부족 시 UNAVAILABLE은 [검증 기록](verification.md#results)에서 별도 확인합니다. 사례의 성공 결과를 전체 배포 안전성이나 탐지율로 확대하지 않습니다.

## 직접 근거 보완의 비교 사례 {#evidence-hardening}

이 사례들은 검토된 V2 원본과 source_revision을 유지하고 Intent의 인용만 바꿉니다. 숫자는 고정된 회귀 snapshot 기준이며 다른 소스에는 그대로 적용하지 않습니다.

| 사례 | 최소 입력 차이 | 이전 조건 | 강화된 1.1.0 | 보완과 확인 |
|---|---|---|---|---|
| DB URL 인용 | state[0].evidence가 prisma/schema.prisma:7 | datasource 범위에 겹쳐 PASS | I-003 db_provider_evidence_missing | 같은 파일의 provider 토큰 줄 6을 인용하고 G-002·Intent 전체 재검사 |
| DB 중괄호 인용 | 같은 evidence가 schema 8줄 | datasource 범위에 겹쳐 PASS | 같은 근거 부족 BLOCK | DB 종류는 바꾸지 않고 직접 인용 보완 |
| worker 명령만 인용 | workloads[1].evidence가 package.json:12 | 명령 인용으로 PASS | I-002 worker_start_evidence_missing | src/worker.js:21 또는 :22의 시작점도 인용 |
| worker 초기화만 인용 | 같은 evidence가 src/worker.js:1 | 비어 있지 않은 진입 파일 줄로 PASS | worker_command_evidence_missing; 두 역할 부족 | 명령 값과 시작점 둘 다 추가 |
| 정상 직접 근거 | provider + 명령 값 + 시작점 | PASS | PASS | 정책 조건 충족이며 서비스 실행은 별도 |

DB 근거와 worker 근거가 동시에 부족하면 I-003이 먼저 차단하고 I-002는 NOT_RUN입니다. 원래 실패와 AI 보완 결과를 분리하고, DB 보완 후에 새로 실행된 worker 검사에서 다음 실패를 확인합니다. 첫 기록의 미실행을 사후에 차단으로 고쳐 적지 않습니다.

새 배포의 근거 보완은 해당 evidence 경로만 수정합니다. AI가 provider 인용을 보완하면서 engine이나 reason도 바꾸면 evidence_repair_scope_violation으로 중단합니다. 정상 근거 보완 뒤에도 전체 Intent를 다시 통과해야 합니다. 같은 부족 응답이 반복되면 공유 3회 제한 안에서 종료하고 후속 실행을 시작하지 않습니다.

과거 배포 예시는 고정된 1.0.0 검사기를 별도 프로세스로 실행한 결과와 현재 검사 결과를 사용합니다. 실제 정책 결과와 모의 서비스 문맥을 구분하며, 생성하지 않은 과거 판정을 새 결과에 이름만 바꿔 표시하지 않습니다. 보존 소스가 없으면 재검사 불가입니다. 예시 화면의 AI 수정은 기록에 따라 응답 재생 또는 실제 모델 호출로 구분합니다.

## 직접 근거 사례의 구현·검증 근거 {#evidence-references}

- `tests.test_policy_evidence_hardening.EvidenceRuleTests.test_datasource_url_and_closing_brace_are_not_provider_evidence`
- `tests.test_policy_evidence_hardening.EvidenceRuleTests.test_worker_needs_registered_command_and_start_anchor`
- `tests.test_policy_evidence_hardening.EvidenceRuleTests.test_approved_evidence_continues_through_patch_and_build_input`

검증 예시의 실제 생성 결과와 이전 commit·입력 식별값은 [전환 예시 안내](upgrade.md#examples)와 생성된 검증 기록에서 확인합니다. 회귀에서 사용한 모의 AI 응답을 실제 모델 성공으로 해석하지 않습니다.
