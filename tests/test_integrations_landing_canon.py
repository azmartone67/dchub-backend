"""The MCP integrations pages must not type their own entity counts.

Measured live 2026-09-07: /integrations/mcp served "1,500+ tracked transactions"
against a canon of 2,100+ and a MEASURED 2,118 distinct deduped deals — an
under-claim of roughly 600, on the page the MCP onboarding funnel points at.

It survived because the page was HALF-canonised, the shape
tests/test_partner_landing_canon.py already names: 32 {canon_facilities}
placeholders and 4 {canon_tools} sat beside 10 hand-typed deal literals, so the
stale number looked exactly as trustworthy as its derived neighbours.

★ NOTHING CAUGHT IT, and that is the reason this file exists. Verified by
mutation on 2026-09-07: restoring all 10 literals left the entire canon suite
green (987 passed, both before and after). The repo's other deal guards do not
reach here —
  * tests/test_agent_surface_floors_match_canon.py scans a fixed SURFACES list
    of DISCOVERY files (llms.txt, README.md, mcp.json …); no route module is in
    it, and its RETIRED_DEAL_FLOORS is scoped to floors that were live-wrong on
    2026-08-31, which "1,500+" was not.
  * test_canonical_counts_drift's repo scan flags the value canon most recently
    RETIRED, so it caught "2,000+" when canon walked to 2,100+ and never looked
    at a floor four generations old.

A denylist detects; only derivation fixes. So this asserts DERIVATION.

★2026-09-07 (same day, second pass): the deal count was not the only typed
figure — it was the one I happened to look at. Widened to TOOLS and MARKETS
after finding, on the same page:

    "80 tools"       x12       "311 markets"        x4      against canon
    "79 MCP tools"   x2        "311 power markets"  x1      88 tools / 300+
    "80+ tools"      x2
    "39 such tools"  x1
    "83 tools"       x1  (in a comment warning about exactly this rot)

`{canon_tools}` moved 85 -> 88 between the two passes of this very fix, which is
the argument for derivation in one line: a literal that was correct this morning
was wrong by lunchtime.

★ TWO LITERALS ARE DELIBERATELY LEFT, and both are bounded below rather than
waived silently:
  * "to 10 tools" is the recommended `allowed_tools` SUBSET size, not a total —
    scoping advice for a client config. Deriving it would have made the page
    tell readers to allow all 88.
  * "83 tools" survives inside a COMMENT that exists to warn about this rot.
    _code_only() strips comment lines, so the guard cannot be defeated by
    quoting the stale value in a note — and cannot be tripped by it either.
"""
import re

import pytest

SRC_PATH = "routes/integrations_landing.py"
SRC = open(SRC_PATH, encoding="utf-8").read()

#: The three pages this module serves, as PER-REQUEST RENDER FUNCTIONS.
#:
#: ★2026-09-10 — these used to be module constants read with getattr(). All
#: three are now raw templates resolved per request (canon_text() at import
#: froze the numbers for the life of the worker), so a getattr() here would
#: hand this guard the UNRESOLVED template: "{canon_deals}" instead of a deal
#: figure, which every findall() below silently reads as "states no figure" —
#: vacuous green over a page that could be serving anything. Render, then
#: assert on what the reader actually gets.
RENDERERS = ("render_mcp_landing", "render_mcp_seo_page", "render_meta_landing")


def _rendered_pages():
    """{renderer name: html}. A missing renderer FAILS, it does not skip."""
    import routes.integrations_landing as il

    out = {}
    for name in RENDERERS:
        fn = getattr(il, name, None)
        assert callable(fn), (
            f"{SRC_PATH}.{name}() is gone — it was renamed and this guard "
            "would otherwise have checked one page fewer without saying so.")
        out[name] = fn()
    return out

#: A deal figure typed as a literal, e.g. "1,500+ tracked transactions".
TYPED_DEALS = re.compile(
    r'[0-9],[0-9]{3}\+\s*(?:</b>\s*<span>)?\s*tracked\s+(?:transactions|deals)',
    re.I)


def _code_only(text: str) -> str:
    """Source minus comment lines — a future note here may quote the retired
    literal on purpose, and a whole-file grep would then fail on the fix."""
    return "\n".join(l for l in text.split("\n") if not l.strip().startswith("#"))


def test_no_typed_deal_count_in_the_integrations_copy():
    hits = TYPED_DEALS.findall(_code_only(SRC))
    assert not hits, (
        f"{SRC_PATH} types its own deal count: {hits}. Render it from canon "
        "with {canon_deals} instead — this page is the MCP funnel's landing "
        "page and it served '1,500+ tracked transactions' against a measured "
        "2,118 for weeks.")


def test_the_deals_placeholder_is_actually_present():
    """Floor. Every assertion here is a scan, and a scan over a file that
    stopped mentioning deals at all would pass vacuously while the page quietly
    lost the claim."""
    n = _code_only(SRC).count("{canon_deals}")
    assert n >= 5, (
        f"only {n} {{canon_deals}} placeholders in {SRC_PATH} — the copy used "
        "to carry 10. Either the deal claims were deleted rather than derived, "
        "or the placeholder name changed and this guard is now scanning for a "
        "token nothing emits.")


def test_the_rendered_pages_state_the_canonical_deal_figure():
    """Behaviour, not source text: what the constants actually render.

    Asserted against canon rather than against a pinned literal, so this keeps
    working across every future walk and fails the moment the page and canon
    disagree — which is the only failure that matters to a reader.
    """
    pytest.importorskip("flask")
    import ai_surface_canon as canon

    expected = canon.canon_nums().get("{canon_deals}")
    assert expected, "canon publishes no deals phrase — cannot assert derivation"

    pages = _rendered_pages()
    assert len(pages) >= 3, sorted(pages)
    for name, html in sorted(pages.items()):
        assert "{canon_" not in html, (
            f"{name}() serves an UNRESOLVED canon placeholder — worse than the "
            "stale number it replaced. canon_text() the template it renders.")
        figures = set(re.findall(
            r'([0-9],[0-9]{3}\+)\s*(?:</b>\s*<span>)?\s*tracked\s+'
            r'(?:transactions|deals)', html, re.I))
        assert figures <= {expected}, (
            f"{name}() states deal figure(s) {sorted(figures - {expected})} but "
            f"canon says {expected!r}")


# ── tools and markets: same contract, added 2026-09-07 ─────────────────────

#: A tool count typed as a literal. The allowed_tools subset is excluded by the
#: SUBSET_OK carve-out below, never by loosening this pattern.
TYPED_TOOLS = re.compile(r'\b(\d{2,3})\+?\s*(?:MCP\s+|such\s+)?tools?\b', re.I)
TYPED_MARKETS = re.compile(r'\b(\d{3})\+?\s*(?:power\s+)?markets?\b', re.I)

#: The ONE legitimate typed tool figure: the recommended allowed_tools subset.
SUBSET_OK = "to 10 tools"


def test_no_typed_tool_count_in_the_integrations_copy():
    code = _code_only(SRC)
    hits = [h for h in TYPED_TOOLS.findall(code) if h != "10"]
    assert not hits, (
        f"{SRC_PATH} types its own tool count: {sorted(set(hits))}. Render it "
        "from canon with {canon_tools} — this page carried '80 tools' x12 while "
        "canon said 88, and canon moved 85 -> 88 inside a single day.")


def test_the_allowed_tools_subset_is_the_only_typed_tool_figure():
    """The carve-out is bounded, not a hole.

    "to 10 tools" is scoping advice for a client config, so it must stay a
    literal. This pins it to exactly one occurrence: if a second typed '10
    tools' ever appears it is almost certainly a real count that slipped in
    under cover of the exception, and the assertion above cannot see it.
    """
    code = _code_only(SRC)
    assert code.count(SUBSET_OK) == 1, (
        f"expected exactly one {SUBSET_OK!r} (the allowed_tools subset), found "
        f"{code.count(SUBSET_OK)}. A second one is either a duplicate or a real "
        "tool count hiding behind the carve-out.")
    assert len([h for h in TYPED_TOOLS.findall(code) if h == "10"]) == 1, (
        "more than one typed '10 tools' in the copy — see above.")


def test_no_typed_market_count_in_the_integrations_copy():
    hits = TYPED_MARKETS.findall(_code_only(SRC))
    assert not hits, (
        f"{SRC_PATH} types its own market count: {sorted(set(hits))}. Render it "
        "from canon with {canon_markets}, which already carries its own '+' — "
        "the page said '311 markets' against a canon floor of 300+.")


def test_the_rendered_pages_state_canonical_tools_and_markets():
    """Behaviour, against canon rather than a pinned literal."""
    pytest.importorskip("flask")
    import ai_surface_canon as canon

    nums = canon.canon_nums()
    want_tools, want_markets = nums.get("{canon_tools}"), nums.get("{canon_markets}")
    assert want_tools and want_markets, "canon publishes no tools/markets phrase"

    pages = _rendered_pages()
    assert len(pages) >= 3, sorted(pages)
    for name, html in sorted(pages.items()):
        tools = {t for t in TYPED_TOOLS.findall(html) if t != "10"}
        assert tools <= {want_tools}, (
            f"{name}() states tool count(s) {sorted(tools - {want_tools})} but "
            f"canon says {want_tools!r}")
        markets = set(TYPED_MARKETS.findall(html))
        bare = want_markets.rstrip("+")
        assert markets <= {bare}, (
            f"{name}() states market count(s) {sorted(markets - {bare})} but "
            f"canon says {want_markets!r}")


# ── substations, added 2026-09-07 (third pass on the same page) ─────────────

TYPED_SUBSTATIONS = re.compile(r'\b([\d,]{5,})\+?\s*substations?\b', re.I)


def test_no_typed_substation_count_in_the_integrations_copy():
    """126,427 was this repo's DB-DOWN SEED, pasted into prose and frozen.

    ai_surface_canon's own note records the identical literal escaping into
    /.well-known/mcp.json while the live snapshot measured 127,269 — so this is
    the second surface to carry the same frozen seed, and the reason the fix is
    {canon_substations} rather than a corrected number.
    """
    hits = TYPED_SUBSTATIONS.findall(_code_only(SRC))
    assert not hits, (
        f"{SRC_PATH} types its own substation count: {sorted(set(hits))}. "
        "Render it from canon with {canon_substations} — the literal 126,427 is "
        "this module's DB-down seed, not a measurement.")


def test_the_rendered_pages_state_the_canonical_substation_figure():
    pytest.importorskip("flask")
    import ai_surface_canon as canon

    want = canon.canon_nums().get("{canon_substations}")
    assert want, "canon publishes no substations phrase"

    seen_any = False
    for name, html in sorted(_rendered_pages().items()):
        figures = set(TYPED_SUBSTATIONS.findall(html))
        if figures:
            seen_any = True
        assert figures <= {want.rstrip("+")}, (
            f"{name}() states substation count(s) "
            f"{sorted(figures - {want.rstrip('+')})} but canon says {want!r}")
    assert seen_any, (
        "no constant states a substation count any more. Floor: this assertion "
        "is a scan, and a page that dropped the claim entirely would pass it "
        "vacuously while quietly losing a coverage figure agents cite.")
