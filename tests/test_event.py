import json
import unittest


class EventTests(unittest.TestCase):
    def test_exception_kind_must_identify_an_exception(self):
        from bicho import Event
        with self.assertRaises(ValueError):
            Event.exception('', '').with_level('debug')

    def test_snapshots_privacy_bounds_and_identity(self):
        from bicho import Event
        class Unsafe:
            def __str__(self):
                raise AssertionError('str called')
            def __repr__(self):
                raise AssertionError('repr called')
            def __iter__(self):
                raise AssertionError('iter called')
        data = {'safe': [1, None], 'password': 'hidden', 'headers': [['Authorization', 'hidden']], 'url': 'https://name:hidden@example.com/api?token=hidden#hidden', 'text': 'Bearer hidden', 'object': Unsafe(), 'unicode': 'é' * 3000, 'deep': [[[[[[['hidden']]]]]]], 'many': list(range(100)), 'nonfinite': float('nan')}
        original = Event.message('failed')
        event = original.with_context('request', data).with_tag('password', 'hidden').with_level('fatal').with_stacktrace('safe stack')
        data['safe'].append('changed')
        self.assertEqual(original.uid, event.uid)
        self.assertEqual(original._timestamp, event._timestamp)
        self.assertEqual(original._contexts, '{}')
        text = event._contexts
        self.assertNotIn('hidden', text)
        self.assertNotIn('changed', text)
        normalized = json.loads(text)['request']
        self.assertEqual(normalized['safe'], [1, None])
        self.assertEqual(normalized['url'], 'https://example.com/api')
        self.assertLessEqual(len(normalized['unicode'].encode()), 2048)
        self.assertEqual(len(normalized['many']), 50)
        self.assertIn('[Truncated]', text)
        self.assertNotIn('safe stack', repr(event))
        self.assertNotIn('failed', repr(event))
        for method, args in [('with_tag', ('x', Unsafe())), ('with_tag', ('x', float('inf'))), ('with_tag', ('', 1)), ('with_context', ('', 1)), ('with_level', ('trace',))]:
            with self.assertRaises(ValueError):
                getattr(event, method)(*args)

    def test_native_exception_has_locations_without_source_or_arguments(self):
        from bicho import Event
        def fail(private_argument):
            raise ValueError('Bearer hidden')
        try:
            try:
                fail('local-secret')
            except ValueError as cause:
                raise RuntimeError('outer failure') from cause
        except RuntimeError as error:
            event = Event.from_exception(error)
        self.assertEqual(event._kind, 'RuntimeError')
        self.assertIn('Traceback (most recent call last):', event._stack)
        self.assertIn('File "', event._stack)
        self.assertNotIn('local-secret', event._stack)
        self.assertNotIn('raise RuntimeError', event._stack)
        self.assertNotIn('hidden', event._contexts)
        self.assertEqual(json.loads(event._contexts)['error_chain'][0]['type'], 'ValueError')
