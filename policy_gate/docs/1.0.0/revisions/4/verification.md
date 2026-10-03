# 문서 revision 4 검증 기록

## 실행 결과 {#results}

2026-10-03, 문서 기준선 commit 544ae0c 위의 revision 4를 검증했습니다. 정책 판정 구현·규칙은 그대로이며, 아래는 이번에 실제 실행한 결과입니다. 보안 탐지율이나 실제 배포 성공률을 뜻하지 않습니다.

| 검증 | 결과 | 확인 범위 |
|---|---|---|
| Python 전체 회귀 | 452개 통과 | 문서·기준선·사례 34개 포함; Gate·runtime·API·복구 |
| 문서 사례와 계약 | 34개 통과 | 28개 기존 검사와 r4의 6개 검사; 아래 실제 입력·기록 재현 |
| redteam 연결 | 39개 사례 통과 | 위 전체 회귀의 통합 검사에서 실행. 고정 corpus 488833528c3c9a389d0af05606c9bb1cdeaa1c99 |
| UI 회귀 | 31개 통과 | 기존 문서 렌더링·링크·검사 결과 |
| 정책 카탈로그·기준선 | 18개 규칙 유효 | HEAD 기준 비교 포함; 기존 기준선 및 문서 보존 |
| 브라우저 | 확인 | r4 표·JSON·실패 기록, 문서 선택으로 이전 r3 조회 |

최초 전체 실행에서는 기본 위치의 redteam 저장소 revision이 고정 계약과 달라 한 오류와 한 실패가 발생했습니다. 정책·fixture를 변경하지 않고 CI와 동일하게 보존된 고정 corpus를 지정한 전체 재실행에서 452개 모두 통과했습니다. 환경 불일치를 통과로 처리하거나 해당 검사를 생략하지 않았습니다.

AWS Adapter 별도 모의 검사 34개와 UI 빌드 결과는 revision 3의 검증 기록으로 보존합니다. 이번 설명 보완에서 이를 새로 실행한 결과로 기재하지 않습니다. 실제 모델 호출·새 Docker 배포·원격 AWS·온프레미스 실행은 하지 않았습니다.

## 네 항목에서 확인한 동작 {#findings}

1. **사유 안내:** 승인 소스 밖 파일, 범위 밖 줄, 빈 줄 인용을 G-002에 넣어 실제 사유 코드를 확인했습니다. 문제 해결 표에서 해당 규칙의 해결 절차로 연결됩니다.
2. **입력 계약:** 문서의 Local Plan JSON을 그대로 파싱해 G-003과 Plan 관계 검사를 통과시켰습니다. manifest JSON은 실제 생성 결과와 동일하며 이를 포함한 묶음으로 패치 규칙 전체가 통과합니다. 필수값 누락·변경 목록 순서·추가 키 및 검사하지 않는 생성 메타데이터의 경계도 확인했습니다.
3. **기록 읽기:** 격리 DB에서 G-002 BLOCK→수정한 Intent의 새 PASS→원인 검토 연결을 저장·조회했습니다. 문서 발췌와 결과를 대조하고, 원래 판정 보존·입력 해시 변화·실행 ID 구분·상세 로그 만료 후 연결 유지·전체 검사는 아직 NOT_RUN임을 확인했습니다.
4. **고정 범위:** 이번 문서 변경 전후 policy_digest·rules_digest·implementation_digest가 같음을 확인했습니다. 이전 revision 1·2·3의 파일 해시와 기존 기준선은 그대로입니다. 문서 포인터 변경만으로 정책 활성화 이벤트나 재검사 작업이 추가되지 않습니다.

기존 기준선 검사는 문서 r1·r2·r3과 명시된 구현 식별 범위를 보호합니다. r4 전체 바이트나 모든 배포 실행 코드를 자동 고정하는 것으로 표현하지 않습니다. [정확한 고정 범위](versioning.md#baseline)를 참고하세요.

## 화면 확인 {#ui}

실행 중인 조종실에서 r4의 Plan 계약표와 manifest 코드, 실패→수정→재검증 기록 표를 확인했습니다. 741px 화면에서 페이지 너비를 넘치지 않았고, 구현·검증 근거는 접힌 상태로 표시됩니다. 문서 선택에서 r3를 고르면 과거 설명이 보존되며 r4로 이동하는 선택 링크가 나타납니다. API 회귀에서도 r3의 고정 절을 조회했습니다. 작은 휴대폰 너비 전체와 보조공학 사용성 전반을 이번 확인으로 검증했다고 주장하지 않습니다.

## 구현·검증 근거 {#references}

- 문서·이전 revision·링크: tests.test_policy_documentation.DocumentationContract
- 기존 실패→수정 사례: tests.test_policy_documentation_baseline.WalkthroughTests
- 새 Plan·manifest·기록 사례: tests.test_policy_documentation_r4.RevisionFourTests
- 고정 corpus 연결: tests.test_e_integration.CorpusIntegrationTests

[재현 방법](validation.md#run) · [변경 내역](releases.md#revision-4)
