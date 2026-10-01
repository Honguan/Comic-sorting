from decimal import Decimal
from pathlib import Path
import sys
import json
import threading
import unittest

from queue_worker import parse_bt_usage, parse_bt_request_usage, parse_jev_stats, run_translation


def usage_line(scope='', tokens=1274185, cost='2.483284', subtotal=None, unpriced=0):
    return (f'[INFO ] module_manager:_finish_llm_usage_run:2163 - LLM {scope}run usage: '
            f'status=finished, requests=94, total_tokens={tokens}, missing_usage_requests=0, '
            f'estimated_cost_usd={cost}, priced_subtotal_usd={cost if subtotal is None else subtotal}, unpriced_requests={unpriced}, '
            'price_basis=OpenAI Standard API equivalent (not a bill), rates_date=2026-09-11')


class UsageTests(unittest.TestCase):
    def test_self_estimated_cost_includes_cache_retry_and_memory_requests(self):
        lines = [
            "[INFO ] module_manager:_imgtrans_pipeline:1123 - LLM translation run: profile_id='codex', model='gpt-6.1-sol', context='page'",
            '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: page=1.png, attempt=1, prompt=1000, completion=200, reasoning=150, total=1200, cache_hit=400, cache_write=100',
            '[DEBUG] llm_chat:request_chat_completion:378 - LLM output-limited response: prompt=100, completion=20, total=120, cache_hit=0, cache_write=0, content="model=unknown, total=999"',
            '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: page=memory-compaction, prompt=100, completion=10, total=110, cache_hit=0, cache_write=0',
            'finished translating all dirs']
        records = []
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *args: None, usage=records.append)
        latest = {record['scope']: record for record in records}
        self.assertEqual(Decimal(latest['total']['cost']), Decimal('0.003990'))
        self.assertEqual(latest['total']['total_tokens'], 1430)
        self.assertEqual(latest['total']['requests'], 3)
        self.assertEqual(latest['total']['unpriced_requests'], 0)
        self.assertIn('Standard API equivalent', latest['translation']['price_basis'])
        self.assertEqual(latest['translation']['rates_date'], '2026-10-01')
        from queue_history import usage_columns
        self.assertEqual(usage_columns([latest['total']])[1:4], ('US$0.01', '3', '完整'))

    def test_estimates_require_known_model_and_consistent_usage(self):
        prefix = '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: '
        short = 'prompt=100, completion=20, total=120'
        self.assertEqual(parse_bt_request_usage(prefix + short, 'gpt-6.1-sol')['cost'], Decimal('0.0004'))
        for detail, model in [(short, 'unknown-model'), ('total=120', 'gpt-6.1-sol'),
                              (short + ', cache_hit=101', 'gpt-6.1-sol'),
                              ('prompt=100, completion=20, total=999', 'gpt-6.1-sol'),
                              (short + ', cache_write=1', 'gpt-5.5')]:
            with self.subTest(detail=detail, model=model):
                record = parse_bt_request_usage(prefix + detail, model)
                self.assertIsNone(record['cost'])
                self.assertEqual(record['unpriced_requests'], 1)
        for detail in [short + ', cache_hit=-1', short + ', cache_write=NaN']:
            self.assertIsNone(parse_bt_request_usage(prefix + detail, 'gpt-6.1-sol'))
        for prompt, expected in [(272000, '0.546'), (272001, '1.091004')]:
            record = parse_bt_request_usage(prefix + f'prompt={prompt}, completion=200, cache_hit=0, cache_write=0', 'gpt-6.1-sol')
            self.assertEqual(record['cost'], Decimal(expected))

    def test_unpriced_native_summaries_preserve_matching_local_estimates(self):
        lines = ["[INFO ] module_manager:_imgtrans_pipeline:1123 - LLM translation run: model='gpt-6.1-sol'",
                 '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: prompt=100, completion=20, total=120',
                 usage_line('translation ', 120, 'unavailable', unpriced=94).replace('requests=94', 'requests=1'),
                 usage_line('', 120, 'unavailable', unpriced=94).replace('requests=94', 'requests=1'),
                 'finished translating all dirs']
        records = []
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *args: None, usage=records.append)
        self.assertEqual(records[-1]['cost'], Decimal('0.0004'))
        self.assertEqual(records[-1]['unpriced_requests'], 0)
        lines[-2] = usage_line('', 120, '0.02').replace('requests=94', 'requests=1')
        records.clear()
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *args: None, usage=records.append)
        self.assertEqual(records[-1]['cost'], Decimal('0.02'))

    def test_official_request_logs_count_each_response_not_cumulative_or_details(self):
        lines = [
            '[INFO ] ocr_llm:_request_ocr_content:350 - Used 10 tokens (Total: 100010)',
            '[INFO ] ocr_llm:_request_ocr_content:350 - Used 20 tokens (Total: 100030)',
            '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: page=1.png, attempt=1, prompt=100, completion=20, reasoning=12, total=120, cache_hit=80',
            '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: page=1.png, attempt=2, prompt=6, completion=4, reasoning=2, cache_hit=5',
            '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: page=2.png, attempt=1, prompt_cache_diagnostics=not_reported',
            "[DEBUG] llm_chat:request_chat_completion:373 - LLM output-limited response: prompt=5, completion=3, total=8, chars=12, content='total=999'",
            'finished translating all dirs']
        records = []
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *args: None, usage=records.append)
        latest = {record['scope']: record for record in records}
        self.assertEqual(latest['OCR']['total_tokens'], 30)
        self.assertEqual(latest['OCR']['requests'], 2)
        self.assertEqual(latest['translation']['total_tokens'], 138)
        self.assertEqual(latest['translation']['requests'], 4)
        self.assertEqual(latest['total']['total_tokens'], 168)
        self.assertEqual(latest['total']['requests'], 6)
        self.assertEqual(latest['total']['missing_usage_requests'], 1)
        self.assertEqual(latest['total']['unpriced_requests'], 6)
        self.assertIsNone(latest['total']['cost'])

    def test_native_summaries_override_request_snapshots_without_double_counting(self):
        lines = ['[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: prompt=8, completion=2, total=10',
                 usage_line('translation ', 10, '0.01'), usage_line('', 10, '0.01'),
                 'finished translating all dirs']
        records = []
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *args: None, usage=records.append)
        self.assertEqual(records[0]['scope'], 'translation')
        self.assertEqual(records[0]['requests'], 1)
        self.assertEqual(records[-1]['total_tokens'], 10)
        self.assertEqual(records[-1]['cost'], Decimal('0.01'))

    def test_unknown_usage_keeps_known_cost_subtotal_when_process_fails(self):
        lines = ["[INFO ] module_manager:_imgtrans_pipeline:1123 - LLM translation run: model='gpt-6.1-sol'",
                 '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: prompt=100, completion=20, total=120',
                 '[INFO ] ocr_llm:_request_ocr_content:350 - Used 10 tokens (Total: 100010)',
                 '[DEBUG] trans_llm:_log_token_usage:1182 - LLM token usage: total=50']
        records = []
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r}); raise SystemExit(1)'],
                            Path.cwd(), None, threading.Event(), lambda *args: None, usage=records.append)
        latest = records[-1]
        self.assertEqual(latest['scope'], 'total')
        self.assertEqual(latest['cost'], Decimal('0.0004'))
        self.assertEqual(latest['total_tokens'], 180)
        self.assertEqual(latest['unpriced_requests'], 2)
        self.assertTrue(latest['cost_incomplete'])
        from queue_history import usage_columns
        self.assertEqual(usage_columns([latest])[3], '僅含已知金額')

    def test_jev_input_tokens_do_not_enter_llm_usage(self):
        data = dict(event='finished', audit_id='new-page', elapsed_seconds=2,
                    blocks=[dict(action='kept', removed_characters=0)],
                    usage=dict(requests=3, reported_input_requests=2, input_tokens=1500))
        line = '[INFO ] jev_filter:record:205 - Jev OCR filter audit: ' + json.dumps(data)
        stats, usage = [], []
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, '-c', f'print({line!r}); raise SystemExit(1)'],
                            Path.cwd(), None, threading.Event(), lambda *args: None,
                            usage=usage.append, jev=stats.append)
        self.assertEqual(usage, [])
        self.assertEqual(stats[0]['input_tokens'], 1500)
        self.assertEqual(stats[0]['missing_input_usage_requests'], 1)
        self.assertNotIn('total_tokens', stats[0])
        from queue_history import jev_columns, jev_totals
        self.assertEqual(jev_columns(jev_totals([{'jev_stats': stats[0]}]))[:3], ('3', '1.50K', '1'))
        data['usage'].update(cost_nano_usd=187950, reported_cost_requests=1, estimated_cost_requests=1)
        priced = parse_jev_stats('[INFO ] jev_filter:record:205 - Jev OCR filter audit: ' + json.dumps(data))
        self.assertEqual(priced['unpriced_requests'], 1)
        self.assertEqual(jev_columns(priced)[3:6], ('US$0.000187950', '回報及估算', '1'))
        from queue_errors import error_info
        self.assertEqual(error_info('failed', 'RuntimeError: Codex translation timed out again after Jev cleanup; original page retained.')[0], 'REQUEST_TIMEOUT')

    def test_jev_audit_deduplicates_and_survives_failed_process(self):
        data = dict(event='finished', audit_id='page1', status='completed', elapsed_seconds=1.25,
                    blocks=[dict(action='trimmed', removed_characters=4), dict(action='kept', removed_characters=0)],
                    usage=dict(requests=3, reported_requests=2, total_tokens=1500))
        line = '[INFO ] jev_filter:record:205 - Jev OCR filter audit: ' + json.dumps(data)
        stats, usage = [], []
        script = f'print({line!r}); print({line!r}); print({usage_line("Jev ", 1500, "0.01")!r}); raise SystemExit(1)'
        with self.assertRaises(RuntimeError):
            run_translation([sys.executable, '-c', script], Path.cwd(), None, threading.Event(),
                            lambda *args: None, usage=usage.append, jev=stats.append)
        self.assertEqual(len(stats), 1)
        self.assertEqual((stats[0]['calls'], stats[0]['trimmed_blocks'], stats[0]['elapsed_seconds']), (1, 1, 1.25))
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0]['missing_usage_requests'], 1)
        self.assertEqual(usage[-1]['cost'], Decimal('0.01'))
        for bad in ('{}', '{invalid', json.dumps(dict(data, elapsed_seconds=-1))):
            self.assertIsNone(parse_jev_stats('[INFO ] jev_filter:record:205 - Jev OCR filter audit: ' + bad))

    def test_stage_elapsed_time_comes_from_progress_not_eta(self):
        lines = ['OCR: 50%|#####     | 1/2 [01:02<00:04, 1.0s/it]',
                 'Translation: 100%|##########| 2/2 [1:02:03<00:00, 1.0s/it]',
                 'Inpaint: 50%|#####     | 1/2 [00:99<00:04, 1.0s/it]']
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
        for subtotal in ('0.998713', '0'):
            self.assertEqual(parse_bt_usage(usage_line(cost='unavailable', subtotal=subtotal, unpriced=1))['cost'], Decimal(subtotal))
        self.assertIsNone(parse_bt_usage(usage_line(cost='unavailable', subtotal='0', unpriced=94))['cost'])
        for subtotal in ('NaN', 'Infinity', '-1', 'invalid'):
            self.assertIsNone(parse_bt_usage(usage_line(cost='unavailable', subtotal=subtotal, unpriced=1)))
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
