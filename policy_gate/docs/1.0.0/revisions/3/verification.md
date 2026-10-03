# 문서 revision 3 검증 기록

## 실행 결과 {#results}

2026-10-03, main fa84467을 통합한 구현 commit 138e6ca와 문서 revision 3의 로컬 변경을 검증했습니다. 실제 실행한 회귀 수이며 보안 탐지율이 아닙니다. 과거 revision 2의 423개·26개 결과는 당시 기록으로 보존합니다.

| 검증 | 결과 | 확인 범위 |
|---|---|---|
| Python 회귀 | 446개 통과 | 문서·기준선·대표 사례 28개 포함, Gate·runtime·API·복구 |
| AWS Adapter 회귀 | 34개 통과 | 모의 환경; 실제 AWS 배포 없음 |
| redteam 호스트 검증 | 39개 통과, 실패·미실행 0 | 고정 corpus revision 488833528c3c9a389d0af05606c9bb1cdeaa1c99 |
| UI 회귀 | 31개 통과 | 정책 결과·문서 렌더링·링크·배포 화면 |
| UI 빌드 | 통과 | 기존 앱 구성 유지 |
| 정책 카탈로그·기준선 | 18개 규칙 유효 | 규칙·문서 연결, 이전 revision 및 고정 파일 해시 |

## 확인한 동작과 한계 {#findings}

근거 파일 누락, DB 분석 불일치, 무관한 패치 각각에 대해 첫 실패·남은 규칙의 미실행·수정 후 새 PASS를 확인했습니다. 실제 전체 경로와 단위 호출의 사유 코드 차이도 확인했습니다. 문서의 전체 Intent JSON을 직접 파싱하고 소스·Intent 검사에 넣었습니다.

Local 실패 시 AWS·온프레미스 명령을 호출하지 않는 검사와 Local 통과 후 병렬 실행하는 기존 회귀를 확인했습니다. 온프레미스는 별도 보존 자료 부족으로 정책 재검사 UNAVAILABLE이며 실행 중인 서비스를 유지하는 기존 회귀도 통과했습니다.

정책·규칙·구현 식별값은 이번 revision 2→3 설명 보완 전후가 같습니다. revision 1·2의 파일은 그대로 보존합니다. 고정 문서 변경·누락·추가 및 활성 정책 식별값 변경을 거부하는 검증도 포함합니다. 문서 변경만으로 활성화 이벤트나 정책 재검사 작업이 생기지 않는 회귀를 통과했습니다.

이 검증은 실제 모델 호출·새 Docker 배포·원격 AWS·온프레미스 실행을 하지 않습니다. 정책 통과를 DB 데이터 이전, 서비스 가용성 또는 실제 공개 범위 검증으로 확대하지 않습니다.

## 화면 확인 {#ui}

실행 중인 조종실에서 정책 1.0.0의 문서 revision 3, Intent 계약표·JSON 예시와 고정 절 연결을 확인했습니다. 741px 화면에서 페이지 전체 너비의 넘침이 없고 표와 코드가 기존 문서 레이아웃에 표시됩니다. 이전 revision 조회는 API 회귀와 문서 선택에서 확인했습니다.

## 구현·검증 근거 {#references}

- 문서·대표 사례: tests.test_policy_documentation, tests.test_policy_documentation_baseline
- Local 선행 조건: tests.test_policy_documentation_baseline.ExecutionFlowTests.test_local_failure_prevents_remote_commands
- Local 이후 병렬 실행: tests.test_control_plane_verify.VerifyAfterDeployTest.test_local_test_runs_first_then_others_together_and_each_is_checked_on_finish
- 온프레미스 검사 범위: tests.test_policy_lifecycle.LifecycleTests.test_main_onprem_scope_is_recorded_without_fabricating_complete_inspection

[재현 명령과 기준](validation.md#run) · [고정 범위](versioning.md#baseline)
