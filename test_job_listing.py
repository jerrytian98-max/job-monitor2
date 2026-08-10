"""职位列表排序、网址标签与筛选的离线测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import app as web_app
from database import JobDatabase


class JobListingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = JobDatabase(str(Path(self.temp_dir.name) / 'jobs.db'))
        self.config_path = Path(self.temp_dir.name) / 'config.yaml'
        self.target_url = (
            'https://jobs.bytedance.com/experienced/position/list'
            '?keywords=%E6%B3%95%E5%8A%A1'
        )
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    'job_sites': [self.target_url],
                    'job_site_labels': {self.target_url: '法务'},
                },
                allow_unicode=True,
            ),
            encoding='utf-8',
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_job(self, title, publish_time, site_label='', source_site='jobs.bytedance.com'):
        self.assertTrue(
            self.db.add_job(
                {
                    'title': title,
                    'company': '示例公司',
                    'salary': '面议',
                    'city': '北京',
                    'description': f'{title}的职位描述',
                    'url': f'https://jobs.example.com/{title}',
                    'source_site': source_site,
                    'site_label': site_label,
                    'publish_time': publish_time,
                }
            )
        )

    def test_jobs_are_sorted_by_publish_date_descending(self):
        self.add_job('较早职位', '2026-01-02', '法务')
        self.add_job('最新职位', '2026-03-10', '法务')
        self.add_job('中间职位', '2026-02-05', '法务')

        jobs = self.db.get_all_jobs()

        self.assertEqual(
            [job['title'] for job in jobs],
            ['最新职位', '中间职位', '较早职位'],
        )

    def test_unknown_publish_date_uses_first_found_time_for_sorting(self):
        self.add_job('发布日期职位', '2026-03-10', '法务')
        self.add_job('较新发现职位', '未知', '法务')
        self.add_job('较早发现职位', '', '法务')

        conn = self.db._get_connection()
        try:
            conn.execute(
                "UPDATE jobs SET found_time = ? WHERE title = ?",
                ('2026-04-01 09:00:00', '较新发现职位'),
            )
            conn.execute(
                "UPDATE jobs SET found_time = ? WHERE title = ?",
                ('2026-02-01 09:00:00', '较早发现职位'),
            )
            conn.commit()
        finally:
            conn.close()

        jobs = self.db.get_all_jobs()

        self.assertEqual(
            [job['title'] for job in jobs],
            ['较新发现职位', '发布日期职位', '较早发现职位'],
        )
        self.assertEqual(
            next(job for job in jobs if job['title'] == '较早发现职位')['publish_time'],
            '未知',
        )

    def test_site_label_filter_matches_partial_text(self):
        self.add_job('法务职位', '2026-03-10', '法务合规')
        self.add_job('战略职位', '2026-03-09', '公司战略')

        jobs = self.db.get_all_jobs(site_label_keyword='法务')

        self.assertEqual([job['title'] for job in jobs], ['法务职位'])
        self.assertEqual(self.db.count_jobs(site_label_keyword='法务'), 1)

    def test_existing_job_keeps_the_first_captured_keyword_label(self):
        self.add_job('重叠职位', '2026-03-10', '美团法律')
        job = {
            'title': '重叠职位',
            'url': 'https://jobs.example.com/重叠职位',
            'site_label': '美团法务',
        }

        self.assertFalse(self.db.update_job_site_label(job))
        self.assertEqual(self.db.count_jobs(site_label_keyword='美团法律'), 1)
        self.assertEqual(self.db.count_jobs(site_label_keyword='美团法务'), 0)
        self.assertEqual(
            self.db.get_all_jobs()[0]['site_label'],
            '美团法律',
        )

    def test_legacy_combined_label_is_reduced_to_the_first_keyword(self):
        self.add_job('旧标签职位', '2026-03-10', '腾讯法律 / 腾讯法务')

        # Database startup migration also normalizes previously saved cards.
        self.db._init_db()

        self.assertEqual(
            self.db.get_all_jobs()[0]['site_label'],
            '腾讯法律',
        )

    def test_api_backfills_old_rows_and_filters_by_configured_label(self):
        self.add_job('旧职位', '2026-03-10')

        with (
            patch.object(web_app, 'db', self.db),
            patch.object(web_app, 'CONFIG_FILE', str(self.config_path)),
        ):
            response = web_app.app.test_client().get(
                '/api/jobs',
                query_string={'site_label': '法'},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['pagination']['total'], 1)
        self.assertEqual(payload['data'][0]['site_label'], '法务')


if __name__ == '__main__':
    unittest.main()
