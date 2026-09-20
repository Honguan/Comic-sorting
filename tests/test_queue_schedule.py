import unittest
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from queue_schedule import QueueSchedule
from queue_worker import Job


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.job = Job(Path('Manga/Chapter 1'), 'translate')
        self.q = Mock(running=False, history_save_failed=False)
        self.q.app.manga_busy = False
        self.q.app.root.grab_current.return_value = None
        self.q.jobs = [self.job]
        self.q.active_jobs = ()
        self.q.start_targets.return_value = (self.job,)
        self.q.configure_start.side_effect = lambda jobs: jobs
        self.q.start.side_effect = lambda **kwargs: setattr(self.q, 'running', True)
        self.s = QueueSchedule(self.q)
        self.s.options = lambda: ('stable',)

    def test_multiple_schedules_busy_selection_and_no_repeat(self):
        self.s.add(True, '1')
        self.s.add(False, (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M'))
        self.assertEqual(len(self.s.entries), 2)
        self.s.entries[0]['deadline'] = 0
        self.q.app.manga_busy = True
        self.s.tick()
        self.q.start.assert_not_called()
        self.q.app.manga_busy = False
        self.s.tick()
        self.q.start.assert_called_once_with(cleanup_confirmed=True, scheduled_jobs=(self.job,))
        self.q.running = False
        self.s.tick()
        self.q.start.assert_called_once()

    def test_changed_options_completed_removed_and_invalid_times(self):
        for value in ('nan', 'inf', '0', '-1', '525601'):
            with self.assertRaises(ValueError):
                self.s.add(True, value)
        with self.assertRaises(ValueError):
            self.s.add(False, '2000-01-01 00:00')
        for change in ('options', 'done', 'removed', 'pages'):
            self.job.status = 'pending'
            self.q.jobs = [self.job]
            self.s.options = lambda: ('stable',)
            self.s.add(True, '1')
            self.s.entries[-1]['deadline'] = 0
            if change == 'options': self.s.options = lambda: ('changed',)
            if change == 'done': self.job.status = 'done'
            if change == 'removed': self.q.jobs = []
            if change == 'pages': self.job.end_page = 2
            self.s.tick()
        self.q.start.assert_not_called()

    def test_cancel_confirmation_and_close(self):
        self.q.confirm_cleanup.return_value = False
        self.s.add(True, '1')
        self.assertEqual(self.s.entries, [])
        self.assertEqual(self.q.active_jobs, ())
        self.s.close(SimpleNamespace(widget=self.q.app.root))
        self.q.app.root.after_cancel.assert_called_once()

    def test_form_modes_preserve_independent_values_and_validate(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        self.q.app.root = root
        self.s.show()
        root.update()
        self.assertEqual(self.s.form_value()[:2], (True, 60))
        self.s.hours.set('2')
        self.s.minutes.set('30')
        self.s.tabs.select(1)
        root.update()
        self.assertFalse(self.s.form_value()[0])
        self.assertGreater(self.s.form_value()[2], datetime.now())
        self.s.date.set('2027-02-30')
        self.assertTrue(self.s.add_button.instate(['disabled']))
        self.s.date.set((datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'))
        self.s.hour.set('24')
        with self.assertRaises(ValueError):
            self.s.form_value()
        self.s.hour.set('12')
        self.assertFalse(self.s.add_button.instate(['disabled']))
        self.s.tabs.select(0)
        root.update()
        self.assertEqual(self.s.form_value()[:2], (True, 150))
        self.s.minutes.set('60')
        self.assertTrue(self.s.add_button.instate(['disabled']))
        self.s.hours.set('0')
        self.s.minutes.set('0')
        with self.assertRaises(ValueError):
            self.s.form_value()
        self.s.hours.set('1')
        self.s.add_button.invoke()
        self.assertEqual(len(self.s.entries), 1)
        self.assertEqual(self.s.table.item(self.s.table.get_children()[0], 'values')[1], '1')
        self.q.start.assert_not_called()
        self.q.running = True
        self.s.refresh()
        self.assertTrue(self.s.add_button.instate(['disabled']))
