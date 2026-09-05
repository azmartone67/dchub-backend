"""The real routes.market_deep_dive canon constants, for tests.

r-market-canon-split (2026-09-05).

WHY THIS EXISTS
---------------
Six test files each carried their own `ast.literal_eval` reader for
MARKETS_CANONICAL_REDIRECT / CURATED_MARKET_SLUGS /
MARKETS_DEEP_DIVE_PAGE_CANON, on the house rule that "tests NEVER import
main" and the belief that importing routes/* pulls main in.

Those three constants are now DERIVED from util.market_aliases rather than
typed out, precisely so a second opinion about which slug names a market
cannot exist — which is the defect this change fixes. A derived value is not
a literal, so `literal_eval` raises `ValueError: malformed node or string`,
and a reader that fell back to skipping it would hand every caller a silently
missing constant instead.

IMPORTING IS THE FIX, AND IT IS SAFE — but "safe" is asserted here, not
assumed: routes.market_deep_dive does not import main at module level, and
`assert_no_main_import()` fails the moment that stops being true. That is
strictly stronger than the AST workaround it replaces, which could only ever
prove the rule was being followed by not exercising it.
"""
import sys


def market_deep_dive():
    """The module under test, with the no-main house rule enforced."""
    before = "main" in sys.modules
    import routes.market_deep_dive as mdd
    if not before:
        assert "main" not in sys.modules, (
            "importing routes.market_deep_dive pulled in main — the house "
            "rule that tests never import main is now broken by this helper. "
            "Move the offending import inside a function in "
            "routes/market_deep_dive.py rather than deleting this assertion.")
    return mdd


def canonical_redirect():
    """MARKETS_CANONICAL_REDIRECT — alias slug -> the /markets slug it 301s to."""
    return dict(market_deep_dive().MARKETS_CANONICAL_REDIRECT)


def curated_slugs():
    """CURATED_MARKET_SLUGS — the metro pages sitemap-markets.xml always emits."""
    return tuple(market_deep_dive().CURATED_MARKET_SLUGS)


def page_canon():
    """MARKETS_DEEP_DIVE_PAGE_CANON — page slug -> market_deep_dives row key."""
    return dict(market_deep_dive().MARKETS_DEEP_DIVE_PAGE_CANON)
