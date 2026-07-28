"""Scheme-less links earn link credit — 2026-07-28.

_URL_RE requires https?://, but the X/Twitter drafts sign off with a bare
"→ dchub.cloud/connect" (X auto-links bare domains, and the house style for a
280-char post drops the scheme). So _quality_score scored a REAL link as no
link: the pillars X card lost 0.20 purely to a missing "https://" and sat at
0.150, silently refused. Its LinkedIn sibling carries the same bare link and
only cleared the gate because other signals covered for it (0.800 -> 1.000
once the link actually counts).

_BARE_LINK_RE requires a PATH on purpose:
  • "dchub.cloud/connect" is somewhere to go — credit.
  • "cite as DC Hub (dchub.cloud)" is a citation, not a destination — no credit.
  • it also drops the false-positive family a generic domain pattern opens up:
    "content_publisher.py", "v2.9.3", "e.g." carry no path, and the TLD list is
    curated so a module filename can never look like a domain.

It feeds the STRIP as well as the credit, for the reason r65-qa introduced the
strip: a bare "dchub.cloud/news/2026-07-28-report-9000" faked exactly the
number+year credit that was already closed for the https:// form.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402


def _is_link(text):
    return bool(cp._URL_RE.search(text) or cp._BARE_LINK_RE.search(text))


# ── real destinations earn the credit ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Connect your AI in 60 seconds → dchub.cloud/connect",
    "Start at dchub.cloud/connect#start",
    "Docs: dchub.cloud/mcp",
    "Mirrored at example.com/report",
    "See https://dchub.cloud/dcpi for the index",      # the pre-existing form
])
def test_destinations_count_as_links(text):
    assert _is_link(text) is True


# ── …and nothing that merely looks domain-shaped does ──────────────────────

@pytest.mark.parametrize("text", [
    "Cite it as DC Hub (dchub.cloud), CC-BY-4.0",   # citation, not a destination
    "The LIVE handler is content_publisher.py",
    "See routes/pillars_master_shell.py for it",
    "gateway v2.9.3 shipped to production",
    "Grid layers, e.g. substations and feeders",
    "The feeder publishes 14.5 MW of load",
    "Reach us at partnerships@dchub.cloud",          # email, not a link
    "It shipped.The next one lands Friday",          # missing-space typo
])
def test_domain_shaped_text_is_not_a_link(text):
    assert _is_link(text) is False


def test_link_recogniser_never_raises():
    for bad in ("", "   ", "/", "://", "a." * 200):
        assert _is_link(bad) in (True, False)


# ── the strip closes the fake-number hole for bare links too ───────────────

FAKER = "DC Hub shipped something. dchub.cloud/news/2026-07-28-report-9000"


def test_bare_link_slug_cannot_fake_a_number_or_year():
    """r65-qa closed this for https:// links; a scheme-less one walked straight
    back through it. The digits must not survive into the number/freshness check."""
    stripped = cp._BARE_LINK_RE.sub(" ", cp._URL_RE.sub(" ", FAKER))
    assert not any(c.isdigit() for c in stripped), (
        "a bare link's slug still supplies digits: %r" % stripped)


def test_the_faker_scores_lower_not_higher():
    """Guards the direction: this post SHOULD lose credit, and must stay well
    under the gate — it is a link and a sentence, with no real number at all."""
    assert cp._quality_score(FAKER) < cp.QUALITY_MIN


# ── the drafts that motivated it ───────────────────────────────────────────

@pytest.fixture(scope="module")
def pillars():
    psm = pytest.importorskip("routes.pillars_master_shell")
    return psm._drafts({
        "dc_verified": 4923, "dc_tracked": 12650, "countries": 170,
        "markets": 311, "ranked_count": 24,
        "continents_ranked": ["NA", "EU", "AS", "SA", "OC"],
    })


@pytest.mark.parametrize("key", ["linkedin", "x"])
def test_pillars_drafts_get_credit_for_their_link(key, pillars):
    """Both sign off with a bare dchub.cloud/... — neither used to count."""
    assert _is_link(pillars[key]) is True, (
        "the %s draft's link is invisible to the scorer again" % key)


def test_pillars_linkedin_clears_the_gate(pillars):
    score = cp._quality_score(pillars["linkedin"])
    assert score >= cp.QUALITY_MIN, "pillars linkedin scored %.3f" % score
