"""가짜 배포기: 이벤트 fixture를 실제 배포기처럼 한 줄씩 stdout에 찍는다.

실제 Local/AWS 배포기(E·A)가 붙기 전까지 화면과 이벤트 흐름을 확인하는 용도다.
    python -m control_plane.fake_deployer --deployment-id d-1 --target local --delay 1
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "events" / "happy_path.jsonl"
LOCAL_URL = "http://localhost:3000"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--target", choices=["local", "aws"], default="aws")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--note", help="배포하지 않고 이 안내 한 줄만 남긴다(실제 배포기에 넘길 입력이 없을 때)")
    args = parser.parse_args()

    if args.note:
        event = {"deployment_id": args.deployment_id, "ts": datetime.now(timezone.utc).isoformat(),
                 "target": args.target, "step": "start", "status": "ok", "detail": args.note}
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return 0

    for line in args.fixture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        event["deployment_id"] = args.deployment_id
        event["ts"] = datetime.now(timezone.utc).isoformat()
        event["target"] = args.target
        if args.target == "local" and "url" in event:
            event["url"] = LOCAL_URL
        print(json.dumps(event, ensure_ascii=False), flush=True)
        time.sleep(args.delay)
    print("fake deployer finished", file=sys.stderr)
    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
