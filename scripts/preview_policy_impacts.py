#!/usr/bin/env python3
"""Inspect a copy of existing deployment records and explicitly recheck policy.

The source DB is opened read-only and backed up consistently. Preserved source
and bundle paths are read by the existing checker; no runtime is configured.
All writes except policy rechecks/reviews are blocked, including webhooks.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.responses import JSONResponse
from control_plane.app import create_app
from policy_gate.reporting import now

ALLOWED_WRITE = re.compile(
    r'/api/deployments/[^/]+/(?:policy-rechecks|policy-reviews|policy-failures/[^/]+/review)'
)


def create_preview(source_db, output_dir, examples=None):
    source_db = Path(source_db).resolve(strict=True)
    output_dir = Path(output_dir).absolute()
    if any(p.is_symlink() for p in (output_dir, *output_dir.parents)):
        raise ValueError('unsafe_preview_directory')
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    destination = output_dir / 'control_plane.db'
    with sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    context = dict(mode='operational_copy', copied_at=now(), manual_rechecks=True)
    (output_dir / 'preview.json').write_text(json.dumps(context, indent=2) + '\n')
    app = create_app(db_path=destination)
    app.state.policy_review_context = context
    if examples:
        app.state.policy_examples_path = Path(examples).resolve(strict=True)
        # Version comparison needs immutable baseline release snapshots as well.
        # Import only release metadata; never import example deployments/checks.
        examples_db = app.state.policy_examples_path.parent / 'control_plane.db'
        if examples_db.is_file():
            with sqlite3.connect(examples_db.as_uri() + '?mode=ro', uri=True) as source:
                releases = source.execute('SELECT digest,payload,created_at FROM policy_releases').fetchall()
            with app.state.store._lock, app.state.store._conn:
                app.state.store._conn.executemany('INSERT OR IGNORE INTO policy_releases VALUES (?,?,?)', releases)

    @app.middleware('http')
    async def restrict_preview(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not (
            request.method == 'POST' and ALLOWED_WRITE.fullmatch(request.url.path)
        ):
            return JSONResponse(status_code=409, content={
                'detail': '검증용 사본에서는 정책 재검사와 검토 기록만 가능합니다. 실제 배포는 실행하지 않습니다.'})
        return await call_next(request)

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--examples', type=Path)
    parser.add_argument('--port', type=int, default=8878)
    args = parser.parse_args()
    app = create_preview(args.source_db, args.output_dir, args.examples)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port)
