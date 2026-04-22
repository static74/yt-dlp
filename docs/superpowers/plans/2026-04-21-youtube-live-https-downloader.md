# YouTube live adaptive HTTPS continuous downloader — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a downloader path for YouTube live adaptive HTTPS formats (itag 271/313 on authenticated live streams) by introducing two methods on `YoutubeIE` that mark `hang=1`-shaped formats with the existing `http_dash_segments_generator` protocol and bind a fragment-yielding generator. Zero changes to any downloader.

**Architecture:** Two new methods on `YoutubeIE`: `_prepare_live_https_formats` (scans format list, detects hang=1 URLs via a `_is_hang_shaped` helper, rewrites protocol, binds a `functools.partial` of the generator, captures a refresh closure) and `_live_https_fragments` (the generator — loops, yields one fragment spec per iteration, reads `ctx['last_error']` as a back-channel, calls `refetch_url` on HTTP 4xx, exits on budget exhaustion or confirmed stream-end). One new call site after the existing `_prepare_live_from_start_formats` invocation.

**Tech Stack:** Python 3.9+, `yt-dlp`, `unittest`, `unittest.mock`. No new dependencies.

**Design doc:** `docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md` (commit `0e569af1d`).

---

## File Structure

**Create:**
- `test/test_youtube_live_https.py` — all three Tier-1 unit tests (URL matcher, generator state machine, stream-end termination).

**Modify:**
- `yt_dlp/extractor/youtube/_video.py` — add two methods on `YoutubeIE` (near the existing `_prepare_live_from_start_formats` / `_live_dash_fragments`), plus one call-site line near `_video.py:~4151`.

Nothing else changes. No edits to the downloader directory, the protocol registry, format selection, or any existing test.

**Naming conventions to match existing code:**
- Method names use single-leading-underscore (extractor-private): `_prepare_live_https_formats`, `_live_https_fragments`, `_is_hang_shaped`.
- Constants are UPPER_SNAKE_CASE, method-local: `FETCH_SPAN = 5`, `ERROR_BUDGET = 30`.
- Private helpers are methods, not module-level functions, to match `_live_dash_fragments` precedent.

---

## Task 1: URL matcher + minimal `_prepare_live_https_formats`

This task lands the test file with one test method, plus the minimal implementation that makes it pass: detecting hang=1 URLs and rewriting `protocol` + `is_live`. The generator and partial binding come in Task 2.

**Files:**
- Create: `test/test_youtube_live_https.py`
- Modify: `yt_dlp/extractor/youtube/_video.py` (add `_is_hang_shaped` staticmethod and minimal `_prepare_live_https_formats` method near `_live_dash_fragments` at line ~2042)

- [ ] **Step 1: Create the test file with the URL-matcher test**

Create `test/test_youtube_live_https.py` with this exact content:

```python
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
```

- [ ] **Step 2: Run the test and verify it fails**

Run from the repo root:

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected output contains:

```
AttributeError: 'YoutubeIE' object has no attribute '_prepare_live_https_formats'
```

Or similar. All four test methods should fail because the method doesn't exist yet.

- [ ] **Step 3: Implement `_is_hang_shaped` and `_prepare_live_https_formats` (minimal)**

Open `yt_dlp/extractor/youtube/_video.py`. Locate the end of `_live_dash_fragments` (around line 2101, before `_get_player_js_version` at line 2103). Insert the two methods right before `_get_player_js_version`.

Add this code (note: `urllib.parse` is already imported at the top of the file):

```python
    @staticmethod
    def _is_hang_shaped(fmt_url):
        """Return True iff URL query has source=yt_live_broadcast or hang=1.

        This is the YouTube live adaptive HTTPS `hang=1` shape described in issue #2.
        """
        if not fmt_url:
            return False
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(fmt_url).query)
        return ('yt_live_broadcast' in qs.get('source', [])
                or '1' in qs.get('hang', []))

    def _prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data):
        """Mark YouTube live adaptive HTTPS formats (hang=1 URLs) with the generator protocol
        so DashSegmentsFD picks them up. See issue #2 and the design doc at
        docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md.

        Minimal phase-A body: detect matching formats and rewrite protocol + is_live. The
        partial binding for `fragments` is added in Task 2; the refetch_url closure is
        added in Task 4.
        """
        for f in formats:
            if f.get('protocol') not in (None, 'https'):
                continue
            if not self._is_hang_shaped(f.get('url')):
                continue
            f['protocol'] = 'http_dash_segments_generator'
            f['is_live'] = True
```

- [ ] **Step 4: Run the test and verify it passes**

Run:

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected: all four test methods pass. Output contains `OK` at the end.

- [ ] **Step 5: Commit**

```bash
git add test/test_youtube_live_https.py yt_dlp/extractor/youtube/_video.py
git commit -m "$(cat <<'EOF'
[ie/youtube] Add live HTTPS URL matcher for hang=1 formats

First slice of the live adaptive HTTPS downloader (issue #2). Adds a
_is_hang_shaped staticmethod and a minimal _prepare_live_https_formats
that rewrites protocol to http_dash_segments_generator and sets is_live
for matching formats. Generator binding and refresh closure come in
follow-up commits.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Generator state machine + partial binding

This task adds `_live_https_fragments` and extends `_prepare_live_https_formats` to bind it via `functools.partial`. The `refetch_url` argument is still a placeholder — tests inject a mock. The real extractor-backed closure comes in Task 4.

**Files:**
- Modify: `test/test_youtube_live_https.py` (add 4 generator test methods)
- Modify: `yt_dlp/extractor/youtube/_video.py` (add `_live_https_fragments` method; extend `_prepare_live_https_formats` to bind partial)

- [ ] **Step 1: Add the generator state-machine tests**

Edit `test/test_youtube_live_https.py`. Add these imports after the existing ones:

```python
import itertools
from unittest import mock

from yt_dlp.networking.exceptions import HTTPError
```

Then append a new test class at the end of the file (after `TestLiveHttpsMatcher`, before the `if __name__ == '__main__':` line):

```python
def _make_http_error(status):
    """Construct an HTTPError without needing a real Response."""
    response = mock.Mock(status=status, reason=f'Status {status}')
    return HTTPError(response)


class TestLiveHttpsGenerator(unittest.TestCase):
    """Drives _live_https_fragments with a fake ctx and a mock refetch_url.
    See design doc §9 Test 2. time.sleep is patched to no-op so tests run fast
    regardless of FETCH_SPAN pacing."""

    def setUp(self):
        self.ie = YoutubeIE(FakeYDL())
        self.initial_url = 'https://example.com/frag?hang=1'
        self.sleep_patch = mock.patch('yt_dlp.extractor.youtube._video.time.sleep')
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def _run_gen(self, refetch_responses, driver):
        """Helper. refetch_responses is a list of (status, url) tuples a fake
        refetch_url will return in order. driver is a generator function that
        receives the live generator and yields control back to the test."""
        refetch_mock = mock.MagicMock(side_effect=list(refetch_responses))
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
        after budget exhausts. Budget is 30 with +=2 per error, so termination occurs
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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m unittest test.test_youtube_live_https.TestLiveHttpsGenerator -v
```

Expected: all four test methods fail with `AttributeError: 'YoutubeIE' object has no attribute '_live_https_fragments'`.

- [ ] **Step 3: Add `_live_https_fragments` method**

Open `yt_dlp/extractor/youtube/_video.py`. At the top of the file, check that `time`, `functools`, and `threading` are already imported (they are, at lines 5, 14, 15 respectively — verify before proceeding). Also verify `HTTPError` is imported from `...networking.exceptions` (line 31).

Insert this method immediately after `_prepare_live_https_formats` (which you added in Task 1):

```python
    def _live_https_fragments(self, video_id, format_id, initial_url, refetch_url, ctx):
        """Fragment generator for live adaptive HTTPS formats. See design doc §6.2.

        Yields one fragment spec per iteration. Reads ctx['last_error'] as the
        FragmentFD back-channel. On HTTP 4xx, calls refetch_url(format_id) which
        returns one of ('ok', url), ('ended', None), or ('retry', None). Exits
        on ('ended', None) or when error budget is exhausted.
        """
        FETCH_SPAN = 5
        ERROR_BUDGET = 30

        frag_index = 0  # internal counter; dash.py rewrites the outgoing value
        error_score = 0
        current_url = initial_url

        self.write_debug(f'[{video_id}] Generating live HTTPS fragments for format {format_id}')
        while True:
            last_error = ctx.pop('last_error', None)

            if last_error is not None:
                error_score += 2
                if isinstance(last_error, HTTPError) and last_error.status < 500:
                    status, new_url = refetch_url(format_id)
                    if status == 'ended':
                        self.to_screen(
                            f'[{video_id}] Live stream ended, finalizing output')
                        return
                    if status == 'ok':
                        current_url = new_url
                    # status == 'retry': keep current_url; budget will terminate if permanent

            if error_score > ERROR_BUDGET:
                self.report_warning(
                    f'[{video_id}] Error budget exhausted, stopping')
                return

            frag_index += 1
            t0 = time.time()
            yield {'url': current_url, 'fragment_count': None}

            if ctx.get('last_error') is None:
                error_score = max(0, error_score - 1)

            elapsed = time.time() - t0
            if elapsed < FETCH_SPAN:
                time.sleep(FETCH_SPAN - elapsed)
```

- [ ] **Step 4: Extend `_prepare_live_https_formats` to bind the partial**

Edit the body of `_prepare_live_https_formats`. Replace the existing match-block inside the `for f in formats:` loop so it binds `fragments` via `functools.partial`, using a placeholder `refetch_url` that raises — the real closure comes in Task 4. The test suite will not exercise the real closure because it passes its own mock `refetch_url`.

Replace this existing body:

```python
        for f in formats:
            if f.get('protocol') not in (None, 'https'):
                continue
            if not self._is_hang_shaped(f.get('url')):
                continue
            f['protocol'] = 'http_dash_segments_generator'
            f['is_live'] = True
```

With this:

```python
        def _placeholder_refetch_url(format_id):
            # Real closure is installed in Task 4 (wires _initial_extract + _list_formats).
            # Tests inject their own refetch_url directly when calling _live_https_fragments.
            raise NotImplementedError(
                'refetch_url closure not yet wired; see design doc §6.1')

        for f in formats:
            if f.get('protocol') not in (None, 'https'):
                continue
            if not self._is_hang_shaped(f.get('url')):
                continue
            f['protocol'] = 'http_dash_segments_generator'
            f['is_live'] = True
            f['fragments'] = functools.partial(
                self._live_https_fragments, video_id, f['format_id'], f['url'],
                _placeholder_refetch_url)
```

- [ ] **Step 5: Run all tests and verify pass**

Run:

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected: all 8 test methods pass (4 matcher + 4 generator). Output contains `OK` at the end.

- [ ] **Step 6: Commit**

```bash
git add test/test_youtube_live_https.py yt_dlp/extractor/youtube/_video.py
git commit -m "$(cat <<'EOF'
[ie/youtube] Add live HTTPS fragment generator and bind via partial

Adds _live_https_fragments with the full state machine from the design
doc: yields one fragment spec per iteration, reads ctx['last_error'] as
back-channel, calls refetch_url on HTTP 4xx, exits on ('ended', None) or
budget exhaustion (threshold 30, +=2 per error, -=1 per success).

Extends _prepare_live_https_formats to bind the partial for matching
formats. refetch_url is a placeholder here; real closure lands in a
follow-up commit. Tests inject their own mock refetch_url.

Covers issue #2 design §6.2 and §8. Unit tests lock in the +2/-1 budget
math and the ('ok'|'ended'|'retry') three-valued contract.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Stream-end termination test

Test 2 covered 'ok', 'retry', and budget exhaustion. Test 3 locks in the 'ended' termination path independently with explicit `StopIteration` + `to_screen` assertions.

**Files:**
- Modify: `test/test_youtube_live_https.py` (add one test method)

- [ ] **Step 1: Add the stream-end termination test**

Edit `test/test_youtube_live_https.py`. Inside the `TestLiveHttpsGenerator` class (after `test_budget_exhaustion_terminates`), add:

```python
    def test_stream_end_returns_cleanly(self):
        """refetch_url returning ('ended', None) terminates generator cleanly and
        emits a 'Live stream ended' message via to_screen. See design doc §9 Test 3.
        Must be driven stepwise — the generator only reaches refetch_url after
        ctx['last_error'] is seeded with an HTTPError."""
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
```

- [ ] **Step 2: Run the test and verify it passes**

Run:

```bash
python3 -m unittest test.test_youtube_live_https.TestLiveHttpsGenerator.test_stream_end_returns_cleanly -v
```

Expected: test passes. The 'ended' code path is already implemented (Task 2), so this is a verification, not a TDD red-green.

If it fails because `to_screen` wasn't called, check that the implementation in `_live_https_fragments` calls `self.to_screen(...)` and not `self.write_debug(...)` on the 'ended' path.

- [ ] **Step 3: Run the full test file to confirm no regressions**

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected: all 9 test methods pass.

- [ ] **Step 4: Commit**

```bash
git add test/test_youtube_live_https.py
git commit -m "$(cat <<'EOF'
[ie/youtube] Lock in stream-end termination for live HTTPS generator

Adds test_stream_end_returns_cleanly — drives the generator stepwise,
seeds ctx['last_error'] with a 403, and asserts StopIteration plus a
to_screen call containing 'Live stream ended'. Complements Test 2 by
exercising the ('ended', None) terminal path explicitly.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Real `refetch_url` closure

Replace the placeholder with the real closure that runs `_initial_extract` + `_list_formats`, returns `(status, url_or_none)`. Follows the `refetch_manifest` pattern in `_prepare_live_from_start_formats` at `_video.py:1942-1955`.

**Files:**
- Modify: `yt_dlp/extractor/youtube/_video.py` (replace placeholder `_placeholder_refetch_url` with real closure)

- [ ] **Step 1: Replace the placeholder with the real closure**

In `_prepare_live_https_formats`, replace the entire method body with this:

```python
    def _prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data):
        """Mark YouTube live adaptive HTTPS formats (hang=1 URLs) with the generator protocol
        so DashSegmentsFD picks them up. See issue #2 and the design doc at
        docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md.
        """
        lock = threading.Lock()

        def refetch_url(format_id):
            """Return fresh URL for the given format_id. See design doc §6.1 / §7 phase 5.

            Returns (status, url_or_none):
              ('ok', url)      - success
              ('ended', None)  - confirmed stream end (live_status flipped OR tier gone)
              ('retry', None)  - transient extractor failure; caller keeps current URL
            """
            with lock:
                try:
                    _, _, _, _, prs, player_url = self._initial_extract(
                        url, smuggled_data, webpage_url, self._webpage_client, video_id)
                    video_details = traverse_obj(prs, (..., 'videoDetails'), expected_type=dict)
                    microformats = traverse_obj(
                        prs, (..., 'microformat', 'playerMicroformatRenderer'),
                        expected_type=dict)
                    _, live_status, new_formats, _ = self._list_formats(
                        video_id, microformats, video_details, prs, player_url)
                except ExtractorError:
                    return 'retry', None

            if live_status != 'is_live':
                return 'ended', None

            match = next(
                (f for f in new_formats
                 if f.get('format_id') == format_id and self._is_hang_shaped(f.get('url'))),
                None)
            if match is None:
                return 'ended', None

            return 'ok', match['url']

        for f in formats:
            if f.get('protocol') not in (None, 'https'):
                continue
            if not self._is_hang_shaped(f.get('url')):
                continue
            f['protocol'] = 'http_dash_segments_generator'
            f['is_live'] = True
            f['fragments'] = functools.partial(
                self._live_https_fragments, video_id, f['format_id'], f['url'], refetch_url)
```

Verify `ExtractorError` and `traverse_obj` are imported at the top of `_video.py`. Run:

```bash
grep -n "^from.*import.*ExtractorError\|^from.*import.*traverse_obj" yt_dlp/extractor/youtube/_video.py | head -5
```

Both should already be imported (ExtractorError is standard; traverse_obj is used heavily throughout the file). If either is missing, add to the existing `from ...utils import ...` block.

- [ ] **Step 2: Run unit tests to confirm no regressions**

Run:

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected: all 9 test methods pass. The tests inject their own `refetch_url` directly into `_live_https_fragments`, so they don't exercise the real closure. The real closure is exercised by Task 6's manual smoke test.

- [ ] **Step 3: Run the broader YouTube test module to confirm no regressions**

Run:

```bash
python3 -m unittest test.test_youtube_misc -v
python3 -m unittest test.test_youtube_lists -v
```

Expected: both pass. These tests don't exercise `_prepare_live_https_formats` directly — this is a safety net to confirm the new code didn't accidentally break an existing YouTube code path.

- [ ] **Step 4: Commit**

```bash
git add yt_dlp/extractor/youtube/_video.py
git commit -m "$(cat <<'EOF'
[ie/youtube] Wire real refetch_url closure for live HTTPS formats

Replaces the Task 2 placeholder with the real refresh closure: runs
_initial_extract + _list_formats, returns a three-valued tuple:
- ('ok', url)      - fresh URL
- ('ended', None)  - successful re-extraction confirmed live_status flip
                     or our format_id is absent from refreshed list
- ('retry', None)  - ExtractorError raised; caller keeps current URL

The three-valued contract (design doc §6.1 / §7 phase 5) separates true
stream-end from transient extractor failures — the latter used to
silently finalize still-live recordings on any network blip.

Uses a threading.Lock, matching the _prepare_live_from_start_formats
convention. No network work happens at _prepare_live_https_formats call
time; the closure runs only when the generator calls it on HTTP 4xx.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire up the call site

Add one line in `_real_extract` so `_prepare_live_https_formats` actually fires on every extraction with `live_status == 'is_live'`.

**Files:**
- Modify: `yt_dlp/extractor/youtube/_video.py:~4151` (one added line after the existing `_prepare_live_from_start_formats` call)

- [ ] **Step 1: Confirm the current state of the call site**

Run:

```bash
sed -n '4150,4156p' yt_dlp/extractor/youtube/_video.py
```

Expected output (lines may vary slightly after Task 1-4 modifications added methods above line 2101 — the lines below should be ~2 lines later than shown; find the actual line by grep if needed):

```
        if needs_live_processing:
            self._prepare_live_from_start_formats(
                formats, video_id, live_start_time, url, webpage_url, smuggled_data, live_status == 'is_live')

        formats.extend(self._extract_storyboard(player_responses, duration))
```

If the exact lines differ, locate them with:

```bash
grep -n "self\._prepare_live_from_start_formats" yt_dlp/extractor/youtube/_video.py
```

- [ ] **Step 2: Add the call**

Using the Edit tool or equivalent, add a new conditional block immediately after the `_prepare_live_from_start_formats` call, before the `formats.extend(self._extract_storyboard(...))` line:

Find this exact block:

```python
        if needs_live_processing:
            self._prepare_live_from_start_formats(
                formats, video_id, live_start_time, url, webpage_url, smuggled_data, live_status == 'is_live')

        formats.extend(self._extract_storyboard(player_responses, duration))
```

Replace with:

```python
        if needs_live_processing:
            self._prepare_live_from_start_formats(
                formats, video_id, live_start_time, url, webpage_url, smuggled_data, live_status == 'is_live')

        if live_status == 'is_live':
            self._prepare_live_https_formats(formats, video_id, url, webpage_url, smuggled_data)

        formats.extend(self._extract_storyboard(player_responses, duration))
```

Rationale for `live_status == 'is_live'` (not `needs_live_processing`): `needs_live_processing` is truthy only when `--live-from-start` is passed or the stream is post-live with duration > 2h (see `_video.py:3195-3198`). Our feature targets the normal "watch live now" path which does not set `--live-from-start`. Gating on `live_status == 'is_live'` fires on every live stream regardless of the from-start flag.

- [ ] **Step 3: Run the full YouTube test suite**

Run:

```bash
python3 -m unittest test.test_youtube_live_https -v
python3 -m unittest test.test_youtube_misc -v
python3 -m unittest test.test_youtube_lists -v
```

Expected: all three pass. No regressions.

- [ ] **Step 4: Commit**

```bash
git add yt_dlp/extractor/youtube/_video.py
git commit -m "$(cat <<'EOF'
[ie/youtube] Wire _prepare_live_https_formats into extraction

Adds the call site for the new live HTTPS preparation pass. Gated on
live_status == 'is_live' (not needs_live_processing) so it fires on
every live stream, not only --live-from-start or long post-live runs.

Closes the implementation path for issue #2: formats=incomplete no
longer needed to be a feature flag for the matcher itself, but remains
the existing opt-in that lets hang=1 formats through the filter at
_video.py:3498-3500 in the first place.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Tier-2 manual smoke test

Not a code change. This is the verification step that proves the whole pipeline works against a real live stream. Run it before declaring phase A done.

**Files:** None.

- [ ] **Step 1: Locate a currently-live YouTube broadcast**

You need a live YouTube broadcast with 1440p or 2160p available. If the original test stream `fO9e9jnhYK8` is still live, use it. Otherwise find any authenticated YouTube live broadcast with high-tier live adaptive HTTPS formats.

Sanity-check that the target stream actually offers itag 271 or 313:

```bash
python3 -m yt_dlp -F \
  --extractor-args "youtube:formats=incomplete" \
  --cookies-from-browser "edge:Profile 5" \
  --remote-components ejs:github \
  "<live_stream_url>"
```

Expected: format table includes a row for itag 271 (2560x1440) or 313 (3840x2160) labelled `(incomplete)`.

- [ ] **Step 2: Run the smoke capture (60 seconds)**

```bash
timeout 90 python3 -m yt_dlp -v -f 271 \
  --extractor-args "youtube:formats=incomplete" \
  --cookies-from-browser "edge:Profile 5" \
  --remote-components ejs:github \
  --no-part \
  -o "/tmp/live_https_smoke.%(ext)s" \
  "<live_stream_url>"
```

The `timeout 90` gives 90 seconds total (to accommodate extraction overhead) and terminates with SIGTERM. Alternatively, Ctrl-C manually after ~60 seconds of fragment downloads are observed in the verbose output.

Look in verbose output for:
- `Generating live HTTPS fragments for format 271` (proves our `_live_https_fragments` generator is running)
- Fragment-by-fragment download progress (proves DashSegmentsFD is consuming the generator)

If the verbose log shows `[download] Destination: ... .webm` followed by fragment-by-fragment progress and no immediate `ERROR`, the smoke test is progressing.

- [ ] **Step 3: Verify the output**

```bash
ffprobe /tmp/live_https_smoke.webm 2>&1 | head -40
```

Expected output contains:

- `Input #0, matroska,webm`
- A video stream: `Stream #0:0: Video: vp9` with resolution `2560x1440` (for itag 271)
- `Duration: 00:00:XX.XX` with XX in the 30-90 second range

If duration is 00:00:05 or less, the generator terminated prematurely — inspect the verbose log for early error-budget exhaustion or spurious `('ended', None)` returns.

- [ ] **Step 4: Record the result**

No commit. Document in your own notes (or as a comment on issue #2) what you observed:
- Output file size
- Duration captured
- Resolution confirmed via ffprobe
- Any 403 retry events seen in verbose log (these prove the refresh path)
- Memory footprint at end (`ps -o rss= -p <pid>` before terminating)

If the smoke test fails, the failure is a phase-A bug. Common failure modes to check:
- `Error budget exhausted, stopping` early in the run → excessive transient errors, tune budget or refresh logic
- `Live stream ended, finalizing output` when stream is clearly still live → misfire in `refetch_url`'s `'ended'` path; add logging to distinguish which sub-case triggered
- First fetch fails with 403 and never recovers → refresh logic not working; verify `_initial_extract` returns a usable URL

---

## Self-review (done before handoff)

**Spec coverage.** Walked through each design-doc section:

- §3 Goals / non-goals — Plan covers all four phase-A goals; all seven non-goals remain unimplemented (not in any task).
- §4 Approach selected — Tasks 1-5 implement Approach 1 exclusively. No dedicated downloader class, no new protocol, no ffmpeg wrapper.
- §5 Architecture — Task 1 adds `_is_hang_shaped`; Task 1+2+4 build up `_prepare_live_https_formats`; Task 2 adds `_live_https_fragments`; Task 5 adds the call site. All in `_video.py`.
- §6.1 `_prepare_live_https_formats` — Match conditions in Task 1 (URL-shape + protocol); refetch closure in Task 4; partial binding in Task 2.
- §6.2 `_live_https_fragments` — Full pseudocode transcribed in Task 2 Step 3.
- §7 Data flow — Phases 1-5 map to the implementation; no test for data flow beyond what `FragmentFD` already exercises (out of scope per §9 "explicitly not built in phase A").
- §8 Error handling — Budget math, 4xx vs 5xx differentiation, 'retry' vs 'ended' all exercised in Task 2 tests + Task 3.
- §9 Testing — Tier 1 tests in Tasks 1, 2, 3; Tier 2 manual in Task 6; Tier 3 UAT deferred to post-implementation.
- §10 Phase-B promotion — Out of scope for this plan.
- §11 Assumptions — Phase-A assumptions don't have corresponding tests; they're validated empirically via Task 6.

**Placeholder scan.** No "TBD", "TODO", "implement later", "add appropriate error handling", or incomplete code blocks. Every step has the actual content.

**Type consistency.** Checked all method signatures match across tasks:
- `_is_hang_shaped(fmt_url) -> bool` consistent
- `_prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data)` consistent
- `_live_https_fragments(self, video_id, format_id, initial_url, refetch_url, ctx)` consistent — passed positionally in partial in Task 2 Step 4 and in the generator definition in Task 2 Step 3
- `refetch_url(format_id) -> (str, str | None)` consistent — placeholder in Task 2 raises `NotImplementedError` with same signature shape; real closure in Task 4 matches.

No drift.
