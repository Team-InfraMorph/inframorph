"""Materialize reviewed V3 test inputs from demo-app; missing or changed inputs fail."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def prepare(repository=None):
    profiles = json.loads((ROOT/'analyzer/board-profiles.json').read_text())
    cache = ROOT/'.local/demo-board-sources'
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(repository).resolve() if repository else Path(temp)/'repo'
        if not repository:
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            for profile in profiles:
                subprocess.run(['git','-C',str(repo),'fetch','--depth=1',
                    'https://github.com/Team-InfraMorph/demo-app.git',profile['commit_sha']], check=True)
        for profile in profiles:
            revision = profile['commit_sha']
            files = {}
            for name, expected in profile['files'].items():
                raw = subprocess.check_output(['git','-C',str(repo),'show',f'{revision}:{name}'])
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError('reviewed_source_hash_mismatch:'+name)
                files[name] = raw
            output = cache/profile['id']/'snapshot'
            # Only this generated cache is replaced. Old evidence and user sources are untouched.
            import shutil
            if output.exists(): shutil.rmtree(output)
            for name, raw in files.items():
                path = output/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
            print(f"Prepared {profile['id']} {revision}: {len(files)} verified source files")

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository',type=Path,help='Existing demo-app clone; read fixed commit, not working files')
    prepare(parser.parse_args().repository)
