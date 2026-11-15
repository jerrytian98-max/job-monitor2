"""Account and tenant isolation checks without network scraping or email."""

import os
import json
import io
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash
import yaml


class CloudPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {'JOB_MONITOR_CLOUD_DATA_DIR': cls.temp.name})
        cls.env.start()
        import cloud_portal
        cls.portal = cloud_portal
        cls.app = cloud_portal.app
        cls.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        with self.portal.connect() as conn:
            conn.execute('DELETE FROM auth_attempts')
            conn.execute('DELETE FROM email_verifications')

    def _client(self, username, password='long-password-123'):
        address = f'{username}@example.com'
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = username + '-csrf'
        sent = []
        with patch.object(self.portal, 'load_admin_email', return_value={
            'sender': 'admin@example.com', 'auth_code': 'fake',
            'smtp_server': 'smtp.example.com', 'smtp_port': 465,
        }), patch.object(self.portal, 'send_verification_code', side_effect=lambda _, code, __: sent.append(code) or True):
            response = client.post('/register/code', data={
                'email': address, 'csrf_token': username + '-csrf',
            })
        self.assertEqual(response.status_code, 200)
        result = client.post('/register', data={
            'email': address, 'password': password, 'code': sent[0],
            'csrf_token': username + '-csrf',
        })
        self.assertEqual(result.status_code, 302)
        with client.session_transaction() as session:
            csrf = session['csrf']
        with self.portal.connect() as conn:
            user_id = conn.execute('SELECT id FROM users WHERE username=?', (address,)).fetchone()['id']
            row = conn.execute('SELECT email,email_verified FROM users WHERE id=?', (user_id,)).fetchone()
        self.assertEqual((row['email'], row['email_verified']), (address, 1))
        self.assertEqual(self.portal.load_config(user_id)['email']['receiver'], address)
        return client, csrf, user_id

    def test_new_account_persists_recruitment_site_defaults(self):
        client, _, user_id = self._client('default-sites')
        template = yaml.safe_load(
            (self.portal.BASE_DIR / 'config.example.yaml').read_text(encoding='utf-8')
        )
        saved = yaml.safe_load(self.portal.config_path(user_id).read_text(encoding='utf-8'))
        public = client.get('/api/config').json['data']
        for key in ('job_sites', 'job_site_labels', 'job_site_modes'):
            self.assertEqual(saved[key], template[key])
            self.assertEqual(public[key], template[key])
        for key in ('job_keywords', 'cities', 'exclude_keywords'):
            self.assertEqual(saved[key], [])
        for key in ('gemini_api_key', 'openai_api_key', 'deepseek_api_key'):
            self.assertEqual(saved.get(key), '')
        self.assertEqual(saved['email'], {'receiver': 'default-sites@example.com'})
        self.assertFalse(saved['local_schedule']['enabled'])

    def test_password_vault_encrypts_at_rest_and_admin_can_view_plaintext(self):
        admin, _, admin_id = self._client('passwordadmin', password='admin-secret-中文')
        member, _, member_id = self._client('passwordmember', password='member-secret-456')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
            stored = conn.execute(
                'SELECT password_hash,password_ciphertext FROM users WHERE id=?', (member_id,)
            ).fetchone()
        self.assertNotIn('member-secret-456', stored['password_hash'])
        self.assertNotIn('member-secret-456', stored['password_ciphertext'])
        self.assertEqual(self.portal.decrypt_password(stored['password_ciphertext']), 'member-secret-456')
        self.assertEqual(member.get('/admin/passwords').status_code, 403)
        page = admin.get('/admin/passwords')
        self.assertEqual(page.status_code, 200)
        self.assertIn('admin-secret-中文'.encode(), page.data)
        self.assertIn(b'member-secret-456', page.data)
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        with self.portal.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM admin_audit WHERE admin_id=? AND action='view_all_user_passwords'",
                (admin_id,),
            ).fetchone()[0], 1)

    def test_short_password_registers_and_logs_in(self):
        client, csrf, _ = self._client('shortpassword', password='x')
        self.assertEqual(client.post('/logout', data={'csrf_token': csrf}).status_code, 302)
        client.get('/login')
        with client.session_transaction() as session:
            csrf = session['csrf']
        response = client.post('/login', data={
            'email': 'shortpassword@example.com', 'password': 'x', 'csrf_token': csrf,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/api/config').status_code, 200)
        page = client.get('/register')
        self.assertEqual(page.status_code, 302)  # Logged-in users return to their dashboard.
        self.assertNotIn(b'minlength="12"', self.app.test_client().get('/register').data)

    def test_ai_filter_switch_and_masked_key_round_trip(self):
        client, csrf, user_id = self._client('aifilter')
        page = client.get('/').data
        self.assertIn(b'href="#ai-filter"', page)
        self.assertIn(b'id="aiFilterPage"', page)
        self.assertNotIn(b'id="aiPromptSummary"', page)
        settings = {
            'gemini_api_key': 'private-key',
            'ai_filter_prompt': '只保留战略职位',
            'ai_filter_enabled': False,
        }
        headers = {'X-CSRF-Token': csrf}
        self.assertEqual(client.post('/api/ai-filter', json=settings, headers=headers).status_code, 200)
        self.assertFalse(self.portal.load_config(user_id)['ai_filter_enabled'])
        public = client.get('/api/config').json['data']
        self.assertNotEqual(public['gemini_api_key'], 'private-key')
        settings['gemini_api_key'] = public['gemini_api_key']
        settings['ai_filter_enabled'] = True
        self.assertEqual(client.post('/api/ai-filter', json=settings, headers=headers).status_code, 200)
        saved = self.portal.load_config(user_id)
        self.assertTrue(saved['ai_filter_enabled'])
        self.assertEqual(saved['gemini_api_key'], 'private-key')
        settings['ai_filter_prompt'] = ''
        self.assertEqual(client.post('/api/ai-filter', json=settings, headers=headers).status_code, 400)

    def test_jobs_card_is_on_its_own_page(self):
        client, _, _ = self._client('jobs-page')
        page = BeautifulSoup(client.get('/').data, 'html.parser')
        self.assertIsNone(page.select_one('#overviewPage #jobs'))
        self.assertIsNotNone(page.select_one('#jobsPage #jobs #jobsList'))
        self.assertEqual(len(page.select('#jobsList')), 1)
        self.assertIsNotNone(page.select_one('.sidebar-nav a[href="#jobs"]'))

    def test_run_history_page_and_account_isolation(self):
        alice, _, alice_id = self._client('run-history-alice')
        bob, _, bob_id = self._client('run-history-bob')
        page = BeautifulSoup(alice.get('/').data, 'html.parser')
        self.assertIsNotNone(page.select_one('#activityPage #runsTableBody'))
        self.assertIsNotNone(page.select_one('.sidebar-nav a[href="#activity"]'))
        self.assertEqual(self.app.test_client().get('/api/runs').status_code, 401)
        with self.portal.connect() as conn:
            conn.execute('''INSERT INTO runs(user_id,kind,status,started_at,finished_at,result)
                VALUES (?, 'monitor', 'completed', '2026-10-29 02:00:00',
                    '2026-10-29 02:03:00', ?)''', (alice_id, json.dumps({
                'site_results': [{'url': 'https://example.com/jobs', 'label': '示例',
                                  'status': 'completed', 'scraped_jobs': 6}],
                'scraped_jobs': 6, 'new_jobs': 2, 'matched_jobs': 3,
                'notification_sent': True, 'error': 'private diagnostic',
            })))
            conn.execute("INSERT INTO runs(user_id,kind,status,result) VALUES (?, 'test', 'queued', NULL)", (alice_id,))
            conn.execute("INSERT INTO runs(user_id,kind,status,result) VALUES (?, 'monitor', 'failed', '{}')", (bob_id,))
        response = alice.get('/api/runs').json
        self.assertEqual(response['pagination']['total'], 2)
        self.assertEqual([item['status'] for item in response['data']], ['queued', 'completed'])
        completed = response['data'][1]
        self.assertEqual(completed['new_jobs'], 2)
        self.assertEqual(completed['site_results'][0]['url'], 'https://example.com/jobs')
        self.assertNotIn('private diagnostic', json.dumps(response))
        self.assertEqual(bob.get('/api/runs').json['pagination']['total'], 1)
        self.assertEqual(bob.get('/api/runs').json['data'][0]['status'], 'failed')

    def test_events_are_finite_replayable_and_isolated_per_user(self):
        alice, _, alice_id = self._client('events-alice')
        bob, _, bob_id = self._client('events-bob')
        self.portal.record_event(alice_id, {'type': 'status', 'data': {'message': 'alice-only-event'}})
        self.portal.record_event(bob_id, {'type': 'status', 'data': {'message': 'bob-only-event'}})
        headers = {'Last-Event-ID': '0'}
        first = alice.get('/api/events', headers=headers)
        second_tab = alice.get('/api/events', headers=headers)
        other = bob.get('/api/events', headers=headers)
        self.assertEqual(first.data, second_tab.data)
        self.assertIn('alice-only-event', first.text)
        self.assertNotIn('bob-only-event', first.text)
        self.assertIn('bob-only-event', other.text)
        self.assertNotIn('alice-only-event', other.text)
        self.assertIsNotNone(first.content_length)
        self.assertEqual(first.headers['Cache-Control'], 'no-store')
        self.assertEqual(alice.get('/api/events', headers={'Last-Event-ID': 'not-a-number'}).status_code, 200)

    def test_run_history_pagination_and_legacy_results(self):
        client, _, user_id = self._client('run-history-pages')
        with self.portal.connect() as conn:
            for index in range(17):
                conn.execute('INSERT INTO runs(user_id,kind,status,result) VALUES (?, ?, ?, ?)',
                             (user_id, 'monitor', 'completed', '{"matched_jobs": 1}' if index == 0 else None))
        first = client.get('/api/runs?page=bad').json
        self.assertEqual((first['pagination']['page'], first['pagination']['total_pages']), (1, 2))
        self.assertEqual(len(first['data']), 15)
        second = client.get('/api/runs?page=999999999999999999999').json
        self.assertEqual(second['pagination']['page'], 2)
        self.assertEqual(len(second['data']), 2)
        self.assertIsNone(second['data'][-1]['site_results'])
        self.assertIsNone(second['data'][-1]['new_jobs'])
        self.assertEqual(second['data'][-1]['matched_jobs'], 1)

    def test_cloud_worker_counts_only_newly_persisted_jobs(self):
        import cloud_worker
        monitor = SimpleNamespace(
            test_mode=False, config={'email': {}},
            last_check_result={'matched_jobs': 3}, check_jobs=lambda: True,
        )
        output = io.StringIO()
        with patch.object(cloud_worker, 'WebJobMonitor', return_value=monitor), \
                patch.object(cloud_worker, 'delivery_config', return_value={}), \
                patch.object(cloud_worker, 'EmailNotifier'), \
                patch.object(cloud_worker.db, 'get_statistics', side_effect=[
                    {'total_jobs': 4}, {'total_jobs': 6},
                ]), patch.object(cloud_worker.sys, 'argv', ['cloud_worker.py', 'fake.yaml']), \
                redirect_stdout(output):
            self.assertEqual(cloud_worker.main(), 0)
        result_line = next(line for line in output.getvalue().splitlines() if line.startswith('CLOUD_RESULT='))
        self.assertEqual(json.loads(result_line.removeprefix('CLOUD_RESULT='))['new_jobs'], 2)

    def test_notification_and_schedule_share_a_separate_card(self):
        client, _, _ = self._client('notification-card')
        page = BeautifulSoup(client.get('/').data, 'html.parser')
        self.assertIsNotNone(page.select_one('#overviewPage .dashboard-layout > .overview-summary #activity'))
        self.assertIsNotNone(page.select_one('#overviewPage .activity-column > .activity-secondary > #notificationScheduleCard'))
        self.assertIsNotNone(page.select_one('#overviewPage .activity-secondary > #notificationScheduleCard + .usage-card'))
        self.assertIsNotNone(page.select_one('#overviewPage .dashboard-primary #settings #configForm'))
        self.assertEqual(len(page.select('#configForm')), 1)
        for selector in ('#emailReceiver', '#scheduleEnabled', '#scheduleTime'):
            self.assertEqual(page.select_one(f'#notificationScheduleCard {selector}').get('form'), 'configForm')
        self.assertIsNone(page.select_one('#notificationScheduleCard button[type="submit"]'))
        self.assertIsNone(page.select_one('.config-settings-card #emailReceiver'))

    def test_new_users_default_to_no_schedule_and_can_run_manually(self):
        client, csrf, user_id = self._client('default-schedule')
        headers = {'X-CSRF-Token': csrf}
        config = client.get('/api/config').json['data']
        self.assertEqual(config['local_schedule'], {'days': [], 'time': '21:00', 'enabled': False})
        page = BeautifulSoup(client.get('/').data, 'html.parser')
        self.assertFalse(page.select_one('#scheduleEnabled').has_attr('checked'))
        self.assertTrue(all(not day.has_attr('checked') for day in page.select('[name="scheduleDay"]')))
        with patch.object(self.portal, 'reject_internal_urls'):
            self.assertEqual(client.post('/api/config', json=config, headers=headers).status_code, 200)
        self.assertEqual(self.portal.load_config(user_id)['local_schedule'], config['local_schedule'])
        self.assertEqual(client.post('/api/monitor/start', headers=headers).status_code, 200)
        self.assertEqual(self.portal.load_config(user_id)['local_schedule'], config['local_schedule'])
        config['local_schedule']['enabled'] = True
        with patch.object(self.portal, 'reject_internal_urls'):
            self.assertEqual(client.post('/api/config', json=config, headers=headers).status_code, 400)
        config['local_schedule']['days'] = [1, 4]
        with patch.object(self.portal, 'reject_internal_urls'):
            self.assertEqual(client.post('/api/config', json=config, headers=headers).status_code, 200)
        self.assertEqual(client.get('/api/config').json['data']['local_schedule'], config['local_schedule'])

    def test_overview_omits_schedule_summary_and_quick_actions(self):
        client, _, _ = self._client('clean-overview')
        page = BeautifulSoup(client.get('/').data, 'html.parser')
        self.assertIsNotNone(page.select_one('#overviewPage #activity'))
        self.assertIsNotNone(page.select_one('#activity #workLog'))
        self.assertIsNone(page.select_one('#activity #jobsFound'))
        self.assertIsNone(page.select_one('#activity #jobsAdded'))
        self.assertIsNone(page.select_one('#overviewPage .schedule-summary-card'))
        self.assertIsNone(page.select_one('#overviewPage .quick-actions'))
        self.assertEqual(len(page.select('#overview .overview-metric')), 3)
        self.assertIsNone(page.select_one('#overviewPage #progressBar'))
        self.assertIsNone(page.select_one('#overviewPage #liveStatusBadge'))
        self.assertFalse(page.select_one('#activity').has_attr('open'))
        self.assertTrue(page.select_one('#stopBtn').has_attr('hidden'))

    def test_openai_and_deepseek_settings_are_isolated_and_masked(self):
        client, csrf, user_id = self._client('multi-ai')
        headers = {'X-CSRF-Token': csrf}
        settings = {
            'ai_provider': 'openai', 'openai_api_key': 'private-openai',
            'openai_model': 'gpt-5.4-mini', 'ai_filter_prompt': '只保留法务',
            'ai_filter_enabled': True,
        }
        self.assertEqual(client.post('/api/ai-filter', json=settings, headers=headers).status_code, 200)
        public = client.get('/api/config').json['data']
        self.assertEqual(public['openai_api_key'], self.portal.SECRET_MASK)
        self.assertEqual(public['ai_provider'], 'openai')
        settings.update(ai_provider='deepseek', deepseek_api_key='private-deepseek',
                        deepseek_model='deepseek-flash', openai_api_key=public['openai_api_key'])
        self.assertEqual(client.post('/api/ai-filter', json=settings, headers=headers).status_code, 200)
        saved = self.portal.load_config(user_id)
        self.assertEqual(saved['openai_api_key'], 'private-openai')
        self.assertEqual(saved['deepseek_api_key'], 'private-deepseek')
        self.assertEqual(saved['ai_provider'], 'deepseek')
        self.assertEqual(self.portal.admin_visible_config(saved)['deepseek_api_key'], self.portal.SECRET_MASK)
        self.assertNotIn(b'private-openai', client.get('/').data)
        self.assertNotIn(b'private-deepseek', client.get('/').data)

    def test_login_config_and_jobs_are_isolated(self):
        alice, alice_csrf, alice_id = self._client('alice')
        bob, bob_csrf, bob_id = self._client('bob')
        guest = self.app.test_client()
        self.assertEqual(guest.get('/api/config').status_code, 401)
        self.assertEqual(guest.get('/').status_code, 302)

        config = self.portal.load_config(alice_id)
        config.update({
            'job_sites': ['https://jobs.example.com/search'],
            'job_site_labels': {'https://jobs.example.com/search': 'Alice'},
            'job_keywords': ['法务'],
            'email': {'sender': 'alice@example.com', 'auth_code': 'private-code',
                      'receiver': 'alice@example.com', 'smtp_server': 'smtp.example.com', 'smtp_port': 465},
            'local_schedule': {'days': [0, 2], 'time': '21:00', 'enabled': True},
        })
        with patch.object(self.portal, 'reject_internal_urls'):
            result = alice.post('/api/config', json=config, headers={'X-CSRF-Token': alice_csrf})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.portal.load_config(alice_id)['email'], {'receiver': 'alice@example.com'})
        self.assertEqual(alice.get('/api/config').json['data']['email'], {'receiver': 'alice@example.com'})
        bob_sites = bob.get('/api/config').json['data']['job_sites']
        self.assertEqual(bob_sites, self.portal.load_config(bob_id)['job_sites'])
        self.assertEqual(len(bob_sites), 22)
        self.assertNotIn('https://jobs.example.com/search', bob_sites)
        self.assertEqual(bob.get('/api/jobs').json['pagination']['total'], 0)
        self.portal.job_db(alice_id).add_job({
            'title': '法务', 'company': '示例', 'url': 'https://jobs.example.com/1',
            'source_site': 'jobs.example.com', 'site_label': 'Alice',
        })
        alice_job = alice.get('/api/jobs').json['data'][0]
        self.assertEqual(alice_job['title'], '法务')
        self.assertEqual(bob.get(f"/api/jobs/{alice_job['id']}").status_code, 404)

        self.assertEqual(bob.post('/api/config', json=config).status_code, 403)
        self.assertEqual(alice.post('/api/config', json=config, headers={
            'X-CSRF-Token': alice_csrf, 'Origin': 'https://evil.example',
        }).status_code, 403)
        self.assertNotEqual(alice.get('/api/monitor/status').json['data']['status'], 'stopped')
        self.assertEqual(bob.get('/api/monitor/status').json['data']['status'], 'stopped')

        queued = alice.post('/api/monitor/start', headers={'X-CSRF-Token': alice_csrf})
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(bob.get('/api/monitor/status').json['data']['is_monitoring'], False)
        self.assertEqual(alice.post('/api/monitor/stop', headers={'X-CSRF-Token': alice_csrf}).status_code, 200)

        logout = alice.post('/logout', data={'csrf_token': alice_csrf})
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(alice.get('/api/config').status_code, 401)
        self.assertEqual(bob.get('/api/config').status_code, 200)

    def test_admin_directory_and_central_sender(self):
        admin, admin_csrf, admin_id = self._client('adminuser')
        member, member_csrf, member_id = self._client('memberuser')
        self.assertEqual(member.get('/admin').status_code, 403)
        member_home = member.get('/')
        self.assertNotIn(b'id="emailSender"', member_home.data)
        self.assertIn(b'id="emailReceiver"', member_home.data)
        self.assertNotIn(b'/admin"', member_home.data)
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
        self.assertEqual(admin.get('/admin').status_code, 200)
        self.assertIn(b'/admin"', admin.get('/').data)
        config = self.portal.load_config(member_id)
        config['email'] = {
            'sender': 'legacy@example.com', 'auth_code': 'legacy-private-code',
            'receiver': 'member@example.com', 'smtp_server': 'smtp.legacy.example',
        }
        config['gemini_api_key'] = 'private-gemini-key'
        self.portal.save_config(member_id, config)
        detail = admin.get(f'/admin/users/{member_id}/config')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'member@example.com', detail.data)
        self.assertNotIn(b'legacy-private-code', detail.data)
        self.assertNotIn(b'legacy@example.com', detail.data)
        self.assertNotIn(b'private-gemini-key', detail.data)
        self.assertEqual(member.get(f'/admin/users/{admin_id}/config').status_code, 403)
        self.assertEqual(member.post('/admin/email', data={'csrf_token': member_csrf}).status_code, 403)
        saved = admin.post('/admin/email', data={
            'csrf_token': admin_csrf, 'sender': 'sender@example.com',
            'auth_code': 'central-private-code', 'smtp_server': 'smtp.example.com',
            'smtp_port': '465',
        })
        self.assertEqual(saved.status_code, 302)
        self.assertNotIn(b'central-private-code', admin.get('/admin').data)
        effective = self.portal.delivery_config(self.portal.load_config(member_id))
        self.assertEqual(effective['sender'], 'sender@example.com')
        self.assertEqual(effective['receiver'], 'member@example.com')
        self.assertEqual(effective['auth_code'], 'central-private-code')
        with patch.object(self.portal, 'EmailNotifier') as notifier:
            notifier.return_value.send_test_email.return_value = True
            response = member.post('/api/email/test', headers={'X-CSRF-Token': member_csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(notifier.call_args.args[0]['sender'], 'sender@example.com')
        self.assertEqual(notifier.return_value.send_test_email.call_args.args[0], 'member@example.com')
        with self.portal.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM admin_audit WHERE admin_id=? AND target_user_id=? AND action='view_user_config'",
                (admin_id, member_id),
            ).fetchone()[0], 1)

    def test_private_and_ip_urls_rejected(self):
        for url in ('http://127.0.0.1/', 'http://localhost/', 'https://10.0.0.1/',
                    'https://example.com:8080/', 'https://user:pass@example.com/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.portal.reject_internal_urls([url])

    def _feedback_picture(self):
        stream = io.BytesIO()
        self.portal.Image.new('RGB', (16, 16), '#456c50').save(stream, format='PNG')
        return stream.getvalue()

    def test_feedback_text_images_and_private_access(self):
        member, csrf, member_id = self._client('feedback-member')
        other, _, _ = self._client('feedback-other')
        admin, _, admin_id = self._client('feedback-admin')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
        content = '<script>alert(1)</script> 问题描述'
        picture = self._feedback_picture()
        response = member.post('/api/feedback', data={
            'content': content, 'images': (io.BytesIO(picture), 'clipboard.png'),
        }, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 201)
        feedback_id = response.json['data']['id']
        with self.portal.connect() as conn:
            stored = conn.execute('SELECT * FROM feedback WHERE id=?', (feedback_id,)).fetchone()
            image_id = conn.execute('SELECT id FROM feedback_images WHERE feedback_id=?', (feedback_id,)).fetchone()[0]
        self.assertEqual((stored['user_id'], stored['content']), (member_id, content))
        image_url = f'/api/feedback/images/{image_id}'
        self.assertEqual(member.get(image_url).data, picture)
        self.assertEqual(admin.get(image_url).mimetype, 'image/png')
        self.assertEqual(admin.get(image_url).headers['Cache-Control'], 'no-store')
        self.assertEqual(other.get(image_url).status_code, 404)
        self.assertEqual(self.app.test_client().get(image_url).status_code, 401)
        self.assertEqual(member.get('/admin/feedback').status_code, 403)
        page = admin.get('/admin/feedback')
        self.assertIn(b'feedback-member@example.com', page.data)
        self.assertIn(image_url.encode(), page.data)
        self.assertIn(b'&lt;script&gt;', page.data)
        self.assertNotIn(b'<script>alert(1)</script>', page.data)
        self.assertEqual(member.post('/api/feedback', data={'content': 'no csrf'}).status_code, 403)

    def test_feedback_rejects_invalid_uploads_and_excessive_requests(self):
        client, csrf, user_id = self._client('feedback-validation')
        headers = {'X-CSRF-Token': csrf}
        for content in (' ', 'x' * 5001):
            self.assertEqual(client.post('/api/feedback', data={'content': content}, headers=headers).status_code, 400)
        for image in (b'<svg onload="alert(1)"></svg>', b'not an image', b'x' * (5 * 1024 * 1024 + 1)):
            response = client.post('/api/feedback', data={
                'content': 'invalid image', 'images': (io.BytesIO(image), 'fake.png'),
            }, headers=headers)
            self.assertEqual(response.status_code, 400)
            response.close()
            response.request.environ['wsgi.input'].close()
        response = client.post('/api/feedback', data={
            'content': 'too many',
            'images': [(io.BytesIO(self._feedback_picture()), f'{index}.png') for index in range(4)],
        }, headers=headers)
        self.assertEqual(response.status_code, 400)
        response = client.post('/api/feedback', data=b'x' * (16 * 1024 * 1024 + 1),
                               content_type='multipart/form-data; boundary=test', headers=headers)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json['success'])
        with patch.object(self.portal, 'is_rate_limited', return_value=True):
            self.assertEqual(client.post('/api/feedback', data={'content': 'rate limited'}, headers=headers).status_code, 429)
        with self.portal.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM feedback WHERE user_id=?', (user_id,)).fetchone()[0], 0)
        self.assertEqual(client.post('/api/feedback', data={'content': 'text only'}, headers=headers).status_code, 201)

    def test_feedback_pagination_and_account_deletion_cleanup(self):
        admin, csrf, admin_id = self._client('feedback-cleanup-admin')
        member, member_csrf, member_id = self._client('feedback-cleanup-member')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
            conn.executemany('INSERT INTO feedback(user_id,content) VALUES (?,?)',
                             [(member_id, f'Feedback {index}') for index in range(21)])
        response = member.post('/api/feedback', data={
            'content': 'delete me', 'images': (io.BytesIO(self._feedback_picture()), 'clipboard.png'),
        }, headers={'X-CSRF-Token': member_csrf})
        feedback_id = response.json['data']['id']
        with self.portal.connect() as conn:
            image_id = conn.execute('SELECT id FROM feedback_images WHERE feedback_id=?', (feedback_id,)).fetchone()[0]
        page = BeautifulSoup(admin.get('/admin/feedback').data, 'html.parser')
        self.assertEqual(len(page.select('article')), 20)
        self.assertIsNotNone(page.select('article')[0].select_one(f'img[src="/api/feedback/images/{image_id}"]'))
        last = BeautifulSoup(admin.get('/admin/feedback?page=99999999999999999').data, 'html.parser')
        self.assertGreaterEqual(len(last.select('article')), 2)
        result = admin.post(f'/admin/users/{member_id}/delete', data={
            'csrf_token': csrf, 'confirmation': 'feedback-cleanup-member@example.com',
        })
        self.assertEqual(result.status_code, 303)
        with self.portal.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM feedback WHERE user_id=?', (member_id,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute('SELECT id FROM feedback_images WHERE id=?', (image_id,)).fetchone())

    def test_feedback_paste_and_admin_view_in_browser(self):
        import base64
        import threading
        from pathlib import Path
        from werkzeug.serving import make_server
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is not installed')
        member, _, _ = self._client('feedback-browser-member')
        admin, _, admin_id = self._client('feedback-browser-admin')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
        server = make_server('127.0.0.1', 0, self.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                options = {'headless': True}
                edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
                if os.name == 'nt' and edge.exists():
                    options['executable_path'] = str(edge)
                try:
                    browser = playwright.chromium.launch(**options)
                except Exception as error:
                    self.skipTest(f'Browser unavailable: {error}')
                try:
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                    context.add_cookies([{'name': 'session', 'value': member.get_cookie('session').value,
                                         'domain': '127.0.0.1', 'path': '/'}])
                    context.route('https://**/*', lambda route: route.fulfill(
                        content_type='text/javascript' if 'bootstrap.bundle' in route.request.url else 'text/css',
                        body='window.bootstrap={Modal:class{show(){} hide(){}}};' if 'bootstrap.bundle' in route.request.url else '',
                    ))
                    context.route('**/api/events', lambda route: route.fulfill(content_type='text/event-stream', body=': heartbeat\n\n'))
                    page = context.new_page()
                    page.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='domcontentloaded')
                    page.locator('.sidebar-nav a[href="#feedback"]').click()
                    page.locator('#feedbackPage').wait_for(state='visible')
                    self.assertTrue(page.locator('#feedbackPage').is_visible())
                    page.locator('#feedbackText').fill('Browser feedback with pasted screenshot')
                    encoded = base64.b64encode(self._feedback_picture()).decode('ascii')
                    page.locator('#feedbackText').evaluate('''(element, encoded) => {
                        const clipboard = new DataTransfer();
                        clipboard.items.add(new File([Uint8Array.from(atob(encoded), char => char.charCodeAt(0))], 'paste.png', {type:'image/png'}));
                        element.dispatchEvent(new ClipboardEvent('paste', {clipboardData:clipboard, bubbles:true, cancelable:true}));
                    }''', encoded)
                    self.assertEqual(page.locator('#feedbackImages img').count(), 1)
                    self.assertTrue(page.locator('#feedbackText').evaluate('element => element.clientWidth >= element.parentElement.clientWidth - 2'))
                    page.set_viewport_size({'width': 390, 'height': 900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    page.route('**/api/feedback', lambda route: route.fulfill(status=503, json={'success':False, 'message':'服务暂时不可用'}))
                    page.locator('#feedbackSubmit').click()
                    page.wait_for_function("document.querySelector('#feedbackStatus').dataset.state === 'error'")
                    self.assertEqual(page.locator('#feedbackText').input_value(), 'Browser feedback with pasted screenshot')
                    self.assertEqual(page.locator('#feedbackImages img').count(), 1)
                    page.unroute('**/api/feedback')
                    page.locator('#feedbackSubmit').click()
                    page.wait_for_function("document.querySelector('#feedbackStatus').dataset.state === 'saved'")
                    self.assertEqual(page.locator('#feedbackText').input_value(), '')
                    self.assertEqual(page.locator('#feedbackImages img').count(), 0)
                    context.clear_cookies()
                    context.add_cookies([{'name':'session', 'value':admin.get_cookie('session').value,
                                         'domain':'127.0.0.1', 'path':'/'}])
                    page.goto(f'http://127.0.0.1:{server.server_port}/admin/feedback', wait_until='domcontentloaded')
                    self.assertIn('Browser feedback with pasted screenshot', page.locator('main').inner_text())
                    self.assertIn('feedback-browser-member@example.com', page.locator('main').inner_text())
                    page.wait_for_function("document.querySelector('article img')?.naturalWidth > 0")
                    context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_admin_delete_removes_one_users_account_files_and_references(self):
        admin, csrf, admin_id = self._client('delete-admin')
        member, _, member_id = self._client('delete-member')
        survivor, _, survivor_id = self._client('delete-survivor')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id IN (?,?)', (admin_id, member_id))
            conn.execute('INSERT INTO admin_audit(admin_id,target_user_id,action) VALUES (?,?,?)',
                         (member_id, survivor_id, 'view_user_config'))
            conn.execute('''INSERT INTO email_verifications
                (purpose,email,user_id,code_digest,expires_at,sent_at) VALUES (?,?,?,?,?,?)''',
                ('bind', 'delete-member@example.com', member_id, 'digest', 9999999999, 1))
            conn.execute('INSERT INTO auth_attempts(identifier) VALUES (?)',
                         ('user:delete-member@example.com',))
        directory = self.portal.tenant_dir(member_id)
        (directory / 'run-test.log').write_text('private log', encoding='utf-8')
        (directory / 'jobs_cache.json').write_text('{}', encoding='utf-8')
        self.portal.job_db(member_id).add_job({
            'title': 'test', 'company': 'test', 'url': 'https://example.com/job/1',
        })
        survivor_config = self.portal.config_path(survivor_id).read_bytes()
        queued_run_id = self.portal.enqueue_run(member_id)
        page = admin.get(f'/admin/users/{member_id}/config')
        self.assertIn(b'id="deleteUserForm"', page.data)
        result = admin.post(f'/admin/users/{member_id}/delete', data={
            'csrf_token': csrf, 'confirmation': 'delete-member@example.com',
        })
        self.assertEqual(result.status_code, 303)
        self.assertFalse(directory.exists())
        self.assertEqual(member.get('/api/config').status_code, 401)
        self.assertEqual(survivor.get('/api/config').status_code, 200)
        self.assertEqual(self.portal.config_path(survivor_id).read_bytes(), survivor_config)
        with self.portal.connect() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM users WHERE id=?', (member_id,)).fetchone())
            for table in ('runs', 'events', 'email_verifications'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table} WHERE user_id=?',
                                              (member_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_audit WHERE admin_id=? OR target_user_id=?',
                                          (member_id, member_id)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_audit WHERE action=?',
                                          (f'delete_user:{member_id}',)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_attempts WHERE identifier=?',
                                          ('user:delete-member@example.com',)).fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        # A stale scheduler snapshot or in-flight request must not recreate
        # the removed tenant's folder, config, queue entry or event records.
        self.assertFalse(self.portal.enqueue_run(member_id))
        self.portal.record_event(member_id, {'type': 'status'})
        import cloud_scheduler
        cloud_scheduler.finish_run(
            {'id': queued_run_id, 'user_id': member_id, 'kind': 'monitor'}, 'completed', {},
        )
        with self.assertRaises(self.portal.UserUnavailableError):
            self.portal.save_config(member_id, {})
        with self.assertRaises(self.portal.UserUnavailableError):
            self.portal.load_config(member_id)
        self.assertFalse(directory.exists())

    def test_admin_delete_requires_permission_confirmation_and_protects_self(self):
        admin, csrf, admin_id = self._client('delete-guard-admin')
        member, member_csrf, member_id = self._client('delete-guard-member')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
        endpoint = f'/admin/users/{member_id}/delete'
        self.assertEqual(member.post(endpoint, data={
            'csrf_token': member_csrf, 'confirmation': 'delete-guard-member@example.com',
        }).status_code, 403)
        self.assertEqual(admin.post(endpoint, data={'confirmation': 'delete-guard-member@example.com'}).status_code, 403)
        self.assertEqual(admin.post(endpoint, data={
            'csrf_token': csrf, 'confirmation': 'another-account@example.com',
        }).status_code, 400)
        self.assertEqual(admin.post(f'/admin/users/{admin_id}/delete', data={
            'csrf_token': csrf, 'confirmation': 'delete-guard-admin@example.com',
        }).status_code, 400)
        self.assertEqual(admin.post('/admin/users/99999999/delete', data={
            'csrf_token': csrf, 'confirmation': 'missing@example.com',
        }).status_code, 404)
        self.assertEqual(member.get('/api/config').status_code, 200)
        self.assertEqual(admin.get('/api/config').status_code, 200)

    def test_admin_stops_active_user_before_deleting(self):
        admin, csrf, admin_id = self._client('delete-running-admin')
        _, _, member_id = self._client('delete-running-member')
        value = self.portal.load_config(member_id)
        value['local_schedule'] = {'days': [0], 'time': '21:00', 'enabled': True}
        self.portal.save_config(member_id, value)
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
            conn.execute("INSERT INTO runs(user_id,kind,status) VALUES (?,'monitor','running')", (member_id,))
        confirmation = {'csrf_token': csrf, 'confirmation': 'delete-running-member@example.com'}
        endpoint = f'/admin/users/{member_id}/delete'
        self.assertEqual(admin.post(endpoint, data=confirmation).status_code, 409)
        self.assertEqual(admin.post(f'/admin/users/{member_id}/stop', data={'csrf_token': csrf}).status_code, 303)
        self.assertFalse(self.portal.load_config(member_id)['local_schedule']['enabled'])
        with self.portal.connect() as conn:
            status = conn.execute('SELECT status FROM runs WHERE user_id=?', (member_id,)).fetchone()['status']
            self.assertEqual(status, 'cancel_requested')
            conn.execute("UPDATE runs SET status='cancelled' WHERE user_id=?", (member_id,))
        self.assertEqual(admin.post(endpoint, data=confirmation).status_code, 303)

    def test_failed_account_delete_rolls_back_database_and_restores_files(self):
        admin, csrf, admin_id = self._client('delete-rollback-admin')
        member, _, member_id = self._client('delete-rollback-member')
        with self.portal.connect() as conn:
            conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin_id,))
            conn.execute(f'''CREATE TRIGGER reject_test_delete BEFORE DELETE ON users
                WHEN OLD.id={int(member_id)} BEGIN SELECT RAISE(ABORT, 'simulated failure'); END''')
        path = self.portal.config_path(member_id)
        original = path.read_bytes()
        try:
            result = admin.post(f'/admin/users/{member_id}/delete', data={
                'csrf_token': csrf, 'confirmation': 'delete-rollback-member@example.com',
            })
            self.assertEqual(result.status_code, 500)
        finally:
            with self.portal.connect() as conn:
                conn.execute('DROP TRIGGER reject_test_delete')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(member.get('/api/config').status_code, 200)

    def test_registration_requires_valid_one_time_email_code(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'verify-csrf'
        address = 'verification@example.com'
        registration = {'email': address, 'password': 'long-password-123',
                        'csrf_token': 'verify-csrf'}
        self.assertEqual(client.post('/register', data={**registration, 'code': '123456'}).status_code, 200)
        with self.portal.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM users WHERE username=?', (address,)).fetchone())
        with patch.object(self.portal, 'load_admin_email', return_value={}):
            missing_smtp = client.post('/register/code', data={'email': address, 'csrf_token': 'verify-csrf'})
        self.assertEqual(missing_smtp.status_code, 503)
        sent = []
        with patch.object(self.portal, 'load_admin_email', return_value={
            'sender': 'admin@example.com', 'auth_code': 'fake',
            'smtp_server': 'smtp.example.com', 'smtp_port': 465,
        }), patch.object(self.portal, 'send_verification_code', side_effect=lambda _, code, __: sent.append(code) or True):
            self.assertEqual(client.post('/register/code', data={'email': address, 'csrf_token': 'verify-csrf'}).status_code, 200)
            self.assertEqual(client.post('/register/code', data={'email': address, 'csrf_token': 'verify-csrf'}).status_code, 429)
        self.assertEqual(len(sent), 1)
        with self.portal.connect() as conn:
            row = conn.execute('SELECT code_digest FROM email_verifications WHERE email=?', (address,)).fetchone()
        self.assertNotIn(sent[0], row['code_digest'])
        wrong = '000000' if sent[0] != '000000' else '111111'
        self.assertEqual(client.post('/register', data={**registration, 'code': wrong}).status_code, 200)
        self.assertEqual(client.post('/register', data={**registration, 'code': sent[0]}).status_code, 302)
        with self.portal.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM email_verifications WHERE email=?', (address,)).fetchone())

    def test_expired_code_and_replay_are_rejected(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'expired-csrf'
        address = 'expired@example.com'
        with self.portal.connect() as conn:
            conn.execute('INSERT INTO email_verifications(purpose,email,user_id,code_digest,expires_at,sent_at) VALUES (?,?,?,?,?,?)',
                         ('register', address, 0, self.portal.code_digest('register', address, 0, '123456'), 1, 1))
        form = {'email': address, 'password': 'long-password-123', 'code': '123456', 'csrf_token': 'expired-csrf'}
        self.assertEqual(client.post('/register', data=form).status_code, 200)
        with self.portal.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM users WHERE username=?', (address,)).fetchone())

    def test_five_wrong_codes_block_the_correct_code(self):
        address = 'wrongcodes@example.com'
        with self.portal.connect() as conn:
            conn.execute('INSERT INTO email_verifications(purpose,email,user_id,code_digest,expires_at,sent_at) VALUES (?,?,?,?,?,?)',
                         ('register', address, 0, self.portal.code_digest('register', address, 0, '123456'),
                          int(time.time()) + 600, int(time.time())))
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'wrong-csrf'
        for _ in range(5):
            self.assertEqual(client.post('/register', data={'email': address, 'password': 'long-password-123',
                'code': '000000', 'csrf_token': 'wrong-csrf'}).status_code, 200)
        # Registration submission throttling is separate; direct verification confirms the code is unusable.
        with self.portal.connect() as conn:
            self.assertFalse(self.portal.consume_code(conn, 'register', address, 0, '123456'))

    def test_verification_mail_uses_admin_smtp(self):
        import cloud_mail
        settings = {'sender': 'sender@example.com', 'auth_code': 'private-code',
                    'smtp_server': 'smtp.example.com', 'smtp_port': 465}
        with patch.object(cloud_mail, 'load_admin_email', return_value=settings), \
             patch.object(cloud_mail.smtplib, 'SMTP_SSL') as smtp:
            self.assertTrue(cloud_mail.send_verification_code('recipient@example.com', '123456', 'register'))
        args, kwargs = smtp.call_args
        self.assertEqual(args[:2], ('smtp.example.com', 465))
        server = smtp.return_value.__enter__.return_value
        server.login.assert_called_once_with('sender@example.com', 'private-code')
        message = server.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'recipient@example.com')
        self.assertIn('123456', message.get_content())

    def test_legacy_admin_can_login_and_bind_verified_email(self):
        with self.portal.connect() as conn:
            user_id = conn.execute('INSERT INTO users(username,password_hash,is_admin) VALUES (?,?,1)',
                ('legacyadmin', generate_password_hash('long-password-123', method='scrypt'))).lastrowid
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'legacy-csrf'
        self.assertEqual(client.post('/login', data={'email': 'legacyadmin', 'password': 'long-password-123',
                                                     'csrf_token': 'legacy-csrf'}).status_code, 302)
        with self.portal.connect() as conn:
            encrypted = conn.execute(
                'SELECT password_ciphertext FROM users WHERE id=?', (user_id,)
            ).fetchone()['password_ciphertext']
        self.assertEqual(self.portal.decrypt_password(encrypted), 'long-password-123')
        self.assertEqual(client.get('/admin').status_code, 200)
        self.assertIn(b'account/email', client.get('/').data)
        with client.session_transaction() as session:
            csrf = session['csrf']
        address = 'legacyadmin@example.com'
        sent = []
        with patch.object(self.portal, 'load_admin_email', return_value={
            'sender': 'admin@example.com', 'auth_code': 'fake',
            'smtp_server': 'smtp.example.com', 'smtp_port': 465,
        }), patch.object(self.portal, 'send_verification_code', side_effect=lambda _, code, __: sent.append(code) or True):
            self.assertEqual(client.post('/account/email/code', data={'email': address, 'csrf_token': csrf}).status_code, 200)
        self.assertEqual(client.post('/account/email', data={'email': address, 'code': sent[0],
                                                              'csrf_token': csrf}).status_code, 302)
        with self.portal.connect() as conn:
            user = conn.execute('SELECT username,email,email_verified,is_admin FROM users WHERE id=?', (user_id,)).fetchone()
        self.assertEqual((user['username'], user['email'], user['email_verified'], user['is_admin']),
                         ('legacyadmin', address, 1, 1))
        self.assertEqual(self.portal.load_config(user_id)['email']['receiver'], address)
        self.assertEqual(client.get('/admin').status_code, 200)

    def test_existing_account_database_is_migrated_without_promoting_users(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'old-accounts.sqlite3')
            with closing(sqlite3.connect(path)) as conn:
                with conn:
                    conn.execute('''CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )''')
                    conn.execute("INSERT INTO users(username,password_hash) VALUES ('legacy','hash')")
            with patch.object(self.portal, 'ACCOUNT_DB', path):
                self.portal.init_db()
                with self.portal.connect() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT is_admin FROM users WHERE username='legacy'"
                    ).fetchone()['is_admin'], 0)
                    columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
                    self.assertIn('password_ciphertext', columns)


if __name__ == '__main__':
    unittest.main()
