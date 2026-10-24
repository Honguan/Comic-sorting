from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bt_run_bridge import install_usage
from model_pricing import effective_rates, get_price, validate_rates
from queue_worker import parse_bt_request_usage, run_translation
from queue_stats import merge_snapshots, SnapshotTotals


class ModelPricingTests(unittest.TestCase):
    def test_native_summary_preserves_only_reconcilable_model_breakdowns(self):
        requests = [dict(scope='translation', model=model, profile_id='test', usage='prompt=100, completion=20, total=120')
                    for model in ('gpt-4o-mini', 'private-model')]
        for cost, count, models in [('unavailable', 2, {'gpt-4o-mini', 'private-model'}), ('0.001', 2, {''}), ('unavailable', 3, {''})]:
            lines = ['COMIC_SORTING_USAGE:' + json.dumps(value) for value in requests]
            lines += [f'[INFO] module_manager:_finish_llm_usage_run:1 - LLM translation run usage: requests={count}, total_tokens={120 * count}, '
                      f'missing_usage_requests=0, unpriced_requests=0, estimated_cost_usd={cost}',
                      'finished translating all dirs']
            records = []
            run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                            threading.Event(), lambda *_args: None, usage=records.append,
                            pricing={'private-model': ['2', '2', '2', '10']})
            total = records[-1]
            self.assertEqual(set(total['by_model']), models)
            self.assertEqual(sum(value['total_tokens'] for value in total['by_model'].values()), 120 * count)
            if count == 3:
                self.assertIsNone(total['cost'])
            else:
                self.assertEqual(sum(Decimal(value['cost']) for value in total['by_model'].values()), total['cost'])

    def test_any_custom_model_and_catalog_context_tiers(self):
        price = get_price('private-model', {'private-model': ['1', '.1', '1.25', '5']})
        line = '[INFO] trans_llm:bridge:0 - LLM token usage: prompt=200, completion=50, total=250, cache_hit=80, cache_write=20'
        record = parse_bt_request_usage(line, 'private-model', price)
        self.assertEqual(record['cost'], Decimal('0.000383'))
        self.assertEqual(record['models'], ['private-model'])
        pricing = {'prompt': '0.000002', 'completion': '0.000012', 'input_cache_read': '0.000001',
                   'overrides': [{'min_prompt_tokens': 200000, 'prompt': '0.000004', 'completion': '0.000018'}]}
        with patch('model_pricing.fetch_catalog', return_value={'vendor/model': pricing}):
            price = get_price('model', catalog=True)
        for count, expected in [(200000, '.412'), (200001, '.818004')]:
            record = parse_bt_request_usage(f'[INFO] trans_llm:bridge:0 - LLM token usage: prompt={count}, completion=1000', 'model', price)
            self.assertEqual(record['cost'], Decimal(expected))
        self.assertEqual(effective_rates(price, 200001)[1], Decimal('1'))  # Inherit base cache price.
        self.assertEqual(get_price('gpt-4o')['rates'][0], Decimal('2.50'))
        with patch('model_pricing.fetch_catalog', return_value={'openai/gpt-4o-2024-05-13': {'prompt': '.000005', 'completion': '.000015'}}):
            snapshot = get_price('gpt-4o-2024-05-13', catalog=True)
        self.assertEqual(effective_rates(snapshot, 100)[0], Decimal('5'))
        for values in [['1', '-1', '1', '2'], ['NaN', '1', '1', '2'], ['1', '1', '1', 'Infinity']]:
            with self.assertRaises(ValueError):
                validate_rates(values)

    def test_structured_usage_keeps_each_model_without_duplicate_native_counts(self):
        requests = [dict(scope='OCR', model='gpt-4o-mini', profile_id='ocr', usage='prompt=100, completion=20, total=120'),
                    dict(scope='translation', model='private-model', profile_id='custom', usage='prompt=100, completion=20, total=120')]
        lines = []  # Later persistent jobs do not emit READY again.
        for request in requests:
            lines += ['COMIC_SORTING_USAGE:' + json.dumps(request),
                      '[INFO] ocr_llm:run:1 - Used 120 tokens (Total: 1000120)',
                      '[DEBUG] trans_llm:run:1 - LLM token usage: prompt=100, completion=20, total=120']
        lines.append('finished translating all dirs')
        records = []
        run_translation([sys.executable, '-c', f'print({chr(10).join(lines)!r})'], Path.cwd(), None,
                        threading.Event(), lambda *_args: None, usage=records.append,
                        pricing={'private-model': ['2', '2', '2', '10']})
        total = records[-1]
        self.assertEqual((total['requests'], total['total_tokens']), (2, 240))
        self.assertEqual(total['cost'], Decimal('0.000427'))
        self.assertEqual(total['models'], ['gpt-4o-mini', 'private-model'])
        self.assertEqual(set(total['model_rates']), {'gpt-4o-mini', 'private-model'})
        unknown = parse_bt_request_usage('[INFO] trans_llm:run:1 - LLM token usage: total=10', 'unknown-model')
        self.assertEqual(unknown['models'], ['unknown-model'])
        self.assertIsNone(unknown['cost'])
        local = parse_bt_request_usage('[INFO] ocr_llm:run:1 - Used 10 tokens (Total: 100)',
                                       'local-model', get_price('local-model', local=True))
        self.assertEqual(local['cost'], Decimal(0))
        merged = merge_snapshots((total, unknown))
        stats = SnapshotTotals()
        stats[0] = merged
        self.assertEqual(stats.aggregate()['models'], ['gpt-4o-mini', 'private-model', 'unknown-model'])
        stats[0] = unknown
        unknown['models'].append('mutated')
        stats[0] = local
        self.assertEqual(stats.aggregate()['models'], ['local-model'])
        del stats[0]
        self.assertEqual(stats.aggregate(), {})

    def test_bridge_preserves_concurrent_model_identity_and_output_limit_usage(self):
        class Result:
            def __init__(self, usage):
                self.usage = usage
        class Requester:
            def request_chat_completion(self, profile, api_args, codex_turn=None):
                if api_args['model'] == 'failed':
                    raise RuntimeError('transport failure')
                result = Result('prompt=100, completion=20, total=120')
                result.codex_turn = codex_turn
                if api_args['model'] == 'limited':
                    raise RuntimeError('output limit')
                return result
        class OCR(Requester):
            pass
        OCR.__module__ = 'ballontranslator.modules.ocr.ocr_llm'
        chat = SimpleNamespace(LLMChatRequester=Requester, LLMChatResult=Result)
        output = io.StringIO()
        with redirect_stdout(output):
            install_usage(chat, lambda result: result.usage)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: OCR().request_chat_completion(SimpleNamespace(id='ocr'), {'model': f'model-{index}'}), range(12)))
            # Newer BallonsTranslator passes extra keywords; the wrapper must forward them.
            turn = Requester().request_chat_completion(SimpleNamespace(id='translator'), {'model': 'turn'}, codex_turn=2)
            self.assertEqual(turn.codex_turn, 2)
            with self.assertRaises(RuntimeError):
                Requester().request_chat_completion(SimpleNamespace(id='translator'), {'model': 'limited'})
            with self.assertRaises(RuntimeError):
                Requester().request_chat_completion(SimpleNamespace(id='translator'), {'model': 'failed'})
        records = [json.loads(line.split(':', 1)[1]) for line in output.getvalue().splitlines()
                   if line.startswith('COMIC_SORTING_USAGE:')]
        self.assertEqual(len(records), 15)
        self.assertEqual({record['model'] for record in records}, {f'model-{index}' for index in range(12)} | {'turn', 'limited', 'failed'})
        self.assertTrue(all(record['scope'] == 'OCR' for record in records if record['model'].startswith('model-')))
        self.assertEqual(next(record['scope'] for record in records if record['model'] == 'limited'), 'translation')
        self.assertEqual(next(record['usage'] for record in records if record['model'] == 'failed'), '')


if __name__ == '__main__':
    unittest.main()
