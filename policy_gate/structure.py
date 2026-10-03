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


def prisma_provider_lines(data):
    """Only the datasource provider key/value token lines, never its enclosing block.

    Reuse the supported-structure validation before locating the assignment. Line
    gaps (including comments and an isolated equals sign) are not evidence.
    """
    _, _, span = prisma_structure(data)
    text = data.decode()
    tokens = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(text)
              if not m.group().isspace() and not m.group().startswith(('//', '/*'))]
    assignments = [(tokens[i], tokens[i+2]) for i in range(len(tokens)-2)
                   if tokens[i][0] == 'provider' and tokens[i+1][0] == '='
                   and span[0] <= tokens[i][1] < span[1]]
    lines = set()
    for key, value in assignments:
        for _, begin, end in (key, value):
            lines.update(range(text.count('\n', 0, begin)+1,
                               text.count('\n', 0, max(begin, end-1))+2))
    return sorted(lines)


def json_value_lines(data, path):
    """Locate an exact JSON member value without substring/comment matching.

    Reject duplicate object keys: a citation cannot identify an ambiguous member.
    JSON is parsed as data and no source code is evaluated.
    """
    from .gate import PolicyError
    text = data.decode()
    decoder = json.JSONDecoder()
    found = []

    def whitespace(index):
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        return index

    def read(index, location):
        index = whitespace(index)
        begin = index
        if text[index] == '{':
            index = whitespace(index+1)
            seen = set()
            if text[index] == '}':
                end = index+1
            else:
                while True:
                    key, index = decoder.raw_decode(text, index)
                    if not isinstance(key, str) or key in seen:
                        raise ValueError()
                    seen.add(key)
                    index = whitespace(index)
                    if text[index] != ':':
                        raise ValueError()
                    index = whitespace(read(index+1, (*location, key)))
                    if text[index] == '}':
                        end = index+1
                        break
                    if text[index] != ',':
                        raise ValueError()
                    index = whitespace(index+1)
        elif text[index] == '[':
            index = whitespace(index+1)
            item = 0
            if text[index] == ']':
                end = index+1
            else:
                while True:
                    index = whitespace(read(index, (*location, item)))
                    item += 1
                    if text[index] == ']':
                        end = index+1
                        break
                    if text[index] != ',':
                        raise ValueError()
                    index = whitespace(index+1)
        else:
            _, end = decoder.raw_decode(text, index)
        if location == tuple(path):
            found.extend(range(text.count('\n', 0, begin)+1,
                               text.count('\n', 0, max(begin, end-1))+2))
        return end

    try:
        end = read(0, ())
        if whitespace(end) != len(text):
            raise ValueError()
    except (ValueError, IndexError, RecursionError):
        raise PolicyError('package_invalid') from None
    return sorted(set(found))
