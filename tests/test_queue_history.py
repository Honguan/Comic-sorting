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
            self.assertIn('無法預估', usage_text([usage]))
            self.assertEqual(usage_text([]), '尚未回報')

    def test_known_costs_are_summed_even_with_missing_or_partial_estimates(self):
        usage = dict(total_tokens=1000, requests=10, cost='1.001', missing_usage_requests=0, unpriced_requests=0)
        partial = dict(usage, cost='0.008', missing_usage_requests=1, unpriced_requests=1)
        unknown = dict(usage, cost=None, unpriced_requests=10)
        for records, expected in (([usage, partial, unknown], 'US$1.01'),
                                  ([partial], 'US$0.01'), ([usage, unknown], 'US$1.01'),
                                  ([dict(usage, cost='0'), unknown], 'US$0.00'),
                                  ([unknown], '無法預估')):
            with self.subTest(expected=expected, records=records):
                text = usage_text(records)
                self.assertIn(expected, text)
                if expected.startswith('US$'):
                    self.assertNotIn('無法預估', text)
                    self.assertIn('僅含已知金額', text)
        self.assertIn('Token 回報不完整', usage_text([partial]))
        self.assertEqual(usage_text([], empty_text='無法預估'), '無法預估')

    def test_legacy_subtotals_are_recovered_from_matching_saved_errors_without_writes(self):
        from queue_worker import parse_bt_usage
        from test_bt_usage import usage_line
        line = usage_line(cost='unavailable', subtotal='0.998713', unpriced=1)
        usage = dict(parse_bt_usage(line), cost=None)
        record = dict(id='legacy', started_at='2026-09-14T17:46:19+08:00', status='done_warning', jobs=[
            dict(path='Chapter 1', status='failed', usage={'total': usage}, error=line),
            dict(path='Chapter 2', status='done', usage={'total': dict(usage, cost='6.717269')}, error=line),
            dict(path='Chapter 3', status='failed', usage={'total': dict(usage, total_tokens=1)}, error=line),
            dict(path='Chapter 4', status='failed', usage={}, error=line),
        ])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.sqlite3'
            save_run(path, record)
            original = path.read_bytes()
            for _ in range(2):
                stored = find_runs(path)[0]
                self.assertEqual(stored['jobs'][0]['usage']['total']['cost'], '0.998713')
                self.assertEqual(stored['jobs'][1]['usage']['total']['cost'], '6.717269')
                self.assertIsNone(stored['jobs'][2]['usage']['total']['cost'])
                self.assertEqual(stored['jobs'][3]['usage'], {})
                self.assertIn('US$7.72', usage_text(scope_records(stored, 'total')))
            self.assertEqual(path.read_bytes(), original)

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
