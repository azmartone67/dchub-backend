#!/usr/bin/env python3
"""tests/test_market_rotation_covers_every_market.py — the scheduled sweep must
be able to reach EVERY market, at whatever cadence production actually runs.

NO NETWORK, NO DB. The real `_default_market_index` is exercised against a
stubbed clock.

THE BUG THIS FENCES (2026-09-02). `_default_market_index` computed

    slot = (now.date().toordinal() * 4 + now.hour // 6) % slots

The `* 4` encoded the four-runs-a-day cadence its own docstring described.
Production runs CRAWLER_SCHEDULE=once — a single fire at 12:00 UTC — so
`hour // 6` was pinned at 2 and the slot advanced by 4 per day against 10 slots.
gcd(4, 10) == 2, so only even slots were ever produced and ten of twenty markets
were unreachable: Phoenix, Chicago, Los Angeles, New York Metro, Salt Lake City,
Columbus, Reno, Des Moines, Denver and Houston had never been swept.

Measured consequence: fiber_routes added ~4,800 rows in the week ending
2026-08-20 and +2 rows in the twelve days after, while the growth board still
reported "growing".

★ The property under test is COVERAGE UNDER A CADENCE, not the arithmetic of any
one call. A test that asserts a specific slot for a specific date passes just as
happily with the multiplier restored — it has to sweep a realistic span of days
at the cadence production actually uses.

Run standalone:   python3 tests/test_market_rotation_covers_every_market.py
Run under pytest: pytest tests/test_market_rotation_covers_every_market.py
"""
import datetime as _dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import infrastructure_discovery as I  # noqa: E402


class _FrozenClock:
    """Stands in for the module's `datetime`, returning a chosen instant."""

    def __init__(self, moment):
        self._moment = moment

    def utcnow(self):
        return self._moment


def _markets_reached(hours, days=400, start=_dt.date(2026, 9, 2)):
    """Every market index the scheduled path can reach, firing at `hours` daily."""
    real = I.datetime
    reached = set()
    try:
        for offset in range(days):
            day = start + _dt.timedelta(days=offset)
            for hour in hours:
                I.datetime = _FrozenClock(_dt.datetime(day.year, day.month, day.day, hour))
                base = I.FiberRouteDiscovery._default_market_index()
                for step in range(I.FiberRouteDiscovery.MARKETS_PER_RUN):
                    reached.add((base + step) % len(I.DC_MARKETS))
    finally:
        I.datetime = real
    return reached


def _all_markets():
    return set(range(len(I.DC_MARKETS)))


def test_once_daily_cadence_reaches_every_market():
    """CRAWLER_SCHEDULE=once — the deployed cadence. This is the regression."""
    reached = _markets_reached(hours=[12])
    missing = sorted(_all_markets() - reached)
    assert not missing, (
        "once-daily firing cannot reach market indices %s — %s. A stride that "
        "shares a factor with the slot count strands half the rotation."
        % (missing, [I.DC_MARKETS[i].get("name", i) if isinstance(I.DC_MARKETS[i], dict)
                     else I.DC_MARKETS[i] for i in missing])
    )


def test_four_daily_cadence_still_reaches_every_market():
    """The cadence the docstring was written for must not regress."""
    missing = sorted(_all_markets() - _markets_reached(hours=[0, 6, 12, 18]))
    assert not missing, "four-runs-a-day lost coverage of %s" % (missing,)


def test_every_off_hour_cadence_reaches_every_market():
    """Coverage must not depend on WHICH hour the single daily run lands on.

    CRAWLER_SCHEDULE moves; a fix that only works at 12:00 is not a fix.
    """
    for hour in range(24):
        missing = sorted(_all_markets() - _markets_reached(hours=[hour], days=120))
        assert not missing, "a single daily run at %02d:00 UTC strands %s" % (hour, missing)


def test_rotation_does_not_assume_a_runs_per_day_multiplier():
    """Guards the specific shape that caused this: a runs-per-day factor applied
    to the day term.

    ★ AST, not substring. This function's own docstring quotes the broken
    expression verbatim to explain the bug, so any grep for `toordinal() *` over
    the source matches the prose and fires on the FIXED code. Parsing the
    expression tree looks at the arithmetic and nothing else.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(I.FiberRouteDiscovery._default_market_index)))

    ordinal_mults = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)
        and any(isinstance(d, ast.Attribute) and d.attr == "toordinal"
                for d in ast.walk(node.left))
    ]
    assert not ordinal_mults, (
        "the day term is multiplied by a constant — that is the exact defect. "
        "Any multiplier sharing a factor with the slot count strands markets at "
        "the deployed once-daily cadence; the day term must step by 1."
    )

    # And confirm the guard is anchored to real code, not passing vacuously
    # because the expression moved somewhere this walk cannot see it.
    assert any(isinstance(n, ast.Attribute) and n.attr == "toordinal" for n in ast.walk(tree)), \
        "no toordinal() call found — this guard is no longer anchored to the slot computation"


if __name__ == "__main__":
    test_once_daily_cadence_reaches_every_market()
    test_four_daily_cadence_still_reaches_every_market()
    test_every_off_hour_cadence_reaches_every_market()
    test_rotation_does_not_assume_a_runs_per_day_multiplier()
    print("ok")
