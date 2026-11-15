"""Offline browser regression checks for configuration save recovery."""

from collections import deque
from copy import deepcopy
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

from jinja2 import Environment, FileSystemLoader


BASE_DIR = Path(__file__).resolve().parent


class FrontendAutosaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        cls.playwright = sync_playwright().start()
        options = {'headless': True}
        if os.name == 'nt':
            edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
            if edge.exists():
                options['executable_path'] = str(edge)
        try:
            cls.browser = cls.playwright.chromium.launch(**options)
        except Exception as error:
            cls.playwright.stop()
            raise unittest.SkipTest(f'Browser unavailable: {error}')
        environment = Environment(loader=FileSystemLoader(BASE_DIR / 'templates'), autoescape=True)
        cls.html = environment.get_template('index.html').render(
            url_for=lambda endpoint, **values: (
                '/static/' + values.pop('filename') + '?' + urlencode(values)
                if endpoint == 'static' else '/' + endpoint
            ), config={}, status={}, cloud_mode=True, csrf_token='test-csrf',
            profile_display_name='Test', email_verified=True, is_admin=False,
        )
        cls.javascript = (BASE_DIR / 'static/js/app.js').read_text(encoding='utf-8')
        cls.stylesheet = (BASE_DIR / 'static/css/workspace.css').read_text(encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.config = {
            'job_sites': [], 'job_site_labels': {}, 'job_site_modes': {},
            'job_keywords': [], 'cities': [], 'exclude_keywords': [],
            'email': {'receiver': 'test@example.com'}, 'ai_provider': 'gemini',
            'check_interval': 12,
            'ai_filter_enabled': False, 'ai_filter_prompt': '',
            'local_schedule': {'enabled': True, 'days': [1, 4], 'time': '21:00'},
        }
        self.failures = deque()
        self.requests = []
        self.pending_routes = []
        self.monitor_status = {'status': 'stopped', 'is_monitoring': False, 'total_jobs_found': 357, 'new_jobs_today': 0}
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.route('**/*', self.route_request)
        self.page.goto('http://job-monitor.test/', wait_until='domcontentloaded')
        self.wait_saved()
        self.assertEqual(self.requests, [])

    def tearDown(self):
        for route in self.pending_routes:
            route.abort()
        self.context.close()

    def route_request(self, route):
        request = route.request
        path = urlparse(request.url).path
        if path == '/':
            route.fulfill(content_type='text/html', body=self.html)
        elif path == '/static/js/app.js':
            route.fulfill(content_type='text/javascript', body=self.javascript)
        elif path == '/static/css/workspace.css':
            route.fulfill(content_type='text/css', body=self.stylesheet)
        elif path == '/api/config' and request.method == 'POST':
            self.assertEqual(request.headers.get('x-csrf-token'), 'test-csrf')
            value = request.post_data_json
            self.requests.append(deepcopy(value))
            failure = self.failures.popleft() if self.failures else None
            if failure == 'network':
                route.abort('failed')
            elif failure == 'timeout':
                self.pending_routes.append(route)
            elif failure == 400:
                route.fulfill(status=400, json={'success': False, 'message': '请至少选择一个星期'})
            elif failure:
                route.fulfill(status=failure, content_type='text/html', body='<html>Request failed</html>')
            else:
                self.config = deepcopy(value)
                route.fulfill(json={'success': True})
        elif path == '/api/config':
            route.fulfill(json={'success': True, 'data': self.config})
        elif path == '/api/jobs':
            route.fulfill(json={'success': True, 'data': [], 'pagination': {'page': 1, 'total': 0, 'total_pages': 0}})
        elif path == '/api/events':
            route.fulfill(content_type='text/event-stream', body=': heartbeat\n\n')
        elif path == '/api/monitor/status':
            route.fulfill(json={'success': True, 'data': self.monitor_status})
        elif 'bootstrap.bundle' in path:
            route.fulfill(content_type='text/javascript', body='window.bootstrap={Modal:class{show(){} hide(){}}};')
        elif path.startswith('/api/'):
            route.fulfill(json={'success': True, 'data': {'status': 'stopped', 'is_monitoring': False}})
        else:
            route.fulfill(body='')

    def wait_saved(self):
        self.page.wait_for_function("document.querySelector('.autosave-status').dataset.state === 'saved'", timeout=10000)

    def wait_error(self):
        self.page.wait_for_function("document.querySelector('.autosave-status').dataset.state === 'error'", timeout=10000)

    def test_job_filters_allow_selecting_and_restoring_options(self):
        observed = []
        jobs = [
            {'title': '产品职位', 'company': '腾讯科技', 'recruitment_site_tag': '腾讯', 'matched_keyword': '产品', 'capture_mode': 'search'},
            {'title': '直抓职位', 'company': '字节跳动', 'recruitment_site_tag': '字节', 'matched_keyword': '', 'capture_mode': 'fixed'},
            {'title': '未知职位', 'company': '原始公司', 'recruitment_site_tag': '小红书', 'matched_keyword': '', 'capture_mode': 'unknown'},
        ]

        def jobs_route(route):
            query = parse_qs(urlparse(route.request.url).query, keep_blank_values=True)
            observed.append(query)
            result = [job for job in jobs if job['recruitment_site_tag'] not in query.get('exclude_site', [])
                      and (job['capture_mode'] == 'fixed' or job['matched_keyword'] not in query.get('exclude_keyword', []))]
            route.fulfill(json={'success': True, 'data': result,
                                'filters': {'sites': ['腾讯', '字节', '小红书'], 'keywords': ['', '产品']},
                                'pagination': {'page': 1, 'total': len(result), 'total_pages': 1}})

        self.page.route('**/api/jobs?*', jobs_route)
        self.page.evaluate("document.getElementById('jobsPage').hidden = false; loadJobs(1)")
        self.page.wait_for_function("document.querySelectorAll('#siteTagFilters .job-filter-chip').length === 3")
        self.assertEqual(self.page.locator('.job-site-label').all_text_contents(), ['腾讯', '字节', '小红书'])
        collapsed_height = self.page.locator('.jobs-filters').bounding_box()['height']
        self.page.get_by_role('button', name='选择招聘网址').click()
        self.assertGreater(self.page.locator('.jobs-filters').bounding_box()['height'], collapsed_height)
        self.assertTrue(self.page.locator('#siteTagMenu').evaluate("""menu => {
            const card = menu.closest('.jobs-filters').getBoundingClientRect();
            const bounds = menu.getBoundingClientRect();
            const options = menu.querySelector('.job-filter-options');
            return bounds.bottom <= card.bottom && options.scrollHeight <= options.clientHeight;
        }"""))
        self.assertEqual(self.page.locator('#siteTagOptions input:checked').count(), 3)
        self.page.locator('#siteTagMenu').get_by_role('button', name='清空').click()
        self.page.wait_for_function("document.querySelectorAll('#siteTagOptions input:checked').length === 0")
        self.page.locator('#siteTagOptions label', has_text='腾讯').locator('input').check()
        self.page.wait_for_function("document.querySelectorAll('#jobsList .job-item').length === 1")
        self.assertIn('产品职位', self.page.locator('#jobsList').inner_text())
        self.assertCountEqual(observed[-1]['exclude_site'], ['字节', '小红书'])
        self.page.locator('#siteTagMenu').get_by_role('button', name='全选').click()
        self.page.wait_for_function("document.querySelectorAll('#jobsList .job-item').length === 3")
        self.page.get_by_role('button', name='选择招聘网址').click()
        self.page.get_by_role('button', name='选择关键词').click()
        self.assertTrue(self.page.locator('#keywordTagMenu').evaluate("""menu =>
            menu.getBoundingClientRect().bottom <= menu.closest('.jobs-filters').getBoundingClientRect().bottom
        """))
        self.page.locator('#keywordTagOptions label', has_text='产品').locator('input').uncheck()
        self.page.wait_for_function("document.querySelectorAll('#jobsList .job-item').length === 2")
        self.assertIn('直抓职位', self.page.locator('#jobsList').inner_text())
        self.assertIn('-', self.page.locator('#jobsList .job-keyword-label').inner_text())
        self.page.keyboard.press('Escape')
        self.assertTrue(self.page.locator('#keywordTagMenu').is_hidden())
        for width in (1440, 390):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))

    def test_job_cards_keep_their_row_and_open_one_full_width_detail(self):
        jobs = [
            {'id': index, 'title': f'战略岗位 {index}', 'company': '示例公司',
             'recruitment_site_tag': '腾讯', 'matched_keyword': '战略', 'capture_mode': 'search',
             'city': '北京市', 'salary': '面议', 'description': '负责产品与业务战略。',
             'url': f'https://jobs.example.com/{index}', 'publish_time': '2026-11-01',
             'found_time': '2026-11-02 10:00:00'} for index in range(24)
        ]
        self.page.evaluate("""jobs => {
            document.querySelector('#overviewPage').hidden = true;
            document.querySelector('#jobsPage').hidden = false;
            renderJobs(jobs, {page: 1, per_page: 24, total: 1200, total_pages: 50});
        }""", jobs)
        self.assertEqual(self.page.locator('.job-item').count(), 24)
        self.assertEqual(self.page.locator('#jobsResultCount').inner_text(), '共 1200 个职位')
        self.assertIn('每页 24 条', self.page.locator('#jobsPageSummary').inner_text())
        self.assertLess(self.page.locator('#pagination .page-link').count(), 10)
        for width, columns in ((1440, 3), (1024, 2), (390, 1)):
            self.page.set_viewport_size({'width': width, 'height': 900})
            cards = self.page.locator('.job-item')
            self.assertEqual(len({round(cards.nth(index).bounding_box()['y']) for index in range(columns)}), 1)
            if columns < 3:
                self.assertGreater(cards.nth(columns).bounding_box()['y'], cards.first.bounding_box()['y'])
            cards.nth(1).locator('.job-summary').click()
            panel = self.page.locator('.job-expanded-panel')
            self.assertEqual(panel.count(), 1)
            self.assertTrue(panel.locator('.job-details').is_visible())
            self.assertEqual(cards.nth(1).locator('.job-summary').get_attribute('aria-expanded'), 'true')
            self.assertEqual(panel.evaluate('node => node.previousElementSibling.classList.contains("job-item")'), True)
            self.assertLess(abs(panel.bounding_box()['width'] - self.page.locator('#jobsList').bounding_box()['width']), 2)
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            cards.nth(2).locator('.job-summary').click()
            self.assertEqual(panel.count(), 1)
            self.assertEqual(cards.nth(1).locator('.job-summary').get_attribute('aria-expanded'), 'false')
            panel.locator('.job-detail-close').click()
            self.assertEqual(panel.count(), 0)
            self.assertTrue(cards.nth(2).locator('.job-summary').evaluate('node => node === document.activeElement'))
        self.page.evaluate("renderJobs([], {page: 1, total: 0, total_pages: 0})")
        self.assertEqual(self.page.locator('#jobsResultCount').inner_text(), '0 个职位')
        self.assertTrue(self.page.locator('#paginationNav').is_hidden())

    def test_site_chips_expand_below_the_selected_row_and_follow_resize(self):
        items = [{'url': f'https://jobs.example.com/{index}', 'label': f'公司 {index}', 'mode': 'search'} for index in range(21)]
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.page.evaluate('items => renderSites(items)', items)
        self.assertEqual(self.page.locator('#sitesList .site-chip').count(), 21)
        self.assertEqual(self.page.locator('.site-editor:visible').count(), 0)
        for width in (1440, 390, 1920):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            if self.page.locator('.site-editor:visible').count():
                self.page.keyboard.press('Escape')
            self.page.locator('.site-chip[data-site-index="1"] .site-chip-toggle').click()
            self.page.wait_for_function('''() => {
                const chip = document.querySelector('.site-chip[data-site-index="1"]');
                const panel = document.querySelector('.site-editor[data-site-index="1"]');
                const rect = panel.getBoundingClientRect();
                const row = [...document.querySelectorAll('.site-chip')].filter(item => Math.abs(item.getBoundingClientRect().top - chip.getBoundingClientRect().top) < 2);
                return !panel.hidden && panel.previousElementSibling === row.at(-1) && rect.top >= chip.getBoundingClientRect().bottom;
            }''')
            self.assertEqual(self.page.locator('.site-editor:visible').count(), 1)
            self.assertTrue(self.page.locator('#sitesList').evaluate('element => element.scrollWidth <= element.clientWidth'))
            self.assertTrue(self.page.locator('.site-editor:visible').evaluate('''element => {
                const controls = [element.querySelector('.site-url-input'), element.querySelector('.site-mode-picker'), element.querySelector('.site-actions')].map(item => item.getBoundingClientRect());
                return Math.max(...controls.map(item => item.top)) - Math.min(...controls.map(item => item.top)) < 3;
            }'''))
        self.page.set_viewport_size({'width': 390, 'height': 1000})
        self.page.wait_for_function('''() => {
            const panel=document.querySelector('.site-editor:not([hidden])');
            return panel.previousElementSibling.dataset.siteIndex === '1';
        }''')
        self.page.keyboard.press('Escape')
        self.assertEqual(self.page.locator('.site-editor:visible').count(), 0)
        self.assertEqual(self.requests, [])

    def test_site_chip_edits_copy_delete_and_save_preserve_the_latest_values(self):
        self.page.evaluate("renderSites([{url:'https://jobs.example.com/?keywords=产品', label:'示例产品', mode:'search'}])")
        self.page.locator('.site-chip-toggle').click()
        self.page.locator('.site-chip .site-label-input').fill('示例产品经理')
        self.page.locator('.site-editor:visible .site-mode-picker > summary').click()
        self.page.locator('.site-editor:visible .ui-select-option').nth(1).click()
        self.wait_saved()
        self.assertEqual(self.requests[-1]['job_site_labels']['https://jobs.example.com/?keywords=产品'], '示例产品经理')
        self.assertEqual(self.requests[-1]['job_site_modes']['https://jobs.example.com/?keywords=产品'], 'fixed')
        self.page.locator('.site-editor:visible .duplicate-btn').click()
        self.assertEqual(self.page.locator('.site-chip').count(), 2)
        self.assertTrue(self.page.locator('.site-editor[data-site-index="1"]').is_visible())
        copied = self.page.locator('.site-editor:visible .site-url-input')
        copied.fill('https://jobs.example.com/?keywords=经理')
        self.wait_saved()
        self.assertEqual(len(self.requests[-1]['job_sites']), 2)
        self.page.locator('.site-editor:visible .delete-btn').click()
        self.wait_saved()
        self.assertEqual(self.requests[-1]['job_sites'], ['https://jobs.example.com/?keywords=产品'])
        self.page.locator('.site-chip-toggle').click()
        self.assertEqual(self.page.locator('.site-chip .site-label-input').input_value(), '示例产品经理')

    def test_overview_shows_one_state_and_progressive_details(self):
        self.page.evaluate('updateStatus()')
        self.assertEqual(self.page.locator('#totalJobs').inner_text(), '357')
        self.assertEqual(self.page.locator('#lastCheck').inner_text(), '尚未检查')
        self.assertTrue(self.page.locator('#stopBtn').is_hidden())
        self.assertFalse(self.page.locator('#activity').evaluate('element => element.open'))
        self.assertEqual(self.page.locator('#progressBar').count(), 0)
        self.monitor_status.update(is_monitoring=True, status='running', running_time=None)
        self.page.evaluate('updateStatus()')
        self.assertEqual(self.page.locator('#statusText').inner_text(), '等待检查')
        self.assertTrue(self.page.locator('#startBtn').is_disabled())
        self.assertTrue(self.page.locator('#stopBtn').is_visible())
        self.monitor_status['running_time'] = '2026-10-29 07:00:00'
        self.page.evaluate('updateStatus()')
        self.assertEqual(self.page.locator('#statusText').inner_text(), '正在检查职位')
        self.page.evaluate("handleStatusEvent({data:{message:'正在检查示例网站', type:'info', work_log:true}})")
        self.assertEqual(self.page.locator('#statusDetail').inner_text(), '正在检查示例网站')
        self.page.locator('#activity > summary').click()
        self.assertIn('正在检查示例网站', self.page.locator('#workLog').inner_text())
        self.assertNotIn('开始检查后', self.page.locator('#workLog').inner_text())
        self.monitor_status['status'] = 'stopping'
        self.page.evaluate('updateStatus()')
        self.assertTrue(self.page.locator('#stopBtn').is_disabled())
        self.monitor_status.update(is_monitoring=False, status='scheduled', scheduled=True, last_check='2026-10-29 07:27:34')
        self.page.evaluate('updateStatus()')
        self.assertEqual(self.page.locator('#statusText').inner_text(), '定时监测已开启')
        self.assertIn('15:27', self.page.locator('#lastCheck').get_attribute('title'))
        self.assertTrue(self.page.locator('#stopBtn').is_hidden())
        self.assertTrue(self.page.locator('#monitorRuntime').is_hidden())

    def test_unselected_schedule_remains_off_after_manual_run(self):
        self.config['local_schedule'] = {'enabled': False, 'days': [], 'time': '21:00'}
        self.page.reload(wait_until='domcontentloaded')
        self.wait_saved()
        self.assertFalse(self.page.locator('#scheduleEnabled').is_checked())
        self.assertEqual(self.page.locator('[name="scheduleDay"]:checked').count(), 0)
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_saved()
        self.assertEqual(self.config['local_schedule'], {'enabled': False, 'days': [], 'time': '22:00'})
        self.page.evaluate('startMonitor()')
        self.assertFalse(self.page.locator('#scheduleEnabled').is_checked())
        self.assertEqual(self.page.locator('[name="scheduleDay"]:checked').count(), 0)

    def test_gateway_failure_retries_and_survives_reload(self):
        self.failures.append(502)
        self.page.locator('#scheduleTime').fill('22:00')
        self.page.wait_for_function("document.querySelector('.autosave-status').textContent.includes('自动重试')")
        self.wait_saved()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(value['check_interval'] == 12 for value in self.requests))
        self.page.reload(wait_until='domcontentloaded')
        self.wait_saved()
        self.assertEqual(self.page.locator('#scheduleTime').input_value(), '22:00')

    def test_network_disconnect_retries_without_another_edit(self):
        self.failures.append('network')
        self.page.locator('#scheduleTime').fill('22:30')
        self.page.wait_for_function("document.querySelector('.autosave-status').textContent.includes('自动重试')")
        self.wait_saved()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.config['local_schedule']['time'], '22:30')

    def test_edit_during_retry_saves_the_latest_value(self):
        self.failures.append(502)
        self.page.locator('#scheduleTime').fill('22:00')
        self.page.wait_for_function("document.querySelector('.autosave-status').textContent.includes('自动重试')")
        self.page.locator('#scheduleTime').fill('23:00')
        self.wait_saved()
        self.assertEqual([value['local_schedule']['time'] for value in self.requests], ['22:00', '23:00'])

    def test_validation_error_waits_for_a_correction(self):
        self.failures.append(400)
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_error()
        self.assertIn('请至少选择一个星期', self.page.locator('.autosave-status').first.inner_text())
        self.page.wait_for_timeout(2200)
        self.assertEqual(len(self.requests), 1)
        self.page.locator('#scheduleTime').fill('23:00')
        self.wait_saved()
        self.assertEqual(self.config['local_schedule']['time'], '23:00')

    def test_expired_session_is_not_reported_as_a_network_error(self):
        self.failures.append(401)
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_error()
        self.assertIn('请重新登录', self.page.locator('.autosave-status').first.inner_text())
        self.assertTrue(self.page.locator('.autosave-retry').first.is_hidden())

    def test_expired_csrf_does_not_retry_automatically(self):
        self.failures.append(403)
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_error()
        self.assertIn('请刷新页面后重试', self.page.locator('.autosave-status').first.inner_text())
        self.page.wait_for_timeout(2200)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.page.locator('.autosave-retry').first.is_hidden())

    def test_request_timeout_retries(self):
        self.page.evaluate('''() => {
            const originalSetTimeout = window.setTimeout;
            window.setTimeout = (callback, delay, ...args) =>
                originalSetTimeout(callback, delay === 30000 ? 100 : delay, ...args);
        }''')
        self.failures.append('timeout')
        self.page.locator('#scheduleTime').fill('22:00')
        self.page.wait_for_function("document.querySelector('.autosave-status').textContent.includes('自动重试')")
        self.wait_saved()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.config['local_schedule']['time'], '22:00')

    def test_connection_recovery_retries_dirty_configuration(self):
        self.page.evaluate('AUTOSAVE_RETRY_DELAYS.splice(0)')
        self.failures.append('network')
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_error()
        self.page.evaluate("window.dispatchEvent(new Event('online'))")
        self.wait_saved()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.config['local_schedule']['time'], '22:00')

    def test_exhausted_retries_can_be_retried_without_editing(self):
        self.page.evaluate('AUTOSAVE_RETRY_DELAYS.splice(0, AUTOSAVE_RETRY_DELAYS.length, 20, 40)')
        self.failures.extend([502, 502, 502])
        self.page.locator('#scheduleTime').fill('22:00')
        self.wait_error()
        self.assertEqual(len(self.requests), 3)
        self.page.locator('.autosave-retry').first.click()
        self.wait_saved()
        self.assertEqual(self.config['local_schedule']['time'], '22:00')
        self.assertTrue(self.page.locator('.autosave-retry').first.is_hidden())


if __name__ == '__main__':
    unittest.main()
