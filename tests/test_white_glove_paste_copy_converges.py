"""The paste-ready copy white-glove hands an operator must not re-drift.

★ 2026-08-23. The human-gated half of white-glove files issue #1872 with
"paste-ready corrected description" per listing. It printed, five lines
apart:

    Canonical numbers (ai_surface_canon): … 1,900+ tracked deals
    Paste-ready corrected description:    … 1,800+ tracked deals

The DETECTOR (white_glove_propagation.load_canon) reads resolve_canon()
— live. The BUILDER (mcp_presence_crawler._build_canonical_description)
read _canonical_numbers() -> mcp_honest_numbers -> ai_surface_canon.PINNED
— the static floors. So an operator who pasted the remedy re-drifted the
listing on the next morning's run, and the lane could not converge by
construction: three registries sat at drifted 5-6 / human_gated 3 for weeks.

This is the SAME class load_canon() was fixed for on 2026-08-18, whose
docstring already warned that "every downstream consumer (registry
submitters, description builders, white-glove propagation) kept pasting
it". The description builder was the half left behind.

The load-bearing property is CONVERGENCE, so that is what is asserted:
run the generated copy through the real detector under a live canon and
require zero findings. detect_number_drift() carries a documented
ZERO-FALSE-POSITIVE CONTRACT — "any page whose DC Hub copy matches the
canon produces []" — which makes [] a meaningful assertion rather than a
tautology.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import mcp_presence_crawler as mpc          # noqa: E402
from routes.white_glove_propagation import detect_number_drift  # noqa: E402

# A canon whose LIVE values have moved past the pinned floors — exactly the
# situation on 2026-08-23 (pinned deals "1,800+", live "1,900+").
LIVE_PUBLIC = {
    "facilities": "18,500+",
    "markets": "300+",
    "deals": "1,900+",
    "countries": "170+",
}
LIVE_CANON = {
    "tools": 82, "tools_live": 82,
    "deals_floor": 1900, "facilities_floor": 18500, "markets_floor": 300,
    "stale_markers": [], "stale_markers_regex": [],
}


# The PINNED floors, deliberately BEHIND live on ALL THREE numbers. A
# fixture where pinned and live coincide cannot tell the two origins apart:
# the first mutation run reverted facilities and markets to the pinned path
# and both assertions still passed, because today's pinned floor happens to
# equal today's live value for those two. Only `deals` actually differed.
PINNED_NUMBERS = {
    "tools": 82,
    "facilities": 17000,
    "markets": 250,
    "deals": 1800,
    "deals_phrase": "1,800+ tracked deals",
}


@pytest.fixture
def live_canon(monkeypatch):
    """Pin resolve_canon() to LIVE_PUBLIC and _canonical_numbers() to the
    stale floors, so the test never touches the network, never depends on
    today's real numbers (which would make it flap), and can actually
    distinguish which of the two origins supplied each number."""
    import ai_surface_canon
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: {"public": dict(LIVE_PUBLIC)}, raising=False)
    monkeypatch.setattr(mpc, "_canonical_numbers", lambda: dict(PINNED_NUMBERS))
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: 82)
    return LIVE_CANON


def test_the_builder_reads_the_live_canon_not_the_pinned_floor(live_canon):
    """The single fact the whole lane rests on."""
    assert mpc._resolve_canon_public() == LIVE_PUBLIC
    desc = mpc._build_canonical_description("glama")
    # every headline number must come from the LIVE side...
    assert "1,900+ tracked deals" in desc
    assert "18,500+ discovered facilities" in desc
    assert "300+ DCPI markets" in desc
    # ...and none of the stale floors may survive into operator copy.
    for stale in ("1,800+", "17,000", "250 DCPI"):
        assert stale not in desc, f"pinned floor {stale!r} leaked into copy"


def test_generated_copy_produces_zero_drift_against_the_live_canon(live_canon):
    """CONVERGENCE. Paste this copy and tomorrow's run must find nothing."""
    for registry in ("glama", "mcp_so", "awesome_mcp_servers", "smithery",
                     "lobehub", "cursor_directory", "_default"):
        desc = mpc._build_canonical_description(registry)
        assert desc, f"{registry}: empty description"
        drifts = detect_number_drift(desc, LIVE_CANON)
        assert drifts == [], (
            f"{registry}: our own remedy copy drifts against our own "
            f"detector: {drifts}")


def test_must_fail_control_the_old_pinned_copy_does_drift(live_canon):
    """Proves the assertion above can fail. This is the copy the builder
    produced BEFORE the fix — the detector must reject it, or
    test_generated_copy... is green for the wrong reason."""
    stale = ("DC Hub is the data layer for data-center infrastructure: 82 "
             "live MCP tools covering 18,500 discovered facilities, 300 "
             "DCPI markets, 1,800+ tracked deals, ISO-grid headroom.")
    drifts = detect_number_drift(stale, LIVE_CANON)
    assert any(d["kind"] == "deals" for d in drifts), (
        "the detector no longer flags the 1,800-vs-1,900 drift — this test "
        "can no longer fail, so the convergence test proves nothing")


def test_floors_are_rendered_as_floors_not_exact_counts(live_canon):
    """"18,500 discovered facilities" states a count we do not have; the
    canon phrase is a FLOOR and the trailing + must survive into copy."""
    desc = mpc._build_canonical_description("glama")
    assert "18,500+ discovered facilities" in desc
    assert "18,500 discovered facilities" not in desc


def test_fallback_to_pinned_floors_when_resolve_canon_is_down(monkeypatch):
    """Fail-soft: a resolver outage must not blank or crash the copy — the
    pinned floors stand and still render as floors."""
    import ai_surface_canon

    def _boom():
        raise RuntimeError("stats unreachable")

    monkeypatch.setattr(ai_surface_canon, "resolve_canon", _boom, raising=False)
    monkeypatch.setattr(mpc, "_canonical_numbers", lambda: dict(PINNED_NUMBERS))
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: 82)
    assert mpc._resolve_canon_public() == {}
    desc = mpc._build_canonical_description("glama")
    assert desc and "82" in desc
    # The fallback must still render FLOORS, not bare exact counts — this is
    # the only path that exercises _floor_phrase, so without these the
    # trailing + can be deleted and every other test stays green.
    assert "17,000+ discovered facilities" in desc
    assert "250+ DCPI markets" in desc
    assert "17,000 discovered facilities" not in desc
    assert "1,800+ tracked deals" in desc


def test_every_capped_variant_stays_within_its_cap(live_canon):
    """Adding the + characters must not push any registry over its cap."""
    for registry, cap in mpc._DESCRIPTION_CHAR_CAPS.items():
        desc = mpc._build_canonical_description(registry)
        assert len(desc) <= cap, f"{registry}: {len(desc)} > {cap}"


# ── the degraded-resolver class ───────────────────────────────────────
# ★ Found by the PRE-EXISTING test_canonical_description_builder_is_drift_free
# when the first version of this fix trusted resolve_canon() wholesale.
#
# resolve_canon() is fail-soft PER RESOLVER and only against EXCEPTIONS. A
# resolver that SUCCEEDS against a degraded or empty database returns a
# valid-looking phrase computed from near-zero rows and overwrites the pinned
# floor it deep-copied. Measured with no DB reachable:
#
#     facilities "400+"   (pinned 18,500+)  — ~46x under-claim
#     deals      "1,400+" (pinned  1,800+)  — and a known stale_marker
#
# Nothing raises. Publishing that into registry copy is worse than never
# resolving at all, so a live value below the pinned floor is rejected:
# canonical_stats floors round DOWN, so a floor only ever moves UP, and a
# value beneath it is a broken resolver rather than a shrinking fleet.

DEGRADED = {"facilities": "400+", "markets": "300+", "deals": "1,400+",
            "countries": "170+"}


@pytest.fixture
def degraded_canon(monkeypatch):
    import ai_surface_canon
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: {"public": dict(DEGRADED)}, raising=False)
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: 82)


def test_a_live_value_below_the_pinned_floor_is_rejected(degraded_canon):
    from ai_surface_canon import PINNED
    pub = mpc._resolve_canon_public()
    pinned = PINNED.get("public") or {}
    # the two degraded numbers are dropped...
    assert "facilities" not in pub, "a 400+ facility count reached the copy"
    assert "deals" not in pub, "a 1,400+ deal count reached the copy"
    # ...and the ones that are AT or ABOVE their floor survive.
    assert pub.get("markets") == "300+"
    assert mpc._canon_floor(pinned.get("markets")) <= 300


def test_degraded_resolvers_cannot_publish_an_under_claim(degraded_canon):
    # ★2026-08-23 — these two floors are read OUT of PINNED, not retyped.
    # They used to be the literals "18,500+" and "1,800+", so the moment the
    # deals pin moved to 1,900+ (the bump claim 100974 refuted the old one
    # into) this test failed while the CODE was behaving exactly right:
    # it kept the floor over a degraded 1,400+. A test that hardcodes the
    # number it is guarding is a fourth typed home for that number.
    from ai_surface_canon import PINNED
    pinned = PINNED.get("public") or {}
    desc = mpc._build_canonical_description("mcphive")
    assert "400+ discovered facilities" not in desc
    assert "1,400+" not in desc, "emitted a phrase the detector calls stale"
    assert f"{pinned['facilities']} discovered facilities" in desc
    assert f"{pinned['deals']} tracked deals" in desc


def test_the_degraded_copy_still_converges_against_the_pinned_canon(degraded_canon):
    """The property that actually matters — the operator's paste must be
    clean even when every resolver is answering from an empty database."""
    from routes.white_glove_propagation import load_canon
    canon = load_canon()
    canon["tools_live"] = canon.get("tools_live") or canon.get("tools")
    for registry in ("mcphive", "glama", "smithery", "_default"):
        drifts = detect_number_drift(
            mpc._build_canonical_description(registry), canon)
        assert drifts == [], f"{registry}: degraded copy drifts: {drifts}"


def test_the_last_resort_deals_fallback_is_not_a_known_stale_claim(monkeypatch):
    """The deepest fallback branch — live has no deals AND the bridge has no
    deals_phrase. It used to emit the literal "1,400+ tracked deals", which
    the drift detector itself flags as a stale_marker: a fallback that
    publishes a claim we already know is stale.

    Reached by neither of the fixtures above, so a mutation restoring the
    literal SURVIVED the first control run. This drives that branch.
    """
    import ai_surface_canon
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: {"public": {}}, raising=False)
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: 82)
    monkeypatch.setattr(mpc, "_canonical_numbers",
                        lambda: {"tools": 82, "facilities": 18500,
                                 "markets": 300, "deals": 1800})  # no phrase
    desc = mpc._build_canonical_description("glama")
    assert "1,400+" not in desc, "last-resort fallback emits a stale_marker"
    assert "1,800+ tracked deals" in desc
    drifts = detect_number_drift(desc, LIVE_CANON | {"deals_floor": 1800})
    assert drifts == [], f"last-resort copy drifts: {drifts}"


def test_a_non_numeric_live_phrase_is_skipped_not_crashed(monkeypatch):
    """`_canon_floor` returns None for a phrase with no digits. Comparing
    that to an int raises TypeError, and this loop is OUTSIDE the try that
    wraps resolve_canon() — so one junk phrase would take down the whole
    builder rather than degrade to the pinned floor.

    (This guard was silently lost once: a mutation harness killed by its
    own timeout left the stripped file on disk, and the next `git add -A`
    committed it. Pin the behaviour, not the code shape.)
    """
    import ai_surface_canon
    monkeypatch.setattr(
        ai_surface_canon, "resolve_canon",
        lambda: {"public": {"facilities": "coming soon", "deals": None,
                            "markets": "300+"}}, raising=False)
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: 82)
    pub = mpc._resolve_canon_public()          # must not raise
    assert "facilities" not in pub and "deals" not in pub
    assert pub.get("markets") == "300+"
    desc = mpc._build_canonical_description("glama")
    assert "coming soon" not in desc
    assert "18,500+ discovered facilities" in desc
