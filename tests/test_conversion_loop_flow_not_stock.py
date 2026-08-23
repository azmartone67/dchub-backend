"""Move #3 must be evidenced by a FLOW, never by a cumulative stock.

★ 2026-08-23. `tier2_score` read:

    paid_grew = m["paid_keys"] > b["paid_keys"]
    conv_grew = m["conversions_30d"] > b["conversions_30d"]
    if paid_grew or conv_grew:
        move3_status = "converted"

`paid_keys` is a CUMULATIVE STOCK compared against a frozen baseline, so it
can only ratchet upward. Once it passed the baseline (22) that OR pinned
move3 to "converted" permanently, whatever sales did. Move #2 is likewise
permanently above its own baseline (684 redeems vs 2), so the composite
score could not fall either.

Measured live on 2026-08-23:

    loop_score 90.0 · loop_healthy True · loop_confirmed_e2e False
    claim_to_paid_30d 0
    move3 "converted": paid_keys 44 (baseline 22),
                       conversions_30d 6 (baseline 8)   <- FELL

...and the note asserted "a wall→key-bound upgrade has converted" while the
metric the move exists to move had declined. A board that reports 90/100
healthy off a number that cannot fall is not measuring anything.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.conversion_loop_master_shell import _BASELINE, tier2_score  # noqa: E402


def _measures(**over) -> dict:
    """Baseline-equal measures; override just what a case is about."""
    m = {
        "claims_minted_30d": _BASELINE["claims_minted_30d"],
        "claims_redeemed_30d": _BASELINE["claims_redeemed_30d"],
        "claims_with_key_30d": _BASELINE["claims_redeemed_30d"],
        "claims_used_30d": _BASELINE["claims_used_30d"],
        "claim_to_paid_30d": _BASELINE["claim_to_paid_30d"],
        "paid_keys": _BASELINE["paid_keys"],
        "conversions_30d": _BASELINE["conversions_30d"],
        "redeem_route_status": 400,
    }
    m.update(over)
    return m


# ── the live case ────────────────────────────────────────────────────
LIVE = _measures(claims_redeemed_30d=684, claims_with_key_30d=684,
                 claims_used_30d=0, claim_to_paid_30d=0,
                 paid_keys=44, conversions_30d=6)


def test_the_live_2026_08_23_reading_no_longer_reports_converted():
    s = tier2_score(LIVE)
    assert s["move3"]["status"] == "stock_only"
    assert s["loop_healthy"] is False
    assert s["loop_confirmed_e2e"] is False
    # 50 for the firing gateway, nothing for move3, nothing for e2e.
    assert s["loop_score"] == 50


def test_the_note_never_claims_a_conversion_that_did_not_happen():
    note = tier2_score(LIVE)["move3"]["note"]
    assert "has converted" not in note
    assert "FELL 8 -> 6" in note
    assert "cannot fall" in note, "the stock must be labelled as cumulative"


def test_a_rising_stock_alone_cannot_score():
    """THE regression. Stock way up, flow flat at baseline -> no points."""
    s = tier2_score(_measures(paid_keys=9999))
    assert s["move3"]["status"] != "converted"
    assert s["loop_score"] == 0


def test_a_rising_flow_still_converts():
    """The fix must not break the case the move is actually for."""
    s = tier2_score(_measures(conversions_30d=_BASELINE["conversions_30d"] + 1))
    assert s["move3"]["status"] == "converted"
    assert "has converted" in s["move3"]["note"]


def test_flow_and_stock_are_reported_side_by_side_and_named():
    basis = tier2_score(LIVE)["move3"]["basis"]
    assert basis["flow_metric"] == "conversions_30d"
    assert basis["stock_metric"] == "paid_keys"
    assert basis["flow_value"] == 6 and basis["flow_baseline"] == 8
    assert basis["stock_value"] == 44 and basis["stock_baseline"] == 22


def test_stock_only_raises_a_worklist_item_rather_than_going_quiet():
    """Downgrading the status must not lose the signal — a stock rising while
    the flow falls is worth someone's attention (comp/seed grants?)."""
    wl = tier2_score(LIVE)["worklist"]
    assert any(w["move"] == 3 for w in wl)


def test_the_score_can_actually_reach_100():
    """Guards against a fix that simply made the number always small."""
    s = tier2_score(_measures(claims_redeemed_30d=684, claims_with_key_30d=684,
                              conversions_30d=_BASELINE["conversions_30d"] + 1,
                              paid_keys=44, claim_to_paid_30d=1))
    assert s["loop_score"] == 100
    assert s["loop_healthy"] is True
    assert s["loop_confirmed_e2e"] is True
