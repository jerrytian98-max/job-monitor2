"""职位匹配模式的离线测试。"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from database import JobDatabase
from matcher import JobMatcher


class MatcherModeTests(unittest.TestCase):
    def test_ai_filter_switch_preserves_saved_settings_without_calling_ai(self):
        settings = {
            'job_keywords': [], 'cities': [], 'exclude_keywords': [],
            'gemini_api_key': 'saved-key', 'ai_filter_prompt': '只保留战略职位',
            'ai_filter_enabled': False,
        }
        job = {'title': '战略分析', 'company': '示例', 'url': 'https://example.com/job/1'}
        matcher = JobMatcher(settings, persist=False)
        with patch('matcher.filter_jobs_with_ai') as ai_filter:
            self.assertEqual(matcher.filter_jobs([job]), [job])
        ai_filter.assert_not_called()

        settings.pop('ai_filter_enabled')
        matcher = JobMatcher(settings, persist=False)
        with patch('matcher.filter_jobs_with_ai', return_value=[job]) as ai_filter:
            self.assertEqual(matcher.filter_jobs([job]), [job])
        ai_filter.assert_called_once()

    def test_selected_provider_is_passed_to_ai_filter(self):
        job = {'title': '战略分析', 'company': '示例', 'url': 'https://example.com/job/2'}
        for provider, key_field, model_field in (
            ('openai', 'openai_api_key', 'openai_model'),
            ('deepseek', 'deepseek_api_key', 'deepseek_model'),
        ):
            with self.subTest(provider=provider):
                settings = {
                    'job_keywords': [], 'cities': [], 'exclude_keywords': [],
                    'ai_provider': provider, key_field: 'provider-key',
                    model_field: 'chosen-model', 'ai_filter_prompt': '只保留战略',
                    'ai_filter_enabled': True,
                }
                matcher = JobMatcher(settings, persist=False)
                with patch('matcher.filter_jobs_with_ai', return_value=[job]) as ai_filter:
                    matcher.filter_jobs([job])
                self.assertEqual(ai_filter.call_args.args[1:4],
                                 ('provider-key', '只保留战略', 'chosen-model'))
                self.assertEqual(ai_filter.call_args.kwargs['provider'], provider)

    def test_ai_sees_only_new_jobs_and_saves_only_selected_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = JobDatabase(str(Path(temp_dir) / 'jobs.db'))
            config = {
                'job_keywords': ['完全不在标题里的搜索词'],
                'cities': [], 'exclude_keywords': [],
                'ai_provider': 'openai', 'openai_api_key': 'test-key',
                'ai_filter_prompt': '只保留战略', 'ai_filter_enabled': True,
            }
            matcher = JobMatcher(config, storage_file=str(Path(temp_dir) / 'jobs_cache.json'),
                                 database=database)
            jobs = [
                {'title': '战略分析', 'description': '完整描述', 'company': '示例',
                 'url': 'https://example.com/job/strategy'},
                {'title': '法务', 'company': '示例', 'url': 'https://example.com/job/legal'},
            ]
            with patch('matcher.filter_jobs_with_ai', return_value=[jobs[0]]) as ai_filter:
                self.assertEqual(matcher.filter_jobs(jobs), [jobs[0]])
            self.assertEqual(ai_filter.call_args.args[0], jobs)
            self.assertEqual([job['title'] for job in database.get_all_jobs()], ['战略分析'])

            matcher = JobMatcher(config, storage_file=matcher.storage_file, database=database)
            with patch('matcher.filter_jobs_with_ai') as ai_filter:
                # The approved but not-yet-notified job is retried; the rejected
                # job is not billed again under the unchanged AI requirement.
                self.assertEqual(matcher.filter_jobs(jobs), [jobs[0]])
            ai_filter.assert_not_called()
            config['ai_filter_prompt'] = '只保留法务'
            matcher = JobMatcher(config, storage_file=matcher.storage_file, database=database)
            with patch('matcher.filter_jobs_with_ai', return_value=[jobs[1]]) as ai_filter:
                self.assertEqual(matcher.filter_jobs(jobs), jobs)
            self.assertEqual(ai_filter.call_args.args[0], [jobs[1]])
            self.assertEqual(len(database.get_all_jobs()), 2)

    def test_ai_failure_does_not_write_jobs_or_rejection_cache(self):
        from ai_filter import AIAnalysisError

        with tempfile.TemporaryDirectory() as temp_dir:
            database = JobDatabase(str(Path(temp_dir) / 'jobs.db'))
            matcher = JobMatcher({
                'ai_provider': 'openai', 'openai_api_key': 'bad-key',
                'ai_filter_prompt': '筛选', 'ai_filter_enabled': True,
            }, storage_file=str(Path(temp_dir) / 'jobs_cache.json'), database=database)
            job = {'title': '职位', 'company': '示例', 'url': 'https://example.com/job/1'}
            with patch('matcher.filter_jobs_with_ai', side_effect=AIAnalysisError('failed')):
                with self.assertRaises(AIAnalysisError):
                    matcher.filter_jobs([job])
            self.assertEqual(database.get_all_jobs(), [])
            self.assertEqual(matcher.known_jobs, {})

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

    def test_city_filter_accepts_partial_multi_city_and_unknown_locations(self):
        matcher = JobMatcher(
            {
                'job_keywords': [],
                'cities': ['北京'],
                'exclude_keywords': [],
            },
            persist=False,
        )

        self.assertTrue(matcher.match_job({'title': '法务', 'city': '北京市'}))
        self.assertTrue(matcher.match_job({'title': '法务', 'city': '北京、上海'}))
        self.assertFalse(matcher.match_job({'title': '法务', 'city': '杭州市'}))
        self.assertTrue(matcher.match_job({'title': '法务', 'city': '未知'}))
        self.assertTrue(matcher.match_job({'title': '法务', 'city': '全国'}))

    def test_exclusion_scans_scraped_fields_case_insensitively_but_not_url(self):
        matcher = JobMatcher(
            {
                'job_keywords': [],
                'cities': [],
                'exclude_keywords': ['实习', 'sales'],
            },
            persist=False,
        )

        self.assertFalse(matcher.match_job({
            'title': '法务专员',
            'company': 'Example SALES Team',
        }))
        self.assertFalse(matcher.match_job({
            'title': '法务专员',
            'site_label': '字节实习',
        }))
        self.assertTrue(matcher.match_job({
            'title': '法务专员',
            'description': '正式岗位',
            'url': 'https://example.com/intern/jobs/1',
        }))

    def test_city_and_exclusion_constraints_run_before_ai_filter(self):
        matcher = JobMatcher(
            {
                'job_keywords': ['这个词不应在启用AI时预先过滤'],
                'cities': ['北京'],
                'exclude_keywords': ['实习'],
                'ai_provider': 'openai',
                'openai_api_key': 'test-key',
                'openai_model': 'test-model',
                'ai_filter_prompt': '只保留合适职位',
                'ai_filter_enabled': True,
            },
            persist=False,
        )
        allowed = {
            'title': '法务专员', 'city': '北京市',
            'url': 'https://example.com/jobs/allowed',
        }
        excluded = {
            'title': '法务实习生', 'city': '北京市',
            'url': 'https://example.com/jobs/excluded',
        }
        wrong_city = {
            'title': '法务专员', 'city': '杭州市',
            'url': 'https://example.com/jobs/wrong-city',
        }

        with patch('matcher.filter_jobs_with_ai', return_value=[allowed]) as ai_filter:
            self.assertEqual(
                matcher.filter_jobs([allowed, excluded, wrong_city]),
                [allowed],
            )
        self.assertEqual(ai_filter.call_args.args[0], [allowed])


if __name__ == '__main__':
    unittest.main()
