"""Offline, stdlib-only regression tests. No Ollama or cloud calls."""
from __future__ import annotations

import io
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import bridge


class FakeUpstream:
    def __init__(self, messages):
        self.messages = [json.dumps(item, ensure_ascii=False).encode('utf-8') + b'\n' for item in messages]
        self.closed = False

    def __iter__(self):
        return iter(self.messages)

    def close(self):
        self.closed = True


class FakeHandler:
    def __init__(self):
        self.request_id = 'brg-offline-test'
        self.wfile = io.BytesIO()
        self.headers_sent = []
        self.close_connection = False

    def send_response(self, code):
        self.headers_sent.append(('status', code))

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass

    def _rid_header(self):
        return bridge.Handler._rid_header(self)

    def _sse(self, obj):
        return bridge.Handler._sse(self, obj)

    def stream(self, events):
        fake = FakeUpstream(events)
        request = {'model': 'example-local-model', 'messages': [{'role': 'user', 'content': 'hi'}],
                   'tools': [], 'max_tokens': 64, 'stream': True}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bridge, 'LOG_DIR', Path(d)):
                with mock.patch.object(bridge.urllib.request, 'urlopen', return_value=fake):
                    bridge.Handler._stream_native(self, request, request['messages'],
                                                  {'condensed': False, 'history_budget_final': 1500,
                                                   'tools_count': 0}, 'fp-test')
                logs = (Path(d) / 'bridge.jsonl').read_text(encoding='utf-8')
        return self.wfile.getvalue().decode('utf-8'), logs, fake.closed


class BridgeOfflineTests(unittest.TestCase):
    def test_experimental_mutex_does_not_collide_with_legacy(self):
        self.assertNotEqual(bridge.MUTEX_NAME, 'HermesOllamaSafeBridge_v1')
        self.assertIn(str(bridge.PORT), bridge.MUTEX_NAME)

    def test_local_only_binding(self):
        for value in ('0.0.0.0', '192.168.0.12', 'example.com'):
            with self.subTest(host=value), self.assertRaises(SystemExit):
                bridge._assert_loopback(value)
        bridge._assert_loopback('127.0.0.1')

    def test_remote_upstream_is_rejected(self):
        for url in ('https://127.0.0.1:11434', 'http://remote.example:11434',
                    'http://127.0.0.1:11434/other',
                    'http://127.0.0.1:11434/?x=1',
                    'http://user:pass@127.0.0.1:11434'):
            with self.subTest(url=url), self.assertRaises(SystemExit):
                bridge._assert_local_upstream(url)
        bridge._assert_local_upstream('http://127.0.0.1:11434')

    def test_request_guard_rejects_oversize_before_read(self):
        class GuardHandler:
            def __init__(self):
                self.headers = {'Content-Length': str(bridge.MAX_BODY_BYTES + 1)}
                self.rfile = io.BytesIO(b'do not read')
                self.path = '/v1/chat/completions'
                self.status = None
            def _json(self, status, body):
                self.status = (status, body)
        obj = GuardHandler()
        bridge.Handler.do_POST(obj)
        self.assertEqual(obj.status[0], 413)
        self.assertEqual(obj.rfile.tell(), 0)

    def test_malformed_request_shape_rejected_without_upstream(self):
        class GuardHandler:
            def __init__(self, blob):
                self.headers = {'Content-Length': str(len(blob))}
                self.rfile = io.BytesIO(blob)
                self.path = '/v1/chat/completions'
                self.status = None
            def _json(self, status, body):
                self.status = (status, body)
        for payload in (b'[]', b'{"messages": "wrong"}', b'{"messages":[5]}',
                        b'{"messages":[], "tools": "bad"}'):
            with self.subTest(payload=payload):
                obj = GuardHandler(payload)
                bridge.Handler.do_POST(obj)
                self.assertEqual(obj.status[0], 400)

    def test_invalid_nested_json_shape_rejected_with_http_400(self):
        class GuardHandler:
            def __init__(self, blob):
                self.headers = {'Content-Length': str(len(blob))}
                self.rfile = io.BytesIO(blob)
                self.path = '/v1/chat/completions'
                self.status = None
            def _json(self, status, body):
                self.status = (status, body)

        good = {'messages': [{'role': 'user', 'content': 'hello'}]}
        corrupt = [
            {'tools': [{'function': 'bad'}]},
            {'tools': [{'function': None}]},
            {'messages': [{'role': 'assistant', 'tool_calls': 'bad'}]},
            {'messages': [{'role': 'assistant', 'tool_calls': ['bad']}]},
            {'messages': [{'role': 'assistant', 'tool_calls': [{'function': 'bad'}]}]},
            {'messages': [{'role': 'assistant', 'tool_calls': None}]},
            {'options': 'bad'},
            {'options': ['bad']},
            {'tools': [False]},
        ]
        for patch in corrupt:
            with self.subTest(patch=patch):
                payload = {**good, **patch}
                blob = json.dumps(payload).encode('utf-8')
                obj = GuardHandler(blob)
                with mock.patch.object(bridge.urllib.request, 'urlopen',
                                       side_effect=AssertionError('upstream must not be called')):
                    bridge.Handler.do_POST(obj)
                self.assertEqual(obj.status[0], 400)
                self.assertEqual(obj.status[1]['error']['code'], 'invalid_request_shape')
        self.assertTrue(bridge.valid_request_shape(good))
        self.assertTrue(bridge.valid_request_shape({**good, 'tools': [{'function': {'name': 'read_file'}}],
                                                    'options': {'num_ctx': 10000}}))

    def test_context_limit_cannot_be_overridden(self):
        payload = {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}],
                   'options': {'num_ctx': 999999, 'temperature': 0.2}}
        native = bridge.to_ollama_chat(payload, payload['messages'])
        self.assertEqual(native['options']['num_ctx'], bridge.NUM_CTX)
        self.assertEqual(native['options']['temperature'], 0.2)

        # In observe + non-native mode the normal passthrough must ALSO
        # override the client's options, not merely set a default.
        class LocalOllamaStub(BaseHTTPRequestHandler):
            seen = []
            def log_message(self, *args):
                return
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                type(self).seen.append(json.loads(raw))
                data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        LocalOllamaStub.seen.clear()
        with ThreadingHTTPServer(('127.0.0.1', 0), LocalOllamaStub) as upstream, \
             ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler) as frontend, \
             tempfile.TemporaryDirectory() as directory:
            t1 = threading.Thread(target=upstream.serve_forever, daemon=True)
            t2 = threading.Thread(target=frontend.serve_forever, daemon=True)
            t1.start(); t2.start()
            try:
                with mock.patch.multiple(bridge, UPSTREAM=f'http://127.0.0.1:{upstream.server_port}',
                                         NATIVE=False, TOOL_POLICY_MODE='observe', LOG_DIR=Path(directory)):
                    body = json.dumps({**payload, 'max_tokens': 64}).encode()
                    req = urllib.request.Request(f'http://127.0.0.1:{frontend.server_port}/v1/chat/completions',
                                                 data=body, method='POST',
                                                 headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                self.assertEqual(LocalOllamaStub.seen[0]['options']['num_ctx'], bridge.NUM_CTX)
            finally:
                upstream.shutdown(); frontend.shutdown()
                t1.join(timeout=3); t2.join(timeout=3)

    def test_non_compressible_margin_is_counted_once(self):
        limit = bridge.NUM_CTX
        self.assertFalse(bridge.non_compressible_exceeds_context({'total': limit}))
        self.assertTrue(bridge.non_compressible_exceeds_context({'total': limit + 1}))
        self.assertFalse(bridge.non_compressible_exceeds_context(
            {'total': limit - bridge.TOTAL_MARGIN_TOKENS}))

    def test_message_tokens_include_reasoning_and_legacy_function_call(self):
        base = {'role': 'assistant', 'content': 'ok'}
        long = {**base, 'reasoning_content': 'R' * 9000,
                'function_call': {'name': 'work', 'arguments': 'x' * 3000}}
        self.assertGreater(bridge.message_tokens(long), bridge.message_tokens(base) + 2000)
        self.assertGreater(bridge.conversation_tokens([long]), bridge.conversation_tokens([base]))

    def test_condense_keeps_notice_and_last_user_with_orphan_system_entries(self):
        # sanitize removes empty assistant messages; fixed len(systems)+1 indices
        # could shift and wrongly delete the notice in earlier implementations.
        messages = [{'role': 'system', 'content': 'must keep A'},
                    {'role': 'system', 'content': 'must keep B'},
                    {'role': 'assistant', 'content': ' '},
                    *[{'role': 'assistant', 'content': 'middle-' + str(i) + 'x' * 1500}
                      for i in range(6)],
                    {'role': 'user', 'content': 'last user invariant'}]
        with mock.patch.object(bridge, 'KEEP_RECENT', 6):
            out, report = bridge.condense(messages, budget_tokens=8)
        self.assertTrue(report['condensed'])
        self.assertIn('must keep A', [m.get('content') for m in out])
        self.assertIn('must keep B', [m.get('content') for m in out])
        self.assertTrue(any(bridge.MARKER in m.get('content', '') for m in out))
        self.assertTrue(any(m.get('content') == 'last user invariant' for m in out))

    def test_repeated_blocked_call_is_stopped_without_upstream(self):
        payload = {'model': 'local-test', 'max_tokens': 64,
                   'messages': [{'role': 'system', 'content': 'sys'},
                                {'role': 'user', 'content': 'run tool'},
                                {'role': 'assistant', 'content':
                                 bridge.BLOCK_MARKER_PREFIX + ' tool blocked'}],
                   'tools': []}
        fp = bridge.request_fingerprint(payload, session_hint='s1')
        with mock.patch.dict(bridge._BLOCK_SEEN, {}, clear=True):
            bridge.note_blocked(fp)
            bridge.note_blocked(fp)
            self.assertTrue(bridge.blocked_recently(fp))
            self.assertTrue(bridge.history_has_block_marker(payload['messages']))

    def test_no_tools_by_default(self):
        self.assertEqual(bridge.ALLOWED_TOOLS, frozenset())
        schema = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {}}}
        self.assertEqual(bridge.filter_tools([schema])[0], [])

    def test_restrict_allowlist_even_if_tools_provided(self):
        def tool(name):
            return {'type': 'function', 'function': {'name': name, 'parameters': {}}}
        with mock.patch.object(bridge, 'ALLOWED_TOOLS', frozenset({'read_file'})):
            kept, blocked = bridge.filter_tools([tool('terminal'), tool('read_file'), tool('browser_click')])
        self.assertEqual([s['function']['name'] for s in kept], ['read_file'])
        self.assertEqual(set(blocked), {'terminal', 'browser_click'})

    def test_bad_generation_limits(self):
        for value in (0, -1, True, False, float('nan'), '1.5', 'abc', 10**9, 3.5):
            with self.subTest(value=value):
                limit, error = bridge.validated_max_tokens({'max_tokens': value})
                self.assertIsNotNone(error)
        self.assertEqual(bridge.validated_max_tokens({'max_tokens': '128'}), (128, None))

    def test_large_tools_budget_fail_closed(self):
        schema = {'type': 'function', 'function': {'name': 'read_file',
                   'description': 'D' * 12000, 'parameters': {'type': 'object'}}}
        payload = {'messages': [{'role': 'user', 'content': 'test'}],
                   'tools': [schema] * 40, 'max_tokens': 128}
        with mock.patch.object(bridge, 'ALLOWED_TOOLS', frozenset({'read_file'})):
            plan = bridge.plan_budget(payload)
        self.assertNotEqual(plan['verdict'], 'ok')

    def test_non_compressible_accounts_for_safety_factor(self):
        messages = [{'role': 'system', 'content': 'А' * 1000},
                    {'role': 'user', 'content': 'Б' * 1000}]
        calc = bridge.non_compressible_tokens(messages, 100, 256)
        raw = bridge.conservative_text_tokens('А' * 1000)
        self.assertGreaterEqual(calc['system_tokens'], int(raw * bridge.MESSAGE_SAFETY_FACTOR))
        self.assertGreaterEqual(calc['last_user_tokens'], int(raw * bridge.MESSAGE_SAFETY_FACTOR))

    def test_canary_identifiers_from_middle_present_in_summary(self):
        messages = [{'role': 'user', 'content': f'Журнал {i} ' + 'данные ' * 300}
                    for i in range(20)]
        messages[5]['content'] = 'А' * 500 + ' CANARY-ALPHA-3F91'
        messages[10]['content'] = 'Б' * 500 + ' CANARY-BRAVO-77C2'
        messages[12]['content'] = 'В' * 500 + ' CANARY-CHARLIE-9D4E'
        summary = bridge.build_summary(messages, 4000)
        self.assertLessEqual(len(summary), 4000)
        for key in ('CANARY-ALPHA-3F91', 'CANARY-BRAVO-77C2', 'CANARY-CHARLIE-9D4E'):
            self.assertIn(key, summary)
        self.assertIn('IDs present in omitted messages', summary)

    def test_summary_is_explicitly_lossy(self):
        text = bridge.build_summary([{'role': 'user', 'content': 'X' * 8000}], 200)
        self.assertLessEqual(len(text), 200)
        self.assertNotEqual(len(text), 8000)

    def test_local_policy_response_has_synthetic_usage(self):
        resp = bridge.text_only_response('model', 'blocked')
        self.assertEqual(resp['usage']['total_tokens'], 0)
        self.assertEqual(resp['choices'][0]['message']['content'], 'blocked')

    def test_stream_normal_done_once_and_usage(self):
        handler = FakeHandler()
        sse, logs, closed = handler.stream([
            {'message': {'content': 'OK'}, 'done': False},
            {'message': {}, 'done': True, 'prompt_eval_count': 13, 'eval_count': 2},
        ])
        self.assertTrue(closed)
        self.assertEqual(sse.count('data: [DONE]'), 1)
        self.assertIn('"kind": "usage"', logs)
        self.assertIn(('X-Bridge-Request-Id', 'brg-offline-test'), handler.headers_sent)

    def test_stream_truncated_no_success_marker(self):
        sse, logs, _ = FakeHandler().stream([{'message': {'content': 'partial'}, 'done': False}])
        self.assertIn('stream_truncated', sse)
        self.assertNotIn('data: [DONE]', sse)
        self.assertNotIn('"finish_reason": "stop"', sse)
        self.assertIn('"kind": "stream_truncated"', logs)

    def test_stream_forbidden_calls_are_filtered(self):
        sse, logs, _ = FakeHandler().stream([
            {'message': {'tool_calls': [{'function': {'name': 'ter', 'arguments': ''}}]}, 'done': False},
            {'message': {'tool_calls': [{'function': {'name': 'minal', 'arguments': '{}'}}]}, 'done': False},
            {'message': {}, 'done': True, 'prompt_eval_count': 13, 'eval_count': 2},
        ])
        self.assertNotIn('"tool_calls":', sse)
        self.assertIn('"kind": "tool_policy_block"', logs)

    def test_http_roundtrip_uses_mock_ollama_and_no_tools(self):
        seen = []
        class LocalOllamaStub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                seen.append(json.loads(self.rfile.read(size).decode('utf-8')))
                answer = json.dumps({'model': 'example-local', 'done': True,
                    'message': {'role': 'assistant', 'content': 'pong'},
                    'prompt_eval_count': 22, 'eval_count': 2}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
        with ThreadingHTTPServer(('127.0.0.1', 0), LocalOllamaStub) as upstream, \
             ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler) as frontend, \
             tempfile.TemporaryDirectory() as d:
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            frontend_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
            upstream_thread.start()
            frontend_thread.start()
            try:
                with mock.patch.object(bridge, 'UPSTREAM', f'http://127.0.0.1:{upstream.server_port}'):
                    with mock.patch.object(bridge, 'LOG_DIR', Path(d)):
                        prompt = {'model':'example-local', 'max_tokens':64, 'stream':False,
                            'messages':[{'role':'user','content':'ping'}],
                            'tools':[{'type':'function', 'function': {'name':'terminal', 'parameters': {}}}]}
                        data = json.dumps(prompt).encode()
                        req = urllib.request.Request(
                            f'http://127.0.0.1:{frontend.server_port}/v1/chat/completions',
                            data=data, headers={'Content-Type':'application/json'}, method='POST')
                        with urllib.request.urlopen(req, timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.headers.get('X-Bridge-Request-Id'))
                            body = json.loads(response.read())
                        self.assertEqual(body['choices'][0]['message']['content'], 'pong')
                        self.assertFalse(seen[0].get('tools'))
                        self.assertEqual(seen[0]['options']['num_predict'], 64)
                        self.assertEqual(seen[0]['options']['num_ctx'], bridge.NUM_CTX)
            finally:
                upstream.shutdown()
                frontend.shutdown()
                upstream_thread.join(timeout=3)
                frontend_thread.join(timeout=3)

    def test_upstream_http_error_does_not_dump_private_request(self):
        from urllib.error import HTTPError
        class HttpHandler:
            def __init__(self, blob):
                self.headers = {'Content-Length': str(len(blob))}
                self.rfile = io.BytesIO(blob)
                self.path = '/v1/chat/completions'
                self.status = None
                self.request_id = None
            def _json(self, status, body):
                self.status = (status, body)
        secret_marker = 'SYNTHETIC_SECRET_SENTINEL_DO_NOT_LOG'
        blob = json.dumps({'model': 'local', 'max_tokens': 128,
                           'messages': [{'role': 'user', 'content': secret_marker}]}).encode()
        obj = HttpHandler(blob)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bridge, 'LOG_DIR', Path(d)):
                with mock.patch.object(bridge.urllib.request, 'urlopen',
                                       side_effect=HTTPError('http://127.0.0.1:11434/api/chat',
                                                             400, 'bad request', {}, io.BytesIO(b'bad'))):
                    bridge.Handler.do_POST(obj)
                saved = list(Path(d).glob('**/*'))
                logs = ''.join(p.read_text(encoding='utf-8') for p in saved if p.is_file())
        self.assertEqual(obj.status[0], 502)
        self.assertNotIn(secret_marker, logs)
        self.assertIn('upstream_http_error', logs)

    def test_log_records_remain_valid_under_threads(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bridge, 'LOG_DIR', Path(d)):
                with ThreadPoolExecutor(max_workers=10) as pool:
                    list(pool.map(lambda i: bridge.log_event('test', {'request_id': f'r-{i}'}), range(70)))
                rows = (Path(d) / 'bridge.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(rows), 70)
            self.assertEqual({json.loads(x)['request_id'] for x in rows}, {f'r-{i}' for i in range(70)})


if __name__ == '__main__':
    unittest.main(verbosity=2)
