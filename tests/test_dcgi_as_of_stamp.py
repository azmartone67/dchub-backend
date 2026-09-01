"""DCGI must date its score from its own inputs, oldest-wins.

★ THE DEFECT (measured live 2026-08-31, get_gas_index state=TX)

    "score": {"dcgi": 81.9, "verdict": "GAS-ADVANTAGED",
              "gas_price_period": "2026-05", ...}
    "provenance": {"as_of": null,
                   "as_of_basis": "UNMEASURED — this response carries no source
                                   timestamp. `retrieved_at` is when DC Hub
                                   served it, NOT when the data was collected."}

A real score, restored 2026-08-30 after the 08-08 withdrawal, published with no
data date — while the SAME payload carried the period of the price that produced
40% of it. The data had a date; the envelope never read it. An undated true score
is how a measurement becomes an undated claim in someone else's blog post, and
DCGI is the index that was withdrawn once already for publishing wrong numbers.

★ WHY OLDEST AND NOT NEWEST. DCGI = 0.60*access + 0.40*cost. The access half
comes from gas_pipelines, the cost half from EIA prices. A blend is no fresher
than its staler input — the rule this codebase already applies to composed
execute_plan envelopes ("an answer is no fresher than its stalest input"). MAX
would hide the stale half behind a fresher-looking number.

★ WHY THE TERRITORY SEEDS STAY UNDATED. DC/PR/VI/GU are seeded rows
(data_basis=modeled_territory*), present precisely BECAUSE gas_pipelines has no
rows for them, with a documented placeholder price. UNMEASURED is the correct
answer for a model. Stamping one would be this same defect inverted, and worse:
the output would look measured.
"""
import datetime

import pytest

from routes.dcgi import _as_date, _period_to_date, _stamp_as_of

AUG = datetime.datetime(2026, 8, 3, 5, 30, tzinfo=datetime.timezone.utc)


# ── the period parser ────────────────────────────────────────────────────────
@pytest.mark.parametrize("period,expected", [
    ("2026-05", datetime.date(2026, 5, 1)),      # monthly, the real-state shape
    ("2026-5", datetime.date(2026, 5, 1)),       # unpadded month
    ("2026-Q2", datetime.date(2026, 4, 1)),      # quarterly, the seed shape
    ("2026-Q1", datetime.date(2026, 1, 1)),
    ("2026-05-17", datetime.date(2026, 5, 17)),  # already a full date
    ("2026-13", None),                            # month 13 is not a month
    ("garbage", None),
    ("", None),
    (None, None),
])
def test_period_to_date(period, expected):
    assert _period_to_date(period) == expected


def test_period_is_dated_from_its_FIRST_day():
    """A month labelled 2026-05 covers all of May, so the earliest moment it can
    honestly claim is the 1st. Dating it to the end would make the figure look up
    to a month fresher than it is — the failure this whole change exists to fix."""
    assert _period_to_date("2026-05") == datetime.date(2026, 5, 1)


# ── the normaliser ───────────────────────────────────────────────────────────
def test_as_date_flattens_tz_aware_timestamps():
    assert _as_date(AUG) == datetime.date(2026, 8, 3)
    assert _as_date(datetime.date(2026, 8, 3)) == datetime.date(2026, 8, 3)
    assert _as_date(None) is None


def test_mixed_types_do_not_raise():
    """created_at is a tz-aware timestamp, a period start is a date, and min()
    across those two raises TypeError rather than choosing. Regression guard for
    the crash this would otherwise be on every real state."""
    row = {"gas_price_period": "2026-05"}
    _stamp_as_of(row, AUG)          # must not raise
    assert row["data_as_of"] == "2026-05-01"


# ── the stamp ────────────────────────────────────────────────────────────────
def test_stamps_the_OLDEST_of_the_two_inputs():
    row = {"gas_price_period": "2026-05"}          # price older than pipelines
    _stamp_as_of(row, AUG)
    assert row["data_as_of"] == "2026-05-01"
    assert "gas price" in row["data_as_of_basis"]


def test_stamps_pipelines_when_pipelines_are_the_stale_half():
    row = {"gas_price_period": "2026-08"}           # price NEWER than pipelines
    _stamp_as_of(row, datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc))
    assert row["data_as_of"] == "2026-03-01"
    assert "gas_pipelines" in row["data_as_of_basis"]


def test_never_takes_the_fresher_input():
    """The whole point. MAX would publish 2026-08 for a score half-built on
    March data."""
    row = {"gas_price_period": "2026-08"}
    _stamp_as_of(row, datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc))
    assert row["data_as_of"] < "2026-08-01"


def test_modeled_territory_rows_are_left_UNMEASURED():
    for basis in ("modeled_territory", "modeled_territory_lng"):
        row = {"gas_price_period": "2026-Q2", "data_basis": basis}
        _stamp_as_of(row, AUG)
        assert "data_as_of" not in row, (
            f"a {basis} row was dated. Those rows exist BECAUSE gas_pipelines has "
            "no data for them and their price is a documented placeholder — "
            "UNMEASURED is the correct answer, and a date here would look measured.")


def test_a_row_with_no_dated_input_is_left_UNMEASURED():
    row = {"gas_price_period": None}
    _stamp_as_of(row, None)
    assert "data_as_of" not in row, "an undated row was given a date out of nothing"


def test_basis_names_both_inputs_and_which_one_is_stalest():
    row = {"gas_price_period": "2026-05"}
    _stamp_as_of(row, AUG)
    b = row["data_as_of_basis"]
    assert "gas_pipelines ingest 2026-08-03" in b
    assert "2026-05" in b
    assert "Stalest:" in b


def test_undated_pipelines_say_UNDATED_rather_than_guessing():
    row = {"gas_price_period": "2026-05"}
    _stamp_as_of(row, None)
    assert row["data_as_of"] == "2026-05-01"
    assert "UNDATED" in row["data_as_of_basis"]


def test_data_as_of_is_the_key_the_mcp_attribution_layer_reads():
    """dchub-mcp-server/lib/attribution.mjs COLLECTION_AS_OF_KEYS is
    {as_of, as_of_date, data_as_of, generated_at, snapshot_date}. Emitting any
    other name leaves provenance.as_of null and the whole fix is a no-op."""
    row = {"gas_price_period": "2026-05"}
    _stamp_as_of(row, AUG)
    assert "data_as_of" in row, (
        "the stamped key must be one the MCP attribution layer collects, or "
        "provenance.as_of stays null and get_gas_index keeps saying UNMEASURED")
