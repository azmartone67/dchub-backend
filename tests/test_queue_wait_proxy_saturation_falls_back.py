"""The queue-depth proxy must not publish a PINNED value as a live wait.

r-queue-saturation-honesty (2026-09-17). Measured live before the fix, by
fetching /api/v1/dcpi/scores/<slug> and comparing the published
queue_wait_months against the calibrated iso_defaults anchor in routes/dcpi.py:

    market   ISO    queue GW   anchor   published   ratio
    dallas   ERCOT     474.7     30mo      83.4mo   2.78x
    houston  ERCOT     474.7     30mo      79.9mo   2.66x
    ashburn  PJM        31.8     48mo      40.5mo   0.84x

clip(12 + GW*0.6, 12, 66) pins at 66 for both Texas markets (474.7 GW is 5.3x
the 90 GW saturation point), so the ISO term stopped varying and the ordering
INVERTED against the anchors: ERCOT, the fastest US large-load interconnect,
published a longer wait than PJM, the slowest. Dallas carried
time_to_power_months 66.7 against Ashburn's 56.7.

These tests pin the two halves of the fix: below saturation the proxy still
writes and still counts as live; at or above it the field is left for the ISO
anchor and drops out of the live-provenance set.
"""
import routes.dcpi as D
from util.dcpi_method import QUEUE_WAIT_PROXY

SAT_GW = float(QUEUE_WAIT_PROXY["saturates_at_gw"])
TX = ("dallas", "Dallas", "TX", "ERCOT", 32.78, -96.80)
VA = ("ashburn", "Ashburn", "VA", "PJM", 39.02, -77.47)


def _gather(monkeypatch, market, active_mw):
    """Drive the real gather with only the two state adapters stubbed."""
    monkeypatch.setattr(D, "_state_queue_depth",
                        lambda state: {"active_mw": active_mw})
    monkeypatch.setattr(D, "_state_gen_additions", lambda state: None)
    return D.gather_metrics_for_market(market)


def test_a_saturated_queue_does_not_publish_a_pinned_wait(monkeypatch):
    """Texas: 474.7 GW. The proxy would pin at the 66mo clip ceiling."""
    m = _gather(monkeypatch, TX, 474_658.2)
    assert m.get("_queue_wait_proxy_saturated") is True
    qw = m["queue_wait_months"]
    assert qw is not None, "the ISO anchor must still fill the field"
    # 66.0 is the pinned proxy value. Anything at or above it means the pinned
    # constant survived — which is the defect, not the fix.
    assert qw < 66.0, f"pinned proxy value still published: {qw}"


def test_the_saturated_market_falls_back_to_its_iso_anchor(monkeypatch):
    """ERCOT's anchor is 30mo and it is the number that must reach the scorer.

    A saturation multiplier may scale it (that is the de-cloning term and is
    deliberate), so assert the anchor is the BASE, not that it survives
    untouched — asserting equality here would fail for a reason unrelated to
    this fix the moment local saturation is present.
    """
    m = _gather(monkeypatch, TX, 474_658.2)
    qw = m["queue_wait_months"]
    # 0.90..1.35 is the published SATURATION_REWRITES band for queue_wait.
    assert 30.0 * 0.90 - 0.05 <= qw <= 30.0 * 1.35 + 0.05, (
        f"{qw} is not ERCOT's 30mo anchor scaled by the saturation band")


def test_an_unsaturated_queue_still_uses_the_live_proxy(monkeypatch):
    """Virginia: 31.8 GW, well below saturation — the proxy still earns its
    live label. Without this, 'fix the pin' could degrade to 'delete the
    adapter' and every market would silently score off a constant."""
    m = _gather(monkeypatch, VA, 31_760.5)
    assert not m.get("_queue_wait_proxy_saturated")
    # 12 + 31.7605*0.6 = 31.06
    assert abs(m["queue_wait_months"] - 31.1) < 0.6, m["queue_wait_months"]


def test_the_boundary_is_inclusive_at_the_published_saturation_point(monkeypatch):
    """Exactly saturates_at_gw counts as saturated — the clip ceiling is
    reached at 90 GW (12 + 90*0.6 = 66.0), so the value carries no information
    from that point ON, not merely past it."""
    at = _gather(monkeypatch, TX, SAT_GW * 1000.0)
    assert at.get("_queue_wait_proxy_saturated") is True
    just_below = _gather(monkeypatch, TX, (SAT_GW - 1.0) * 1000.0)
    assert not just_below.get("_queue_wait_proxy_saturated")


def test_queue_capacity_is_still_measured_and_still_live(monkeypatch):
    """The DEPTH is a real measurement; only the WAIT was a bad derivation.
    Dropping both would lose data the fix never needed to touch."""
    m = _gather(monkeypatch, TX, 474_658.2)
    assert m["queue_capacity_mw"] == 474_658.2
    assert m.get("_queue_depth_active_gw") == 474.7
    basis = str(m.get("data_basis") or m)
    assert "queue_capacity_mw" in basis
