#!/usr/bin/env python3
"""The dynamic sitemap lists EVERY LIVE listing, at the per-listing path.
NO NETWORK, NO DB.

2026-09-16. routes/sitemap_auto._generate_sitemap's listings block emitted the
listings index with the slug in a query string, and filtered `status =
'public'`. Both were wrong at once, and the second hid the first:

  * Both live listings are `pocket`, so the WHERE clause matched nothing and
    the block published ZERO listing URLs. A block that emits nothing cannot be
    caught by looking at what it emits — which is why the fake cursor below
    APPLIES the WHERE clause it is handed instead of ignoring it.
  * `dchub.cloud/listings/<slug>` is served: dchub-frontend#1491 (live worker
    5.0.0) renders a crawlable teaser page per listing — 200 with
    `x-robots-tag: index, follow` for a live slug, a real not-found with
    `noindex` for an unknown one.

The liveness rule is not retyped here or in the generator. Both import
routes.exclusive_listings._LIVE_WHERE, the same predicate the /listings feed,
its counts and GET /api/v1/listings/summary filter on, so the sitemap lists
exactly the listings whose teaser cards are public — and a listing that stops
being live leaves every one of those surfaces on the same build.

Run:  python3 -m pytest tests/test_sitemap_lists_live_listings.py -rEf
"""
import datetime as _dt
import importlib.util
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "routes" / "sitemap_auto.py"
LISTING_LOC = re.compile(r"<loc>https://dchub\.cloud/listings/([^<]+)</loc>")
LOC_WITH_LASTMOD = re.compile(
    r"<loc>(https://dchub\.cloud/listings/[^<]+)</loc><lastmod>([^<]+)</lastmod>")

NOW = _dt.datetime(2026, 9, 16, 1, 39, tzinfo=_dt.timezone.utc)

# slug, status, expires_at, updated_at. Two live, one withdrawn, one expired.
LISTINGS = [
    ("dfw-40", "pocket", None, _dt.datetime(2026, 9, 2, tzinfo=_dt.timezone.utc)),
    ("phx-60", "public", None, _dt.datetime(2026, 9, 11, tzinfo=_dt.timezone.utc)),
    ("atl-20", "withdrawn", None, _dt.datetime(2026, 9, 5, tzinfo=_dt.timezone.utc)),
    ("ord-80", "pocket", _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc),
     _dt.datetime(2026, 9, 9, tzinfo=_dt.timezone.utc)),
]
LIVE = {"dfw-40", "phx-60"}
NOT_LIVE = {"atl-20": "withdrawn", "ord-80": "past its expires_at"}

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
    given. A cursor that returned every fixture row regardless would pass this
    file with the `status = 'public'` bug still in place."""

    def __init__(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())

        def reads(table):
            return re.search(rf"\bFROM\s+{table}\b", s, re.I) is not None

        counts = re.search(r"\bCOUNT\s*\(", s, re.I) is not None
        if not reads("exclusive_listings"):
            # Every other block (markets, news) is out of scope here, but
            # /api/v1/sitemap/health runs their counts in the same `try`, and a
            # cursor that raised on them would take the listings count down
            # with it and make the health assertion below unmeasurable.
            self._rows = [(0,)] if counts else []
            return
        statuses = _statuses_asked_for(s)
        checks_expiry = _EXPIRY_RE.search(s) is not None
        kept = []
        for slug, status, expires_at, updated_at in LISTINGS:
            if statuses is not None and status not in statuses:
                continue
            if checks_expiry and expires_at is not None and expires_at <= NOW:
                continue
            kept.append((slug, updated_at))
        self._rows = [(len(kept),)] if counts else kept

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, *_a, **_k):
        return _Cur()

    def close(self):
        pass


def _load():
    spec = importlib.util.spec_from_file_location("_sitemap_live_listings_probe", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_generate_sitemap"), "loaded something that is not sitemap_auto"
    return mod


@pytest.fixture
def xml(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_conn", lambda: _Conn())
    return mod._generate_sitemap()


def test_every_live_listing_is_listed_at_its_own_path(xml):
    listed = set(LISTING_LOC.findall(xml))
    assert listed == LIVE, (
        f"the sitemap lists {sorted(listed)}; the live listings are "
        f"{sorted(LIVE)}. Both `pocket` and `public` are live — filtering "
        f"`status = 'public'` here is what published zero listing URLs.")


@pytest.mark.parametrize("slug,why", sorted(NOT_LIVE.items()))
def test_a_listing_that_stopped_being_live_drops_out_on_the_next_build(xml, slug, why):
    """The sitemap is rebuilt per request and cached for an hour, so 'the next
    build' is the next cache miss. A URL that outlives its listing is a
    submitted URL for a page the teaser renderer will answer not-found for."""
    assert f"<loc>https://dchub.cloud/listings/{slug}</loc>" not in xml, (
        f"{slug} is {why} but still in the sitemap")


def test_lastmod_is_each_listings_own_updated_at(xml):
    """One shared lastmod (build date, or the newest listing's) would tell a
    crawler every listing changed whenever any of them did."""
    found = dict(LOC_WITH_LASTMOD.findall(xml))
    expected = {f"https://dchub.cloud/listings/{slug}": updated.strftime("%Y-%m-%d")
                for slug, status, expires_at, updated in LISTINGS if slug in LIVE}
    got = {loc: stamp for loc, stamp in found.items() if loc in expected}
    assert got == expected, f"lastmod mismatch: {got} != {expected}"
    assert len(set(expected.values())) > 1, (
        "the fixture gives both live listings the same updated_at, so this "
        "test would pass on a single shared lastmod")


def test_no_listing_url_carries_the_slug_in_a_query_string(xml):
    """The retired shape. It pointed every listing at the same index page, so
    the per-listing teaser pages were unreachable from the sitemap and the
    index self-canonicalised over all of them."""
    assert "/listings?l=" not in xml


def test_the_module_builds_no_query_string_listing_url_anywhere():
    """Source-level, because the block sits under `except Exception: pass` with
    `_safe` swallowing inside it: a revived query-string emitter that reads a
    column the live table lacks emits nothing, and no behavioural test above
    can see it."""
    assert "/listings?l=" not in SOURCE.read_text(encoding="utf-8")


def test_the_generator_and_the_listings_feed_share_one_liveness_rule():
    """★ ONE PREDICATE, TWO CONSUMERS. A copy of the rule in this module would
    be free to drift from the feed's, and the sitemap would then advertise a
    set of listings /listings does not show."""
    from routes.exclusive_listings import _LIVE_WHERE
    mod = _load()
    joined = " AND ".join(_LIVE_WHERE)
    assert joined in mod._LIVE_LISTINGS_SQL, (
        f"the sitemap's listing SQL does not carry the feed's own rule "
        f"({joined!r}): {mod._LIVE_LISTINGS_SQL!r}")
    assert joined in mod._LIVE_LISTINGS_COUNT_SQL, (
        "GET /api/v1/sitemap/health counts a different population than the "
        "sitemap lists")


def test_the_health_count_matches_what_the_generator_emits(monkeypatch):
    """The dead-field lesson (facilities_with_power, #4436): a count on
    /api/v1/sitemap/health that does not map to a block above it describes
    nothing this generator publishes."""
    pytest.importorskip("flask")
    from flask import Flask
    mod = _load()
    monkeypatch.setattr(mod, "_conn", lambda: _Conn())
    app = Flask(__name__)
    app.register_blueprint(mod.sitemap_auto_bp)
    body = app.test_client().get("/api/v1/sitemap/health").get_json()
    assert "public_listings" not in body, (
        "public_listings is back. It counted `status = 'public'`, which is not "
        "the population the listings block lists.")
    assert body["live_listings"] == len(LIVE) == len(
        set(LISTING_LOC.findall(mod._generate_sitemap())))
