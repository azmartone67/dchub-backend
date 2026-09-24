"""The REST usage tracker's self-serve (dch_live_) rows, against a REAL Postgres.

Skips without USAGE_TRACKER_SQL_DSN. The db-parity job in pre-merge.yml sets it
and then FAILS if this file skipped — a skipped SQL proof proves nothing.

Everything runs through production code. Requests go through the tracker's real
before/after_request hooks and its real _flush(). Billing is read by the real
stripe_metered routes, and first use by the real install-stats route. Tables
come from the DDL their writers run, never retyped:
  mcp_dev_keys, mcp_call_log, api_usage_meter  as tests/test_install_first_use_sql.py
  api_endpoint_log  routes/api_usage_tracker._SCHEMA
  metered_keys      the CREATE in routes/stripe_metered._ensure_metered_keys
  api_keys          db_persistence.CRITICAL_TABLES['api_keys'] (the flush reads it)
All of it lives in a private schema, dropped afterwards.
"""
import ast
import json
import os
import secrets
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

import routes.api_usage_tracker as tracker  # noqa: E402
import routes.install_stats as ist  # noqa: E402
import routes.stripe_metered as billing  # noqa: E402
from tests.test_install_first_use_contract import _claim_key_literal_prefix  # noqa: E402
from tests.test_install_first_use_sql import (  # noqa: E402
    _call_log_ddl,
    _dev_keys_ddl,
    _meter_ddl,
    _read,
)

DSN = os.environ.get("USAGE_TRACKER_SQL_DSN")
SCHEMA = "usage_tracker_self_serve_t"
N = tracker.STORED_PREFIX_LEN
ADMIN = "test-admin-" + secrets.token_hex(8)
REAL_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


def _string_in(rel, fn_name, needle):
    for fn in ast.walk(ast.parse(_read(rel))):
        if isinstance(fn, ast.FunctionDef) and fn.name == fn_name:
            for n in ast.walk(fn):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and needle in n.value:
                    return n.value
    raise AssertionError(f"{rel}:{fn_name} no longer contains {needle!r}")


def _api_keys_ddl():
    for n in ast.parse(_read("db_persistence.py")).body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CRITICAL_TABLES" for t in n.targets):
            cols = ast.literal_eval(n.value)["api_keys"]["columns"]
            return "CREATE TABLE api_keys (%s)" % ", ".join("%s %s" % c for c in cols)
    raise AssertionError("db_persistence.CRITICAL_TABLES moved")


# Keys in the claim handler's own shape. The metered one is what the usage-based
# checkout mints (same literal) and enrols in metered_keys.
_SHAPE = _claim_key_literal_prefix()
KEYS = {
    "web-map-xkey":   _SHAPE + secrets.token_hex(16),   # js/map.js: X-API-Key
    "web-map-bearer": _SHAPE + secrets.token_hex(16),   # map.html: Bearer
    "web-map-mcp":    _SHAPE + secrets.token_hex(16),   # used through the MCP server only
    "install-verify-first-use": _SHAPE + secrets.token_hex(16),  # our probe
}
METERED = _SHAPE + secrets.token_hex(16)
CUSTOMER = "cus_test_" + secrets.token_hex(4)

# (headers, path) — each one request through the real hooks.
REQUESTS = [
    ({"X-API-Key": KEYS["web-map-xkey"]}, "/api/v1/facilities"),
    ({"Authorization": "Bearer " + KEYS["web-map-bearer"]}, "/api/auth/me"),
    # the MCP server's callAPI() fan-out for a tool call: X-Internal-Key + the key
    ({"X-API-Key": KEYS["web-map-mcp"], "X-Internal-Key": "k",
      "X-MCP-Platform": "claude", "X-MCP-Session": "s-1"}, "/api/v1/facilities"),
    ({"X-API-Key": METERED}, "/api/v1/facilities"),
    ({"X-API-Key": METERED}, "/api/v1/facilities"),
    ({"X-API-Key": KEYS["install-verify-first-use"]}, "/api/v1/market-brief/all"),
]


def _scoped_dsn():
    return DSN + ("&" if "?" in DSN else "?") + "options=" + quote("-c search_path=" + SCHEMA)


_REAL_CONNECT = psycopg2.connect   # captured before any test patches it


def _connect():
    return _REAL_CONNECT(_scoped_dsn())


def _rows(sql, args=()):
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        c.close()


def _exec(sql, args=()):
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _instrument_now():
    c = _connect()
    try:
        with c.cursor() as cur:
            recs = [ist._key_record(r) for p in ist._FIRST_USE_POPULATIONS
                    for r in ist._first_use(cur, p)]
            ctl = [ist._key_record(r)
                   for r in ist._first_use(cur, ist._PROBE_PREFIX, ist._EXCLUDE_NOTHING)]
    finally:
        c.close()
    return ist._instrument(recs + ctl, ctl)


@pytest.fixture(scope="module")
def recorded():
    """Build the tables, drive REQUESTS through the tracker, flush them."""
    if not DSN:
        pytest.skip("USAGE_TRACKER_SQL_DSN not set")
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(_dev_keys_ddl())
        cur.execute(_call_log_ddl())
        cur.execute(tracker._SCHEMA)
        cur.execute(_meter_ddl())
        cur.execute(_string_in("routes/stripe_metered.py", "_ensure_metered_keys",
                               "CREATE TABLE IF NOT EXISTS metered_keys"))
        cur.execute(_api_keys_ddl())
        for client, key in KEYS.items():
            cur.execute("INSERT INTO mcp_dev_keys (api_key, developer_id, tier, metadata, created_at) "
                        "VALUES (%s, %s, 'free', %s::jsonb, NOW() - interval '2 days')",
                        (key, "dev_" + client, json.dumps({"client_name": client})))
        cur.execute("INSERT INTO mcp_dev_keys (api_key, developer_id, tier, metadata) "
                    "VALUES (%s, 'dev_metered', 'paid', %s::jsonb)",
                    (METERED, json.dumps({"source": "usage_based_checkout"})))
        cur.execute("INSERT INTO metered_keys (api_key, stripe_customer_id, last_reported_at) "
                    "VALUES (%s, %s, NOW() - interval '1 hour')", (METERED, CUSTOMER))
        # what the MCP track callback writes for web-map-mcp's tool call, and the
        # row market_brief logs for the probe's bulk call (settled: 20 min old)
        cur.execute("INSERT INTO mcp_call_log (timestamp, tool, api_key, session_id, status, "
                    "  user_agent, event_type) VALUES "
                    "(NOW() - interval '1 day', 'get_news', %s, 's-real', 'ok', %s, 'tool_call'), "
                    "(NOW() - interval '20 minutes', 'bulk', %s, 's-real', 'ok', %s, 'bulk:FREE')",
                    (KEYS["web-map-mcp"], REAL_UA, KEYS["install-verify-first-use"], REAL_UA))

    saved = (tracker._ensure_schema, tracker._ensure_flusher_running,
             os.environ.get("DATABASE_URL"))
    tracker._ensure_schema = lambda: None
    tracker._ensure_flusher_running = lambda: None
    os.environ["DATABASE_URL"] = _scoped_dsn()
    try:
        before = _instrument_now()
        app = flask.Flask(__name__)
        for rule in {p for _, p in REQUESTS}:
            app.add_url_rule(rule, rule, lambda: "ok")
        tracker.install_tracker(app)
        with tracker._BUFFER_LOCK:
            tracker._BUFFER.clear()
        client = app.test_client()
        for headers, path in REQUESTS:
            assert client.get(path, headers=headers).status_code == 200
        flushed = tracker._flush()
        yield {"before": before, "flushed": flushed}
    finally:
        tracker._ensure_schema, tracker._ensure_flusher_running, db_url = saved
        if db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = db_url
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


# ── the writer ──────────────────────────────────────────────────────────────

def test_the_flush_wrote_every_recorded_request(recorded):
    assert recorded["flushed"].get("flushed") == len(REQUESTS), recorded["flushed"]


def test_rest_use_by_a_self_serve_key_lands_under_its_prefix_and_its_own_tier(recorded):
    log = dict(_rows("SELECT api_key_prefix, COUNT(*) FROM api_endpoint_log GROUP BY 1"))
    assert log == {
        KEYS["web-map-xkey"][:N]: 1,
        KEYS["web-map-bearer"][:N]: 1,
        METERED[:N]: 2,
        KEYS["install-verify-first-use"][:N]: 1,
        "internal": 1,                       # the MCP fan-out, as before this change
    }
    meter = {k: (t, n) for k, t, n in _rows(
        "SELECT api_key, tier, calls_count FROM api_usage_meter")}
    assert meter == {
        KEYS["web-map-xkey"][:N]: ("free", 1),
        KEYS["web-map-bearer"][:N]: ("free", 1),
        METERED[:N]: ("paid", 2),            # its registry tier — never a default
        KEYS["install-verify-first-use"][:N]: ("free", 1),
        "internal": ("internal", 1),
    }


def test_the_mcp_fan_out_never_lands_under_the_key(recorded):
    p = KEYS["web-map-mcp"][:N]
    assert _rows("SELECT COUNT(*) FROM api_endpoint_log WHERE api_key_prefix = %s", (p,)) == [(0,)]
    assert _rows("SELECT COUNT(*) FROM api_usage_meter WHERE api_key = %s", (p,)) == [(0,)]


def test_no_table_holds_a_full_key(recorded):
    held = {r[0] for r in _rows("SELECT api_key_prefix FROM api_endpoint_log "
                                "UNION SELECT api_key FROM api_usage_meter")}
    assert all(len(v) <= N for v in held), held


# ── billing: the meter rows must not reach Stripe or a customer's usage ─────

@pytest.fixture
def billing_client(recorded, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    for var in ("STRIPE_API_KEY", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)          # dry run, by the route's own rule
    app = flask.Flask(__name__)
    app.register_blueprint(billing.stripe_metered_bp)
    return app.test_client()


def _report(client):
    body = client.post("/api/v1/billing/report-to-stripe",
                       headers={"X-Admin-Key": ADMIN}).get_json()
    assert body["mode"] == "dry_run", body
    mine = [r for r in body["results"] if r["customer"] == CUSTOMER]
    assert len(mine) == 1, body
    return mine[0]


def test_stripe_reporting_bills_none_of_the_tracker_rows(billing_client):
    assert _rows("SELECT calls_count FROM api_usage_meter WHERE api_key = %s",
                 (METERED[:N],)) == [(2,)]          # the rows exist ...
    r = _report(billing_client)
    assert (r["calls"], r["status"]) == (0, "no_new_usage")   # ... and are not billed
    # Positive control: one real MCP call for the same key IS counted.
    _exec("INSERT INTO mcp_call_log (timestamp, tool, api_key, status, event_type) "
          "VALUES (NOW() - interval '1 minute', 'get_news', %s, 'ok', 'tool_call')", (METERED,))
    try:
        r = _report(billing_client)
        assert (r["calls"], r["status"]) == (1, "dry_run")
    finally:
        _exec("DELETE FROM mcp_call_log WHERE api_key = %s", (METERED,))


def test_a_customers_usage_display_reads_none_of_them(billing_client):
    def mtd():
        body = billing_client.get("/api/v1/billing/usage",
                                  headers={"X-API-Key": METERED}).get_json()
        return body["current_month"]["calls_mtd"]
    assert mtd() == 0
    # Positive control: a FULL-key row, as POST /track-usage writes one, is read.
    _exec("INSERT INTO api_usage_meter (api_key, tier, usage_date, calls_count) "
          "VALUES (%s, 'developer', CURRENT_DATE, 5)", (METERED,))
    try:
        assert mtd() == 5
    finally:
        _exec("DELETE FROM api_usage_meter WHERE api_key = %s", (METERED,))


# ── the reader: install-stats first use ─────────────────────────────────────

@pytest.fixture
def first_use(recorded, monkeypatch):
    monkeypatch.setattr(ist.psycopg2, "connect", lambda *a, **k: _connect())
    app = flask.Flask(__name__)
    ist.register_install_stats(app)
    return app.test_client().get("/api/v1/ops/install-stats/first-use").get_json()


def test_the_instrument_leaves_blind_once_a_keyed_rest_request_is_recorded(recorded):
    before = recorded["before"]["api_endpoint_log"]
    assert (before["verdict"], before["keys_known_to_have_made_a_rest_request"],
            before["of_those_seen_here"]) == ("blind", 1, 0)
    after = _instrument_now()["api_endpoint_log"]
    assert (after["verdict"], after["keys_known_to_have_made_a_rest_request"],
            after["of_those_seen_here"]) == ("live", 1, 1)
    assert after["first_row_at"] is not None
    assert _instrument_now()["api_usage_meter"]["verdict"] == "live"


def test_first_use_counts_rest_for_self_serve_keys_and_mcp_as_mcp(first_use):
    assert first_use["ok"] is True
    web = first_use["populations"]["web-%"]
    w = web["windows"]["all_time"]
    assert (w["minted"], w["first_use_any"], w["mcp_only"], w["rest_only"], w["both"]) == (
        3, 3, 1, 2, 0)
    assert web["rest_visibility"] == {
        "keys": 3, "keys_in_a_format_the_rest_tracker_records": 3,
        "keys_seen_in_api_endpoint_log": 2, "keys_seen_in_api_usage_meter": 2,
        "keys_with_rest_rows_in_mcp_call_log": 0}
    # counted from the tracker's first row on — and a floor for anything earlier
    first_row = first_use["instrument"]["api_endpoint_log"]["first_row_at"]
    assert "FLOOR" not in web["reading"] and "is counted" in web["reading"]
    assert first_row in web["reading"] and "are a floor" in web["reading"]
    assert first_use["instrument"]["api_endpoint_log"]["verdict"] == "live"
    assert first_use["control"]["keys_seen_in_api_endpoint_log"] == 1
    claim = first_use["evidence_status_claims"]["rest_tracker_cannot_see_these_keys"]
    assert claim["status"] == "hypothesis"
    text = json.dumps(first_use)
    for key in list(KEYS.values()) + [METERED]:
        assert key not in text and key[:N] not in text, "a key leaked into the public body"


# ── keyless partner-egress traffic (2026-09-21) ─────────────────────────────
# The tracker also COUNTS keyless requests from a declared partner egress into
# partner_keyless_daily, and flushes the partner pay-link refs
# (routes/partner_attribution) into partner_offer_refs. Only Postgres runs the
# upserts, the day grain and the attribution joins. Private schema, dropped.

PSCHEMA = "partner_keyless_t"
PARTNER_IP = "104.248.242.235"
PARTNER = "anythingmcp/"


def _pscoped_dsn():
    return DSN + ("&" if "?" in DSN else "?") + "options=" + quote("-c search_path=" + PSCHEMA)


def _prows(sql, args=()):
    c = _REAL_CONNECT(_pscoped_dsn())
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        c.close()


def _pexec(sql, args=()):
    c = _REAL_CONNECT(_pscoped_dsn())
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
        c.commit()
    finally:
        c.close()


@pytest.fixture(scope="module")
def partner_schema():
    if not DSN:
        pytest.skip("USAGE_TRACKER_SQL_DSN not set")
    import routes.checkout_payment_refs as payment_refs
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {PSCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {PSCHEMA}")
        cur.execute(f"SET search_path TO {PSCHEMA}")
        cur.execute(tracker._SCHEMA)
        cur.execute(_meter_ddl())
        cur.execute(payment_refs._DDL)
        cur.execute(_string_in("routes/pricing_click_tracker.py", "_ensure_pricing_table",
                               "CREATE TABLE IF NOT EXISTS pricing_checkout_clicks"))
    try:
        yield
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {PSCHEMA} CASCADE")
        admin.close()


@pytest.fixture
def partner_client(partner_schema, monkeypatch):
    import rate_limiter
    import routes.partner_attribution as pa
    monkeypatch.setenv("DATABASE_URL", _pscoped_dsn())
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    monkeypatch.setattr(tracker, "_ensure_schema", lambda: None)
    monkeypatch.setattr(tracker, "_ensure_flusher_running", lambda: None)
    for table in ("partner_keyless_daily", "api_endpoint_log", "api_usage_meter"):
        _pexec(f"TRUNCATE {table}")
    _pexec("DROP TABLE IF EXISTS partner_offer_refs")
    tracker._drain_partner()
    tracker._drain_buffer()
    pa._drain()
    rate_limiter._buckets.clear()
    app = flask.Flask("partner-keyless-sql")
    app.add_url_rule("/api/energy/prices/<state>", "prices", lambda state: "ok")
    app.add_url_rule("/api/v1/pipeline", "pipeline", lambda: ("walled", 403))
    app.before_request(rate_limiter.rate_limit_before)
    tracker.install_tracker(app)
    app.register_blueprint(tracker.api_usage_tracker_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    yield c
    rate_limiter._buckets.clear()


def _partner_get(client, path, n=1):
    for _ in range(n):
        client.get(path, headers={"User-Agent": "node", "CF-Connecting-IP": PARTNER_IP})


def test_partner_keyless_counts_land_per_day_route_and_status_and_sum(partner_client):
    _partner_get(partner_client, "/api/energy/prices/TX", 2)
    _partner_get(partner_client, "/api/v1/pipeline")
    first = tracker._flush_partner()
    assert first["partner_keyless_rows"] == 2, first
    _partner_get(partner_client, "/api/energy/prices/CA")
    tracker._flush_partner()
    rows = _prows("SELECT usage_date = (NOW() AT TIME ZONE 'UTC')::date, partner, method, "
                  "endpoint_rule, status, requests FROM partner_keyless_daily ORDER BY 4, 5")
    assert rows == [(True, PARTNER, "GET", "/api/energy/prices/<state>", 200, 3),
                    (True, PARTNER, "GET", "/api/v1/pipeline", 403, 1)]
    # never in the keyed tables, whose readers assume a key
    assert _prows("SELECT COUNT(*) FROM api_endpoint_log") == [(0,)]
    assert _prows("SELECT COUNT(*) FROM api_usage_meter") == [(0,)]


def test_a_failed_flush_loses_no_count(partner_client, monkeypatch):
    _partner_get(partner_client, "/api/v1/pipeline", 2)
    _pexec("ALTER TABLE partner_keyless_daily RENAME TO partner_keyless_daily_away")
    try:
        out = tracker._flush_partner()
        assert out["partner_keyless_rows"] == 0 and "partner_keyless_error" in out
    finally:
        _pexec("ALTER TABLE partner_keyless_daily_away RENAME TO partner_keyless_daily")
    tracker._flush_partner()
    assert _prows("SELECT requests FROM partner_keyless_daily") == [(2,)]


def test_partner_refs_and_what_they_led_to(partner_client):
    import routes.partner_attribution as pa
    code, other = "DCM-ABC1", "DCM-ABC12"          # a prefix of each other, on purpose
    assert pa.note_offer_ref(code, PARTNER, "pair_code", "/api/v1/pipeline")
    assert pa.note_offer_ref(code, PARTNER, "pair_code", "/api/v1/pipeline")
    assert tracker._flush_partner()["partner_refs"] == {"refs": 1}
    assert pa.note_offer_ref(code, PARTNER, "pair_code", "/api/site-score")
    tracker._flush_partner()
    assert _prows("SELECT partner, kind, first_path, served FROM partner_offer_refs") == [
        (PARTNER, "pair_code", "/api/v1/pipeline", 3)]
    pays = [("cs_1", code, True), ("cs_2", "ref_" + code + "__tool_pipeline__ts_1", None),
            ("cs_3", code, False),                          # test mode: not counted
            ("cs_4", other, True),                          # not a partner ref
            ("cs_5", "ref_" + other + "__tool_x__ts_2", True)]
    for sid, cref, live in pays:
        _pexec("INSERT INTO mcp_checkout_payments (stripe_session_id, client_reference_id, "
               "livemode) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sid, cref, live))
    for ref, known in (("ref_" + code + "__tool_pipeline__ts_3", True),
                       ("ref_" + code + "__tool_pipeline__ts_4", False),
                       ("ref_" + other + "__tool_x__ts_5", True)):
        _pexec("INSERT INTO pricing_checkout_clicks (plan, ref, known_plan) "
               "VALUES ('metered', %s, %s)", (ref, known))
    c = _REAL_CONNECT(_pscoped_dsn())
    try:
        with c.cursor() as cur:
            out = pa.read_attribution(cur, 30)
    finally:
        c.close()
    assert out["refs"] == [{"partner": PARTNER, "kind": "pair_code", "refs": 1, "served": 3}]
    assert out["payments"] == {PARTNER: 2}
    assert out["pricing_clicks"] == {PARTNER: 1}


def test_the_admin_read_returns_the_rollup(partner_client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    _partner_get(partner_client, "/api/energy/prices/TX", 2)
    tracker._flush_partner()
    r = partner_client.get("/api/v1/admin/usage-tracker/partner-traffic?days=7",
                           headers={"X-Admin-Key": ADMIN})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    agg = body["keyless_requests"]["partners"][PARTNER]
    assert agg["total"] == 2
    assert agg["by_route"] == {"GET /api/energy/prices/<state>": 2}
    assert agg["by_status"] == {"200": 2}
    assert body["attribution"]["refs"].startswith("absent")
    assert body["window_days"] == 7 and set(body["basis"]) == {"keyless_requests", "attribution"}
