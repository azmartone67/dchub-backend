"""
click→pay per plan and per path (frontend#1534 (c)), against a real Postgres.

routes/handoff_definition builds the SQL; routes/schema_repair._click_to_pay runs
it for /api/v1/admin/funnel/leakage. Only a real database shows the latest-click
credit, the lookback, livemode, the operator and ChatGPT exclusions and the
per-stage DISTINCT counting what this file claims.

Everything lives in a throwaway SCHEMA on this connection's search_path, so the
tables other db-parity lanes own are never touched.

  mcp_go_c   M1 metered pk-A, S1                      paid P1 an hour later
             M2 pro, bare session ref S2              clicked FIRST
             M3 metered, the same ref S2              clicked LAST: P2 credits it
             M4 unsigned developer                    out (not ours)
             M5 probe-UA developer                    out (not a human)
             M6 developer, operator session           out (self traffic)
             M7 developer k-C, ChatGPT session S7     out of this column; its
                                                      payment P7 is ChatGPT's paid
             M8 developer k-D                         P8 lands 8 days later: out
                                                      of the 7-day lookback
             M9 starter (a link from before)          its own plan row
             M10 metered, 40 days ago                 out of the window
             P10 livemode=false on pk-A               not a payment
  rest_wall  W1 pro, no ref, no session (a REST wall)  its own column, paid null
             W2 the same, probe UA                    out
  cold_go_p  C1 pro, page ref                         paid P11
             C2 metered, no ref                       a press, never joinable
             C3 starter, known_plan=false             out (not a /pricing plan)
             C4 probe-UA metered                      out
  chatgpt    walls: G1 G2 G3 (chatgpt), G4 (openai-mcp), S7 (ChatGPT)  = 5
             out: G5 (claude), an operator session with a chatgpt signal
             views: G1 only (G2 probe UA, G3 invalid token, G5 not ChatGPT)
             identified: G1 (relay page), G4 (high-intent claim) = 2
             paid: G1 (topup), S7 (relayed /go/c payment P7) = 2

Set CLICK_TO_PAY_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip: a skipped proof is not a proof.
"""
import ast
import os
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from mcp_calls_deloop import self_traffic_session_prefixes  # noqa: E402
from routes import handoff_definition as H  # noqa: E402
from routes._audience_identity import operator_emails  # noqa: E402

DSN = os.environ.get("CLICK_TO_PAY_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="CLICK_TO_PAY_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = "ctp_click_to_pay_test"
IV = "30 days"
REAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0"
PROBE_UA = "curl/8.7.1"
S1, S2, S7, S8, S9, G1, G2, G3, G4, G5 = (
    "c7a0%04x-0000-4000-8000-%012x" % (i, i) for i in range(1, 11))
# Built from the declared prefixes, never typed: the fixture follows the seed.
OP = self_traffic_session_prefixes()[0] + "-0000-4000-8000-0000000000c7"
# M6's operator session carries NO ChatGPT signal: sharing OP would let the
# ChatGPT exclusion hide a missing operator exclusion (a surviving mutation did).
OP_M6 = self_traffic_session_prefixes()[0] + "-0000-4000-8000-0000000000c8"
PAGE_REF = "ref_pricing-page__tool_none__ts_1789990000"

DDL = """
CREATE TABLE mcp_checkout_clicks (id SERIAL PRIMARY KEY, clicked_at TIMESTAMPTZ,
  plan TEXT, ref TEXT, ref_kind TEXT, sig_ok BOOLEAN, user_agent TEXT, session_id TEXT);
CREATE TABLE pricing_checkout_clicks (id SERIAL PRIMARY KEY, clicked_at TIMESTAMPTZ,
  plan TEXT, ref TEXT, known_plan BOOLEAN, user_agent TEXT);
CREATE TABLE mcp_checkout_payments (id BIGSERIAL PRIMARY KEY, stripe_session_id TEXT UNIQUE,
  client_reference_id TEXT, livemode BOOLEAN, paid_at TIMESTAMPTZ);
CREATE TABLE mcp_upgrade_signals (id SERIAL PRIMARY KEY, session_id TEXT, mcp_client TEXT,
  created_at TIMESTAMP);
CREATE TABLE relay_opens (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, session_id TEXT,
  valid BOOLEAN, user_agent TEXT);
CREATE TABLE relay_identify_captures (id BIGSERIAL PRIMARY KEY, mcp_session_id TEXT,
  email TEXT, captured_at TIMESTAMPTZ);
CREATE TABLE mcp_high_intent_sessions (mcp_session_id TEXT, claim_email TEXT,
  first_hit_at TIMESTAMPTZ);
CREATE TABLE mcp_session_upgrades (mcp_session_id TEXT, upgraded_at TIMESTAMPTZ,
  stripe_session_id TEXT);
CREATE TABLE mcp_topups (mcp_session_id TEXT, created_at TIMESTAMPTZ, stripe_session_id TEXT);
-- paid_attributed v4 reads the payer from here (live column names, 2026-09-24).
CREATE TABLE mcp_conversions (id BIGSERIAL PRIMARY KEY, caller_id TEXT, user_email TEXT,
  stripe_session_id TEXT);
"""


def _seed(cur):
    ago = lambda d: "now() - interval '%s'" % d  # noqa: E731
    clicks = [  # (age, plan, ref, ref_kind, sig_ok, ua, session_id)
        ("2 days", "metered", "pk-A", "pack_key", True, REAL_UA, S1),          # M1
        ("3 days", "pro", S2, "session", True, REAL_UA, None),                 # M2
        ("2 days 12 hours", "metered", S2, "session", True, REAL_UA, None),    # M3
        ("2 days", "developer", "k-X", "sub_key", False, REAL_UA, S9),         # M4
        ("2 days", "developer", "k-Y", "sub_key", True, PROBE_UA, S9),         # M5
        ("2 days", "developer", "k-B", "sub_key", True, REAL_UA, OP_M6),       # M6
        ("2 days", "developer", "k-C", "sub_key", True, REAL_UA, S7),          # M7
        ("10 days", "developer", "k-D", "sub_key", True, REAL_UA, S8),         # M8
        ("2 days", "starter", S9, "session", True, REAL_UA, None),             # M9
        ("40 days", "metered", "pk-OLD", "pack_key", True, REAL_UA, S1),       # M10
        ("1 day", "pro", "", "none", True, REAL_UA, None),                     # W1 REST wall
        ("1 day", "pro", "", "none", True, PROBE_UA, None),                    # W2 probe UA
    ]
    for age, plan, ref, kind, ok, ua, sid in clicks:
        cur.execute("INSERT INTO mcp_checkout_clicks (clicked_at, plan, ref, ref_kind, sig_ok,"
                    " user_agent, session_id) VALUES (" + ago(age) + ", %s, %s, %s, %s, %s, %s)",
                    (plan, ref, kind, ok, ua, sid))
    for age, plan, ref, known, ua in [
            ("1 day", "pro", PAGE_REF, True, REAL_UA),                          # C1
            ("1 day", "metered", None, True, REAL_UA),                          # C2
            ("1 day", "starter", PAGE_REF + "x", False, REAL_UA),               # C3
            ("1 day", "metered", PAGE_REF + "y", True, PROBE_UA)]:              # C4
        cur.execute("INSERT INTO pricing_checkout_clicks (clicked_at, plan, ref, known_plan,"
                    " user_agent) VALUES (" + ago(age) + ", %s, %s, %s, %s)", (plan, ref, known, ua))
    for sess, cref, live, age in [
            ("P1", "pk-A", True, "1 day 23 hours"),
            ("P2", S2, True, "2 days"),
            ("P7", "k-C", True, "1 day"),
            ("P8", "k-D", True, "1 day 23 hours"),     # 8 days 1 hour after M8
            ("P10", "pk-A", False, "1 day"),
            ("P11", PAGE_REF, True, "12 hours")]:
        cur.execute("INSERT INTO mcp_checkout_payments (stripe_session_id, client_reference_id,"
                    " livemode, paid_at) VALUES (%s, %s, %s, " + ago(age) + ")", (sess, cref, live))
    for sid, client in [(G1, "chatgpt"), (G2, "chatgpt"), (G3, "chatgpt"), (G4, "openai-mcp"),
                        (S7, "ChatGPT"), (G5, "claude"), (OP, "chatgpt"), (G1, "chatgpt")]:
        cur.execute("INSERT INTO mcp_upgrade_signals (session_id, mcp_client, created_at)"
                    " VALUES (%s, %s, now() - interval '3 days')", (sid, client))
    for sid, valid, ua in [(G1, True, REAL_UA), (G2, True, PROBE_UA), (G3, False, REAL_UA),
                           (G5, True, REAL_UA)]:
        cur.execute("INSERT INTO relay_opens (ts, session_id, valid, user_agent)"
                    " VALUES (now() - interval '2 days', %s, %s, %s)", (sid, valid, ua))
    cur.execute("INSERT INTO relay_identify_captures (mcp_session_id, email, captured_at)"
                " VALUES (%s, 'g1@example.com', now() - interval '2 days')", (G1,))
    cur.execute("INSERT INTO mcp_high_intent_sessions (mcp_session_id, claim_email, first_hit_at)"
                " VALUES (%s, 'g4@example.com', now() - interval '3 days')", (G4,))
    cur.execute("INSERT INTO mcp_topups (mcp_session_id, created_at)"
                " VALUES (%s, now() - interval '1 day')", (G1,))
    # v4: a pack on ChatGPT session G2 paid by an OPERATOR mailbox. G2 is not
    # an operator session, so only the payer check can keep it out of `paid`.
    cur.execute("INSERT INTO mcp_topups (mcp_session_id, created_at, stripe_session_id)"
                " VALUES (%s, now() - interval '1 day', 'cs_op_g2')", (G2,))
    cur.execute("INSERT INTO mcp_conversions (user_email, stripe_session_id)"
                " VALUES (%s, 'cs_op_g2')", (sorted(operator_emails())[0].upper(),))


@pytest.fixture(scope="module")
def cur():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
    c.execute("CREATE SCHEMA " + SCHEMA)
    c.execute("SET search_path TO " + SCHEMA)
    c.execute(DDL)
    _seed(c)
    try:
        yield c
    finally:
        c.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
        conn.close()


def test_every_path_and_plan_counts_what_the_seed_says(cur):
    cur.execute(H.click_to_pay_by_plan_sql(IV))
    got = {(p, plan): (int(k), int(paid)) for p, plan, k, paid in cur.fetchall()}
    assert got == {
        ("mcp_go_c", "metered"): (2, 2),     # M1+P1, M3+P2 (the later click on S2)
        ("mcp_go_c", "pro"): (1, 0),         # M2: same ref, earlier click, no credit
        ("mcp_go_c", "developer"): (1, 0),   # M8 only; P8 is outside the lookback
        ("mcp_go_c", "starter"): (1, 0),     # M9
        ("cold_go_p", "pro"): (1, 1),        # C1+P11
        ("cold_go_p", "metered"): (1, 0),    # C2, no ref
        ("rest_wall_go_c", "pro"): (1, 0),   # W1; W2 is a probe; never in mcp_go_c
    }


def test_without_the_cold_table_the_mcp_column_still_reads(cur):
    cur.execute(H.click_to_pay_by_plan_sql(IV, include_cold=False))
    assert {r[0] for r in cur.fetchall()} == {"mcp_go_c", "rest_wall_go_c"}


def test_chatgpt_stages_count_distinct_sessions(cur):
    cur.execute(H.chatgpt_relay_stages_sql(IV))
    # paid: G1 (topup) and S7 (P7). G2's operator-paid pack is out (v4).
    assert tuple(int(x) for x in cur.fetchone()) == (5, 1, 2, 2)


def _endpoint_helper():
    """schema_repair._click_to_pay, pulled out of the source and run as written."""
    src = (REPO / "routes" / "schema_repair.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_click_to_pay")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "schema_repair.py", "exec"), ns)
    return ns["_click_to_pay"]


def test_the_endpoint_block_fills_every_plan_and_names_the_rest_other(cur):
    out = _endpoint_helper()(cur.connection, cur, 30)
    mcp, cold = out["mcp_go_c"], out["cold_go_p"]
    assert mcp["metered"] == {"clicks": 2, "paid": 2, "click_to_pay_pct": 100.0}
    assert mcp["developer"] == {"clicks": 1, "paid": 0, "click_to_pay_pct": 0.0}
    assert mcp["other"] == {"clicks": 1, "paid": 0, "click_to_pay_pct": 0.0}
    assert cold["pro"] == {"clicks": 1, "paid": 1, "click_to_pay_pct": 100.0}
    assert cold["developer"] == {"clicks": 0, "paid": 0, "click_to_pay_pct": None}
    # A REST-wall click can never join a payment: unmeasurable, never a 0% rate.
    assert out["rest_wall_go_c"]["pro"] == {"clicks": 1, "paid": None, "click_to_pay_pct": None}
    assert out["chatgpt_upgrade_h"] == {"plan": "metered", "walls": 5, "views": 1,
                                        "identified": 2, "paid": 2}
    assert "cold_go_p_note" not in out


def test_the_endpoint_block_reports_cold_unmeasured_until_its_table_exists(cur):
    cur.execute("ALTER TABLE pricing_checkout_clicks RENAME TO pricing_checkout_clicks_hidden")
    try:
        out = _endpoint_helper()(cur.connection, cur, 30)
    finally:
        cur.execute("ALTER TABLE pricing_checkout_clicks_hidden RENAME TO pricing_checkout_clicks")
    assert out["cold_go_p"] is None and "cold_go_p_note" in out
    assert out["mcp_go_c"]["metered"]["paid"] == 2
