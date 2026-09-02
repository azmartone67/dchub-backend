"""tests/test_qa_sweep_0902_measurement.py — measurement fixes from the
2026-09-02 QA sweep (findings 5:4d, 5:4e, 2:F2, 2:F6, 2:F7).

House rule: never import main. Route modules are imported directly (they
import only flask/psycopg2 lazily) or read as source.

Mutations recorded in the PR body:
  4d  relay_stats reads a different table            → RED
  4e  persisted_stats reads a different table / 0 on failure → RED
  F2  authorize_started removed from ledger _KINDS or retention → RED
  F6  lane I passes on a non-zero delta / adoption shell drops is_public_ip → RED
  F7  harness predicate drops a name / _assemble drops the companion → RED
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        s = fh.read()
    assert len(s) > 500, "%s read as %d bytes" % (os.path.join(*parts), len(s))
    return s


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    """Records SQL; answers from a per-prefix table of results."""
    def __init__(self, answers):
        self.answers = answers
        self.log = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        for key, rows in self.answers.items():
            if key in s:
                if isinstance(rows, Exception):
                    raise rows
                self._rows = list(rows)
                return
        raise AssertionError("unexpected SQL: " + s[:100])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, answers):
        self.answers = answers
        self.cur = None
        self.rolled_back = 0

    def cursor(self, **kw):
        self.cur = _Cur(self.answers)
        return self.cur

    def rollback(self):
        self.rolled_back += 1


# ── 4d · upgrade-handoff stats read the table the relay writes ──────────────
def test_handoff_stats_read_relay_opens_not_only_the_legacy_page_table():
    uh = _load("routes/upgrade_handoff.py", "_uh_0902")
    assert uh.RELAY_TABLE == "relay_opens"
    conn = _Conn({"FROM relay_opens WHERE ts >= NOW() - INTERVAL '30 days' GROUP BY 1": [("get_dchub_recommendation", 90), ("analyze_site", 12)],
                  "FROM relay_opens WHERE ts >= NOW() - INTERVAL '30 days'": [(102, 97, 88)]})
    out = uh.relay_stats(conn)
    assert out["human_opens_30d"] == 102 and out["human_opens_valid_30d"] == 97
    assert out["human_sessions_30d"] == 88
    assert out["human_opens_by_tool"] == {"get_dchub_recommendation": 90, "analyze_site": 12}
    assert out["human_opens_source"] == "relay_opens"
    assert all("FROM relay_opens" in s for s in conn.cur.log), conn.cur.log


def test_handoff_relay_read_failure_is_null_not_zero():
    uh = _load("routes/upgrade_handoff.py", "_uh_0902b")
    conn = _Conn({"FROM relay_opens": Exception("relation relay_opens does not exist")})
    out = uh.relay_stats(conn)
    assert out["human_opens_30d"] is None and "human_opens_error" in out
    assert conn.rolled_back == 1


def test_handoff_stats_route_labels_the_legacy_source_deprecated():
    src = _src("routes", "upgrade_handoff.py")
    assert "DEPRECATED" in src and 'out["legacy_source"] = "upgrade_page_events"' in src
    assert "out.update(relay_stats(c))" in src, "the route does not publish the relay read"
    # the relay table name is the one human_relay.py actually writes
    assert 'CREATE TABLE IF NOT EXISTS relay_opens' in _src("routes", "human_relay.py")


# ── 4e · webhook received_total persisted ───────────────────────────────────
def test_persisted_webhook_stats_count_the_idempotency_ledger():
    from util import stripe_webhook_stats as sws
    seen = []

    def execute(sql, params=(), fetch=False):
        seen.append(sql)
        return 1, [(4, 1, 3, dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc))]

    out = sws.persisted_stats(execute)
    assert out["persisted_total"] == 4 and out["persisted_24h"] == 1 and out["persisted_7d"] == 3
    assert out["last_persisted_at"].startswith("2026-09-01T12:00")
    assert out["persisted_source"] == "stripe_webhook_events"
    assert seen and "FROM stripe_webhook_events" in seen[0]
    # the table the webhook handler actually writes, per verified event id
    main_src = _src("main.py")
    assert "CREATE TABLE IF NOT EXISTS stripe_webhook_events" in main_src
    assert "out['stats'].update(_pws(_pg_execute))" in main_src, "diagnostics route does not publish it"


def test_persisted_webhook_stats_failure_is_null_never_zero():
    from util import stripe_webhook_stats as sws

    def execute(sql, params=(), fetch=False):
        raise RuntimeError("db down")

    out = sws.persisted_stats(execute)
    assert out["persisted_total"] is None and out["persisted_24h"] is None
    assert out["persisted_error"].startswith("RuntimeError")


# ── F2 · the middle of the OAuth on-ramp ────────────────────────────────────
def test_ledger_whitelist_accepts_oauth_authorize_started():
    src = _src("routes", "oauth_challenge_ledger.py")
    kinds = re.search(r"_KINDS\s*=\s*\{(.*?)\}", src, re.S)
    assert kinds, "_KINDS whitelist not found"
    assert '"oauth_authorize_started"' in kinds.group(1), (
        "oauth_authorize_started missing from _KINDS — the emit handler drops unknown "
        "kinds with a bare `continue`, so the gateway's counter would read 0 forever")


def test_retention_publishes_authorize_started_with_an_instrumented_since():
    src = _src("routes", "mcp_retention.py")
    assert "kind = 'oauth_authorize_started'" in src, "retention never aggregates the kind"
    assert '"authorize_started_30d": int(ch.get("authorize_started") or 0)' in src
    assert '"authorize_started_instrumented_since": "2026-09-02"' in src
    # it lives inside challenge_side, beside the two ends it sits between
    cs = src.index('ib["challenge_side"] = {')
    blk = src[cs:src.index("gateway_reporting", cs)]
    assert "authorize_started_30d" in blk and "new_identities_30d" in blk


# ── F6 · one complete week, one call count ──────────────────────────────────
_PT = _load("routes/published_truth_master_shell.py", "_pts54_0902")


def _ctx(fc=1834, wc=1810):
    return {"funnel": {"real_external_calls_complete_wk": fc},
            "weekly_series": {"weeks": [
                {"week_start": "2026-08-17", "calls": 2100},
                {"week_start": "2026-08-24", "calls": wc}]}}


def test_lane_I_fails_on_the_measured_24_row_drift_and_names_it():
    checks = _PT._lane_series_parity(_ctx(1834, 1810))
    assert _PT._lane_verdict(checks) == "FAIL"
    k = checks[0]
    assert k["pass"] is False and k["critical"]
    assert "-24" in k["detail"] and "1810" in k["detail"] and "1834" in k["detail"]
    assert "2026-08-24" in k["detail"], "the lane must name the week"


def test_lane_I_passes_only_when_the_two_surfaces_agree():
    assert _PT._lane_verdict(_PT._lane_series_parity(_ctx(1834, 1834))) == "PASS"
    assert _PT._lane_verdict(_PT._lane_series_parity(_ctx(1834, 1835))) == "FAIL"


def test_lane_I_is_unmeasured_not_green_when_a_source_is_missing():
    assert _PT._lane_verdict(_PT._lane_series_parity({"funnel": {"real_external_calls_complete_wk": 1}})) == "?"
    c = _ctx(1834, 1810)
    c["weekly_series"]["weeks"][-1]["calls"] = None       # unobserved week
    assert _PT._lane_verdict(_PT._lane_series_parity(c)) == "?"
    c["funnel"] = {}
    assert _PT._lane_verdict(_PT._lane_series_parity(c)) == "?"


def test_lane_I_is_registered_and_fed():
    ids = [lid for lid, _, _ in _PT._LANES]
    assert "series_parity" in ids
    src = _src("routes", "published_truth_master_shell.py")
    assert '"weekly_series": _get_json("/api/v1/reports/weekly-series' in src
    assert "/api/v1/reports/weekly-series" in _src("routes", "weekly_series.py")


def test_adoption_shell_counts_distinct_agents_once_on_the_canonical_basis():
    src = _src("routes", "agent_adoption_master_shell.py")
    body = src[src.index("def _measure"):src.index("def _persist_snapshot")]
    real_q = body[body.index("SELECT lower(platform) AS p"):body.index("GROUP BY 1")]
    assert "is_public_ip AND is_real_external" in real_q, "per-platform query lacks is_public_ip"
    assert "SELECT COUNT(DISTINCT agent_id)" in body and "= ANY(%s)" in body, \
        "distinct agents are still summed per platform"
    assert 'out["real_agents_7d"] += ag' not in body, "the per-platform sum still feeds the headline"
    assert "calls on NAMED platforms" in src[src.index("def _headline"):]
    assert "calls on NAMED platforms/7d" in src[src.index("subj = ("):]
    assert '"real_calls_7d_label": "calls on NAMED platforms (7d)"' in src


def test_adoption_measure_publishes_the_union_count_not_the_sum():
    """Run _measure against a fake cursor: an agent seen as both 'claude' and
    'anthropic' is ONE agent."""
    aa = _load("routes/agent_adoption_master_shell.py", "_aa_0902")
    answers = {
        "FROM ai_cumulative": [("claude", 900)],
        "SELECT lower(platform) AS p": [("claude", 200, 3), ("anthropic", 100, 3), ("cursor", 10, 1)],
        "SELECT COUNT(DISTINCT agent_id)": [(4,)],
        "AND (platform IS NULL OR lower(platform) IN ('mcp',''))": [(50,)],
        "WITH first_call AS": [(10, 4, 1)],
        "FROM mcp_conversions": [(3,)],
    }
    conn = _Conn(answers)
    aa._conn = lambda: conn
    m = aa._measure()
    assert m["real_agents_7d"] == 4, "headline must be the DISTINCT-once count"
    assert m["real_agents_7d_platform_sum"] == 7, "the per-platform sum is kept beside it"
    assert m["real_calls_7d"] == 310
    union_sql = [s for s in conn.cur.log if "SELECT COUNT(DISTINCT agent_id)" in s]
    assert union_sql and "is_public_ip AND is_real_external" in union_sql[0]


# ── F7 · harnesses reported beside the top caller, never deny-listed ────────
def test_harness_predicate_names_all_four_and_uses_no_like():
    import mcp_calls_deloop as d
    assert set(d.HARNESS_CLIENT_NAMES) == {"actionist-apps-verification", "skeptic-verifier",
                                           "vouch-census", "mcp-spec-study"}
    p = d.harness_predicate()
    for n in d.HARNESS_CLIENT_NAMES:
        assert f"'{n}'" in p
    assert "LIKE" not in p and "%" not in p, "must be safe inlined AND under bound params"
    assert p.startswith("lower(COALESCE(NULLIF(client_name, ''), platform, ''))")
    # reported, not excluded: none of the names may be in any exclusion list
    src = _src("mcp_calls_deloop.py")
    before = src[:src.index("HARNESS_CLIENT_NAMES = (")]
    for n in d.HARNESS_CLIENT_NAMES:
        # quoted forms only: a prose mention in a comment is not a listing
        assert f"'{n}'" not in before and f'"{n}"' not in before, (
            f"{n} was deny-listed — F7 says report, don't deny")


def test_weekly_series_publishes_net_of_harnesses_beside_net_of_top():
    from routes.weekly_series import _assemble
    wk = dt.date(2026, 8, 24)
    rows = {wk: (40, 1810, 5000)}
    net = {wk: (1810, 1300, 510, 40, 39, "chain-hire")}
    harness = {wk: (1722, 37, 88)}
    out = _assemble(rows, [wk], net, harness=harness)[0]
    assert out["calls_net_of_top"] == 510 and out["agents_net_of_top"] == 39
    assert out["calls_net_of_harnesses"] == 1722
    assert out["agents_net_of_harnesses"] == 37
    assert out["harness_calls"] == 88
    assert "actionist-apps-verification" in out["harness_names"]
    assert out["calls"] == 1810, "the canonical count must not move"
    # absent companion → absent keys, not zeros
    bare = _assemble(rows, [wk], net)[0]
    assert "calls_net_of_harnesses" not in bare


def test_weekly_series_run_queries_the_harness_companion_on_the_same_population():
    src = _src("routes", "weekly_series.py")
    run = src[src.index("def _run("):src.index('out["weeks"] = _assemble(')]
    assert "harness_predicate" in run
    hq = run[run.index("harness_rows = {}"):]
    assert '" WHERE " + where + " AND " + pop +' in hq, "harness query must reuse the same where/pop strings"
    assert "harness=harness_rows" in src[src.index('out["weeks"] = _assemble('):]
