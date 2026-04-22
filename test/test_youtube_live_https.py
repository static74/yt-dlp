#!/usr/bin/env python3

# Allow direct execution
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.helper import FakeYDL
from yt_dlp.extractor import YoutubeIE

import itertools
from unittest import mock

from yt_dlp.networking.exceptions import HTTPError


class TestLiveHttpsMatcher(unittest.TestCase):
    def setUp(self):
        self.ie = YoutubeIE(FakeYDL())

    def _make_fmt(self, url, protocol='https'):
        return {'format_id': '271', 'protocol': protocol, 'url': url, '_client': 'web'}

    def test_matches_hang_shaped_urls(self):
        match_cases = [
            'https://example.com/videoplayback?source=yt_live_broadcast&hang=1&noclen=1',
            'https://example.com/videoplayback?source=yt_live_broadcast',
            'https://example.com/videoplayback?hang=1',
        ]
        for url in match_cases:
            with self.subTest(url=url):
                fmt = self._make_fmt(url)
                self.ie._prepare_live_https_formats([fmt], 'vid', url, 'https://wp', {})
                self.assertEqual(fmt['protocol'], 'http_dash_segments_generator',
                                 f'expected rewrite on {url}')
                self.assertTrue(fmt['is_live'], 'expected is_live=True after match')

    def test_ignores_non_matching_urls(self):
        nomatch_cases = [
            ('https://example.com/videoplayback?other=1', 'https'),
            ('https://example.com/videoplayback', 'https'),
        ]
        for url, proto in nomatch_cases:
            with self.subTest(url=url):
                fmt = self._make_fmt(url, protocol=proto)
                self.ie._prepare_live_https_formats([fmt], 'vid', url, 'https://wp', {})
                self.assertEqual(fmt['protocol'], proto, 'expected no rewrite')
                self.assertNotIn('is_live', fmt)

    def test_ignores_non_https_protocol(self):
        url = 'rtmp://example.com/live?hang=1'
        fmt = self._make_fmt(url, protocol='rtmp')
        self.ie._prepare_live_https_formats([fmt], 'vid', url, 'https://wp', {})
        self.assertEqual(fmt['protocol'], 'rtmp', 'rtmp formats must not be rewritten')

    def test_handles_missing_url(self):
        fmt = {'format_id': '271', 'protocol': 'https', '_client': 'web'}
        self.ie._prepare_live_https_formats([fmt], 'vid', 'https://wp', 'https://wp', {})
        self.assertEqual(fmt['protocol'], 'https', 'formats without url must not be rewritten')


def _make_http_error(status):
    """Construct an HTTPError without needing a real Response."""
    response = mock.Mock(status=status, reason=f'Status {status}')
    return HTTPError(response)


class TestLiveHttpsGenerator(unittest.TestCase):
    """Drives _live_https_fragments with a fake ctx and a mock refetch_url.

    See design doc section 9 Test 2. time.sleep is patched to no-op so tests
    run fast regardless of FETCH_SPAN pacing.
    """

    def setUp(self):
        self.ie = YoutubeIE(FakeYDL())
        self.initial_url = 'https://example.com/frag?hang=1'
        self.sleep_patch = mock.patch('yt_dlp.extractor.youtube._video.time.sleep')
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def _run_gen(self, refetch_responses, driver):
        """Helper. refetch_responses is a list (or iterable) of (status, url)
        tuples a fake refetch_url will return in order. driver is unused but
        kept for future extensions."""
        refetch_mock = mock.MagicMock(side_effect=iter(refetch_responses))
        ctx = {}
        gen = self.ie._live_https_fragments('vid', '271', self.initial_url, refetch_mock, ctx)
        return gen, ctx, refetch_mock

    def test_happy_path_no_errors(self):
        """5 yields, all against initial_url, refetch never called."""
        gen, ctx, refetch = self._run_gen([], None)
        yields = list(itertools.islice(gen, 5))
        self.assertEqual(len(yields), 5)
        for y in yields:
            self.assertEqual(y['url'], self.initial_url)
            self.assertIsNone(y['fragment_count'])
        self.assertEqual(refetch.call_count, 0)

    def test_403_triggers_refresh_and_new_url_adopted(self):
        """First yield uses initial_url; after injecting 403, next yield uses new URL."""
        new_url = 'https://example.com/frag-refreshed?hang=1'
        gen, ctx, refetch = self._run_gen([('ok', new_url)], None)

        first = next(gen)
        self.assertEqual(first['url'], self.initial_url)

        ctx['last_error'] = _make_http_error(403)
        second = next(gen)
        self.assertEqual(second['url'], new_url)
        self.assertEqual(refetch.call_count, 1)

    def test_transient_refresh_keeps_current_url(self):
        """'retry' from refetch means generator keeps initial_url; does NOT terminate."""
        gen, ctx, refetch = self._run_gen([('retry', None)], None)

        first = next(gen)
        self.assertEqual(first['url'], self.initial_url)

        ctx['last_error'] = _make_http_error(403)
        second = next(gen)
        self.assertEqual(second['url'], self.initial_url,
                         'on retry, generator must keep current_url')
        self.assertEqual(refetch.call_count, 1)

    def test_budget_exhaustion_terminates(self):
        """Continuous 403s with refetch always returning retry. Generator must terminate
        after budget exhausts. Budget is 30 with += 2 per error, so termination occurs
        after 16 failed yields (score hits 32 on iter 17, which returns without yielding).
        """
        gen, ctx, refetch = self._run_gen(itertools.repeat(('retry', None)), None)

        yielded = 0
        try:
            while True:
                next(gen)
                yielded += 1
                ctx['last_error'] = _make_http_error(403)
        except StopIteration:
            pass

        self.assertEqual(yielded, 16,
                         f'expected exactly 16 yields before budget-exit, got {yielded}')

    def test_stream_end_returns_cleanly(self):
        """refetch_url returning ('ended', None) terminates generator cleanly and
        emits a 'Live stream ended' message via to_screen. See design doc
        section 9 Test 3. Must be driven stepwise: the generator only reaches
        refetch_url after ctx['last_error'] is seeded with an HTTPError."""
        gen, ctx, refetch = self._run_gen([('ended', None)], None)

        with mock.patch.object(self.ie, 'to_screen') as to_screen:
            first = next(gen)
            self.assertEqual(first['url'], self.initial_url)

            ctx['last_error'] = _make_http_error(403)
            with self.assertRaises(StopIteration):
                next(gen)

        self.assertEqual(refetch.call_count, 1)
        self.assertEqual(to_screen.call_count, 1)
        msg = to_screen.call_args[0][0]
        self.assertIn('Live stream ended', msg)


if __name__ == '__main__':
    unittest.main()
