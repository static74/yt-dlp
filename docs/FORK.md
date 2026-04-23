# Fork-specific changes (`static74/yt-dlp`)

This file documents everything that differs between this fork and upstream `yt-dlp/yt-dlp`. Two audiences are targeted:

1. **Integration / downstream users** who want to use the fork's added features. Jump to [Features](#features) and [Usage](#usage).
2. **Future maintainers doing an upstream sync** (including future Claude sessions). Jump to [Upstream sync playbook](#upstream-sync-playbook).

> **Do not submit this work as a PR to `yt-dlp/yt-dlp`.** Upstream's `CONTRIBUTING.md` has an [AUTOMATED CONTRIBUTIONS (AI / LLM) POLICY](https://github.com/yt-dlp/yt-dlp/blob/master/CONTRIBUTING.md#automated-contributions-ai--llm-policy) that prohibits AI-generated code and the code on this branch carries `Co-Authored-By: Claude ...` commit trailers. Submission would violate policy and risks the author account being blocked. See [Why no upstream PR](#why-no-upstream-pr) for the full rationale and alternative paths.

---

## Features

### YouTube live adaptive HTTPS downloader (hang=1 / `source=yt_live_broadcast`)

Enables continuous downloading of the high-tier live adaptive HTTPS video formats (itag 271 at 1440p and itag 313 at 2160p) from authenticated YouTube live streams. These formats are served with `source=yt_live_broadcast` and `hang=1` on the URL query string, which causes the connection to hold until the next segment is ready, then send one segment and close. Stock yt-dlp downloaders retrieve exactly one fragment against such URLs and stop, because they have no mechanism for continuous fragment-by-fragment fetching of this URL shape.

**Status:** Phase A (production-ready for fork use). Phase B items (upstream-ready refactor, extra edge-case test coverage) are deferred. See [`docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md`](superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md) §10 for Phase B scope.

**Tracking issue:** [static74/yt-dlp#2](https://github.com/static74/yt-dlp/issues/2) (closed, fully documented).

---

## Usage

The feature activates automatically on every live stream when the `formats=incomplete` extractor arg is passed. No new command-line flag was added.

### Minimum invocation

```bash
python3 -m yt_dlp -f 271 \
  --extractor-args "youtube:formats=incomplete" \
  --cookies-from-browser "<your_browser>:<profile>" \
  "<youtube_live_url>"
```

### What the flag does

- `--extractor-args "youtube:formats=incomplete"` opts past the filter at `yt_dlp/extractor/youtube/_video.py:3498-3500` that hides live adaptive HTTPS (`hang=1`) formats by default. This is an existing, upstream flag, not a fork-added one.
- `-f 271` (or `-f 313`) selects the high-tier format.
- Authentication cookies are required; the formats are only served to logged-in sessions with appropriate entitlements (Premium on the tested stream).

### What happens automatically

When `_real_extract` finishes and the stream has `live_status == 'is_live'`, the fork's `_prepare_live_https_formats` method scans every format. For each HTTPS format whose URL query carries `source=yt_live_broadcast` or `hang=1`, it:

1. Rewrites the format's `protocol` field to `http_dash_segments_generator` (an existing upstream protocol).
2. Sets `is_live = True`.
3. Binds the format's `fragments` key to a `functools.partial` of `_live_https_fragments`, capturing a `refetch_url` closure that can re-run extraction to get a fresh URL on HTTP 4xx.

`DashSegmentsFD` then consumes the generator fragment-by-fragment until the stream ends or the error budget is exhausted.

### Affected formats

| itag | Resolution | Codec | Protocol (fork) |
|------|-----------|-------|-----------------|
| 271  | 2560x1440 | vp9   | `http_dash_segments_generator` |
| 313  | 3840x2160 | vp9   | `http_dash_segments_generator` |

Lower-tier live formats (160, 133, 134, 135, 136, 137, 242, 243, 244, 247, 248, 278) are also rewritten if they come down as `hang=1`-shaped. The itag list above is just what is commonly encountered.

### Known limitations (Phase A)

- **No explicit ad-splicing.** YouTube's `Cuepoint-Type: TYPE_AD` metadata passes through in the WebM container. Downstream processing can strip if needed.
- **Shape drift terminates the stream.** If a format's URL stops being `hang=1`-shaped mid-run, `refetch_url` returns `('ended', None)` and the generator exits. Real-world occurrence unknown; Phase B may relax this.
- **Error budget is hard-coded.** `FETCH_SPAN = 5`, `ERROR_BUDGET = 30`, `+=2` per error, `-=1` per success. Not user-tunable. See `_live_https_fragments` for rationale.
- **Only `ExtractorError` triggers retry.** `URLError` and `TransportError` raised in the refresh path currently propagate rather than being treated as transient. Phase B widens this.

---

## Code changes reference

### Commits

All live HTTPS work lands in this ordered commit range on `master`:

```
d4073fd63 [ie/youtube] Add live HTTPS URL matcher for hang=1 formats
5ce349b79 [ie/youtube] Remove em-dash from _is_hang_shaped docstring
f519656f4 [ie/youtube] Add live HTTPS fragment generator and bind via partial
c50ca7fe8 [ie/youtube] Lock in stream-end termination for live HTTPS generator
b63426206 [ie/youtube] Wire real refetch_url closure for live HTTPS formats
db1f77299 [ie/youtube] Wire _prepare_live_https_formats into extraction
36f8e715b [ie/youtube] Hint at formats=incomplete when filtering live adaptive HTTPS
```

Plus docs:

```
bd92caeba docs: design for YouTube live adaptive HTTPS continuous downloader (phase A)
2b573a50e docs: fix two review findings in live HTTPS design
0e569af1d docs: tighten Test 3 wording (review finding)
5c9119a7b docs: implementation plan for YouTube live HTTPS downloader
ee1c7ffec docs: tick completed checkboxes on live HTTPS plan
```

### Files touched (diff vs upstream master)

| Path | Status | Purpose |
|------|--------|---------|
| `yt_dlp/extractor/youtube/_video.py` | modified, +122 lines | Adds `_is_hang_shaped`, `_prepare_live_https_formats`, `_live_https_fragments`; one call-site line after `_prepare_live_from_start_formats` |
| `test/test_youtube_live_https.py` | new file, +170 lines | 9 unit tests (URL matcher, generator state machine, stream-end termination) |
| `docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md` | new file | Design contract |
| `docs/superpowers/plans/2026-04-21-youtube-live-https-downloader.md` | new file | Task-by-task implementation plan (all 27 steps ticked) |
| `docs/FORK.md` | new file (this file) | Fork overview |

### New methods on `YoutubeIE`

All live near the existing `_live_dash_fragments` method in `_video.py`:

- **`_is_hang_shaped(fmt_url)`** `@staticmethod`. Returns `True` iff the URL query string contains `source=yt_live_broadcast` or `hang=1`. Pure function, safe to call anywhere.
- **`_prepare_live_https_formats(self, formats, video_id, url, webpage_url, smuggled_data)`**. Iterates `formats`, rewrites matching entries, binds generator partials, captures a `refetch_url` closure scoped to this extraction.
- **`_live_https_fragments(self, video_id, format_id, initial_url, refetch_url, ctx)`**. The generator. Yields `{'url': current_url, 'fragment_count': None}` per iteration. Reads `ctx['last_error']` as back-channel.

### Upstream call-site addition

One `if` block inserted after the existing `_prepare_live_from_start_formats` call in `_real_extract` (near line 4151):

```python
if live_status == 'is_live':
    self._prepare_live_https_formats(formats, video_id, url, webpage_url, smuggled_data)
```

Gating on `live_status == 'is_live'` (not `needs_live_processing`) is intentional. See plan document Task 5 Step 2 rationale for why.

---

## Testing

### Unit tests (always before pushing or merging upstream)

```bash
python3 -m unittest test.test_youtube_live_https -v
```

Expected: 9 tests pass. Covers:

- 4 URL-matcher cases (match, non-match, wrong protocol, missing URL)
- 4 generator state-machine cases (happy path, 403 triggers refresh, transient retry keeps URL, budget exhaustion terminates)
- 1 stream-end termination with explicit `to_screen` assertion

### Smoke test against a live YouTube stream

The full procedure is in the plan document, Task 6, step-by-step. One-line summary:

```bash
timeout 90 python3 -m yt_dlp -v -f 271 \
  --extractor-args "youtube:formats=incomplete" \
  --cookies-from-browser "edge:Profile 5" \
  --no-part \
  -o "/tmp/live_https_smoke.%(ext)s" \
  "<currently_live_youtube_url>"
```

Then `ffprobe /tmp/live_https_smoke.webm` and check: container is matroska/webm, resolution 2560x1440, frame rate 30/1, packet count ÷ 30 ≈ seconds captured.

Healthy signals in the verbose log:

- `Invoking dashsegments downloader on "...source=yt_live_broadcast&...hang=1&..."`
- `[debug] [youtube] [<id>] Generating live HTTPS fragments for format 271`
- Fragment counter incrementing at roughly one every 5 seconds

---

## Upstream sync playbook

When `yt-dlp/yt-dlp` cuts a release that has fixes the fork needs, follow this protocol.

### Before merging upstream

1. **Confirm all fork work is committed and pushed.** `git status` clean, `git log origin/master..HEAD` empty.
2. **Run unit tests** (see [Testing](#testing)). Must be green before the merge, otherwise you cannot distinguish pre-existing breakage from merge-induced breakage.
3. **Note the current merge base:** `git merge-base upstream/master master`. This is your "was working at this commit" anchor.

### Performing the merge

```bash
git fetch upstream master
git merge upstream/master
```

Expect conflicts in `yt_dlp/extractor/youtube/_video.py` only. The other fork files (`docs/`, `test/test_youtube_live_https.py`) live in paths upstream does not touch.

### Upstream seams the fork depends on

These are the exact upstream functions and attributes the fork-added code calls into. If upstream changes their signature, return shape, or behaviour, the fork-added code breaks. Inspect each after merge:

| Seam | Location | What the fork does with it |
|------|----------|---------------------------|
| `_initial_extract(url, smuggled_data, webpage_url, client, video_id)` | `_video.py` | Called inside `refetch_url` closure. Expects 6-tuple return: `(_, _, _, _, prs, player_url)` |
| `_list_formats(video_id, microformats, video_details, prs, player_url)` | `_video.py` | Called inside `refetch_url`. Expects 4-tuple return: `(_, live_status, new_formats, _)` |
| `_prepare_live_from_start_formats(...)` call site | `_video.py:~4151` | The fork-added block is inserted immediately after this call. If upstream moves or removes this call, relocate the insertion. |
| Filter on `hang=1` formats | `_video.py:~3498-3500` | The `formats=incomplete` extractor arg is what lets these formats through in the first place. If upstream changes the filter or its opt-out, update the fork docs to match. |
| `http_dash_segments_generator` protocol | downloader registry | The protocol name we rewrite to. If upstream renames or removes, swap. |
| `FragmentFD` `ctx['last_error']` back-channel | `yt_dlp/downloader/fragment.py` | The generator reads this to detect HTTP errors. If upstream changes the contract, the refresh logic breaks silently (no errors, just never retries). |

### After merging upstream

Run **both** test suites back to back:

```bash
python3 -m unittest test.test_youtube_live_https -v
python3 -m unittest test.test_youtube_misc -v
python3 -m unittest test.test_youtube_lists -v
```

If all green, run a live smoke test (see above). If the smoke test captures 30+ seconds of playable video, the merge is clean.

### Signals of breakage (troubleshooting)

| Symptom | Most likely cause |
|---------|-------------------|
| `AttributeError: 'YoutubeIE' object has no attribute '_initial_extract'` or `_list_formats` | Upstream renamed or removed one of these. Update the `refetch_url` closure in `_prepare_live_https_formats`. |
| `TypeError: _initial_extract() takes N positional arguments but M were given` | Upstream changed the signature. Update the call. |
| `ValueError: not enough values to unpack` | Upstream changed the tuple shape of `_initial_extract` or `_list_formats`. Update the unpacking. |
| Unit tests green, smoke test fetches one fragment then stops | Call site wasn't reached. Check that the `_prepare_live_https_formats` call still lives in `_real_extract` at the correct insertion point. |
| Unit tests green, smoke test stuck at `frag 0` forever | `ctx['last_error']` contract changed, or the `http_dash_segments_generator` protocol was renamed. Check downloader registry. |
| Smoke test ends after ~5 seconds with "Live stream ended, finalizing output" but stream is still live | `refetch_url` closure is misfiring. Add `self.write_debug` statements to see which branch is hit. |
| Smoke test ends with "Error budget exhausted" early | Either real consecutive 403s (rare) or the success-decrement path is broken. Check that `error_score = max(0, error_score - 1)` still runs on successful yield. |

### What NOT to merge from upstream

Nothing. The fork does not patch upstream behaviour, only extends it. Plain `git merge upstream/master` is correct. There is no patch to reapply.

---

## Why no upstream PR

Recorded here so it is not re-litigated every time the question comes up.

`yt-dlp/yt-dlp`'s [`CONTRIBUTING.md`](https://github.com/yt-dlp/yt-dlp/blob/master/CONTRIBUTING.md) states:

> Please refrain from submitting issues or pull requests that have been generated by an LLM or other fully-automated tools. Any submission that is in violation of this policy will be closed, and the submitter may be blocked from this repository without warning.

> Additionally, AI-generated code conflicts with this project's license (Unlicense), since you cannot truly release code into the public domain if you didn't author it yourself.

Every Phase A code commit on this branch carries a `Co-Authored-By: Claude ...` trailer, which is an explicit self-declaration of AI assistance. Submitting as-is would violate the policy on the face of the commit metadata and risk a repository block.

### Legitimate paths to upstream (if ever desired)

1. **Clean-room rewrite.** Open an upstream feature-request issue first to get maintainer buy-in. Then reimplement the feature from scratch without AI assistance, able to defend every line in review. Strip AI trailers. Address Phase B items.
2. **Third-party plugin.** yt-dlp has a plugin system (`yt_dlp_plugins/`) designed for extensions that do not need to land in core. No policy conflict, no upstream review gate.

Neither is planned as of the current fork state. This feature exists for the fork author's use.

---

## Links

- Design doc: [`docs/superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md`](superpowers/specs/2026-04-21-youtube-live-https-downloader-design.md)
- Implementation plan (task-by-task, all 27 steps ticked): [`docs/superpowers/plans/2026-04-21-youtube-live-https-downloader.md`](superpowers/plans/2026-04-21-youtube-live-https-downloader.md)
- Tracking issue: [static74/yt-dlp#2](https://github.com/static74/yt-dlp/issues/2)
- Upstream `CONTRIBUTING.md`: [yt-dlp/yt-dlp/blob/master/CONTRIBUTING.md](https://github.com/yt-dlp/yt-dlp/blob/master/CONTRIBUTING.md)
