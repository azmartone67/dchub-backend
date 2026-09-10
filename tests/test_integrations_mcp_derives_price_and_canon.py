"""/integrations/mcp — the third page to ship BOTH of the same two bugs.

Measured live 2026-09-10, cf-cache-status MISS on the current worker:

    Pro ($199/mo)      the button beside it charges $99, and tier_registry has
                       said 99 since r-price-collapse (2026-09-05)
    "20,700+"  x6      canon's facilities floor is walked by healers between
                       deploys; this page froze it at import

#4320 fixed the same pair on /connect/*, and routes/agent_concierge.py had the
canon half on /agent. This module had both and the sweep missed it.

WHY EACH CHECK IS SHAPED THE WAY IT IS
  · The price assertion compares the SET of "$N/mo" figures the page renders
    against the set tier_registry produces. A bare `"$199" not in html` is the
    substring trap that has bitten this repo before — a banned literal that is
    a suffix of a bigger legitimate number passes while the page is wrong, and
    a legitimate number that CONTAINS the banned one fails while it is right.
  · The per-request check walks the AST. A grep for `canon_text` stays green
    when the call moves back to module scope, because the per-request call site
    mentions it too.
  · Fail-open is tested in the DANGEROUS direction as well as the safe one: an
    unreadable price must render NO parenthetical, never a guessed one.
"""
from __future__ import annotations

import ast
import os
import re

from routes import integrations_landing as L

_MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "routes", "integrations_landing.py")
_PRICED_TIERS = ("developer", "pro")


def _rendered() -> str:
    return L.render_mcp_landing()


def _registry_prices() -> set:
    from tier_registry import price
    return {int(price(t)) for t in _PRICED_TIERS}


def test_the_page_renders_exactly_the_prices_the_registry_holds():
    """Set equality, not a banned substring.

    Floor: the registry must yield at least two distinct paid prices, or this
    test could pass over an empty page.
    """
    want = _registry_prices()
    assert len(want) >= 2, want
    got = {int(m) for m in re.findall(r"\$([0-9][0-9,]*)/mo", _rendered())}
    got = {int(str(g).replace(",", "")) for g in got}
    assert got == want, (
        "page renders %r, tier_registry holds %r — a retyped price cannot "
        "track the SSOT" % (sorted(got), sorted(want)))


def _module_scope_template(name: str):
    """The module-scope assignment for `name`, as an AST node."""
    tree = ast.parse(open(_MODULE, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node
    return None


def test_no_price_is_typed_into_the_landing_template():
    """No `$<digits>/mo` literal inside the template this page renders.

    AST, not grep, and scoped to the assignment rather than the file: the block
    comment on this change deliberately quotes the $199 it removes, and a
    source-text scan would fail on the explanation of the bug — the "comment
    that quotes the drift" trap.
    """
    node = _module_scope_template("_MCP_LANDING_TEMPLATE")
    assert node is not None, "_MCP_LANDING_TEMPLATE is gone"
    offenders = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            for m in re.finditer(r"\$[0-9][0-9,]*/mo", sub.value):
                offenders.append(m.group(0))
    assert not offenders, (
        "priced literals in the landing template: %r — derive from "
        "tier_registry" % offenders)


def test_the_uncovered_siblings_are_named_not_forgotten():
    """★ THIS MODULE HAS THREE canon_text()-AT-IMPORT TEMPLATES. ONE IS FIXED.

    MCP_SEO_PAGE_HTML and META_LANDING_HTML carry the identical shape — frozen
    canon plus a retyped `$9/mo` Starter price. That price is CURRENTLY CORRECT
    (tier_registry says 9), which is exactly why it is easy to leave and easy
    to forget, and it is the same setup the $199 had before r-price-collapse.
    They are out of this change's scope, and this test publishes that rather
    than letting the narrower guard read as full coverage.

    It FAILS when a sibling is fixed or removed, so the exclusion cannot
    outlive the thing it excuses.
    """
    src = open(_MODULE, encoding="utf-8").read()
    tree = ast.parse(src)
    still_frozen = []
    for name in ("MCP_SEO_PAGE_HTML", "META_LANDING_HTML"):
        node = _module_scope_template(name)
        if node is None:
            continue
        if any(isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
               and sub.func.id == "canon_text" for sub in ast.walk(node)):
            still_frozen.append(name)
    assert still_frozen == ["MCP_SEO_PAGE_HTML", "META_LANDING_HTML"], (
        "the known-uncovered siblings changed (%r). If they were fixed, widen "
        "test_no_price_is_typed_into_the_landing_template to the whole module "
        "and delete this test; if they were renamed, follow them."
        % (still_frozen,))


def test_canon_is_resolved_per_request_not_at_import():
    """The landing template must NOT be canon_text()'d at module scope.

    That freeze is why the page served a facilities floor the healers had
    already walked past: canon moves between deploys, the module does not.
    """
    tree = ast.parse(open(_MODULE, encoding="utf-8").read())
    frozen = []
    for node in tree.body:                      # MODULE SCOPE ONLY
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_MCP_LANDING_TEMPLATE" not in targets:
            continue
        for sub in ast.walk(node.value):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "canon_text"):
                frozen.append(node.lineno)
    assert not frozen, (
        "_MCP_LANDING_TEMPLATE is canon_text()'d at import (line %r); resolve "
        "it inside render_mcp_landing()" % frozen)

    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "render_mcp_landing"),
              None)
    assert fn is not None, "render_mcp_landing() is gone"
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "canon_text" in calls, (
        "render_mcp_landing() does not resolve canon, so nothing does")


def test_nothing_unresolved_reaches_the_reader():
    html = _rendered()
    assert len(html) > 20000, "page is %d bytes — too small to be the page" % len(html)
    assert not re.findall(r"__[A-Z][A-Z0-9_]*__", html), \
        sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", html)))
    assert not re.findall(r"\{canon_[a-z_]+\}", html), \
        sorted(set(re.findall(r"\{canon_[a-z_]+\}", html)))


def test_an_unreadable_price_renders_no_parenthetical_not_a_guess():
    """★ THE DANGEROUS DIRECTION. Fail-open must drop the number, not invent one.

    A page that omits a price costs a reader one click. A page that states the
    wrong one is the version a partner quotes back, and is the whole reason
    this test file exists.
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
    finally:
        builtins.__import__ = real_import

    assert tokens["__PRO_PRICE__"] == "", tokens
    assert tokens["__DEV_PRICE__"] == "", tokens
    assert tokens["__PRO_CALLS__"] == "metered", tokens
    for value in tokens.values():
        assert not re.search(r"\$[0-9]", value), (
            "fail-open emitted a price anyway: %r" % (tokens,))
