"""Paid numerics on public routes: the full answer for what /pricing sells, a
200 tease for everyone else (free/anon tighten, 2026-09-21).

WHY. Measured live and keyless (cache-busted) before this change, every one of
these answered with the paid numbers for every market:

  /api/v1/data/dcpi-current.json, /data/dcpi-current.json   excess, constraint,
  /api/v1/data/dcpi-history.csv, /data/dcpi-history.csv     time-to-power
  /api/v1/dcpi/history (no market: every market's daily series)
  /api/v1/dcpi/scores/<slug> and /v2, /dcpi/<slug>, the og cards, the embed
  /api/v1/markets/list-rich, /api/v1/markets/compare, /markets/<slug>.json

The list /api/v1/dcpi/scores was already gated. The single-market surfaces
answered through a 2026-07-03 switch that published every market's scores one
URL at a time, and the all-market history used the same switch.

Now util/numeric_tease.serve_full_or_tease decides once: Developer and above
(API key, JWT or website session), a key holding $10-pack credits (one credit
per delivered 200), X-Internal-Key and admin get the full answer; every other
caller gets HTTP 200 with at most TEASE_ROWS rows, the numbers null, and the
_gated/_preview_only/_locked_fields/_total_available envelope plus the
pack-led ladder.

The routes run for real: their own blueprints on a Flask app, with the
database answered from a table; main.py's two handlers are pulled out with ast
and executed, as tests/test_keyed_rest_walls_open.py does.
"""
import ast
import base64
import builtins
import copy
import datetime as dt
import functools
import json
import logging
import os
import pathlib
import re
import sys
import types

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "numeric-tease-test-internal-key"
ADMIN = "numeric-tease-test-admin-key"
FREE_KEY = "dch_live_" + "f" * 32
PACK_KEY = "dch_live_" + "p" * 32          # free tier, holding $10-pack credits
DEV_KEY = "dch_live_" + "d" * 32
PRO_KEY = "dch_live_" + "k" * 32
STARTER_KEY = "dch_live_" + "s" * 32
UNKNOWN_KEY = "dch_live_" + "u" * 32
PLANS = {FREE_KEY: "free", PACK_KEY: "free", DEV_KEY: "developer", PRO_KEY: "pro",
         STARTER_KEY: "starter"}
SESSIONS = {"jwt-dev": "developer", "jwt-free": "free"}

NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 9, 21)

# Distinctive values: a leak is a number from this set showing up in a tease.
SCORE_ROWS = [
    # slug, name, state, iso, excess, constraint, ttp, verdict
    ("abilene", "Abilene", "TX", "ERCOT", 62.4, 51.6, 27.3, "CAUTION"),
    ("akron", "Akron", "OH", "PJM", 31.7, 44.9, 18.2, "AVOID"),
    ("albany", "Albany", "NY", "NYISO", 38.1, 41.3, 21.4, "AVOID"),
    ("ashburn", "Ashburn", "VA", "PJM", 44.7, 55.4, 32.4, "AVOID"),
    ("dallas", "Dallas", "TX", "ERCOT", 65.8, 54.7, 30.3, "CAUTION"),
]
LEAK_NUMBERS = {"62.4", "51.6", "27.3", "31.7", "44.9", "18.2", "38.1", "41.3",
                "21.4", "44.7", "55.4", "32.4", "65.8", "54.7", "30.3",
                "3690", "6964", "11.65", "7067.1", "3351.2", "31760.5", "19.6", "40.5",
                "1.37", "2.29"}                     # the change feed's deltas
# The cards and the embed print Ashburn's scores rounded: 45, 55 and ~32 months.
INT_LEAKS = (r">\s*45\s*<", r">\s*55\s*<", r"~32\s*mo", r"\b45/100", r"\b55/100")


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")


# ── a database answered from a table ─────────────────────────────────────────

def _mps_row(slug):
    s, n, st, iso, e, c, t, v = next(r for r in SCORE_ROWS if r[0] == slug)
    return {"id": 1374, "market_slug": s, "market_name": n, "state": st, "iso": iso,
            "latitude": 39.019295, "longitude": -77.47066,
            "constraint_score": c, "excess_power_score": e, "time_to_power_months": t,
            "queue_capacity_mw": 31760.5, "queue_wait_months": 40.5,
            "reserve_margin_pct": 19.6, "gen_additions_12mo_mw": 3351.2,
            "curtailment_pct": 1.0, "stranded_capacity_mw": None,
            "emergency_count_30d": 0, "avg_kwh_cents": 11.65, "quality_score": 100,
            "top_risks_json": ["Queue wait 40.5 months"],
            "top_opportunities_json": ["3351.2 MW of additions"],
            "verdict": v, "tier_required": "lite-pro", "computed_at": NOW,
            "trend_30d": {"excess": [44.7, 44.7]}, "data_basis_json": None,
            "signal_tier": "full", "published": True, "iso_type": "RTO"}


class FakeCursor:
    def __init__(self, db, dict_rows):
        self.db, self.dict_rows, self._rows = db, dict_rows, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        sql = re.sub(r"\s*=\s*%s", " = %s", " ".join(sql.split()))
        rows = self.db.answer(sql, params)
        self._rows = [r if self.dict_rows else tuple(r.values()) for r in rows]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, db, default_dict=False):
        self.db, self.default_dict = db, default_dict

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.db, self.default_dict or cursor_factory is not None)

    def close(self):
        pass

    def commit(self):
        pass


class FakeDB:
    """Answers each query the routes under test make, by what the SQL says.

    A query it has no answer for is recorded, not raised: most routes read
    inside a try, so raising would turn a gap in this table into a quiet
    fallback. The client fixture fails the test on any miss."""

    def __init__(self):
        self.misses = []

    def answer(self, sql, params):
        p = tuple(params or ())
        if "ORDER BY computed_at DESC LIMIT 100000" in sql:          # history CSV
            return [{"computed_at": NOW, "market_slug": s, "market_name": n, "state": st,
                     "iso": iso, "excess_power_score": e, "constraint_score": c,
                     "time_to_power_months": t, "verdict": v}
                    for s, n, st, iso, e, c, t, v in SCORE_ROWS]
        if sql.startswith("SELECT DISTINCT ON (market_slug) market_slug, market_name, state, iso, excess_power_score"):
            return [{"market_slug": s, "market_name": n, "state": st, "iso": iso,
                     "excess_power_score": e, "constraint_score": c,
                     "time_to_power_months": t, "verdict": v, "computed_at": NOW}
                    for s, n, st, iso, e, c, t, v in SCORE_ROWS]
        if "COUNT(DISTINCT snapshot_date)" in sql:
            return [{"d": 90, "lo": TODAY - dt.timedelta(days=89), "hi": TODAY}]
        if "snapshot_date AS day" in sql:                              # api_history
            return [{"market_slug": s, "market_name": n,
                     "day": TODAY - dt.timedelta(days=d), "excess": e, "constraint": c}
                    for s, n, st, iso, e, c, t, v in SCORE_ROWS for d in (2, 1, 0)]
        if "SELECT DISTINCT snapshot_date FROM dcpi_daily_snapshots" in sql:
            return [{"snapshot_date": TODAY}, {"snapshot_date": TODAY - dt.timedelta(days=1)}]
        if "JOIN dcpi_daily_snapshots o" in sql:                        # changes
            return [{"market_slug": s, "market_name": n, "old_excess": e - 1.37,
                     "new_excess": e, "old_constraint": c + 2.29, "new_constraint": c,
                     "old_verdict": "CAUTION" if s == "akron" else v, "new_verdict": v}
                    for s, n, st, iso, e, c, t, v in SCORE_ROWS]
        if "FROM dcpi_daily_snapshots WHERE lower(market_slug) = %s" in sql:
            slug = p[0]
            row = next(r for r in SCORE_ROWS if r[0] == slug)
            return [{"snapshot_date": TODAY - dt.timedelta(days=d), "market_name": row[1],
                     "excess_power_score": row[4] - d * 0.5,
                     "constraint_score": row[5] + d * 0.25, "verdict": row[7]}
                    for d in range(9, -1, -1)]
        if "snapshot_date::timestamptz AS computed_at" in sql:          # score forecast
            return []
        if "FROM market_power_scores WHERE market_slug = %s" in sql:
            slug = p[0]
            return [_mps_row(slug)] if any(r[0] == slug for r in SCORE_ROWS) else []
        # ★ water_risk, not usgs_water_stress. This fixture used to answer
        # `AVG(stress_index) FROM usgs_water_stress` with a plausible 3.25 —
        # a column that has never existed on that table. Answering a query
        # the real database raises UndefinedColumn on is how these tests sat
        # green over a dead read for months (#5259 and follow-up). 56.25 is
        # the WRI Medium-High midpoint, which bands to 3.
        if "FROM water_risk" in sql:
            return [{"water_stress_score": 56.25}]
        if "eia_retail_rates" in sql:
            return [{"rate_cents_kwh": 8.75}]
        self.misses.append(sql[:160])
        return []


# ── the gate's inputs: keys, sessions, pack balances, credit burns ───────────

@pytest.fixture
def ledger(monkeypatch):
    import api_tier_gating
    import routes.mcp_conversion_plays as plays
    import routes.partner_attribution as pa
    import util.location_meter as lm
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key",
                        lambda k: {"plan": PLANS[k], "user_id": k} if k in PLANS else None)
    monkeypatch.setattr(api_tier_gating, "_get_decode_jwt",
                        lambda: (lambda tok: {"user_id": tok} if tok in SESSIONS else None))
    monkeypatch.setattr(api_tier_gating, "get_user_plan",
                        lambda user_id=None, email=None: SESSIONS.get(user_id, "free"))
    monkeypatch.setattr(lm, "pack_active", lambda api_key=None, **kw: api_key == PACK_KEY)
    state = {"burns": [], "burn_ok": True}

    def consume(key, sid, n):
        state["burns"].append((key, n))
        return {"ok": state["burn_ok"], "remaining": 41}
    monkeypatch.setattr(plays, "consume_credits", consume)
    pa._drain()
    yield state
    pa._drain()


@pytest.fixture
def client(ledger, monkeypatch):
    import routes.dcpi as dcpi
    import routes.dcpi_temporal as temporal
    import routes.market_deep_dive as mdd
    import routes.open_data as od
    db = FakeDB()
    monkeypatch.setattr(od, "_conn", lambda: FakeConn(db))
    monkeypatch.setattr(temporal, "_conn", lambda: FakeConn(db, default_dict=True))
    monkeypatch.setattr(dcpi, "_conn", lambda: FakeConn(db))
    monkeypatch.setattr(dcpi, "_ensure_tables", lambda: None)
    monkeypatch.setattr(dcpi, "_redis_get_page", lambda key: None)
    monkeypatch.setattr(dcpi, "_redis_set_page", lambda key, html: None)
    monkeypatch.setattr(dcpi, "_dcpi_facility_list_html", lambda *a: "")
    monkeypatch.setattr(dcpi, "_DCPI_PAGE_CACHE", {})
    narrative = types.ModuleType("routes.report_narrative")
    narrative.attach_market_narrative = lambda row, risks, opps: (
        "Ashburn excess power 44.7 against constraint 55.4.")
    monkeypatch.setitem(sys.modules, "routes.report_narrative", narrative)
    monkeypatch.setattr(mdd, "read_deep_dive", lambda slug: {
        "market_name": "Dallas", "key_stats": {}, "generated_at": NOW})
    monkeypatch.setattr(mdd, "read_live_stats", lambda slug, stats, name=None: ({
        "total_mw": 7067.1, "facility_count": 386, "dcpi_score": 49.3,
        "dcpi_as_of": NOW.isoformat(), "excess_power_score": 65.8,
        "constraint_score": 54.7, "time_to_power_months": 30.3,
        "verdict": "CAUTION"}, None, None))
    app = flask.Flask("numeric-tease")
    app.register_blueprint(od.open_data_bp)
    app.register_blueprint(temporal.dcpi_temporal_bp)
    app.register_blueprint(dcpi.dcpi_bp)
    app.register_blueprint(mdd.market_deep_dive_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    yield c
    assert not db.misses, f"queries the fake database could not answer: {db.misses}"


PARTNER_IP = "104.248.242.235"     # a declared partner egress (routes/partner_attribution)


def _get(client, path, key=None, headers=None, cookie=None, ip="203.0.113.7"):
    h = {"User-Agent": "dchub-test", "CF-Connecting-IP": ip}
    h.update(headers or {})
    if key:
        h["X-API-Key"] = key
    if cookie:
        client.set_cookie("dchub_token", cookie)
    try:
        return client.get(path, headers=h)
    finally:
        if cookie:
            client.delete_cookie("dchub_token")


# ── the routes: how to read rows and numbers out of each ─────────────────────

def _csv_rows(r):
    lines = [ln for ln in r.get_data(as_text=True).splitlines() if ln]
    return [ln for ln in lines[1:] if not ln.startswith(("# ", '"# '))]


ROUTES = {
    "/api/v1/data/dcpi-current.json": lambda b: b["snapshot"],
    "/data/dcpi-current.json": lambda b: b["snapshot"],
    "/api/v1/dcpi/history": lambda b: list(b["series"].values()),
    "/api/v1/dcpi/history?market=ashburn": lambda b: b["series"],
    "/api/v1/dcpi/forecast?market=ashburn": lambda b: b["projection"],
    "/api/v1/dcpi/changes": lambda b: b["changes"],
    "/api/v1/dcpi/scores/ashburn": lambda b: [b],
    "/api/v1/dcpi/scores/ashburn/v2": lambda b: [b],
    "/markets/dallas.json": lambda b: [b],
}
CSV_ROUTES = ["/api/v1/data/dcpi-history.csv", "/data/dcpi-history.csv"]
HTML_ROUTES = ["/dcpi/ashburn", "/api/v1/dcpi/page/ashburn",
               "/research/ashburn", "/api/v1/research/ashburn",
               "/dcpi/og/ashburn.svg", "/api/v1/dcpi/og/ashburn", "/dcpi/embed/ashburn",
               "/api/v1/dcpi/embed/ashburn"]
ALL_ROUTES = list(ROUTES) + CSV_ROUTES + HTML_ROUTES


def _leaks(text):
    # Not after a digit, colon or dot and not before a digit: a response's own
    # as_of clock ("...T04:03:55.41+00:00") must not read as a leaked 55.4.
    return (sorted(n for n in LEAK_NUMBERS
                   if re.search(r"(?<![\d.])(?<!\d:)" + re.escape(n) + r"(?!\d)", text))
            + [p for p in INT_LEAKS if re.search(p, text)])


_ENVELOPE = {"_locked_fields", "upgrade_options", "upgrade_url", "gating_matrix"}


def _numbers_in(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return []
    if isinstance(v, (int, float)):
        return [v]
    items = v.values() if isinstance(v, dict) else v if isinstance(v, list) else []
    return [n for x in items for n in _numbers_in(x)]


def _locked_values(body):
    """Every (key, value) in the body whose key the tease says it locked and
    whose value still carries a number: structural, so a projected or derived
    value nobody listed in LEAK_NUMBERS is caught as well."""
    locked, found = set(body.get("_locked_fields") or ()), []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _ENVELOPE:
                    continue
                if k in locked and _numbers_in(v):
                    found.append((k, v))
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(body)
    return found


def _assert_tease(r, path):
    assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:300])
    text = r.get_data(as_text=True)
    assert not _leaks(text), f"{path} tease still carries {_leaks(text)}"
    assert "private" not in (r.headers.get("Cache-Control") or ""), path
    if path in ROUTES:
        body = r.get_json(force=True)
        rows = ROUTES[path](body)
        assert 1 <= len(rows) <= 3, (path, len(rows))
        assert body["_gated"] is True and body["_preview_only"] is True
        assert body["_locked_fields"] and isinstance(body["_total_available"], int)
        assert not _locked_values(body), f"{path} locked fields still hold numbers: {_locked_values(body)[:3]}"
        assert _plan_of(body["upgrade_url"])[0] == "metered"          # the pack leads
        assert [o["plan"] for o in body["upgrade_options"]] == ["pack", "developer"]
        assert body["gating_matrix"].endswith("/api/v1/gating-matrix")
        assert "https://dchub.cloud/pricing" not in text, "a bare /pricing link in a tease"
    elif path in CSV_ROUTES:
        rows = _csv_rows(r)
        assert 1 <= len(rows) <= 3, rows
        assert r.headers["X-DCHub-Total-Available"] == str(len(SCORE_ROWS))
        assert _plan_of(r.headers["X-DCHub-Upgrade-Url"])[0] == "metered"
        assert text.splitlines()[0].startswith("computed_at,market_slug")
        assert "go/c/" in text.splitlines()[-1]                        # names the ladder
    return text


def _assert_full(r, path):
    assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:300])
    text = r.get_data(as_text=True)
    assert _leaks(text), f"{path} full answer carries none of the numbers"
    assert "no-store" in r.headers.get("Cache-Control", ""), path
    if path in ROUTES:
        body = r.get_json(force=True)
        assert not body.get("_gated") and not body.get("_preview_only"), path
    return text


# ── who gets the tease ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ALL_ROUTES)
def test_keyless_gets_the_tease(client, ledger, path):
    """FAILS on main: every route answered keyless with the paid numbers."""
    _assert_tease(_get(client, path), path)
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", list(ROUTES) + CSV_ROUTES)
@pytest.mark.parametrize("key", [FREE_KEY, STARTER_KEY, UNKNOWN_KEY])
def test_a_key_below_developer_gets_the_tease_not_an_error(client, ledger, path, key):
    _assert_tease(_get(client, path, key), path)
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ["/api/v1/dcpi/scores/ashburn", "/dcpi/ashburn",
                                  "/api/v1/data/dcpi-current.json"])
def test_a_signed_in_free_user_gets_the_tease(client, path):
    _assert_tease(_get(client, path, cookie="jwt-free"), path)


@pytest.mark.parametrize("path", list(ROUTES) + CSV_ROUTES)
def test_a_pack_that_cannot_burn_gets_the_tease_not_the_data(client, ledger, path):
    ledger["burn_ok"] = False
    _assert_tease(_get(client, path, PACK_KEY), path)


# ── who gets the full answer ─────────────────────────────────────────────────

@pytest.mark.parametrize("path", ALL_ROUTES)
@pytest.mark.parametrize("key", [DEV_KEY, PRO_KEY])
def test_developer_and_above_get_everything(client, ledger, path, key):
    _assert_full(_get(client, path, key), path)
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_a_pack_key_gets_everything_for_one_credit(client, ledger, path):
    r = _get(client, path, PACK_KEY)
    _assert_full(r, path)
    assert r.headers["X-DCHub-Access"] == "pack"
    assert ledger["burns"] == [(PACK_KEY, 1)]


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_the_mcp_server_is_unchanged(client, ledger, path):
    _assert_full(_get(client, path, headers={"X-Internal-Key": SECRET}), path)
    assert ledger["burns"] == []


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_admin_is_unchanged(client, ledger, path):
    _assert_full(_get(client, path, headers={"X-Admin-Key": ADMIN}), path)


@pytest.mark.parametrize("path", ["/api/v1/dcpi/scores/ashburn", "/dcpi/ashburn",
                                  "/api/v1/data/dcpi-history.csv", "/markets/dallas.json"])
def test_a_signed_in_developer_is_not_locked_out(client, path):
    """The website session resolves through require_plan, like a key does."""
    _assert_full(_get(client, path, cookie="jwt-dev"), path)


def test_a_wrong_internal_or_admin_key_is_just_keyless(client):
    for h in ({"X-Internal-Key": "nope"}, {"X-Admin-Key": "nope"}):
        _assert_tease(_get(client, "/api/v1/dcpi/scores/ashburn", headers=h),
                      "/api/v1/dcpi/scores/ashburn")


def test_a_gate_that_cannot_decide_serves_the_tease(client, monkeypatch):
    """Fail closed: an exception inside the gate is never the full answer."""
    import api_tier_gating

    def broken(*a, **k):
        raise RuntimeError("gate down")
    monkeypatch.setattr(api_tier_gating, "require_plan", broken)
    _assert_tease(_get(client, "/api/v1/dcpi/scores/ashburn", DEV_KEY),
                  "/api/v1/dcpi/scores/ashburn")


@pytest.mark.parametrize("key,locked", [(None, True), (FREE_KEY, True), (DEV_KEY, False)])
def test_the_png_card_draws_the_scores_only_for_a_paid_caller(client, monkeypatch, key, locked):
    """A PNG cannot be grepped, so read what the renderer was asked to draw."""
    from PIL import ImageDraw
    drawn, real = [], ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **k):
        drawn.append(str(text))
        return real(self, xy, text, *a, **k)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    r = _get(client, "/dcpi/og/ashburn.png", key)
    assert r.status_code == 200 and r.mimetype == "image/png"
    scores = {"45", "55", "32mo"}
    if locked:
        assert "Locked" in drawn and not scores & set(drawn), drawn
    else:
        assert scores <= set(drawn), drawn


# ── what the tease keeps ─────────────────────────────────────────────────────

def test_the_tease_keeps_names_verdicts_and_coarse_coordinates(client):
    b = _get(client, "/api/v1/dcpi/scores/ashburn").get_json()
    assert (b["market_slug"], b["market_name"], b["iso"], b["state"], b["verdict"]) == \
        ("ashburn", "Ashburn", "PJM", "VA", "AVOID")
    assert (b["latitude"], b["longitude"]) == (39.02, -77.47)            # 2 dp
    for k in ("excess_power_score", "constraint_score", "time_to_power_months",
              "composite_score", "queue_capacity_mw", "gen_additions_12mo_mw",
              "avg_kwh_cents", "trend_30d", "top_risks_json"):
        assert b[k] is None and k in b["_locked_fields"], k
    assert "narrative" not in b
    assert b["forecast"] == {"available": False, "reason": "locked_in_preview"}

    cur = _get(client, "/api/v1/data/dcpi-current.json").get_json()
    assert [r["verdict"] for r in cur["snapshot"]] == ["CAUTION", "AVOID", "AVOID"]
    assert cur["_total_available"] == len(SCORE_ROWS) and cur["count"] == 3

    ch = _get(client, "/api/v1/dcpi/changes").get_json()
    assert ch["changes"][0]["verdict_change"] == {"from": "CAUTION", "to": "AVOID"}
    assert ch["count"] == ch["_total_available"] == len(SCORE_ROWS)

    ent = _get(client, "/markets/dallas.json").get_json()
    kept = {m["name"]: m["value"] for m in ent["variableMeasured"]}
    assert kept["Facilities"] == 386 and kept["DCPI Verdict"] == "CAUTION"
    assert kept["Total Capacity"] is None and kept["Time to Power"] is None


def test_the_csv_tease_keeps_the_header_and_verdicts(client):
    r = _get(client, "/data/dcpi-history.csv")
    rows = _csv_rows(r)
    for line in rows:
        cells = line.split(",")
        assert cells[5:8] == ["", "", ""], line                          # numbers empty
        assert cells[8] in ("CAUTION", "AVOID"), line                    # verdict kept
    assert "dcpi-history-preview.csv" in r.headers["Content-Disposition"]


def test_the_page_tease_trips_no_self_heal_detector(client):
    """dchub_self_heal fetches three /dcpi/<slug> pages keyless; the tease must
    not read there as a broken page (an em-dash data cell, 'None · None')."""
    import dchub_self_heal as sh
    body = _get(client, "/dcpi/ashburn").get_data(as_text=True)
    hits = [name for name, pat in sh.HTML_BAD_PATTERNS.items()
            if (pat.search(body) if hasattr(pat, "search") else pat in body)]
    assert not hits, hits
    # detect() also returns the periodic *_tick entries, which fire on any page
    # that says "DCPI"; the other patterns are the ones that mean "broken".
    content = {p["name"] for p in sh.PATTERNS if not p["name"].endswith("_tick")}
    assert "dcpi_none_iso_state_alt" in content
    assert not [h for h in sh.detect(body, 200) if h[0] in content]


def test_a_tease_carrying_a_partner_ref_is_never_shared(client):
    """The envelope's ladder is plan_or_pack_wall's: for a keyless caller from a
    declared partner egress its links carry a ref minted for that one request,
    so that one body must stay out of every shared cache."""
    r = _get(client, "/api/v1/dcpi/scores/ashburn", ip=PARTNER_IP)
    body = _assert_tease_status_only(r)
    refs = {_plan_of(u)[1] for u in [body["upgrade_url"]] +
            [o["url"] for o in body["upgrade_options"]]}
    assert len(refs) == 1 and refs.pop().startswith("a-"), refs
    assert "no-store" in r.headers["Cache-Control"]
    plain = _get(client, "/api/v1/dcpi/scores/ashburn")
    assert {_plan_of(plain.get_json()["upgrade_url"])[1]} == {""}
    assert "no-store" not in plain.headers["Cache-Control"]


def _assert_tease_status_only(r):
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["_gated"] is True and not _leaks(r.get_data(as_text=True))
    return body


def test_a_tease_is_the_same_body_for_every_caller_so_it_may_be_cached(client):
    a = _get(client, "/api/v1/dcpi/scores/ashburn").get_data()
    b = _get(client, "/api/v1/dcpi/scores/ashburn", FREE_KEY).get_data()
    assert a == b


# ── main.py's handlers, pulled out with ast ──────────────────────────────────

@functools.lru_cache(maxsize=1)
def _main_tree():
    return ast.parse((ROOT / "main.py").read_text())


def _main_function(name):
    found = [n for n in _main_tree().body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, (name, len(found))
    node = copy.deepcopy(found[0])
    node.decorator_list = []
    return node


def _free_names(node):
    loads, bound = set(), set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            (loads if isinstance(x.ctx, ast.Load) else bound).add(x.id)
        elif isinstance(x, (ast.FunctionDef, ast.Lambda)):
            if isinstance(x, ast.FunctionDef):
                bound.add(x.name)
        elif isinstance(x, ast.arg):
            bound.add(x.arg)
        elif isinstance(x, ast.alias):
            bound.add((x.asname or x.name).split(".")[0])
        elif isinstance(x, ast.ExceptHandler) and x.name:
            bound.add(x.name)
    return loads - bound - set(dir(builtins))


class CompareCursor:
    STATS = {"Dallas": (308, 3690.0, 65.9, 300.0, 107, 248, 60),
             "Ashburn": (339, 6964.0, 60.6, 500.0, 73, 304, 28)}

    def __init__(self):
        self._rows, self._city = [], None

    def execute(self, sql, params=()):
        self._city = params[0] if params else None
        if "COUNT(*) as facility_count" in sql:
            self._rows = [self.STATS[self._city]]
        else:
            self._rows = [("Digital Realty", 17), ("Equinix", 9)]

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)


class CompareConn:
    def cursor(self):
        return CompareCursor()

    def close(self):
        pass


@pytest.fixture
def main_client(ledger, monkeypatch):
    import psycopg2
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    # In the query's own order, excess_power_score DESC: a preview that took the
    # first rows would rank markets by the score it hides.
    rich_rows = [(s, n, st, iso, c, e, v, 11.65, 100, "lite-pro", NOW)
                 for s, n, st, iso, e, c, t, v in sorted(SCORE_ROWS, key=lambda r: -r[4])]

    class RichCursor(FakeCursor):
        def execute(self, sql, params=None):
            self._rows = ([(len(SCORE_ROWS),)] if "COUNT(*)" in sql else list(rich_rows))
    class RichConn(FakeConn):
        def cursor(self, cursor_factory=None):
            return RichCursor(None, False)
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: RichConn(None))

    ns = {"request": flask.request, "jsonify": flask.jsonify, "logger": logging.getLogger("t"),
          "get_read_db": lambda: CompareConn(), "RAILWAY_EXCLUSION": "",
          "MARKET_ALIASES": {"dallas": ["Dallas"], "ashburn": ["Ashburn"]},
          "build_market_universe": lambda c: [], "utc_iso_z": lambda: "2026-09-21T12:00:00Z"}
    nodes = [_main_function("compare_markets"), _main_function("_markets_list_rich")]
    missing = set().union(*(_free_names(n) for n in nodes)) - set(ns)
    assert not missing, f"main.py now reads {sorted(missing)}; supply it"
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    app = flask.Flask("numeric-tease-main")
    app.add_url_rule("/api/v1/markets/compare", "compare", ns["compare_markets"])
    app.add_url_rule("/api/v1/markets/list-rich", "rich", ns["_markets_list_rich"])
    return app.test_client()


MAIN_ROUTES = {"/api/v1/markets/compare?markets=dallas,ashburn": lambda b: b["comparison"],
               "/api/v1/markets/list-rich": lambda b: b["markets"]}


@pytest.mark.parametrize("path", list(MAIN_ROUTES))
def test_markets_keyless_gets_counts_and_verdicts_only(main_client, ledger, path):
    """FAILS on main: MW, scores and cents per kWh answered keyless."""
    r = _get(main_client, path)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    text = json.dumps(body)
    assert not _leaks(text), _leaks(text)
    rows = MAIN_ROUTES[path](body)
    assert 1 <= len(rows) <= 3
    assert body["_gated"] is True and body["_preview_only"] is True
    assert _plan_of(body["upgrade_url"])[0] == "metered"
    assert not _locked_values(body), _locked_values(body)[:3]
    if "compare" in path:
        m = rows[0]["metrics"]
        assert m["total_power_mw"] is None and m["facilities"] == 308     # counts kept
        assert rows[0]["top_providers"] == ["Digital Realty", "Equinix"]
    else:
        assert [r["slug"] for r in rows] == ["abilene", "akron", "albany"]  # slug order
        assert all(r["verdict"] and r["excess_power_score"] is None for r in rows)
        assert body["_total_available"] == len(SCORE_ROWS)


@pytest.mark.parametrize("path", list(MAIN_ROUTES))
@pytest.mark.parametrize("who", ["dev", "pack", "internal", "admin"])
def test_markets_paid_callers_get_everything(main_client, ledger, path, who):
    key = {"dev": DEV_KEY, "pack": PACK_KEY}.get(who)
    headers = {"internal": {"X-Internal-Key": SECRET},
               "admin": {"X-Admin-Key": ADMIN}}.get(who)
    r = _get(main_client, path, key, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert _leaks(r.get_data(as_text=True))
    assert not r.get_json().get("_gated")
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert ledger["burns"] == ([(PACK_KEY, 1)] if who == "pack" else [])
