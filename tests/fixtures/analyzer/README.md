# Analyzer 오프라인 입력

팀 demo-app의 다음 고정 커밋에서 분석에 필요한 파일만 그대로 복사했다.

- v1: `ebad709867cf1f3523075d8038ae41bd69ecae9b` ([원본](https://github.com/Team-InfraMorph/demo-app/tree/ebad709867cf1f3523075d8038ae41bd69ecae9b))
- v2: `f67109815d689ae52c2474dcb1bf04209d154a5d` ([원본](https://github.com/Team-InfraMorph/demo-app/tree/f67109815d689ae52c2474dcb1bf04209d154a5d))

`snapshot/`은 package.json, Prisma 스키마, 서버·이미지·worker 소스의 축소 스냅샷이다.
`repo_map.json`은 공통 fixture에서 tree만 이 파일 집합으로 줄였다.
`replay.json`은 각 파일의 Read 호출과 공통 expected intent를 반환하는 수작업 응답 시퀀스다.
실제 모델의 추론 기록이 아니며 모델 품질 평가 결과로 사용하지 않는다.
