"""Offline adversarial provider/configuration regressions. No credentials or network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
SCRATCH = tempfile.TemporaryDirectory()
for key, leaf in [('ORASK_CONFIG_DIR', 'config'), ('ORASK_STATE_DIR', 'state'),
                  ('ORASK_CACHE_DIR', 'cache')]:
    os.environ[key] = str(Path(SCRATCH.name) / leaf)
os.environ['OPENROUTER_API_KEY'] = ''
from orask import core


def block_network(event, args):
    if event == 'socket.connect':
        raise AssertionError('offline boundary tests cannot use the network')


sys.addaudithook(block_network)
CATALOG = [{'id': 'test/model', 'name': 'Test', 'context_length': 100000,
            'top_provider': {'max_completion_tokens': 32000},
            'reasoning': {'supported_efforts': ['low', 'medium', 'high', 'max']},
            'pricing': {'prompt': '0.000001', 'completion': '0.000001'}}]


class Boundaries(unittest.TestCase):
    def setUp(self):
        core._config_cache = None
        core.USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        core.USER_CONFIG.unlink(missing_ok=True)
        self.catalog = patch.object(core, 'get_catalog', return_value=CATALOG)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def ask_response(self, response):
        with patch.object(core, '_request', return_value=response):
            return core.ask('review', model='test/model', max_tokens=1000)

    def test_config_collection_shapes(self):
        for key, value in [('aliases', 'oops'), ('roles', []), ('categories', ['coding']),
                           ('categories', {'coding': None}), ('allowed_models', 'test/model'),
                           ('deny_file_patterns', [4]), ('aliases', {'x': 7}),
                           ('roles', {'advisor': []}), ('default_panel', 4)]:
            with self.subTest(key=key, value=value):
                core.USER_CONFIG.write_text(json.dumps({key: value}))
                with self.assertRaises(core.OpenRouterError):
                    core.load_config(refresh=True)

    def test_boolean_cannot_disable_numeric_guard(self):
        core._config_cache = dict(core.load_config(), max_cost_usd_per_call=False,
                                  max_input_chars=False)
        self.assertEqual(core._float_setting('max_cost_usd_per_call', 1.0), 1.0)
        self.assertEqual(core._setting('max_input_chars', 600000), 600000)

    def test_catalog_nested_shapes(self):
        for field, value in [('id', []), ('id', ['invalid']), ('name', []),
                             ('pricing', []), ('top_provider', 'bad'),
                             ('reasoning', {'supported_efforts': [3]}),
                             ('architecture', {'input_modalities': {}}),
                             ('benchmarks', {'artificial_analysis': []}),
                             ('context_length', float('inf'))]:
            with self.subTest(field=field):
                self.assertFalse(core._valid_catalog([{**CATALOG[0], field: value}]))

    def test_optional_usage_cannot_destroy_answer(self):
        for usage in [42, [], {'prompt_tokens': 'oops'},
                      {'completion_tokens_details': 42, 'prompt_tokens_details': []},
                      {'cost': float('nan'), 'completion_tokens': float('inf')}]:
            with self.subTest(usage=usage):
                result = self.ask_response({'choices': [{'message': {'content': 'final'},
                                                         'finish_reason': 'stop'}], 'usage': usage})
                self.assertTrue(result['ok'])
                self.assertEqual(result['answer'], 'final')
                self.assertTrue(result['notes'])
                self.assertEqual(result['usage']['cost_usd'], 0)

    def test_malformed_choices_keep_billing_and_private_data_out_of_errors(self):
        for choices in [[], [None], 'broken', [{'message': {'content': ['bad']}}]]:
            with self.subTest(choices=choices):
                result = self.ask_response({'choices': choices, 'usage': {'cost': 0.02},
                                            'private_payload': 'PRIVATE_DOCUMENT'})
                self.assertFalse(result['ok'])
                self.assertEqual(result['usage']['cost_usd'], 0.02)
                self.assertNotIn('PRIVATE_DOCUMENT', json.dumps(result))

    def test_malformed_error_fields_are_actionable(self):
        for error in [{'message': 4}, {'message': 'failed', 'metadata': {'error_type': []}},
                      {'code': 'unavailable', 'message': 'failed'}, 'bad']:
            with self.subTest(error=error):
                with self.assertRaises(core.OpenRouterError) as raised:
                    self.ask_response({'error': error, 'private_payload': 'PRIVATE_DOCUMENT'})
                self.assertNotIn('PRIVATE_DOCUMENT', str(raised.exception))

    def test_http_error_body_is_bounded_and_closed(self):
        class Body(io.BytesIO):
            size_requested = None

            def read(self, size=-1):
                self.size_requested = size
                return super().read(size)

        body = Body(b'x' * 10000)
        error = urllib.error.HTTPError('https://example.invalid', 400, 'bad', {}, body)
        with patch.object(core, 'get_api_key', return_value='fake'), \
                patch.object(core.urllib.request, 'urlopen', side_effect=error), \
                self.assertRaises(core.OpenRouterError):
            core._request('POST', '/chat/completions', {})
        self.assertTrue(body.closed)
        self.assertGreater(body.size_requested, 0)
        self.assertLessEqual(body.size_requested, 2000)

    def test_broken_stream_is_not_retried_or_exposed_as_traceback(self):
        with patch.object(core, 'get_api_key', return_value='fake'), \
                patch.object(core.urllib.request, 'urlopen',
                             side_effect=ConnectionResetError('reset')) as request, \
                self.assertRaises(core.OpenRouterError):
            core._request('POST', '/chat/completions', {})
        self.assertEqual(request.call_count, 1)

    def test_verify_does_not_claim_offline_catalogue_is_current(self):
        with patch.object(core, 'get_catalog', side_effect=core.OpenRouterError('offline')), \
                self.assertRaises(core.OpenRouterError):
            core.verify_categories()

    def test_success_body_is_also_bounded(self):
        response = io.BytesIO(b'x' * 100)
        with patch.object(core, 'get_api_key', return_value='fake'), \
                patch.object(core, 'MAX_RESPONSE_BYTES', 64), \
                patch.object(core.urllib.request, 'urlopen', return_value=response), \
                self.assertRaises(core.OpenRouterError):
            core._request('POST', '/chat/completions', {})
        self.assertTrue(response.closed)

    def test_embedded_error_still_logs_reported_cost(self):
        with patch.object(core, 'log_call') as log, self.assertRaises(core.OpenRouterError):
            self.ask_response({'error': {'message': 'failed'}, 'usage': {'cost': 0.12}})
        self.assertEqual(log.call_args.args[0]['cost_usd'], 0.12)

    def test_filtered_partial_answer_is_not_success(self):
        result = self.ask_response({'choices': [{'message': {'content': 'partial'},
                                               'finish_reason': 'content_filter'}]})
        self.assertFalse(result['ok'])
        self.assertEqual(result['answer'], 'partial')


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SCRATCH.cleanup()
