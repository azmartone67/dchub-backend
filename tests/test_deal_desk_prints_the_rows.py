"""A count is not an answer: the brief must print the rows, not "12 items".

`top_level_findings` renders any list as "<n> items". For a ranking question the
n rows it is counting ARE the answer, so the printed brief for "rank markets for
a 200 MW AI campus in Texas" named ZERO of its twelve shortlisted markets and
said `shortlist  12 items` instead. That is strictly less than the tool call it
was made from.

★ THE PREMISE IS PINNED, NOT ASSUMED. The two fixtures are the SAME live run
(2026-09-10, enterprise key), captured on both sides of the slim/full swap:

    deal_desk_plan_execution_slim.json   what execute_plan puts in the envelope
    deal_desk_plan_execution_full.json   the pre-slim results the loopback held

Eleven of the twelve markets exist nowhere in the slim one — `_slimStepResult`
cuts the preview mid-way through market #2 — so "the brief is richer" is a
claim about a value that could not have come from the envelope. The control
matters as much as the outcome: a test that only asserted the twelve names on
the full page would still pass if the slim envelope quietly started carrying
them, and the product claim would be hollow with the test green.

The fan-out shape is kept verbatim (two `step 2` rows, two `step 3` rows) —
trimming a real fixture below its shape is how a fan-out defect gets a green
suite (dchub-backend#4356).
"""
import json
import os

import pytest

from routes.deal_desk import brief_model, render_brief_html, step_tables

HERE = os.path.dirname(os.path.abspath(__file__))
SLIM = os.path.join(HERE, "fixtures", "deal_desk_plan_execution_slim.json")
FULL = os.path.join(HERE, "fixtures", "deal_desk_plan_execution_full.json")

# The twelve markets site_selection_canvas returned for this run, in rank order.
# Written out rather than derived from the fixture: a list read out of the same
# file it is checking against passes no matter what that file says.
SHORTLIST = ["Midland", "El Paso", "Woodlands", "McAllen", "Katy", "Midlothian",
             "Red Oak", "Garland", "Fort Worth", "Carrollton", "Houston",
             "Richardson"]


def _load(path):
    with open(path) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def slim_env():
    return _load(SLIM)


@pytest.fixture(scope="module")
def full_env():
    return _load(FULL)


# ── the premise ────────────────────────────────────────────────────────────

def test_the_two_fixtures_are_the_same_run(slim_env, full_env):
    """Otherwise the comparison below is between two different questions."""
    assert slim_env["intent"] == full_env["intent"]
    assert slim_env["intent_class"] == full_env["intent_class"]
    slim_steps = [(s["step"], s["tool"], s.get("args")) for s in slim_env["executed"]]
    full_steps = [(s["step"], s["tool"], s.get("args")) for s in full_env["executed"]]
    assert slim_steps == full_steps
    # …and the fan-out is intact: one planned step, several targets, one number.
    assert len(slim_steps) > len({s for s, _, _ in slim_steps}), (
        "fixture lost the fan-out — every step number is unique, so this suite "
        "can no longer see a defect that collapses variants of one step")


def test_the_shortlist_is_absent_from_the_slim_envelope(slim_env):
    """The control. Eleven of twelve names cannot come from what the agent saw."""
    blob = json.dumps(slim_env)
    missing = [m for m in SHORTLIST if m not in blob]
    assert len(missing) >= 10, (
        "the slim envelope now carries {} of the {} markets — the richness claim "
        "in this file is measuring something else".format(
            len(SHORTLIST) - len(missing), len(SHORTLIST)))


def test_the_full_fixture_really_carries_them(full_env):
    """A floor, so this suite cannot pass by finding nothing anywhere."""
    blob = json.dumps(full_env)
    assert all(m in blob for m in SHORTLIST)


# ── the outcome ────────────────────────────────────────────────────────────

def test_the_brief_from_full_names_every_shortlisted_market(full_env):
    html = render_brief_html(brief_model(full_env))
    absent = [m for m in SHORTLIST if m not in html]
    assert not absent, f"shortlisted markets missing from the printed brief: {absent}"


def test_the_brief_from_slim_names_none_of_them(slim_env):
    """The before picture, so the diff above is attributable to the swap."""
    html = render_brief_html(brief_model(slim_env))
    named = [m for m in SHORTLIST if m in html]
    assert len(named) <= 1, (
        f"the slim brief already names {named} — the full/slim difference this "
        "change exists to create is no longer where this test is looking")


def test_the_rows_carry_their_decision_columns(full_env):
    B = brief_model(full_env)
    tables = [t for s in B["steps"] for t in s["tables"]]
    assert [t["key"] for t in tables] == ["shortlist"]
    tb = tables[0]
    assert tb["total"] == 12 and tb["shown"] == 12
    assert "verdict" in tb["columns"] and "composite_score" in tb["columns"]
    top = tb["rows"][0]
    assert top["label"].startswith("Midland")
    assert "BUILD" in top["values"]
    html = render_brief_html(B)
    # The verdict must reach the page beside its market, not just the model.
    assert "BUILD" in html and "CAUTION" in html


# ── what must stay OFF the page ────────────────────────────────────────────

def test_a_24h_demand_series_stays_off_the_page(full_env):
    """`demand_24h` is 19 rows of {mw, period} — data for a chart, and 19 of them
    would bury the answer. It fails BOTH rules (no name, no ranked score), so
    this is the end-to-end case; the two rules are isolated below."""
    grid = next(s for s in full_env["executed"] if s["tool"] == "get_grid_intelligence")
    assert len(grid["result"]["demand_24h"]) >= 12, "fixture lost the negative case"
    keys = {t["key"] for t in step_tables(grid["result"])}
    assert "demand_24h" not in keys


def test_scored_rows_with_no_name_are_not_a_table():
    """The label rule ALONE. Nothing in the live fixture is scored-but-unnamed,
    so without this case dropping the label requirement changes no test result
    and the rule rots behind the score rule that happens to also reject it."""
    scored_only = [{"composite_score": 91}, {"composite_score": 74}]
    assert step_tables({"shortlist": scored_only}) == []
    # …and the same rows become a table the moment they name themselves, so the
    # assertion above is about the missing label and not about the rows.
    named = [dict(r, market=f"m{i}") for i, r in enumerate(scored_only)]
    assert [t["key"] for t in step_tables({"shortlist": named})] == ["shortlist"]


def test_a_list_that_names_but_never_scores_is_not_a_table(full_env):
    """`site_evaluation_handoff` rows are {tool, why, parameters} — a next-step
    hint, with no number to rank by."""
    dcpi = next(s for s in full_env["executed"] if s["tool"] == "get_market_dcpi_rank")
    assert dcpi["result"].get("site_evaluation_handoff"), "fixture lost the negative case"
    keys = {t["key"] for t in step_tables(dcpi["result"])}
    assert "site_evaluation_handoff" not in keys


def test_one_row_is_a_finding_not_a_table():
    """top_level_findings already prints a single row; a one-row table is noise."""
    one = [{"market": "Ashburn", "verdict": "BUILD", "composite_score": 80}]
    assert step_tables({"shortlist": one}) == []
    assert [t["key"] for t in step_tables({"shortlist": one + [dict(one[0], market="Reston")]})] \
        == ["shortlist"]


def test_a_long_list_says_how_many_it_left_out():
    rows = [{"market": f"m{i}", "composite_score": i} for i in range(40)]
    tb = step_tables({"shortlist": rows})[0]
    assert tb["total"] == 40
    assert tb["shown"] < 40
    html = render_brief_html(brief_model(
        {"executed": [{"step": 1, "tool": "t", "status": "executed",
                       "result": {"shortlist": rows}}]}))
    assert f"of {tb['total']} shortlist rows shown" in html
