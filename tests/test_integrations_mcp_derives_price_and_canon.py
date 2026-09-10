"""routes/integrations_landing.py — every page it serves derives its numbers.

Measured live 2026-09-10, cf-cache-status MISS on the current worker:

    Pro ($199/mo)      the button beside it charges $99, and tier_registry has
                       said 99 since r-price-collapse (2026-09-05)
    "20,700+"  x6      canon's facilities floor is walked by healers between
                       deploys; this page froze it at import

#4320 fixed the same pair on /connect/*, and routes/agent_concierge.py had the
canon half on /agent. This module had both and the sweep missed it.

★ SECOND PASS, SAME DAY. The first pass fixed ONE of this module's three
canon_text()-at-import templates (/integrations/mcp) and published the other
two as known-uncovered rather than letting a narrow guard read as full
coverage. This file now covers all three: /integrations/mcp,
/integrations/mcp/data-center-mcp-server and /integrations/meta.

The siblings' retyped price was "$9/mo" and it was CORRECT when it was found —
tier_registry.price('starter') really is 9. That is the entire argument for
fixing it anyway: the $199 was correct in exactly the same way until
r-price-collapse moved the number and left the page advertising the old one. A
correct literal is a wrong literal that has not been repriced yet.

WHY EACH CHECK IS SHAPED THE WAY IT IS
  · The price assertions compare the SET of "$N/mo" figures a page renders
    against the set tier_registry produces. A bare `"$199" not in html` is the
    substring trap that has bitten this repo before — a banned literal that is
    a suffix of a bigger legitimate number passes while the page is wrong, and
    a legitimate number that CONTAINS the banned one fails while it is right.
  · The per-request checks walk the AST. A grep for `canon_text` stays green
    when the call moves back to module scope, because the per-request call site
    mentions it too.
  · The module-wide price scan carries a FLOOR that runs the scanner against a
    poisoned synthetic module. A scan whose only evidence is "found nothing" is
    equally consistent with "cannot find anything", and this one is now the
    guard for three pages instead of one.
  · Fail-open is tested in the DANGEROUS direction as well as the safe one: an
    unreadable price must render NO price, never a guessed one.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from routes import integrations_landing as L

_MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "routes", "integrations_landing.py")
_PRICED_TIERS = ("developer", "pro")

#: page label -> (module-scope template constant, per-request render function)
_PAGES = {
    "/integrations/mcp": ("_MCP_LANDING_TEMPLATE", "render_mcp_landing"),
    "/integrations/mcp/data-center-mcp-server": ("_MCP_SEO_PAGE_TEMPLATE",
                                                 "render_mcp_seo_page"),
    "/integrations/meta": ("_META_LANDING_TEMPLATE", "render_meta_landing"),
}

_PRICE_RE = re.compile(r"\$([0-9][0-9,]*)/mo")


def _rendered(page: str = "/integrations/mcp") -> str:
    return getattr(L, _PAGES[page][1])()


def _prices_in(html: str) -> set:
    return {int(m.replace(",", "")) for m in _PRICE_RE.findall(html)}


def _registry_prices() -> set:
    from tier_registry import price
    return {int(price(t)) for t in _PRICED_TIERS}


def _paid_prices() -> list:
    from tier_registry import paid_plans, price
    return sorted(p for p in (int(price(t) or 0) for t in paid_plans()) if p > 0)


def test_the_page_renders_exactly_the_prices_the_registry_holds():
    """Set equality, not a banned substring.

    Floor: the registry must yield at least two distinct paid prices, or this
    test could pass over an empty page.
    """
    want = _registry_prices()
    assert len(want) >= 2, want
    got = _prices_in(_rendered())
    assert got == want, (
        "page renders %r, tier_registry holds %r — a retyped price cannot "
        "track the SSOT" % (sorted(got), sorted(want)))


def test_the_sibling_pages_quote_the_lowest_paid_price_the_registry_holds():
    """★ THE TWO PAGES THE FIRST PASS LEFT.

    Both say "paid tiers start at $N" — a claim about the CHEAPEST paid plan,
    so it is asserted as the minimum of the registry's positive prices rather
    than against price('starter'), which is only today's answer to that.
    """
    positives = _paid_prices()
    assert len(positives) >= 2, positives                    # floor
    want = positives[0]
    assert want < min(_registry_prices()), (
        "the entry price %r is not below the tiers the landing page "
        "advertises %r — one of the two is quoting the wrong plan"
        % (want, sorted(_registry_prices())))
    for page in ("/integrations/mcp/data-center-mcp-server", "/integrations/meta"):
        got = _prices_in(_rendered(page))
        assert got == {want}, (
            "%s renders %r, the registry's entry price is %r"
            % (page, sorted(got), want))


def _module_scope_template(name: str, tree: ast.AST | None = None):
    """The module-scope assignment for `name`, as an AST node."""
    tree = tree if tree is not None else ast.parse(open(_MODULE, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node
    return None


def _priced_literals(tree: ast.AST) -> list:
    """Every `$<digits>/mo` inside a non-docstring string constant of `tree`.

    AST, not grep, and docstrings excluded: the block comments on this change
    deliberately quote the $199 and the $9 they remove, and a source-text scan
    would fail on the explanation of the bug — the "comment that quotes the
    drift" trap. Comments are not AST nodes; docstrings are, so they are
    dropped explicitly by identity rather than by pattern.
    """
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    out = []
    for sub in ast.walk(tree):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and id(sub) not in docstrings):
            out += _PRICE_RE.findall(sub.value)
    return out


def test_the_price_scanner_can_actually_find_a_price():
    """★ FLOOR. The scan below reports "no offenders"; this proves it CAN.

    A repo-glob that is green at zero matches is green whether the code is
    clean or the scanner is broken, and this one now stands in front of three
    pages. Poisoned module in, offender out.
    """
    poisoned = ast.parse('_T = ("""<p><b>Pro ($199/mo)</b>: 2,000 calls/day.</p>""")\n')
    assert _priced_literals(poisoned) == ["199"], _priced_literals(poisoned)

    clean = ast.parse('def f():\n    """Renders " from $N/mo"."""\n    return " from $%d/mo" % n\n')
    assert _priced_literals(clean) == [], _priced_literals(clean)


def test_no_price_is_typed_into_any_template_in_the_module():
    """★ WIDENED from the landing template to the WHOLE MODULE.

    The first pass scoped this to _MCP_LANDING_TEMPLATE and named the two
    siblings it did not cover. Both are fixed, so the scope is now every string
    the module holds — which is what stops the next page added here from
    arriving with a fourth hand-typed price.
    """
    tree = ast.parse(open(_MODULE, encoding="utf-8").read())

    missing = [n for n, _ in _PAGES.values() if _module_scope_template(n, tree) is None]
    assert not missing, (
        "template constant(s) %r are gone — this scan is no longer looking at "
        "the pages it is named for" % (missing,))

    offenders = sorted(set(_priced_literals(tree)))
    assert not offenders, (
        "priced literals in routes/integrations_landing.py: %r — derive them "
        "from tier_registry" % offenders)


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_canon_is_resolved_per_request_not_at_import(page):
    """No page's template may be canon_text()'d at module scope.

    That freeze is why /integrations/mcp served a facilities floor the healers
    had already walked past: canon moves between deploys, the module does not.
    """
    template, renderer = _PAGES[page]
    tree = ast.parse(open(_MODULE, encoding="utf-8").read())

    frozen = []
    for node in tree.body:                      # MODULE SCOPE ONLY
        if not isinstance(node, ast.Assign):
            continue
        if template not in [t.id for t in node.targets if isinstance(t, ast.Name)]:
            continue
        for sub in ast.walk(node.value):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "canon_text"):
                frozen.append(node.lineno)
    assert not frozen, (
        "%s is canon_text()'d at import (line %r); resolve it inside %s()"
        % (template, frozen, renderer))

    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == renderer), None)
    assert fn is not None, "%s() is gone" % renderer
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "canon_text" in calls, (
        "%s() does not resolve canon, so nothing does" % renderer)


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_nothing_unresolved_reaches_the_reader(page):
    html = _rendered(page)
    assert len(html) > 10000, "%s is %d bytes — too small to be the page" % (page, len(html))
    assert not re.findall(r"__[A-Z][A-Z0-9_]*__", html), \
        sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", html)))
    assert not re.findall(r"\{canon_[a-z_]+\}", html), \
        sorted(set(re.findall(r"\{canon_[a-z_]+\}", html)))


def test_an_unreadable_price_renders_no_number_not_a_guess():
    """★ THE DANGEROUS DIRECTION. Fail-open must drop the number, not invent one.

    A page that omits a price costs a reader one click. A page that states the
    wrong one is the version a partner quotes back, and is the whole reason
    this test file exists. Covers both derivations: the landing page's tier
    pane and the entry price the two sibling pages quote.
    """
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "tier_registry":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    builtins.__import__ = _boom
    try:
        tokens = L._tier_pane_tokens()
        entry = L._entry_price_token()
    finally:
        builtins.__import__ = real_import

    assert tokens["__PRO_PRICE__"] == "", tokens
    assert tokens["__DEV_PRICE__"] == "", tokens
    assert tokens["__PRO_CALLS__"] == "metered", tokens
    for value in tokens.values():
        assert not re.search(r"\$[0-9]", value), (
            "fail-open emitted a price anyway: %r" % (tokens,))
    assert entry == "", (
        "fail-open emitted an entry price anyway: %r" % (entry,))


def test_the_sibling_sentences_still_read_without_their_price():
    """Fail-open must leave PROSE, not a hole.

    The token carries the whole " from $N/mo" fragment, so dropping it has to
    leave a grammatical sentence rather than "paid tiers  raise the limits".
    Renders the real templates with the token emptied and checks the seam.
    """
    from ai_surface_canon import canon_text
    for template in ("_MCP_SEO_PAGE_TEMPLATE", "_META_LANDING_TEMPLATE"):
        html = canon_text(getattr(L, template)).replace("__PAID_FROM__", "")
        assert "aid tiers raise the limits" in html or "aid plans raise the limits" in html, (
            "%s: the price-free sentence did not survive" % template)
        assert "  raise the limits" not in html, (
            "%s: dropping the price left a double space" % template)
        assert not _PRICE_RE.findall(html), (
            "%s: a price survived the empty token — it is typed, not derived"
            % template)
