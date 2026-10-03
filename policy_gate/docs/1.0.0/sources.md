# 설계 근거와 출처

## 버전과 업그레이드

[SemVer](https://semver.org/spec/v2.0.0.html)는 공개 계약과 출시 내용의 불변성을 정의합니다. InfraMorph는 지원 범위와 판정 조건을 공개 계약에 포함합니다.

[Spring Boot 업그레이드 안내](https://docs.spring.io/spring-boot/upgrading.html)는 건너뛴 릴리스의 변경도 검토하도록 안내합니다. 비교 API는 previous 연결을 따라 변경을 누적하고 연결이 없으면 오류로 처리합니다.

## 판정과 로그

[OPA Decision Logs](https://www.openpolicyagent.org/docs/management-decision-logs)의 판정 식별자·정책 revision·민감정보 제거 개념을 적용했습니다. OPA를 도입하지 않았습니다.

[OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)의 발생 시각·관찰 시각 구분과 [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)의 비밀정보 제외·로그 주입 대응을 참고했습니다. UI는 로그를 텍스트로만 표시하고 구조화 이벤트로 해석하지 않습니다.

## 현재 한계

SQLite 판정 기록은 애플리케이션에서 수정하지 않습니다. DB 관리자에 대한 위변조 방지나 외부 감사 시스템 수준의 보장은 없습니다. 보존 자료 재검사는 운영 이미지의 실시간 drift 검사를 수행하지 않습니다. 운영자 신원은 현재 로컬 접근 경계를 따르며 외부 사용자 인증을 새로 도입하지 않았습니다.
