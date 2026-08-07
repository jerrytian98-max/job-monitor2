"""职位匹配模式的离线测试。"""

import unittest
import tempfile
from pathlib import Path

from database import JobDatabase
from matcher import JobMatcher


class MatcherModeTests(unittest.TestCase):
    def test_tracking_urls_are_only_reported_once_per_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = JobDatabase(str(Path(temp_dir) / 'jobs.db'))
            matcher = JobMatcher(
                {'job_keywords': [], 'cities': [], 'exclude_keywords': []},
                storage_file=str(Path(temp_dir) / 'jobs_cache.json'),
                database=database,
            )
            jobs = [
                {
                    'title': '同一职位',
                    'company': '阿里云',
                    'url': (
                        'https://careers.aliyun.com/off-campus/position-detail'
                        '?positionId=1001&track_id=first'
                    ),
                },
                {
                    'title': '同一职位',
                    'company': '阿里云',
                    'url': (
                        'https://careers.aliyun.com/off-campus/position-detail'
                        '?track_id=second&positionId=1001'
                    ),
                },
            ]

            self.assertEqual(matcher.filter_jobs(jobs), [jobs[0]])
            self.assertEqual(len(database.get_all_jobs()), 1)
            self.assertFalse(database.add_job(jobs[1]))

    def test_fixed_url_job_bypasses_global_keyword_filter(self):
        matcher = JobMatcher(
            {
                'job_keywords': ['法务'],
                'cities': [],
                'exclude_keywords': [],
            },
            persist=False,
        )
        job = {
            'title': '产品经理',
            'description': '负责招聘网址中指定的产品线',
            '_fixed_url_mode': True,
        }

        self.assertTrue(matcher.match_job(job))

    def test_fixed_url_job_still_obeys_exclusion_keywords(self):
        matcher = JobMatcher(
            {
                'job_keywords': ['法务'],
                'cities': [],
                'exclude_keywords': ['实习'],
            },
            persist=False,
        )
        job = {
            'title': '产品实习生',
            'description': '固定职位列表',
            '_fixed_url_mode': True,
        }

        self.assertFalse(matcher.match_job(job))


if __name__ == '__main__':
    unittest.main()
