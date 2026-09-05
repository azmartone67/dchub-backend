"""Canon must resolve PER REQUEST, not once at import.

`_X = canon_text("{canon_y}")` at module scope reads as canon-bound and is
not. canon_text() runs ONCE, when the module is imported, so the value freezes
for the life of the process. It satisfies
tests/test_canon_placeholders_resolved.py — the placeholder genuinely IS inside
a resolver call — and it drains the ledger entry, while the surface keeps
serving whatever the canon said at boot.

Measured on the live site before this fence existed, same host, same second:

    /api/v1/canon/phrases   facilities = one value
    /partners/cohere        facilities = an OLDER one

Two answers to one question, from one process. dchub-backend #3831 named the
mechanism while RETIRING a page rather than routing it, on the grounds that
routing it would publish that drift to readers.

★ WHY A VALUE COMPARISON CANNOT FIND THIS. A test that asserts the surface
equals canonical_stats.<phrase>() reads the SAME latched value the surface
does, in the same process, so it passes either way. The only check that
separates "derived" from "frozen" is to MOVE the canon and require the surface
to follow — which is what the *_not_latched_at_import tests in
tests/test_dcpi_market_count_derived.py do.

This module is the structural half: no NEW module may join the latched set.
"""
import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP = ("node_modules", "/.git/", "/tests/", "dchub-frontend/")

# ── the debt register ────────────────────────────────────────────────────
# Every entry is a real import-time latch in shippable code. Same contract as
# tests/test_canonical_counts_drift.py's KNOWN_STALE_COUNT_DEBT: this is a
# DEBT REGISTER, not an allow-list, and the only legal direction is smaller.
# Fixing one means deleting its entry in the same commit — a fix that leaves
# the entry behind re-permits the defect the moment someone re-adds it.
#
# routes/partner_landing.py and routes/mcp_outreach_drafts.py were removed
# from this list by the change that introduced it.
# routes/architecture_landing.py came off because #3837 deleted the module
# outright — a retired surface is a legitimate way for this list to shrink,
# and test_register_only_shrinks is what noticed the entry had gone stale.
_KNOWN_LATCHED = {
    'agent_hub.py': ['SALES_SYSTEM_PROMPT', '_CANON_FAC'],
    'ai_agent_discovery.py': ['AGENTS_MD_FALLBACK'],
    'ai_interconnection.py': ['_CANON_FAC'],
    'ai_outreach_agent.py': ['_CANON_FAC'],
    'api_response_enrichment.py': ['_CANON_FAC'],
    'dchub-fix-all.py': ['STATS_SCRIPT'],
    'enhanced_promotion.py': ['_CANON_FAC'],
    'generate_facility_pages.py': ['_CANON_FAC'],
    'global_intelligence_agent.py': ['_CANON_FAC'],
    'inject_meta_tags.py': ['_CANON_FAC'],
    'linkedin_image_post.py': ['POST_TEXT'],
    'linkedin_poster.py': ['_CANON_FAC'],
    'moltbook_integration.py': ['AGENT_DESCRIPTION'],
    'routes/brain_answer_cache.py': ['_VERIFY_SYSTEM'],
    'routes/comprehensive_report.py': ['_CANON_FAC'],
    'routes/dchub_media_hub.py': ['_CANON_FAC'],
    'routes/demo.py': ['DEMO_SYSTEM_PROMPT'],
    'routes/integrations_landing.py': ['MCP_LANDING_HTML', 'MCP_SEO_PAGE_HTML', 'META_LANDING_HTML', '_RECIPE_PAGE_TEMPLATE'],
    'routes/mcp_connect.py': ['_PAGE_TEMPLATE'],
    'routes/media_editorial.py': ['ANALYST_VOICE'],
    'routes/media_outreach.py': ['_CANON_FAC'],
    'routes/onboard_auto_approve.py': ['_CANON_FAC'],
    'routes/onboarding_recover.py': ['_CANON_DEALS', '_CANON_FAC'],
    'routes/quick_redirects.py': ['_AGENTS_MD'],
    'routes/seo_pages.py': ['_CANON_DEALS', '_CANON_FAC'],
    'seo_promotion_engine.py': ['_CANON_FAC'],
}


def _latched_modules():
    """{relpath: [names]} for every module-scope canon_text() assignment."""
    found = {}
    for p in sorted(_ROOT.rglob("*.py")):
        rel = str(p.relative_to(_ROOT))
        if any(x in "/" + rel for x in _SKIP):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        names = []
        for node in tree.body:                       # MODULE SCOPE ONLY
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", "") == "canon_text"):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        if names:
            found[rel] = sorted(names)
    return found


def test_no_new_import_time_canon_latch():
    """A module not already in the register may not latch canon at import."""
    live = _latched_modules()
    new = {f: n for f, n in live.items() if f not in _KNOWN_LATCHED}
    assert not new, (
        "NEW import-time canon latch — canon_text() at module scope freezes "
        "the value for the life of the process:\n"
        + "\n".join(f"  {f}: {', '.join(n)}" for f, n in sorted(new.items()))
        + "\n\nResolve inside the render/response path instead. See "
          "routes/partner_landing.py::_canon_values or "
          "routes/competitive_intel.py::_resolved_differentiators."
    )


@pytest.mark.parametrize("fname", sorted(["routes/partner_landing.py",
                                          "routes/mcp_outreach_drafts.py"]))
def test_fixed_modules_stay_fixed(fname):
    """The two modules this fence was written for must not regress."""
    assert fname not in _latched_modules(), (
        f"{fname} latches canon at import again"
    )


def test_register_only_shrinks():
    """An entry whose module no longer latches must be DELETED, not kept.

    A stale register entry is indistinguishable from unfixed debt, and it
    silently exempts the file if the latch ever comes back.
    """
    live = _latched_modules()
    stale = sorted(f for f in _KNOWN_LATCHED if f not in live)
    assert not stale, (
        "these modules no longer latch canon at import — delete their register "
        "entries in the same commit as the fix:\n  " + "\n  ".join(stale)
    )
