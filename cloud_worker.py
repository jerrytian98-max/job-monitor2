"""Run one tenant's monitoring pass in an isolated Python process."""

import json
import sys

from app import WebJobMonitor
from cloud_mail import delivery_config
from database import db
from notifier import EmailNotifier


def main():
    monitor = WebJobMonitor(sys.argv[1], test_mode='--test' in sys.argv[2:])
    if not monitor.test_mode:
        # Ignore any legacy tenant SMTP credentials. Only the administrator
        # controls the sender; the tenant controls the destination address.
        monitor.config['email'] = delivery_config(monitor.config)
        monitor.notifier = EmailNotifier(monitor.config['email'])
    def status(message, kind='info', work_log=False):
        print('CLOUD_EVENT=' + json.dumps({
            'type': 'status',
            'data': {'message': message, 'type': kind, 'work_log': work_log},
        }, ensure_ascii=False), flush=True)
    monitor.status_callback = status
    monitor.job_callback = lambda job: print('CLOUD_EVENT=' + json.dumps({
        'type': 'job_found', 'data': job,
    }, ensure_ascii=False), flush=True)
    jobs_before = db.get_statistics().get('total_jobs', 0) if not monitor.test_mode else 0
    success = monitor.check_jobs()
    jobs_after = db.get_statistics().get('total_jobs', 0) if not monitor.test_mode else 0
    monitor.last_check_result['new_jobs'] = max(0, jobs_after - jobs_before)
    print('CLOUD_RESULT=' + json.dumps(monitor.last_check_result, ensure_ascii=False), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
