"""Interactive policy previews preserve real records without executing services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from control_plane.db import Store
from scripts.preview_policy_impacts import create_preview


class PolicyImpactPreviewTests(unittest.TestCase):
    def test_copy_manual_recheck_and_write_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = Store(root / 'source.db')
            try:
                project = source.create_project('https://github.com/Team-InfraMorph/demo-app', 'main', ['local'])
                did = source.begin_deploy(project['project_id'])
                source.set_target_status(did, 'local', 'LIVE')
                source.set_status(did, 'LIVE')
                before = list(source._conn.iterdump())
                app = create_preview(source.path, root / 'preview')
                try:
                    with patch('control_plane.policy_lifecycle.schedule', side_effect=AssertionError('no auto schedule')):
                        with TestClient(app) as client:
                            response = client.get('/api/policies/impacts').json()
                            self.assertEqual(response['review_context']['mode'], 'operational_copy')
                            self.assertTrue(response['review_context']['manual_rechecks'])
                            self.assertEqual(response['items'][0]['id'], did)
                            self.assertEqual(app.state.store._all('SELECT * FROM policy_jobs'), [])
                            for path in ['/api/deploy', '/api/projects', '/api/webhooks/github',
                                         f'/api/deployments/{did}/policy-redeploy',
                                         f'/api/deployments/{did}/retry', f'/api/deployments/{did}/verify']:
                                self.assertEqual(client.post(path, json={}).status_code, 409, path)
                            result = client.post(f'/api/deployments/{did}/policy-rechecks', json={'target':'local'})
                            self.assertEqual(result.status_code, 202, result.text)
                            item = client.get('/api/policies/impacts').json()['items'][0]
                            self.assertEqual(item['latest_recheck']['decision'], 'UNAVAILABLE')
                            self.assertEqual(item['status'], 'LIVE')
                            self.assertEqual(item['action'], 'unavailable')
                    self.assertEqual(list(source._conn.iterdump()), before)
                    self.assertTrue((root / 'preview/preview.json').is_file())
                    with self.assertRaises(FileExistsError):
                        create_preview(source.path, root / 'preview')
                finally:
                    app.state.store.close()
            finally:
                source.close()


if __name__ == '__main__':
    unittest.main()
