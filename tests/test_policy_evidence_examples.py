"""The example producer executes real pinned checkers without claiming deployment."""
import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_policy_evidence import generate
from control_plane.db import Store
from control_plane.policy_lifecycle import comparison, impacts, statistics

ROOT = Path(__file__).resolve().parents[1]


class EvidenceExampleTests(unittest.TestCase):
    def test_actual_baseline_and_current_examples_stay_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp).resolve() / 'examples'
            args = argparse.Namespace(output_dir=output, openai=False)
            with redirect_stdout(io.StringIO()), patch('builder.runtime.build', side_effect=AssertionError('no build')):
                asyncio.run(generate(args))
            data = json.loads((output / 'examples.json').read_text())
            baseline = json.loads((ROOT / 'policy_gate/baselines/1.0.0.json').read_text())
            self.assertEqual(data['baseline_policy_digest'], baseline['identity']['policy_digest'])
            self.assertEqual(data['live_ai'], 'not_run')
            self.assertEqual(data['service_verification'], 'not_run')
            items = {row['id']: row for row in data['items']}
            self.assertEqual(len(items), 7)
            self.assertTrue(all(row['read_only'] and not row['service_verified'] for row in items.values()))
            for suffix, code in [('db-url-only', 'db_provider_evidence_missing'),
                                 ('worker-initializer', 'worker_command_evidence_missing')]:
                row = items['policy-example-' + suffix]
                self.assertEqual(row['original_decision'], 'PASS')
                self.assertEqual(row['latest_recheck']['decision'], 'BLOCK')
                self.assertEqual(row['latest_recheck']['reason_code'], code)
                records = row['policy_data']['results']
                self.assertTrue(any(r['version'] == '1.0.0' and r['policy_digest'] == baseline['identity']['policy_digest'] for r in records))
                self.assertTrue(any(r['version'] == '1.1.0' and r['decision'] == 'BLOCK' for r in records))
            self.assertEqual(items['policy-example-missing']['latest_recheck']['decision'], 'UNAVAILABLE')
            exhausted = items['policy-example-repair-exhausted']
            self.assertEqual(exhausted['latest_recheck']['decision'], 'BLOCK')
            self.assertEqual(len(exhausted['policy_data']['repairs']), 3)
            for suffix in ('repair-db', 'repair-worker'):
                row = items['policy-example-' + suffix]
                self.assertEqual(row['latest_recheck']['decision'], 'PASS')
                self.assertTrue(all(s['decision'] == 'NOT_RUN' for s in row['policy_data']['summaries']))
            self.assertEqual(data['record_stores'], {'validation': 'checks.db', 'viewer': 'control_plane.db'})
            with sqlite3.connect(output / 'checks.db') as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deployment_targets WHERE status='LIVE'").fetchone()[0], 0)
                self.assertGreater(connection.execute('SELECT COUNT(*) FROM policy_results').fetchone()[0], 0)
                self.assertGreater(connection.execute('SELECT COUNT(*) FROM policy_events').fetchone()[0], 0)
            viewer = Store(output / 'control_plane.db')
            try:
                self.assertEqual(statistics(viewer), [])
                self.assertEqual(impacts(viewer), [])
                self.assertEqual(viewer._all('SELECT * FROM projects', ()), [])
                self.assertEqual(viewer._all('SELECT * FROM policy_results', ()), [])
                self.assertEqual(viewer._all('SELECT * FROM policy_events', ()), [])
                compared = comparison(viewer, '1.0.0', '1.1.0', baseline['identity']['policy_digest'])
                self.assertEqual(compared['history_status'], 'known')
                self.assertEqual({c['impact_scope'] for c in compared['changes'] if c.get('impact_scope')}, {'db', 'worker'})
            finally:
                viewer.close()
            # A second run cannot silently overwrite its own provenance or DB.
            with self.assertRaises(FileExistsError):
                asyncio.run(generate(args))


if __name__ == '__main__':
    unittest.main()
