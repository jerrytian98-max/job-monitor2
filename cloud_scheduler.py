"""Single cloud scheduler: fair queued runs and missed weekly run catch-up."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime

from cloud_portal import (
    BASE_DIR, UserUnavailableError, connect, config_path, enqueue_run,
    load_config, record_event, tenant_dir,
)
from local_schedule import WeeklySchedule


def enqueue_due_runs():
    with connect() as conn:
        users = conn.execute('SELECT id FROM users ORDER BY id').fetchall()
    for user in users:
        user_id = user['id']
        try:
            value = load_config(user_id)
            schedule = value.get('local_schedule')
            if value.get('job_sites') and schedule and schedule.get('enabled'):
                if WeeklySchedule(config_path(user_id)).due(schedule):
                    enqueue_run(user_id)
        except Exception as error:
            record_event(user_id, {'type': 'status', 'data': {'message': f'计划检查失败：{error}', 'type': 'error'}})


def claim_next_run():
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute("SELECT id,user_id,kind FROM runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if run:
            conn.execute("UPDATE runs SET status='running',started_at=CURRENT_TIMESTAMP WHERE id=?", (run['id'],))
    return dict(run) if run else None


def finish_run(run, status, result):
    with connect() as conn:
        updated = conn.execute(
            'UPDATE runs SET status=?,finished_at=CURRENT_TIMESTAMP,result=? WHERE id=?',
            (status, json.dumps(result, ensure_ascii=False), run['id']),
        )
        if not updated.rowcount:
            return  # The administrator already removed this tenant/run.
    if run['kind'] == 'monitor' and status in ('completed', 'failed'):
        try:
            value = load_config(run['user_id'])
            schedule = value.get('local_schedule')
            if schedule:
                WeeklySchedule(config_path(run['user_id'])).complete(schedule, datetime.now())
        except UserUnavailableError:
            return
        except FileNotFoundError:
            with connect() as conn:
                exists = conn.execute('SELECT 1 FROM users WHERE id=?', (run['user_id'],)).fetchone()
            if not exists:
                return
            raise
    message = '抓取完成' if status == 'completed' else ('抓取已取消' if status == 'cancelled' else '抓取失败')
    if run['kind'] == 'test':
        message = '测试' + message
    if result.get('matched_jobs') is not None:
        message += f"，找到 {result['matched_jobs']} 个新职位"
    record_event(run['user_id'], {'type': 'status', 'data': {'message': message, 'type': 'success' if status == 'completed' else 'error', 'work_log': True}})


def execute(run):
    user_id = run['user_id']
    directory = tenant_dir(user_id)
    env = os.environ.copy()
    env['JOB_MONITOR_DATA_DIR'] = str(directory)
    env['JOB_PROFILE'] = ''
    env['JOB_MONITOR_CONFIG_FILE'] = str(config_path(user_id))
    command = [sys.executable, '-u', str(BASE_DIR / 'cloud_worker.py'), str(config_path(user_id))]
    if run['kind'] == 'test':
        command.append('--test')
    record_event(user_id, {'type': 'status', 'data': {'message': '云端正在抓取', 'type': 'info', 'work_log': True}})
    log_path = directory / f"run-{run['id']}.log"
    offset = 0
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=env, cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT, text=True)
        started = time.monotonic()
        cancelled = False
        while process.poll() is None:
            offset = publish_events(log_path, offset, user_id)
            with connect() as conn:
                status = conn.execute('SELECT status FROM runs WHERE id=?', (run['id'],)).fetchone()['status']
            if status == 'cancel_requested' or time.monotonic() - started > 3600:
                cancelled = status == 'cancel_requested'
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(2)
        process.wait()
    publish_events(log_path, offset, user_id)
    result = {}
    for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('CLOUD_RESULT='):
            try:
                result = json.loads(line.removeprefix('CLOUD_RESULT='))
            except ValueError:
                pass
    status = 'cancelled' if cancelled else ('completed' if process.returncode == 0 else 'failed')
    finish_run(run, status, result)


def publish_events(log_path, offset, user_id):
    with log_path.open('r', encoding='utf-8', errors='replace') as stream:
        stream.seek(offset)
        while True:
            line = stream.readline()
            if not line or not line.endswith('\n'):
                break
            offset = stream.tell()
            if line.startswith('CLOUD_EVENT='):
                try:
                    record_event(user_id, json.loads(line.removeprefix('CLOUD_EVENT=')))
                except ValueError:
                    pass
        return offset


def main():
    with connect() as conn:
        conn.execute("UPDATE runs SET status='queued',started_at=NULL WHERE status IN ('running','cancel_requested')")
    while True:
        enqueue_due_runs()
        run = claim_next_run()
        if run:
            try:
                execute(run)
            except Exception as error:
                finish_run(run, 'failed', {'error': str(error)})
        else:
            time.sleep(5)


if __name__ == '__main__':
    main()
