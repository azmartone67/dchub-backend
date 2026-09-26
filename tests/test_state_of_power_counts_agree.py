"""/api/v1/reports/state-of-power: every count describes one stated population.

Audit 2026-09-26, live: summary.markets_scored 331, scored_market_count 33, and
verdict_distribution 33 BUILD / 70 CAUTION / 100 AVOID = 203. The split came
off /api/v1/dcpi/leaderboard, which clamps limit to 100 (AVOID stopped at
exactly 100), and it carried the three aggregate rural regions canonical_stats
excludes from `markets` (rural-spp and upper-michigan were #3 and #4 of the
BUILD top 10). No network, no DB: the leaderboard self-call and the SQL count
are stubbed.
"""
import re
import pathlib

import pytest

import routes.energy_report as er
import routes.state_of_power as sop

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _row(slug, verdict, composite):
    return {"market_slug": slug, "market_name": slug.title(), "iso": "SPP",
            "verdict": verdict, "composite_score": composite,
            "excess_power_score": 50, "constraint_score": 20,
            "time_to_power_months": 12}


# The shape measured live: BUILD carries 2 aggregate regions, CAUTION 1, and
# AVOID is truncated at the leaderboard's cap.
_BOARD = {
    "BUILD": [_row("rural-spp", "BUILD", 80), _row("upper-michigan", "BUILD", 79)]
             + [_row(f"b{i}", "BUILD", 70 - i) for i in range(31)],
    "CAUTION": [_row("pacific-nw-rural", "CAUTION", 40)]
               + [_row(f"c{i}", "CAUTION", 30) for i in range(69)],
    "AVOID": [_row(f"a{i}", "AVOID", 10) for i in range(100)],
    "LOW_SIGNAL": [],
}
_COUNTED = {"BUILD": 31, "CAUTION": 69, "AVOID": 231}


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"leaderboard": [dict(r) for r in self._rows]}


@pytest.fixture
def gathered(monkeypatch):
    import requests

    def _get(url, params=None, **kw):
        if "dcpi/leaderboard" in url:
            return _Resp(_BOARD[params["verdict"]])
        raise RuntimeError("unexpected self-call %s" % url)

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(sop, "_fuel_block", lambda: {"fuel_mix": []})
    monkeypatch.setattr(sop, "_canon_mkts", lambda default=300: 331)

    def run(counted):
        monkeypatch.setattr(er, "_dcpi_verdict_population", lambda: counted)
        e = er._gather_energy_uncached("monthly")
        monkeypatch.setattr(er, "_gather_energy", lambda w: dict(e))
        return sop._gather()
    return run


def test_verdicts_sum_to_markets_scored_and_the_ranking_pool_is_inside_it(gathered):
    d = gathered(dict(_COUNTED))
    v = d["verdict_distribution"]
    assert sum(v.values()) == d["summary"]["markets_scored"] == 331
    assert d["scored_market_count"] <= v["BUILD"]
    assert d["scored_market_count"] == 31
    for key in ("markets_scored", "verdict_distribution", "scored_market_count"):
        assert d["populations"][key], key
    assert "sums to it" in d["populations"]["verdict_distribution"]


def test_aggregate_regions_are_not_ranked_as_markets(gathered):
    d = gathered(dict(_COUNTED))
    slugs = {r["slug"] for r in d["build_markets"] + d["avoid_markets"]}
    assert not slugs & set(er._DCPI_AGGREGATE_REGION_SLUGS)


def test_a_capped_fallback_is_declared_incomplete_not_passed_off(gathered):
    d = gathered(None)          # the SQL count is unavailable
    v = d["verdict_distribution"]
    assert v["AVOID"] == 100    # the cap, so the split is partial …
    assert d["summary"]["markets_scored"] == 331
    assert "INCOMPLETE" in d["populations"]["verdict_distribution"]


def test_the_aggregate_slugs_match_canon():
    dcpi = (_ROOT / "routes" / "dcpi.py").read_text(encoding="utf-8")
    m = re.search(r"^_DCPI_AGGREGATE_REGION_SLUGS = (\(.*?\))$", dcpi, re.M)
    assert m and eval(m.group(1)) == er._DCPI_AGGREGATE_REGION_SLUGS
    canon = (_ROOT / "canonical_stats.py").read_text(encoding="utf-8")
    assert "('pacific-nw-rural','rural-spp','upper-michigan')" in canon
