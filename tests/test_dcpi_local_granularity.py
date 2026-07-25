"""r-local-granularity (2026-07-25) — pins the market-LOCAL scoring terms.

THE BUG: every DCPI scorer input was ISO/state-level, so same-state markets
(allen/fort-worth/irving/abilene, all 53.4/57.8) and intl-default markets
(barueri/bologna/midrand — three continents at 41.1/44.8) carried
byte-identical (constraint, excess) pairs: 101/317 markets on 07-24 per the
integrity shell's dc_pairs check. The fix adds bounded additive terms from
the market's OWN footprint (substations 40km, gem_power operating 60km,
canonical facilities 25km).

INVARIANTS PINNED HERE:
  1. NEUTRAL when local keys are absent — legacy callers score byte-identically.
  2. BOUNDED — excess moves <= +8.0, constraint <= +6.0, and never above 100.
  3. ADDITIVE-ONLY — local data can only raise a score, never lower it
     (absence of data must never penalize; HIFLD is US-only).
  4. DIFFERENTIATES — two markets identical except local footprint get
     different scores (the whole point).
  5. FAIL-SOFT — a dead DB yields the zero-dict, not an exception.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402


_BASE = {
    "queue_wait_months": 30, "reserve_margin_pct": 15,
    "emergency_count_30d": 1, "demand_growth_yoy_pct": 4,
    "gen_additions_12mo_mw": 1200, "curtailment_pct": 4,
    "queue_approval_rate_pct": 50, "stranded_capacity_mw": 300,
    "btm_headroom_mw": 400,
}

_MAX_LOCAL = {"local_substation_count": 10000, "local_max_kv": 765,
              "local_gen_mw": 999999, "local_dc_count": 10000}


def test_neutral_without_local_keys():
    with_zero = dict(_BASE, local_substation_count=0, local_max_kv=0,
                     local_gen_mw=0, local_dc_count=0)
    assert dcpi.compute_excess_power_score(_BASE) == \
        dcpi.compute_excess_power_score(with_zero)
    assert dcpi.compute_constraint_score(_BASE) == \
        dcpi.compute_constraint_score(with_zero)


def test_bounded_adjustment():
    e0 = dcpi.compute_excess_power_score(_BASE)
    c0 = dcpi.compute_constraint_score(_BASE)
    e1 = dcpi.compute_excess_power_score(dict(_BASE, **_MAX_LOCAL))
    c1 = dcpi.compute_constraint_score(dict(_BASE, **_MAX_LOCAL))
    assert 0 < e1 - e0 <= 8.0 + 1e-6, f"excess bump {e1 - e0} out of bound"
    assert 0 < c1 - c0 <= 6.0 + 1e-6, f"constraint bump {c1 - c0} out of bound"


def test_additive_only_never_lowers():
    for loc in ({"local_substation_count": 3},
                {"local_gen_mw": 50},
                {"local_max_kv": 500},
                {"local_dc_count": 2}):
        assert dcpi.compute_excess_power_score(dict(_BASE, **loc)) >= \
            dcpi.compute_excess_power_score(_BASE)
        assert dcpi.compute_constraint_score(dict(_BASE, **loc)) >= \
            dcpi.compute_constraint_score(_BASE)


def test_clipped_at_100():
    hot = dict(_BASE, reserve_margin_pct=40, gen_additions_12mo_mw=99999,
               curtailment_pct=50, queue_approval_rate_pct=100,
               stranded_capacity_mw=99999, btm_headroom_mw=99999, **_MAX_LOCAL)
    assert dcpi.compute_excess_power_score(hot) <= 100.0


def test_differentiates_cloned_markets():
    """The fort-worth/abilene case: same state inputs, different footprint."""
    fort_worth = dict(_BASE, local_substation_count=315, local_max_kv=345,
                      local_gen_mw=4000, local_dc_count=30)
    abilene = dict(_BASE, local_substation_count=72, local_max_kv=345,
                   local_gen_mw=900, local_dc_count=2)
    assert dcpi.compute_excess_power_score(fort_worth) != \
        dcpi.compute_excess_power_score(abilene)
    assert dcpi.compute_constraint_score(fort_worth) != \
        dcpi.compute_constraint_score(abilene)


def test_differentiates_intl_defaults():
    """barueri (1,984 MW local gen) vs midrand (488 MW) — gem_power is
    GLOBAL, so intl markets split even with zero US-only substation rows."""
    barueri = dict(_BASE, local_gen_mw=1984, local_dc_count=12)
    midrand = dict(_BASE, local_gen_mw=488, local_dc_count=5)
    assert dcpi.compute_excess_power_score(barueri) != \
        dcpi.compute_excess_power_score(midrand)


def test_helper_fail_soft(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(dcpi, "_conn", _boom)
    dcpi._LOCAL_INFRA_CACHE.clear()
    out = dcpi._local_infra_metrics(32.75, -97.33)
    assert out == {"local_substation_count": 0, "local_max_kv": 0.0,
                   "local_gen_mw": 0.0, "local_dc_count": 0}


def test_helper_rejects_unknown_coords(monkeypatch):
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise RuntimeError("must not connect")
    monkeypatch.setattr(dcpi, "_conn", _boom)
    assert dcpi._local_infra_metrics(None, None)["local_substation_count"] == 0
    assert dcpi._local_infra_metrics(0, 0)["local_gen_mw"] == 0.0
    assert called["n"] == 0, "helper hit the DB for unknown coords"


def test_provenance_documented_in_source():
    src = open(dcpi.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert "local_grid_access" in src
    assert "no_local_infrastructure_rows" in src
    assert "absence never penalizes" in src
