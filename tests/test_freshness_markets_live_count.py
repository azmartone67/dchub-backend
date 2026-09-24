"""/api/health/data-freshness: the markets feed publishes the live count, never the seed.

r-markets-live (2026-09-24). feeds['markets'].record_count was len(SAMPLE_MARKETS),
a 16-row seed list, published as the size of the markets feed beside a "300+
markets" canon. An agent reading get_backup_status (Grok, live, 2026-09-24) saw
null (anonymous masking) with record_count_source 'static_constant' and no live
figure behind the claim; the REST value was 16.

The feed now carries canonical_stats' measured market count (the one the canon
floors) when stat_is_live('markets') is true, and null + 'unmeasured' when it is
not: never the seed. This executes the handler's REAL block, cut out of main.py
by its markers, with canonical_stats stubbed and the freshness helper replaced.
No DB and no network.
"""
import ast
import pathlib
import sys
import textwrap
import types

import pytest

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SRC = MAIN.read_text(encoding="utf-8")
START = "        try:\n            from canonical_stats import get_canonical_stats, stat_is_live\n"
END = "            'health': _fr_mk['health'] if _mk_live else 'unknown'\n        }\n"


def _block():
    i = SRC.index(START)
    j = SRC.index(END, i) + len(END)
    assert j - i < 4000, "markers matched across more than the markets block"
    return textwrap.dedent(SRC[i:j])


def _run(monkeypatch, markets, live):
    stub = types.ModuleType("canonical_stats")
    stub.get_canonical_stats = lambda: {"markets": markets, "facilities": 24500}
    stub.stat_is_live = lambda key: live and key == "markets"
    monkeypatch.setitem(sys.modules, "canonical_stats", stub)
    seen = {}

    def _freshness_of(table, count, interval):
        seen.update(table=table, count=count, interval=interval)
        return {"freshness_source": "market_power_scores.computed_at",
                "last_updated": "2026-09-24T04:52:00", "newest_record": "2026-09-24T04:52:00",
                "health": "healthy"}

    ns = {"feeds": {}, "_freshness_of": _freshness_of}
    exec(compile(_block(), "main.py:data_freshness[markets]", "exec"), ns)
    return ns["feeds"]["markets"], seen


def test_a_measured_count_is_published_as_live(monkeypatch):
    feed, seen = _run(monkeypatch, 312, live=True)
    assert feed["record_count"] == 312
    assert feed["record_count_source"] == "live_table"
    assert "markets_phrase" in feed["record_count_basis"]
    assert seen == {"table": "market_power_scores", "count": 312,
                    "interval": "6 hours (DCPI recompute, 4x daily)"}
    assert feed["last_updated"] == "2026-09-24T04:52:00"
    assert feed["health"] == "healthy"


def test_an_unmeasured_count_is_null_never_the_seed(monkeypatch):
    # A cold process: canonical_stats holds its static fallback (300), unmeasured.
    feed, _ = _run(monkeypatch, 300, live=False)
    assert feed["record_count"] is None
    assert feed["record_count_source"] == "unmeasured"
    assert feed["health"] == "unknown"


def test_the_feed_is_no_longer_derived_from_the_seed_list():
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "data_freshness")
    # Read CODE, not text: the fix's own comment quotes "len(SAMPLE_MARKETS)".
    seed_len = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "len" and n.args
                and isinstance(n.args[0], ast.Name) and n.args[0].id == "SAMPLE_MARKETS"]
    assert not seed_len, "data_freshness still measures the seed list"
    consts = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)}
    assert "static_constant" not in consts
    # exactly one markets feed, the one executed above
    body = ast.get_source_segment(SRC, fn)
    assert body.count("feeds['markets'] = {") == 1
