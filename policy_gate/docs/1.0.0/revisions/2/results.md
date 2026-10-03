# 검사 결과와 근거 읽기

## 판정의 의미 {#classification}

| 결과 | 실제 의미 | 다음 행동 |
|---|---|---|
| PASS | 수행한 규칙 조건 충족 | 전체 필수 검사와 다음 단계 결과 확인 |
| BLOCK | 현재 구현이 거부 조건으로 분류 | 사유별 입력·조건 확인 후 수정 |
| UNSUPPORTED | 지원 범위 밖으로 분류 | 지원 검토, 통과로 대체하지 않음 |
| ERROR | 검사기 또는 호출 예외로 판정 완료 못 함 | 환경·구현 진단, 위반과 구분 |
| NOT_RUN | 해당 규칙이 실행되지 않음 | 선행 실패·중단·누락 확인 |
| NOT_APPLICABLE | 명시된 적용 조건에 해당하지 않음 | 제외 사유 확인, 수행 PASS와 구분 |

현재 공통 분류는 고정 오류 코드와 예외 종류를 사용합니다. PolicyError/SourcePolicyError 중 unavailable 또는 알 수 없는 검사 실패는 ERROR, unsupported를 포함하거나 지정된 미지원 코드는 UNSUPPORTED, 나머지는 BLOCK입니다. 다른 예외는 고정 policy_check_failed 등으로 남습니다. 코드의 단어가 아니라 실제 결과를 읽으세요.

**unreadable_source는 현재 BLOCK입니다.** 파일 권한이나 UTF-8 문제까지 앱 위반으로 확정해선 안 됩니다. 반대로 G-002/G-003에서 직접 모델을 파싱하다 발생한 예외는 I-000/L-001의 schema_invalid와 다른 ERROR가 될 수 있습니다. [분류 보완안](improvements.md#classification)은 아직 미구현입니다.

## 어떤 근거가 실제로 있나요 {#evidence}

규칙에 따라 references, changed_paths, observed/claimed, target, source_revision, digest 등이 남습니다. 실패 전에 evidence를 채우지 못했다면 값이 없을 수 있습니다. 없는 파일·줄·기대값을 문서나 UI가 추측하여 채우지 않습니다.

원래 판정과 나중의 원인 검토는 별도입니다. BLOCK이 나중에 확인된 오탐으로 분류되어도 원래 기록은 보존됩니다. 수정 commit·회귀 테스트·새 통과 검사 연결이 해결 근거입니다.

## 첫 실패와 뒤의 미실행 {#first-failure}

예를 들어 I-003에서 DB 불일치가 발생하면 앞의 I-000/I-001/I-006 통과는 유지되고, 뒤의 I-004/I-002는 NOT_RUN입니다. 전체 통과가 아니며 각 미실행 항목을 새 위반으로 해석하지 않습니다. 실행 ID·checkpoint·대상·시도를 함께 확인합니다.

## 과거 기록 {#legacy}

legacy 2.2.0에는 단계 요약만 있을 수 있습니다. 정식 1.0.0 규칙 설명을 볼 수 있어도 그것이 과거에 해당 규칙을 수행했다는 뜻은 아닙니다. 과거 기록의 문서 revision 1 링크는 그대로 유지하며 최신 설명은 명시적으로 선택합니다.
