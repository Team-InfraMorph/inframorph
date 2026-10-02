# E 인계: 공개 범위·차단 사례·연결 계약

## 공개 범위

- 지원 앱: 승인된 Node 22 / Express / Prisma demo-app v1·v2.
- public HTTP 서비스는 하나만 허용합니다. `--publish`일 때 cloudflared가 그 서비스의 내부 포트에 연결합니다.
- web의 호스트 포트는 `127.0.0.1`에만 바인딩합니다. DB·worker·schema·tunnel에는 호스트 공개 포트를 만들지 않습니다.
- tunnel은 web의 **모든 HTTP 경로**를 전달합니다. 인증·경로별 접근 제어를 제공하는 프록시라고 주장하지 않습니다. `/`, `/health`, `/api/notes`, `/api/images`는 공개 데모 기능입니다.
- `/.env`, `/.git/config`, `/package.json`, `/prisma/schema.prisma`는 실제 앱에서 403/404인지 확인합니다. 이 네 사례가 모든 민감 경로 검증을 대신하지는 않습니다.
- 첫 배포 실패 시 해당 Compose 프로젝트를 `down --remove-orphans`하여 앱·터널을 종료합니다. DB/업로드 named volume은 보존합니다. 정리 실패는 `initial_deployment_cleanup_failed`로 별도 보고하며 성공으로 처리하지 않습니다.
- 외부 노출 검증은 실제 Docker의 PortBindings와 공개 HTTPS 응답을 대조합니다. 타인의 서버나 임의 네트워크를 스캔하지 않습니다.

## 재현 가능한 차단 사례

검증 corpus revision: `bece81ed034824aed5a6749d64f7e4316222a2e0` (redteam PR #7).

실행: `.venv/bin/python scripts/verify_e_redteam.py --corpus ../redteam-repo --output .local/redteam.json`

| 입력 fixture | 정상 대조군 | 실제 기대 결과·오류 코드 | 검사 의미 |
|---|---|---|---|
| intent-missing-file | intent-control | reject / evidence_file_missing | 없는 파일 근거를 통과시키지 않음 |
| intent-missing-line | intent-control | reject / evidence_line_missing | 파일 범위를 벗어난 줄 번호 차단 |
| intent-public-worker | intent-control | reject / schema_invalid | worker 공개 금지 |
| patch-outside-allowlist | patch-storage-control | reject / patch_allowlist_violation | 허용되지 않은 파일 변경 차단 |
| patch-child-process, patch-eval | patch-storage-control | reject / forbidden_code_pattern | 합의한 실행 패턴 차단 |
| patch-syntax | patch-storage-control | reject / javascript_syntax_invalid | JS 구문 오류 차단 |
| patch-symlink | patch-storage-control | reject / symlink_forbidden | 링크 파일을 읽기 전 거부 |

17개는 정상·공격을 합친 Intent/Patch corpus입니다. Prompt/tool 8개는 C 연결 전이며 통과로 세지 않습니다. corpus의 추가 파일 허용 목록은 테스트 전용이며 운영 Builder에 적용하지 않습니다. baseline 파일 해시를 확인하고, fixture revision은 baseline digest에서 만든 합성 식별자입니다. 실제 Git commit을 검증한 것처럼 표시하지 않습니다.

운영 프로필은 `DEFAULT_PATHS`, C 번들 해시·원본 비교, 승인 lockfile 검증을 별도로 적용합니다. 위험 소스는 실행하지 않고 JS 구문만 검사합니다. Prisma는 승인 generator를 확인한 후 격리된 이미지 빌드에서 validate/generate합니다. 문자열 검사는 난독화된 악성 코드 전체를 탐지하는 샌드박스가 아닙니다.

## B/C/D/A 연결 계약

| 담당 | E에 전달할 것 / E가 반환할 것 | 연결 전 조건 |
|---|---|---|
| B | 동결 snapshot, full SHA, Plan | snapshot이 실제 SHA의 코드인지 Mapper가 보장. evidence는 `파일:양의 줄 번호`만 지원 |
| C → Intent Gate | Intent + 원본 snapshot + SHA | unknowns는 실패. 필드 참조 표기 대신 실제 소스 줄 근거 사용 |
| C → Patch Gate | `source/`, `patch.diff`, `manifest.json` | manifest schema 1.0.0, 동일 SHA·target·해시·변경 목록. Gate 통과 전 Builder 금지 |
| Builder → A | B BuildArtifact (`source_revision`, `target`, `image`, `platform`) | 동일 Docker daemon, linux/amd64, `app:<full SHA>`. 이미지 label에 patched_digest와 템플릿 digest 기록. A가 ECR push와 AWS 배포 담당 |
| D → Local | Plan + BuildArtifact + 전용 state 디렉터리 | state 이름은 Plan.app. 대상별 동시 실행 잠금. DATABASE_URL은 E가 관리 |
| E → D/C | DeployEvent JSONL, 고정 실패 코드 | 앱 원시 로그·비밀값은 전달하지 않음. C 자동 수정 루프/사용자 표시 연결은 담당자와 별도 검증 |

현재 B 계약과 C 변환은 Local도 PostgreSQL을 사용합니다. 따라서 동일 patched source의 단일 이미지를 Local/AWS에 전달합니다. 대상별 패치 내용이 달라지면 같은 SHA 태그를 덮어쓰지 않고 image_tag_collision으로 중단합니다. 최신 B/C 계약이 바뀌면 함께 다시 검토해야 합니다.

## 연결 전 실행 체크리스트

- [x] 실제 v1 HTTPS·민감 경로 4개 404·최초 실패 정리·공개 접근 종료·볼륨 보존
- [x] Intent·Patch 게이트, Builder·Local 단위/회귀 검사
- [x] 원본 → 실제 C 번들 → Gate → AMD64 이미지 → Local 검증
- [x] v1/v2 데이터 유지, worker, 이전 이미지 롤백, 실패 복구 검증
- [ ] 다른 네트워크의 실제 휴대폰으로 v1 공개 URL에서 메모·사진 저장/조회
- [ ] D의 UI/실패 피드백과 실제 순서 연결
- [ ] A의 AWS URL과 합동 검증

실제 휴대폰 검증은 `deploy --publish`로 데모를 유지한 상태에서 수행하고, 기종·네트워크·SHA·시각·기능 결과를 이슈 #15에 기록합니다. 자동 통합 스크립트는 배포와 롤백을 진행하므로 URL이 바뀌며, 이를 휴대폰 검증 기록으로 대체하지 않습니다.

## 실제 최초 실패·공개 경계 검사

```sh
.venv/bin/python scripts/verify_e_boundaries.py \
  --plan schemas/fixtures/v1/plan.local.json \
  --artifact BUILD.json \
  --state-dir .local/runtime/e-boundary-demo \
  --output .local/boundary-NEW
```

전용 앱 이름과 새 output을 사용합니다. v1을 공개하고 메모·사진·민감 경로·실제 포트 바인딩을 확인한 뒤 실패를 주입합니다. 컨테이너 제거, 공개 health 접근 종료, 볼륨 보존을 확인하고 result.json을 기록합니다. 실제 휴대폰 접속 여부는 false로 명시합니다.
