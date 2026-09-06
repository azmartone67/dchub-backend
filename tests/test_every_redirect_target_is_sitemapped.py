"""If we 301 an alias to a slug, that slug must be a page we advertise.

r-dc-sitemapped (2026-09-06).

WHAT WAS MEASURED
-----------------
Live, cache-busted 2026-09-06, sweeping the invariant this family established
— both sitemaps advertise the same canonical slugs — across every alias twin
rather than the ones already spot-checked:

    /markets/dc              200, "Washington, DC Market Deep-Dive", DCPI 24.1
    sitemap-dcpi.xml         lists /dcpi/dc
    sitemap-markets.xml      575 URLs, /markets/dc ABSENT

A live page, the destination of a live 301 from /markets/washington, in no
sitemap. Both arms of sitemap-markets.xml missed it, for two independent
reasons that predate this work:

  * the unconditional arm emits CURATED_MARKET_SLUGS, and `dc` was not in it;
  * the DB arm routes every slug through listable_market_slug, which drops
    anything under 3 characters as junk (#3571, 2026-09-01) — and `dc` is two.

The DB arm could not have reached it regardless: it joins on
LOWER(REPLACE(city,' ','-')), so Washington folds to `washington`, never to
the market_slug `dc`.

THE PROPERTY
------------
A redirect target that no sitemap lists is a crawl dead-end: the alias URL
tells a crawler "the page is over there", and nothing ever points at over
there. So every canonical target of DCPI_METRO_ALIASES must be reachable by
ONE of the two arms — curated (always emitted) or listable (emitted when the
DB arm produces it).

Deliberately NOT "must be curated". cheyenne and the-dalles are reachable via
the DB arm and carry a REAL per-market lastmod from it; forcing them into the
curated list would swap that for the pinned static date, which is the
r-lastmod-honesty regression. Reachable by either arm is the honest bar.

No DB: both predicates are pure.
"""
import pytest

from routes.market_deep_dive import (CURATED_MARKET_SLUGS,
                                     MARKETS_CANONICAL_REDIRECT,
                                     _slug_title, listable_market_slug)
from util.market_aliases import DCPI_METRO_ALIASES

#: Every slug an alias 301s to.
TARGETS = sorted({c for c in DCPI_METRO_ALIASES.values()})


def test_there_are_targets_to_check():
    """Anti-vacuity: an empty alias table would pass every case below."""
    assert len(TARGETS) >= 8, TARGETS
    assert "dc" in TARGETS, (
        "the pair that motivated this guard is gone; re-point it at whatever "
        "canonical targets exist now")


@pytest.mark.parametrize("target", TARGETS)
def test_every_redirect_target_can_reach_the_sitemap(target):
    """Curated (always emitted) or listable (emitted when the DB arm yields
    it). Failing BOTH is what made /markets/dc a live page in no sitemap."""
    curated = target in CURATED_MARKET_SLUGS
    listable = listable_market_slug(target, set()) is not None
    assert curated or listable, (
        f"/markets/{target} is the destination of a 301 and neither arm of "
        f"sitemap-markets.xml can emit it: it is not in CURATED_MARKET_SLUGS "
        f"and listable_market_slug rejects it (len<3, punctuation, or it is "
        f"itself a redirect key). A redirect target no sitemap lists is a "
        f"crawl dead-end.")


@pytest.mark.parametrize("target", TARGETS)
def test_no_redirect_target_is_itself_a_redirect(target):
    """A target that 301s again is a chain, and listable_market_slug drops
    redirect keys — so such a target would fail the test above too, but for a
    reason worth naming separately."""
    assert target not in MARKETS_CANONICAL_REDIRECT, (
        f"/markets/{target} is both a redirect destination and a redirect "
        f"source")


@pytest.mark.parametrize("slug", sorted(CURATED_MARKET_SLUGS))
def test_every_curated_slug_has_a_human_name(slug):
    """The hub and the sitemap-adjacent listings label pages with
    _slug_title, which falls back to the title-cased slug. `dc` rendered as
    "Dc" for a market whose own page is titled "Washington, DC"."""
    name = _slug_title(slug)
    assert name and name != slug, slug
    # A SHORT slug must not be shown as its own title-cased self: that is the
    # _slug_title fallback firing, and for `dc` it rendered "Dc". Longer slugs
    # are left alone — "seattle" -> "Seattle" is the fallback working
    # correctly, and demanding a hand-written name for all 35 would be noise.
    if len(slug) <= 3:
        assert name != slug.replace("-", " ").title(), (
            f"{slug!r} has no entry in _SLUG_TO_MARKET_NAME, so the hub "
            f"renders the fallback {name!r}")


def test_dc_is_named_and_curated():
    """The specific regression, pinned with its real values."""
    assert "dc" in CURATED_MARKET_SLUGS
    assert _slug_title("dc") == "Washington, DC"


def test_the_targets_that_rely_on_the_db_arm_are_left_there():
    """cheyenne and the-dalles are reachable WITHOUT being curated, and that
    is deliberate: the DB arm gives them a real per-market lastmod, while the
    curated arm would stamp the pinned static date (r-lastmod-honesty).

    Pinned so a later "just curate them all" tidy-up has to argue with this.
    """
    for slug in ("cheyenne", "the-dalles"):
        assert slug in TARGETS
        assert slug not in CURATED_MARKET_SLUGS, (
            f"{slug} was moved into the curated arm; it loses its real "
            f"lastmod for the pinned static date")
        assert listable_market_slug(slug, set()) == slug
