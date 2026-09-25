"""Key-mint scan guard against a REAL Postgres (r-mint-scan, 2026-09-24).

Skips without MINT_SCAN_SQL_DSN. The db-parity job in pre-merge.yml sets it and
then FAILS if this file skipped — a skipped SQL proof proves nothing.

WHY A DATABASE. Which rows are scan traffic, what "reused" means once the
gate_carry seed is subtracted, and what the per-caller ceiling counts are all
decided in SQL (window functions, FILTER, a regex, interval arithmetic). A stub
cursor answers whatever it is primed with; only Postgres can say what the
queries select.

WHAT IT PINS
  · GET /api/v1/mcp/retention: key_reuse and summary read NON-scan rows, and
    publish excluded_scan_mints / minted_incl_scan beside them;
  · GET /api/v1/funnel/attribution: the "N% retried with their key" figure
    excludes the same rows;
  · a born-gated key (notes gate_carry:N, call_count seeded to N) is not
    "reused" until it is actually called;
  · check_mint_rate counts the right rows on both mint tables;
  · the radar detector fires on the measured spike and not on the baseline.

Every handler and detector here is the production function; only the
connection is pointed at a private DATABASE, dropped afterwards. A database,
not a schema: the funnel and radar readers probe to_regclass('public.
auto_trial_keys'), so the tables must live in `public` — and a shared parity
database's public schema is not this file's to own.
"""
import os
from unittest import mock

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

from routes import mint_guard  # noqa: E402
from routes.auto_trial import _SCHEMA as TRIAL_DDL  # noqa: E402

DSN = os.environ.get("MINT_SCAN_SQL_DSN")
DBNAME = "mint_scan_guard_t"
_REAL_CONNECT = psycopg2.connect

pytestmark = pytest.mark.skipif(not DSN, reason="MINT_SCAN_SQL_DSN not set")

# A complete ISO week two weeks back, 01:00 on its Tuesday: complete (so the
# retention endpoint keeps it), mature (> 7 days old) and inside 30 days,
# whatever day the suite runs.
WEEK_TS = "date_trunc('week', now()) - interval '14 days' + interval '1 day 1 hour'"

N_REAL = 30          # 30 real agents, 30 ip hashes, 1 key each
N_REAL_REUSED = 15   # half of them called their key 3 times
N_SCAN = 500         # one (ip_hash, UA) pair minting 500 keys in one day
N_PROBE = 3          # probe-UA mints
N_HARNESS_LOG = 3    # our MCP harness: UA `node`, calls logged under dchub-internal
N_HARNESS_STAMP = 2  # our MCP harness, tagged at mint (mcp_platform), never called
N_INTERNAL = N_HARNESS_LOG + N_HARNESS_STAMP
# kept: the real agents, the born-gated key, and one external `node` agent
# (a real client whose UA is plain node must NOT be dropped with the harness)
N_KEPT = N_REAL + 2
N_EXCLUDED = N_SCAN + N_PROBE + N_INTERNAL


def _scoped_dsn():
    from psycopg2.extensions import make_dsn, parse_dsn
    return make_dsn(**{**parse_dsn(DSN), "dbname": DBNAME})


def _connect(*a, **k):
    c = _REAL_CONNECT(_scoped_dsn())
    c.autocommit = True
    return c


def _exec(sql, args=None):
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return None
    finally:
        c.close()


@pytest.fixture(scope="module")
def world():
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {DBNAME}")
    admin.close()
    admin = _connect()
    with admin.cursor() as cur:
        cur.execute(TRIAL_DDL)
        cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS mcp_platform TEXT")
        cur.execute('CREATE TABLE mcp_call_log (api_key TEXT, platform TEXT, "timestamp" TIMESTAMPTZ)')
        # the retention endpoint's first (un-guarded) query reads this table
        cur.execute("CREATE TABLE mcp_tool_calls (ip_address TEXT, created_at TIMESTAMPTZ,"
                    " client_name TEXT, platform TEXT)")
        cur.execute("""CREATE TABLE mcp_dev_keys (api_key TEXT PRIMARY KEY,
                        email TEXT, created_at TIMESTAMPTZ DEFAULT NOW(),
                        last_used_at TIMESTAMPTZ, metadata JSONB)""")
        # real agents: one key per ip hash; half used 3 times, a week later
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count, last_used_at)
            SELECT 'dch_trial_real' || g, {WEEK_TS}, 'realip' || g, 'Claude-User',
                   CASE WHEN g <= %s THEN 3 ELSE 1 END,
                   CASE WHEN g <= %s THEN {WEEK_TS} + interval '8 days' END
              FROM generate_series(1, %s) g""", (N_REAL_REUSED, N_REAL_REUSED, N_REAL))
        # one real, born-gated key: seeded call_count 10, never called since
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count, notes)
            VALUES ('dch_trial_borngated', {WEEK_TS}, 'realip-gated', 'Cursor/1.0',
                    10, 'gate_carry:10 (cumulative unbound usage carried ...)')""")
        # the scan: one pair, 500 born-gated mints in one day
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count, notes)
            SELECT 'dch_trial_scan' || g, {WEEK_TS} + (g || ' seconds')::interval,
                   'scanip', 'Grok/1.0', 11, 'gate_carry:10 (...)'
              FROM generate_series(1, %s) g""", (N_SCAN,))
        # probe UAs — few, but self-declared
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count)
            SELECT 'dch_trial_probe' || g, {WEEK_TS}, 'probeip' || g,
                   'dchub-selfheal-probe/1.0', 2
              FROM generate_series(1, %s) g""", (N_PROBE,))
        # our harness, recognised by its call log: looks "reused" (5 calls)
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count)
            SELECT 'dch_trial_harness' || g, {WEEK_TS}, 'harnessip' || g, 'node', 5
              FROM generate_series(1, %s) g""", (N_HARNESS_LOG,))
        cur.execute(f"""
            INSERT INTO mcp_call_log (api_key, platform, "timestamp")
            SELECT 'dch_trial_harness' || g, 'dchub-internal', {WEEK_TS}
              FROM generate_series(1, %s) g, generate_series(1, 5)""", (N_HARNESS_LOG,))
        # our harness, stamped at mint, no calls yet
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count, mcp_platform)
            SELECT 'dch_trial_stamped' || g, {WEEK_TS}, 'stampip' || g, 'node', 1,
                   'dchub-internal'
              FROM generate_series(1, %s) g""", (N_HARNESS_STAMP,))
        # a real external agent that also sends UA `node`: kept
        cur.execute(f"""
            INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash,
                                         request_ua, call_count, mcp_platform)
            VALUES ('dch_trial_extnode', {WEEK_TS}, 'extnodeip', 'node', 1, 'chatgpt')""")
        cur.execute(f"""
            INSERT INTO mcp_call_log (api_key, platform, "timestamp")
            VALUES ('dch_trial_extnode', 'chatgpt', {WEEK_TS})""")
    admin.close()
    yield
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
    admin.close()


def _get(module, bp, path):
    app = flask.Flask(__name__)
    app.register_blueprint(bp)
    with mock.patch.object(module, "_conn", _connect):
        r = app.test_client().get(path)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    return r.get_json()


def _retention():
    import routes.mcp_retention as mr
    return _get(mr, mr.mcp_retention_bp, "/api/v1/mcp/retention?weeks=4")


def _the_week(j):
    wk = _exec(f"SELECT date_trunc('week', {WEEK_TS})::date")[0][0]
    rows = [r for r in j["key_reuse"]
            if str(r["week"]).startswith(str(wk)) or wk.strftime("%d %b %Y") in str(r["week"])]
    assert len(rows) == 1, j["key_reuse"]
    return rows[0]


# ── the KPIs ────────────────────────────────────────────────────────────────

def test_retention_key_reuse_excludes_scan_and_says_how_many(world):
    row = _the_week(_retention())
    assert row["minted"] == N_KEPT, row
    assert row["excluded_scan_mints"] == N_EXCLUDED, row
    assert row["minted_incl_scan"] == N_KEPT + N_EXCLUDED, row
    assert row["distinct_ips"] == N_KEPT, row
    assert row["excluded_scan_ips"] == 1 + N_PROBE + N_INTERNAL, row


def test_a_born_gated_key_is_not_reuse_until_it_is_called(world):
    row = _the_week(_retention())
    assert row["reused_2plus"] == N_REAL_REUSED, (
        "reused_2plus must count real agents that called 2+ times — not the "
        "gate_carry seed, and not the scan rows", row)


def test_retention_summary_is_the_non_scan_rate_with_the_raw_rate_beside_it(world):
    s = _retention()["summary"]
    assert s["minted_30d"] == N_KEPT
    assert s["excluded_scan_mints_30d"] == N_EXCLUDED
    assert s["minted_30d_incl_scan"] == N_KEPT + N_EXCLUDED
    assert float(s["pct_reused_30d"]) == round(100.0 * N_REAL_REUSED / N_KEPT, 1)
    # the pre-fix figure, kept visible: the scan rows all read "reused"
    assert float(s["pct_reused_30d_incl_scan"]) > 90
    assert int(s["returned_next_week_mature"]) == N_REAL_REUSED
    assert int(s["mature_cohort_30d"]) == N_KEPT


def test_retention_publishes_its_scan_definition(world):
    j = _retention()
    assert j["scan_exclusion"]["pair_per_day"] == mint_guard.scan_thresholds()["pair_per_day"]
    assert "excluded_scan_mints" in j["scan_exclusion"]["definition"]


def test_funnel_attribution_retried_rate_excludes_scan(world):
    import routes.funnel_attribution as fa
    j = _get(fa, fa.funnel_attribution_bp, "/api/v1/funnel/attribution")
    st = j["stages"]
    assert st["trials_minted"] == N_KEPT, st
    assert st["trials_used_2plus_calls"] == N_REAL_REUSED, st
    assert st["excluded_scan_mints"] == N_EXCLUDED, st
    assert j["rates"]["calls_to_2plus_use_pct"] == round(100.0 * N_REAL_REUSED / N_KEPT, 1)


def test_internal_harness_is_excluded_both_ways_and_a_node_agent_is_kept(world):
    """By call log (dchub-internal calls) and by mint stamp (mcp_platform); a
    real agent with UA `node` and an external platform stays counted."""
    rows = _exec(f"""WITH {mint_guard.scored_trial_keys_cte("interval '30 days'")[0]}
                     SELECT api_key, is_scan FROM scored
                      WHERE api_key LIKE 'dch_trial_harness%%'
                         OR api_key LIKE 'dch_trial_stamped%%'
                         OR api_key = 'dch_trial_extnode'""",
                 mint_guard.scored_trial_keys_cte("interval '30 days'")[1])
    got = {k: v for k, v in rows}
    assert len(got) == N_INTERNAL + 1, got
    assert got.pop("dch_trial_extnode") is False
    assert all(got.values()), got


def test_retention_names_the_internal_exclusion(world):
    se = _retention()["scan_exclusion"]
    assert se["internal_platform_like"] == "%dchub%"
    assert "harness" in se["definition"]


def test_the_thresholds_are_env_tunable(world):
    """Raise the pair threshold above the scan's 500/day and it stops being
    excluded — the definition is the env, not a constant."""
    with mock.patch.dict(os.environ, {"DCHUB_MINT_SCAN_PAIR_PER_DAY": "1000",
                                      "DCHUB_MINT_SCAN_IP_PER_DAY": "1000"}):
        row = _the_week(_retention())
    assert row["excluded_scan_mints"] == N_PROBE + N_INTERNAL, row


# ── the ceiling's counts, on both mint tables ───────────────────────────────

def test_trial_ceiling_counts_this_callers_recent_rows(world):
    _exec("DELETE FROM auto_trial_keys WHERE request_ip_hash = 'rlip'")
    _exec("""INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash, request_ua)
             SELECT 'dch_trial_rl' || g, now() - (g || ' minutes')::interval, 'rlip', 'RL-UA'
               FROM generate_series(1, 10) g""")
    c = _connect()
    try:
        with c.cursor() as cur:
            hit = mint_guard.check_mint_rate(cur, "trial", ip_key="rlip", ua="other")
            assert hit and hit["scope"] == "ip" and hit["window"] == "hour", hit
            # the oldest of the ten ages out of the hour in ~50 minutes
            assert 2900 < hit["retry_after"] <= 3000, hit
            assert mint_guard.check_mint_rate(cur, "trial", ip_key="other-ip",
                                              ua="other") is None
            assert mint_guard.check_mint_rate(cur, "trial", ip_key="rlip", ua="other",
                                              count_ip=False) is None
    finally:
        c.close()


def test_claim_ceiling_counts_claim_api_rows_by_raw_ip(world):
    _exec("""INSERT INTO mcp_dev_keys (api_key, created_at, metadata)
             SELECT 'dch_live_rl' || g, now() - (g || ' minutes')::interval,
                    jsonb_build_object('source', 'claim_api', 'ip', '198.51.100.99',
                                       'user_agent', 'node')
               FROM generate_series(1, 10) g""")
    c = _connect()
    try:
        with c.cursor() as cur:
            hit = mint_guard.check_mint_rate(cur, "claim", ip_key="198.51.100.99",
                                             ua="x", count_ua=False)
            assert hit and hit["scope"] == "ip", hit
            assert mint_guard.check_mint_rate(cur, "claim", ip_key="198.51.100.98",
                                              ua="x", count_ua=False) is None
    finally:
        c.close()


# ── the alert ───────────────────────────────────────────────────────────────

def test_radar_fires_on_the_measured_spike_and_not_on_the_baseline(world):
    import routes.brain_consistency_radar as radar
    _exec("TRUNCATE auto_trial_keys")
    # four trailing 7-day windows at the measured baseline
    for w, n in ((1, 217), (2, 195), (3, 226), (4, 238)):
        _exec("""INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash, request_ua)
                 SELECT 'dch_trial_b%s_' || g,
                        now() - (%s * interval '7 days') - interval '1 day',
                        'ip' || g, 'Claude-User'
                   FROM generate_series(1, %s) g""", (w, w, n))
    with mock.patch.object(radar, "_db", _connect):
        assert radar.check_weekly_mint_spike() == []
    _exec("""INSERT INTO auto_trial_keys (api_key, minted_at, request_ip_hash, request_ua)
             SELECT 'dch_trial_s_' || g, now() - interval '1 day',
                    'scanip' || (g % 145), 'Grok/1.0'
               FROM generate_series(1, 11442) g""")
    with mock.patch.object(radar, "_db", _connect):
        f = radar.check_weekly_mint_spike()
    assert len(f) == 1 and f[0]["issue"] == "trial_key_mint_spike", f
    assert f[0]["count"] == 11442
    assert "145 ip hashes" in f[0]["detail"]
