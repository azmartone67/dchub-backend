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
    # routes/mcp_connect.py came off 2026-09-10. Its _PAGE_TEMPLATE was the
    # LATCH THAT SHIPPED: the install pages served the cold-start pinned floor
    # while /api/v1/canon/phrases in the same process served the live one. The
    # template is now a raw constant resolved per request in _render_page(), and
    # tests/test_connect_install_pages_derive_canon.py renders it to prove it.
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


def test_registered_names_only_shrink():
    """★ A registered FILE may not grow NEW latched names under its entry.

    The two tests above leave a hole between them. The first only looks at
    files ABSENT from the register; the second only asks whether a registered
    file still latches SOMETHING. Neither compares the NAMES, so once a file
    is listed, a new module-scope canon_text() added beside the registered one
    inherits the exemption and no guard fires — the entry reads as one unit of
    debt while carrying two.

    That is not hypothetical. Measured 2026-09-10 on origin/main, this file
    listed ai_outreach_agent.py as ['_CANON_FAC'] while the module latched
    ['_CANON_FAC', '_CANON_NEWS_SOURCES']; _CANON_NEWS_SOURCES was added under
    cover of the existing entry and drained no ledger. Every other one of the
    25 entries matched its live names exactly.

    Requiring live ⊆ registered makes the register name-level: fixing one latch
    means deleting its NAME in the same commit, and adding one fails here until
    it is written down deliberately.
    """
    live = _latched_modules()
    # Floor: an empty scan would satisfy the subset check vacuously for every
    # entry. test_register_only_shrinks would also fail in that case, but this
    # test must not depend on a sibling to avoid a silent green.
    assert live, (
        "the scanner found NO import-time latches anywhere — it is broken, "
        "not the tree clean (the register below lists 25 known latches)"
    )
    grew = {
        f: sorted(set(live.get(f, ())) - set(registered))
        for f, registered in _KNOWN_LATCHED.items()
        if set(live.get(f, ())) - set(registered)
    }
    assert not grew, (
        "registered file(s) grew a NEW import-time canon latch — the register "
        "entry exempts the FILE, not any name someone adds to it later:\n"
        + "\n".join(
            f"  {f}: {', '.join(n)}\n"
            f"      registered: {', '.join(_KNOWN_LATCHED[f])}"
            for f, n in sorted(grew.items())
        )
        + "\n\nResolve it inside the render/response path (see "
          "canonical_stats.news_sources_phrase() and "
          "routes/partner_landing.py::_canon_values), or add the name to its "
          "register entry with a dated comment saying why it cannot move."
    )


def test_registered_names_are_not_stale():
    """The other half of the ratchet: a FIXED name must leave the register.

    test_registered_names_only_shrink above fails when a file grows a name.
    On its own that still permits the mirror defect test_register_only_shrinks
    closes at file level — a name that has been resolved per request but left
    written down. A stale NAME is indistinguishable from unfixed debt and it
    silently re-exempts that exact identifier if the latch ever comes back.

    Only files that still latch something are checked here; a file that latches
    nothing at all is test_register_only_shrinks' case, and reporting it twice
    would just make one deletion look like two failures.
    """
    live = _latched_modules()
    stale = {
        f: sorted(set(registered) - set(live[f]))
        for f, registered in _KNOWN_LATCHED.items()
        if f in live and set(registered) - set(live[f])
    }
    assert not stale, (
        "register lists name(s) that no longer latch canon at import — delete "
        "the NAME in the same commit as the fix:\n"
        + "\n".join(
            f"  {f}: {', '.join(n)}\n"
            f"      still latching: {', '.join(live[f])}"
            for f, n in sorted(stale.items())
        )
    )
