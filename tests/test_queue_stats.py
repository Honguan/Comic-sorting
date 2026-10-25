import unittest
from decimal import Decimal

from queue_stats import SnapshotTotals, merge_snapshots, model_usage


class SnapshotTests(unittest.TestCase):
    def test_model_breakdown_replacement_is_independent_and_conserves_totals(self):
        first = dict(model='a', models=['a'], requests=1, total_tokens=100,
                     prompt=80, completion=20, cost='0.001', unpriced_requests=0, missing_usage_requests=0)
        unknown = dict(model='b', models=['b'], requests=1, total_tokens=50,
                       cost=None, unpriced_requests=1, missing_usage_requests=0)
        for record in (first, unknown):
            record['by_model'] = model_usage(record)
        merged = merge_snapshots((first, unknown))
        stats = SnapshotTotals(lambda key: key[1])
        stats[0, 'total'] = merged
        merged['by_model']['a']['total_tokens'] = 999
        result = stats.aggregate('total')
        self.assertEqual(sum(value['total_tokens'] for value in result['by_model'].values()), 150)
        self.assertEqual(result['by_model']['a']['cost'], Decimal('0.001'))
        self.assertIsNone(result['by_model']['b']['cost'])
        self.assertTrue(result['cost_incomplete'])
        stats[0, 'total'] = first
        self.assertEqual(set(stats.aggregate('total')['by_model']), {'a'})
        self.assertNotIn('cost_incomplete', stats.aggregate('total'))
        stats[1, 'total'] = unknown
        stats[0, 'OCR'] = first
        self.assertEqual(set(stats.aggregate('OCR')['by_model']), {'a'})
        del stats[1, 'total']
        self.assertEqual(set(stats.aggregate('total')['by_model']), {'a'})
        del stats[0, 'total']
        self.assertEqual(stats.aggregate('total'), {})
        stats.clear()
        self.assertFalse(stats.model_totals)

    def test_legacy_mixed_models_remain_unallocated(self):
        record = dict(model='a', models=['a', 'b'], requests=2, total_tokens=240, cost='0.004')
        self.assertEqual(model_usage(record), {'': dict(requests=2, total_tokens=240, cost='0.004')})
        record['models'] = ['a']
        self.assertEqual(set(model_usage(record)), {'a'})

    def test_replacement_groups_unknown_and_zero_costs(self):
        stats = SnapshotTotals(lambda key: key[1])
        first = dict(total_tokens=10, requests=1, cost='0', missing_usage_requests=0)
        stats[0, 'total'] = first
        first['total_tokens'] = 999  # Mutating the source must not corrupt the old delta.
        stats[1, 'total'] = dict(total_tokens=20, requests=1, cost=None, missing_usage_requests=1)
        stats[0, 'OCR'] = dict(total_tokens=5, cost='0.3')
        self.assertEqual(stats.aggregate('total')['total_tokens'], 30)
        self.assertEqual(stats.aggregate('total')['cost'], Decimal('0'))
        self.assertTrue(stats.aggregate('total')['cost_incomplete'])
        stats[0, 'total'] = dict(total_tokens=15, requests=2, cost=None, missing_usage_requests=0)
        self.assertEqual(stats.aggregate('total')['total_tokens'], 35)
        self.assertIsNone(stats.aggregate('total')['cost'])
        self.assertEqual(stats.aggregate('total')['requests'], 3)
        self.assertEqual(stats.aggregate('OCR')['cost'], Decimal('0.3'))
        del stats[1, 'total']
        self.assertEqual(stats.aggregate('total')['total_tokens'], 15)
        stats.clear()
        self.assertEqual(stats.aggregate('total'), {})
        self.assertFalse(stats)

    def test_missing_elapsed_is_not_zero_and_phase_snapshots_match(self):
        stats = SnapshotTotals()
        stats.update({0: {'calls': 1}, 1: {'calls': 2, 'elapsed_seconds': 0}})
        self.assertEqual(stats.aggregate()['elapsed_seconds'], 0)
        stats[1] = {'calls': 2}
        self.assertNotIn('elapsed_seconds', stats.aggregate())
        merged = merge_snapshots([dict(scope='total', cost=None, total_tokens=5),
                                  dict(scope='total', cost='0.004', total_tokens=7)])
        self.assertEqual(merged, dict(scope='total', cost='0.004', total_tokens=12, cost_incomplete=True))
        stats.clear()
        stats[0] = merged
        self.assertTrue(stats.aggregate()['cost_incomplete'])
        stats[0] = dict(cost='0.004', total_tokens=12)
        self.assertNotIn('cost_incomplete', stats.aggregate())
