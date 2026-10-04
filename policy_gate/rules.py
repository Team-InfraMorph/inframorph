"""Independent checks for supported requirements and restricted transformations."""
import json
import re
import shlex
from pathlib import Path
from .structure import inspect_js, prisma_structure, prisma_provider_lines, json_value_lines
from .reporting import rule


def fail(code, path=None, line=None, *, diagnostics=None):
    from .gate import PolicyError
    error = PolicyError(code)
    error.path, error.line = path, line
    if diagnostics is not None:
        error.diagnostics = diagnostics
    raise error


def anchor(role, path, lines):
    lines = sorted(set(lines))
    return dict(role=role, path=path, start_line=min(lines) if lines else None,
                end_line=max(lines) if lines else None, lines=lines)


def cited(references, required):
    from schemas.common import parse_evidence
    return any(name == required['path'] and line in required['lines']
               for name, line in map(parse_evidence, references))


def evidence_contract(rule_id, references, anchors, allowed_path, *, claimed, observed):
    """Host-built, source-text-free diagnostics used for narrowly scoped repairs."""
    return dict(rule_id=rule_id, claimed=claimed, observed=observed,
                submitted_references=list(references), required_anchors=anchors,
                missing_roles=[a['role'] for a in anchors if not cited(references, a)],
                allowed_evidence_paths=[allowed_path])


def package_scripts(files):
    try:
        package = json.loads(files.get('package.json', b'{}'))
    except (ValueError, UnicodeError):
        fail('package_invalid')
    if not isinstance(package, dict) or not isinstance(package.get('scripts', {}), dict):
        fail('package_invalid')
    return package.get('scripts', {})


def config_check(config, secrets):
    if set(config) & set(secrets): fail('config_overrides_secret')
    allowed = {'PORT': r'[0-9]{1,5}', 'STORAGE_DRIVER': r'fs|s3|gcs', 'THUMB_SIZE': r'[1-9][0-9]{0,3}'}
    for key, value in config.items():
        if re.search(r'SECRET|PASSWORD|TOKEN|API_KEY|DATABASE_URL', key) or key in {'NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'PATH', 'HOME'}:
            fail('config_policy_violation')
        if key not in allowed: fail('config_not_supported')
        if not re.fullmatch(allowed[key], value): fail('config_policy_violation')


def intent_rules(intent, files):
    from schemas.common import parse_evidence
    with rule('I-006') as evidence:
        config_check(intent.config, intent.secrets)
        evidence.update(setting_names=sorted(intent.config), secret_count=len(intent.secrets))
    with rule('I-003') as evidence:
        schema = files.get('prisma/schema.prisma')
        dbs = [s for s in intent.state if s.kind == 'relational_db']
        if schema is not None:
            provider, _, span = prisma_structure(schema)
            evidence.update(path='prisma/schema.prisma', observed=provider, claimed=[d.engine for d in dbs])
            if not dbs: fail('db_requirement_missing', 'prisma/schema.prisma')
            for index, db in enumerate(intent.state):
                if db.kind != 'relational_db': continue
                if db.engine != provider: fail('db_provider_mismatch', 'prisma/schema.prisma')
                details = evidence_contract('I-003', db.evidence,
                    [anchor('db_provider', 'prisma/schema.prisma', prisma_provider_lines(schema))],
                    f'/state/{index}/evidence', claimed=[db.engine], observed=provider)
                evidence.update(details)
                valid = False
                for ev in db.evidence:
                    name, line = parse_evidence(ev)
                    if name == 'prisma/schema.prisma':
                        offset = sum(len(row) for row in schema.decode().splitlines(True)[:line-1])
                        valid |= span[0] <= offset + len(schema.decode().splitlines()[line-1]) and offset <= span[1]
                if not valid: fail('db_evidence_unrelated', 'prisma/schema.prisma', diagnostics=details)
                if details['missing_roles']:
                    fail('db_provider_evidence_missing', 'prisma/schema.prisma', diagnostics=details)
        elif dbs: fail('db_schema_missing', 'prisma/schema.prisma')
    with rule('I-004', applies=any(s.kind == 'persistent_files' for s in intent.state), reason='no_persistent_files') as evidence:
        storage = [item for item in intent.state if item.kind == 'persistent_files']
        evidence.update(references=[e for item in storage for e in item.evidence])
        if storage:
            # Only the independently reviewed original storage module is a supported
            # semantic profile; source-like strings in arbitrary code aren't proof.
            from .gate import sha
            from code_patch.runner import BASE_IMAGES
            for item in storage:
                name = 'src/images.js'
                if item.path.rstrip('/') != 'uploads': fail('storage_path_unsupported')
                if name not in files or sha(files[name]) != BASE_IMAGES:
                    fail('storage_evidence_unsupported', name)
                parsed = inspect_js({name: files[name]})[name]
                supported = [c for c in parsed.get('calls',[]) if c['name'] in {'writeFile','readFile'}]
                related = False
                for ev in item.evidence:
                    file, line = parse_evidence(ev)
                    if file != name: continue
                    rows=files[name].decode().splitlines(True)
                    start=sum(map(len, rows[:line-1])); end=start+len(rows[line-1])
                    related |= any(c['start'] < end and c['end'] > start for c in supported)
                if not related: fail('storage_evidence_unrelated', name)
    with rule('I-002') as evidence:
        evidence.update(services=[w.name for w in intent.workloads if w.kind.value=='worker'])
        for index, w in enumerate(intent.workloads):
            if w.kind.value != 'worker': continue
            try: parts = shlex.split(w.command)
            except ValueError: fail('worker_command_unsupported')
            if len(parts) != 2 or parts[0] != 'node' or not re.fullmatch(r'src/[A-Za-z0-9_/-]+\.js', parts[1]) or '..' in parts[1].split('/'):
                fail('worker_command_unsupported')
            name = parts[1]
            if name not in files: fail('worker_entry_missing', name)
            parsed = inspect_js({name: files[name]})[name]
            if parsed.get('error'): fail(parsed['error'], name)
            # The reviewed demo worker's entry function is tick. A generic helper
            # call in the same file is not evidence of that worker starting.
            starts = [item for item in parsed.get('worker_starts', []) if item['function'] == 'tick']
            start_lines = sorted({line for item in starts for line in item['lines']})
            script_match = False
            scripts = package_scripts(files)
            command_lines = json_value_lines(files.get('package.json', b'{}'), ('scripts', 'worker'))
            if scripts.get('worker') != w.command: command_lines = []
            details = evidence_contract('I-002', w.evidence,
                [anchor('worker_command', 'package.json', command_lines),
                 anchor('worker_start', name, start_lines)],
                f'/workloads/{index}/evidence', claimed={'command': w.command},
                observed={'command': w.command if command_lines else None,
                          'start_count': len(starts)})
            evidence.update(details)
            for ev in w.evidence:
                file, line = parse_evidence(ev)
                if file == name and files[file].decode().splitlines()[line-1].strip(): script_match = True
                if file == 'package.json' and any(v == w.command for v in scripts.values()):
                    row = files[file].decode().splitlines()[line-1]
                    script_match |= w.command in row
            if not script_match: fail('worker_evidence_unrelated', name, diagnostics=details)
            if 'worker_command' in details['missing_roles']:
                fail('worker_command_evidence_missing', 'package.json', diagnostics=details)
            if 'worker_start' in details['missing_roles']:
                fail('worker_start_evidence_missing', name, diagnostics=details)
        scripts = package_scripts(files)
        worker_cmd = scripts.get('worker')
        if worker_cmd and worker_cmd not in [w.command for w in intent.workloads if w.kind.value == 'worker']:
            fail('worker_requirement_missing', 'package.json')


def plan_rules(intent, plan):
    databases = [
        state
        for state in intent.state
        if state.kind == "relational_db"
    ]
    if plan.db is not None:
        if len(databases) != 1:
            fail("plan_state_mismatch")
        source = databases[0]
        if (
            plan.db.source_engine != source.engine
            or plan.db.orm != source.orm
        ):
            fail("plan_state_mismatch")
    config_check(plan.config, plan.secrets)
    if plan.source_revision != intent.source_revision or plan.app != intent.app: fail('plan_intent_mismatch')
    fields = lambda x: (x.name, x.kind.value, x.public, x.port, x.health, x.command)
    if sorted(map(fields, intent.workloads)) != sorted(map(fields, plan.services)): fail('plan_workload_mismatch')
    if bool(plan.db) != any(s.kind == 'relational_db' for s in intent.state): fail('plan_state_mismatch')
    storage = [s for s in intent.state if s.kind == 'persistent_files']
    if bool(plan.storage) != bool(storage) or len(storage) > 1: fail('plan_state_mismatch')
    if storage and plan.storage.path.rstrip('/') != storage[0].path.rstrip('/'): fail('plan_state_mismatch')
    expected_secrets = set(intent.secrets) | ({'DATABASE_URL'} if plan.db else set())
    if set(plan.secrets) != expected_secrets: fail('plan_secret_mismatch')
    for key, value in intent.config.items():
        if plan.config.get(key) != value: fail('plan_config_mismatch')
    if set(plan.config) - set(intent.config) - {'PORT', 'STORAGE_DRIVER'}: fail('plan_config_mismatch')
    for service in plan.services:
        if service.name in {'db', 'schema', 'tunnel'} or service.cpu > 4096 or service.mem > 8192: fail('plan_resource_unsupported')
    web = next(s for s in plan.services if s.public)
    if 'PORT' in plan.config and plan.config['PORT'] != str(web.port): fail('plan_config_mismatch')


def patch_rules(original, patched, plan, changed, js, *, profile="reviewed"):
    name = 'prisma/schema.prisma'
    with rule('P-003', applies=plan.db is not None or name in changed, reason="database_not_requested",) as evidence:
        if plan.db is not None:
            if name not in original or name not in patched:
                fail("prisma_transform_unsupported", name)

            old, before, _ = prisma_structure(original[name])
            new, after, _ = prisma_structure(patched[name])

            evidence.update(path=name, before=old, after=new)

            if old != plan.db.source_engine:
                fail("prisma_transform_unsupported", name)

            if new != plan.db.target_engine:
                fail("prisma_provider_invalid", name)

            if before != after:
                fail("prisma_structure_changed", name)

            if (
                plan.db.patch == "none"
                and original[name] != patched[name]
            ):
                fail("prisma_transform_unsupported", name)
        elif name in changed:
            fail("prisma_transform_unsupported", name)

    with rule('P-004', applies=any(n.endswith(('.js','.cjs','.mjs')) for n in changed), reason='javascript_unchanged') as evidence:
        evidence.update(changed_paths=[n for n in changed if n.endswith(('.js','.cjs','.mjs'))])
        before_js = inspect_js({k: original[k] for k in changed if k in original})
        root = Path(__file__).resolve().parents[1]
        templates = {str(p.relative_to(root / 'code_patch/templates')): p.read_bytes() for p in (root / 'code_patch/templates').glob('*.js')}
        approved = inspect_js({f'src/{k}': v for k,v in templates.items()})
        # Only reviewed source forms can receive the approved replacement module.
        from code_patch.runner import BASE_IMAGES
        from .gate import sha
        for name in changed:
            if not name.endswith(('.js','.cjs','.mjs')): continue
            ast = js[name]['normalized']
            if name in before_js and ast == before_js[name].get('normalized'): continue
            if plan.storage and name in approved and ast == approved[name]['normalized']:
                if name == 'src/storage.js' and name not in original: continue
                if name == 'src/images.js' and (sha(original.get(name,b'')) == BASE_IMAGES or original.get(name) == templates['images.js']): continue
            if profile == "corpus":
                expected = {
                    'src/storage.js': b"async function readImage(key, storage) {\n  return storage.get(key);\n}\nmodule.exports = { readImage };\n",
                    'src/storage-adapter.js': b"module.exports = { get: async (key) => Buffer.from(key) };\n",
                }
                if name in expected and patched[name] == expected[name]: continue
            # Fixed inert corpus transformations are a test-only profile selected by
            # host-side callers, never by bundle/manifest or app name.
            fail('patch_behavior_changed', name)
    with rule('P-005', applies=bool(set(changed) & {'package.json','package-lock.json'}), reason='dependencies_unchanged') as evidence:
        evidence.update(changed_paths=sorted(set(changed)&{'package.json','package-lock.json'}))
        for name in ('package.json','package-lock.json'):
            if name not in changed: continue
            if not plan.storage: fail('dependency_change_forbidden', name)
            trusted = root / 'code_patch/templates/package-lock.json'
            if name == 'package-lock.json' and patched[name] != trusted.read_bytes(): fail('dependency_change_forbidden', name)
            if name == 'package.json':
                try:
                    a, b = json.loads(original[name]), json.loads(patched[name])
                    from code_patch.runner import GCS_SDK_VERSION, SDK_VERSION
                    expected = dict(a); expected['dependencies'] = a.get('dependencies', {}) | {
                        '@aws-sdk/client-s3': SDK_VERSION,
                        '@google-cloud/storage': GCS_SDK_VERSION,
                    }
                    if b != expected: fail('dependency_change_forbidden', name)
                except (ValueError, KeyError): fail('dependency_change_forbidden', name)
