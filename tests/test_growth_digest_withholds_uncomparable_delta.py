"""The Growth-Ops north star must not publish a WoW across a definition change.

On 2026-09-09 the digest mailed, in its SUBJECT LINE:

    NORTH STAR · distinct real agents week-to-date: 5 (-25 vs same point last wk)

Both sides read `mcp_calls_identity WHERE is_real_external`, a column STORED AT
INGEST — so rows written before dchub-backend#3962 (2026-09-05 23:47:13Z) keep
the looser classification forever and the two sides count different populations.
flask_mcp_endpoints.py has withheld exactly this comparison since 2026-08-20.
_north_star() never asked; it re-derived nothing and checked nothing.

Note what the fix is NOT: the registry was already returning
quotable_as_trend=False for those windows on the strength of the superseding
corrections alone. The digest would have withheld the delta the day it shipped
if it had simply consulted the helper. Registering #3962 names the real cause
and covers the `crosses` case; consulting the verdict is what stops the number.

All times here are PINNED. A test about a window boundary that reads the wall
clock stops testing the boundary the moment the clock moves past it.
"""
import datetime as dt

import pytest

from routes.weekly_series import _DEFINITION_CHANGES, comparability_for_spans

SELF_BRANDING = "dchub-backend#3962"
EFFECTIVE = dt.datetime(2026, 9, 5, 23, 47, 13, tzinfo=dt.UTC)


def _refs(verdict):
    return {c.get("ref") for c in
            (verdict.get("changes") or []) + (verdict.get("superseded_by") or [])
            if isinstance(c, dict)}


def test_self_branding_change_is_registered():
    assert any(c.get("ref") == SELF_BRANDING for c in _DEFINITION_CHANGES), (
        "#3962 changed which callers count as real and went five days "
        "unregistered. An unregistered change is one every consumer of "
        "comparability_for_spans reads as safe."
    )


def test_it_fires_on_a_window_that_contains_it():
    v = comparability_for_spans([(EFFECTIVE - dt.timedelta(hours=1),
                                  EFFECTIVE + dt.timedelta(hours=1))])
    assert v.get("crosses_definition_change") is True
    assert SELF_BRANDING in _refs(v)


def test_it_does_not_fire_on_a_window_before_it():
    """A marker that fires on everything is not a marker."""
    v = comparability_for_spans([(dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
                                  dt.datetime(2026, 9, 4, tzinfo=dt.UTC))])
    assert SELF_BRANDING not in {c.get("ref") for c in (v.get("changes") or [])}


# ── the render site ────────────────────────────────────────────────────────

def _headline(monkeypatch, comparability):
    import routes.growth_ops_digest as g
    monkeypatch.setattr(g, "_shell_lanes", lambda _m: {}, raising=False)
    monkeypatch.setattr(g, "_north_star", lambda: {
        "agents_wk": 5, "agents_prev_wk": 44, "agents_prev_wtd": 30,
        "conv_30d": 4, "comparability": comparability,
    }, raising=False)
    d = g._build_digest()
    # Read "text" by KEY. A str(d) fallback renders the whole dict on one line,
    # where every substring assertion below passes off the repr — the trap
    # tests/test_growth_digest_counts_what_it_prints.py already documents.
    assert isinstance(d, dict) and "text" in d, (
        sorted(d) if isinstance(d, dict) else type(d))
    return next(l for l in d["text"].split("\n") if "NORTH STAR" in l)


@pytest.mark.parametrize("verdict,label", [
    ({"quotable_as_trend": False, "crosses_definition_change": True,
      "changes": [{"ref": SELF_BRANDING}]}, "crosses a change"),
    ({"quotable_as_trend": False, "superseded_by_correction": True,
      "superseded_by": [{"ref": "dchub-mcp-server#294"}]}, "superseded"),
    ({"error": "db down"}, "verdict errored"),
    (None, "no verdict at all"),
])
def test_delta_is_withheld_when_not_quotable(monkeypatch, verdict, label):
    line = _headline(monkeypatch, verdict)
    assert "WITHHELD" in line, f"{label}: delta was published anyway — {line!r}"
    assert "-25" not in line and "vs same point last wk)" not in line, (
        f"{label}: the number is still in the line — {line!r}")
    assert "5" in line, f"{label}: the LEVEL must survive; only the delta goes"


def test_delta_is_published_when_the_verdict_is_clean():
    """The refusal has to be conditional. A headline that never prints a delta
    passes every assertion above and tells the reader nothing, forever."""
    import routes.growth_ops_digest as g
    mp = pytest.MonkeyPatch()
    try:
        line = _headline(mp, {"quotable_as_trend": True})
    finally:
        mp.undo()
    assert "WITHHELD" not in line
    assert "-25 vs same point last wk" in line, line
