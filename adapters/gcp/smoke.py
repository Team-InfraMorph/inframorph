import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import DeploymentError


@dataclass(frozen=True)
class SmokeResult:
    url: str
    status: int
    body_preview: str


def external_https_smoke(
    base_url: str,
    health_path: str,
    timeout_seconds: int = 180,
    interval_seconds: int = 2,
) -> SmokeResult:
    """GET <base_url><health_path> over verified HTTPS until it returns 200.

    The new revision already serves all traffic when this runs, so the first
    request normally succeeds; a short interval keeps retries cheap.
    """
    if not health_path.startswith("/"):
        raise DeploymentError("health path must start with /")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise DeploymentError("smoke base URL must be an https origin")
    url = "https://{}{}".format(parsed.netloc, health_path)
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    context = ssl.create_default_context()
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, headers={"User-Agent": "InfraMorph-GCP-Adapter/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=10, context=context) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                if response.status == 200:
                    return SmokeResult(url, response.status, body[:512])
                last_error = "HTTP {}".format(response.status)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(interval_seconds)
    raise DeploymentError("external HTTPS smoke failed for {}: {}".format(url, last_error))
