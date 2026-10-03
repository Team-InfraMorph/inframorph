# InfraMorph 체험 보드 실행 안내

밝은 화면과 기존 메모·이미지 API를 사용하는 Local 전용 체험입니다.

| 선택 | 고정 GitHub commit | 구성 |
|---|---|---|
| board-v1 | f7b3983d1955391b5752ad051718d9b6cde2ebdc | web, DB, uploads |
| board-v2 | 8ef6ccdcb619050591ee32061734a95c17d50b9a | 같은 화면·DB·파일 + 기존 집계 worker |

소스 브랜치는 Team-InfraMorph/demo-app의 feat/e-demo-board입니다. 기존 demo-v1·demo-v2 태그와 fixture는 유지합니다.
정책 1.1.0의 G-002/G-003은 이 두 묶음만 추가 지원합니다. 1.0.0 문서·기준선은 변경하지 않습니다.

## 실제 AI 시연 시작

새 checkout에서 Python 의존성을 설치하고 control_plane/web에서 npm ci와 npm run build를 실행합니다.
서버의 비공개 .env에 OPENAI_API_KEY를 직접 설정합니다. 앱 소스·공개 터널·Git·채팅에 키를 넣지 않습니다.

```sh
.venv/bin/python -m control_plane.runtime --github --openai --model gpt-6-luna \
  --publish --root .local/demo-board-live --port 8877
```

조종실은 http://127.0.0.1:8877 에서만 열립니다. 기존 8876 서버와 DB·프로젝트·실행 소스는 분리되어 있습니다.
새 터널은 앱만 공개합니다. 키가 없으면 서버가 시작 전에 중단하며 저장 응답 재생으로 바꾸지 않습니다.

1. demo-app과 동일 브랜치를 선택하고 체험 V1을 Local로 배포합니다.
2. 실제 모델 호출 횟수·고정 source SHA·정책 1.1.0 검사·빌드·공개 응답을 확인합니다.
3. 앱에서 고유한 메모와 이미지 한 건을 저장하고 ID·이미지 확인키·해시를 보관합니다.
4. 같은 프로젝트에서 체험 V2를 선택합니다. worker 추가에 대한 승인 대기를 확인하고 승인합니다.
5. 새 공개 주소에서 기존 메모를 다시 조회하고 같은 이미지 확인키의 응답 해시를 비교합니다.
6. smoke 단계의 화면·기본 이미지 확인과 worker 집계 결과를 확인합니다.
7. 폰에서 공개 주소·메모·이미지를 직접 확인하고 브라우저 모바일 크기 검사와 구분해 기록합니다.

같은 절차를 API로 실행하려면 아래 도구를 사용합니다. `--approve-v2`는 worker 추가의 승인 대기 상태를 확인한 후 승인하는 명시적 옵션입니다. 기존 결과 파일을 덮어쓰지 않습니다.

```sh
.venv/bin/python scripts/verify_demo_board_live.py \
  --control-plane http://127.0.0.1:8877 --approve-v2 \
  --output .local/demo-board-live/verification.json
```

이 도구는 GitHub/OpenAI 모드, 실제 API 호출, V1/V2 workload 차이, 전체 정책 PASS, 현재 이미지·worker 검증 기록을 요구합니다.
키 설정 전에는 실행할 수 없습니다. 실행 실패나 정책 차단을 자동 면제하지 않습니다.

## 고정 입력을 사용하는 Docker 검증

```sh
.venv/bin/python scripts/verify_demo_board_docker.py \
  --output .local/board-contract-new-run --publish
```

GitHub에서 두 실제 commit을 수집하고, 사람이 작성한 테스트 Intent로 정책·패치·빌드·실행을 검증합니다.
모델 호출과 조종실 승인 절차는 이 명령의 확인 범위가 아닙니다. 별도 프로젝트 이름을 사용하며 출력 폴더는 새 경로여야 합니다.
`--publish`를 생략하면 터널을 만들지 않습니다. DB·uploads 볼륨은 검사 종료 후에도 유지됩니다.

2026-10-03의 실제 결과는 validation/demo-board/docker-verification.json에 있습니다.
두 이미지 ID, 공개 주소, 검증 메모 ID·이미지 키·해시, 현재 worker 컨테이너와 집계 시각을 연결합니다.
공개 주소는 해당 노트북과 Quick Tunnel 실행 중에만 유효하며 새 배포에서 변경될 수 있습니다.

## 이번 확인 결과

- 실제 GitHub 수집, Docker 빌드·실행, cloudflared 응답: 확인.
- V1 메모 #2와 이미지 확인키를 V2에서 다시 조회하고 원본 이미지 SHA-256 일치: 확인.
- V2 현재 이미지의 worker가 검증 메모 #4를 포함한 note_count=4를 약 10초 안에 기록: 확인.
- 샘플 그림 5개와 실제 조종실 캡처 1개, 화면 파일 10개의 실제 응답 해시: 확인.
- Python 전체 482개, AWS 모의 검사 43개, UI 37개, 고정 redteam 39개: 통과.
- 새 소스·대상 제한·로그 경계 회귀와 1.0.0 바이트 보존: 확인.
- 브라우저 390px 모바일 크기, 가로 넘침 없음, 확인키 재조회·확대 창 키보드 닫기: 확인.
- 실제 OpenAI 분석부터 조종실 배포까지: OPENAI_API_KEY 미설정으로 미실행.
- 실제 폰 확인: 사용자 확인 대기.

## 실패 위치

- 요청 거부: 체험 버전·저장소·대상 조합과 호스트 버전 목록을 확인합니다.
- G-002: 등록 SHA와 전체 파일 묶음, Intent의 실제 코드 근거를 확인합니다. 화면 문구를 근거로 바꾸지 않습니다.
- G-003: 체험 보드는 Local 전용입니다. AWS·온프레미스는 선택하거나 우회 실행할 수 없습니다.
- board_asset_mismatch: 실제 실행 이미지와 검토된 src/web 파일을 대조합니다.
- board_worker_*: 정책 위반이 아닌 실행 실패입니다. 현재 컨테이너·이미지·DB·집계 시각을 확인합니다.
- 롤백: 기존 Local 복구 경로가 이전 앱과 데이터 재조회를 확인했는지 별도 기록을 확인합니다.

외부 CDN, 신규 webhook, 계산 API, DB 스키마 변경, 실제 AWS/온프레미스 실행은 추가하지 않았습니다.
