from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest

from queue_worker import parse_bt_usage, run_translation


def usage_line(scope='', tokens=1274185, cost='2.483284'):
    return (f'[INFO ] module_manager:_finish_llm_usage_run:2163 - LLM {scope}run usage: '
            f'status=finished, requests=94, total_tokens={tokens}, missing_usage_requests=0, '
            f'estimated_cost_usd={cost}, priced_subtotal_usd=2.483284, unpriced_requests=0, '
            'price_basis=OpenAI Standard API equivalent (not a bill), rates_date=2026-09-11')


class UsageTests(unittest.TestCase):
    def test_stage_elapsed_time_comes_from_progress_not_eta(self):
        lines = ['OCR: 50%|#####     | 1/2 [01:02<00:04, 1.0s/it]',
                 'Translation: 100%|##########| 2/2 [1:02:03<00:00, 1.0s/it]',
                 'Inpaint: 50%|#####     | 1/2 [00:99<00:04, 1.0s/it]',
                 'finished translating all dirs']
        times = []
        with self.assertRaises(RuntimeError):  # Timing must survive an incomplete run.
            run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                            threading.Event(), lambda *args: None, timing=lambda *args: times.append(args))
        self.assertEqual(times, [('OCR', 62), ('Translation', 3723)])

    def test_real_summary_values_and_missing_estimates(self):
        for scope, tokens, cost in [('OCR ', 503029, '0.971684'), ('translation ', 771156, '1.511601'), ('', 1274185, '2.483284')]:
            record = parse_bt_usage(usage_line(scope, tokens, cost))
            self.assertEqual(record['scope'], scope.strip() or 'total')
            self.assertEqual(record['total_tokens'], tokens)
            self.assertEqual(record['cost'], Decimal(cost))
        self.assertIsNone(parse_bt_usage(usage_line(cost='unavailable'))['cost'])
        for line in [usage_line(cost='NaN'), usage_line(tokens=-1), 'LLM run usage: invalid', usage_line(cost='invalid')]:
            self.assertIsNone(parse_bt_usage(line))

    def test_summary_reaches_callback_even_when_process_fails(self):
        records = []
        script = f'print({usage_line()!r}); raise SystemExit(1)'
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, '-c', script], Path.cwd(), None,
                            threading.Event(), lambda *args: None, usage=records.append)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['total_tokens'], 1274185)
