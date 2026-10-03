# [E] Policy Gate v2.2 — 상세 평가 및 최종 설계

> 상태: 이번 범위의 규칙·정책 결과 API·UI를 구현하고 자체 검증을 완료했다. main 병합과 팀 합의는 아직 아니다.\
> 최종 정리: 2026-10-03. 설계 기준 commit: inframorph `800681b (origin/main, 2026-10-03 fetch 기준)` 및 전달받은 InfraMorph 기획.\
> 담당 범위: E의 정책 검사, Builder·Local Adapter 실행 경계, redteam.\
> 작업 브랜치: inframorph `feat/e-policy-hardening`, redteam-repo `feat/e-policy-fixtures` (pin `48e27852`).\
> 이 문서는 이전 v2 초안과 대화에서 작성한 고도화 문서를 통합·대체한다.
>
> 구현 결과 요약: 근거·설정·Prisma 구조 보존·허용 파일 내부 변경 검사를 추가하고, `GET /api/deployments/{id}/policy`와 정책 검사 카드를 연결했다.
> 검증은 `tests` 360개, `adapters/aws/tests` 34개, 정책 UI 6개, redteam 39개(실패 0·미실행 0), 실제 Docker·공개 HTTPS 재배포로 수행했다.
> 아래 설계 본문은 착수 시점 기준을 유지하고, 각 수용 기준의 달성 여부는 체크 항목과 `E_VALIDATION.md`를 우선한다.
> 미구현으로 남은 설계 항목은 5절의 `required_rules`/`evaluated_rules` 기록과 폐기된 정책의 과거 PASS 차단 적용이다.

## 1. 결정 요약

InfraMorph의 Policy Gate는 AI가 제안한 요구사항과 환경 전환 패치의 **검증 가능한 사실, 허용 변환, 실행 결과물의 동일성**을 확인한다.

최종 방향은 다음과 같다.

1. 검토된 데모 프로필을 유지하면서 규칙을 강화한다.
2. 근거의 존재뿐 아니라 주장과 코드의 관계를 검사한다. 그러나 앱의 사업적 의미 전체를 증명한다고 주장하지 않는다.
3. 파일 허용 목록에 더해 변환별 사전조건·허용 변경·사후조건을 검사한다.
4. Gate·패처·Builder·Adapter가 함께 지원하는 프로필만 실행한다.
5. 원본 앱의 신뢰, AI가 추가한 변경의 안전성, 실제 배포 상태를 별도로 기록한다.
6. 기존 UI의 정책 타임라인·패치 검증 표시를 확장해 규칙별 결과와 차단 원인을 보여준다. 설명은 고정된 규칙 카탈로그에서 제공한다.

**범위 제외:** RAG·embedding·벡터 DB·검색 기반 설명 생성은 설계·구현하지 않는다. 원래 E 목표인 정상 입력 통과, 공격/위반 차단, 근거·패치·노출 검사와 그 결과 표시가 이번 완료 범위다.

작업 브랜치: inframorph `feat/e-policy-hardening`(최신 main에서 생성), redteam-repo `feat/e-policy-fixtures`(기존 브랜치 유지). 이 절 이후의 설계 본문은 착수 시점 기준으로 유지하며, 구현 결과는 문서 상단 요약과 각 수용 기준 체크를 따른다.

**우선순위는 정책 계약 → 근거·설정 검사 → 제한 변환 검사 → 실행 바인딩 → 정책 결과 UI → 정상·공격 통합 검증다.**

## 2. 기존 설계의 상세 재평가

### 2.1 유지할 강점

- 실제 Gate 거부가 Builder·Adapter 실행을 막는 기존 연결을 재사용할 수 있다.
- 원본·diff·패치 결과의 동일성을 확인하고 Builder가 검증된 메모리 바이트를 소비한다.
- AI의 맥락 판단과 결정론적 실행 제한을 분리하는 방향이 주제에 맞는다.
- 정상·위반 쌍과 외부 예제를 함께 평가하려는 방향이 적절하다.

### 2.2 보완이 필요한 설계 결함

아래 우선순위는 **설계 보완 순서**다. 새로 재현한 코드 취약점 목록이 아니다.

| 우선순위 | 기존 안의 빈틈 | 문제가 되는 경우 | 이번 수정 |
| --- | --- | --- | --- |
| 필수 | Gate 개선과 외부 앱 지원을 직접 연결 | Gate는 허용해도 패처·Builder가 고정 파일/lock 때문에 거부 | 전체 실행 경로의 프로필 지원 확인 |
| 필수 | 패치 보존과 원본 앱 신뢰의 구분 부족 | 원본의 위험 동작을 그대로 보존해도 안전한 앱으로 오해 | 원본 승인 범위와 변환 검사 결과 분리 |
| 필수 | “허용 위치 밖의 AST 보존”이 추상적 | await 추가, 오류 처리, 호출 순서 변경을 임의로 허용 | 변환 명세와 제한된 구문 비교 정의 |
| 필수 | 주장한 근거만 검사 | 실제 worker·DB 사용을 Intent에서 누락 | 지원 범위 내 독립 사실 추출과 누락 검사 |
| 필수 | 의존성·빌드 실행 범위가 불명확 | lock 일치만으로 새 generator나 설치 코드까지 신뢰 | 승인 dependency 세트·generator·빌드 명령 별도 고정 |
| 필수 | commit 태그와 실행 artifact 구분 부족 | 같은 commit에 정책·템플릿·패치가 달라짐 | 검사 식별자·빌드 식별자·이미지 digest 분리 |
| 높음 | BLOCK·UNSUPPORTED·ERROR의 경계 부족 | 미지원 문법을 악성 입력으로 표시하거나 오류를 자동 수정 시도 | 판정/완료 여부/재시도 책임 명세 |
| 높음 | rollback과 정책 변경 처리 누락 | 폐기된 정책의 과거 PASS로 새 작업 허가 | 재검증·폐기·기존 실행 상태를 분리 |
| 높음 | 정책 상세 UI 계약 부족 | 실패 코드만 보이거나 이전 재시도 결과를 현재 통과로 오해 | 단계·target·attempt별 결과 API와 고정 설명 |

### 2.3 현재 코드에서 확인한 제약

| 위치 | 관찰 | 설계에 미치는 영향 |
| --- | --- | --- |
| `policy_gate/gate.py::validate_intent` | 근거 파일·줄 존재 검사 | 관계와 누락 검사를 추가해야 함 |
| `policy_gate/gate.py::validate_patch` | 경로·무결성·문자열 금지 패턴 | 허용 파일 내부의 변환 구조 검사 필요 |
| `analyzer/source_policy.py` | 검토된 데모 소스 해시와 예상 Intent/Plan 제한 | 즉시 제거하지 않고 교체 조건 필요 |
| `code_patch/runner.py::transform` | 고정 Prisma·images·의존성 조합 | E만 변경해서 외부 앱 지원 완료라고 할 수 없음 |
| `builder/runtime.py::check_build_profile` | lock digest·generator·필수 경로 제한 | 신규 프로필에 Builder 지원이 함께 필요 |
| `builder/templates/Dockerfile` | src/prisma 중심 복사, 고정 server 진입점 | 임의 디렉터리 배치는 아직 지원하지 않음 |
| `schemas/artifacts.py` | `app:<revision>` 형태의 이미지 태그 | 정책 버전별 빌드 식별자와 동일하지 않음 |
| `adapters/local/runtime.py` | 고정 저장 경로·worker 명령·schema 적용 흐름 | 지원 능력과 실제 데이터 적용 안전성 분리 |

## 3. 보장 범위와 위협 모델

### 3.1 보장하려는 것

- 지원 프로필 안에서 Intent의 주장이 검증 가능한 소스 사실과 모순되지 않는다.
- 지원 추출기가 확인한 필수 요구사항을 Intent가 누락하면 통과시키지 않는다.
- AI 패치는 승인된 변환 명세를 벗어나지 않는다.
- 검증한 입력과 다음 단계가 소비하는 입력이 동일하다.
- 검사 실패·미지원·도구 오류는 실행 권한이 되지 않는다.

### 3.2 보장하지 않는 것

- 모든 JavaScript의 동작·업무 의미·비밀값·난독화 분석.
- AST 동일성만으로 환경 변화 전후의 완전한 의미적 동등성 증명.
- 원래 앱에 존재하던 모든 취약점의 발견.
- 정적 검사만으로 실제 DB 데이터 손실이나 모든 외부 통신 부재를 증명.
- 컨테이너를 적대적 멀티테넌트 실행에 충분한 보안 경계로 인증.

초기 운영 대상은 검토된 샘플과 명시적으로 등록한 외부 예제다. **임의의 공격자 소스를 안전하게 실행하는 서비스로 확장하려면 빌드/런타임 격리, 자격 증명·네트워크 통제를 별도 설계해야 한다.** 이는 데모 프로필 해시를 제거하는 것만으로 해결되지 않는다.

### 3.3 신뢰 경계

| 항목 | 취급 |
| --- | --- |
| 레포 코드·README·Agent 산출물·manifest | 비신뢰 데이터 |
| Repo Mapper 결과 | 출처와 입력 digest를 대조할 계약. 파일 목록 누락도 확인 |
| 실행 정책·승인 부품·프로필 | 도구 측에서 리뷰·버전 관리. 레포가 선택·수정 불가 |
| secret 값 | 별도 실행 환경 주입. 정책 결과·정책 로그에 포함하지 않음 |
| 정책 검사 프로세스와 로컬 작업 상태 | 현재 MVP의 신뢰 기반. 호스트 관리자/도구 자체 침해는 범위 밖 |

공격자가 바꿀 수 있는 입력은 source, evidence, Intent, Plan, diff, manifest다. 필수 검사 생략, 오래된 승인 재사용, 검증 뒤 변조, 파서 자원 고갈도 평가한다.

## 4. 지원 프로필을 전체 파이프라인의 계약으로 관리

프로필은 파일 해시 목록에 그치지 않고 아래 능력을 명시한다.

| 필드 묶음 | 내용 |
| --- | --- |
| 실행 대상 | Node·모듈 형식·Prisma 버전·Local/AWS |
| 원본 승인 | reviewed-demo 또는 reviewed-example, 승인 범위·revision |
| 소스 형태 | 진입점·저장 경로·지원하는 import/call 형태 |
| 변환 | transform ID, 명세 버전, 승인 부품 digest |
| 빌드 | Dockerfile digest, base image digest, dependency 세트, platform |
| 검사 | parser 버전/옵션, 필수 rule ID, 자원 제한 |
| 배포 | Adapter가 지원하는 DB·storage·command·노출 형태 |

프로필 선택은 운영자/도구 설정과 결정론적 사전조건으로 한다. 레포나 모델의 `profile` 필드는 선택 권한이 없다. 명시적 선택 없이 여러 프로필이 동시에 해당되면 자동으로 더 느슨한 프로필을 고르지 않고 중단한다.

**유효 지원 범위 = Gate ∩ Planner ∩ Patcher ∩ Builder ∩ Adapter.** 시작 전에 필요한 transform·target을 각 모듈이 지원하는지 확인한다. 불일치는 `UNSUPPORTED / PIPELINE_CAPABILITY_MISMATCH`다.

첫 외부 예제는 기존 스택·레이아웃과 가까운 사례를 고정 revision으로 등록한다. 서로 다른 프로필에 대해 소스 구조를 달리한 사례를 검증하되, 사례가 2개 통과했다고 임의 Express 앱 지원으로 표현하지 않는다.

## 5. 판정 계약과 오류 처리

| decision | 정의 | 예시 | 실행 |
| --- | --- | --- | --- |
| PASS | 해당 단계의 필수 검사 전체 완료·충족 | 지원 변환만 적용 | 다음 단계 가능 |
| BLOCK | 지원 범위에서 위반·모순을 확인 | 필드 삭제, hash 불일치, 관련 근거 없음 | 중단 |
| UNSUPPORTED | 현재 분석/변환 능력으로 판정 불가 | 동적 진입점, 미지원 문법, 미지원 pipeline | 중단 |
| ERROR | 도구/환경 실패로 검사 미완료 | 파서 프로세스 장애, 내부 시간 초과 | 중단 |

입력 크기·개수 같은 명시적 한도 초과는 `BLOCK / INPUT_LIMIT_EXCEEDED`로 기록한다. 한도 내 입력에서 검사 시간이 초과되면 `ERROR / CHECK_TIMEOUT`이다. 손상된 정책 패키지·필수 규칙 누락은 `ERROR`이며 실행하지 않는다.

검사 결과에 `complete`, `required_rules`, `evaluated_rules`를 기록한다. `PASS`는 필수 규칙 누락이나 미완료가 있으면 생성할 수 없다. 중간에 BLOCK 후 검사를 중단해도 실행은 금지하며, 확인하지 않은 영역까지 안전하다고 표시하지 않는다. 적용 제외는 프로필의 결정론적 조건과 사유로 기록한다.

하나의 결과를 집계할 때 확인된 위반이 있으면 BLOCK, 위반 없이 실행 오류가 있으면 ERROR, 둘 다 없고 지원 불가 항목이 있으면 UNSUPPORTED로 한다. 이 경우에도 `complete=false`와 각 finding은 유지한다. 모든 필수 검사가 완료된 경우에만 PASS다.

의존하는 검사가 실패하면 후속 규칙은 수행하지 않는다. 원인 규칙과 미실행 규칙을 구분하며, 로그는 고정된 규칙 순서로 정렬한다. 오류와 이미 확인한 위반이 동시에 있으면 두 사실을 보관하고 미완료 상태를 표시한다.

C/D는 고정된 `retry_hint`만 받는다. 근거·패치를 고칠 수 있는 실패만 기존 자기 수정 예산 안에서 재시도한다. 위변조·정책 로딩 오류·미지원 프로필을 모델의 반복 수정으로 해결하려 하지 않는다. 예산은 전체 작업에 공유하고 정책 재시도마다 초기화하지 않는다.

## 6. 규칙 명세

모든 규칙은 `ID / 적용 조건 / 입력 / 검사 / 실패 사유 / 정상 사례 / 위반 사례 / 책임 모듈`을 가진다. 이 절의 ID를 정식 제안으로 사용하고 이전 초안의 축약 ID와 혼용하지 않는다.

### 6.1 입력과 근거

| ID | 적용·검사 | 대표 결과 |
| --- | --- | --- |
| G-001 | snapshot의 전체 파일 목록·경로·파일 종류·크기·digest 확인 | 변조·누락·외부 경로 → BLOCK |
| G-002 | 스키마·revision·지원 프로필과 전체 pipeline 일치 | 미지원 조합 → UNSUPPORTED |
| I-001 | 주장 근거를 현재 파일의 해당 구문 범위에 연결 | 실존하지만 무관한 구문 → BLOCK |
| I-002 | worker 명령을 허용 형식으로 분해하고 진입 파일 확인 | 정적 파일 없음 → BLOCK; 동적 명령 → UNSUPPORTED |
| I-003 | Prisma datasource의 provider/ORM을 실제 구문에서 추출 | Intent와 불일치 → BLOCK |
| I-004 | 지원 패턴에서 파일 쓰기·조회와 저장 경로 관계 추출 | 모순 → BLOCK; 관계 추출 불가 → UNSUPPORTED |
| I-005 | 프로필이 필수로 정의한 실행·저장 사실과 Intent를 양방향 비교 | 확인된 필수 요구 누락 → BLOCK |
| I-006 | config와 secret 이름·출처·허용값 검사 | 실행 옵션 주입·reserved 이름 충돌 → BLOCK |

Evidence는 파일/줄만으로 승인하지 않는다. E 내부에서 `claim_path`, `snapshot_digest`, `file`, `source_span`, `fact_kind`, `fact_value`, `extractor_version`을 연결한다. Agent가 제공한 fact는 다시 추출·대조한다. 댓글/README는 코드의 실행 사실을 증명하는 근거로 사용하지 않는다.

줄 번호는 UI 인용 위치이며 파일 digest와 함께 사용한다. 이전 revision의 줄 번호를 그대로 현재 근거로 인정하지 않는다. 범용 호출 그래프를 만들지 않고, 지원 프로필의 정적 참조 관계까지만 확인한다.

worker에 package script가 반드시 있어야 하는 것은 아니다. 직접 실행이 지원되면 실제 `node src/worker.js` 명령과 파일로 충분하다. package script를 근거로 제시한 경우에만 해당 script와 실제 command의 일치를 추가로 확인한다.

I-005는 모든 코드에서 모든 요구를 발견한다는 보장이 아니다. 프로필이 정의한 구문에서 추출된 필수 사실의 누락을 검사한다. 저장 데이터를 영구 보관해야 하는 사업적 이유는 C의 판단이며, 읽기/쓰기 코드가 있다는 사실과 구분해 기록한다.

지원 목록에 없는 일반 config는 `UNSUPPORTED / CONFIG_NOT_SUPPORTED`, secret·reserved 이름 충돌이나 금지 실행 옵션은 `BLOCK / CONFIG_POLICY_VIOLATION`으로 구분한다. 프로필과 어긋난 값이 모두 공격이라는 의미는 아니다.

설정은 출처를 분리한다. `DATABASE_URL` 같은 secret, `STORAGE_DRIVER` 같은 Planner 설정, `PORT` 같은 service 설정을 앱의 임의 config로 덮어쓰지 못한다. 같은 값을 두 곳에 선언하는 경우에도 프로필의 명시적 동등성 규칙이 없으면 충돌로 취급한다. 외부 예제에 정상 config가 추가되면 승인 프로필에서 타입·범위를 추가한다.

### 6.2 Intent와 Plan의 연결

| ID | 검사 | 비고 |
| --- | --- | --- |
| L-001 | source/app/target 및 검증된 Intent digest 일치 | target은 운영자 선택에 결합 |
| L-002 | workload kind·command·public·port·health 대응 | 승인되지 않은 서비스 추가 금지 |
| L-003 | DB/storage/secret/config가 허용된 환경별 대응 | Local/AWS 값 자체가 같을 필요 없음 |
| L-004 | 자원 상한·공개 정책·reserved 서비스 이름 | CPU/메모리의 양수 검사만으로 대체 불가 |

전체 Planner 로직을 복제하지 않고 필수 불변조건과 승인 mapping 버전을 검사한다. 정책 프로필은 선택한 target의 제약을 제공한다. 공개 HTTP 1개와 비공개 worker/DB를 검사하되, 이것으로 실제 네트워크가 검증됐다고 표시하지 않는다.

### 6.3 변환별 Patch Gate

| ID | 검사 | 실패 예시 |
| --- | --- | --- |
| P-001 | diff를 적용한 결과와 source/manifest/hash 일치 | 기대값을 함께 바꿔 제출해도 실제 바이트 불일치 |
| P-002 | Plan에서 필요한 transform과 변경 집합 대응 | 필요 없는 새 파일·수정 |
| P-003 | Prisma 변환 명세 충족 | 필드·관계·index·default 변경 |
| P-004 | storage 부품·호출 변환 명세 충족 | 인증·라우트·응답·순서 변경 |
| P-005 | package·lock·generator의 승인 조합 | 새 실행 hook, 미승인 dependency·generator |
| P-006 | 지원 구문에서 금지 실행 능력 검사 | child_process import 또는 금지 전역 호출 |

**DB-T01: SQLite → PostgreSQL**

- 사전조건: 단일 지원 datasource, 승인 generator, 지원 Prisma 문법, 승인 의존성 조합.
- 허용: datasource provider와 프로필에 명시된 접속 설정 변환.
- 보존: 모델·필드·타입·관계·index·unique·default·매핑·generator 설정.
- 사후조건: parser가 양쪽 구조를 정상 해석하고 허용 구문을 제외한 구조가 일치. 승인된 Prisma 도구로 대상 스키마 유효성 확인.
- provider별 특수 타입이나 지원하지 않는 속성은 UNSUPPORTED. 주석을 제거한 문자열 치환으로 안전성을 주장하지 않음.
- 이미 PostgreSQL인 입력의 no-op 지원은 별도 사전조건으로 정의. 기존 공유 Plan 계약이 표현할 수 없다면 B와 계약을 맞출 때까지 자동 허용하지 않음.
- 기존 migrations가 있거나 기존 데이터 이전이 필요한 경우 별도 지원 없이 진행하지 않음. 구조 보존과 실제 데이터 변환의 안전성은 다름.

**ST-T01: 파일 저장 → 승인 storage 부품**

- 사전조건: 승인된 저장 경로와 호출 형태, 명확한 함수 binding, 승인된 storage API.
- 허용: 고정 부품 추가, 해당 import, 명세에 있는 쓰기/읽기/삭제 호출 변환.
- 명세는 인자·반환값·await 필요성·예외 전파·호출 순서·응답 시점을 포함한다.
- 단순히 함수 전체를 “수정 가능 영역”으로 비워두지 않는다. 정적 원본의 특정 구문을 기준으로 허용할 변경 모양을 제한한다.
- 승인된 변환으로 얻을 수 있는 구조와 Agent 결과 구조를 비교한다. 라우트·인증·입력 검증·추가 네트워크 호출·임의 업무 로직은 보존한다.
- 동기 호출을 비동기로 바꿀 때 필요한 async 전파 범위가 명세 밖이면 UNSUPPORTED.
- 독립된 정상/위반 테스트를 사용한다. 생성기와 검사기가 같은 함수를 호출해 같은 실수를 공유하는 검증만으로 완료하지 않는다.

**구문 검사 원칙**

JS parser의 버전·ECMAScript 범위·module mode·정규화 옵션을 고정한다. 위치·일반 주석은 비교에서 제외할 수 있지만 문자열 값, 연산자, 호출 순서, directive(예: strict mode), 의미 있는 import는 보존한다. 이름이 같은 지역 함수와 전역 실행 능력을 구분할 수 있는 지원 범위를 명시한다. alias나 computed 접근을 해석하지 못하면 위험하지 않다고 추측하지 않는다.

Prisma parser 후보는 대표 문법·미지원 문법·구조 비교 검증을 거쳐 선정한다. 선정 전에는 지원 범위를 확대하지 않는다. 파서 자체는 대상 레포에서 가져오지 않고 도구 의존성으로 고정한다.

### 6.4 의존성과 빌드 실행

기존 lock 전체 고정을 당장 해제하지 않는다. 새 프로필에 필요한 승인 dependency 세트를 등록한다. 직접 의존성 이름만 보지 않고 lock의 전이 의존성·출처·integrity·플랫폼 조건도 포함한다. 로컬 파일·Git·임의 URL dependency는 초기 프로필에서 지원하지 않는다.

현재 빌드는 `npm ci --ignore-scripts`를 사용한다. 이 옵션은 npm lifecycle script 실행을 줄이는 통제이며, 직접 실행하는 Prisma 도구까지 실행하지 않는다는 뜻은 아니다. 명시적 script 실행 명령의 예외도 npm 문서에 설명돼 있다. 따라서 generator·도구 binary·빌드 명령·dependency 세트를 별도로 승인한다. [npm ci 공식 문서](https://docs.npmjs.com/cli/v11/commands/npm-ci/)

이미지 기본 태그만 고정하지 않고 검토한 base image digest와 template digest를 빌드 입력에 기록한다. 이것만으로 운영체제 패키지까지 bit-for-bit 재현 가능해지는 것은 아니므로 실제 결과 이미지 digest도 기록한다. 의존성 취약점 스캔·완전한 재현 빌드는 별도의 보장 항목이다.

## 7. 정책 결과와 실행 결과물을 결합

### 7.1 내부 결과 계약

`PolicyResult`는 E 내부 sidecar로 시작한다. 기존 Intent·Plan·BuildArtifact에 합의 없이 필드를 추가하지 않는다.

필수 필드:

- `result_schema_version`, `decision`, `complete`, `stage`
- `deployment_id`, `attempt_id`, `policy_run_id`
- `policy_digest`, `profile_digest`, `parser_version`, `validator_version`
- `input_binding`: snapshot/Intent/Plan/patch digest와 해당 target
- `required_rules`, `evaluated_rules`, `not_applicable_rules`
- `findings`: rule_id, reason_code, 안전한 claim 경로·소스 위치
- `retry_hint`, 검사 시간·입력 크기

단계별 PASS는 해당 단계에서만 유효하다. Intent PASS로 Patch Gate나 실행 검증을 생략할 수 없다. 출력 생성 실패·신뢰 상태 저장 실패 시 승인 결과를 외부에 먼저 발행하지 않는다.

### 7.2 세 종류의 식별자

1. **검사 식별자:** 입력 digest + 정책·프로필·검사기 버전 + target + 승인된 publish 설정.
2. **빌드 식별자:** patched source + dependency 세트 + template/base image digest + platform 등 실제 이미지 입력.
3. **실행 식별자:** 이미지 ID/registry digest + Plan + 적용 정책 결과 + 배포 attempt.

동일 commit 문자열은 이 셋을 대신하지 않는다. Local과 AWS가 같은 patched source와 빌드 입력을 사용하면 한 이미지를 공유하되 Plan 검사는 target별로 수행한다. 정책만 바뀌면 재검증은 필요하지만 코드·빌드 입력이 같다면 재빌드가 반드시 필요한 것은 아니다.

현재 `app:<revision>` 계약은 유지하면서 다른 빌드 입력과 충돌하면 안전하게 거부한다. 같은 commit의 복수 빌드를 지원하려면 B/A와 이미지 식별 계약을 변경한다. 검증 후 이미지 태그를 다시 해석해 다른 이미지로 바뀌지 않도록 실행 단계에는 실제 digest/ID를 연결한다. 이미지 label은 신뢰되지 않은 이미지의 진위를 보장하는 서명이 아니다.

### 7.3 재배포·정책 변경·롤백

- AI 분석을 생략해도 현재 snapshot의 필수 정책 검사를 유지한다.
- 프로필/정책/파서/검사기 변경은 해당 검사 캐시를 무효화한다.
- 작업 시작 시 정책 버전을 고정하고 실행 승인 직전에 폐기 여부를 확인한다. 긴급 폐기된 정책은 새 실행을 허가하지 않는다.
- 이미 실행 중인 앱을 정책 변경만으로 자동 삭제하지 않는다. 영향 표시와 재배포/격리 여부는 운영 절차로 처리한다.
- 롤백도 새 실행 요청이다. 과거 PASS만으로 허용하지 않고 보관된 원본·패치·이미지와 현재 허용 정책을 확인한다. 재검증 불가/폐기 상태이면 중단하고 복구 판단을 요청한다.
- 정책 차단을 우회하는 긴급 예외 버튼은 MVP에서 만들지 않는다. 예외가 필요하면 별도 리뷰된 정책 버전을 배포한다.

## 8. 검사기의 실행 제한과 원자성

필수 Gate는 repo 프로그램·repo parser plugin·npm script를 실행하지 않는다. AST 검사는 도구에 포함된 parser로 수행한다. 빌드에서 필요한 승인 도구 실행은 다음 단계의 별도 권한이다.

프로필은 파일 수·개별 크기·총 크기·파서 깊이·오류 개수·프로세스 메모리/시간·전체 검사 deadline을 포함한다. 파일마다 timeout을 둬서 전체 시간은 무제한이 되는 형태를 피한다. 현재 2,000파일/4MiB 개별/20MiB tree 제한을 기준으로 시작하되 전체 deadline과 파서 한도는 실측 후 수치로 고정한다.

검증 상태는 임시 기록 후 원자적으로 확정하고, 해당 앱·작업의 기존 잠금과 연결한다. 일부 규칙 결과만 저장된 상태에서 PASS를 읽을 수 없어야 한다. 모든 오류 응답은 원시 소스·secret·호스트 경로를 노출하지 않는다.

## 9. 정적 검사 이후의 실행 검증

| 검사 | 책임 | 통과해도 보장하지 않는 것 |
| --- | --- | --- |
| 공개 web 1개, worker/DB 비공개 Plan | E Gate | 실제 인프라 포트가 그대로 적용됨 |
| Compose/실제 포트, 공개 HTTPS, 저장 데이터 확인 | E Local Adapter | 모든 URL·모든 외부 통신의 안전성 |
| AWS 네트워크·IAM·health·적용 결과 | A Adapter | 정적 Gate만으로 실 AWS 검증 완료 |
| DB 적용 전 위험 확인·복구 가능성 | E/A 실행 절차와 팀 계약 | 모델 구조 보존만으로 DB 무손실 |

현재 Local은 Prisma schema 적용을 수행하므로 이전 개발자 commit→현재 원본의 변경도 데이터에 영향을 줄 수 있다. 이번 Patch Gate는 현재 원본→AI 패치만 제한한다. destructive migration을 자동 승인하지 않고 데이터 이전·삭제를 새 지원 범위로 추가하려면 별도 검증과 복구 계획이 필요하다.

## 10. 정책 결과 UI — 이번 구현의 필수 범위

### 10.1 최신 main에서 재사용할 부분

`control_plane/web/src/App.jsx`에는 정책 타임라인, `eventDetail`, `PatchCard`의 검증 표시가 이미 있다. 전체 화면·스타일을 재설계하지 않고 배포 상세 화면에 Policy 요약과 규칙별 상세를 연결한다. `api.js`, D의 결과 조회·저장, E 결과 발행 경로만 필요한 범위에서 변경한다.

### 10.2 표시 항목

| 영역 | 표시 |
| --- | --- |
| 요약 | 검사 전 / 검사 중 / 통과 / 차단 / 미지원 / 검사 오류 |
| 검사 구분 | Intent / Plan / Patch / 빌드 전 재검사 |
| 판정 근거 | rule_id, 고정 사유 설명, 안전한 파일·줄 위치 |
| 수정 안내 | 규칙 카탈로그에 정의한 다음 조치 |
| 범위 | target, attempt, source revision, 정책 버전 |
| 실행 상태 | 정책 통과와 실제 배포·공개 URL 확인을 별도 표시 |

`BLOCK`을 악성 레포 판정으로 표시하지 않는다. 없는 결과는 미검사로 보여주며 과거 성공으로 채우지 않는다. 일부 검사만 통과하면 전체 통과 배지를 표시하지 않는다. 재시도는 이전 결과와 현재 결과를 구분하며 새로고침 후에도 유지한다.

### 10.3 데이터 계약과 전달

- E는 7절의 구조화 결과를 신뢰된 작업 상태에 저장한다. 성공뿐 아니라 BLOCK/UNSUPPORTED/ERROR도 저장한다.
- D는 deployment/target/stage/attempt로 조회할 수 있게 한다. 제안 API는 `GET /api/deployments/{id}/policy`이며 결과 목록과 현재 attempt를 반환한다. 기존 `/patch` 계약은 유지한다.
- 기존 DeployEvent/SSE는 결과 갱신 알림에 사용하고, UI는 알림 후 저장된 상세 결과를 다시 조회한다. 이벤트 문자열을 파싱해서만 전체 정책 상태를 추측하지 않는다.
- 최초 분석 실패도 deployment ID에 연결해 조회 가능해야 한다. 패치 생성 전 실패했다고 결과를 누락하지 않는다.
- UI는 등록된 reason_code를 고정 한국어 문구로 매핑한다. 미등록 코드는 일반 실패 안내와 안전한 rule ID로 표시한다.
- 소스·secret·HTML을 그대로 렌더링하지 않는다. 파일명은 텍스트로 표시하며 임의 파일 열기/API 경로로 사용하지 않는다.
- D의 기존 접근 통제와 터널 공개 제한을 유지한다. 상세 정책 API를 공개 앱이나 GitHub webhook용 터널에 노출하지 않는다.

### 10.4 UI 수용 기준

- [x] 정상 입력의 Intent/Patch 정책 통과 확인. — `test_analysis_records_real_intent_and_plan_checks`, redteam 정상 대조군 allow.
- [x] 공격/위반 입력의 규칙 ID·차단 이유·근거 위치·수정 안내 확인. — `policy-card.test.mjs` "failure shows rule, evidence and remedy".
- [x] UNSUPPORTED와 ERROR를 BLOCK과 다른 문구로 표시. — 같은 테스트 "unsupported and error are separate from blocked"(미지원·검사 오류·검사 미완료).
- [x] 단계별 일부 통과, 누락 결과, 분석 초기 실패를 정확하게 표시. — `test_initial_source_failure_is_recorded_without_patch`, `test_unknown_and_not_checked_are_distinct`, "missing results never imply success".
- [x] Local/AWS target과 최초/재시도 결과를 혼동하지 않음. — `test_target_attempt_and_failure_survive_reopen`, "retry and targets remain distinct".
- [x] 새로고침·SSE 재연결 후 같은 결과 유지. — 결과는 `policy_results` 테이블에 보관하고 UI가 다시 조회한다. 현재 구현은 SSE 알림 기반이 아니라 1.5초 주기 재조회이며, 조회 실패 시 과거 성공을 남기지 않는다("failed refresh does not render stale success").
- [x] 표시된 정책 통과와 실제 빌드/배포 상태가 서로 다를 수 있음을 표현. — 카드 상단에 "정책 통과와 실제 배포·공개 접속 확인은 별도 결과입니다" 고정 표기.
- [x] 조작된 파일명·사유가 실행되거나 민감 내용을 노출하지 않음. — "source-controlled text is escaped", `test_error_details_are_not_reflected`, 미등록 코드는 `policy_check_failed`로만 표시.

## 10A. redteam-repo 확장과 저장소 간 순서

redteam 작업은 `feat/e-policy-fixtures`에서 계속한다. 해당 저장소는 비실행 fixture·기대 결과·정합성 검사만 담당하며 Gate 구현과 UI는 inframorph에 둔다.

1. 기존 25개 사례와 manifest 형식을 먼저 확인하고 정상 사례와 최소 차이의 위반 사례를 추가한다.
2. worker 실존/근거 불일치, DB provider 불일치, config/secret 충돌, Prisma 필드·index 삭제, 무관한 JS 변경, 주석 오탐 사례를 우선한다.
3. 신규 case ID마다 적용 규칙, 기대 판정·reason_code, 변경 원본, 검증 소유자를 기록한다. 기존 allow/reject 표현과 새 판정 계약의 매핑을 runner에서 명시한다.
4. 기존 검사기에서 미구현인 규칙은 통과한 것처럼 기록하지 않는다. fixture 정합성 통과와 실제 Gate 기대 판정 충족을 분리한다.
5. source 변조·과거 승인·UI 재연결은 실제 실행 경계가 필요한 inframorph 테스트로 검증한다. 억지로 정적 diff 사례로 대체하지 않는다.
6. redteam commit이 준비되면 inframorph의 `validation/redteam-source.json`을 정확한 SHA로 갱신하고 전체 실제 Gate 검증을 실행한다.
7. 새 결과 수를 반영하되 기존 25개가 빠지지 않았는지 확인한다. 두 저장소 PR은 연결하고 검증 가능한 redteam SHA를 먼저 제공한다.

구현 시 두 저장소에 실제 변경이 생기면 각각 커밋·push하고 PR을 연결한다. 이번 계획 수정 단계에서는 fixture·pin·기대 결과를 변경하지 않는다.

## 11. 테스트와 수용 기준

### 11.1 필수 평가 묶음

| 묶음 | 정상/위반/미지원 사례 |
| --- | --- |
| 근거 | 실존 관련 구문 / 무관한 줄 / 동적 분석 불가 |
| 누락 | 지원 worker·DB 선언 / 확인된 요구 누락 / 범위 밖 동적 로딩 |
| 설정 | 승인 config / secret·reserved 충돌 / 미지원 정상 config |
| Prisma | provider 변환 / field·index 삭제 / provider 특수 기능 |
| JS | 승인 호출 변환 / 인증·응답 순서 변경 / async 전파 미지원 |
| 주석 | 의미 없는 주석 변경 / 실행 구문 추가 / 해석 불가 구문 |
| 의존성 | 승인 조합 / 새 hook·generator / 미등록 정상 조합 |
| 동일성 | 검사한 바이트 / 검사 후 변조 / 잘못된 이미지 tag 재사용 |
| 정책 | 현재 정책 / 폐기된 PASS / 필수 rule 누락 |
| 오류 | 정상 검사 / parser crash·timeout / 부분 결과 저장 |
| 동시 실행 | 단일 실행 / 같은 앱의 충돌 / 취소 후 상태 정리 |
| UI | 정상/차단/미지원/오류 / 과거 attempt / 누락 결과 / 조작된 사유 |

단일 규칙 정상/위반 쌍 외에 복합 위반, 난수·경계 입력, 실제 C 생성 결과를 포함한다. 규칙 판정을 의도적으로 약화했을 때 테스트가 실패하는지도 확인해 검사기와 테스트가 같은 착오를 공유하지 않게 한다.

기존 25개 redteam은 회귀 기준으로 유지한다. 신규 자료는 변경을 반영한 새 검토 SHA로 pin을 갱신한다. 기존 기대값을 바꿔 통과시키지 않고 규칙 변경 사유와 별도 평가 결과를 남긴다.

실제 결과: 기존 25개를 그대로 유지한 채 14개를 추가해 39개가 됐고(prompt 4·intent 17·patch 14·path 4), `validation/redteam-source.json`의 pin을 `48e27852`로 갱신했다. 기존 25개의 기대값은 변경하지 않았다.

### 11.2 MVP 수용 기준

- [x] 지원 프로필·parser·transform 버전 고정. — 정책 `VERSION=2.2.0`·`profile=reviewed-node22`를 모든 결과에 기록. parser는 `policy_gate/vendor/acorn.cjs`를 고정 포함하고 `ecmaVersion: 2022`로 호출한다.
- [x] 필수 보안 회귀 집합에서 잘못된 허용 0건. — redteam 39개에서 failed 0. 유한 표본이며 실제 공격 성공률 0%를 뜻하지 않는다.
- [x] 지원한다고 선언한 정상 사례 전부 통과. — 정상 대조군 전부 allow, 실제 Docker·공개 HTTPS 배포까지 통과.
- [x] 미지원 정상 사례를 정책 위반과 구분. BLOCK도 악성 의도를 확정하는 표시는 아님. — `config_not_supported`·`unreviewed_runtime_source`·`*unsupported*`는 UNSUPPORTED(`complete=false`)로 분리.
- [x] BLOCK/UNSUPPORTED/ERROR 이후 다음 실행 단계 호출 0회. — `test_consistent_but_forbidden_patch_stops_before_builder_and_adapter`, `test_rejected_patch_cannot_build`.
- [~] 위변조·과거 PASS·누락 규칙으로 실행 불가. — 위변조는 `test_source_and_diff_tampering_after_gate_are_blocked`, 잘못된 승인은 `test_wrong_intent_approval_blocks_all_later_stages`로 차단. **누락 규칙의 `required_rules`/`evaluated_rules` 기록과 폐기된 정책의 과거 PASS 차단은 미구현이다.** 현재는 `complete` 플래그만 사용한다.
- [~] v1/v2와 외부 예제 1~2개에서 Gate뿐 아니라 patch→build→Local 검증. — v1/v2는 실제 빌드·배포·롤백까지 검증했다. **외부 예제는 미검증이다.**
- [x] 실제 모델 평가와 replay/호스트 경계 평가 별도 보고. — redteam 결과에 `live_model_behavior=not_measured`, `mode=adversarial_replay`를 사례별로 기록.
- [ ] 전체 검사 시간·메모리·오탐·미지원 비율 기록. — 미측정. parser 상한만 고정했다(`--max-old-space-size=192`, timeout 20초, 입력 24MiB).
- [x] 정책 결과가 누락돼도 기존 D 소비자가 성공으로 해석하지 않음. — `test_unknown_and_not_checked_are_distinct`, "missing results never imply success".
- [x] 정책 요약·규칙별 상세가 실제 결과와 일치하고 새로고침 후 유지. — 저장된 payload를 그대로 조회해 표시한다.

표기: `[x]` 달성, `[~]` 일부 달성(미달 부분 명시), `[ ]` 미달성.

잘못된 허용과 잘못된 차단은 지원 범위 내부의 표본으로 계산하고 분모를 기록한다. UNSUPPORTED 비율과 ERROR 비율은 별도 보고한다. 모든 입력을 UNSUPPORTED로 돌려 보안 점수가 좋아 보이는 것을 막는다. 유한 테스트의 0건은 실제 공격 성공률 0%라는 주장이 아니다.

## 12. 구현 단계·담당·완료 산출물

| 단계 | E 작업 | 협업 의존성 | 완료 조건 |
| --- | --- | --- | --- |
| 0 | 결과 계약·rule catalog·profile manifest·실패 테스트 | B/C/D와 필드·사유 코드 합의 | 계약과 테스트 기대값 리뷰 가능 |
| 1 | 사실 추출, config, Intent–Plan 검사 | B의 추출·mapping 계약, C의 근거 | 실제 정상 Intent 통과·누락/모순 차단 |
| 2 | Prisma DB-T01, generator/dependency 검사 | C의 patch 산출물 | 무관한 구조 변경 차단, 정상 빌드 |
| 3 | JS ST-T01, AST 제한 변환 | C 패처의 지원 형태 | 정상 변환·업무 로직 변경·미지원 구분 |
| 4 | 검사/빌드/실행 식별자·캐시·rollback 정책 | B/A artifact 계약, D 상태 관리 | stale 승인·이미지 바꿔치기 시험 차단 |
| 5 | 신규 프로필과 외부 예제, redteam 갱신 | B/C/A의 실제 capability | 전체 경로 통과·지원 범위 문서화 |
| 6 | 정책 상세 API·기존 UI 확장 | D 저장/API/SSE 계약 | 원인·다음 조치·미완료·재시도 표시 |
| 7 | redteam 고정 SHA·전체 흐름 시연 | 두 저장소 CI·C 실제 결과 | 정상 배포 및 위반 차단이 UI까지 일치 |

### 12.1 과도한 구현을 피하기 위한 최소 단위

첫 PR은 **결과 계약 + I-002/I-003/I-006 + P-003의 정상/위반 테스트**로 제한한다. 기존 CLI·공유 모델을 유지하고 구조화된 결과를 E 내부에 보관한다. 프로필은 우선 정적 파일과 모듈별 지원 상수로 구현하며 별도 정책 서버·프로필 서비스를 만들지 않는다. 정책 결과 API와 UI용 데이터 모델은 첫 단계에 정의하고 실제 화면 연결은 6단계에 완료한다.

공통 보안 규칙을 감사 로그만 남기는 모드로 낮추지 않는다. 신규 규칙은 기존 차단을 유지한 비실행 평가에서 비교하고, 정상 사례가 검증된 프로필에 순차 적용한다. 정책 UI가 준비되지 않아도 안전한 고정 오류 코드로 중단할 수 있어야 한다.

### 12.2 검토 중 남겨둔 트레이드오프

| 선택 | 얻는 것 | 비용·완화 |
| --- | --- | --- |
| 제한된 변환만 지원 | 판정 기준을 검증하기 쉬움 | 미지원 정상 앱 증가. 지원 비율을 함께 공개 |
| 정책 변경 시 과거 승인 재검증 | 오래된 승인 우회 방지 | 복구가 지연될 수 있음. 배포 전 유효한 복구 후보를 미리 확인 |
| 원본 승인과 패치 검증 분리 | 임의 원본까지 안전하다는 오해 방지 | 외부 예제 등록 작업 필요. E 단독 책임으로 표현하지 않음 |
| 고정된 정책 설명 사용 | 실제 판정과 문구의 일관성 | 새로운 규칙의 문구를 함께 관리해야 함 |

특히 롤백 중 정책 차단이 발생해도 현재 실행 자원을 먼저 제거하지 않는다. 정책상 허용되는 후보와 실제 가용성을 별도로 기록한다. 이미 실패한 새 버전의 정리와 기존 버전의 복원은 Adapter 책임이며, 데이터베이스를 자동으로 과거 상태로 되돌린다는 보장은 없다.

각 단계는 좁은 PR로 진행한다. UI는 E 결과를 보여주는 최소 범위로 D와 연결한다. 기존 데모 프로필을 먼저 끄지 않으며 신규 프로필이 실패하면 자동으로 느슨한 정책으로 내려가지 않는다.

## 13. 점수표와 남은 결정

이전 문서의 90점은 개념 방향 중심 평가였다. 이번에는 현재 코드와 접점을 포함한 **구현 준비도** 기준으로 재평가했다. 점수는 설계 리뷰 판단이며 보안 정확도·테스트 통과율이 아니다.

| 항목 | 배점 | 이전 설계 재평가 | 수정 설계 |
| --- | --- | --- | --- |
| 주제 적합성·책임 경계 | 15 | 14 | 14 |
| 신뢰·공격·오류 모델 | 15 | 11 | 14 |
| 근거·Plan·지원 범위 명세 | 15 | 12 | 14 |
| 제한 변환·오탐 통제 | 20 | 17 | 18 |
| 승인·artifact·버전·복구 | 15 | 12 | 14 |
| 평가 집합·UI 검증 | 10 | 8 | 9 |
| 단계별 구현·협업 가능성 | 10 | 8 | 9 |
| **합계** | **100** | **82** | **92** |

이 문서 변경으로 현재 구현 점수가 올라가지는 않는다. 기존 정책 깊이 평가 65점과 E 연결 평가 90점은 각각 다른 기준이며 이번에 코드를 재검증해 갱신하지 않았다.

남은 8점은 아래 구현 전 결정과 실증이 필요하기 때문이다.

- [x] JS/Prisma parser를 대표 사례로 비교하고 버전·옵션 확정. — acorn을 저장소에 고정하고 `ecmaVersion: 2022`로 확정. Prisma는 무손실 토큰화로 비교하며 generator를 실행하지 않는다. parser 사용 불가 시 `javascript_parser_unavailable` ERROR로 중단한다(통과로 처리하지 않음).
- [ ] storage 비동기 변환의 정확한 사전/사후조건을 C와 확정. — `patch_behavior_changed`로 허용 파일 내부 변경을 차단하는 구현은 넣었으나 C와의 합의는 남았다.
- [ ] B/A/D와 profile·artifact·정책 결과 소비 계약 확정. — `GET /api/deployments/{id}/policy`를 제공했을 뿐 소비 계약은 합의 전이다.
- [ ] 전체 deadline·파서 자원 제한 수치를 실측 후 확정. — 상한값만 지정했고 실측은 하지 않았다.
- [ ] 외부 예제와 별도 평가 집합에서 오탐·미지원 비율 측정.

이 결정들이 끝나기 전에는 데모 프로필 밖의 자동 실행을 일반적으로 허용하지 않는다. 개별 규칙·결과 계약·회귀 테스트부터 구현을 시작하는 것은 가능하다.

## 14. 발표·문서에서 사용할 완료 표현

“지원하는 Node·Prisma 앱에서 AI가 만든 요구사항과 환경 전환 패치를 검사한다. 승인된 DB·저장 변환은 배포되고, 근거 불일치·무관한 모델 삭제·업무 로직 변경은 규칙 ID와 함께 실행 전에 중단된다. 정책 화면은 어떤 규칙에서 왜 중단됐는지 보여주며 실행 권한은 결정론적 검사가 통제한다.”

위 표현은 지원 프로필(reviewed-node22)과 v1/v2 데모 범위에서 사용할 수 있다. 신규 규칙과 정책 상세 UI는 구현·검증을 마쳤고 redteam 39개에서 잘못된 허용이 없었다.

다만 아래는 함께 밝힌다. 외부 예제, 오탐·미지원 비율, 실제 모델 행동은 측정하지 않았다. 문자열·구문 기반 검사는 모든 공격 탐지를 보장하지 않으며, BLOCK은 규칙 위반 표시이고 악성 의도의 확정이 아니다. "정책 통과"는 배포 성공과 별개로 표시한다.
