"""Check the real reusable workflow's profile/path and secret boundaries."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


BASE_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = BASE_DIR / '.github' / 'workflows'


class WorkflowIsolationTests(unittest.TestCase):
    def test_each_user_has_a_separate_cloud_workflow_and_state(self):
        shared = yaml.safe_load((WORKFLOW_DIR / 'monitor_profile.yml').read_text(encoding='utf-8'))
        trigger = shared.get('on', shared.get(True))
        self.assertIn('workflow_call', trigger)
        steps = shared['jobs']['monitor']['steps']
        resolver = next(step for step in steps if step.get('id') == 'profile')['run']
        script = resolver.split("\n", 1)[1].rsplit("\nPY", 1)[0]
        outputs = []
        for number, profile, config, state in (
            (1, '', 'config.yaml', ('jobs.db', 'jobs_cache.json')),
            (2, 'user2', 'config_user2.yaml', ('jobs_user2.db', 'jobs_cache_user2.json')),
        ):
            wrapper = yaml.safe_load((WORKFLOW_DIR / f'user{number}_monitor.yml').read_text(encoding='utf-8'))
            self.assertEqual(wrapper.get('on', wrapper.get(True))['schedule'][0]['cron'], '0 13 * * 2,5')
            job = wrapper['jobs']['monitor']
            self.assertEqual(job['uses'], './.github/workflows/monitor_profile.yml')
            self.assertEqual(job['with']['profile'], profile)
            self.assertIn(f'JOB_MONITOR_USER{number}_CONFIG_B64', job['secrets']['config_b64'])
            for provider in ('gemini', 'openai', 'deepseek'):
                self.assertIn(f'{provider.upper()}_API_KEY_USER{number}', job['secrets'][provider + '_api_key'])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'outputs'
                result = subprocess.run([sys.executable, '-c', script], cwd=BASE_DIR,
                                        env=dict(os.environ, JOB_PROFILE=profile, GITHUB_OUTPUT=str(path)),
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(line.split('=', 1) for line in path.read_text().splitlines())
            self.assertEqual(values['config'], config)
            self.assertEqual((values['database'], values['cache']), state)
            self.assertEqual(values['artifact'], f'job-monitor-state-user{number}')
            self.assertEqual(values['cache_prefix'], f'job-monitor-state-user{number}-')
            outputs.append(values)
        self.assertNotEqual(outputs[0]['artifact'], outputs[1]['artifact'])
        state_actions = [step for step in steps if step.get('uses', '').startswith(('actions/cache/', 'actions/upload-artifact'))]
        self.assertEqual(len(state_actions), 3)
        for step in state_actions:
            self.assertEqual(step['with']['path'].splitlines(),
                             ['${{ steps.profile.outputs.database }}', '${{ steps.profile.outputs.cache }}'])


if __name__ == '__main__':
    unittest.main()
