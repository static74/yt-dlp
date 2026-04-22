# YouTube live adaptive HTTPS continuous downloader — phase-A design

**Date:** 2026-04-21
**Author:** static74 (working with Claude)
**Tracks issue:** `static74/yt-dlp#2`
**Status:** approved design, pending implementation plan
**Target head at design time:** `165ee77a2`

## 1. Context

`yt-dlp` currently surfaces high-tier live adaptive HTTPS formats (itag 271 at 1440p, itag 313 at 2160p) on authenticated YouTube live streams when the user passes `--extractor-args youtube:formats=incomplete`. The URLs are valid and return real authenticated VP9 data. No downloader in the current set can walk them continuously: each yields exactly one ~5-second fragment and stops.

Root cause (per issue #2):

- YouTube's `source=yt_live_broadcast` endpoint returns one `Target-Duration-Us: 5000000` WebM fragment per GET.
- `hang=1` holds the connection until the next fragment is ready, then closes it.
- `noclen=1` suppresses `Content-Length`.
- The response is not seekable and not byte-range resumable.
- To walk the timeline, the client must issue fresh GETs. None of yt-dlp's existing downloaders do this for this URL shape; ffmpeg's `-reconnect_at_eof` uses byte-range resume which the server rejects.

This is a pure downloader gap. The extractor side already works once `formats=incomplete` lifts the filter at `yt_dlp/extractor/youtube/_video.py:3498-3500`.

## 2. Scope decisions

Phase A answers three scope questions:

| Question | Choice | Consequence |
|---|---|---|
| Upstream vs fork-only? | **C — pragmatic first, refactor-ready for upstream** | Phase A lives in the YouTube extractor; phase B lifts shared code into a downloader module and (optionally) a new protocol name. |
| Expected run duration? | **B — hours** | Must handle URL expiry, transient 4xx, and long-running memory behavior. Short runs and always-on 24/7 capture are not phase-A targets. |
| Integration point? | **Approach 1 — extractor-level `_live_https_fragments` generator, reusing `http_dash_segments_generator` protocol** | Smallest phase-A patch. Phase-B promotion is mechanical. Approach 2 (new downloader class / new protocol) pays generalization cost upfront without evidence; Approach 3 (ffmpeg wrapper) is throwaway. |

## 3. Goals and non-goals

### Goals (phase A)

- Make `-f 271` / `-f 313` on an authenticated live YouTube broadcast produce a continuous, playable WebM file when invoked with `--extractor-args youtube:formats=incomplete`.
- Survive URL expiry transparently over hours-long runs via reactive refresh.
- Terminate gracefully when the broadcast ends or when a bounded error budget is exhausted.
- Phase A is opt-in via the existing `formats=incomplete` extractor-arg. No new user flag.

### Non-goals (phase A)

- Generic (non-YouTube) hang=1 downloader support.
- Proactive URL refresh from the `expire` query param.
- Sequence-number dedup (the `hang=1` server contract should prevent dupes; add in phase B if observed).
- Sequence-number gap detection or recovery (gaps are permanent for a live-only URL).
- Bandwidth stepdown between itags mid-stream.
- Live resume across process restart (FragmentFD already treats `live=True` as non-resumable).
- CI coverage of the live path.
- A new downloader class, new protocol name, or any edits outside the extractor.

## 4. Approach selected: Approach 1 rationale

Two alternatives considered and rejected for phase A:

- **Approach 2 — new `LiveHttpsFD` downloader + new `live_http` protocol.** Cleaner upstream story, but commits to a generalization (any extractor can use it) before there's evidence for a second extractor, and requires touching `downloader/__init__.py`, external downloader delegation, and format-selection plumbing.
- **Approach 3 — ffmpeg wrapper loop.** Issue #2 Test C already disproves ffmpeg's own `-reconnect_at_eof` (byte-range resume is rejected). Any ffmpeg-based solution would have to drive the loop from yt-dlp, making it structurally equivalent to Approaches 1/2 but with worse observability (no access to sequence numbers) and no phase-B carryover.

Approach 1 gets to a runnable implementation fastest, preserves full observability (we control the HTTP call), and the phase-B lift is mechanical: move the generator body into a shared module, optionally alias a new protocol name.

## 5. Architecture

All new code lives in `yt_dlp/extractor/youtube/_video.py`. Zero changes to any downloader.

**Two new methods on `YoutubeIE`, placed next to their DASH analogues:**

- **`_prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data)`** — scans `formats` for entries that match the hang=1 shape and, for each match, rewrites its protocol to `http_dash_segments_generator`, binds a fragment generator via `functools.partial`, and sets `is_live=True`. Holds a closure over enough extraction context to refresh URLs when they expire.
- **`_live_https_fragments(self, video_id, itag, client_name, initial_url, refetch_url, ctx)`** — the generator. Infinite loop that yields one fragment dict per iteration, reads `ctx['last_error']` as an error back-channel, calls `refetch_url` on HTTP failures, and exits on budget exhaustion or stream-end signal.

**Call site:** one added line at `_video.py:~4151`, guarded by `live_status == 'is_live'`:

```python
if needs_live_processing:
    self._prepare_live_from_start_formats(
        formats, video_id, live_start_time, url, webpage_url, smuggled_data, live_status == 'is_live')
# NEW:
if live_status == 'is_live':
    self._prepare_live_https_formats(formats, video_id, url, webpage_url, smuggled_data)
```

**Filter at `_video.py:3498-3500`: unchanged.** `formats=incomplete` stays the opt-in.

**Downloader side: nothing.** `DashSegmentsFD` at `dash.py:18` already disables external-downloader delegation for `http_dash_segments_generator` (`real_downloader = None`), already consumes callable `fragments` via `_get_fragments` at `dash.py:74`, and already handles `--load-info-json` re-extraction via `ReExtractInfo` at `dash.py:32-34`.

### What's reused vs new

| Concern | Handled by | Notes |
|---|---|---|
| Fragment concat, merging | `FragmentFD` (parent of `DashSegmentsFD`) | Free |
| Progress reporting, resume semantics | `FragmentFD` | Free |
| `--load-info-json` re-extraction | `DashSegmentsFD._get_fragments` via `ReExtractInfo` | Free |
| Loop, GET, yield | `_live_https_fragments` | New |
| URL expiry + refresh | Closure into `_initial_extract` | New |
| Stream-end detection | `_live_https_fragments` (bounded error budget) | New |

## 6. Components

### 6.1 `_prepare_live_https_formats`

**Signature:**

```python
def _prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data):
```

**Match conditions:**

- `f.get('protocol') == 'https'` (or unset), AND
- URL query (parsed with `urllib.parse.parse_qs`) contains `source=yt_live_broadcast` OR `hang=1`.

**For each match:**

- Build a `refetch_url(itag, client_name)` closure capturing `(url, smuggled_data, webpage_url, video_id)` and a `threading.Lock()`. On call:
  1. Acquire lock.
  2. Try `_initial_extract(url, smuggled_data, webpage_url, self._webpage_client, video_id)` plus `_list_formats(...)`.
  3. If `ExtractorError` raised, return `None`.
  4. If `live_status != 'is_live'`, return `None`.
  5. Find the format matching `(itag, client_name)`; if absent, return `None`.
  6. Return that format's `url`.
- `f['protocol'] = 'http_dash_segments_generator'`
- `f['fragments'] = functools.partial(self._live_https_fragments, video_id, f['_itag'], f['_client'], f['url'], refetch_url)`
- `f['is_live'] = True`

Does NOT rewrite `f['url']`. `--get-url`, `--dump-json`, `--simulate` keep seeing the initial URL. `DashSegmentsFD` only consumes `f['fragments']` during actual download.

**Returns:** None (mutates in place).
**Raises:** Nothing. Non-matching formats are untouched.
**Thread-safety:** `refetch_url` locks before re-extraction, matching the `_prepare_live_from_start_formats` convention.

### 6.2 `_live_https_fragments`

**Signature:**

```python
def _live_https_fragments(self, video_id, itag, client_name, initial_url, refetch_url, ctx):
```

**Contract:**

- `ctx` is the fragment-downloader context dict. Generator reads `ctx.pop('last_error', None)` at the top of each iteration (same contract as `_live_dash_fragments`).
- Yields dicts of shape:
  ```python
  {'url': current_url, 'fragment_count': None}
  ```
  `DashSegmentsFD._get_fragments` at `dash.py:78-91` recomputes `frag_index` from iteration count and rewrites it on the outgoing fragment spec, so we don't include it in our yield. We keep an internal `frag_index` counter for our own debug logging.

**Constants:**

- `FETCH_SPAN = 5` (nominal inter-fragment interval; matches observed `Target-Duration-Us: 5000000`)
- `ERROR_BUDGET = 30` (matches `_live_dash_fragments`)

**Loop outline:**

```python
frag_index  = 0    # internal counter for debug logging only; dash.py rewrites the outgoing value
error_score = 0
current_url = initial_url

while True:
    last_error = ctx.pop('last_error', None)

    if last_error is not None:
        error_score += 2
        if isinstance(last_error, HTTPError) and last_error.status < 500:
            new_url = refetch_url(itag, client_name)
            if new_url is None:
                self.to_screen(f'[{video_id}] Live stream ended, finalizing output')
                return
            current_url = new_url
        # 5xx and network errors: do not refresh, retry same URL

    if error_score > ERROR_BUDGET:
        self.report_warning(f'[{video_id}] Error budget exhausted, stopping')
        return

    frag_index += 1
    t0 = time.time()
    yield {'url': current_url, 'fragment_count': None}

    if ctx.get('last_error') is None:
        error_score = max(0, error_score - 1)   # slow decay on success

    elapsed = time.time() - t0
    if elapsed < FETCH_SPAN:
        time.sleep(FETCH_SPAN - elapsed)
```

## 7. Data flow

### Phase 1: extraction (once, upfront)

- `YoutubeIE._real_extract` → `_list_formats` → `process_https_formats` at `_video.py:3493`.
- Filter at 3498-3500: `formats=incomplete` keeps format 271.
- Built with `protocol='https'`, `url=<hang=1 URL with expire=...>`.
- `adjust_incomplete_format` tags `(incomplete)` with `pref_adjustment=-20` (unchanged).
- `_prepare_live_from_start_formats` runs (untouched by us; skips non-`is_from_start` formats).
- **NEW:** `_prepare_live_https_formats` runs. Matches format 271. Rewrites protocol, binds generator, sets `is_live=True`.

### Phase 2: selection + dispatch

- `-f 271` selects the modified format.
- `get_suitable_downloader(protocol='http_dash_segments_generator')` → `DashSegmentsFD`.
- `DashSegmentsFD.real_download(filename, info_dict)`.

### Phase 3: fragment loop

- `DashSegmentsFD.real_download` at `dash.py:17`:
  - Protocol is generator → `real_downloader = None`.
  - `ctx = {filename, live=True, total_frags=None}` (`len(generator)` raises `TypeError`, caught at `dash.py:37-39`).
  - `_get_fragments(fmt, ctx, extra_query=None)`.
- `_get_fragments`:
  - `_resolve_fragments(fmt['fragments'], ctx)` calls the partial → returns generator `G`.
  - Iterates `G`, yields per-fragment download specs to `FragmentFD`.
- `FragmentFD.download_and_append_fragments_multiple`:
  - For each spec: HTTP GET → write bytes → loop.
  - On failure: `ctx['last_error'] = exc` (generator reads on next iteration).

### Phase 4: generator steady state

Described in §6.2.

### Phase 5: URL refresh

`refetch_url` acquires its lock, re-runs `_initial_extract` + `_list_formats`, and returns either a fresh URL for the matching `(itag, client_name)` or `None`. `None` means "stop gracefully" and covers three cases (auth loss, live→VOD transition, itag dropped from tier list).

### Interactions

- **`--load-info-json`:** generator can't serialize; `DashSegmentsFD` raises `ReExtractInfo` at `dash.py:32-34` → outer YDL loop re-extracts → format freshly prepared.
- **`--concurrent-fragments`:** N/A; consumption is sequential.
- **Ctrl-C:** `FragmentFD` catches `KeyboardInterrupt`, closes file, generator is GC'd.
- **`--merge-output-format mp4`, `--recode-video`:** run post-download on the concatenated WebM.
- **`--cookies-from-browser`, PO tokens, EJS cache:** live inside `_initial_extract`, inherited by `refetch_url`.

### Subtlety

The initial URL captured at extraction time may expire before the first fetch. First-fragment-failure-then-recover is a normal path, not an abort: it costs one extra refresh (~5–10 seconds) and the run continues.

## 8. Error handling

### Back-channel

One key: `ctx['last_error']`. Generator reads at iteration top, downloader writes on fetch failure. No exceptions cross the generator boundary during steady state.

### Error table

| Category | Detection | Action | Termination |
|---|---|---|---|
| Transient network (timeout, DNS, conn reset) | `URLError` / `OSError` / `TimeoutError` | `error_score += 2`, retry same URL | `error_score > 30` |
| HTTP 403 | `HTTPError(status=403)` | `error_score += 2`, refresh URL | budget OR refresh returns `None` |
| HTTP 404 | `HTTPError(status=404)` | Same as 403 | Same |
| HTTP other 4xx | `HTTPError(status < 500)` | Same as 403 | Same |
| HTTP 5xx | `HTTPError(status >= 500)` | `error_score += 2`, do NOT refresh | `error_score > 30` |
| Stream ended | `refetch_url` returns `None` | `to_screen('Live stream ended, finalizing output')`, return | Immediate, graceful |
| Auth loss mid-run | `_initial_extract` raises inside `refetch_url` | `refetch_url` catches, returns `None` → stream-end path | Immediate, graceful |
| Budget exhausted | `error_score > 30` | `report_warning('Error budget exhausted, stopping')`, return | Immediate |
| User Ctrl-C | `KeyboardInterrupt` in `FragmentFD` | File closed, generator GC'd | Immediate, partial file preserved |

### Budget math

- `error_score` starts at 0. `+= 2` per error, `max(0, -1)` per clean iteration. Budget `> 30`.
- Worst sustained failure: score grows 2/iter → terminates at ~iter 16 → ~80 seconds of failed attempts before giving up.
- Worst recoverable (1 failure per 2 successes): net `0` per average 3 iterations → never terminates, correct behavior.
- One-off 403 at startup: score=2, refresh, recover by iter 3.

### Refresh policy differentiation (403/404/4xx vs 5xx)

- 4xx: almost certainly expiry. Refresh is the right move.
- 5xx: server-side flap. Refresh re-invokes full `_initial_extract` (JS challenge, player-response fetch) which is expensive and may cascade during a YouTube outage. Retry same URL instead.

This is a deliberate divergence from `_live_dash_fragments`'s uniform refresh. Rationale: MPD refresh is cheap (one HTTP fetch); full re-extraction is not.

## 9. Testing

### Tier 1: unit tests (deterministic, live in `test/test_youtube_live_https.py`)

Three tests. The second is the one we will not drop under any condition.

**Test 1: URL matching heuristic.** Parametrized over:

- `source=yt_live_broadcast&hang=1&noclen=1` → match
- `source=yt_live_broadcast` alone → match
- `hang=1` alone → match
- No relevant params → no match
- Non-HTTPS URL → no match

Assert `_prepare_live_https_formats` does (or does not) rewrite the format's `protocol`.

**Test 2: generator state machine (required, load-bearing).** Drives `_live_https_fragments` with a fake `refetch_url` that records calls, and a synthetic `ctx` that the test mutates between `next()` calls to simulate `FragmentFD` setting `last_error`. Three sub-cases:

- **Happy path:** no errors, 5 yields against `initial_url`, `refetch_url` never called.
- **403 refresh:** one yield against `initial_url`, test sets `ctx['last_error'] = HTTPError(status=403)`, next yield uses the new URL, `refetch_url` call count == 1.
- **Budget exhaustion:** continuous 403s; assert generator terminates after the predicted number of yields based on `+2/-1` math and budget 30.

This test locks in the budget math. That math is the subtlest piece of phase-A logic and the most likely to be edited silently by future maintainers. This test is non-negotiable.

**Test 3: stream-end termination.** Fake `refetch_url` returns `None` on first call. Assert `list(gen)` is finite and does not raise.

### Tier 2: manual smoke test

A short script or documented invocation for post-implementation verification on a known-live broadcast. Run 60 seconds, Ctrl-C, verify ffprobe output matches expected resolution and codec.

### Tier 3: acceptance UAT

30+-minute run on the user's actual target stream. Pass conditions:

- Output file plays end-to-end in VLC.
- Verbose log shows successful recovery from any 403 observed (proves refresh works).
- Memory usage at end comparable to start.

### Explicitly not built in phase A

- HTTP fixture server for integration tests (phase B).
- CI coverage of the live path.
- Property-based testing of the error budget.

## 10. Phase-B promotion plan

When this refactors for upstream submission:

- `_live_https_fragments` body → shared module (candidate: `yt_dlp/downloader/_live_http.py` or `yt_dlp/extractor/youtube/_live_https.py` depending on reviewer preference).
- YouTube-specific URL-matching heuristic stays in the extractor.
- Optionally introduce `live_http` as a sibling protocol of `http_dash_segments_generator`, with both routing to `DashSegmentsFD`. One-line change in `downloader/__init__.py`.
- Tier 1 tests migrate to `test/`, alongside existing YouTube extractor tests.
- Tier 2 smoke script becomes a `devscripts/` entry or is documented in the PR description.
- Add fixture-based integration test if a simulation of the hang=1 contract proves feasible.
- Honor `CONTRIBUTING.md` style rules (long lines, quotes, fallbacks).
- Review AI/LLM contribution policy in `CONTRIBUTING.md` before submission.

## 11. Open questions and explicit assumptions

Phase A proceeds on these assumptions. If any turn out wrong during implementation or UAT, they become discovered phase-A bugs, not design flaws.

- **Assumption:** `hang=1` on the YouTube endpoint reliably blocks server-side until the next fragment is ready. Evidence: issue #2 Test B observed one fragment per GET, wall-clock pacing matched `Target-Duration-Us: 5000000`. If the server ever short-circuits and returns a stale fragment repeatedly, the `FETCH_SPAN` sleep floor prevents hammering but dupe detection is not in phase A.
- **Assumption:** `_initial_extract` + `_list_formats` is callable multiple times per download run without excessive cost or rate-limiting. Evidence: `_live_dash_fragments` already exercises the same pattern (`refetch_manifest`) for hour-scale captures.
- **Assumption:** The `expire` query param on the initial URL gives meaningful headroom (>= 1 minute) for the extraction-to-download handoff. If expiries are tight enough that first-fragment-always-fails, we pay one refresh per run but never diverge from the design.
- **Assumption:** Auth state (`--cookies-from-browser`, PO tokens) loaded at initial extraction remains in-memory and reusable across subsequent `_initial_extract` calls within the same process. Evidence: yt-dlp loads the cookiejar once at startup; PO token and JS challenge caches are process-lifetime.
