"""Execution checks for the host-registered board; never policy PASS evidence."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import time

from builder.runtime import RuntimeFailure


def note_event(logs, *, since, minimum):
    """Only current-container JSON events after the probe; no substring matches."""
    for line in logs.splitlines()[-200:]:
        if len(line) > 1024:
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or set(event) != {'event', 'count', 'at'}:
                continue
            at = datetime.fromisoformat(event['at'].replace('Z', '+00:00'))
            if (event['event'] == 'note_count' and type(event['count']) is int
                    and event['count'] >= minimum and at.tzinfo is not None
                    and since <= at <= datetime.now(timezone.utc)):
                return event
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def check_worker(execute, args, url, image_id, request, *, timeout=30):
    ids = execute(args + ['ps', '-q', 'worker'], timeout=5).splitlines()
    if len(ids) != 1 or not ids[0].strip():
        raise RuntimeFailure('board_worker_not_running')
    container = ids[0].strip()
    def inspect():
        rows = json.loads(execute(['docker', 'inspect', container], timeout=5))
        if len(rows) != 1 or rows[0].get('Image') != image_id or not rows[0].get('State', {}).get('Running'):
            raise RuntimeFailure('board_worker_not_running')
        return rows[0]['State'].get('StartedAt')
    started = inspect()
    since = datetime.now(timezone.utc)
    deadline = time.monotonic() + timeout
    note = json.loads(request(url + '/api/notes', data=json.dumps({'text':'InfraMorph worker verification'}).encode(), content_type='application/json', expected=201))
    notes = json.loads(request(url + '/api/notes'))
    if note not in notes:
        raise RuntimeFailure('board_worker_probe_missing')
    minimum = len(notes)
    while time.monotonic() < deadline:
        if inspect() != started:
            raise RuntimeFailure('board_worker_restarted')
        logs = execute(['docker', 'logs', '--since', since.isoformat(), '--tail', '200', container], timeout=5)
        event = note_event(logs, since=since, minimum=minimum)
        if event and time.monotonic() <= deadline:
            return {'code':'board_worker_verified', 'container_id':container, 'image_id':image_id,
                    'probe_note_id':note['id'], 'minimum_count':minimum, 'observed_count':event['count'], 'observed_at':event['at']}
        time.sleep(min(1, max(0, deadline-time.monotonic())))
    raise RuntimeFailure('board_worker_timeout')


def check_assets(url, profile, request):
    """Verify all reviewed UI files in the actual running image, not its tag label."""
    hashes = {}
    for name, expected in profile['files'].items():
        if not name.startswith('src/web/'):
            continue
        raw = request(url + '/' + name.removeprefix('src/web/'))
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeFailure('board_asset_mismatch')
        if '/assets/' in name:
            try:
                asset = json.loads(raw)
                data = base64.b64decode(asset['data'], validate=True)
                valid = (asset['mime']=='image/png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or (asset['mime']=='image/jpeg' and data.startswith(b'\xff\xd8\xff'))
                if len(raw)>220*1024 or not valid:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise RuntimeFailure('board_asset_invalid') from None
        hashes[name] = expected
    if hashlib.sha256(request(url + '/')).hexdigest() != profile['files']['src/web/index.html']:
        raise RuntimeFailure('board_asset_mismatch')
    return {'code':'board_assets_verified','file_count':len(hashes)}
