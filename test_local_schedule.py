import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from pathlib import Path
from local_schedule import WeeklySchedule, validate_schedule


class WeeklyScheduleTests(unittest.TestCase):
    def test_multiple_days_restart_and_single_catchup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            schedule = dict(days=[1, 4], time='21:00', enabled=True)
            scheduler = WeeklySchedule(path)
            scheduler.configure(schedule, datetime(2026, 9, 14, 10))
            self.assertIsNone(scheduler.due(schedule, datetime(2026, 9, 15, 20, 59)))
            self.assertEqual(scheduler.due(schedule, datetime(2026, 9, 15, 21)), datetime(2026, 9, 15, 21))
            scheduler.complete(schedule, datetime(2026, 9, 15, 21, 10))
            scheduler = WeeklySchedule(path)
            self.assertIsNone(scheduler.due(schedule, datetime(2026, 9, 16)))
            self.assertEqual(scheduler.due(schedule, datetime(2026, 10, 1)), datetime(2026, 9, 29, 21))
            scheduler.complete(schedule, datetime(2026, 10, 1))
            self.assertIsNone(scheduler.due(schedule, datetime(2026, 10, 1, 1)))
            self.assertIsNotNone(scheduler.due(schedule, datetime(2026, 10, 2, 21)))

    def test_initial_schedule_edit_and_profile_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            one = WeeklySchedule(Path(directory) / 'config.yaml')
            two = WeeklySchedule(Path(directory) / 'config_user2.yaml')
            schedule = dict(days=[1], time='21:00', enabled=True)
            one.configure(schedule, datetime(2026, 9, 14))
            self.assertIsNone(two.due(schedule, datetime(2026, 9, 16)))
            self.assertIsNotNone(one.due(schedule, datetime(2026, 9, 16)))
            paused = dict(schedule, enabled=False)
            self.assertIsNone(one.due(paused, datetime(2026, 9, 16)))
            self.assertIsNotNone(one.due(schedule, datetime(2026, 9, 16)))
            self.assertIsNone(one.due(dict(schedule, time='20:00'), datetime(2026, 9, 16)))

    def test_invalid_values(self):
        for days, time in [([], '21:00'), ([7], '21:00'), ([True], '21:00'), ([1], '25:00')]:
            with self.assertRaises(ValueError):
                validate_schedule(dict(days=days, time=time))

    def test_disabled_schedule_can_have_no_days(self):
        schedule = dict(days=[], time='21:00', enabled=False)
        self.assertEqual(validate_schedule(schedule), schedule)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = WeeklySchedule(Path(directory) / 'config.yaml')
            self.assertIsNone(scheduler.due(schedule, datetime(2026, 9, 16)))

    def test_monitor_executes_one_catchup_then_records_completion(self):
        from main import JobMonitor
        monitor = JobMonitor.__new__(JobMonitor)
        monitor.config_file = 'unused-config.yaml'
        monitor.test_mode = True
        monitor.matcher = Mock()
        monitor.check_jobs = Mock(return_value=True)
        schedule = dict(days=[1, 4], time='21:00', enabled=True)
        monitor._load_config = Mock(return_value={'local_schedule': schedule})
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch('main.WeeklySchedule') as factory:
            factory.return_value.due.return_value = datetime(2026, 9, 15, 21)
            monitor.run_weekly(stop)
            monitor.check_jobs.assert_called_once_with()
            factory.return_value.complete.assert_called_once()
            stop.wait.assert_called_once_with(15)

    def test_completion_of_old_plan_does_not_overwrite_new_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler = WeeklySchedule(Path(directory) / 'config.yaml')
            old = dict(days=[1], time='21:00', enabled=True)
            new = dict(days=[2], time='20:00', enabled=True)
            scheduler.configure(old, datetime(2026, 9, 14))
            scheduler.configure(new, datetime(2026, 9, 16, 19))
            scheduler.complete(old, datetime(2026, 9, 16, 21))
            self.assertEqual(scheduler.due(new, datetime(2026, 9, 16, 21)), datetime(2026, 9, 16, 20))
