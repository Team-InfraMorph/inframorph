# 정책 1.1.0 검증 기록

## 실행 결과 {#results}

2026-10-03, main 19bb429에서 분리한 체험 보드 작업본에서 확인한 결과입니다.

| 검증 | 확인 범위 |
|---|---|
| 고정 입력 회귀 | Python 482개·UI 37개·AWS 모의 43개·redteam 39개 통과. 기존 정책·문서·수명 주기·redteam 사례 유지, 체험 파일 변조·누락·대상 제한·오래된 worker 로그 거부 |
| GitHub 소스·Docker | 등록된 V1·V2 commit을 GitHub에서 수집하고 실제 이미지를 빌드·실행 |
| 실행 결과 | 두 버전의 화면·이미지 10개 파일 응답 해시 일치, V1 메모·이미지의 V2 재조회·해시 유지 |
| worker | 현재 V2 이미지 컨테이너의 검증 메모와 유효한 note_count 집계 확인 |
| 공개 접속 | cloudflared 주소를 통한 메모·이미지 API 응답 확인 |
| UI | 브라우저의 샘플 업로드·재조회와 모바일 크기 확인. 실제 휴대폰 확인은 별도 |

이 Docker 실행의 Intent는 명시적인 고정 검증 입력입니다. 실제 OpenAI 분석 성공으로 기록하지 않습니다. 원본 증거는 별도 실행 안내 DEMO_BOARD.md의 검증 기록에 연결합니다.

## 확인 범위 {#findings}

G-002와 G-003을 확장해 등록된 체험 묶음과 Local 제한을 추가했습니다. 기존 1.0.0 문서·기준선과 과거 결과를 수정하지 않습니다. 새 파일의 허용은 임의 앱 지원이 아니며 화면 문구는 분석 근거로 인정하지 않습니다.

## 화면 확인 {#ui}

기존 조종실의 버전 선택과 대상 선택 구조를 유지합니다. 체험 버전을 선택하면 Local만 선택할 수 있으며 서버도 검증합니다. smoke 단계에 화면 파일·worker 집계의 구조화된 요약을 표시합니다. 정책 문서 1.0.0·1.1.0은 같은 버전 선택기로 구분합니다.

## 검증의 한계 {#limits}

실제 OpenAI 호출은 키 설정 후 별도의 실행이 필요합니다. 저장 응답 재생으로 대체하지 않습니다. AWS 검사는 모의 검사이며 실제 AWS·온프레미스 배포를 하지 않습니다. 모바일 크기 브라우저 확인은 실제 폰 확인이 아닙니다.

## 구현·검증 근거 {#references}

- tests.test_demo_board.BoardTests
- tests.test_demo_board.BoardRequestTests
- tests.test_demo_board.ExecutionClassificationTests
- scripts/verify_demo_board_docker.py: 고정 입력 + 실제 GitHub·Docker
- scripts/verify_demo_board_live.py: 실제 GitHub·OpenAI 조종실 검증

[재현 방법](validation.md#run) · [변경 이력](releases.md#board-release)
