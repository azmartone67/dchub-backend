"""The publish gate judges what SHIPS, not the draft — 2026-07-28.

The posters have always truncated: X hard-cuts at 280, Bluesky cuts at 297 and
appends an ellipsis. The gate scored the untruncated draft, so it was rating an
artifact nobody would ever see. Measured on the pillars X card:

    draft  359 chars -> 0.600 PASS      (what the gate scored)
    wire   280 chars -> 0.150 REFUSED   (what actually published, link cut off)

The tweet that shipped ended mid-phrase with its entire sign-off — CC-BY,
"over MCP" and the link — discarded. No measurement that scores a draft can see
this, which is why it survived four rounds of fixes to this same scorer.

as_published() is now the single source of truth: BOTH posters and the gate go
through it, so a platform limit cannot drift between them. The gate truncates
ONCE, at the top, so every downstream signal is honest — a stat, an entity or a
link past the cut is not in the published post, and must not earn credit,
satisfy the zero-stat guard, or drive dedup.

This is a deliberate behaviour change: over-limit drafts that "passed" while
shipping mangled are now blocked. That is the point — the block is visible and
actionable, a silently mangled tweet is not.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402

# The pillars X copy exactly as it stood before #1849 — the case that proved it.
OLD_X_359 = (
    "DC Hub shipped 3 things a training-data model can't fake:\n\n"
    "• Provenance — cite with confidence (4,923 verified in a 12,650-tracked frontier)\n"
    "• Global grid — 24 grids ranked across five continents; Japan/Korea/Brazil now live "
    "beside the US & EU\n"
    "• Memory — save a site, get its DCPI deltas back next session\n\n"
    "CC-BY-4.0, over MCP, no signup → dchub.cloud/connect")


# ── the transform ──────────────────────────────────────────────────────────

def test_x_cuts_at_280_with_no_suffix():
    out = cp.as_published("z" * 400, "twitter")
    assert len(out) == 280 and out == "z" * 280


def test_bluesky_cuts_at_297_and_appends_an_ellipsis():
    """Mirrors the poster exactly — the goal is to PREDICT the poster, not to
    be more correct than it."""
    out = cp.as_published("y" * 400, "bluesky")
    assert out == "y" * 297 + "..."
    assert len(out) == 300


def test_linkedin_is_never_truncated():
    """Its poster does not truncate, so its wire text IS the draft."""
    long_post = "x" * 5000
    assert cp.as_published(long_post, "linkedin") == long_post


@pytest.mark.parametrize("platform", ["twitter", "bluesky", "linkedin", "", None])
def test_posts_that_fit_are_returned_unchanged(platform):
    text = "ERCOT added 427 GW to its queue this week. https://dchub.cloud/dcpi"
    assert cp.as_published(text, platform) == text


def test_as_published_never_raises():
    for text in ("", None, "x" * 10000):
        for plat in ("twitter", "bluesky", None, "nonsense"):
            assert isinstance(cp.as_published(text, plat), str)


# ── poster and gate cannot drift ───────────────────────────────────────────

def test_no_poster_truncates_independently():
    """If a poster grows its own [:280] again, the gate silently goes back to
    judging a different artifact. That regression must fail loudly here."""
    src = open(os.path.join(ROOT, "content_publisher.py"), encoding="utf-8").read()
    for bad in ("content_text[:280]", "content_text[:297]"):
        assert bad not in src, (
            "a poster truncates with %s instead of as_published() — the gate "
            "and the wire can now disagree" % bad)


def test_every_wire_limited_platform_is_declared():
    """The posters that truncate are X and Bluesky; both must be in the map,
    or the gate will score a draft for one of them."""
    assert set(cp._WIRE_LIMITS) == {"twitter", "bluesky"}


# ── the behaviour that motivated it ────────────────────────────────────────

def test_the_mangled_tweet_is_now_refused():
    """The whole point: the 280 chars that actually publish lose the link and
    end mid-phrase, and that is what the gate must judge."""
    wire = cp.as_published(OLD_X_359, "twitter")
    assert cp._quality_score(OLD_X_359) >= cp.QUALITY_MIN, (
        "precondition: the draft used to pass")
    assert cp._quality_score(wire) < cp.QUALITY_MIN, (
        "the mangled tweet still clears the gate")


def test_truncation_is_what_costs_it_the_link():
    wire = cp.as_published(OLD_X_359, "twitter")
    def has_link(t):
        return bool(cp._URL_RE.search(t) or cp._BARE_LINK_RE.search(t))
    assert has_link(OLD_X_359) is True
    assert has_link(wire) is False, (
        "if the wire text keeps its link this fixture no longer models the bug")


def test_the_current_pillars_x_draft_is_unaffected():
    """It was refitted in #1849, so truncation is a no-op and it still passes —
    this change must not punish copy that already fits."""
    psm = pytest.importorskip("routes.pillars_master_shell")
    x = psm._drafts({
        "dc_verified": 4923, "dc_tracked": 12650, "countries": 170,
        "markets": 311, "ranked_count": 24,
        "continents_ranked": ["NA", "EU", "AS", "SA", "OC"],
    })["x"]
    assert cp.as_published(x, "twitter") == x
    assert cp._quality_score(x) >= cp.QUALITY_MIN
