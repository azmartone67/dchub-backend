"""Capacity Source live availability: GET /api/v1/listings/summary, and the
llms.txt Capacity Source block that reads the same cached summary.

What these pin:
  * the summary counts exactly the listings the teaser feed shows (drafts and
    expired listings stay out) and names no listing's title, slug, provider,
    price or contact;
  * markets rank by listing count, then MW, at most 50; delivery types count
    only values the listing-field rules accept;
  * the table is read at most once a minute per process, and once for any
    number of concurrent callers; an unreadable table answers ok:false;
  * /api/v1/listings/summary is served by its own view, not the listing
    route, and "summary" can never become a listing slug;
  * llms.txt's Capacity Source block is its source text, byte for byte, until
    a listing is live. Then it reads live: no "(upcoming)" in the heading, the
    program sentence in live wording, and one availability line.

Storage is a stand-in for Postgres behind the module's own _fetch seam. It
evaluates the WHERE clause the module actually sends (the status list and the
expiry test) and projects the columns its SELECT names, so the live filter is
exercised from the SQL rather than restated here. Any other statement shape
fails the test.
"""
import ast
import json
import pathlib
import re
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402

ADMIN_KEY = "admin-key-for-summary-tests-0123456789"  # secretscan:allow (test placeholder)
TITLE = "Title Sentinel Powered Shell"
SLUG = "slug-sentinel-dfw"
SUMMARY_TEXT = "Summary sentinel about the site."
PROVIDER = "Provider Sentinel Holdings"
ASKING_PRICE = 9876543
PRICE_LOW, PRICE_HIGH = 4321.5, 4999.5
OPERATOR = "operator-private@sentinel.example"
OWNER = "owner-private-sentinel"
HEADING = "\n## Capacity Source"
UPCOMING_SUFFIX = " (upcoming)"
UPCOMING_SENTENCE = ("The program is UPCOMING while the first listings are\n"
                     "onboarded, and GET /api/v1/listings says so in `program.status`")
LIVE_SENTENCE = ("Listings are live; GET /api/v1/listings returns them\n"
                 "with `program.status`")

_SELECT_RE = re.compile(
    r"^SELECT (?P<cols>.+?) FROM exclusive_listings"
    r"(?: WHERE (?P<where>.+?))?(?: ORDER BY (?P<order>updated_at DESC))?"
    r"(?P<limit> LIMIT %s)?$")
_STATUS_RE = re.compile(r"^status IN \((?P<values>'[a-z]+'(?:, '[a-z]+')*)\)$")
_EXPIRY = "(expires_at IS NULL OR expires_at > NOW())"
_DETAIL_TEXT_RE = re.compile(r"^detail->>'(?P<key>[a-z_]+)'$")


def _listing(**over):
    row = {"id": 1, "slug": SLUG, "title": TITLE, "summary": SUMMARY_TEXT,
           "status": "pocket", "tier_required": "registered",
           "market": "Dallas", "state": "TX", "country": "US",
           "latitude": 32.776712, "longitude": -96.797012, "capacity_mw": 40.0,
           "asking_price": ASKING_PRICE, "asking_currency": "USD",
           "detail": {"delivery_type": "powered_shell",
                      "provider": {"name": PROVIDER, "disclosed": True},
                      "price": {"low": PRICE_LOW, "high": PRICE_HIGH,
                                "unit": "usd_per_kw_month"}},
           "contact": {"email": OPERATOR}, "owner_id": OWNER,
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


class _Store:
    """Postgres stand-in for the module's _fetch(sql, params, cols)."""

    def __init__(self):
        self.listings, self.statements, self.fail = [], [], None

    def fetch(self, sql, params, cols):
        statement = " ".join(sql.split())
        self.statements.append(statement)
        if self.fail is not None:
            raise self.fail
        m = _SELECT_RE.match(statement)
        assert m, f"unexpected SQL: {statement[:160]}"
        exprs = [e.strip() for e in m.group("cols").split(",")]
        assert len(exprs) == len(cols), (exprs, cols)
        rows = list(self.listings)
        for predicate in _predicates(statement):
            status = _STATUS_RE.match(predicate)
            if status:
                allowed = set(re.findall(r"'([a-z]+)'", status.group("values")))
                rows = [r for r in rows if r["status"] in allowed]
            elif predicate == _EXPIRY:
                now = datetime.now(timezone.utc)
                rows = [r for r in rows if r["expires_at"] is None or r["expires_at"] > now]
            else:
                raise AssertionError(f"unexpected predicate: {predicate}")
        if m.group("order"):
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
        if m.group("limit"):
            rows = rows[:params[-1]]
        return [dict(zip(cols, (self._value(r, e) for e in exprs))) for r in rows]

    @staticmethod
    def _value(row, expr):
        text = _DETAIL_TEXT_RE.match(expr)
        if text:
            value = (row.get("detail") or {}).get(text.group("key"))
            return value if value is None or isinstance(value, str) else json.dumps(value)
        assert expr in row, f"unexpected column: {expr}"
        return row[expr]

    def summary_reads(self):
        return [s for s in self.statements if "detail->>'delivery_type'," in s]


def _predicates(statement):
    where = _SELECT_RE.match(statement).group("where")
    return where.split(" AND ") if where else []


def _no_connection():
    raise AssertionError("these tests reach storage only through _fetch")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    store = _Store()
    monkeypatch.setattr(el, "_dsn", lambda: "postgresql://stand-in/listings")
    monkeypatch.setattr(el, "_fetch", store.fetch)
    monkeypatch.setattr(el, "_conn", _no_connection)
    monkeypatch.setattr(el, "_ensure_schema", lambda: None)
    el._SUMMARY_CACHE.update(at=None, value=None)
    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    store.app, store.client = app, app.test_client()
    yield store
    el._SUMMARY_CACHE.update(at=None, value=None)


@pytest.fixture
def clock(monkeypatch):
    """The summary cache's monotonic clock, set by hand."""
    now = {"t": 1000.0}
    fake = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    fake["monotonic"] = lambda: now["t"]
    monkeypatch.setattr(el, "time", types.SimpleNamespace(**fake))
    return now


def _get_summary(env):
    r = env.client.get("/api/v1/listings/summary")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r, r.get_json()


# ── the summary ───────────────────────────────────────────────────────────

def test_an_empty_table_summarizes_to_nothing_live(env):
    r, j = _get_summary(env)
    # ★ WHOLE-PAYLOAD equality on purpose: this is the summary's exact shape,
    #   so a key that appears here appears in an agent's hands. `citation` is
    #   the PUBLIC one (2026-09-16) — the summary aggregates teaser-level
    #   columns only, and is written to be quoted.
    assert j == {"ok": True, "citation": el._teaser_citation(),
                 "program_status": "upcoming", "live_count": 0,
                 "total_mw": None, "markets": [], "delivery_types": {},
                 "latest_updated_at": None, "generated_at": j["generated_at"],
                 "url": "https://dchub.cloud/listings", "mcp_tool": "source_capacity"}
    assert j["citation"]["license"] == el.TEASER_LICENSE == "CC-BY-4.0"
    assert j["citation"]["redistribution"] == "permitted_with_attribution"
    assert j["program_status"] == el._program(0)["status"]
    assert datetime.fromisoformat(j["generated_at"]).utcoffset() == timedelta(0)
    assert r.headers["Cache-Control"] == "private, no-store"
    assert len(env.summary_reads()) == 1


def test_two_listings_in_one_market_add_up(env):
    env.listings = [
        _listing(id=1, slug="dfw-shell", capacity_mw=40.0,
                 detail={"delivery_type": "powered_shell"}),
        _listing(id=2, slug="dfw-turnkey", capacity_mw=60.5,
                 detail={"delivery_type": "turnkey"},
                 updated_at=datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)),
    ]
    _, j = _get_summary(env)
    assert j["program_status"] == el._program(2)["status"] == "live"
    assert j["live_count"] == 2 and j["total_mw"] == 100.5
    assert j["markets"] == [{"market": "Dallas", "state": "TX", "country": "US", "count": 2,
                             "mw": 100.5, "delivery_types": ["powered_shell", "turnkey"]}]
    assert j["delivery_types"] == {"powered_shell": 1, "turnkey": 1}
    assert j["latest_updated_at"] == "2026-09-05T12:30:00.000000+00:00"


def test_three_listings_across_markets_rank_by_count_before_mw(env):
    env.listings = [
        _listing(id=1, slug="dfw-land", market="Dallas", state="TX", capacity_mw=10.0,
                 detail={"delivery_type": "land"},
                 updated_at=datetime(2026, 9, 4, tzinfo=timezone.utc)),
        _listing(id=2, slug="dfw-shell", market=" dallas ", state="tx", capacity_mw=15.0,
                 detail={"delivery_type": "powered_shell"},
                 updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _listing(id=3, slug="phx-turnkey", market="Phoenix", state="AZ", capacity_mw=80.0,
                 detail={"delivery_type": "turnkey"},
                 updated_at=datetime(2026, 9, 3, tzinfo=timezone.utc)),
    ]
    _, j = _get_summary(env)
    assert j["live_count"] == 3 and j["total_mw"] == 105
    assert j["markets"] == [
        {"market": "Dallas", "state": "TX", "country": "US", "count": 2, "mw": 25,
         "delivery_types": ["land", "powered_shell"]},
        {"market": "Phoenix", "state": "AZ", "country": "US", "count": 1, "mw": 80,
         "delivery_types": ["turnkey"]},
    ]
    assert j["delivery_types"] == {"land": 1, "powered_shell": 1, "turnkey": 1}
    assert j["latest_updated_at"] == "2026-09-04T00:00:00.000000+00:00"


def test_equal_counts_rank_by_mw_and_unrecognized_delivery_types_are_not_counted(env):
    env.listings = [
        _listing(id=1, slug="iad", market="Ashburn", state="VA", capacity_mw=None,
                 detail={"delivery_type": "spaceport"}),
        _listing(id=2, slug="cmh", market="Columbus", state="OH", capacity_mw=20.0, detail={}),
        _listing(id=3, slug="rno", market="Reno", state="NV", capacity_mw=90.0,
                 detail={"delivery_type": "colocation"}),
    ]
    _, j = _get_summary(env)
    assert [(m["market"], m["mw"]) for m in j["markets"]] == [
        ("Reno", 90), ("Columbus", 20), ("Ashburn", None)]
    assert j["markets"][1]["delivery_types"] == [] and j["markets"][2]["delivery_types"] == []
    assert j["delivery_types"] == {"colocation": 1}
    assert j["live_count"] == 3 and j["total_mw"] == 110


def test_markets_stop_at_fifty(env):
    env.listings = [_listing(id=i, slug=f"m{i}", market=f"Market {i:02d}", capacity_mw=float(i))
                    for i in range(1, 56)]
    _, j = _get_summary(env)
    assert j["live_count"] == 55 and len(j["markets"]) == 50
    assert j["markets"][0]["market"] == "Market 55" and j["markets"][-1]["market"] == "Market 06"


def test_drafts_and_expired_listings_stay_out_exactly_as_in_the_feed(env):
    assert "draft" in el._VALID_STATUSES
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    env.listings = [
        _listing(id=1, slug="live-pocket", status="pocket", capacity_mw=10.0),
        _listing(id=2, slug="live-public", status="public", capacity_mw=20.0),
        _listing(id=3, slug="still-draft", status="draft", capacity_mw=300.0),
        _listing(id=4, slug="expired", status="pocket", capacity_mw=4000.0, expires_at=expired),
    ]
    feed = env.client.get("/api/v1/listings").get_json()
    _, j = _get_summary(env)
    assert sorted(item["slug"] for item in feed["items"]) == ["live-pocket", "live-public"]
    assert j["live_count"] == feed["count"] == 2 and j["total_mw"] == 30
    feed_read = next(s for s in env.statements if s.startswith("SELECT id, slug,"))
    (summary_read,) = env.summary_reads()
    assert set(_predicates(summary_read)) == set(_predicates(feed_read))


def test_the_summary_names_no_title_slug_provider_price_or_contact(env):
    env.listings = [_listing()]
    feed = env.client.get("/api/v1/listings").get_data(as_text=True)
    r, j = _get_summary(env)
    body = r.get_data(as_text=True)
    assert j["live_count"] == 1
    # Control: the same row through the teaser feed carries its title, slug
    # and disclosed provider, so their absence below is the summary's doing.
    for shown in (TITLE, SLUG, PROVIDER):
        assert shown in feed
    for hidden in (TITLE, SLUG, SUMMARY_TEXT, PROVIDER, str(ASKING_PRICE),
                   str(PRICE_LOW), str(PRICE_HIGH), OPERATOR, OWNER):
        assert hidden not in body


# ── caching and failure ───────────────────────────────────────────────────

def test_the_table_is_read_at_most_once_a_minute(env, clock):
    env.listings = [_listing(id=1, slug="one")]
    _, first = _get_summary(env)
    env.listings.append(_listing(id=2, slug="two", market="Phoenix", state="AZ"))
    clock["t"] += el._SUMMARY_TTL_S - 1
    _, cached = _get_summary(env)
    assert cached == first and len(env.summary_reads()) == 1
    clock["t"] += 2
    _, fresh = _get_summary(env)
    assert fresh["live_count"] == 2 and len(env.summary_reads()) == 2


def test_concurrent_callers_share_one_read(env, monkeypatch):
    env.listings = [_listing()]
    read = env.fetch

    def slow_read(sql, params, cols):
        time.sleep(0.2)
        return read(sql, params, cols)

    monkeypatch.setattr(el, "_fetch", slow_read)
    gate, results = threading.Barrier(8), []

    def call():
        gate.wait()
        results.append(el.cached_listings_summary())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 8 and all(r is results[0] for r in results)
    assert results[0]["live_count"] == 1 and len(env.summary_reads()) == 1


def test_an_unreadable_table_answers_ok_false_until_a_later_read_succeeds(env, clock):
    env.fail = RuntimeError("could not connect to server")
    for _ in range(2):
        r = env.client.get("/api/v1/listings/summary")
        assert r.status_code == 200
        assert r.get_json() == {"ok": False, "error": "listings_unavailable"}
        assert r.headers["Cache-Control"] == "private, no-store"
    assert len(env.statements) == 1
    env.fail, env.listings = None, [_listing()]
    clock["t"] += el._SUMMARY_TTL_S + 1
    _, j = _get_summary(env)
    assert j["ok"] is True and j["live_count"] == 1


# ── routing ───────────────────────────────────────────────────────────────

def test_the_summary_path_is_served_by_its_own_view(env):
    adapter = env.app.url_map.bind("dchub.test")
    assert adapter.match("/api/v1/listings/summary", method="GET") == (
        "exclusive_listings.listings_summary", {})
    assert env.app.view_functions["exclusive_listings.listings_summary"] is el.listings_summary
    assert [r.rule for r in env.app.url_map.iter_rules()
            if r.endpoint == "exclusive_listings.listings_summary"] == ["/api/v1/listings/summary"]
    # Control: any other word in that position still reaches the listing route.
    assert adapter.match("/api/v1/listings/dfw-40", method="GET") == (
        "exclusive_listings.get_listing", {"slug_or_id": "dfw-40"})


def test_summary_can_never_become_a_listing_slug(env):
    assert "summary" in el._RESERVED_SLUGS
    assert el._safe_get_listing("Summary") is None and env.statements == []
    headers = {"X-Admin-Key": ADMIN_KEY}
    for body in ({"title": "Summary"}, {"title": "Dallas shell", "slug": "summary"}):
        r = env.client.post("/api/v1/admin/listings", json=body, headers=headers)
        assert r.status_code == 400, r.get_data(as_text=True)
        assert "reserved path word" in r.get_json()["message"]
    # Control: an unreserved slug gets past that check and on to storage.
    r = env.client.post("/api/v1/admin/listings",
                        json={"title": "Dallas shell", "slug": "summary-dallas"}, headers=headers)
    assert r.status_code == 503 and r.get_json()["error"] == "write_failed"


# ── llms.txt ──────────────────────────────────────────────────────────────

@pytest.fixture
def llms_txt(env):
    import ai_discovery_routes

    app = Flask("capacity-source-llms")
    ai_discovery_routes.register_discovery_routes(app)
    client = app.test_client()

    def render():
        r = client.get("/llms.txt")
        assert r.status_code == 200
        return r.get_data(as_text=True)

    return render


def _block(text):
    """From the Capacity Source heading up to the next heading."""
    start = text.index(HEADING) + 1
    end = text.find("\n## ", start)
    return text[start:] if end < 0 else text[start:end + 1]


def _source_block():
    """The block as written in serve_llms_txt, read from the source."""
    import ai_discovery_routes

    tree = ast.parse(pathlib.Path(ai_discovery_routes.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "serve_llms_txt")
    texts = [n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, str) and HEADING in n.value]
    assert len(texts) == 1, f"expected the block in one literal, found {len(texts)}"
    return _block(texts[0])


def test_llms_block_is_served_as_written_while_nothing_is_live(env, llms_txt):
    body = llms_txt()
    assert el._SUMMARY_CACHE["value"]["live_count"] == 0
    assert _block(body) == _source_block()


def test_llms_block_reads_live_while_listings_are_live(env, llms_txt):
    env.listings = [
        _listing(id=1, slug="dfw-a", capacity_mw=40.0,
                 updated_at=datetime(2026, 9, 3, tzinfo=timezone.utc)),
        _listing(id=2, slug="dfw-b", capacity_mw=25.0,
                 updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _listing(id=3, slug="phx-a", market="Phoenix", state="AZ", capacity_mw=40.0,
                 updated_at=datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)),
    ]
    heading, rest = _source_block().split("\n", 1)
    # As written, the block carries the upcoming wording the live form replaces.
    assert heading.endswith(UPCOMING_SUFFIX) and rest.count(UPCOMING_SENTENCE) == 1
    line = ("Available now: 3 listings, 105 MW across Dallas and Phoenix, "
            "last updated 2026-09-05. Browse with source_capacity.")
    block = _block(llms_txt())
    assert block == (heading[:-len(UPCOMING_SUFFIX)] + "\n" + line + "\n"
                     + rest.replace(UPCOMING_SENTENCE, LIVE_SENTENCE))
    assert "upcoming" not in block.lower()
    # llms.txt and the endpoint share one read.
    _, j = _get_summary(env)
    assert j["live_count"] == 3 and len(env.summary_reads()) == 1


def test_llms_program_wording_is_live_only_while_listings_are_live(env, llms_txt, clock):
    upcoming = _block(llms_txt())
    assert el._SUMMARY_CACHE["value"]["live_count"] == 0
    assert upcoming.split("\n", 1)[0].endswith(UPCOMING_SUFFIX)
    assert UPCOMING_SENTENCE in upcoming and LIVE_SENTENCE not in upcoming

    env.listings = [_listing()]
    clock["t"] += el._SUMMARY_TTL_S + 1
    live = _block(llms_txt())
    assert el._SUMMARY_CACHE["value"]["live_count"] == 1
    assert not live.split("\n", 1)[0].endswith(UPCOMING_SUFFIX)
    assert LIVE_SENTENCE in live and "upcoming" not in live.lower()


def test_llms_line_names_three_markets_and_counts_the_rest(env, llms_txt):
    tool_count_shape = "Zone " + "9" + " tools"
    env.listings = [
        _listing(id=1, slug="a", market="Dallas", capacity_mw=500.0),
        _listing(id=2, slug="b", market="Dallas", capacity_mw=250.5),
        _listing(id=3, slug="c", market=tool_count_shape, capacity_mw=200.0),
        _listing(id=4, slug="d", market="Phoenix", capacity_mw=150.0),
        _listing(id=5, slug="e", market="Reno", capacity_mw=100.0,
                 updated_at=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)),
        _listing(id=6, slug="f", market="Austin", capacity_mw=50.0),
    ]
    block = _block(llms_txt())
    assert block.split("\n")[1] == (
        "Available now: 6 listings, 1,250.5 MW across Dallas, Phoenix, Reno and "
        "2 more markets, last updated 2026-09-10. Browse with source_capacity.")
    assert tool_count_shape not in block


def test_llms_line_for_one_listing_without_capacity(env, llms_txt):
    env.listings = [_listing(capacity_mw=None)]
    assert _block(llms_txt()).split("\n")[1] == (
        "Available now: 1 listing across Dallas, last updated 2026-09-02. "
        "Browse with source_capacity.")


def test_llms_block_is_unchanged_when_the_summary_cannot_be_read(env, llms_txt, monkeypatch):
    env.fail = RuntimeError("could not connect to server")
    unreadable = llms_txt()
    assert env.statements and el._SUMMARY_CACHE["value"] is None
    assert _block(unreadable) == _source_block()

    calls = []

    def raising():
        calls.append(1)
        raise RuntimeError("summary raised")

    monkeypatch.setattr(el, "cached_listings_summary", raising)
    assert llms_txt() == unreadable and calls
