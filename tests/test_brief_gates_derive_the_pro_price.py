"""The four brief paywalls must DERIVE the Pro price, never type it.

★ WHAT THIS EXISTS FOR, measured live 2026-09-10 on the same deploy:

    GET /markets/dallas/brief.pdf   402
      "PDF export is a PRO feature ($499/mo). Visit dchub.cloud/pricing…"

    POST /api/v1/deal-desk          402
      "The Deal Desk Brief is a Pro deliverable ($99/mo) …"

`tier_registry.price('pro')` is 99. The figure the brief gates quoted was the
control arm of a Pro pricing A/B (`routes/pricing_ab.py`: "Arm A: $499/mo …
Arm B: $99/mo"). `pricing_ab` itself was repaired in r73 — "Arm A must be the
CANONICAL Pro price — it was 499, which made the live A/B '$499 vs $99' and
showed ~half of visitors a 2.5x price" — and then `r-price-collapse`
(2026-09-05) settled Pro at 99. These four modules never consulted the A/B at
all, so their literal survived both changes and went on telling every gated
caller the product cost five times what it does.

★ IT IS ONLY EVER READ BY SOMEONE WHO HAS NOT PAID. That is why it lasted: a
price in an upgrade gate is invisible to everyone who could notice it is
wrong. The same argument the sibling guard makes about a CORRECT literal
applies with more force here — a correct literal is a wrong literal that has
not been repriced yet.

Deliberately shaped like tests/test_integrations_mcp_derives_price_and_canon.py
(#4334), which fixed this defect class on /integrations/*:

  · AST, not grep, and docstrings dropped by identity. The comments on this
    change quote the figure they remove, and a source-text scan would fail on
    the explanation of the bug — the "comment that quotes the drift" trap.
    Comments are not AST nodes; docstrings are, so they go explicitly.
  · The scan carries a FLOOR. A scan whose only evidence is "found nothing" is
    equally consistent with "cannot find anything", and this one stands in
    front of four modules.
  · Fail-open is tested in the DANGEROUS direction too: a tier with no list
    price must render NO price rather than a guessed one. "$0/mo" on an
    enterprise gate would read as free.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from tier_registry import TIER_PRICE_USD_MONTH, price, price_display

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: every module that renders a paywall quoting the Pro price
_GATES = (
    "routes/market_brief.py",
    "routes/hyperscaler_brief.py",
    "routes/operator_brief.py",
    "routes/partner_landing.py",
)

_PRICE_RE = re.compile(r"\$([0-9][0-9,]*)/mo")


def _priced_literals(tree: ast.AST) -> list:
    """Every `$<digits>/mo` inside a non-docstring string constant of `tree`."""
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    out = []
    for sub in ast.walk(tree):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and id(sub) not in docstrings):
            out += _PRICE_RE.findall(sub.value)
    return out


# ── the floor ───────────────────────────────────────────────────────────────

def test_the_price_scanner_can_actually_find_a_price():
    """★ FLOOR. The scan below reports "no offenders"; this proves it CAN.

    Green at zero matches is green whether the modules are clean or the
    scanner is broken. Poisoned module in, offender out.
    """
    poisoned = ast.parse('_T = ("""<a>Unlock with PRO · $%d/mo</a>""")\n'
                         % (price("pro") + 400))
    assert _priced_literals(poisoned) == [str(price("pro") + 400)]

    # …and it must NOT fire on a docstring, a comparison figure in another
    # unit, or a format string that derives its number at runtime.
    clean = ast.parse(
        'def f():\n'
        '    """Renders " from $N/mo"."""\n'
        '    x = "$100K+/yr for similar coverage"\n'
        '    return " from $%d/mo" % n\n')
    assert _priced_literals(clean) == [], _priced_literals(clean)


# ── the gates ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", _GATES)
def test_no_pro_price_is_typed_into_the_gate_module(rel):
    path = os.path.join(_ROOT, rel)
    assert os.path.exists(path), f"{rel} moved — this scan is looking at nothing"
    tree = ast.parse(open(path, encoding="utf-8").read())
    offenders = sorted(set(_priced_literals(tree)))
    assert not offenders, (
        f"priced literal(s) {offenders!r} typed into {rel} — derive them with "
        "tier_registry.price_display('pro')")


@pytest.mark.parametrize("rel", _GATES)
def test_the_gate_module_actually_imports_the_deriver(rel):
    """The ban alone is satisfied by a module that quotes no price at all.

    A gate that silently stopped naming a price would pass the scan above and
    be a worse page. This pins that each module still reaches the registry.
    """
    src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    imports_it = any(
        isinstance(n, ast.ImportFrom) and n.module == "tier_registry"
        and any(a.name == "price_display" for a in n.names)
        for n in ast.walk(tree))
    assert imports_it, f"{rel} no longer imports price_display from tier_registry"
    assert "price_display(" in src, f"{rel} imports the deriver but never calls it"


# ── the formatter ───────────────────────────────────────────────────────────

def test_the_displayed_price_is_the_registry_price():
    assert price_display("pro") == f"${price('pro')}/mo"
    assert price_display("developer") == f"${price('developer')}/mo"


def test_a_tier_with_no_list_price_renders_NO_price_not_a_zero():
    """★ THE DANGEROUS DIRECTION. Enterprise is contact-sales; `None` formatted
    naively is "$0/mo", which reads as free on the one page where being wrong
    costs the most."""
    assert TIER_PRICE_USD_MONTH.get("enterprise") is None, (
        "enterprise gained a list price — re-check what these gates render")
    assert price_display("enterprise") == ""
    assert price_display("nonsense-tier") == ""


def test_the_partner_landing_token_resolves_to_the_registry_price():
    """partner_landing carries copy as @@TOKEN@@ and fills it per request. The
    price rides that same mechanism, so this proves the token is wired, not
    merely spelled."""
    from routes.partner_landing import _resolve_canon
    out = _resolve_canon("PRO+ (@@PRO_PRICE@@) lifts limits")
    assert price_display("pro") in out, out
    assert "@@PRO_PRICE@@" not in out, "token left unresolved in rendered copy"
