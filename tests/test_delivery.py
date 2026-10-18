import contextlib
import http.server
import json
import threading
import time
import socket
from email.utils import formatdate
import unittest


@contextlib.contextmanager
def collector(responses=None):
    records = []
    responses = list(responses or [(200, b'{}', {})])
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            records.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
            action = responses[min(len(records) - 1, len(responses) - 1)]
            if callable(action):
                action(self)
                return
            status, body, headers = action
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1/logs', records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class DeliveryTests(unittest.TestCase):
    def test_payload_size_and_severity_limits(self):
        from bicho import Client, Config, Event
        client = Client(Config('http://127.0.0.1:1/v1/logs', 'key'))
        large = Event.message('x' * 16384).with_stacktrace('x' * 16384)
        for i in range(80):
            large = large.with_tag(f'tag{i}', 'x' * 512)
        with self.assertRaisesRegex(ValueError, '60 KiB'):
            client.send(large)
        with collector() as (endpoint, records):
            client = Client(Config(endpoint, 'key'))
            for level, severity in [('debug', 5), ('info', 9), ('warning', 13), ('error', 17), ('fatal', 21)]:
                client.send(Event.exception('Error', 'failed').with_level(level))
                log = json.loads(records[-1][2])['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
                self.assertEqual(log['severityNumber'], severity)

    def test_body_content_type_and_network_errors_are_sanitized(self):
        from bicho import Client, Config, DeliveryError
        def wrong_type(handler):
            handler.connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}')
        with collector([wrong_type]) as (endpoint, records):
            with self.assertRaises(DeliveryError) as caught:
                Client(Config(endpoint, 'key')).capture_message('failed')
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(records), 1)
        with collector() as (endpoint, records):
            pass
        with self.assertRaises(DeliveryError) as caught:
            Client(Config(endpoint, 'credential-secret', timeout=1)).capture_message('failed')
        self.assertTrue(caught.exception.retryable)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn('credential-secret', str(caught.exception))

    def test_dns_cannot_route_loopback_http_to_remote_address(self):
        from bicho import Client, Config, DeliveryError
        from unittest.mock import patch
        with collector() as (endpoint, records):
            port = int(endpoint.split(':')[2].split('/')[0])
            with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', port))]), patch('socket.socket.connect') as connect:
                with self.assertRaises(DeliveryError):
                    Client(Config(f'http://localhost:{port}/v1/logs', 'credential-secret', timeout=0.1)).capture_message('failed')
                connect.assert_not_called()

    def test_chunk_header_drip_cannot_extend_deadline(self):
        from bicho import Client, Config, DeliveryError
        def drip(handler):
            handler.connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n')
            for unused in range(40):
                try:
                    handler.connection.sendall(b'0')
                except OSError:
                    break
                time.sleep(0.025)
        with collector([drip]) as (endpoint, records):
            start = time.monotonic()
            with self.assertRaises(DeliveryError):
                Client(Config(endpoint, 'key', timeout=0.15)).capture_message('failed')
            self.assertLess(time.monotonic() - start, 0.5)

    def test_lost_and_truncated_acknowledgments_reuse_bytes(self):
        from bicho import Client, Config, Event
        def lost(handler):
            handler.connection.shutdown(socket.SHUT_RDWR)
        def truncated(handler):
            handler.connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{')
            handler.connection.shutdown(socket.SHUT_RDWR)
        with collector([lost, truncated, (200, b'{}', {})]) as (endpoint, records):
            client = Client(Config(endpoint, 'key'))
            event = Event.message('failed')
            self.assertEqual(client.send(event), event.uid)
            self.assertEqual(client.send(event), event.uid)
            self.assertEqual(len(records), 4)
            self.assertEqual(len({record[2] for record in records}), 1)

    def test_body_drip_is_bounded_by_total_deadline(self):
        from bicho import Client, Config, DeliveryError
        def drip(handler):
            handler.connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 40\r\n\r\n')
            for unused in range(40):
                try:
                    handler.connection.sendall(b' ')
                except OSError:
                    break
                time.sleep(0.025)
        with collector([drip]) as (endpoint, records):
            start = time.monotonic()
            with self.assertRaises(DeliveryError) as caught:
                Client(Config(endpoint, 'key', timeout=0.15)).capture_message('failed')
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(caught.exception.retryable)
            self.assertIsNone(caught.exception.__context__)

    def test_retry_after_seconds_and_dates_are_not_early(self):
        from bicho import Client, Config, DeliveryError
        for value in ['1', formatdate(time.time() + 2, usegmt=True)]:
            with collector([(429, b'', {'Retry-After': value}), (200, b'{}', {})]) as (endpoint, records):
                start = time.time()
                Client(Config(endpoint, 'key', timeout=4)).capture_message('failed')
                elapsed = time.time() - start
                if value == '1':
                    self.assertGreaterEqual(elapsed, 1)
                else:
                    from email.utils import parsedate_to_datetime
                    self.assertGreaterEqual(time.time(), parsedate_to_datetime(value).timestamp())
                self.assertEqual(len(records), 2)
        with collector([(503, b'', {'Retry-After': '60'})]) as (endpoint, records):
            with self.assertRaises(DeliveryError) as caught:
                Client(Config(endpoint, 'key', timeout=0.1)).capture_message('failed')
            self.assertEqual(caught.exception.retry_after, 60)
            self.assertEqual(len(records), 1)

    def test_large_retry_after_is_not_ignored(self):
        from bicho import Client, Config, DeliveryError
        with collector([(429, b'', {'Retry-After': '99999999999999999999'})]) as (endpoint, records):
            with self.assertRaises(DeliveryError) as caught:
                Client(Config(endpoint, 'key', timeout=1)).capture_message('failed')
            self.assertGreater(caught.exception.retry_after, 60)
            self.assertEqual(len(records), 1)

    def test_native_otlp_delivery_and_reserved_attributes(self):
        from bicho import Client, Config, Event
        with collector() as (endpoint, records):
            client = Client(Config(endpoint, 'public', secret='backend', environment='test', release='v1'))
            event = Event.exception('ValueError', 'failed é').with_tag('log.record.uid', 'wrong').with_tag('large', 2 ** 70).with_tag('ok', True).with_context('job', {'id': 3})
            self.assertEqual(client.send(event), event.uid)
            self.assertEqual(client.send(event), event.uid)
            client.capture_message('another', tags={'operation': 'refresh'}, contexts={'job': 1})
            client.capture_exception(ValueError('bad'))
            self.assertEqual(records[0][2], records[1][2])
            path, headers, body = records[0]
            self.assertEqual(path, '/v1/logs')
            self.assertEqual(headers['X-Bicho-Key'], 'public')
            self.assertEqual(headers['X-Bicho-Secret'], 'backend')
            log = json.loads(body)['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
            attrs = {a['key']: a['value'] for a in log['attributes']}
            self.assertEqual(attrs['log.record.uid'], {'stringValue': event.uid})
            self.assertEqual(attrs['large'], {'stringValue': str(2 ** 70)})
            self.assertEqual(attrs['ok'], {'boolValue': True})
            self.assertEqual(attrs['sdk.name'], {'stringValue': 'bicho.python'})
            self.assertEqual(log['severityNumber'], 17)
            self.assertNotIn('backend', repr(client))
            with self.assertRaises(ValueError):
                client.send(Event.message('low').with_level('info'))
            self.assertEqual(len(records), 4)

    def test_strict_acknowledgments_are_terminal_and_do_not_leak(self):
        from bicho import Client, Config, DeliveryError
        valid = [b'{}', b'{"partialSuccess":{}}', b'{"partialSuccess":{"rejectedLogRecords":"0","errorMessage":""}}', b'{"partialSuccess":{"rejectedLogRecords":0}}']
        invalid = [b'null', b'[]', b'', b'{', b'{"upstream-secret":"hidden"}', b'{"partialSuccess":null}', b'{"partialSuccess":{"rejectedLogRecords":true}}', b'{"partialSuccess":{"rejectedLogRecords":0.0}}', b'{"partialSuccess":{"rejectedLogRecords":null}}', b'{"partialSuccess":{"rejectedLogRecords":"00"}}', b'{"partialSuccess":{"errorMessage":null}}', b'{"partialSuccess":{"rejectedLogRecords":"1"}}', b'{"partialSuccess":{"errorMessage":"upstream-secret"}}', b'{"partialSuccess":{"unknown":0}}', b'{"partialSuccess":{"rejectedLogRecords":NaN}}', b'{"partialSuccess":{},"partialSuccess":{}}', b'[' * 1100 + b']' * 1100, b' ' * 65537]
        for body in valid + invalid:
            with self.subTest(body=body[:80]), collector([(200, body, {})]) as (endpoint, records):
                client = Client(Config(endpoint, 'key-secret'))
                if body in valid:
                    client.capture_message('test')
                else:
                    with self.assertRaises(DeliveryError) as caught:
                        client.capture_message('test')
                    self.assertFalse(caught.exception.retryable)
                    self.assertEqual(caught.exception.status, 200)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertNotIn('upstream-secret', repr(caught.exception))
                self.assertEqual(len(records), 1)

    def test_retry_status_and_redirect_never_forwards_credentials(self):
        from bicho import Client, Config, DeliveryError, Event
        with collector() as (wrong, wrong_records):
            with collector([(307, b'upstream-secret', {'Location': wrong})]) as (endpoint, records):
                with self.assertRaises(DeliveryError) as caught:
                    Client(Config(endpoint, 'key-secret')).capture_message('failed')
                self.assertEqual(caught.exception.status, 307)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(wrong_records), 0)
                self.assertEqual(len(records), 1)
        with collector([(503, b'private', {'Retry-After': '0'}), (429, b'private', {'Retry-After': '0'}), (200, b'{}', {})]) as (endpoint, records):
            event = Event.message('failed')
            self.assertEqual(Client(Config(endpoint, 'key')).send(event), event.uid)
            self.assertEqual(len(records), 3)
            self.assertEqual(len({record[2] for record in records}), 1)
        for status in [201, 400, 401, 403, 500]:
            with collector([(status, b'private', {})]) as (endpoint, records):
                with self.assertRaises(DeliveryError):
                    Client(Config(endpoint, 'key')).capture_message('failed')
                self.assertEqual(len(records), 1)
