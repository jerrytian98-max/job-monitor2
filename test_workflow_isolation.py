from pathlib import Path
import unittest


WORKFLOW_DIR = Path(__file__).resolve().parent / ".github" / "workflows"


class WorkflowIsolationTests(unittest.TestCase):
    def test_each_user_has_a_separate_cloud_workflow_and_state(self):
        expected = {
            "user1_monitor.yml": {
                "secret": "JOB_MONITOR_USER1_CONFIG_B64",
                "artifact": "job-monitor-state-user1",
                "cache": "job-monitor-state-user1-",
                "state_files": ("jobs.db", "jobs_cache.json"),
            },
            "user2_monitor.yml": {
                "secret": "JOB_MONITOR_USER2_CONFIG_B64",
                "artifact": "job-monitor-state-user2",
                "cache": "job-monitor-state-user2-",
                "state_files": ("jobs_user2.db", "jobs_cache_user2.json"),
            },
        }
        self.assertEqual(
            {path.name for path in WORKFLOW_DIR.glob("*.yml")},
            set(expected),
        )
        for filename, required in expected.items():
            content = (WORKFLOW_DIR / filename).read_text(encoding="utf-8")
            self.assertIn(required["secret"], content)
            self.assertIn(required["artifact"], content)
            self.assertIn(required["cache"], content)
            self.assertIn("0 13 * * 2,5", content)
            for state_file in required["state_files"]:
                self.assertIn(state_file, content)


if __name__ == "__main__":
    unittest.main()
