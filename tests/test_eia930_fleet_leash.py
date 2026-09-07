"""The EIA-930 fleet gets a leash because its SOURCE publishes ~27h late.

THE DEFECT (measured live 2026-09-07): 48 `iso_metric_count_zero_24h`
findings stood open, one per ISO, filed 2026-08-14 → 09-03, each with a
merged markdown investigation note and no fix. They were never an outage.

`grid_data.timestamp` is the EIA *period*, not our write time. Our newest PJM
row was `2026-09-06 03:00:00` — byte-identical to the newest period EIA itself
would serve (probed live against api.eia.gov v2 with the prod key; PJM/MISO/
DUK/SOCO/CISO/ERCO all 25-28h behind). So the ingest was exactly current with
a source that publishes a day late, and a detector asking "any write in the
last 24h" regenerated a finding for every EIA-only stream, every day, forever.

The split proves one cause rather than 48: every RESOLVED row was ENTSO-E /
EU_* / BR_* / ONS / ISONE — streams on their own feeds — and every OPEN row
was EIA-930.

★ WHAT THIS FILE IS REALLY GUARDING. The leash list is the one place in this
  repo where adding a name SILENCES a detector, so the dangerous direction is
  a stream getting leashed that should have stayed loud. ERCOT/CAISO/NYISO/
  SPP/AESO/ISONE have their own live feeds and read ~0.4h old while EIA sits
  25h stale; if one of them ever appeared here, a real outage would go quiet
  for a week. test_streams_with_their_own_live_feed_are_never_leashed is the
  assertion that matters most in this file.

★ Stdlib only, and NO repo scan — this file must not walk the tree, or
  tests/_scan_floors.py will (correctly) demand a pinned floor for a scan it
  does not own.
"""
import sys

import pytest

from routes import freshness_public as fp


def _fleet():
    return fp._INTERMITTENT_STREAMS["grid_data"]


# ── 1 · the negative that matters ────────────────────────────────────────────

# Each of these has its own live (non-EIA) feed and reads ~0.4h old in
# grid_data while EIA-930 is ~27h stale. Leashing one would hide a real death
# for _INTERMITTENT_MAX_H.
_OWN_LIVE_FEED = ("ERCOT", "CAISO", "NYISO", "SPP", "AESO",
                  "ISONE", "IESO", "HYDROQUEBEC", "ENTSOE",
                  # joined this list when routes/iso_miso.py was repointed
                  # onto public-api.misoenergy.org (5-min intervals)
                  "MISO")


@pytest.mark.parametrize("iso", _OWN_LIVE_FEED)
def test_streams_with_their_own_live_feed_are_never_leashed(iso):
    """★ A stream that publishes in real time must stay on the 24h clock."""
    assert iso not in _fleet(), (
        f"{iso} has its own live feed — leashing it would silence a real "
        f"outage for {fp._INTERMITTENT_MAX_H:.0f}h")


# ── 2 · the fleet is DERIVED, not restated ───────────────────────────────────

def test_every_registered_utility_ba_is_in_the_fleet():
    """The 43 BAs come from eia_utility_bas._BAS — the registry that owns them.

    If someone adds a BA there, it must inherit the leash automatically; a
    second hand-typed list is the drift this arrangement exists to avoid.
    """
    from routes.eia_utility_bas import _BAS
    codes = {b["code"] for b in _BAS if b.get("code")}
    assert codes, "the BA registry read empty — that is a bug, not an empty fleet"
    missing = sorted(codes - _fleet())
    assert not missing, f"registered BAs not covered by the leash: {missing}"


@pytest.mark.parametrize("iso", ("PJM", "TVA", "BPA"))
def test_the_eia_fed_isos_are_in_the_fleet(iso):
    """Not utility BAs, but EIA-930-fed by design (see each iso_* docstring).
    MISO was here until it got its own live feed — see _OWN_LIVE_FEED."""
    assert iso in _fleet()


def test_the_entsoe_zones_are_still_leashed():
    """The 2026-09-03 EU entries must survive the union."""
    for z in ("EU_BG", "EU_DK_1", "EU_DK_2", "EU_GR", "EU_IE_SEM"):
        assert z in _fleet(), z


# ── 3 · the 48 findings this shipped for ─────────────────────────────────────

# Verbatim from `SELECT url FROM brain_findings WHERE issue LIKE
# 'iso_metric_count_zero%' AND status='open'` on 2026-09-07.
_OPEN_ON_0907 = (
    "AEC AECI APS AVA BANC BPA CHPD CPLE CPLW DOPD DUK EPE EU_IE_SEM FPC FPL "
    "GCPD GVL IID IPCO JEA LDWP LGEE MISO NEVP NWMT PACE PACW PGE PJM PNM "
    "PSCO PSEI SC SCEG SCL SEC SOCO SPA SRP TAL TEC TEPC TIDC TPWR TVA WACM "
    "WALC WAUW").split()


def test_every_finding_open_on_0907_is_covered():
    """Every one EXCEPT MISO, which got a real fix instead of a leash: its
    feed was not retired, it moved to public-api.misoenergy.org."""
    assert len(_OPEN_ON_0907) == 48
    missing = sorted(set(_OPEN_ON_0907) - _fleet() - {"MISO"})
    assert not missing, f"still firing daily with no fix available: {missing}"


# ── 4 · a leash, never immunity ──────────────────────────────────────────────

def test_the_leash_is_finite():
    """Past this, an EIA stream is judged like any other — death is caught."""
    assert 0 < fp._INTERMITTENT_MAX_H <= 336, fp._INTERMITTENT_MAX_H


def test_an_unreadable_registry_silences_nothing(monkeypatch):
    """★ Fail-soft direction. If the BA registry cannot be read the fleet is
    EMPTY, so findings file exactly as they did before — a list that cannot be
    read must never quiet a detector."""
    monkeypatch.setitem(sys.modules, "routes.eia_utility_bas", None)
    assert fp._eia930_fleet() == frozenset()


def test_an_empty_registry_is_treated_as_a_bug_not_an_empty_fleet(monkeypatch):
    import types
    stub = types.ModuleType("routes.eia_utility_bas")
    stub._BAS = []
    monkeypatch.setitem(sys.modules, "routes.eia_utility_bas", stub)
    assert fp._eia930_fleet() == frozenset()
