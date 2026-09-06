"""One market_power_scores row, three surfaces, one set of numbers.

r-ttp-one-precision (2026-09-06).

WHAT WAS MEASURED
-----------------
Live, cache-busted 2026-09-06, immediately after #4016:

    market    /api/v1/dcpi/scores   /markets Dataset   /pockets Dataset
    ashburn   24.2                  24.2               24.0
    dallas    66.6                  66.6               67.0

Same row. Same `Observed` timestamp in both Datasets. DCPI Score, Excess Power
Score and Grid Constraint Score identical to the decimal. Only Time to Power
disagreed — a pure PRECISION disagreement: /pockets rounded to whole months,
the other two publish 1dp.

★ AND I SHIPPED IT. The 0dp rounding had been in routes/pockets.py for a long
time and was harmless while the value was only ever DISPLAYED — all three
templates render it through `|int`, so "24mo" either way. #4010 turned it into
a published measure and #4016 gave /markets the same measure at 1dp, and the
two together turned a display convention into a third published number for one
measurement.

★ WHY THE EXISTING PARITY GUARD MISSED IT. #4016's test asserted the two
surfaces publish the same measure NAMES. Names matched perfectly; the values
did not. Name parity is not agreement, and this file is the difference.

WHAT THIS GUARD ASSERTS
-----------------------
Given ONE row, the stats dict each surface hands util.market_entity must carry
identical values for every measure they share. Executed against the real
readers — a re-implementation of either surface's rounding here would agree
with itself and prove nothing.
"""
import datetime

import pytest

from routes import market_deep_dive as mdd
from routes import pockets as pk
from util.market_entity import market_entity

#: The measures both surfaces publish, BY THEIR PUBLISHED NAMES. `Facilities`
#: / `Total Capacity` are /markets-only (they come from the narrative
#: snapshot) and the deployability rank is /pockets-only, so neither belongs
#: in an agreement test.
#:
#: ★ COMPARED AS PUBLISHED MEASURES, not as the stats dicts the surfaces build.
#: The first version of this file compared the dicts and found /markets
#: carrying 46.13 where /pockets carried 46.1 — real, but upstream of the
#: point where the shared builder rounds. What ships is what has to agree, and
#: putting the rounding in util.market_entity is what makes it agree by
#: construction instead of by two callers remembering.
SHARED = ("DCPI Score", "Excess Power Score", "Grid Constraint Score",
          "Time to Power")

#: One row, with values chosen so every rounding rule is visible AT 1dp vs
#: 0dp for every field: 46.13 -> 46.1/46.0, 60.44 -> 60.4/60.0,
#: 24.23 -> 24.2/24.0. A probe where any of them collapses to the same number
#: under both rules cannot tell a correct precision from a lossy one.
ROW_MDD = (60.44, 46.13, "AVOID", 24.23,
           datetime.datetime(2026, 9, 6, 6, 33, 27))
#: routes/pockets orders its SELECT differently; same numbers.
ROW_PK = ("ashburn", "Ashburn", "PJM", "VA", "AVOID", 46.13, 60.44, 24.23,
          datetime.datetime(2026, 9, 6, 6, 33, 27))


def _measures(stats):
    """{measure name: published value} — what util.market_entity emits."""
    node = market_entity("ashburn", "Ashburn", stats, canonical_slug="ashburn")
    return {v["name"]: v["value"] for v in node["variableMeasured"]}


def _markets_stats(monkeypatch):
    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return ROW_MDD
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Conn:
        def cursor(self): return Cur()
        def close(self): pass
    monkeypatch.setattr(mdd, "_conn", lambda: Conn())
    stats, _stored, _live = mdd.read_live_stats("ashburn", {})
    return _measures(stats)


def _pockets_stats(monkeypatch):
    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return ROW_PK
        def fetchall(self): return []
    class Conn:
        def cursor(self): return Cur()
        def rollback(self): pass
    monkeypatch.setattr(pk, "_get_db", lambda: Conn())
    monkeypatch.setattr(pk, "_return_db", lambda c: None)
    d = pk._fetch_pocket_detail("ashburn")
    return _measures({
        "dcpi_score":           d["dcpi_score"],
        "excess_power_score":   d["excess_power_score"],
        "constraint_score":     d["constraint_score"],
        "time_to_power_months": d["time_to_power_months"],
    })


def test_the_shared_set_is_not_empty():
    """Anti-vacuity: emptied, every comparison below passes trivially."""
    assert len(SHARED) == 4


@pytest.mark.parametrize("field", SHARED)
def test_both_surfaces_publish_the_same_value_for_one_row(field, monkeypatch):
    mk = _markets_stats(monkeypatch)
    pkt = _pockets_stats(monkeypatch)
    assert field in mk, f"/markets stopped carrying {field}"
    assert field in pkt, f"/pockets stopped carrying {field}"
    assert mk[field] == pkt[field], (
        f"one row, two published values for {field}: /markets={mk[field]!r} "
        f"/pockets={pkt[field]!r}. Name parity is not agreement — #4016's "
        f"guard checked the measure NAMES and both surfaces passed it while "
        f"serving 24.2 and 24.0 for the same market.")


def test_time_to_power_keeps_the_precision_dcpi_publishes(monkeypatch):
    """The specific regression. /api/v1/dcpi/scores serves 1dp and owns the
    number; a surface that rounds it to whole months publishes a third,
    lossier answer to a question already answered."""
    assert _pockets_stats(monkeypatch)["Time to Power"] == 24.2
    assert _markets_stats(monkeypatch)["Time to Power"] == 24.2


def test_the_probe_row_can_actually_distinguish_the_roundings():
    """A row whose values are already whole numbers would make every test
    here pass under BOTH rounding rules."""
    for v in (46.13, 60.44, 24.23):
        assert round(v, 1) != round(v, 0), v


DCPI_PRECISION = {"Excess Power Score": 46.1, "Grid Constraint Score": 60.4,
                  "Time to Power": 24.2}


@pytest.mark.parametrize("name,expected", sorted(DCPI_PRECISION.items()))
def test_the_published_precision_matches_what_dcpi_serves(name, expected,
                                                          monkeypatch):
    """Agreement is not enough — BOTH surfaces could round to whole numbers,
    agree with each other perfectly, and disagree with
    /api/v1/dcpi/scores, which owns these values and serves 1dp.

    A mutation that dropped the shared builder to 0dp survived the agreement
    tests above for exactly that reason. This is the half that catches it.
    """
    for surface, got in (("markets", _markets_stats(monkeypatch)),
                         ("pockets", _pockets_stats(monkeypatch))):
        assert got[name] == expected, (
            f"/{surface} publishes {name}={got[name]!r}; /dcpi serves "
            f"{expected!r} for the same row")


def test_display_is_unchanged_by_the_precision(monkeypatch):
    """All three templates render this through `|int`, which is why the 0dp
    rounding stayed invisible for so long — and why raising the precision
    changes no rendered page."""
    assert int(_pockets_stats(monkeypatch)["Time to Power"]) == 24
