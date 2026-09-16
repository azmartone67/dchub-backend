"""Capacity Source must survive the trip into every registry description.

★ 2026-09-16. Capacity Source has been live since 2.12.13 (source_capacity,
request_capacity_intro, accept_capacity_terms), and the auto-publisher that
generates the paste-ready copy for every registry listing did not mention it.
A reader of any DC Hub listing got facilities, grid, fiber and gas, and no way
to learn that DC Hub also sources data-center capacity to buy or lease.

Three properties are load-bearing here, and each one is a way the line could be
added and still be worthless:

1. It has to REACH every registry. The builder is a character-capped ladder;
   a line that only fits the 1,500-char cap reaches mcphive and nothing else.
2. It has to be COUNT-FREE. A registry re-crawls on its own schedule, so a
   number pasted into a listing goes stale somewhere no detector of ours can
   see. The convergence test below is the same one that caught the 1,800-vs-
   1,900 re-drift in test_white_glove_paste_copy_converges.py.
3. It must not be PAID FOR with the canonical counts. Fitting the blurb by
   dropping 21,900+ / 300+ / 2,200+ would trade three published floors for one
   capability.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import mcp_presence_crawler as mpc          # noqa: E402
from routes.white_glove_propagation import detect_number_drift  # noqa: E402

# The owner-given wording. Byte-identical to CAPACITY_BLURB in the mcp-server's
# lib/capacity-source-summary.mjs — two repos, so two literals, and this is the
# one that fails when they part. Retyped here ON PURPOSE rather than imported
# from the module under test: importing it would assert the module equals
# itself.
BLURB = ("Capacity Source: powered land/shell/turnkey incl. off-market "
         "listings via source_capacity; browse dchub.cloud/listings.")

LIVE_PUBLIC = {"facilities": "21,900+", "markets": "300+", "deals": "2,200+"}
GROWN_PUBLIC = {"facilities": "100,000+", "markets": "1,000+", "deals": "10,000+"}

# The distinctive tail of each rung, used to identify which one came back
# without rebuilding the ladder here (a test that recomputes the answer it is
# checking cannot catch the ladder being wrong).
FULL_ONLY = "paid tiers unlock"
FULL_AND_LEAN = "water risk, and renewable mix"
MEDIUM_ONLY = "ISO-grid, interconnection, fiber, energy, water, tax."


def _canon(monkeypatch, public, tools):
    monkeypatch.setattr(mpc, "_resolve_canon_public", lambda: dict(public))
    monkeypatch.setattr(mpc, "_our_actual_tool_count", lambda: tools)
    monkeypatch.setattr(mpc, "_canonical_numbers", lambda: {
        "tools": tools,
        "facilities": int(re.sub(r"[^\d]", "", public["facilities"])),
        "markets": int(re.sub(r"[^\d]", "", public["markets"])),
        "deals": int(re.sub(r"[^\d]", "", public["deals"])),
        "deals_phrase": f"{public['deals']} tracked deals",
    })


@pytest.fixture
def live(monkeypatch):
    _canon(monkeypatch, LIVE_PUBLIC, 91)


@pytest.fixture
def grown(monkeypatch):
    """Canon after the next few moves. Chosen so full+blurb no longer fits the
    500-char cap — that is the cliff the `lean` rung exists to catch."""
    _canon(monkeypatch, GROWN_PUBLIC, 120)


def test_the_blurb_reaches_every_registry(live):
    """Not just the one with the loosest cap."""
    for registry, cap in mpc._DESCRIPTION_CHAR_CAPS.items():
        desc = mpc._build_canonical_description(registry)
        assert BLURB in desc, f"{registry} (cap {cap}): no Capacity Source line"
        assert len(desc) <= cap, f"{registry}: {len(desc)} chars over cap {cap}"


def test_the_blurb_names_the_tool_and_the_page(live):
    """A capability nobody can reach is not surfaced. The tool is how an agent
    gets there, the URL is how a human does."""
    desc = mpc._build_canonical_description("smithery")
    assert "source_capacity" in desc
    assert "dchub.cloud/listings" in desc


def test_the_blurb_carries_no_number_that_could_go_stale(live):
    """A registry listing is re-crawled on the REGISTRY's schedule. Any digit
    in this line is a number we cannot heal once it is pasted."""
    for label, line in (("the contract literal here", BLURB),
                        ("mcp_presence_crawler.CAPACITY_SOURCE_BLURB",
                         mpc.CAPACITY_SOURCE_BLURB)):
        assert not re.search(r"\d", line), (
            f"{label} grew a digit: {line!r}. Registry copy is outside every "
            "drift detector we own — put counts in the tool response, not in "
            "the listing.")


def test_generated_copy_still_converges_against_the_drift_detector(live):
    """The property test_white_glove_paste_copy_converges.py exists for: paste
    this copy and tomorrow's run must find nothing to correct."""
    canon = {
        "tools": 91, "tools_live": 91,
        "facilities_floor": 21900, "markets_floor": 300, "deals_floor": 2200,
        "stale_markers": [], "stale_markers_regex": [],
    }
    for registry in mpc._DESCRIPTION_CHAR_CAPS:
        desc = mpc._build_canonical_description(registry)
        assert detect_number_drift(desc, canon) == [], (
            f"{registry}: the Capacity Source line made our own remedy copy "
            f"drift against our own detector")


def test_the_lean_rung_absorbs_the_next_canon_move(grown):
    """★ THE CLIFF. full+blurb is 496 against a 500 cap: four characters. One
    canon move spends them, and without an intermediate rung the whole pitch
    would drop to `medium` — a ~245-character regression on smithery, pulsemcp,
    glama and every unknown registry, arriving silently on the day a count grew
    a digit."""
    desc = mpc._build_canonical_description("smithery")
    assert BLURB in desc
    # It really did fall off `full` — otherwise this fixture is not exercising
    # the cliff at all and the assertion below proves nothing.
    assert FULL_ONLY not in desc, (
        "full+blurb still fits at the grown canon, so this test no longer "
        "reaches the rung it is here to check — grow GROWN_PUBLIC further")
    # ...and landed on `lean`, not all the way down to `medium`.
    assert FULL_AND_LEAN in desc, (
        f"dropped past `lean` to a shorter rung: {desc!r}")


def test_a_cap_too_small_for_the_blurb_still_keeps_the_canonical_counts(live):
    """The blurb is never paid for with the floors. At a cap that cannot hold
    it, the builder must fall back to exactly the copy it produced before this
    line existed."""
    mpc._DESCRIPTION_CHAR_CAPS["_test_tight"] = 200
    try:
        desc = mpc._build_canonical_description("_test_tight")
    finally:
        mpc._DESCRIPTION_CHAR_CAPS.pop("_test_tight", None)
    assert BLURB not in desc
    assert len(desc) <= 200
    for phrase in (LIVE_PUBLIC["facilities"], LIVE_PUBLIC["markets"],
                   LIVE_PUBLIC["deals"]):
        assert phrase in desc, (
            f"{phrase} was dropped to make room for a line that did not even "
            f"fit: {desc!r}")


def test_must_fail_control_a_blurbless_builder_fails_the_reach_test(live, monkeypatch):
    """Proves test_the_blurb_reaches_every_registry can fail. Without a control
    it would also pass against a builder that appends the line to no registry
    at all, if BLURB were ever weakened to something every string contains."""
    monkeypatch.setattr(mpc, "CAPACITY_SOURCE_BLURB", "")
    desc = mpc._build_canonical_description("smithery")
    assert BLURB not in desc, (
        "the builder emits the Capacity Source line even with the blurb "
        "emptied — the reach test above cannot fail and proves nothing")


# ── the priced rung (★2026-09-16) ────────────────────────────────────

def test_the_priced_rung_reaches_the_registries_with_room(live):
    """Registries whose cap can hold it carry the tier a reader converts on;
    the 500-cap ones must be UNCHANGED, because the price is worth less than
    the counts and the free-tier line it would push off."""
    import tier_registry
    price = tier_registry.price_display("pro")
    roomy = [r for r, cap in mpc._DESCRIPTION_CHAR_CAPS.items() if cap >= 600]
    assert roomy, "HARNESS ERROR: no registry has room — this test proves nothing"
    for registry in roomy:
        desc = mpc._build_canonical_description(registry)
        assert price in desc, f"{registry} (cap ≥600) lost the priced rung: {desc!r}"
        assert BLURB in desc, f"{registry}: priced rung crowded out Capacity Source"
        assert len(desc) <= mpc._DESCRIPTION_CHAR_CAPS[registry]
    tight = mpc._build_canonical_description("smithery")
    assert price not in tight, (
        "the priced rung fits a 500 cap now, so it is being paid for with copy "
        "that was there first — re-check what it displaced")
    assert BLURB in tight and LIVE_PUBLIC["facilities"] in tight


def test_the_price_is_derived_not_typed(live, monkeypatch):
    """★ MUTATION. Pro was $299 until the 2026-09-05 collapse. If this copy
    typed the price, the listing would still say $299 and nothing would tell
    us. Move the registry's price and the copy must move with it."""
    import tier_registry
    real = tier_registry.price_display
    monkeypatch.setattr(tier_registry, "price_display",
                        lambda tier, *a, **k: "$1234/mo" if tier == "pro" else real(tier, *a, **k))
    desc = mpc._build_canonical_description("glama")
    assert "$1234/mo" in desc, (
        "the price in registry copy did not follow tier_registry — it is a "
        "hand-typed literal, which is the defect this rung was built to avoid")
    assert real("pro") not in desc
