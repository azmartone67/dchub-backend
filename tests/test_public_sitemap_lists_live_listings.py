#!/usr/bin/env python3
"""The PUBLIC sitemap lists every LIVE listing at its own path. NO NETWORK, NO DB.

2026-09-16. Listing URLs were added to routes/sitemap_auto.py, which registers
`/api/v1/sitemap.xml` and `/api/v1/sitemap/health` and nothing else. That
artefact carried them. The sitemap search engines actually fetch —
https://dchub.cloud/sitemap.xml, a <sitemapindex> over the shards this builder
produces, submitted to GSC/Bing and named in robots.txt — carried ZERO listing
URLs, because it is built by main.py's `_build_sitemap_sections` instead.
Measured that day:

    https://dchub.cloud/api/v1/sitemap.xml   381 urls, 2 of them /listings/<slug>
    https://dchub.cloud/sitemap.xml          7 children, 0 listing URLs anywhere

contracts/route_serving_map.json attributes `GET /sitemap.xml` and `GET
/sitemap-<section>.xml` to `main` alone, and `GET /api/v1/sitemap.xml` to
`routes.sitemap_auto` — several modules mention these paths, one serves each.

★ THE STUB CURSOR APPLIES THE WHERE CLAUSE IT IS HANDED. A cursor that returned
  every fixture row regardless would pass this file with `status = 'public'`
  retyped in the builder — which is the exact bug that published zero listing
  URLs from the other generator while both live listings were `pocket`.

★ AND THE SHARD IS REQUIRED TO BE SERVED. A sitemap child that is listed in the
  index and 404s is worse than no child at all (/sitemap-listings.xml 404s
  today), so the last test here binds the shard the listings land in to both
  _SITEMAP_FIXED_SECTIONS (which serve_sitemap_shard answers from) and
  _sitemap_shard_files (which the index is rendered from).

Run:  python3 -m pytest tests/test_public_sitemap_lists_live_listings.py -rEf
"""
import ast
import datetime as _dt
import os
import re
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "main.py")

LISTING_LOC = re.compile(r"<loc>https://dchub\.cloud/listings/([^<]+)</loc>")
LOC_WITH_LASTMOD = re.compile(
    r"<loc>(https://dchub\.cloud/listings/[^<]+)</loc><lastmod>([^<]+)</lastmod>")

NOW = _dt.datetime(2026, 9, 16, 1, 39, tzinfo=_dt.timezone.utc)

# slug, status, expires_at, updated_at. Three live, one withdrawn, one expired.
#
# `ams-10&beta` is deliberate: a raw `&` in a <loc> is the single commonest way
# a sitemap comes back 200 and unparseable. Without it the well-formedness test
# below would pass on a builder that dropped the percent-encoding entirely.
LISTINGS = [
    ("dfw-40", "pocket", None, _dt.datetime(2026, 9, 2, tzinfo=_dt.timezone.utc)),
    ("phx-60", "public", None, _dt.datetime(2026, 9, 11, tzinfo=_dt.timezone.utc)),
    ("ams-10&beta", "public", None, _dt.datetime(2026, 9, 13, tzinfo=_dt.timezone.utc)),
    ("atl-20", "withdrawn", None, _dt.datetime(2026, 9, 5, tzinfo=_dt.timezone.utc)),
    ("ord-80", "pocket", _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc),
     _dt.datetime(2026, 9, 9, tzinfo=_dt.timezone.utc)),
]
LIVE = {"dfw-40", "phx-60", "ams-10&beta"}
NOT_LIVE = {"atl-20": "withdrawn", "ord-80": "past its expires_at"}

# What each live slug must appear as in a <loc>, spelled out rather than
# computed with the same call the builder uses — a test that re-runs the
# builder's own encoder cannot notice the encoder going away.
EMITTED = {"dfw-40": "dfw-40", "phx-60": "phx-60", "ams-10&beta": "ams-10%26beta"}

_STATUS_IN_RE = re.compile(r"status\s+IN\s*\(([^)]*)\)", re.I)
_STATUS_EQ_RE = re.compile(r"status\s*=\s*'([^']*)'", re.I)
_EXPIRY_RE = re.compile(r"expires_at\s+IS\s+NULL\s+OR\s+expires_at\s*>\s*NOW\(\)", re.I)


def _statuses_asked_for(sql):
    """The statuses this SQL admits, or None when it does not filter on status
    at all. Read out of the statement so NARROWING the filter (back to
    'public') and DROPPING it both change what the fake table returns."""
    m = _STATUS_IN_RE.search(sql)
    if m:
        return {v.strip().strip("'") for v in m.group(1).split(",") if v.strip()}
    m = _STATUS_EQ_RE.search(sql)
    return {m.group(1)} if m else None


class _Cur:
    """A stand-in `exclusive_listings` that HONOURS the WHERE clause it is
    given. `_build_sitemap_sections` issues a dozen other SELECTs; they answer
    [] so the rest of the artefact is empty and the listing URLs are the whole
    thing under test."""

    def __init__(self):
        self._rows = []
        self.seen = []

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.seen.append(s)
        if not re.search(r"\bFROM\s+exclusive_listings\b", s, re.I):
            self._rows = []
            return self
        statuses = _statuses_asked_for(s)
        checks_expiry = _EXPIRY_RE.search(s) is not None
        kept = []
        for slug, status, expires_at, updated_at in LISTINGS:
            if statuses is not None and status not in statuses:
                continue
            if checks_expiry and expires_at is not None and expires_at <= NOW:
                continue
            kept.append((slug, updated_at))
        self._rows = kept
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Log:
    """Records what the builder logged, so the observability claim is testable
    rather than asserted in a comment."""

    def __init__(self):
        self.lines = []

    def _rec(self, level):
        def _f(msg, *a, **k):
            try:
                self.lines.append(f"{level}:{msg % a if a else msg}")
            except Exception:
                self.lines.append(f"{level}:{msg}")
        return _f

    def __getattr__(self, name):
        return self._rec(name)


def _builder_source():
    src = open(SRC, encoding="utf-8").read()
    i = src.index("def _build_sitemap_sections(")
    j = src.index("\ndef ", i + 100)
    fn = src[i:j]
    ast.parse(fn)                       # the source we run must be the source
    return fn


def _run_builder(cur, log):
    """Execute the SHIPPED `_build_sitemap_sections` against `cur`.

    Same extraction as tests/test_sitemap_no_duplicate_selfcanon.py. The thin
    gate is disabled because the fixtures carry no power_mw; it is irrelevant
    to the listings block, which is not capacity-gated."""
    ns = {
        "os": os,
        "logger": log,
        "get_read_db": lambda: _Conn(cur),
        "_build_sitemap_facilities_ungated": lambda: [],
        "_SITEMAP_THIN_GATE_FLOOR": 0,
        "_SITEMAP_PROVEN_MIN_IMPRESSIONS": 10,
        "_SITEMAP_PROVEN_CAP": 9000,
        "_POCKET_SITEMAP_CEILING": 0,
        "refresh_suppressed_slugs": lambda *a, **k: None,
        "__name__": "main_stub",
    }
    exec(compile(_builder_source(), SRC, "exec"), ns)
    prev = os.environ.get("SITEMAP_THIN_GATE_DISABLE")
    os.environ["SITEMAP_THIN_GATE_DISABLE"] = "1"
    try:
        return ns["_build_sitemap_sections"]()
    finally:
        if prev is None:
            os.environ.pop("SITEMAP_THIN_GATE_DISABLE", None)
        else:
            os.environ["SITEMAP_THIN_GATE_DISABLE"] = prev


@pytest.fixture
def built():
    log = _Log()
    sections = _run_builder(_Cur(), log)
    return sections, log


@pytest.fixture
def xml(built):
    """Every rendered <url> the builder produced, across every section, so a
    test cannot pass by finding the URL in a shard nobody serves."""
    sections, _ = built
    return "\n".join("\n".join(v or []) for v in sections.values())


def test_every_live_listing_is_listed_at_its_own_path(xml):
    listed = set(LISTING_LOC.findall(xml))
    want = {EMITTED[s] for s in LIVE}
    assert listed == want, (
        f"the public sitemap lists {sorted(listed)}; the live listings are "
        f"{sorted(want)}. Both `pocket` and `public` are live — filtering "
        f"`status = 'public'` is what published zero listing URLs from the "
        f"other generator.")


@pytest.mark.parametrize("slug,why", sorted(NOT_LIVE.items()))
def test_a_listing_that_stopped_being_live_drops_out_on_the_next_build(xml, slug, why):
    """The sections dict is memoised for an hour and snapshotted by cron, so
    'the next build' is the next cache miss. A URL that outlives its listing is
    a submitted URL for a page the teaser renderer answers not-found for."""
    assert f"<loc>https://dchub.cloud/listings/{slug}</loc>" not in xml, (
        f"{slug} is {why} but still in the public sitemap")


def test_lastmod_is_each_listings_own_updated_at(xml):
    """The pinned _STATIC_LASTMOD is reserved for hardcoded/curated URLs by
    this builder's own lastmod-honesty note. One shared lastmod would tell a
    crawler every listing changed whenever any of them did."""
    found = dict(LOC_WITH_LASTMOD.findall(xml))
    expected = {f"https://dchub.cloud/listings/{EMITTED[slug]}":
                updated.strftime("%Y-%m-%d")
                for slug, status, expires_at, updated in LISTINGS if slug in LIVE}
    got = {loc: stamp for loc, stamp in found.items() if loc in expected}
    assert got == expected, f"lastmod mismatch: {got} != {expected}"
    assert len(set(expected.values())) > 1, (
        "the fixture gives both live listings the same updated_at, so this "
        "test would pass on a single shared lastmod")


def test_the_shard_the_listings_land_in_is_rendered_as_well_formed_xml(built):
    """Render the shard exactly as serve_sitemap_shard does and parse it. An
    unescaped slug or a stray f-string would produce a 200 that every crawler
    rejects."""
    sections, _ = built
    shard = _shard_holding_listings(sections)
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + '\n'.join(sections[shard]) + '\n</urlset>')
    root = ET.fromstring(body)          # raises on malformed XML
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall(".//s:url/s:loc", ns)]
    listing_locs = [u for u in locs if "/listings/" in u]
    assert sorted(listing_locs) == sorted(
        f"https://dchub.cloud/listings/{EMITTED[s]}" for s in LIVE), listing_locs


def _shard_holding_listings(sections):
    holding = [k for k, v in sections.items()
               if any("/listings/" in e for e in (v or []))]
    assert len(holding) == 1, (
        f"the listing URLs are in {holding}; they must live in exactly one "
        f"shard or the index will advertise the same URL twice")
    return holding[0]


def test_the_listings_shard_is_both_served_and_listed_in_the_index(built):
    """★ A child of /sitemap.xml that 404s is worse than no child: GSC reports
    the whole index as a fetch error. /sitemap-listings.xml 404s today, which
    is exactly this failure. Whatever shard the listings land in must be one
    serve_sitemap_shard answers AND one _sitemap_shard_files names."""
    sections, _ = built
    shard = _shard_holding_listings(sections)
    src = open(SRC, encoding="utf-8").read()

    m = re.search(r"^_SITEMAP_FIXED_SECTIONS\s*=\s*\(([^)]*)\)", src, re.M)
    assert m, "_SITEMAP_FIXED_SECTIONS is gone from main.py"
    fixed = tuple(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
    assert shard in fixed, (
        f"the listings went into shard {shard!r}, which is not in "
        f"_SITEMAP_FIXED_SECTIONS {fixed}. serve_sitemap_shard answers "
        f"`section in _SITEMAP_FIXED_SECTIONS` — /sitemap-{shard}.xml would "
        f"404 while the index advertised it.")

    ns = {"_SITEMAP_FIXED_SECTIONS": fixed, "_SITEMAP_FACILITIES_PER_SHARD": 10000}
    i = src.index("def _sitemap_shard_files(")
    j = src.index("\ndef ", i + 10)
    exec(compile(src[i:j], SRC, "exec"), ns)
    files = ns["_sitemap_shard_files"](sections)
    assert f"sitemap-{shard}.xml" in files, (
        f"sitemap-{shard}.xml is served but the index never names it, so no "
        f"crawler reaches the listing URLs: {files}")


def test_the_builder_and_the_listings_feed_share_one_liveness_rule():
    """★ ONE PREDICATE, TWO CONSUMERS. A copy of the rule in main.py would be
    free to drift from the feed's, and the sitemap would then advertise a set
    of listings /listings does not show. Source-level as well as behavioural:
    the block sits in a try/except, so a builder that lost the import and fell
    back to a retyped literal would log a warning and emit nothing, and the
    behavioural tests above would fail without naming the cause."""
    from routes.exclusive_listings import _LIVE_WHERE
    fn = _builder_source()
    assert "from routes.exclusive_listings import _LIVE_WHERE" in fn, (
        "the public sitemap builder no longer imports the feed's liveness "
        "rule — a retyped copy is how the two lists drift apart")
    joined = " AND ".join(_LIVE_WHERE)
    for predicate in _LIVE_WHERE:
        assert predicate not in fn.replace(
            "from routes.exclusive_listings import _LIVE_WHERE", ""), (
            f"the builder retypes {predicate!r} instead of joining the "
            f"imported rule ({joined!r})")
    assert '" AND ".join(_LISTING_LIVE_WHERE)' in fn, (
        "the imported rule is not what the listings SQL is built from")


def test_a_failed_listings_fetch_is_logged_and_not_swallowed():
    """★ NOT `except Exception: pass`. The other generator's copy of this block
    sits inside a bare swallow, which is why it emitted nothing for weeks in
    silence. This one must name its error."""
    class _Boom(_Cur):
        def execute(self, sql, params=None):
            if re.search(r"\bFROM\s+exclusive_listings\b", str(sql), re.I):
                raise RuntimeError("relation does not exist")
            return super().execute(sql, params)

    log = _Log()
    sections = _run_builder(_Boom(), log)
    assert not any("/listings/" in e for e in (sections.get("static") or []))
    warned = [l for l in log.lines
              if l.startswith("warning:") and "live-listings" in l]
    assert warned, (
        "the listings fetch blew up and the build logged nothing about it:\n"
        + "\n".join(log.lines[-15:]))
    assert "relation does not exist" in warned[0], (
        f"the warning does not carry the error: {warned[0]!r}")


def test_the_count_emitted_is_logged_every_build(built):
    """Zero live listings is a normal business state, and it must be
    distinguishable in the logs from a block that never ran. See
    [[feedback_scan_that_can_find_nothing_needs_a_floor]] — the log line IS the
    floor here, because a real floor would fail CI on a quiet week."""
    _, log = built
    line = [l for l in log.lines if "live listing pages added" in l]
    assert line, ("the build never reported how many listing URLs it emitted:\n"
                  + "\n".join(log.lines[-15:]))
    assert f"{len(LIVE)} live listing pages added" in line[0], line[0]


def test_hitting_the_row_cap_is_announced_rather_than_truncating_in_silence():
    """★ The block asks for at most N live listings. Two are live today, so the
    cap is slack — but a program that grows past it would publish a PREFIX of
    the catalogue and look exactly like a healthy build. The warning is the
    only thing that tells the difference."""
    src = _builder_source()
    m = re.search(r"_listings_cap\s*=\s*(\d+)", src)
    assert m, "the listings query no longer has a named cap"
    cap = int(m.group(1))

    class _Many(_Cur):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if re.search(r"\bFROM\s+exclusive_listings\b", str(sql), re.I):
                self._rows = [(f"listing-{i}", NOW) for i in range(cap)]
            return self

    log = _Log()
    sections = _run_builder(_Many(), log)
    assert sum(1 for e in sections["static"] if "/listings/" in e) == cap
    hit = [l for l in log.lines if l.startswith("warning:") and "cap" in l]
    assert hit, ("the build published a truncated catalogue and said nothing:\n"
                 + "\n".join(log.lines[-15:]))

    log2 = _Log()
    _run_builder(_Cur(), log2)
    assert not [l for l in log2.lines if l.startswith("warning:") and "cap" in l], (
        "the cap warning fires on an ordinary build too, so it means nothing")


def test_no_listing_url_carries_the_slug_in_a_query_string(xml):
    """The retired shape. It pointed every listing at the same index page, so
    the per-listing teaser pages were unreachable from the sitemap and the
    index self-canonicalised over all of them. robots.txt also carries
    `Disallow: /*?`, so those URLs were crawl-blocked on top."""
    assert "/listings?l=" not in xml


def test_the_builder_builds_no_query_string_listing_url_anywhere():
    """Source-level, because the block is in a try/except: a revived
    query-string emitter that reads a column the live table lacks emits
    nothing, and no behavioural test above can see it."""
    assert "/listings?l=" not in _builder_source()
