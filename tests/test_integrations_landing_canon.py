"""The MCP integrations pages must not type their own deal count.

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
"""
import re

import pytest

SRC_PATH = "routes/integrations_landing.py"
SRC = open(SRC_PATH, encoding="utf-8").read()

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
    import routes.integrations_landing as il

    expected = canon.canon_nums().get("{canon_deals}")
    assert expected, "canon publishes no deals phrase — cannot assert derivation"

    checked = 0
    for name in ("MCP_LANDING_HTML", "MCP_SEO_PAGE_HTML", "META_LANDING_HTML"):
        html = getattr(il, name, None)
        if not html:
            continue
        checked += 1
        assert "{canon_" not in html, (
            f"{name} serves an UNRESOLVED canon placeholder — worse than the "
            "stale number it replaced. Wrap the string in canon_text().")
        figures = set(re.findall(
            r'([0-9],[0-9]{3}\+)\s*(?:</b>\s*<span>)?\s*tracked\s+'
            r'(?:transactions|deals)', html, re.I))
        assert figures <= {expected}, (
            f"{name} states deal figure(s) {sorted(figures - {expected})} but "
            f"canon says {expected!r}")
    assert checked >= 3, (
        f"only {checked} of the 3 named constants were found in "
        "routes/integrations_landing.py — they were renamed, and this guard "
        "just checked almost nothing.")
