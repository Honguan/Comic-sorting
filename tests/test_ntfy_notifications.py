import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError, URLError

from ntfy_notifications import protect_token, publish, unprotect_token, validate_settings


class NtfyTests(unittest.TestCase):
    def test_local_http_server_receives_utf8_json_and_redirect_is_not_followed(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            redirect = False

            def do_POST(self):
                received.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                if self.headers.get('User-Agent') != 'Comic-sorting':
                    self.send_response(403)
                    self.send_header('Server', 'cloudflare')
                    self.end_headers()
                    self.wfile.write(b'error code: 1010')
                elif self.redirect:
                    self.send_response(307)
                    self.send_header('Location', '/redirected')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"id":"test","event":"message"}')

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = validate_settings(dict(server=f'http://127.0.0.1:{server.server_port}', topic='test-topic', token=''))
            self.assertEqual(publish(config, '佇列結束', '漫畫完成\n' + '中文' * 3000, 4, threading.Event()), 'NTFY_OK')
            path, headers, body = received[0]
            self.assertEqual(path, '/')
            self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
            self.assertEqual(headers['User-Agent'], 'Comic-sorting')
            self.assertEqual((body['topic'], body['title'], body['priority']), ('test-topic', '佇列結束', 4))
            self.assertTrue(body['message'].startswith('漫畫完成\n'))
            self.assertLessEqual(len(body['message'].encode('utf-8')), 4096)
            Handler.redirect = True
            self.assertEqual(publish(config, 'redirect', 'test', 3, threading.Event()), 'NTFY_HTTP_307')
            self.assertEqual(len(received), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_authentication_http_errors_retry_and_ambiguous_network_delivery(self):
        config = validate_settings(dict(server='https://ntfy.example/', topic='my-topic', token='tk_secret'))
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        for status, retry_after, attempts in ((401, '', 1), (403, '', 1), (429, '1', 2), (429, '60', 1), (503, '', 2)):
            with self.subTest(status=status, retry_after=retry_after), mock.patch('ntfy_notifications.build_opener') as factory:
                client = factory.return_value
                client.open.side_effect = HTTPError(config['server'], status, 'private response', {'Retry-After': retry_after}, io.BytesIO(b'private'))
                self.assertEqual(publish(config, 'title', 'body', 3, stop), f'NTFY_HTTP_{status}')
                self.assertEqual(client.open.call_count, attempts)
                request = client.open.call_args.args[0]
                self.assertEqual(request.get_header('Authorization'), 'Bearer tk_secret')
                self.assertEqual(client.open.call_args.kwargs['timeout'], 10)
        with mock.patch('ntfy_notifications.build_opener') as factory:
            factory.return_value.open.side_effect = URLError('tk_secret')
            self.assertEqual(publish(config, 'title', 'body', 3, stop), 'NTFY_NETWORK')
            self.assertEqual(factory.return_value.open.call_count, 1)
        with mock.patch('ntfy_notifications.build_opener') as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"event":"keepalive"}'
            self.assertEqual(publish(config, 'title', 'body', 3, stop), 'NTFY_RESPONSE')

    def test_cloudflare_block_is_distinguished_from_topic_permissions(self):
        config = validate_settings(dict(server='https://ntfy.example', topic='test-topic', token='tk_secret'))
        for server, body, expected in (
                ('cloudflare', b'error code: 1010', 'NTFY_CLOUDFLARE_1010'),
                ('cloudflare', b'{"code":40301,"http":403,"error":"forbidden"}', 'NTFY_HTTP_403'),
                ('nginx', b'403 Forbidden', 'NTFY_HTTP_403')):
            with self.subTest(server=server, body=body), mock.patch('ntfy_notifications.build_opener') as factory:
                response = io.BytesIO(body)
                factory.return_value.open.side_effect = HTTPError(
                    config['server'], 403, 'Forbidden', {'Server': server}, response)
                self.assertEqual(publish(config, 'title', 'body', 3, threading.Event()), expected)
                self.assertEqual(factory.return_value.open.call_count, 1)
                self.assertTrue(response.closed)

    def test_settings_and_windows_token_storage(self):
        valid = dict(server='https://ntfy.sh', topic='comic-test', token='')
        for update in ({'topic': ''}, {'topic': 'a/b'}, {'topic': 'a' * 65}, {'topic': '中文'},
                       {'server': None}, {'topic': 123},
                       {'server': 'file:///tmp'}, {'server': 'https://user:pwd@host'},
                       {'server': 'https://host/topic'}, {'server': 'https://host/?q=1'},
                       {'server': 'http://host', 'token': 'tk_secret'}, {'token': 'tk_secret\nheader:x'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_settings(dict(valid, **update))
        token = 'tk_' + 'a' * 40
        ciphertext = protect_token(token)
        self.assertNotIn(token, ciphertext)
        self.assertEqual(unprotect_token(ciphertext), token)
        self.assertEqual(protect_token(''), '')
        self.assertEqual(unprotect_token(''), '')
        with self.assertRaises(Exception):
            unprotect_token('corrupt ciphertext')


if __name__ == '__main__':
    unittest.main()
