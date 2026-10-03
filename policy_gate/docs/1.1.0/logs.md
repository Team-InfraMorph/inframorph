# 실패와 로그 확인

## 기록 구분

정책 판정, 운영 감사 이벤트, 마스킹된 상세 진단을 구분합니다. 판정·감사 기록은 보존하고 상세 로그는 30일 후 만료합니다. 만료 후에도 실패 코드와 수정·재검증 연결은 남습니다.

## 실패 원인 검토

정상 위반 차단, 확인된 오탐, 미지원, 검사기 오류, 환경 오류, 확인 불가로 검토합니다. 원래 판정은 수정하지 않습니다. 반복 차단만으로 정책을 완화하지 않습니다.

## 개선 절차

실패 → 입력·정책·근거 확인 → 원인 분류 → 최소 재현 → 정상/위반 회귀 테스트 → 앱·검사기·문서 수정 → 새 검사로 해결 확인 순서입니다.

## 민감정보와 제한

구조화된 코드와 허용 필드만 우선 보존합니다. 자유 텍스트는 저장 전에 마스킹하고 8KiB로 제한합니다. 로그는 비신뢰 자료이며 실행 지시나 소스 근거가 아닙니다. 마스킹은 모든 민감정보 탐지를 보장하지 않습니다.

로컬 운영자의 검토는 인증된 개인 신원이 아닙니다. 애플리케이션 수준의 기록 보존을 외부 감사 시스템 수준의 위변조 방지 보장으로 해석하지 않습니다.


## 실패부터 새 검사까지 읽는 예시 {#walkthrough}

검토된 v1 소스의 분석에서 web 근거를 `src/missing.js:1`로 잘못 인용한 상황입니다. 아래는 **격리된 테스트 DB에서 실제 G-002와 저장 함수를 실행한 재현 사례**입니다. 운영 배포의 장애 이력이 아니며 모델·빌드·서비스 실행은 하지 않았습니다. 두 검사 ID는 읽기 쉽게 검사-A와 검사-B로 표시합니다.

| 순서 | 기록과 확인 | 판단·다음 행동 |
|---|---|---|
| 1. 첫 검사 | 배포-A / Local / 최초 시도 0 / source / G-002 BLOCK / non_source_evidence | 이 배포의 소스 검사를 통과하지 못함. 일반 경로에서는 이후 Intent·설계·패치·빌드로 진행하지 않음 |
| 2. 당시 자료 대조 | 정책 1.1.0, 소스 ebad709…; Intent의 web evidence와 동일 snapshot 비교 | 없는 파일을 인용했음을 확인. 판정 행의 evidence에는 소스 revision·파일 수만 있으므로 원본 입력 없이 특정 인용을 추정하지 않음 |
| 3. 수정 | 앱 소스는 유지하고 실제 소스 줄을 인용한 새 Intent 사용 | fixture의 검증된 Intent로 교체한 재현. 실제 운영은 [재분석 절차](troubleshooting.md#actions)에 따라 새 분석을 생성. 과거 입력·판정은 수정하지 않음 |
| 4. 새 검사 | 같은 프로젝트의 배포-B / Local / 최초 시도 0 / 검사-B / G-002 PASS | 소스 SHA와 정책 해시는 같지만 Intent 입력 해시와 실행 ID는 다름. 두 배포 각각의 최초 검사이므로 attempt가 둘 다 0인 것이 정상 |
| 5. 원인 검토 | 검사-A에 violation 검토 추가, resolved_execution_id=검사-B, 회귀 테스트 연결 | 잘못된 인용을 막은 정상 차단으로 검토. 정책 결함·오탐으로 자동 분류하지 않음 |
| 6. 남은 확인 | 검사-A는 BLOCK 그대로, 검사-B는 소스 단계만 PASS | 전체 요약은 NOT_RUN. 나머지 필수 단계·빌드·배포·공개 접속의 성공을 이 사례로 주장하지 않음 |

이 사례는 앱 코드를 고치지 않았으므로 fix_commit은 빈 문자열입니다. 수정 commit이 없는 상황에 소스 SHA나 가짜 commit을 채우지 않습니다. 앱을 실제 수정했다면 별도 새 commit의 일반 배포 결과로 확인해야 합니다.

## 검사 결과의 각 값 읽기 {#record-fields}

| 값 | 이 사례의 의미 | 잘못 해석하지 않을 점 |
|---|---|---|
| execution_id | 검사-A와 검사-B를 구분하는 UUID; 문서에서만 별칭 사용 | 같은 대상·시도여도 새 검사는 새 ID |
| stage / checkpoint | source 단계 / source 시점 | 규칙 ID와 다름. 규칙은 G-002 |
| decision / complete | 검사-A는 BLOCK / true | G-002 단일 단계가 판정을 마쳤다는 뜻. 실행 허용이나 전체 정책 완료가 아님 |
| required_rules / evaluated_rules | 둘 다 G-002 | 실제 실행된 규칙도 차단될 수 있음. 다른 단계의 규칙까지 검사했다는 뜻이 아님 |
| rules[].evidence | source_revision과 file_count=4 | 위반 파일·줄·기대값을 모두 저장하는 구조가 아님. 파일 수는 해당 fixture의 수집 결과 |
| binding.input_sha256 | 잘못된 Intent와 수정 Intent의 식별값이 다름 | 원문을 복원하거나 주장 내용이 참임을 입증하는 값이 아님 |
| policy_digest / documents_digest | 판정 구현과 설명 묶음의 내용 식별값 | 문서 링크는 해당 정책 버전의 설명. 당시 정확한 내용은 저장된 해시와 Git 이력으로 대조 |
| classification / resolved_execution_id | 별도 검토가 원인 분류와 새 PASS를 연결 | 원래 BLOCK을 PASS로 변경하거나 실행을 면제하지 않음 |

원인 검토의 연결 검사는 같은 프로젝트에서 같은 stage의 완료된 PASS이고, 실패했던 규칙이 PASS인지 확인합니다. 이것만으로 같은 target·정책·입력의 운영 전환 승인이 보장되지는 않습니다. 검토자는 원래 기록과 새 기록의 소스·대상·입력·정책을 함께 대조해야 하며, 전체 정책 업데이트 검토·재배포 승인과 이 원인 분류 기록을 혼동하지 않습니다.

## 이벤트와 로그를 대조하는 순서 {#event-order}

```text
검사-A  inspection_started
검사-A  rule_evaluated       G-002 BLOCK / non_source_evidence
검사-A  inspection_finished  BLOCK
검사-B  inspection_started
검사-B  rule_evaluated       G-002 PASS / passed
검사-B  inspection_finished  PASS
검사-A  failure_reviewed     violation → 검사-B 연결
```

위 순서는 저장된 이벤트의 seq를 기준으로 정리한 발췌입니다. 실제 이벤트에는 deployment_id·target·execution_id·occurred_at·observed_at 등이 있으며, 시각은 UTC로 보존합니다. target이 없는 검토 이벤트도 있으므로 실행 ID로 원래 기록을 찾습니다. failure_reviewed 이벤트에는 review_id와 classification이 남고, 해결 실행 ID·설명·테스트는 별도 failure_reviews의 payload에서 읽습니다.

rule_evaluated라는 이벤트 이름만으로 검사가 수행됐다고 판단하지 않습니다. 다른 사례에서는 decision=NOT_RUN인 규칙도 이 이름의 이벤트로 보존됩니다. 실제 decision과 evaluated_rules를 함께 확인하세요.

이 사례에서 자동 저장된 진단 발췌는 `non_source_evidence`라는 사유 코드이고 masked=false, truncated=false입니다. 원본 Intent나 모델 응답 전체를 남긴 로그가 아닙니다. 실패 코드는 판정에도 보존됩니다. 상세 진단의 30일 만료 후 payload가 비어도 검사-A의 BLOCK, 원인 검토, 검사-B 연결은 유지됩니다. 이 만료 처리 역시 테스트에서 확인합니다.

조회 API `GET /api/deployments/{id}/policy`는 판정, `GET /api/deployments/{id}/policy-history`는 이벤트·진단·원인 검토를 제공합니다. 서로 다른 배포의 이력은 각각 조회한 뒤 해결 실행 ID로 연결합니다. 화면의 한 로그 문자열을 별도 판정 이벤트처럼 해석하지 않습니다.

## 구현·검증 근거 {#references}

- `tests.test_policy_documentation_contracts.VersionContractTests.test_record_walkthrough`
- `control_plane/policy_results.py`: 검사 시작·규칙 결과·종료와 판정 보존
- `control_plane/policy_lifecycle.py`: 원인 검토 연결·진단 만료

다음 JSON은 테스트에서 저장·조회한 값 중 읽기에 필요한 필드만 추린 발췌입니다. failed/corrected/review/events는 설명을 위한 묶음이며 단일 API 응답 형식이 아닙니다. ID만 별칭으로 바꾸고 임의 시각은 만들지 않았습니다. 해시·시각·seq 등 생략된 필드는 실제 조회 결과에서 확인합니다. 테스트가 매번 같은 발췌를 다시 생성해 문서와 대조합니다.

```json
{
  "failed": {
    "family": "inframorph-policy",
    "version": "1.1.0",
    "target": "local",
    "attempt": 0,
    "stage": "source",
    "checkpoint": "source",
    "decision": "BLOCK",
    "complete": true,
    "required_rules": [
      "G-002"
    ],
    "evaluated_rules": [
      "G-002"
    ],
    "reason_code": "non_source_evidence",
    "execution_id": "검사-A",
    "rules": [
      {
        "rule_id": "G-002",
        "revision": 2,
        "decision": "BLOCK",
        "reason_code": "non_source_evidence",
        "evidence": {
          "file_count": 4,
          "source_revision": "ebad709867cf1f3523075d8038ae41bd69ecae9b"
        }
      }
    ]
  },
  "corrected": {
    "family": "inframorph-policy",
    "version": "1.1.0",
    "target": "local",
    "attempt": 0,
    "stage": "source",
    "checkpoint": "source",
    "decision": "PASS",
    "complete": true,
    "required_rules": [
      "G-002"
    ],
    "evaluated_rules": [
      "G-002"
    ],
    "reason_code": "passed",
    "execution_id": "검사-B",
    "rules": [
      {
        "rule_id": "G-002",
        "revision": 2,
        "decision": "PASS",
        "reason_code": "passed",
        "evidence": {
          "file_count": 4,
          "source_revision": "ebad709867cf1f3523075d8038ae41bd69ecae9b"
        }
      }
    ]
  },
  "review": {
    "actor": "local_operator",
    "classification": "violation",
    "fix_commit": "",
    "identity_verified": false,
    "regression_test": "tests.test_policy_documentation_contracts.VersionContractTests.test_record_walkthrough",
    "resolved_execution_id": "검사-B",
    "summary": "승인된 소스의 실제 근거로 분석을 다시 생성했습니다."
  },
  "events": [
    {
      "execution_id": "검사-A",
      "event": "inspection_started"
    },
    {
      "execution_id": "검사-A",
      "event": "rule_evaluated",
      "rule_id": "G-002",
      "decision": "BLOCK",
      "reason_code": "non_source_evidence"
    },
    {
      "execution_id": "검사-A",
      "event": "inspection_finished",
      "decision": "BLOCK"
    },
    {
      "execution_id": "검사-B",
      "event": "inspection_started"
    },
    {
      "execution_id": "검사-B",
      "event": "rule_evaluated",
      "rule_id": "G-002",
      "decision": "PASS",
      "reason_code": "passed"
    },
    {
      "execution_id": "검사-B",
      "event": "inspection_finished",
      "decision": "PASS"
    },
    {
      "execution_id": "검사-A",
      "event": "failure_reviewed",
      "classification": "violation"
    }
  ]
}
```
