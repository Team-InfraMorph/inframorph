# V3 체험 보드 실행 안내

기존 V1(web·DB·파일 저장), V2(worker 추가)는 그대로 유지합니다.
V3는 V2의 API·DB·worker에 밝은 화면과 이미지 갤러리를 추가합니다. worker는 V3의 신규 기능이 아닙니다.
GitHub 모드에서 V1 / V2 / V3를 선택하며 V3는 Local 전용입니다. fixture 모드는 기존 V1·V2만 제공합니다.
V3 고정 소스는 analyzer/board-profiles.json의 demo-app commit입니다. 별도 board-v1/board-v2 선택은 없습니다.

## 테스트 자료 관리

실제 소스·그림·잠금 파일은 demo-app에서 관리합니다. inframorph에는 검토 SHA·파일 해시와 작은 예상 Intent·RepoMap만 보관합니다.
기존 V1·V2의 축소 오프라인 fixture는 변경하지 않습니다. V3 연동 테스트 전에 다음 명령으로 고정 소스를 가져옵니다.

```sh
python3 scripts/prepare_demo_board_sources.py
# 이미 있는 demo-app clone에서 같은 고정 commit을 읽어도 됩니다.
python3 scripts/prepare_demo_board_sources.py --repository /path/to/demo-app
.venv/bin/python -m unittest tests.test_demo_board -v
```

소스는 Git에서 제외된 .local/demo-board-sources/v3/snapshot에 생성됩니다. 작업 디렉터리나 최신 브랜치로 대체하지 않습니다.
다운로드 실패·해시 불일치·준비되지 않은 입력은 실패입니다. CI도 같은 준비 단계를 수행합니다.

## V2 → V3 데이터 유지 확인

1. 같은 저장소·브랜치·프로젝트에서 기존 V2를 Local로 배포합니다.
2. V2 API로 메모와 PNG 이미지를 저장하고 메모 ID·이미지 확인키·SHA-256을 기록합니다.
3. 같은 프로젝트에서 V3를 배포합니다. 서버가 검토를 요청하면 내용을 확인하고 승인합니다.
4. V3 화면에서 기존 메모를 서버에서 다시 조회하고 이미지 확인키로 원본 해시를 비교합니다.
5. V3의 화면 응답 해시와 현재 worker 컨테이너·이미지·집계 이벤트를 확인합니다.

V2와 V3는 모두 worker를 갖습니다. 이 전환을 worker 추가나 새로운 인프라 요구의 증거로 표현하지 않습니다.

## 고정 입력 Docker 검증

```sh
.venv/bin/python scripts/verify_demo_board_docker.py --output .local/v3-new-run --publish
```

실제 GitHub의 기존 V2와 V3 commit을 가져와 고정 Intent로 검사·빌드·실행하며 같은 데이터 볼륨을 사용합니다.
실제 AI 분석이나 조종실 승인 검증은 아닙니다. --publish는 앱만 공개하며 생략하면 터널을 만들지 않습니다.
출력 경로는 새 경로여야 하며 기존 기록을 덮어쓰지 않습니다. DB·업로드 볼륨은 실행 후 유지됩니다.

## 실제 AI와 조종실 검증

```sh
.venv/bin/python -m control_plane.runtime --github --openai --model gpt-6-luna \
  --publish --root .local/demo-v3-live --port 8877
.venv/bin/python scripts/verify_demo_board_live.py --control-plane http://127.0.0.1:8877 \
  --output .local/demo-v3-live/verification.json
```

OPENAI_API_KEY가 필요합니다. 실제 호출 실패를 저장 응답으로 대체하지 않습니다.
--approve-changes를 명시한 경우에만 서버가 요구한 검토를 승인합니다. V2→V3에 worker 추가 승인을 억지로 요구하지 않습니다.
조종실은 localhost에 두고 실제 폰 확인은 데스크톱 모바일 크기 검사와 별도로 기록합니다.

## 검증 기록의 범위

validation/demo-board의 2026-10-03 기록은 이전 board-v1/board-v2 commit으로 수행했던 고정 입력 실험 기록입니다.
그 기록을 V3의 실행 성공으로 재사용하지 않습니다. V3 실행은 새 출력 경로와 소스 SHA로 별도 기록합니다.
정책 1.1.0의 기존 자동 복구를 유지하며 V3 지원만 추가합니다. 정책 1.0.0과 기존 샘플·태그는 보존합니다.

2026-10-04(KST) V2→V3의 실제 GitHub·Docker 고정 입력 검증 결과는 `validation/demo-board/v2-to-v3.json`에 기록했습니다. 메모·이미지 해시 유지, V3 화면 10개 응답 및 현재 worker 집계를 확인했습니다. 실제 AI·공개 접속·폰 확인 결과로 해석하지 않습니다.
