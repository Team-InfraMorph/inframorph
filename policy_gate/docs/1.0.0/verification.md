# 정책 1.0.0 검증 기록

## 실행 결과 {#results}

2026-10-03, main d49ebee의 AWS·배포 복구 변경을 통합하고 정책 버전별 문서 구조로 정리한 작업본에서 다음 결과를 확인했습니다.

| 검증 | 결과 | 확인한 범위 |
|---|---|---|
| Python 전체 회귀 | 467개 통과 | 정책·호출 경로·문서 예시·기록·재검사와 고정 redteam corpus |
| 문서·정책 수명 주기 집중 검사 | 69개 통과, 전체 회귀에 포함 | 버전별 조회, 과거 기록 보존, 문서 링크·예시와 기준선 |
| AWS Adapter 모의 검사 | 43개 통과 | AWS 실행 경로의 모의 결과; 실제 AWS 배포 아님 |
| UI 자동 검사 | 34개 통과 | 문서 렌더링·버전 링크·기존 링크 정리와 검사 카드 |
| UI 빌드·카탈로그 | 통과 | 배포용 화면 생성, 18개 규칙·문서·기준선 일치 |

redteam corpus는 validation/redteam-source.json에 고정된 commit 488833528c3c9a389d0af05606c9bb1cdeaa1c99의 39개 사례입니다. 이 결과를 범용 앱의 보안 정확도나 운영 배포 성공률로 해석하지 않습니다.

## 확인 범위 {#findings}

지원 소스·Intent·Plan·패치·빌드 입력의 검사 조건과 규칙 목록은 유지했습니다. 문서는 정책 버전마다 한 묶음으로 조회하며 신규 검사 기록에 별도 문서 수정 번호를 추가하지 않습니다. 과거 판정에 저장된 필드는 보존하고, 문서 변경만으로 정책 활성화나 재검사 작업을 추가하지 않는지 확인했습니다.

이번 관리 코드와 메타데이터 구조 변경은 검사기 구현 식별값과 정책 해시에 반영됩니다. 기존 정책 연결을 새 검사 결과로 간주하지 않으며, 활성화 시 기존 영향 평가·재검사 절차를 적용합니다. 이후 설명 문구만 수정하면 문서 해시만 달라집니다.

## 화면 확인 {#ui}

브라우저에서 정책 버전 선택만 남은 화면과 버전별 문서 링크를 확인했습니다. 기존 문서 수정 번호가 포함된 링크도 같은 버전의 규칙·절로 연결됩니다. 규칙의 표·JSON 예시와 접을 수 있는 구현 근거를 유지합니다. 과거 검사에서 규칙 문서로 연결되는 경로는 UI 자동 검사로 확인했습니다.

## 검증의 한계 {#limits}

실제 모델 호출·신규 Docker 배포·AWS 또는 온프레미스 운영 배포는 이번 검증에 포함하지 않습니다. 정책 통과, 실제 배포, 공개 접속은 각각 다른 결과입니다. 화면의 의미 전달과 문서 설명의 정확성 전체를 자동 검사만으로 보장하지 않습니다.

## 구현·검증 근거 {#references}

- tests.test_policy_documentation.DocumentationContract
- tests.test_policy_documentation_baseline.WalkthroughTests
- tests.test_policy_documentation_contracts.VersionContractTests
- tests.test_policy_lifecycle.LifecycleTests

[재현 방법](validation.md#run) · [변경 이력](releases.md#policy-baseline)
