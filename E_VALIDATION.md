# E 구현 및 검증 기록

> 초기 E 단독 구현의 기록입니다. 최신 통합 상태·redteam 연결 범위는 [E_INTEGRATION.md](E_INTEGRATION.md)를 참조하세요.

범위: Intent Gate (#6), Patch Gate (#9), Builder (#10), Local Adapter (#11), 외부 검증 중 Local 부분 (#15), redteam 연동.
이 문서는 구현 사용법과 검증 근거이며, 팀 설계의 원본은 기존 합의 문서입니다.

## 이번 작업의 기준

- 브랜치: `feat/e-runtime-implementation`. PR 제출용 E 변경 브랜치. 공통 스키마 PR #20 브랜치를 기준으로 리뷰합니다.
- 기준 main: `fb9d6ca`. B의 미병합 스키마 `71bea166baa339d05fe3bb5847c60e4095defbff`를 **로컬에서만** 병합했습니다.
- C 번들 호환성 검증: `ac4d3e5f4c2e8520f0a78b8e724d54da6467cb45`의 fixture/Code Patch를 별도 디렉터리에서 사용했습니다. C 구현 파일은 이 변경에 복사하지 않았습니다.
- 최신 B 계약에서 Local DB도 PostgreSQL입니다. 초기 SQLite 설명보다 실제 B/C 계약을 적용해 Local/AWS가 같은 AMD64 이미지를 사용할 수 있게 했습니다.
- 일반 앱을 모두 배포하는 범용 플랫폼이 아니라 합의한 Node 22 + Express + Prisma demo-app v1/v2 프로필입니다.

## 구현된 경계

| 구간 | 동작 |
|---|---|
| Intent Gate | B 스키마, revision 일치, unknowns 차단, 실제 파일·라인 근거 검증, 공개 HTTP/worker 규칙 |
| Patch Gate | C manifest·원본·결과·diff 해시 검증, 실제 diff 적용 결과 비교, 수정 경로 허용 목록, 삭제·심볼릭 링크·하드링크 차단, JS 구문 검사, 위험 패턴·일부 비밀키 패턴 차단 |
| Builder | 검증한 메모리 바이트로만 컨텍스트 구성, 승인된 lock/dependencies/Prisma generator만 사용, 설치 스크립트 비활성화, AMD64 및 provenance 확인, 동일 태그 내용 충돌 거부, 로컬 동시 빌드 잠금 |
| Local | Plan → Compose, 내부 PostgreSQL, web/worker, 비공개 DB, loopback 앱 포트, named volume, 비밀값 파일 0600, 상태 디렉터리 0700, immutable image ID 사용 |
| 검증·복구 | HTTP health, 메모 생성·조회, 이미지 업로드·조회, 이전 배포 데이터 확인, 실패 배포 복원, 명시적 이전 이미지 롤백, 양 버전 데이터 재확인 |
| 외부 URL | 명시적 `--publish` 옵션의 Quick Tunnel, HTTPS로 동일 데이터를 다시 확인한 후 성공 이벤트 |
| D 연결점 | B `DeployEvent` JSONL. `build()`는 B `BuildArtifact`, `deploy()`는 실행 결과 반환. 원시 앱 로그/비밀값은 이벤트에 넣지 않음 |

Patch 허용 목록은 `policy_gate/gate.py:DEFAULT_PATHS`에 있고 Builder가 호출할 때 변경하지 않습니다.
redteam corpus의 별도 허용 목록은 테스트에서만 주입됩니다. 이 테스트 프로필을 운영 허용 목록으로 취급하면 안 됩니다.

## 로컬 사용

Python 3.12, Node, Git, Docker Desktop/Engine + Buildx + Compose가 필요합니다. Node 22가 대상 런타임입니다.
Docker daemon은 신뢰할 수 있는 개발용 호스트이며, E와 A는 같은 daemon을 사용한다는 합의입니다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v

# C가 생성한 bundle: source/, patch.diff, manifest.json
.venv/bin/python -m policy_gate intent --snapshot SNAPSHOT --input INTENT.json --revision FULL_COMMIT_SHA
.venv/bin/python -m builder --snapshot SNAPSHOT --bundle BUNDLE --plan PLAN.json --output BUILD.json
.venv/bin/python -m adapters.local deploy --plan PLAN.json --artifact BUILD.json --state-dir .local/runtime/APP_NAME
.venv/bin/python -m adapters.local rollback --state-dir .local/runtime/APP_NAME
```

`APP_NAME`은 Plan.app과 같아야 합니다. state 디렉터리는 전용이며 symlink 경로를 허용하지 않습니다.
`DATABASE_URL`은 Local Adapter가 관리합니다. 그 외 Plan.secrets에 필요한 값은 `--secrets-file`의 JSON 객체로 전달합니다.
옵션 없이 외부 공개되지 않습니다. `--publish`는 테스트 앱에 인증 없는 임시 공개 URL을 생성합니다.

```sh
# E 게이트에 실제 공격 fixture 연결: 저장소 변경 없음
.venv/bin/python scripts/verify_e_redteam.py --corpus ../redteam-repo --output .local/redteam-result.json

# B + C + E의 실제 Docker 통합. output은 새 디렉터리여야 함.
# C_ROOT에는 위 C revision의 코드와 fixture가 있어야 함.
.venv/bin/python scripts/verify_e_local.py --c-root C_ROOT --output .local/run-NEW --state-dir .local/runtime/demo-app --publish
```

통합 스크립트는 v1 → v2(worker 추가) → v1 롤백을 수행하고 `events.jsonl`, `result.json`을 남깁니다.
롤백 시 worker는 제거됩니다. 이전 배포도 publish 상태였다면 그 Compose의 tunnel이 유지되므로, 검증 종료 시 해당 프로젝트를 down하여 공개를 종료합니다. 마지막 v1 컨테이너와 named volume은 자동 삭제하지 않습니다.
실행을 종료하려면 current.json의 config 경로를 사용해 해당 Compose 프로젝트만 `down` 합니다. `-v`는 데이터 삭제이므로 사용하지 않습니다.

## 검증 결과

아래는 **E 로컬 구현 단계(PR #24, `feat/e-runtime-implementation`) 시점의 기록**이며 그대로 보존한다. 이후 단계의 최신 수치는 [E_INTEGRATION.md](E_INTEGRATION.md)의 검증 결과와 [POLICY_GATE_V2_PLAN.md](POLICY_GATE_V2_PLAN.md)를 따른다. 현재 pin 기준 redteam은 39개이고 C 연동 미실행 사례는 없다.

- 단위·회귀 검사: 68개 통과 (기존 협업 규칙 및 B 스키마 검사 포함).
- E Intent/Patch redteam: 17/17 기대한 판정. 나머지 8개는 C의 prompt/tool 검증이므로 `not-run` 표시.
- v1/v2 C 번들의 실제 Gate + Docker AMD64 빌드 통과.
- AWS용 C 번들도 같은 로컬 이미지와 호환되는 BuildArtifact인지 확인. ECR/ECS 호출 없음.
- 실제 v1 → v2(worker 추가) → v1 롤백 통과. 두 버전에서 생성한 메모·이미지를 롤백 후 재확인했습니다.
- 실제 v2 컨테이너 시작 직후 실패를 주입해 v1 자동 복원과 기존 메모·이미지 유지를 확인했습니다.
- 검증 후 이번 테스트의 컨테이너·네트워크만 종료했습니다. 이미지와 named volume, 로컬 실행 기록은 보존했습니다.
- Quick Tunnel을 통한 외부 HTTPS health·메모·이미지 조회 통과. 검증 종료 시 해당 테스트 프로젝트를 down하여 공개를 종료합니다.
- 재현 가능한 실행 기록: `.local/e-validation/run-verified/result.json`, `events.jsonl`. 공격 입력 기록: `.local/e-validation/redteam.json`. 실행 기록 디렉터리는 비밀값이 섞일 가능성을 고려해 Git에서 제외했습니다.

CI에는 PR 본문 검사와 전체 Python 테스트를 추가했습니다. 실제 Docker·외부 tunnel 검증은 위 명시적 통합 스크립트로 분리했습니다.
원격 CI 결과는 해당 PR의 Checks에서 확인합니다. 아래 로컬 실행 기록과 구분합니다.

## 완료와 분리해야 하는 한계

1. B/C의 미병합 계약을 사용했습니다. main 통합 시 최신 계약과 다시 비교해야 합니다. D의 실제 Control Plane 및 A의 ECR/ECS end-to-end는 이번 로컬 완료 범위 밖입니다.
2. Intent의 evidence는 파일·라인의 존재를 검증합니다. 그 줄이 주장 전체를 의미적으로 증명하는지까지 판정하지 않습니다. Mapper의 SHA에 해당하는 동결 snapshot을 호출자가 전달해야 합니다.
3. 정규식 위험 패턴 검사는 샌드박스나 모든 악성 JS 탐지기가 아닙니다. 임의 앱 실행을 안전하게 보장하지 않습니다. 실제 앱 컨테이너는 outbound 통신이 가능하며 '외부 통신 0건'을 주장하지 않습니다.
4. Builder는 승인된 C lockfile에 묶여 있습니다. 의존성을 변경할 때 profile을 검토해 갱신해야 합니다. 기반 Node/Postgres/cloudflared 이미지의 digest 고정, 서명·SBOM은 아직 구현 범위가 아닙니다.
5. 롤백은 이미지·Compose 설정을 되돌리고 DB/업로드 volume을 유지합니다. DB 스키마/데이터를 과거 시점으로 되돌리지는 않습니다. 파괴적 Prisma 변경은 자동 승인하지 않습니다. 무중단·트랜잭션 배포를 보장하지 않습니다.
6. C 자동 수정 루프는 고정 오류 코드를 받을 수 있습니다. 원시 빌드 로그의 정교한 비밀값 제거 및 자동 재시도 오케스트레이션은 별도 연결 작업입니다.
7. 기본 smoke는 demo-app의 `/health`, `/api/notes`, `/api/images` 계약입니다. 범용 앱용 probe는 별도 프로필이 필요합니다.

## 이전 점수표 — 기능 중심 자체 평가

> 담당 일정과 실패 경로를 재검토한 최신 평가는 [E_SCOPE_AUDIT.md](E_SCOPE_AUDIT.md)의 **84/100**입니다. 아래 91점은 이전 평가로 보존합니다.

| 항목 | 배점 | 점수 | 근거와 감점 이유 |
|---|---:|---:|---|
| Policy Gate | 25 | 23 | 실제 C 번들·diff·소스 대조, 17개 공격/정상 판정. 의미적 근거 판정과 난독화 악성 코드 탐지는 미포함 |
| Builder·A 전달 | 20 | 18 | 실제 AMD64 v1/v2 빌드, Local/AWS 동일 이미지, 승인 lock·태그 충돌 방지. A의 ECR/ECS 연결 및 기반 이미지 digest 고정은 남음 |
| Local 배포·영속성 | 25 | 24 | 실제 DB·업로드·worker·외부 HTTPS 및 v1/v2 데이터 유지. 범용 앱·다중 호스트 대상은 아님 |
| 복구·검증 | 20 | 18 | 이전 이미지 롤백 및 양 버전 데이터 확인, 실패 복원 회귀 검사. DB 시점 복구·무중단 보장은 없음 |
| 통합·재현성 | 10 | 8 | 실제 C fixture와 B 계약, JSONL·CLI·재현 스크립트·CI 파일. D UI/A 클라우드 통합 및 원격 CI 실행은 남음 |
| **합계** | **100** | **91** | **E 로컬 데모 준비도. 전체 제품 완성도나 보안 인증 점수가 아님** |

main 병합 준비 시 B/C 계약의 최종 상태를 먼저 확인하고, D가 Gate→Builder→Local 호출 순서를 연결한 뒤 A와 동일 daemon의 BuildArtifact를 전달해 검증해야 합니다.


## PR 보완 검증 — 최초 실패 및 v1 공개 경계

- 최초 공개 배포 실패 시 앱·터널을 종료하고 named volume을 보존하도록 수정했습니다. 정리 실패도 별도 오류로 보고합니다.
- 실제 v1 HTTPS 메모·이미지 동작 확인, `/.env`, `/.git/config`, `/package.json`, `/prisma/schema.prisma` 4개 모두 404.
- 실제 Docker 바인딩에서 DB·tunnel 비공개 및 web loopback 제한 확인.
- 공개 앱이 동작하는 시점에 실패를 주입한 후 컨테이너 제거·외부 health 접근 종료·볼륨 보존 확인.
- 기록: `.local/e-validation/boundary-final/result.json` 및 `events.jsonl`. 모바일 직접 검증은 false로 기록합니다.
- 공격 사례와 노출 범위, 인계 계약은 [E_HANDOFF.md](E_HANDOFF.md)에 정리했습니다.
- 이 기록은 실제 휴대폰, A의 AWS, D의 Control Plane 연결 검증을 대신하지 않습니다.

최신 자체 평가: Builder 19/20, Local 24/25, Policy 23/25, 공개 검증 16/20, 문서 10/10 = **92/100**. 미완료 항목은 실제 휴대폰 확인과 팀 합동 연결입니다. 보안 보장 확률을 뜻하지 않습니다.
