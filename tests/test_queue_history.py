import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queue_history import find_runs, save_run, scope_records, usage_text


class HistoryStorageTests(unittest.TestCase):
    def test_updates_search_and_exact_usage_without_double_counting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history 中文.sqlite3'
            self.assertEqual(find_runs(path), [])
            self.assertFalse(path.exists())
            usage = dict(total_tokens=1274185, requests=94, cost='2.483284',
                         missing_usage_requests=0, unpriced_requests=0)
            record = dict(id='run-one', started_at='2026-09-13T01:02:03+08:00', status='running',
                          jobs=[dict(path='D:/Mangas/漫畫 100%_Test/Chapter 1',
                                     usage={'OCR': usage, 'translation': usage, 'total': usage})])
            save_run(path, record)
            record['status'] = 'done'
            save_run(path, record)
            stored = find_runs(path, '100%_test', '2026-09-13', '2026-09-13')
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]['status'], 'done')
            self.assertEqual(stored[0]['jobs'][0]['usage']['total']['cost'], '2.483284')
            self.assertEqual(len(scope_records(stored[0], 'total')), 1)
            self.assertIn('1.27M', usage_text(scope_records(stored[0], 'total')))
            self.assertIn('US$2.49', usage_text(scope_records(stored[0], 'total')))
            other = copy.deepcopy(record)
            other.update(id='run-two', started_at='2026-09-14T10:00:00+08:00')
            save_run(path, other)
            self.assertEqual([row['id'] for row in find_runs(path)], ['run-two', 'run-one'])
            self.assertEqual(len(find_runs(path, start='2026-09-14')), 1)
            self.assertEqual(find_runs(path, "' OR 1=1 --"), [])
            for start, end in [('bad-date', ''), ('2026-09-14', '2026-09-13')]:
                with self.assertRaises(ValueError):
                    find_runs(path, start=start, end=end)
            usage['cost'] = None
            self.assertIn('未完整提供', usage_text([usage]))
            self.assertEqual(usage_text([]), '尚未回報')

    def test_corrupt_history_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.sqlite3'
            original = b'not a database'
            path.write_bytes(original)
            with self.assertRaises(sqlite3.Error):
                save_run(path, dict(id='run', started_at='2026-09-13', jobs=[]))
            with self.assertRaises(sqlite3.Error):
                find_runs(path)
            self.assertEqual(path.read_bytes(), original)
