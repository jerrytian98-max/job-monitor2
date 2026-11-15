"""Persistent weekly scheduling in the computer's local wall-clock time."""
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path


def validate_schedule(value):
    if not isinstance(value, dict):
        raise ValueError('本地运行计划必须是对象')
    enabled = value.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('启用计划必须为布尔值')
    days = value.get('days')
    if not isinstance(days, list) or (enabled and not days) or any(type(day) is not int or day not in range(7) for day in days):
        raise ValueError('请至少选择一个星期（周一到周日）')
    time = value.get('time', '')
    if not isinstance(time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time):
        raise ValueError('运行时间必须为 HH:MM')
    return dict(days=sorted(set(days)), time=time, enabled=enabled)


class WeeklySchedule:
    lock = threading.RLock()
    def __init__(self, config_path):
        self.path = Path(config_path).with_suffix('.schedule.json')

    def write(self, state):
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.replace(self.path)

    def configure(self, schedule, now=None):
        with self.lock:
            return self._configure(schedule, now)

    def _configure(self, schedule, now=None):
        now = now or datetime.now()
        schedule = validate_schedule(schedule)
        signature = [schedule['days'], schedule['time']]
        state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if state.get('signature') != signature:
            state = dict(signature=signature, through=now.isoformat())
            self.write(state)
        return state

    def due(self, schedule, now=None):
        now = now or datetime.now()
        state = self.configure(schedule, now)
        if not schedule.get('enabled', True):
            return None
        hour, minute = map(int, schedule['time'].split(':'))
        for offset in range(8):
            candidate = (now - timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now and candidate.weekday() in schedule['days']:
                return candidate if candidate > datetime.fromisoformat(state['through']) else None
        return None

    def complete(self, schedule, through):
        with self.lock:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if state.get('signature') != [schedule['days'], schedule['time']]:
                return  # User edited the schedule while the old task was running.
            state['through'] = through.isoformat()
            self.write(state)
