"""Bounded syntax inspection for the reviewed Node/Prisma profile."""
import json
import re
import subprocess
from pathlib import Path


def inspect_js(files):
    from .gate import PolicyError, clean_env
    data = {k: v.decode() for k, v in files.items() if k.endswith(('.js', '.cjs', '.mjs'))}
    try:
        p = subprocess.run(['node', '--max-old-space-size=192', str(Path(__file__).with_name('inspect_js.cjs'))],
                           input=json.dumps(data), text=True, capture_output=True, timeout=20, env=clean_env())
        value = json.loads(p.stdout)
        if p.returncode or 'error' in value:
            raise ValueError()
        return value
    except (OSError, ValueError, subprocess.SubprocessError):
        raise PolicyError('javascript_parser_unavailable') from None


# Tokenization is lossless except comments/whitespace. It does not execute a
# Prisma generator and never normalizes model definitions away.
TOKEN = re.compile(r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]', re.M)


def prisma_structure(data):
    from .gate import PolicyError
    text = data.decode()
    tokens = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(text)
              if not m.group().isspace() and not m.group().startswith(('//', '/*'))]
    values = [t[0] for t in tokens]
    # The supported profile has one datasource and balanced blocks.
    depth, blocks = 0, []
    for i, value in enumerate(values):
        if value == '{':
            if depth == 0:
                if i < 2 or values[i-2] not in {'datasource', 'generator', 'model', 'enum'}:
                    raise PolicyError('prisma_structure_unsupported')
                blocks.append([values[i-2], i, None])
            depth += 1
        elif value == '}':
            depth -= 1
            if depth < 0: raise PolicyError('prisma_structure_unsupported')
            if depth == 0: blocks[-1][2] = i
    ds = [b for b in blocks if b[0] == 'datasource']
    if depth or len(ds) != 1:
        raise PolicyError('prisma_structure_unsupported')
    _, start, end = ds[0]
    indices = [i for i in range(start+1, end-2) if values[i:i+2] == ['provider', '=']]
    if len(indices) != 1 or values[indices[0]+2] not in {'"sqlite"', '"postgresql"'}:
        raise PolicyError('prisma_structure_unsupported')
    i = indices[0]+2
    provider = json.loads(values[i])
    normalized = values[:]; normalized[i] = '"<provider>"'
    return provider, normalized, (tokens[start][1], tokens[end][2])
