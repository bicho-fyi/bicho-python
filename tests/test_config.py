import unittest


class ConfigTests(unittest.TestCase):
    def test_rejects_malformed_dns_labels(self):
        from bicho import Config
        for host in ['example..com', 'example.-com', 'example-.com', 'example.com.', 'example.' + 'a' * 64]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                Config(f'https://{host}/v1/logs', 'key')

    def test_required_metadata_is_string_and_timeout_errors_are_safe(self):
        from bicho import Config
        for kwargs in [{'service_name': None}, {'service_name': 42}, {'timeout': 10 ** 1000}, {'key': None}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config('https://collector.example/v1/logs', **({'key': 'key'} | kwargs))

    def test_validates_destination_credentials_metadata(self):
        from bicho import Config
        valid = ['https://collector.example/v1/logs', 'http://localhost/v1/logs', 'http://127.0.0.1:1234/v1/logs', 'http://[::1]/v1/logs']
        for endpoint in valid:
            self.assertNotIn('private-key', repr(Config(endpoint, 'private-key')))
        invalid = ['http://example.com/v1/logs', 'https://u:p@example.com/v1/logs', 'https://example.com/v1/logs?', 'https://example.com/v1/logs#', 'https://example.com/a/../v1/logs', 'https:///example.com/v1/logs', 'https://example.com\\v1/logs', 'https://example.com:/v1/logs', 'https://example.com:0/v1/logs', 'https://example.com:65536/v1/logs', 'http://127.1/v1/logs', 'http://localhost.evil/v1/logs', 'https://example.com\n/v1/logs', 'https://[::1]evil/v1/logs']
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Config(endpoint, 'private-key')
        for kwargs in [{'key': ''}, {'key': 'bad\n'}, {'key': 'é'}, {'key': 'x' * 513}, {'secret': ''}, {'timeout': True}, {'timeout': float('nan')}, {'timeout': float('inf')}, {'timeout': 0}, {'timeout': 61}, {'service_name': ''}, {'release': 'é' * 257}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(valid[0], **({'key': 'private-key'} | kwargs))
