"""util/feed_fetch.py — the ONE bounded way RSS/Atom bytes enter this repo.

WHY THIS EXISTS
---------------
`feedparser.parse(url)` does its own network I/O and feedparser 6.0.12 accepts
no `timeout=`. There is nothing to bound it with, so one dead host stalls the
caller for as long as the kernel will hold the socket.

Measured in production 2026-08-26 on `worker:deals` (PR #3208): a single dead
feed URL consumed **696.6s of a 712.9s run**, and a catch-up run on unchanged
code the same morning burned **1771.6s** on the same feed before the scheduler's
1800s hard-timeout killed the thread. The stall is not a fixed cost — it is
unbounded, and one feed can eat an entire scheduler budget.

#3208 fixed exactly one of the FOURTEEN call sites in this repo, in
`crawler_scheduler.py`. This module is that fix made shared, for the other
thirteen — two of which were missing from the census #3208 published, including
`routes/jobs_routes.py`, which is a live HTTP handler carrying a second copy of
the whole defect.

THE CONSTRAINTS THAT SHAPE THE CODE
-----------------------------------
★ NOT `socket.setdefaulttimeout()`. It is process-global, and the worker runs
  the brain, the watchdog and the scheduler in sibling threads. Bounding a feed
  fetch by that route also bounds every unrelated socket in the process.

★ The timeout MUST be a `(connect, read)` TUPLE. `requests` applies a scalar
  `timeout=N` to connect AND read alike, so a dead host burns the entire read
  budget just failing to open a socket. The tuple keeps *reaching* a dead host
  cheap and leaves the long budget for a live host's body transfer.

★★ AND a wall-clock deadline on top of both, covering TWO holes the tuple
  leaves open:

    - `requests`' read timeout is BETWEEN BYTES, not total. A host that dribbles
      one byte every 29s under a 30s read budget never times out at all.
    - `timeout=` is re-applied to EVERY redirect hop, and requests follows up to
      30 by default, so a chain of slow redirects multiplies the budget by 30.

  `FEED_TOTAL_TIMEOUT_SECONDS` covers both from ONE deadline: a response hook
  checks it per hop, `_read_body()` checks it per chunk, and FEED_MAX_REDIRECTS
  caps the chain at 5 regardless. Without this the module would still be
  unbounded, just less obviously.

★★★ WHY `parse_feed()` PASSES `response_headers`, AND WHY BOTH KEYS MATTER
--------------------------------------------------------------------------
Handing feedparser BYTES instead of a URL silently drops two things it would
otherwise have taken from the HTTP response: the base URI for resolving
relative links, and the charset. Measured against feedparser 6.0.12 with a feed
whose XML declaration carries no `encoding=` and whose body is UTF-8:

    response_headers=          encoding      title      <link>/rel/path
    ─────────────────────────────────────────────────────────────────────
    (omitted)                  utf-8         'café'     '/rel/path'    ← unresolved
    content-location only      iso-8859-1    'cafÃ©'    resolved       ← MOJIBAKE
    content-location + type    utf-8         'café'     resolved       ← correct

The middle row is the trap. Supplying the URL *alone* to recover relative-link
resolution makes feedparser apply the HTTP/1.1 `text/*` default charset of
iso-8859-1, which mis-decodes every UTF-8 feed in the repo and sets `bozo`.
The two keys are not independent: pass both or pass neither.

WHY THE FETCH RAISES
--------------------
`feedparser.parse(url)` swallows transport failures and returns an empty feed.
That is how `feeds.reuters.com`, which does not resolve at all, read as
"0 entries" for months instead of as a dead source (#3208). Every function here
raises on a transport error or a non-2xx, so a broken feed is LOUD. All thirteen
call sites already sit inside a `try`/`except` that logs and continues, so this
converts a silent zero into a logged failure without changing control flow.

requests, not urllib (regression_lint urllib-request-on-railway).
"""
from __future__ import annotations

import logging
import os
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ── Budgets ─────────────────────────────────────────────────────────────────
# Defaults match crawler_scheduler.FEED_{CONNECT,READ}_TIMEOUT_SECONDS (#3208).
_DEFAULT_CONNECT = 5.0      # TCP+TLS handshake only
_DEFAULT_READ = 30.0        # between-bytes gap on the body
_DEFAULT_TOTAL = 60.0       # wall clock for the whole body, drip-proof
_DEFAULT_MAX_BYTES = 16 * 1024 * 1024

# ★ requests re-applies `timeout=` to EVERY redirect hop and its default cap is
#   30, so a chain of slow redirects costs up to 30x the budget — the tuple does
#   NOT bound the chain. Five is more than any real feed needs; the deepest
#   chain among the 77 configured URLs is two.
FEED_MAX_REDIRECTS = 5

# ★ Ceilings, not suggestions. The env overrides exist so an incident can be
#   mitigated without a deploy — they must not be able to re-open the hole this
#   module closes, so every one of them is clamped. An operator who sets
#   DCHUB_FEED_READ_TIMEOUT=9999 gets the ceiling, not 9999.
_MAX_CONNECT = 30.0
_MAX_READ = 120.0
_MAX_TOTAL = 300.0
_MAX_MAX_BYTES = 64 * 1024 * 1024


def _budget(env_name, default, ceiling):
    """Read a positive float from the environment, clamped to `ceiling`."""
    raw = os.environ.get(env_name, '').strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %s", env_name, raw, default)
        return default
    if val <= 0:
        logger.warning("%s=%r is not positive — using %s", env_name, raw, default)
        return default
    if val > ceiling:
        logger.warning("%s=%s exceeds the %s ceiling — clamped", env_name, val, ceiling)
        return ceiling
    return val


FEED_CONNECT_TIMEOUT_SECONDS = _budget('DCHUB_FEED_CONNECT_TIMEOUT', _DEFAULT_CONNECT, _MAX_CONNECT)
FEED_READ_TIMEOUT_SECONDS = _budget('DCHUB_FEED_READ_TIMEOUT', _DEFAULT_READ, _MAX_READ)
FEED_TOTAL_TIMEOUT_SECONDS = _budget('DCHUB_FEED_TOTAL_TIMEOUT', _DEFAULT_TOTAL, _MAX_TOTAL)
FEED_MAX_BYTES = int(_budget('DCHUB_FEED_MAX_BYTES', _DEFAULT_MAX_BYTES, _MAX_MAX_BYTES))

# The UA #3208 settled on. It replaces feedparser's own default
# ("feedparser/6.0.12 +https://github.com/kurtmckee/feedparser/") at the ten call
# sites that never set one. Verified regression-free: all 77 configured feed URLs
# were probed both ways on 2026-08-26 and every one of the 44 that produce entries
# returned the SAME entry count under this UA.
DEFAULT_FEED_AGENT = 'Mozilla/5.0 (compatible; DCHub/3.0; +https://dchub.cloud)'
DEFAULT_FEED_ACCEPT = 'application/rss+xml, application/xml, text/xml, */*'


class FeedTooLarge(Exception):
    """Body exceeded FEED_MAX_BYTES. A feed does not legitimately do this."""


class FeedReadTimeout(Exception):
    """Body was still arriving after FEED_TOTAL_TIMEOUT_SECONDS.

    Distinct from requests' ReadTimeout, which only fires on a between-bytes
    GAP. This is the drip case: bytes keep coming, just never enough of them.
    """


def _read_body(resp, deadline, max_bytes):
    """Stream `resp` under a wall-clock deadline and a size cap.

    The cap counts DECOMPRESSED bytes — `iter_content` undoes Content-Encoding
    — so it is also what stops a gzip bomb from a compromised publisher.
    """
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        if time.monotonic() > deadline:
            raise FeedReadTimeout(
                f"feed body still arriving after {FEED_TOTAL_TIMEOUT_SECONDS}s "
                f"({size} bytes so far)"
            )
        size += len(chunk)
        if size > max_bytes:
            raise FeedTooLarge(f"feed body exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b''.join(chunks)


class FeedResponse(NamedTuple):
    """What a bounded fetch yielded. `url` is FINAL, i.e. after redirects."""
    url: str
    body: bytes
    content_type: str
    status_code: int


def _deadline_guard(deadline):
    """A `requests` response hook, fired once per REDIRECT HOP.

    ★ This is what makes FEED_TOTAL_TIMEOUT_SECONDS a total. Without it the
      budget covered only the body: `timeout=` is re-applied to every hop, so a
      redirect chain multiplies it. Measured against a 12-hop server — the hook
      fires on each 302, and raising from one aborts the chain (4 hops, 1.2s).
    """
    def hook(resp, *args, **kwargs):
        if time.monotonic() > deadline:
            raise FeedReadTimeout(
                f"feed redirect chain still running after "
                f"{FEED_TOTAL_TIMEOUT_SECONDS}s (at {resp.url})"
            )
        return resp
    return hook


def fetch_feed_response(url, agent=None, request_headers=None):
    """GET one feed under the full budget. Returns a `FeedResponse`.

    Raises on any transport error or non-2xx — see "WHY THE FETCH RAISES".

    Worst case is FEED_TOTAL_TIMEOUT_SECONDS plus one connect+read, because the
    deadline is checked BETWEEN hops rather than interrupting one: 60 + 35 = 95s
    at the defaults, against the 1800s a scheduler slot has.
    """
    import requests

    headers = {
        'User-Agent': agent or DEFAULT_FEED_AGENT,
        'Accept': DEFAULT_FEED_ACCEPT,
    }
    if request_headers:
        headers.update(request_headers)

    deadline = time.monotonic() + FEED_TOTAL_TIMEOUT_SECONDS
    session = requests.Session()
    session.max_redirects = FEED_MAX_REDIRECTS
    try:
        resp = session.get(
            url,
            timeout=(FEED_CONNECT_TIMEOUT_SECONDS, FEED_READ_TIMEOUT_SECONDS),
            headers=headers,
            stream=True,
            hooks={'response': _deadline_guard(deadline)},
        )
        try:
            resp.raise_for_status()
            # ★ The SAME deadline the hop guard used, so redirects and body
            #   share one budget instead of each getting a fresh one.
            body = _read_body(resp, deadline, FEED_MAX_BYTES)
        finally:
            resp.close()
    finally:
        session.close()
    return FeedResponse(
        url=resp.url,
        body=body,
        content_type=resp.headers.get('content-type', 'application/rss+xml'),
        status_code=resp.status_code,
    )


def fetch_feed_bytes(url, agent=None, request_headers=None):
    """The feed body, bounded. Raises rather than returning empty bytes."""
    return fetch_feed_response(url, agent=agent, request_headers=request_headers).body


def parse_feed(url, agent=None, request_headers=None):
    """Bounded drop-in for `feedparser.parse(url)`.

    ★ Differs from `feedparser.parse(url)` in exactly one way that callers must
      know about: it RAISES on a dead host instead of returning an empty feed.
      Every call site in this repo already handles that; see the module
      docstring.

    `response_headers` carries BOTH content-type and content-location. Passing
    one without the other mis-decodes UTF-8 feeds — measured, see above.
    """
    import feedparser

    resp = fetch_feed_response(url, agent=agent, request_headers=request_headers)
    return feedparser.parse(
        resp.body,
        response_headers={
            # Final URL AFTER redirects — the base URI feedparser.parse(url)
            # would itself have resolved relative links against.
            'content-location': resp.url,
            'content-type': resp.content_type,
        },
    )
