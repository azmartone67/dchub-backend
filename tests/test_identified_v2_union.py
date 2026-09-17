"""`identified` v2: both writers of a bound email, not just the first one.

THE DEFECT, measured live 2026-09-17 on /api/v1/mcp/handoff-funnel:

    paid_attributed  1
    identified       0
    identify_captures {captured: 2, distinct_emails: 2, reached_the_rung: 0}
    relayed_checkout_payments {payments: 1, matched: 1, attributable_to_a_session: 1}

A paying customer the funnel said we had never identified — whose email we hold,
because it is on the Stripe Checkout Session. `attributable_to_a_session: 1`
proves the session id resolved; the stamp matched zero rows because A RELAY
TOKEN IS MINTED STATELESSLY, so holding one never implied the session has an
mcp_high_intent_sessions row.

WHAT THESE PIN
  * v2 counts DISTINCT sessions over BOTH writers, and a session in both lanes
    counts once;
  * each lane is windowed on its OWN time column — one table's clock applied to
    the other would silently drop captures from sessions that arrived earlier;
  * NOTHING is inserted into mcp_high_intent_sessions. That table is the
    denominator of the three rungs above this one, so creating a row to make
    this number move would inflate them by the same act;
  * v1 is still published, the version is published, and the read path runs no
    DDL — the table is created by the capture writer, and its absence must read
    as "fall back to v1", not as an error or a zero;
  * `identify_captures.reached_the_rung` is a SEPARATE diagnostic and is NOT
    this number. It will keep reading 0 for a capture whose session has no
    high-intent row, which is the mechanism being reported.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FUNNEL = (ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
DEFN = (ROOT / "routes" / "handoff_definition.py").read_text(encoding="utf-8")

from routes.handoff_definition import (  # noqa: E402
    IDENTIFIED_BASIS, IDENTIFIED_DEFINITION_CHANGELOG,
    IDENTIFIED_DEFINITION_VERSION, _identified_lanes,
    identified_capture_lane_sql, identified_count_sql, identified_definition,
    identified_v1_sql, relayed_checkout_session_filters)


# ── both writers, once each ──────────────────────────────────────────────
def test_the_union_reads_both_writers():
    sql = identified_count_sql("30 days")
    assert "mcp_high_intent_sessions" in sql, "the v1 lane is gone"
    assert "relay_identify_captures" in sql, "the capture lane is missing"
    assert " union " in sql


def test_a_session_in_both_lanes_counts_once():
    """★ COUNT(DISTINCT), not a SUM of two counts — which is exactly the bug
    paid_attributed v1 had (it summed mcp_session_upgrades and mcp_topups)."""
    sql = identified_count_sql("30 days")
    assert "count(distinct u.sid)" in sql
    assert sql.count("count(") == 1, "a second aggregate means a sum, not a union"


def test_each_lane_is_windowed_on_its_own_clock():
    """★ The high-intent lane on first_hit_at (the session's arrival, what every
    rung above uses); the capture lane on captured_at (when the email was
    given). Windowing captures on first_hit_at would drop every capture from a
    session that arrived before the window — silently."""
    hi, cap = _identified_lanes("30 days")
    assert "first_hit_at >" in hi and "captured_at" not in hi
    assert "captured_at >" in cap and "first_hit_at" not in cap


def test_the_blank_session_is_excluded_not_counted():
    sql = identified_count_sql("30 days")
    assert "coalesce(u.sid,'') <> ''" in sql


def test_the_self_traffic_exclusion_applies_to_the_union_and_can_be_dropped():
    """The same contract paid_attributed keeps: the exclusion rides the union,
    and the _including_self_traffic figure is the difference."""
    on = identified_count_sql("30 days")
    off = identified_count_sql("30 days", include_self_traffic=True)
    assert on != off, "include_self_traffic changed nothing"
    assert len(on) > len(off), "the exclusion is the ADDED clause"
    assert off in on or on.startswith(off.split(" and ")[0])


def test_the_capture_lane_is_published_alone_so_the_mover_is_visible():
    lane = identified_capture_lane_sql("30 days")
    assert "relay_identify_captures" in lane
    assert "mcp_high_intent_sessions" not in lane, "that is not the lane alone"
    assert "count(distinct u.sid)" in lane


def test_v1_is_still_published_unchanged():
    v1 = identified_v1_sql("30 days")
    assert "mcp_high_intent_sessions" in v1
    assert "relay_identify_captures" not in v1
    assert "claim_email is not null" in v1


# ── the line nobody may cross ────────────────────────────────────────────
def test_nothing_inserts_into_the_denominator_table():
    """★★ mcp_high_intent_sessions is the denominator of paywall_hit,
    high_intent and relay_minted. Creating a row to move THIS number would
    inflate all three — buying a green number by corrupting the stages that
    give it meaning."""
    for src, name in ((DEFN, "handoff_definition"),
                      ((ROOT / "routes" / "relay_identify.py")
                       .read_text(encoding="utf-8"), "relay_identify")):
        low = src.lower()
        assert "insert into mcp_high_intent_sessions" not in low, name


def test_the_read_path_runs_no_ddl():
    """★ The capture table is created by the WRITE path on first capture. A
    read endpoint that CREATEs it would make the funnel a schema author, and
    its absence must read as 'fall back to v1', not as an error."""
    fn = next(n for n in ast.walk(ast.parse(FUNNEL))
              if isinstance(n, ast.FunctionDef)
              and n.name == "_identify_capture_table_present")
    body = ast.get_source_segment(FUNNEL, fn) or ""
    assert "to_regclass" in body, "not asking the catalog"
    for ddl in ("CREATE ", "create table", "ensure_schema"):
        assert ddl not in body, f"the read path runs {ddl!r}"


def test_an_absent_capture_table_falls_back_to_v1_and_says_so():
    i = FUNNEL.index("emailed_v2 = (one(_identified_count_sql(iv))")
    seg = FUNNEL[i - 400:i + 700]
    assert "_identify_capture_table_present()" in seg
    assert "emailed = emailed_v2 if emailed_v2 is not None else emailed_v1" in seg
    assert '"identified_definition_applied": 2 if emailed_v2 is not None else 1' \
        in FUNNEL


# ── published honestly ───────────────────────────────────────────────────
def test_the_version_and_changelog_are_published():
    d = identified_definition()
    assert d["definition_version"] == IDENTIFIED_DEFINITION_VERSION == 2
    assert set(d["changelog"]) == {1, 2}
    assert d["basis"] == IDENTIFIED_BASIS
    assert '"identified": _identified_definition()' in FUNNEL, (
        "the definition is not published on the endpoint")


def test_the_changelog_names_the_defect_not_just_the_change():
    """A changelog entry that says what changed but not what was WRONG is how
    the next reader re-litigates it."""
    v2 = IDENTIFIED_DEFINITION_CHANGELOG[2]
    assert "paid_attributed 1" in v2 and "identified 0" in v2
    assert "statelessly" in v2


def test_the_basis_separates_this_number_from_reached_the_rung():
    """★ They are different questions and Grok's acceptance criterion
    conflated them. reached_the_rung measures 'had a high-intent row to
    stamp' — it will stay 0 and that is the diagnosis."""
    assert "reached_the_rung" in IDENTIFIED_BASIS
    assert "is NOT this number" in IDENTIFIED_BASIS


def test_the_siblings_are_published():
    for field in ('"identified_v1_high_intent_rows"',
                  '"identified_from_relay_captures"',
                  '"identified_including_self_traffic"',
                  '"identified_basis"'):
        assert field in FUNNEL, field


def test_the_funnel_calls_the_definition_and_does_not_restate_it():
    """The one-writer rule this module exists to enforce. A second copy of the
    union in the endpoint is how human_acted's four surfaces rotted."""
    i = FUNNEL.index("emailed_v1 = one(_identified_v1_sql(iv))")
    seg = FUNNEL[i:i + 900]
    assert "_identified_count_sql(iv)" in seg
    assert "relay_identify_captures" not in seg, (
        "the endpoint spells the union itself instead of calling it")
