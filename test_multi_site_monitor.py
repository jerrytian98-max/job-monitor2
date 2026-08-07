"""多网址监测循环和简洁工作日志的离线测试。"""

import unittest
from unittest.mock import patch

import app as web_app


class FakeMatcher:
    def __init__(self):
        self.marked = []

    def filter_jobs(self, jobs):
        return list(jobs)

    def mark_as_notified(self, jobs):
        self.marked.extend(jobs)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.calls = []

    def send_notification(self, jobs, site_label=None, new_jobs=None):
        notification_jobs = jobs if new_jobs is None else new_jobs
        self.calls.append(
            (site_label, list(jobs), list(notification_jobs))
        )
        self.sent.extend(jobs)
        return True


class FakeDatabase:
    def get_statistics(self):
        return {'total_jobs': 2, 'new_jobs_today': 2}


class MultiSiteMonitorTests(unittest.TestCase):
    def test_all_sites_run_and_one_failure_does_not_stop_the_loop(self):
        sites = [
            'https://jobs.example.com/search?keywords=one',
            'https://jobs.example.com/search?keywords=two',
            'https://jobs.example.com/search?keywords=three',
        ]
        labels = {
            sites[0]: '标签一',
            sites[1]: '标签二',
            sites[2]: '标签三',
        }
        factory_calls = []
        status_events = []
        found_jobs = []

        class FakeScraper:
            def __init__(self, site_url):
                self.site_url = site_url

            def scrape_jobs(self, site_url, keywords, cities):
                self.assert_url(site_url)
                if site_url == sites[1]:
                    raise RuntimeError('模拟单个网址失败')
                return [
                    {
                        'title': f'职位-{labels[site_url]}',
                        'company': '示例公司',
                        'url': f'{site_url}#job',
                    }
                ]

            def assert_url(self, site_url):
                if site_url != self.site_url:
                    raise AssertionError('抓取器收到错误网址')

        def fake_factory(site_url, status_callback=None, job_callback=None):
            factory_calls.append(site_url)
            return FakeScraper(site_url)

        def capture_status(message, status_type, work_log=False):
            status_events.append((message, status_type, work_log))

        monitor = web_app.WebJobMonitor.__new__(web_app.WebJobMonitor)
        monitor.config = {
            'job_sites': sites,
            'job_site_labels': labels,
            'job_keywords': [],
            'cities': [],
        }
        monitor.matcher = FakeMatcher()
        monitor.notifier = FakeNotifier()
        monitor.status_callback = capture_status
        monitor.job_callback = found_jobs.append

        with (
            patch.object(web_app, 'get_scraper_with_callback', fake_factory),
            patch.object(web_app, 'db', FakeDatabase()),
        ):
            success = monitor.check_jobs()

        self.assertTrue(success)
        self.assertEqual(factory_calls, sites)
        self.assertEqual(len(found_jobs), 2)
        self.assertEqual(len(monitor.notifier.sent), 2)
        self.assertEqual(
            [
                (label, len(jobs), len(new_jobs))
                for label, jobs, new_jobs in monitor.notifier.calls
            ],
            [(None, 2, 2)],
        )
        self.assertEqual(monitor.last_check_result['scraped_jobs'], 2)
        self.assertEqual(monitor.last_check_result['matched_jobs'], 2)
        self.assertTrue(monitor.last_check_result['notification_sent'])

        work_log_messages = [
            message
            for message, _, work_log in status_events
            if work_log
        ]
        self.assertEqual(
            work_log_messages,
            [
                '正在抓取【标签一】',
                '抓取完毕【标签一】找到1个新职位',
                '正在抓取【标签二】',
                '抓取完毕【标签二】找到0个新职位',
                '正在抓取【标签三】',
                '抓取完毕【标签三】找到1个新职位',
            ],
        )

    def test_no_email_when_all_sites_have_no_new_jobs(self):
        sites = [
            'https://jobs.example.com/search?keywords=one',
            'https://jobs.example.com/search?keywords=two',
        ]

        class EmptyScraper:
            def scrape_jobs(self, site_url, keywords, cities):
                return []

        monitor = web_app.WebJobMonitor.__new__(web_app.WebJobMonitor)
        monitor.config = {
            'job_sites': sites,
            'job_site_labels': {
                sites[0]: '标签一',
                sites[1]: '标签二',
            },
            'job_keywords': [],
            'cities': [],
        }
        monitor.matcher = FakeMatcher()
        monitor.notifier = FakeNotifier()
        monitor.status_callback = lambda *args: None
        monitor.job_callback = lambda job: None

        with (
            patch.object(
                web_app,
                'get_scraper_with_callback',
                lambda *args: EmptyScraper(),
            ),
            patch.object(web_app, 'db', FakeDatabase()),
        ):
            success = monitor.check_jobs()

        self.assertTrue(success)
        self.assertEqual(monitor.notifier.calls, [])
        self.assertFalse(monitor.last_check_result['notification_sent'])

    def test_result_label_combines_site_label_and_search_keyword(self):
        site = 'https://zhaopin.meituan.com/web/social'
        found_jobs = []

        class KeywordScraper:
            def scrape_jobs(self, site_url, keywords, cities):
                return [{
                    'title': '法务专家',
                    'company': '美团',
                    'url': 'https://zhaopin.meituan.com/web/position/detail?id=1',
                    '_search_keyword': '法务',
                }]

        monitor = web_app.WebJobMonitor.__new__(web_app.WebJobMonitor)
        monitor.config = {
            'job_sites': [site],
            'job_site_labels': {site: '美团'},
            'job_keywords': ['法务'],
            'cities': [],
        }
        monitor.matcher = FakeMatcher()
        monitor.notifier = FakeNotifier()
        monitor.status_callback = lambda *args: None
        monitor.job_callback = found_jobs.append

        with (
            patch.object(
                web_app,
                'get_scraper_with_callback',
                lambda *args: KeywordScraper(),
            ),
            patch.object(web_app, 'db', FakeDatabase()),
        ):
            success = monitor.check_jobs()

        self.assertTrue(success)
        self.assertEqual(found_jobs[0]['site_label'], '美团法务')

    def test_fixed_url_mode_skips_global_search_keywords_and_keeps_base_label(self):
        site = 'https://jobs.example.com/fixed-list?department=product'
        found_jobs = []
        scraper_calls = []

        class FixedUrlScraper:
            def scrape_jobs(self, site_url, keywords, cities, search_mode='search'):
                scraper_calls.append((site_url, list(keywords), search_mode))
                return [{
                    'title': '产品经理',
                    'company': '示例公司',
                    'url': 'https://jobs.example.com/job/1',
                    'description': '职位列表中的岗位',
                }]

        monitor = web_app.WebJobMonitor.__new__(web_app.WebJobMonitor)
        monitor.config = {
            'job_sites': [site],
            'job_site_labels': {site: '固定产品'},
            'job_site_modes': {site: 'fixed'},
            'job_keywords': ['法务'],
            'cities': [],
        }
        monitor.matcher = FakeMatcher()
        monitor.notifier = FakeNotifier()
        monitor.status_callback = lambda *args: None
        monitor.job_callback = found_jobs.append

        with (
            patch.object(
                web_app,
                'get_scraper_with_callback',
                lambda *args: FixedUrlScraper(),
            ),
            patch.object(web_app, 'db', FakeDatabase()),
        ):
            success = monitor.check_jobs()

        self.assertTrue(success)
        self.assertEqual(scraper_calls, [(site, [], 'fixed')])
        self.assertEqual(found_jobs[0]['site_label'], '固定产品')
        self.assertTrue(found_jobs[0]['_fixed_url_mode'])

    def test_email_contains_database_top_50_and_marks_current_jobs(self):
        current_job = {
            'title': '本轮新职位',
            'url': 'https://jobs.example.com/current',
            'site_label': '字节战略',
            'publish_time': '2026-07-26',
        }
        stored_jobs = [current_job] + [
            {
                'title': f'历史职位-{index}',
                'url': f'https://jobs.example.com/history/{index}',
                'site_label': '历史标签',
                'publish_time': f'2026-07-{25 - (index % 20):02d}',
            }
            for index in range(1, 60)
        ]

        class HistoryDatabase:
            def get_all_jobs(self, limit=None, offset=0):
                self.request = (limit, offset)
                return stored_jobs

        class SummaryMatcher(FakeMatcher):
            def __init__(self):
                super().__init__()
                self.persist = True
                self.database = HistoryDatabase()

        monitor = web_app.WebJobMonitor.__new__(web_app.WebJobMonitor)
        monitor.matcher = SummaryMatcher()
        monitor.notifier = FakeNotifier()

        success, notified_count, failed_labels = (
            monitor._send_notification_summary([current_job])
        )

        self.assertTrue(success)
        self.assertEqual(notified_count, 1)
        self.assertEqual(failed_labels, [])
        self.assertEqual(monitor.matcher.database.request, (50, 0))
        _, email_jobs, notification_jobs = monitor.notifier.calls[0]
        self.assertEqual(len(email_jobs), 50)
        self.assertEqual(notification_jobs, [current_job])
        self.assertTrue(email_jobs[0]['_is_new_this_run'])
        self.assertTrue(
            all(
                not job['_is_new_this_run']
                for job in email_jobs[1:]
            )
        )


if __name__ == '__main__':
    unittest.main()
