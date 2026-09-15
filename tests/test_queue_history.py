import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queue_history import find_runs, history_totals, save_run, scope_records, usage_text


class HistoryStorageTests(unittest.TestCase):
    def test_grand_totals_include_all_runs_and_known_costs_without_double_counting(self):
        from queue_worker import parse_bt_usage
        from test_bt_usage import usage_line
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.sqlite3'
            empty = history_totals(path)
            self.assertEqual((empty['runs'], empty['jobs'], empty['elapsed_seconds']), (0, 0, 0))
            self.assertEqual(empty['usage']['total'], '尚未回報')
            self.assertFalse(path.exists())
            total = dict(total_tokens=1000, requests=2, cost='0.004', missing_usage_requests=0, unpriced_requests=0)
            for index in range(200):
                save_run(path, dict(id=str(index), started_at='2026-09-15', elapsed_seconds=1.25, jobs=[
                    dict(path='Chapter 1', usage={'OCR': dict(total, total_tokens=250, requests=1, cost='0.001'),
                                               'translation': dict(total, total_tokens=750, requests=1, cost='0.003'),
                                               'total': total})]))
            line = usage_line(cost='unavailable', subtotal='0.998713', unpriced=1)
            legacy = dict(parse_bt_usage(line), cost=None)
            save_run(path, dict(id='legacy', started_at='2026-09-14', elapsed_seconds=None, jobs=[
                dict(path='Older', status='failed', error=line, usage={'total': legacy})]))
            for name, cost, seconds in (('unknown', None, 15), ('zero', '0', 35)):
                record = dict(id=name, started_at='2026-09-14', elapsed_seconds=seconds, jobs=[
                    dict(path=name, status='cancelled', usage={'total': dict(total, cost=cost)})])
                save_run(path, record)
                save_run(path, record)  # Updating a checkpoint must not add a second batch.
            original = path.read_bytes()
            self.assertEqual(len(find_runs(path)), 200)
            result = history_totals(path)
            self.assertEqual((result['runs'], result['jobs'], result['elapsed_seconds']), (203, 203, 300))
            self.assertIn('50.00K tokens｜預估 US$0.20｜200', result['usage']['OCR'])
            self.assertIn('150.00K tokens｜預估 US$0.60｜200', result['usage']['translation'])
            self.assertIn('1.48M tokens｜預估 US$1.80｜498', result['usage']['total'])
            self.assertIn('僅含已知金額', result['usage']['total'])
            self.assertEqual(path.read_bytes(), original)

    def test_legacy_history_adds_sort_index_on_save_and_keeps_latest_200_order(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.sqlite3'
            records = [dict(id=str(i), started_at=f'2026-09-14T12:00:{i % 60:02}+08:00', jobs=[])
                       for i in range(205)]
            db = sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY, started_at TEXT NOT NULL, paths TEXT NOT NULL, data TEXT NOT NULL)')
                db.executemany('INSERT INTO runs VALUES(?,?,?,?)',
                               [(r['id'], r['started_at'], '', json.dumps(r)) for r in records])
                db.commit()
            finally:
                db.close()
            original = path.read_bytes()
            before = find_runs(path)
            self.assertEqual(len(before), 200)
            self.assertEqual(path.read_bytes(), original)
            save_run(path, records[0])
            self.assertEqual(find_runs(path), before)
            db = sqlite3.connect(path)
            try:
                plan = db.execute('EXPLAIN QUERY PLAN SELECT data FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 200').fetchall()
                self.assertTrue(any('USING INDEX runs_started_at' in row[3] for row in plan), plan)
                self.assertFalse(any('TEMP B-TREE' in row[3] for row in plan), plan)
            finally:
                db.close()

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
