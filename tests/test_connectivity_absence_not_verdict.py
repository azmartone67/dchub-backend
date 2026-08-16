"""tests/test_connectivity_absence_not_verdict.py — absence-is-not-a-finding guard (2026-08-16).

Guards `score_connectivity()` in routes/connectivity_score.py, which powers the MCP
`get_fiber_readiness` and `score_facility` tools, the site-planner fiber panel and
the map.

The bug this locks shut is the LANE 1 substation_band class, one dataset over:
`carrier_facility_presence` is PeeringDB — global, but THIN outside dense US/EU
metros. Finding no carrier near a point has two causes, and the module collapsed
them into one, then published the confident one as fact:

  (1) BUCKET       — _bucket(None) returned "build-required".
  (2) SCORE        — the distance component fell back to `(nearest_km or 99.0)`,
                     so max(0, 40-99) = 0 — the worst possible score, produced
                     entirely by a missing row. site_planner then recorded that
                     fabricated 0 as coverage: 'validated'.
  (3) VERDICT      — "No carrier-served facility within 50 km — greenfield fiber
                     build required; budget long lateral construction."

The fix gates all three on positive evidence that the dataset describes the region
(any carrier row within _COVERAGE_DEG), mirroring the None posture fiber_coverage_km
already had. Absence of evidence now returns bucket "unknown" / score None, and the
reason is machine-readable as carrier_data_coverage="none_in_region".

★ The distinction under test is COVERAGE-CONFIRMED vs COVERAGE-UNKNOWN, not
"zero carriers". Zero carriers WITH confirmed coverage is a real finding and must
keep its verdict — test_c_confirmed_* below assert the claim still ships, so this
guard cannot be satisfied by blanket-nulling everything.

Run:  python3 -m pytest tests/test_connectivity_absence_not_verdict.py -v
"""
from __future__ import annotations

import pytest

from routes import connectivity_score as cs


# ── fake DB ────────────────────────────────────────────────────────────────
# The real cursor is used as: `with c.cursor() as cur` -> execute / fetchall /
# fetchone. Dispatch on the SQL text so the test does not depend on statement
# ORDER inside score_connectivity().

class _FakeCursor:
    def __init__(self, carrier_rows, coverage_hit, hex_rows):
        self._carrier_rows = carrier_rows
        self._coverage_hit = coverage_hit
        self._hex_rows = hex_rows
        self._last = None
        self.coverage_probe_ran = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM carrier_facility_presence" in s and "LIMIT 1" in s:
            self._last = "coverage"
            self.coverage_probe_ran = True
        elif "FROM carrier_facility_presence" in s:
            self._last = "carriers"
        elif "FROM fcc_fiber_hex" in s:
            self._last = "hex"
        else:
            self._last = "other"

    def fetchall(self):
        if self._last == "carriers":
            return list(self._carrier_rows)
        if self._last == "hex":
            return list(self._hex_rows)
        return []

    def fetchone(self):
        if self._last == "coverage":
            return (1,) if self._coverage_hit else None
        return None


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture
def run_score(monkeypatch):
    """Call score_connectivity against scripted DB results; hand back (out, cursor)."""
    def _run(carrier_rows=(), coverage_hit=False, hex_rows=(), lat=50.11, lon=8.68):
        cur = _FakeCursor(carrier_rows, coverage_hit, hex_rows)
        monkeypatch.setattr(cs, "_conn", lambda: _FakeConn(cur))
        out = cs.score_connectivity(lat, lon, 50.0)
        assert not out.get("error"), out
        return out, cur
    return _run


# ── (1) the bucket ─────────────────────────────────────────────────────────

def test_bucket_none_with_coverage_is_build_required():
    """Zero carriers where the dataset DOES reach is a real finding — keep it."""
    assert cs._bucket(None, coverage_known=True) == "build-required"


def test_bucket_none_without_coverage_is_unknown():
    """THE PIN: no coverage evidence must not become a siting verdict."""
    assert cs._bucket(None, coverage_known=False) == "unknown"


def test_bucket_measured_distances_unchanged():
    """The four measured bands must not move — this fix touches only the None case."""
    assert cs._bucket(0.05) == "on-net"
    assert cs._bucket(1.0) == "near-net"
    assert cs._bucket(2.5) == "acceptable"
    assert cs._bucket(40.0) == "build-required"


# ── (2) no data anywhere in region -> publish nothing ──────────────────────

def test_no_coverage_returns_unknown_bucket(run_score):
    out, cur = run_score(carrier_rows=(), coverage_hit=False)
    assert cur.coverage_probe_ran, "the wider coverage probe must actually run"
    assert out["near_net_bucket"] == "unknown"
    assert out["carrier_data_coverage"] == "none_in_region"


def test_no_coverage_scores_none_not_zero(run_score):
    """A 0/100 built from a missing row is the fabricated number this kills."""
    out, _ = run_score(carrier_rows=(), coverage_hit=False)
    assert out["score"] is None, "unmeasurable must be None, never 0"
    assert out["factors"] is None
    assert out["single_carrier_risk"] is None


def test_no_coverage_verdict_makes_no_greenfield_claim(run_score):
    out, _ = run_score(carrier_rows=(), coverage_hit=False)
    v = out["verdict_short"].lower()
    assert "greenfield fiber build required" not in v
    assert "budget long lateral construction" not in v
    assert "cannot" in v and "absent data" in v


# ── (3) coverage confirmed -> the claim must still ship ────────────────────

def test_c_confirmed_by_wider_probe_still_asserts(run_score):
    """Nothing within 50 km, but PeeringDB describes the region: claim is earned."""
    out, cur = run_score(carrier_rows=(), coverage_hit=True)
    assert cur.coverage_probe_ran
    assert out["near_net_bucket"] == "build-required"
    assert out["carrier_data_coverage"] == "confirmed"
    assert "greenfield fiber build required" in out["verdict_short"]
    # pin the VALUE, not just non-None: nearest carrier is beyond the radius, so
    # all three components are legitimately 0. Asserting only `is not None` here
    # let a mutation of the distance component ride through undetected.
    assert out["score"] == 0
    assert out["factors"] == {"distance": 0.0, "carrier_depth": 0.0, "diversity": 0.0}


def test_c_confirmed_by_bbox_row_skips_extra_probe(run_score):
    """A bbox row outside the radius already proves coverage — no second query."""
    far = (50.11 + 0.55, 8.68)          # ~61 km N: inside the bbox, outside 50 km
    out, cur = run_score(carrier_rows=[("Lumen", far[0], far[1])], coverage_hit=False)
    assert not cur.coverage_probe_ran, "bbox evidence should make the probe unnecessary"
    assert out["carrier_count"] == 0
    assert out["near_net_bucket"] == "build-required"
    assert out["carrier_data_coverage"] == "confirmed"


def test_carriers_in_range_score_unchanged(run_score):
    """The ordinary path must be untouched: real carriers -> real score."""
    rows = [("Lumen", 50.111, 8.681), ("Zayo", 50.12, 8.69), ("Cogent", 50.13, 8.70)]
    out, cur = run_score(carrier_rows=rows, coverage_hit=False)
    assert not cur.coverage_probe_ran
    assert out["carrier_count"] == 3
    assert isinstance(out["score"], int) and out["score"] > 0
    assert out["carrier_data_coverage"] == "confirmed"
    assert out["near_net_bucket"] in ("on-net", "near-net", "acceptable")
    assert out["factors"] is not None


def test_zero_km_carrier_is_not_read_as_99(run_score):
    """`nearest_km or 99.0` mis-read a genuine 0.0 km as the far sentinel."""
    out, _ = run_score(carrier_rows=[("Lumen", 50.11, 8.68)], coverage_hit=False)
    assert out["nearest_carrier_km"] == 0.0
    assert out["near_net_bucket"] == "on-net"
    assert out["factors"]["distance"] == 100.0
