"""Canon must resolve PER REQUEST, not once at import.

`_X = canon_text("{canon_y}")` at module scope reads as canon-bound and is
not. canon_text() runs ONCE, when the module is imported, so the value
freezes for the life of the process. It satisfies
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

★2026-09-13 — A LATCH DOES NOT HAVE TO BE THE WHOLE VALUE. This scan used to
match only `X = canon_text(...)`. A canon_text() call NESTED inside a
module-scope literal (a list of tool dicts, a dict of templates, a
.replace() chain) runs once at import all the same, and the scan could not
see it: routes/integrations_landing.py `_WEBMCP_TOOLS` served the cold-start
pinned facility floor on the live /integrations page that way (fixed in
#4580). The scan now asks whether BUILDING the bound value calls canon_text()
anywhere, except inside a lambda body, which runs per call
(routes/agent_a2a.py `_LIVE_SKILL_SUMMARIES`). test_scanner_contract pins
exactly which shapes count.
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
#
# ★2026-09-13 — entries marked (09-13) were found when the scan learned to
# see NESTED calls, class bodies and an aliased canon_text (main.py imports it
# as _canon_text). They are written down as found, not fixed. Most are one-shot
# scripts, outreach/SEO copy generators and email templates. The ones a web
# request serves: the seven *_RECIPE_HTML pages (which also sit behind the
# already-listed _RECIPE_PAGE_TEMPLATE), A2A_AGENT_CARD, main.py's
# _MCP_LANDING_HTML, NAV_LINKS, the paywall hint _VARIANTS and _DCHUB_FACTS.
# routes/agent_a2a.py came off the same day: AGENT_CARD's copies are raw
# templates now, and only _card() resolves them, per request.
# routes/partner_landing.py was FIXED instead: three _PARTNERS strings still
# wrapped {canon_facilities} in canon_text() beside @@CANON_*@@ copy the page
# already resolves per request. They carry the token now, so
# test_fixed_modules_stay_fixed holds under the wider scan.
_KNOWN_LATCHED = {
    'agent_hub.py': ['SALES_SYSTEM_PROMPT', 'SEO_POST_TEMPLATES',   # (09-13)
                     '_CANON_FAC'],
    'ai_agent_discovery.py': ['A2A_AGENT_CARD',                     # (09-13)
                              'AGENTS_MD_FALLBACK'],
    'ai_interconnection.py': ['_CANON_FAC'],
    'ai_outreach_agent.py': ['AI_PLATFORMS', 'MCP_SERVICE_HEADERS',  # (09-13)
                             'SOCIAL_PLATFORMS', '_CANON_FAC'],
    'api_response_enrichment.py': ['_CANON_FAC'],
    'backend_patch_mcp_routes.py': ['SERVER_CARD'],                 # (09-13)
    'competitor_intelligence.py': ['CompetitorAnalysis.COVERAGE_GAPS',  # (09-13)
                                   'CompetitorAnalysis.DC_HUB_ADVANTAGES'],
    'dchub-fix-all.py': ['STATS_SCRIPT'],
    'email_service.py': ['WELCOME_SERIES_TEMPLATES'],               # (09-13)
    'enhanced_promotion.py': ['_CANON_FAC'],
    'fix_slug_body_update.py': ['PRESS_RELEASES'],                  # (09-13)
    'gdci.py': ['GDCI_METHODOLOGY'],                                # (09-13)
    'generate_facility_pages.py': ['_CANON_FAC'],
    'global_intelligence_agent.py': ['_CANON_FAC'],
    'inject_meta_tags.py': ['HOME_META', 'TOOL_META',               # (09-13)
                            '_CANON_FAC'],
    'linkedin_image_post.py': ['POST_TEXT'],
    'linkedin_poster.py': ['_CANON_FAC'],
    'main.py': ['_MCP_LANDING_HTML'],                               # (09-13)
    'moltbook_integration.py': ['AGENT_DESCRIPTION'],
    'populate_press_bodies.py': ['PRESS_RELEASES'],                 # (09-13)
    'routes/ai_platform_tool_tuner.py': ['GENERIC_DESCRIPTIONS'],   # (09-13)
    'routes/brain_answer_cache.py': ['_VERIFY_SYSTEM'],
    'routes/competitive_vs.py': ['_DCHUB_FACTS'],                   # (09-13)
    'routes/comprehensive_report.py': ['_CANON_FAC'],
    'routes/content_enqueue.py': ['_CAMPAIGN_POSTS'],               # (09-13)
    'routes/dchub_media_hub.py': ['_CANON_FAC'],
    'routes/demo.py': ['DEMO_SYSTEM_PROMPT'],
    'routes/devrel_targets.py': ['PLATFORM_BLUEPRINTS'],            # (09-13)
    # routes/integrations_landing.py came off 2026-09-13. MCP_LANDING_HTML
    # (the latch that SHIPPED "20,700+" on /integrations/mcp), MCP_SEO_PAGE_HTML
    # and META_LANDING_HTML went first, on 09-10, as raw templates resolved in
    # render_*(). The seven *_RECIPE_HTML pages and _RECIPE_PAGE_TEMPLATE
    # followed: slot dicts whose canon slots are lambdas, rendered by
    # _recipe_page() inside each handler. tests/test_integrations_recipes_
    # follow_canon.py moves canon and requires every placeholder to follow.
    # routes/mcp_connect.py came off 2026-09-10. Its _PAGE_TEMPLATE was the
    # LATCH THAT SHIPPED: the install pages served the cold-start pinned floor
    # while /api/v1/canon/phrases in the same process served the live one. The
    # template is now a raw constant resolved per request in _render_page(), and
    # tests/test_connect_install_pages_derive_canon.py renders it to prove it.
    'routes/media_editorial.py': ['ANALYST_VOICE'],
    'routes/media_outreach.py': ['_CANON_FAC'],
    'routes/nav_config_routes.py': ['NAV_LINKS'],                   # (09-13)
    'routes/onboard_auto_approve.py': ['_CANON_FAC'],
    'routes/onboarding_recover.py': ['_CANON_DEALS', '_CANON_FAC'],
    'routes/paywall_hint_middleware.py': ['_VARIANTS'],             # (09-13)
    'routes/quick_redirects.py': ['_AGENTS_MD'],
    'routes/seo_pages.py': ['_CANON_DEALS', '_CANON_FAC'],
    'seo_agents.py': ['OUTREACH_TEMPLATES', 'SOCIAL_TEMPLATES'],    # (09-13)
    'seo_meta_tags.py': ['HOME_META', 'TOOL_META'],                 # (09-13)
    'seo_promotion_engine.py': ['_CANON_FAC'],
    'welcome_emails.py': ['EMAILS', '_CANON_SUBSTATIONS'],          # (09-13)
}

# Statements whose bodies run while the module is imported.
_IMPORT_TIME_BLOCKS = tuple(getattr(ast, n) for n in
                            ("If", "For", "While", "With", "Try", "TryStar")
                            if hasattr(ast, n))


def _resolvers(tree):
    """canon_text, plus every name this module imports it AS.

    main.py does `from ai_surface_canon import canon_text as _canon_text`, so a
    scan keyed on the literal name canon_text never saw its latch.
    """
    names = {"canon_text"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names
                         if a.name == "canon_text" and a.asname)
    return names


def _resolves_canon(expr, resolvers):
    """True if EVALUATING `expr` calls canon_text(), however deeply nested.

    A call inside a list, a dict, a .replace() chain or an f-string runs when
    the enclosing value is built — at import, for a module-scope value. A
    lambda BODY is the one place not searched: it runs when the lambda is
    called. The lambda's default values are built with it, so they are.
    """
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            stack += node.args.defaults + [d for d in node.args.kw_defaults if d]
            continue
        if isinstance(node, ast.Call) and (
                getattr(node.func, "id", None) in resolvers
                or getattr(node.func, "attr", None) == "canon_text"):
            return True
        stack += ast.iter_child_nodes(node)
    return False


def _bound_names(target):
    """X, (A, B), X["k"] and X.attr all bind the latched value under a name."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _bound_names(elt)]
    while isinstance(target, (ast.Subscript, ast.Attribute, ast.Starred)):
        target = target.value
    return [target.id] if isinstance(target, ast.Name) else []


def _latched_names(body, resolvers, prefix=""):
    """Names bound at import to a value whose construction called canon_text().

    Walks module-scope statements, plus the bodies of module-scope
    if/try/with/for/while blocks and class bodies, since those run at import
    too. A def body runs per call and is never entered.
    """
    names = []
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if node.value is not None and _resolves_canon(node.value, resolvers):
                targets = getattr(node, "targets", None) or [node.target]
                names += [prefix + n for t in targets for n in _bound_names(t)]
        elif isinstance(node, ast.ClassDef):
            names += _latched_names(node.body, resolvers, f"{prefix}{node.name}.")
        elif isinstance(node, _IMPORT_TIME_BLOCKS):
            inner = (node.body + getattr(node, "orelse", [])
                     + getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                inner = inner + handler.body
            names += _latched_names(inner, resolvers, prefix)
    return names


def _latched_modules():
    """{relpath: [names]} for every name a module latches canon into at import."""
    found = {}
    for p in sorted(_ROOT.rglob("*.py")):
        rel = str(p.relative_to(_ROOT))
        if any(x in "/" + rel for x in _SKIP):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "canon_text" not in text:     # every shape above names it somewhere
            continue
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        names = _latched_names(tree.body, _resolvers(tree))
        if names:
            found[rel] = sorted(set(names))
    return found


def _latched_in(src):
    tree = ast.parse(src)
    return sorted(set(_latched_names(tree.body, _resolvers(tree))))


def test_scanner_contract():
    """★ Which shapes the scanner counts as a latch, pinned directly.

    Every other test here compares the scan to the register. If the scanner
    quietly stopped seeing a shape, the latches of that shape would read as
    FIXED, and test_registered_names_are_not_stale would demand their names be
    deleted, turning a blind scanner into a shorter register. These cases pin
    the contract without depending on the tree.
    """
    latched = {
        'X = canon_text(T)': ['X'],
        'X = [{"description": canon_text(T)}]': ['X'],
        'X = canon_text(T).replace("a", "b")': ['X'],
        'X = "<p>" + canon_text(T) + "</p>"': ['X'],
        'from ai_surface_canon import canon_text as _ct\nX = {"k": _ct(T)}': ['X'],
        'import ai_surface_canon\nX = (ai_surface_canon.canon_text(T),)': ['X'],
        'X: list = [canon_text(T)]': ['X'],
        'try:\n    X = [canon_text(T)]\nexcept Exception:\n    X = []': ['X'],
        'class C:\n    X = [canon_text(T)]': ['C.X'],
        'X = {}\nX["k"] = canon_text(T)': ['X'],
        # a lambda's DEFAULT is evaluated when the literal is built
        'X = [lambda t=canon_text(T): t]': ['X'],
    }
    per_request = [
        'X = {"k": lambda: canon_text(T)}',     # routes/agent_a2a.py's shape
        'def render():\n    return [canon_text(T)]',
        'T = "x"\ndef render():\n    return canon_text(T)',
    ]
    for src, names in latched.items():
        assert _latched_in(src) == names, f"scanner missed an import-time latch: {src!r}"
    for src in per_request:
        assert _latched_in(src) == [], f"scanner flagged a per-request resolve: {src!r}"


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
        "not the tree clean (the register below lists known latches)"
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
