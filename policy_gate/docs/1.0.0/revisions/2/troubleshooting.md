# 실패에서 수정과 재검증까지

## 다시 시작하는 순서 {#restart}

1. 실패한 배포·대상·실행 ID·시도·checkpoint와 정책 버전을 확인합니다.
2. 첫 실패를 찾고 뒤의 NOT_RUN과 진단 로그를 구분합니다.
3. 같은 commit의 입력과 규칙의 기대 조건을 비교합니다. 자료가 없다면 원인 확인 불가로 남깁니다.
4. 앱 소스, 분석, 설계, 패치 생성기, 검사 환경 중 수정할 대상을 정합니다.
5. 입력이 바뀌면 새 검사·필요한 정상 배포를 수행하고, 같은 자료의 일시 환경 문제라면 환경 복구 후 재검사합니다.
6. 새 실행에서 해당 실패 규칙과 전체 필수 검사가 완료됐는지 확인합니다. 과거 기록은 수정하지 않습니다.

## 사유에서 규칙으로 {#reasons}

| 사유 또는 증상 | 확인할 설명 | 우선 확인할 자료 |
|---|---|---|
| evidence_file_missing / evidence_line_missing | [I-001](rules/I-001.md#remedy) | 실제 수집 파일·줄과 source_revision |
| db_provider_mismatch / db_requirement_missing / db_evidence_unrelated | [I-003](rules/I-003.md#decisions) | Prisma datasource와 Intent.state |
| storage_evidence_unrelated / storage_evidence_unsupported | [I-004](rules/I-004.md#decisions) | 검토된 저장 코드·uploads·인용 |
| worker_requirement_missing / worker_command_unsupported | [I-002](rules/I-002.md#decisions) | scripts.worker와 worker 명령 |
| config_overrides_secret / config_not_supported | [I-006](rules/I-006.md#decisions) | config 이름과 secrets 이름 |
| intent_source_mismatch / unreviewed_runtime_source | [G-002](rules/G-002.md#decisions) | 실제 소스·검토 프로필·분석 |
| plan_source_mismatch | [G-003](rules/G-003.md#decisions) | 대상별 실행 프로필 |
| schema_invalid / plan_state_mismatch / plan_secret_mismatch | [L-001](rules/L-001.md#decisions) · [L-002](rules/L-002.md#decisions) | 실패 stage와 Intent·Plan |
| artifact_digest_mismatch / revision_or_target_mismatch | [P-001](rules/P-001.md#decisions) | 원본·bundle·manifest·대상 |
| diff_source_mismatch / diff_checker_unavailable | [P-002](rules/P-002.md#decisions) | diff 적용 결과와 Git 환경 |
| prisma_structure_changed | [P-003](rules/P-003.md#remedy) | provider 외 스키마 변경 |
| patch_behavior_changed | [P-004](rules/P-004.md#remedy) | 전체 코드 diff와 승인 변환 |
| dependency_change_forbidden | [P-005](rules/P-005.md#decisions) | package 전체 차이와 승인 lock |
| forbidden_code_pattern / javascript_parser_unavailable | [P-006](rules/P-006.md#decisions) | 실패 path·파서 환경 |
| secret_in_source | [P-007](rules/P-007.md#remedy) | 비공개 소스의 민감정보 |
| unreviewed_dependency_lock / build_profile_incomplete | [X-001](rules/X-001.md#decisions) | 최종 Builder 입력 |
| unreadable_source | [공통 분류](results.md#classification) | 권한·인코딩·파일 수집 |

같은 코드는 여러 규칙의 공통 함수에서 발생할 수 있습니다. 사유 검색 결과만으로 규칙을 단정하지 말고 실행 기록의 rule_id를 우선합니다.

## 무엇을 수정해야 하나요 {#repair}

| 확인 결과 | 조치 | 해결을 확인하는 방법 |
|---|---|---|
| 앱은 맞고 분석이 다름 | 사실에 맞게 재분석 | 동일 소스의 새 Intent·Plan 검사 |
| 앱 자체 변경이 필요 | 새 commit으로 일반 배포; 지원 범위 별도 확인 | 새 소스에 대한 전체 검사와 실제 배포 |
| Plan이 Intent와 다름 | 설계 생성 수정 | G-003·L-001·L-002부터 다음 단계 |
| 패치가 무관한 코드를 바꿈 | 생성기/템플릿 수정 후 bundle 재생성 | P-001부터 패치 전체·빌드 직전 검사 |
| 파서·Git 등 검사 환경 문제 | 환경 복구 | 같은 입력의 새 검사에서 ERROR 해소 |
| 자료 없음·원인 미확인 | 확인 불가와 필요한 자료 기록 | 자료 확보 전 해결로 표시하지 않음 |

정책 업데이트 재검사 버튼은 잘못된 분석·패치를 새로 생성하지 않습니다. 기존 자료가 동일하면 같은 위반이 다시 나오는 것이 정상입니다. 운영자 검토로 BLOCK/ERROR를 면제할 수 없습니다.
