import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from translation_queue import TranslationQueue


class HistorySnapshotTests(unittest.TestCase):
    def test_snapshot_groups_once_and_preserves_per_chapter_values(self):
        q = TranslationQueue.__new__(TranslationQueue)
        q.active_jobs = [SimpleNamespace(status='done', error=''),
                         SimpleNamespace(status='failed', error='timeout'),
                         SimpleNamespace(status='pending', error='')]
        q.history_run = {'jobs': [{'path': str(i), 'stage_seconds': {'stale': 9},
                                   'usage': {'stale': {}}} for i in range(3)]}
        q.app = SimpleNamespace(history_path='unused')
        q.jev_records = {1: {'requests': 1}}
        stages = {(0, 'Translation'): 2.5, (1, 'OCR'): 3.0}
        usage = {(0, 'translation'): {'cost': Decimal('0.12'), 'requests': 2},
                 (1, 'total'): {'cost': None, 'requests': 1}}
        q.stage_times = Mock(items=Mock(return_value=stages.items()))
        q.usage_records = Mock(items=Mock(return_value=usage.items()))
        with patch('translation_queue.save_run') as save:
            self.assertTrue(q.save_history())
        jobs = save.call_args.args[1]['jobs']
        self.assertEqual([j['stage_seconds'] for j in jobs],
                         [{'Translation': 2.5}, {'OCR': 3.0}, {}])
        self.assertEqual([j['usage'] for j in jobs],
                         [{'translation': {'cost': '0.12', 'requests': 2}},
                          {'total': {'cost': None, 'requests': 1}}, {}])
        self.assertEqual(jobs[1]['jev_stats'], {'requests': 1})
        self.assertEqual(jobs[1]['error'], 'timeout')
        self.assertEqual(jobs[2]['status'], 'pending')
        self.assertEqual(usage[0, 'translation']['cost'], Decimal('0.12'))
        q.stage_times.items.assert_called_once_with()
        q.usage_records.items.assert_called_once_with()
        with patch('translation_queue.save_run'):
            self.assertTrue(q.save_history(refresh=False))
        q.stage_times.items.assert_called_once_with()
        q.usage_records.items.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
