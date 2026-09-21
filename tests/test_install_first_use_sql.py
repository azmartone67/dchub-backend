"""/api/v1/ops/install-stats/first-use against a REAL Postgres.

Skips without INSTALL_FIRST_USE_SQL_DSN. The db-parity job in pre-merge.yml sets
it and then FAILS if this file skipped — a skipped SQL proof proves nothing.

Every table is built from the DDL its production writer runs, never retyped:
  mcp_dev_keys     dchub-mcp-v2.1/migration_001_api_keys.sql
  mcp_call_log     migration_001_api_keys.sql + migrations/2026-05-25_funnel_instrumentation.sql
  api_endpoint_log routes/api_usage_tracker._SCHEMA
  api_usage_meter  the CREATE in routes/stripe_metered._ensure_table
All of it lives in a private schema, so nothing another SQL test owns is touched.

Every arm of the SQL has a POSITIVE control row below — a zero is only evidence
if the same query can return non-zero.
"""
import ast
import datetime
import json
import os
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

import routes.install_stats as ist  # noqa: E402
from routes.api_usage_tracker import _SCHEMA as _TRACKER_SCHEMA  # noqa: E402
from routes.api_usage_tracker import STORED_PREFIX_LEN, TRACKED_KEY_PREFIX  # noqa: E402

DSN = os.environ.get("INSTALL_FIRST_USE_SQL_DSN")
SCHEMA = "install_first_use_t"
FAR_ZONE = "Pacific/Kiritimati"   # UTC+14: a zone-dependent date would move

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _meter_ddl():
    """The CREATE TABLE string inside stripe_metered._ensure_table()."""
    tree = ast.parse(_read("routes/stripe_metered.py"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_ensure_table":
            for n in ast.walk(fn):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and "CREATE TABLE IF NOT EXISTS api_usage_meter" in n.value):
                    return n.value
    raise AssertionError("stripe_metered._ensure_table no longer creates api_usage_meter")


def _dev_keys_ddl():
    src = _read("dchub-mcp-v2.1/migration_001_api_keys.sql")
    m = re.search(r"CREATE TABLE IF NOT EXISTS mcp_dev_keys \(.*?\n\);", src, re.S)
    assert m, "mcp_dev_keys DDL moved"
    return m.group(0)


def _call_log_ddl():
    src = _read("migration_001_api_keys.sql")
    m = re.search(r"CREATE TABLE IF NOT EXISTS mcp_call_log \(.*?\n\);", src, re.S)
    assert m, "mcp_call_log DDL moved"
    return m.group(0) + "\n" + _read("migrations/2026-05-25_funnel_instrumentation.sql")


# ── keys: (client_name, api_key, mint offset) ───────────────────────────────
# Mints sit at 01:00 UTC on a day N days back, so hour offsets below never cross
# midnight and the day figures do not depend on when the suite runs.
def _k(n):
    return "dch_live_" + ("%032x" % n)


TRACKED_KEY = TRACKED_KEY_PREFIX + "t" * 30
KEYS = {
    "web-mcp":          (_k(1), 3),
    "web-bulk":         (_k(2), 10),
    "web-both":         (_k(3), 5),
    "web-issued":       (_k(4), 2),
    "web-probeua":      (_k(5), 4),
    "web-selfsession":  (_k(6), 4),
    "web-qaplatform":   (_k(7), 4),
    "web-unclassified": (_k(8), 6),
    "web-meter":        (_k(9), 40),
    "web-tracked":      (TRACKED_KEY, 20),
    "web-never":        (_k(11), 1),
    "web-nullevent":    (_k(12), 8),
    "install-claude":   (_k(20), 6),
    "install-verify-first-use": (_k(30), 1),
    "claude-code":      (_k(40), 2),        # outside every population
}
REAL_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


def _mint(cur, client, key, days_back):
    cur.execute(
        "INSERT INTO mcp_dev_keys (api_key, developer_id, tier, status, metadata, created_at) "
        "VALUES (%s, %s, 'free', 'active', %s::jsonb, "
        "        date_trunc('day', NOW()) - make_interval(days => %s) + interval '1 hour')",
        (key, "dev_" + client, json.dumps({"client_name": client}), days_back))


def _call(cur, client, hours_after_mint, event_type, ua=REAL_UA, session="s-real",
          platform=None, at_sql=None):
    key = KEYS[client][0]
    at = at_sql or ("(SELECT created_at FROM mcp_dev_keys WHERE api_key = %s) "
                    "+ make_interval(hours => %s)")
    args = (key, hours_after_mint) if at_sql is None else ()
    cur.execute(
        "INSERT INTO mcp_call_log (timestamp, tool, api_key, session_id, status, "
        "  platform, user_agent, event_type) "
        f"VALUES ({at}, 'get_news', %s, %s, 'ok', %s, %s, %s)",
        args + (key, session, platform, ua, event_type))


def _seed(cur):
    for client, (key, days) in KEYS.items():
        _mint(cur, client, key, days)
    _mint(cur, "", _k(50), 3)   # no client_name at all
    cur.execute("INSERT INTO mcp_dev_keys (api_key, developer_id, created_at) "
                "VALUES (%s, 'dev_none', NOW() - interval '3 days')", (_k(51),))

    _call(cur, "web-mcp", 2, "tool_call", platform="claude")
    _call(cur, "web-bulk", 26, "bulk:FREE")
    _call(cur, "web-both", 1, "key_first_use")
    _call(cur, "web-both", 50, "tool_error")
    _call(cur, "web-issued", 0, "key_issued")
    _call(cur, "web-probeua", 1, "tool_call", ua="dchub-selfheal/1.0")
    _call(cur, "web-selfsession", 1, "tool_call", session="88e20dac-0000-0000")
    _call(cur, "web-qaplatform", 1, "tool_call", platform="dchub-internal")
    _call(cur, "web-unclassified", 1, "key_first_paid_tool")
    _call(cur, "web-nullevent", 3, None)
    _call(cur, "install-claude", 1, "tool_call")
    _call(cur, "claude-code", 1, "tool_call")
    # Our probe: used on BOTH channels, self-tagged, the REST call settled.
    probe_ua = "dchub-install-verify/1.0"
    _call(cur, "install-verify-first-use", 1, "tool_call", ua=probe_ua)
    _call(cur, "install-verify-first-use", 0, "bulk:FREE", ua=probe_ua,
          at_sql="NOW() - interval '20 minutes'")

    # api_endpoint_log: the tracker's 24-char prefix, only ever for a dchub_ key.
    cur.execute(
        "INSERT INTO api_endpoint_log (called_at, api_key_prefix, endpoint_path, status) "
        "SELECT created_at + interval '5 hours', LEFT(api_key, %s), '/api/v1/facilities', 200 "
        "  FROM mcp_dev_keys WHERE api_key = %s", (STORED_PREFIX_LEN, TRACKED_KEY))
    # api_usage_meter: the tracker's prefix row, and a /track-usage FULL-key row.
    cur.execute(
        "INSERT INTO api_usage_meter (api_key, tier, usage_date, calls_count) "
        "SELECT LEFT(api_key, %s), 'free', (created_at AT TIME ZONE 'UTC')::date, 1 "
        "  FROM mcp_dev_keys WHERE api_key = %s", (STORED_PREFIX_LEN, TRACKED_KEY))
    cur.execute(
        "INSERT INTO api_usage_meter (api_key, tier, usage_date, calls_count) "
        "SELECT api_key, 'free', (created_at AT TIME ZONE 'UTC')::date + 2, 1 "
        "  FROM mcp_dev_keys WHERE api_key = %s", (KEYS["web-meter"][0],))


@pytest.fixture(scope="module")
def db():
    if not DSN:
        pytest.skip("INSTALL_FIRST_USE_SQL_DSN not set")
    conn = psycopg2.connect(DSN, options="-c TimeZone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(_dev_keys_ddl())
        cur.execute(_call_log_ddl())
        cur.execute(_TRACKER_SCHEMA)
        cur.execute(_meter_ddl())
        _seed(cur)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


_REAL_CONNECT = psycopg2.connect   # captured before any test patches it


def _connect(zone="UTC"):
    return _REAL_CONNECT(DSN, options=f"-c TimeZone={zone} -c search_path={SCHEMA}")


def _records(db, prefix, exclude=ist._PROBE_PREFIX, zone="UTC"):
    c = _connect(zone)
    try:
        with c.cursor() as cur:
            rows = ist._first_use(cur, prefix, exclude)
    finally:
        c.close()
    return {r["client"]: r for r in map(ist._key_record, rows)}


# ── the fixture is what it claims ───────────────────────────────────────────

def test_the_tables_were_built_from_the_writers_ddl(db):
    with db.cursor() as cur:
        cur.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s", (SCHEMA,))
        cols = {(t, c): d for t, c, d in cur.fetchall()}
    assert cols[("mcp_dev_keys", "created_at")] == "timestamp with time zone"
    assert cols[("mcp_call_log", "timestamp")] == "timestamp with time zone"
    assert ("mcp_call_log", "event_type") in cols and ("mcp_call_log", "user_agent") in cols
    assert cols[("api_endpoint_log", "api_key_prefix")] == "text"
    assert cols[("api_usage_meter", "usage_date")] == "date"


# ── every arm can return non-zero ───────────────────────────────────────────

def test_each_arm_returns_non_zero_for_its_positive_control(db):
    web = _records(db, "web-%")
    assert web["web-mcp"]["mcp"]                        # mcp_call_log, MCP
    assert web["web-bulk"]["rest_in_call_log"]          # mcp_call_log, REST bulk
    assert web["web-both"]["rest_in_call_log"]          # mcp_call_log, onboarding
    assert web["web-tracked"]["in_endpoint_log"]        # api_endpoint_log, prefix join
    assert web["web-tracked"]["in_meter"]               # api_usage_meter, prefix row
    assert web["web-meter"]["in_meter"]                 # api_usage_meter, full-key row
    assert web["web-nullevent"]["mcp"]                  # pre-r47 NULL event_type


# ── per-key facts ───────────────────────────────────────────────────────────

def test_per_key_channel_grain_and_timing(db):
    web = _records(db, "web-%")
    assert set(web) == {c for c in KEYS if c.startswith("web-")}
    want = {
        #  client            mcp    rest   grain   hours  days
        "web-mcp":          (True,  False, "call", 2.0,  0),
        "web-bulk":         (False, True,  "call", 26.0, 1),
        "web-both":         (True,  True,  "call", 1.0,  0),
        "web-issued":       (False, False, None,   None, None),
        "web-probeua":      (False, False, None,   None, None),
        "web-selfsession":  (False, False, None,   None, None),
        "web-qaplatform":   (False, False, None,   None, None),
        "web-unclassified": (False, False, None,   None, None),
        "web-meter":        (False, True,  "day",  None, 2),
        "web-tracked":      (False, True,  "call", 5.0,  0),
        "web-never":        (False, False, None,   None, None),
        "web-nullevent":    (True,  False, "call", 3.0,  0),
    }
    got = {c: (r["mcp"], r["rest"], r["grain"],
               None if r["hours"] is None else round(r["hours"], 6), r["days"])
           for c, r in web.items()}
    assert got == want
    assert web["web-issued"]["not_use_rows"] == 1
    assert web["web-unclassified"]["unclassified_rows"] == 1
    for c in ("web-probeua", "web-selfsession", "web-qaplatform"):
        assert web[c]["mcp_any_row"] and not web[c]["mcp"], c
    assert [c for c, r in web.items() if r["tracker_sees_format"]] == ["web-tracked"]


def test_the_probe_is_out_of_the_population_and_in_the_control(db):
    inst = _records(db, "install-%")
    assert set(inst) == {"install-claude"}
    ctl = _records(db, ist._PROBE_PREFIX, ist._EXCLUDE_NOTHING)
    p = ctl["install-verify-first-use"]
    # its own rows fail the UA rule, so they are never USE ...
    assert (p["mcp"], p["rest"]) == (False, False)
    # ... but the any-row columns see both channels, and the REST call settled
    assert (p["mcp_any_row"], p["rest_any_row"], p["rest_settled"]) == (True, True, True)
    assert not p["in_endpoint_log"] and not p["tracker_sees_format"]


def test_results_do_not_depend_on_the_session_time_zone(db):
    assert _records(db, "web-%", zone=FAR_ZONE) == _records(db, "web-%")


# ── the route, end to end, on this database ─────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setattr(ist.psycopg2, "connect",
                        lambda *a, **k: _connect(FAR_ZONE))
    app = flask.Flask(__name__)
    ist.register_install_stats(app)
    return app.test_client()


def test_the_route_publishes_the_measured_figures(client):
    body = client.get("/api/v1/ops/install-stats/first-use").get_json()
    assert body["ok"] is True and body["label"] == "probe-excluded"
    web = body["populations"]["web-%"]
    w = {k: {f: web["windows"][k][f] for f in (
            "minted", "first_use_any", "mcp_only", "rest_only", "both", "never_used")}
         for k in ("7d", "30d", "all_time")}
    assert w == {
        "7d":       {"minted": 8,  "first_use_any": 2, "mcp_only": 1, "rest_only": 0, "both": 1, "never_used": 6},
        "30d":      {"minted": 11, "first_use_any": 5, "mcp_only": 2, "rest_only": 2, "both": 1, "never_used": 6},
        "all_time": {"minted": 12, "first_use_any": 6, "mcp_only": 2, "rest_only": 3, "both": 1, "never_used": 6},
    }
    t = web["windows"]["all_time"]["time_to_first_use"]
    assert t["hours_call_grain"] == {"keys": 5, "median": 3.0, "max": 26.0}
    assert t["day_grain_only_keys"] == 1 and t["days"]["keys"] == 6
    assert web["excluded_by_row_rules"]["keys_whose_only_mcp_rows_were_probe_rows"] == 3
    assert web["mcp_call_log_rows_not_counted_as_use"] == {"not_use_rows": 1, "unclassified_rows": 1}
    assert web["rest_visibility"] == {
        "keys": 12, "keys_in_a_format_the_rest_tracker_records": 1,
        "keys_seen_in_api_endpoint_log": 1, "keys_seen_in_api_usage_meter": 2,
        "keys_with_rest_rows_in_mcp_call_log": 2}
    inst = body["populations"]["install-%"]["windows"]["all_time"]
    assert (inst["minted"], inst["mcp_only"]) == (1, 1)
    assert body["control"]["keys"] == 1
    assert body["control"]["keys_with_rest_rows_in_mcp_call_log"] == 1
    # three keyed REST requests reached the origin (web-bulk, web-both's
    # onboarding POST, the probe's bulk call) and the tracker holds none
    el = body["instrument"]["api_endpoint_log"]
    assert (el["verdict"], el["keys_known_to_have_made_a_rest_request"],
            el["of_those_seen_here"], el["keys_with_rows_here"]) == ("blind", 3, 0, 1)
    assert body["instrument"]["mcp_call_log.mcp"]["verdict"] == "live"
    assert body["evidence_status_claims"]["rest_tracker_cannot_see_these_keys"]["status"] == "observed"
    assert "FLOOR" in web["reading"]
    text = json.dumps(body)
    for key, _ in KEYS.values():
        assert key not in text and key[:STORED_PREFIX_LEN] not in text, (
            "a key leaked into the public body")
