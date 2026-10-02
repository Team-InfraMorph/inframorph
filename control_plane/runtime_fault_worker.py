"""Explicit development fault: corrupt actual HTTP image reads; no product default."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from analyzer import e_worker


def perform(data):
    if data["action"] != "deploy":
        return e_worker.perform(data)
    from adapters.local import runtime
    directory = Path(data["fault_dir"])
    directory.mkdir(mode=0o700, exist_ok=True)
    mode = data["fault"]
    if mode not in {"first", "always"}:
        raise ValueError("invalid_fault_mode")
    try:
        fd = os.open(directory / "claimed", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        first = True
    except FileExistsError:
        first = False
    if mode == "first" and not first:
        return e_worker.perform(data)
    real_request = runtime.request
    observed = {}
    injected = False

    def request(url, **kwargs):
        nonlocal injected
        result = real_request(url, **kwargs)
        if url.endswith("/api/notes") and kwargs.get("data") is not None:
            observed["note"] = json.loads(result)
        if url.endswith("/api/images") and kwargs.get("data") is not None:
            observed["image_url"] = json.loads(result)["url"]
        if "/api/images/" in url and kwargs.get("data") is None and not injected:
            injected = True
            (directory / "record.json").write_text(json.dumps(observed))
            return result + b"fault"
        return result

    with patch.object(runtime, "request", request):
        return e_worker.perform(data)


def main():
    try:
        raw = sys.stdin.buffer.read(120_001)
        if len(raw) > 120_000:
            raise ValueError("input_limit")
        result = perform(json.loads(raw))
    except Exception as error:
        import re
        code = str(error) if type(error).__name__ in {"PolicyError", "RuntimeFailure", "SourcePolicyError"} else "e_runtime_failed"
        result = {"ok": False, "code": code if re.fullmatch(r"[a-z_]{1,80}", code) else "e_runtime_failed"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
