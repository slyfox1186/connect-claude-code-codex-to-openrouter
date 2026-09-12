"""Offline adversarial provider/configuration regressions. No credentials or network."""

from __future__ import annotations

import base64
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

    def test_replayed_attachments_obey_current_limits(self):
        part = {'type': 'file', 'file': {'filename': 'a.pdf',
                'file_data': 'data:application/pdf;base64,' + base64.b64encode(b'12345').decode()}}
        history = [{'role': 'user', 'content': [part, part]}]
        for limits in [{'max_attachments': 1}, {'max_attachment_total_bytes': 9},
                       {'max_attachment_bytes': 4}]:
            with self.subTest(limits=limits), \
                    patch.object(core, '_config_cache', {**core.load_config(), **limits}), \
                    self.assertRaises(core.OpenRouterError):
                core.build_messages('follow up', history=history, model_slug='test/model')

    def test_known_key_hardlink_cannot_be_attached(self):
        core.ENV_FILE.write_text('OPENROUTER_API_KEY=PRIVATE_TEST_KEY')
        alias = Path(SCRATCH.name) / 'innocent.txt'
        alias.unlink(missing_ok=True)
        os.link(core.ENV_FILE, alias)
        try:
            messages, notes = core.build_messages('review', files=[str(alias)])
            self.assertNotIn('PRIVATE_TEST_KEY', json.dumps(messages))
            self.assertTrue(any('key' in n.lower() for n in notes))
        finally:
            core.ENV_FILE.unlink()
            alias.unlink()

    def test_thread_attachment_budget_is_total_across_turns(self):
        part = {'type': 'file', 'file': {'filename': 'a.pdf',
                'file_data': 'data:application/pdf;base64,JVBERi0='}}
        core._config_cache = {**core.load_config(),
                              'thread_attachment_bytes': len(json.dumps(part))}
        with patch.object(core, 'THREAD_DIR', Path(SCRATCH.name) / 'budget-thread'):
            self.assertTrue(core.save_thread('budget', 'first', 'a', 'test/model',
                                             attachments=[part]))
            self.assertFalse(core.save_thread('budget', 'second', 'b', 'test/model',
                                              attachments=[part]))
            self.assertEqual(len(core.load_thread('budget')), 2)

    def test_malformed_thread_parts_are_not_replayed(self):
        bad = [{'role': ['user'], 'content': 'x'}, {'role': 'user', 'content': [
            {'type': 'file', 'file': 42}, {'type': 'input_audio', 'input_audio': 'bad'}]}]
        self.assertEqual(core.usable_turns(bad), [])

    def test_iterable_files_are_materialized(self):
        self.assertEqual(core.as_list(iter(['a.py', 'b.py'])), ['a.py', 'b.py'])

    def test_guide_fences_do_not_expose_code_as_headings(self):
        for body in ['````markdown\n```\n## hidden\n````\n## visible',
                     '```python\n```not-a-close\n## hidden\n```\n## visible']:
            with self.subTest(body=body):
                self.assertEqual([m.group(2) for _, m in core._guide_headings(body)], ['visible'])

    def test_guide_read_refuses_swapped_symlink(self):
        path = Path(SCRATCH.name) / 'guide.md'
        path.unlink(missing_ok=True)
        path.symlink_to(ROOT / 'README.md')
        with self.assertRaises(core.OpenRouterError):
            core._guide_read(path)

    def test_invalid_and_future_guide_dates_need_verification(self):
        path = Path(SCRATCH.name) / 'dated.md'
        for value in ['2026-99-99', '9999-01-01']:
            path.write_text(f'---\nverified: {value}\n---\n# Fixture')
            with patch.object(core, '_guide_map', return_value={'fixture': path}):
                self.assertTrue(core.list_guides()[0]['stale'])

    def test_annotation_content_counts_for_replay_and_storage(self):
        image = {'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(b'X' * 100).decode()}}
        annotation = {'type': 'file', 'file': {'hash': 'fixture', 'content': [
            {'type': 'text', 'text': 'parsed text' * 20}, image]}}
        history = [{'role': 'assistant', 'content': 'answer', 'annotations': [annotation]}]
        self.assertEqual(core.attachment_summary(history)['image'], 1)
        self.assertGreater(core.text_chars(history), 100)
        core._config_cache = {**core.load_config(), 'max_attachment_bytes': 10,
                              'thread_attachment_bytes': 100}
        with self.assertRaises(core.OpenRouterError):
            core.build_messages('follow up', history=history)
        with patch.object(core, 'THREAD_DIR', Path(SCRATCH.name) / 'annotation-thread'):
            self.assertFalse(core.save_thread('budget', 'q', 'a', 'test/model',
                                              annotations=[annotation]))

    def test_long_displayed_name_resumes_original_thread(self):
        name = '2026-09-12_Connection_Bridge_Complete_Repository_Review_and_Reconciliation'
        with patch.object(core, 'THREAD_DIR', Path(SCRATCH.name) / 'long-name'):
            self.assertTrue(core.save_thread(name, 'q', 'a', 'test/model'))
            listed = core.list_threads()[0]['name']
            self.assertEqual(core.load_thread(listed), core.load_thread(name))

    def test_oversized_transcript_does_not_replace_readable_history(self):
        with patch.object(core, 'THREAD_DIR', Path(SCRATCH.name) / 'size-thread'), \
                patch.object(core, 'MAX_FILE_BYTES', 2000):
            self.assertTrue(core.save_thread('size', 'q', 'a', 'test/model'))
            before = core._thread_path('size').read_bytes()
            self.assertFalse(core.save_thread('size', '🙂' * 1000, 'a', 'test/model'))
            self.assertEqual(core._thread_path('size').read_bytes(), before)

    def test_malformed_base64_cannot_understate_size(self):
        blob = base64.b64encode(b'X' * 100).decode() + '=' * 1000
        part = {'type': 'file', 'file': {'file_data': 'data:application/pdf;base64,' + blob}}
        core._config_cache = {**core.load_config(), 'max_attachment_bytes': 10}
        with self.assertRaises(core.OpenRouterError):
            core.build_messages('review', history=[{'role': 'user', 'content': [part]}])

    def test_partial_log_tail_does_not_consume_new_record(self):
        with patch.object(core, 'CALL_LOG', Path(SCRATCH.name) / 'partial.jsonl'):
            core.CALL_LOG.write_text('{"ts":')
            core.log_call({'ok': True, 'cost_usd': 0.25})
            self.assertEqual(len(core.read_log()), 1)

    def test_mcp_requires_provider_parameter_support(self):
        response = {'choices': [{'message': {'content': 'final'}, 'finish_reason': 'stop'}]}
        with patch.object(core, '_request', return_value=response) as request:
            core.ask('review', model='test/model', _mcp_call=True)
        self.assertEqual(request.call_args.args[2]['provider'], {'require_parameters': True})

    def test_invalid_request_numbers_are_refused_before_transport(self):
        cases = [('temperature', value) for value in [float('nan'), float('inf'), -1, 3, True]]
        cases += [(key, value) for key in ['max_tokens', 'max_context_tokens']
                  for value in [float('nan'), float('inf'), True, 3.5, 'oops']]
        for key, value in cases:
            with self.subTest(key=key, value=value), patch.object(core, '_request') as request:
                with self.assertRaises(core.OpenRouterError):
                    core.ask('review', model='test/model', **{key: value})
                request.assert_not_called()

    def test_model_info_exposes_effective_context_window(self):
        model = {**CATALOG[0], 'top_provider': {'context_length': 50000}}
        with patch.object(core, 'get_catalog', return_value=[model]):
            self.assertEqual(core.model_info('test/model')['context_length'], 50000)

    def test_unrepresentable_timeout_has_actionable_error(self):
        with patch.object(core, 'get_api_key', return_value='fake'), \
                patch.object(core.urllib.request, 'urlopen', side_effect=OverflowError), \
                self.assertRaises(core.OpenRouterError):
            core._request('POST', '/chat/completions', {}, timeout=1e100)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SCRATCH.cleanup()
