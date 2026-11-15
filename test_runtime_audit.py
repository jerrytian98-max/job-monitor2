"""Regression checks for database lifecycle, local clock and event delivery."""

from datetime import datetime, timedelta
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from database import JobDatabase, IDENTITY_MIGRATION_VERSION
from event_stream import EventHistory


BASE_DIR = Path(__file__).resolve().parent


class DatabaseLifecycleTests(unittest.TestCase):
    def test_one_shot_cli_reports_failure_to_github_actions(self):
        import main
        for success in (True, False):
            with patch.object(main, 'JobMonitor') as monitor, patch.object(
                sys, 'argv', ['main.py', '--once']
            ):
                monitor.return_value.run_once.return_value = success
                self.assertEqual(main.main(), 0 if success else 1)

    def test_import_does_not_create_database_in_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, JOB_MONITOR_DATA_DIR=directory, JOB_PROFILE='user2',
                       PYTHONPATH=str(BASE_DIR), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([
                sys.executable, '-c',
                'import os, database; assert not os.listdir(os.environ["JOB_MONITOR_DATA_DIR"])',
            ], cwd=directory, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_time_windows_and_today_use_the_same_local_clock_as_inserts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = JobDatabase(str(Path(directory) / 'jobs.db'))
            now = datetime(2026, 10, 30, 1, 0)
            with patch('database.datetime') as clock:
                clock.now.return_value = now
                for index, hours in enumerate((0, 23, 25)):
                    job = dict(title=f'job-{index}', company='test', url=f'https://example.com/{index}')
                    self.assertTrue(database.add_job(job))
                    with closing(database._get_connection()) as conn, conn:
                        conn.execute('UPDATE jobs SET found_time=? WHERE title=?',
                                     ((now - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S'), job['title']))
                self.assertEqual({job['title'] for job in database.get_new_jobs(24)}, {'job-0', 'job-1'})
                self.assertEqual(database.get_statistics()['new_jobs_today'], 1)
                self.assertEqual(database.clear_old_jobs(1), 1)
                self.assertEqual(database.count_jobs(), 2)

    def test_legacy_dedup_is_backed_up_and_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'jobs.db')
            database = JobDatabase(path, migrate_legacy=False)
            with closing(database._get_connection()) as conn, conn:
                for index in range(2):
                    conn.execute('''INSERT INTO jobs(title,company,url,site_label,job_hash)
                        VALUES (?,?,?,?,?)''', ('法务', '阿里云',
                        f'https://careers.aliyun.com/off-campus/position-detail?positionId=42&track_id={index}',
                        '阿里法律 / 阿里法务', f'legacy-{index}'))
            self.assertEqual(JobDatabase(path, migrate_legacy=False).count_jobs(), 2)
            migrated = JobDatabase(path)
            self.assertEqual(migrated.count_jobs(), 1)
            self.assertEqual(migrated.get_all_jobs()[0]['site_label'], '阿里法律')
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], IDENTITY_MIGRATION_VERSION)
            backups = list(Path(directory).glob('jobs.db.dedup-backup-*'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
            with patch.object(JobDatabase, '_migrate_job_identities', side_effect=AssertionError('repeated migration')):
                self.assertEqual(JobDatabase(path).count_jobs(), 1)


class EventDeliveryTests(unittest.TestCase):
    def test_native_eventsource_reconnects_both_tabs_with_their_last_event_ids(self):
        import app
        from werkzeug.serving import make_server
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable')
        observed = []
        def wsgi(environ, start_response):
            if environ.get('PATH_INFO') == '/api/events':
                observed.append(environ.get('HTTP_LAST_EVENT_ID'))
            return app.app(environ, start_response)
        server = make_server('127.0.0.1', 0, wsgi, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(app, 'event_history', EventHistory()), sync_playwright() as playwright:
                options = {'headless': True}
                if os.name == 'nt':
                    options['executable_path'] = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
                try:
                    browser = playwright.chromium.launch(**options)
                except Exception as error:
                    self.skipTest(f'Browser unavailable: {error}')
                try:
                    pages = [browser.new_page(), browser.new_page()]
                    for page in pages:
                        page.goto(f'http://127.0.0.1:{server.server_port}/sse-test-fixture')
                        page.evaluate('''() => { window.received=[]; window.feed=new EventSource('/api/events');
                            feed.onmessage=event=>received.push(JSON.parse(event.data).data.message); }''')
                    deadline = time.monotonic() + 5
                    while len(observed) < 2 and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertGreaterEqual(len(observed), 2)
                    for message in ('first', 'after-disconnect'):
                        app.send_event('status', {'message': message})
                        for page in pages:
                            page.wait_for_function('message=>received.includes(message)', arg=message, timeout=8000)
                    for page in pages:
                        self.assertEqual(page.evaluate('received'), ['first', 'after-disconnect'])
                        page.evaluate('feed.close()')
                    self.assertIn('1', observed)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_independent_cursors_receive_the_same_events_and_history_is_bounded(self):
        history = EventHistory(capacity=2)
        for label in ('old', 'one', 'two'):
            history.publish({'type': 'status', 'data': {'message': label}})
        first = history.read('0')
        second = history.read('0')
        self.assertEqual(first, second)
        self.assertEqual([key for key, _ in first[1]], [2, 3])
        self.assertEqual(history.read('2')[1], [first[1][-1]])
        self.assertEqual(history.read('invalid'), (3, []))

    def test_local_endpoint_finishes_and_does_not_consume_other_tabs_events(self):
        import app
        history = EventHistory()
        with patch.object(app, 'event_history', history):
            a, b = app.app.test_client(), app.app.test_client()
            self.assertIn('event: cursor', a.get('/api/events').text)
            app.send_event('status', {'message': 'same for both tabs'})
            first = a.get('/api/events', headers={'Last-Event-ID': '0'})
            second = b.get('/api/events', headers={'Last-Event-ID': '0'})
            self.assertEqual(first.data, second.data)
            self.assertIn('same for both tabs', first.text)
            self.assertIsNotNone(first.content_length)
            self.assertEqual(first.headers['Cache-Control'], 'no-store')

    def test_dry_run_status_does_not_open_or_migrate_a_database(self):
        import app
        monitor = app.WebJobMonitor.__new__(app.WebJobMonitor)
        monitor.test_mode = True
        monitor.last_check_result = {}
        with patch.object(app, 'db') as database:
            monitor._after_check()
        database.get_statistics.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows launchers')
class WindowsLauncherTests(unittest.TestCase):
    def test_user_launchers_preserve_profile_port_working_directory_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            # Run the real Python executable, but stop before application startup.
            (Path(directory) / 'sitecustomize.py').write_text(
                'import json, os\nprint("LAUNCH_FIXTURE="+json.dumps({'
                '"profile":os.environ.get("JOB_PROFILE",""),'
                '"port":os.environ.get("FLASK_PORT"),"cwd":os.getcwd()}),flush=True)\nos._exit(7)\n',
                encoding='utf-8',
            )
            env = dict(os.environ, PYTHONPATH=directory, PYTHONDONTWRITEBYTECODE='1')
            env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env['PATH']
            for filename, profile, port in (
                ('启动Web服务.bat', '', '5000'),
                ('启动Web服务-用户1(默认).bat', '', '5000'),
                ('启动Web服务-用户2(分身).bat', 'user2', '5001'),
            ):
                result = subprocess.run(f'cmd.exe /d /c call "{BASE_DIR / filename}"',
                                        cwd=directory, env=env, input='\n', capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=15)
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
                line = next(line for line in result.stdout.splitlines() if line.startswith('LAUNCH_FIXTURE='))
                values = json.loads(line.split('=', 1)[1])
                self.assertEqual((values['profile'], values['port']), (profile, port))
                self.assertEqual(Path(values['cwd']), BASE_DIR)


if __name__ == '__main__':
    unittest.main()
