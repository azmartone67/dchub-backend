"""routes/quarterly_report.py:_headline_stats() must read the live DCPI market
count from canonical_stats, never publish the frozen _CANON['markets'] seed.

cross_surface_metric_divergence (queue #23): _CANON['markets'] = 300 was typed
once and never re-read. Every other field _headline_stats() carries
(facilities, substations, mna_usd, pipeline_gw) is overridden from a live
query when one succeeds; markets was the one field with no live path at
all, so the quarterly report's "N DCPI markets" stat and meta description
drifted from state_of_power.py, which already fixed this exact class of bug
via _canon_mkts() (single source: canonical_stats.get_canonical_stats()).

No DB, no network: canonical_stats is monkeypatched, and _conn() is left to
fail closed (DATABASE_URL unset in CI) exactly like production does when the
DB is unreachable.
"""
import canonical_stats
import routes.quarterly_report as qr


def test_headline_stats_reads_live_markets_from_canonical_stats(monkeypatch):
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda force=False: {"markets": 331})
    s = qr._headline_stats()
    assert s["markets"] == 331
    assert s["_origin"]["markets"] == "canonical_stats.get_canonical_stats"


def test_headline_stats_falls_back_to_canon_seed_when_canon_read_fails(monkeypatch):
    def _boom(force=False):
        raise RuntimeError("no DB")
    monkeypatch.setattr(canonical_stats, "get_canonical_stats", _boom)
    s = qr._headline_stats()
    assert s["markets"] == qr._CANON["markets"]
    assert "markets" not in s["_origin"]


def test_headline_stats_falls_back_when_canon_omits_markets(monkeypatch):
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda force=False: {})
    s = qr._headline_stats()
    assert s["markets"] == qr._CANON["markets"]
    assert "markets" not in s["_origin"]
