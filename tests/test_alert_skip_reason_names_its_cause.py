"""A skip reason must name the condition it measured, not a category.

`_current_dcpi_for_market` returned a bare None, and the caller printed
"no_current_value" for THREE different conditions:

    a swallowed exception   (the KeyError(0) fixed in #3989)
    no matching market row
    a row whose score is NULL

Only the first was a bug — but all three produced the same string, so after
#3989 deployed there was NO WAY to tell from outside whether the fix had worked.
Verified: the fix was an ancestor of the live deploy and the deployed tree
carried it, yet the observable was byte-identical to the broken state.

That is the same defect the fix was about, one level up: one message standing
for several conditions. A reason that cannot distinguish success from failure
is not a diagnostic.
"""
import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import lp_alerts_cron as lp  # noqa: E402


class RealDictRow(dict):
    pass


class _Cur:
    """Minimal RealDictCursor stand-in: fetchone returns what it was given."""
    def __init__(self, row=None, boom=None):
        self._row, self._boom = row, boom
    def execute(self, *a, **k):
        if self._boom:
            raise self._boom
    def fetchone(self):
        return self._row


# The query returns the four DCPI components; the helper composites them the
# same way routes/lp_sites.py derives dcpi_score_at_save, so current and
# baseline are the same quantity.
_ROW = RealDictRow({"excess": 71.5, "constraint_s": 40.0,
                    "ttp": 24.0, "verdict": "BUILD"})


def test_a_real_score_returns_a_composite_and_no_reason():
    v, why = lp._current_dcpi_for_market(_Cur(_ROW), "ashburn", 39.0, -77.4)
    assert why is None
    assert isinstance(v, float) and 0.0 <= v <= 100.0
    from routes.dcpi import derive_composite_score
    assert v == float(derive_composite_score(71.5, 40.0, 24.0, "BUILD")), (
        "the helper must return the SAME composite the baseline was derived "
        "from, not one of its components")


def test_no_matching_market_says_so_and_names_the_market():
    v, why = lp._current_dcpi_for_market(_Cur(None), "birstein", 50.3, 9.3)
    assert v is None
    assert why.startswith("market_not_in_scores")
    assert "birstein" in why, "the reason must name the market that did not match"


def test_a_null_score_is_distinct_from_a_missing_row():
    """These are different problems: one is a misconfigured alert, the other is
    a market awaiting its first score."""
    v, why = lp._current_dcpi_for_market(
        _Cur(RealDictRow({"excess": None})), "ashburn", 39.0, -77.4)
    assert v is None and why.startswith("score_is_null")


def test_an_exception_names_its_type_instead_of_vanishing():
    """★ THE ONE THAT HID #3989's BUG. A swallowed exception looked exactly like
    'no data' for as long as it existed."""
    v, why = lp._current_dcpi_for_market(
        _Cur(boom=KeyError(0)), "ashburn", 39.0, -77.4)
    assert v is None
    assert why.startswith("dcpi_lookup_failed")
    assert "KeyError" in why


def test_an_alert_with_no_market_is_its_own_reason():
    v, why = lp._current_dcpi_for_market(_Cur(None), None, 39.0, -77.4)
    assert v is None and why == "alert_has_no_market"


@pytest.mark.parametrize("row,boom,expect", [
    (_ROW, None, None),
    (None, None, "market_not_in_scores"),
    (RealDictRow({"excess": None}), None, "score_is_null"),
    (None, KeyError(0), "dcpi_lookup_failed"),
])
def test_every_outcome_is_distinguishable(row, boom, expect):
    """★ THE POINT. Four conditions, four distinct answers. Previously all four
    produced the identical string 'no_current_value'."""
    _v, why = lp._current_dcpi_for_market(_Cur(row, boom), "m", 1.0, 2.0)
    if expect is None:
        assert why is None
    else:
        assert why.startswith(expect)


def test_reasons_are_pairwise_distinct():
    seen = {
        lp._current_dcpi_for_market(_Cur(None), "m", 1.0, 2.0)[1],
        lp._current_dcpi_for_market(_Cur(RealDictRow({"excess": None})), "m", 1.0, 2.0)[1],
        lp._current_dcpi_for_market(_Cur(None, KeyError(0)), "m", 1.0, 2.0)[1],
        lp._current_dcpi_for_market(_Cur(None), None, 1.0, 2.0)[1],
    }
    assert len(seen) == 4, f"reasons collapsed into {seen}"


def test_the_caller_carries_the_specific_reason_through():
    """A helper that names its cause is useless if the caller overwrites it."""
    src = open(os.path.join(_ROOT, "routes", "lp_alerts_cron.py"),
               encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "fire_pending_alerts")
    body = ast.get_source_segment(src, fn) or ""
    assert 'dcpi_why or "no_current_value"' in body, (
        "the caller no longer forwards the helper's specific reason")
