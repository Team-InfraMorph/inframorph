# Code Patch Agent

- 담당: C · 리뷰: E
- 기획서 구간: 7 · 8 · 11 · 이슈: #8

`RepoMap`과 검증된 `Plan`을 받아 **새 복사본**에 저장 모듈을 적용한다. 현재는 공개
`demo-app` v1·v2의 검토된 소스 형태를 지원하는 결정적 템플릿 구현이다. 모델, 셸,
패키지 설치, 네트워크 호출은 패치 함수 안에서 실행하지 않는다. 일반 레포용 코드 생성은
아직 지원하지 않으며, 지원하지 않는 소스는 추측해서 수정하지 않고 오류로 반환한다.

## 실행

레포 루트에서 실행한다. POSIX(macOS/Linux), Python 3.12 및 공통 `requirements.txt` 환경을 사용한다.
출력의 부모 디렉터리는 미리 존재해야 하고, 출력 디렉터리는 새 경로여야 한다.

```sh
mkdir -p .local/code-patch
.venv/bin/python -m code_patch \
  --snapshot tests/fixtures/analyzer/v1/snapshot \
  --repo-map tests/fixtures/analyzer/v1/repo_map.json \
  --plan schemas/fixtures/v1/plan.local.json \
  --output-dir .local/code-patch/v1
```

`v2`로 바꾸면 worker가 포함된 복사본을 만든다. `plan.aws.json`으로 바꾸어도
**패치된 소스와 lockfile은 동일**하다. 런타임의 `STORAGE_DRIVER`로 fs/S3를 선택한다.
현재 예제 Plan은 Planner의 실제 출력이 아닌 공통 계약 fixture다.

출력은 다음과 같다.

- `source/`: 패치한 소스. 원본 경로는 쓰지 않는다.
- `patch.diff`: 원본 대비 unified diff. 신규 파일과 마지막 개행이 없는 파일도 표현한다.
- `manifest.json`: source revision, 원본/결과/차이 해시, 변경 경로와 파일별 해시,
  제외 경로, `patched`/`unchanged` 상태, `requires_policy_gate: true`.

동일한 패치를 다시 적용하면 변경이 없고 빈 diff가 나온다. 실패 시 부분 결과를
게시하지 않는다. Manifest는 C 모듈의 초기 인터페이스이며 공통 `BuildArtifact`를
대체하지 않는다. Plan·RepoMap의 revision이 같아야 하고 실제 커밋의 불변 스냅샷을
만들고 인증하는 책임은 Repo Mapper에 있다.

```python
from pathlib import Path
from code_patch import patch_snapshot

manifest = patch_snapshot(Path("snapshot"), repo_map, plan, Path("new-result"))
# E의 Policy Gate가 original_digest/patched_digest/diff_sha256을 검증한 뒤
# 검증한 바로 그 결과를 Builder에 전달한다. 여기서는 빌드를 시작하지 않는다.
```

## 수정 범위와 의존성

| 파일 | 허용하는 변경 |
| --- | --- |
| `prisma/schema.prisma` | 검토된 SQLite provider를 PostgreSQL로 변경 |
| `src/images.js` | 기존 saveImage/readImage API를 storage 모듈에 연결 |
| `src/storage.js` | fs/S3 구현 템플릿 추가 |
| `package.json` | `@aws-sdk/client-s3`의 고정 버전 추가 |
| `package-lock.json` | 검토한 의존성 그래프에 해당하는 고정 lockfile 적용 |

`src/server.js`·worker·HTTP 라우트는 그대로 복사한다. 삭제와 다른 파일 수정은
거절한다. 기존 storage 모듈, 다른 Prisma 모델, 기존 migrations, 다른 dependency
graph/lockfile은 자동으로 덮어쓰지 않는다. 원본 source에 민감값이 검출되면 실패하고,
`.env*`·인증 파일·`.git`·`node_modules` 등은 복사하지 않는다. 현재 텍스트 스냅샷만
지원하며 바이너리 파일, symlink, hardlink, 경로 탈출을 거절한다. 비밀값 패턴 검사는
최선의 탐지이며 완전한 비밀 탐지나 E의 Policy Gate를 대신하지 않는다.

템플릿은 `@aws-sdk/client-s3@3.1144.0`, Prisma/Client `6.19.3`, Express `4.22.3`,
dotenv `16.6.1`로 고정했다. `base-package-lock.json`은 공개 데모 v1 커밋
`ebad709867cf1f3523075d8038ae41bd69ecae9b`에서 가져왔다. 새 lockfile은 별도 개발
디렉터리에서 npm 11.3.0으로 `--package-lock-only --ignore-scripts`를 사용해 한 번
생성했다. 패치 실행 중에는 의존성을 새로 해석하지 않는다. 버전을 바꿀 때는 두 데모의
설치·빌드·실행 검증을 다시 수행해야 한다.

**기존 SQLite 데이터나 기존 이미지 파일의 S3 이관은 하지 않는다.** 이번 MVP는 스키마와
호출부의 전환이다. 스키마 반영은 이후 배포 단계의 책임이며 patch 함수는 DB에 접근하지 않는다.

## 실행 환경 변수: A/E 연결용 제안

| 변수 | Local | AWS |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL 컨테이너 연결 값 | RDS PostgreSQL 연결 값 |
| `STORAGE_DRIVER` | `fs` | `s3` |
| `PORT` | 기본 3000 | 기본 3000 |
| `S3_BUCKET` | 불필요 | Adapter가 만든 버킷 이름 |
| `AWS_REGION` | 불필요 | 배포 리전 |

현재 Local Plan 계약은 PostgreSQL + `uploads/` 볼륨이다. 저장 경로는 앱 실행
디렉터리의 `uploads`이며 개발 이미지에서는 `/app/uploads`다. S3는 기본 AWS SDK
자격증명 체인을 사용한다. 코드·diff·이미지에 AWS 키를 넣지 않는다. S3 버킷/리전 변수명은
A/E 통합 시 확정할 제안이며, 공통 Plan에 AWS 리소스 식별자를 새로 추가하지 않았다.

`NoSuchKey`만 파일 없음으로 반환하고 권한 오류·버킷 없음·통신 오류는 실패로 전파한다.
원본에 삭제 API가 없어 삭제 경로는 새로 만들지 않았다. 구현 방식은
[AWS JavaScript SDK의 S3 예제](https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/javascript_s3_code_examples.html)를 따른다.

## 로컬 검증

```sh
# 모델/API/Docker 호출 없는 Python 테스트 + Node 저장 모듈 테스트
.venv/bin/python -m unittest discover -s tests -v

# 검토된 고정 데모만 실제 실행: Docker Desktop + Node 필요
.venv/bin/python -m code_patch.local_smoke --case all
```

첫 Docker 실행은 공식 Node/PostgreSQL 이미지와 npm 패키지를 다운로드할 수 있다.
팀 API, AWS, `.env`를 사용하지 않는다. 실행 시마다 고유한 이미지·컨테이너·네트워크·볼륨을
사용한다. 웹 포트는 `127.0.0.1`에만 열고 DB 포트는 공개하지 않는다. 임시 DB는 테스트
네트워크 안에서만 trust 인증을 사용한다. 컨테이너/볼륨/네트워크는 종료 시 제거하고
이미지는 보고서에 이름을 기록해 남긴다. Docker prune은 실행하지 않는다.

검증 순서는 다음과 같다.

1. Local/AWS 소스가 동일한지, 원본과 diff 해시·수정 허용 경로·JS 문법 확인.
2. `linux/amd64` Node 22 이미지 빌드. `npm ci --ignore-scripts`, 고정 Prisma 도구의
   validate/generate 실행. 레포의 install/start 스크립트는 실행하지 않는다.
   설치한 S3 SDK도 `--network none` 컨테이너의 loopback HTTP 대역 서버에 연결해
   업로드/조회·없는 키·권한 오류 전파를 확인한다.
3. 비어 있는 PostgreSQL에 `prisma db push --skip-generate` 실행. 데이터 삭제 허용
   옵션과 reset은 사용하지 않는다. 이는 개발용 스키마 반영이며 기존 DB 마이그레이션이 아니다.
4. health, 메모 생성/조회, PNG 업로드/바이트 일치, 잘못된 입력/없는 이미지 확인.
5. 앱 컨테이너 재생성·DB 재시작 후 메모와 이미지 유지 확인.
6. v2 worker가 같은 이미지로 PostgreSQL 메모 수를 읽고 SIGTERM에 정상 종료하는지 확인.

결과는 `.local/code-patch-smoke/<실행별 디렉터리>/`의 diff·manifest·report·summary에
저장한다. 개발 smoke는 고정 fixture만 실행한다. 임의 레포의 안전성을 판정하는 Policy
Gate나 제품 Builder/Local Adapter 구현이 아니며, 이 모듈들을 연결한 전체 배포 검증은 남아 있다.
S3는 command 대역 테스트와 실제 SDK + loopback HTTP 대역 서버까지 확인한다.
실제 AWS/S3 호환 서비스, IAM 권한, 버킷 생성은 미검증이다.

현재 Python 자동 테스트 84개(Node 저장 모듈 테스트 3개 실행 포함)와
v1·v2 로컬 smoke는 모두 통과했다. 최신 통합 테스트 결과는
`.local/code-patch-smoke/persistence-and-sdk/summary.json`에 보관했다.
두 사례 모두 앱 컨테이너 재생성·DB 재시작 후 메모와 PNG 바이트가 유지됐고,
v2 worker도 동일 이미지로 DB를 읽고 정상 종료했다. 이 경로의 결과는 Git에 포함하지 않는다.
