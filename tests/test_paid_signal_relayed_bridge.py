"""paid -> signal: a third bridge lane, via the click that sold it.

Measured after the real $10 prove, 2026-09-17:

    paid_signal_attribution_30d
      paid_total 3 · bridged_to_signal 0 · unattributable 3 · rate 0.0%
    relayed_checkout_payments
      payments 1 · matched_a_relayed_click 1 · attributable_to_a_session 1

The same payment was simultaneously PROVEN to come from a relayed /go/c click
and reported as unattributable. The two existing lanes cannot see it:
attribution_signal_id is never set for an agent-channel buy, and the Stripe
webhook has no MCP caller_id to share.

WHAT THESE PIN
  * the lane resolves its session with the SAME builder paid_attributed joins
    on — a second spelling would let the two disagree about which session
    bought while both looked measured;
  * a conversion's own session_id WINS when set; the click walk is the fallback;
  * the signal must PRECEDE the sale. A signal after it is the customer hitting
    a wall they already paid to pass, and counting it lets a bridge point
    backwards in time;
  * the lane is tried LAST, so it can only ever convert rows that were
    previously `unattributable` — it cannot re-label a row the direct lanes
    already claimed;
  * the four buckets partition paid_total, and the rate counts all three
    bridges.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FUNNEL = (ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
DEFN = (ROOT / "routes" / "handoff_definition.py").read_text(encoding="utf-8")

from routes.handoff_definition import (  # noqa: E402
    PAID_RELAYED_CHECKOUT_LOOKBACK, PAID_SIGNAL_RELAYED_BRIDGE_BASIS,
    _relayed_click_session_for, paid_relayed_click_session_sql,
    paid_signal_relayed_bridge_predicate, paid_signal_relayed_session_sql,
    relayed_checkout_session_filters)


# ── one builder, not two spellings ──────────────────────────────────────
def test_the_lane_resolves_its_session_with_the_paid_join_builder():
    """★ The same function paid_attributed uses. If this lane spelled the join
    itself, the funnel could attribute one payment to two different sessions
    and both numbers would look measured."""
    sess = paid_signal_relayed_session_sql()
    assert _relayed_click_session_for(
        "(select pay2.client_reference_id from mcp_checkout_payments pay2"
        " where pay2.stripe_session_id = p.stripe_session_id"
        " order by pay2.paid_at desc limit 1)", "p.conv_at") in sess
    # and the lane's row filters are the lane's, not restated
    assert relayed_checkout_session_filters() in sess
    assert PAID_RELAYED_CHECKOUT_LOOKBACK in sess


def test_it_shares_the_lookback_with_the_paid_join():
    """Two different lookbacks would mean a payment could be attributable to a
    session for one stage and not the other."""
    a = re.findall(r"interval '([^']+)'", paid_relayed_click_session_sql())
    b = re.findall(r"interval '([^']+)'", paid_signal_relayed_session_sql())
    assert a and a == b, (a, b)


def test_the_endpoint_calls_the_predicate_and_does_not_spell_it():
    i = FUNNEL.index("THEN 'relayed_click'")
    seg = FUNNEL[i - 900:i + 80]
    assert "_ps_relayed_bridge()" in seg
    assert "mcp_checkout_clicks" not in seg, (
        "the endpoint spells the join itself instead of calling the builder")


# ── the resolution order ────────────────────────────────────────────────
def test_the_conversions_own_session_wins_when_set():
    """A session on the row is a direct fact; the click walk is inference."""
    sess = paid_signal_relayed_session_sql()
    assert sess.startswith("coalesce(nullif(p.session_id,''), ")
    assert sess.index("p.session_id") < sess.index("mcp_checkout_clicks")


def test_the_click_walk_goes_through_the_payment_row():
    sess = paid_signal_relayed_session_sql()
    assert "mcp_checkout_payments pay2" in sess
    assert "pay2.stripe_session_id = p.stripe_session_id" in sess
    assert "order by pay2.paid_at desc limit 1" in sess, (
        "an unordered scalar subquery picks an arbitrary payment")


def test_the_cte_carries_the_columns_the_lane_reads():
    """★ A predicate referencing p.session_id / p.stripe_session_id against a
    CTE that does not select them is a hard SQL error, and this block swallows
    its own exceptions — it would read as 'no bridge' forever."""
    i = FUNNEL.index("WITH paid AS (")
    cte = FUNNEL[i:FUNNEL.index("labeled AS (", i)]
    assert "c.session_id AS session_id" in cte
    assert "c.stripe_session_id AS stripe_session_id" in cte


# ── time has a direction ────────────────────────────────────────────────
def test_the_signal_must_precede_the_sale():
    """★ A signal raised AFTER the sale is the customer hitting a wall they
    already paid to pass. Counting it would let the bridge point backwards."""
    pred = paid_signal_relayed_bridge_predicate()
    assert "s2.created_at <= p.conv_at" in pred
    assert ">=" not in pred.split("s2.created_at")[1][:20]


def test_the_predicate_reads_the_signal_table_on_the_session():
    pred = paid_signal_relayed_bridge_predicate()
    assert "from mcp_upgrade_signals s2" in pred
    assert "nullif(s2.session_id,'') = " in pred, (
        "a blank session on the signal side would match a blank resolution")


# ── it can only rescue the unattributable ───────────────────────────────
def test_the_lane_is_tried_last():
    """★ The two lanes above are direct facts. If this one ran first it could
    re-label rows they already claimed, and the numbers would move for a
    reason nobody could see."""
    for earlier in ("THEN 'signal_id'", "THEN 'caller_bridge'"):
        assert FUNNEL.index(earlier) < FUNNEL.index("THEN 'relayed_click'")
    assert FUNNEL.index("THEN 'relayed_click'") < FUNNEL.index(
        "ELSE 'unattributable'")


def test_the_basis_says_it_is_tried_last():
    assert "tried LAST" in PAID_SIGNAL_RELAYED_BRIDGE_BASIS
    assert "previously" in PAID_SIGNAL_RELAYED_BRIDGE_BASIS
    assert "_relayed_click_session_for" in PAID_SIGNAL_RELAYED_BRIDGE_BASIS


# ── the arithmetic ──────────────────────────────────────────────────────
def test_the_new_bucket_is_counted_in_the_total_and_the_rate():
    """★ A bucket added to the CASE but not to the sum silently DROPS rows out
    of paid_total, which would make the rate rise by losing its denominator."""
    i = FUNNEL.index('_rel = _bridge.get("relayed_click", 0)')
    seg = FUNNEL[i:i + 900]
    assert "_paid_total = _sig + _cal + _rel + _un" in seg
    assert '"bridged_to_signal":                   _sig + _cal + _rel' in seg
    assert "round(100.0 * (_sig + _cal + _rel) / _paid_total, 1)" in seg


def test_every_bucket_is_published_separately():
    for field in ('"bridged_via_attribution_signal_id"',
                  '"bridged_via_caller_key"',
                  '"bridged_via_relayed_click"',
                  '"bridged_via_relayed_click_basis"',
                  '"unattributable"'):
        assert field in FUNNEL, field


def test_the_builder_lives_in_the_one_definition_module():
    """handoff_definition is the single writer of these definitions — four
    surfaces restating human_acted is what that rule exists to prevent."""
    fn = next(n for n in ast.walk(ast.parse(DEFN))
              if isinstance(n, ast.FunctionDef)
              and n.name == "paid_signal_relayed_bridge_predicate")
    body = ast.get_source_segment(DEFN, fn) or ""
    assert "paid_signal_relayed_session_sql(" in body
