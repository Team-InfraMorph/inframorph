"""배포기의 '완료' 보고와 별개로 조종실이 접속 주소를 직접 호출해 본다. 배포기 말만 믿지 않는다.

- 주소에 경로가 없으면 plan의 health 경로(예: /health)를 붙여 호출한다.
- 막 뜬 앱은 잠깐 준비 중일 수 있어 INFRAMORPH_VERIFY_ATTEMPTS(기본 3)번까지 1초 간격으로 다시 시도한다.
- `.invalid` 주소(RFC 2606, 가짜 배포기의 예시 주소)는 호출하지 않고 '확인 안 함'으로 남긴다.
"""
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

TIMEOUT_S = 5
WAIT_S = 1.0


def check_url(url, health="/health"):
    """결과 = {status: ok|fail|skipped, url, code?, ms?, detail?, checked_at}."""
    checked_at = datetime.now(timezone.utc).isoformat()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").endswith("invalid"):
        return {"status": "skipped", "url": url, "detail": "가짜 배포기 주소 · 확인 안 함", "checked_at": checked_at}
    target = url if parts.path not in ("", "/") else url.rstrip("/") + (health or "/")
    attempts = max(1, int(os.environ.get("INFRAMORPH_VERIFY_ATTEMPTS", "3")))
    detail = "응답 없음"
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(target, timeout=TIMEOUT_S) as res:
                return {"status": "ok", "url": target, "code": res.status,
                        "ms": int((time.monotonic() - started) * 1000), "checked_at": checked_at}
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            detail = f"연결 실패: {getattr(exc, 'reason', exc)}"
        if attempt + 1 < attempts:
            time.sleep(WAIT_S)
    return {"status": "fail", "url": target, "detail": detail, "checked_at": checked_at}
