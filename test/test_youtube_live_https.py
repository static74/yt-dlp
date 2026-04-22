#!/usr/bin/env python3

# Allow direct execution
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.helper import FakeYDL
from yt_dlp.extractor import YoutubeIE


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


if __name__ == '__main__':
    unittest.main()
