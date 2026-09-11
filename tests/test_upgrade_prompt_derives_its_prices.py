"""/api/v1/mcp/upgrade-prompt must DERIVE every price it relays — never type one.

★ MEASURED LIVE 2026-09-10 23:31Z, anonymous, cache-busted (cf-cache-status MISS):

    GET /api/v1/mcp/upgrade-prompt?tool=compare_sites                      200
      pricing.pro_monthly             "$299/month"
      pricing.savings_annual          "$1,200/year (50% off vs monthly)"
      founding_member_offer.discount  "$99/mo billed annually ($1,188/year vs
                                       $299/mo monthly)"
      agent_friendly_message          "... Annual pricing: $99/mo billed annually
                                       (saves $1,200/year vs monthly) ..."

tier_registry.price('pro') is 99. r-price-collapse (2026-09-05) withdrew Pro
Annual from ANNUAL_OPTIONS because at a $99 list the $1,188 annual link is
12 x 99 exactly — a zero saving — and the same day /pricing retired the
founding-member spotlight ("$99 was never a discount"). None of it reached this
handler, because it typed its figures. Same defect class as #4362's brief
paywalls: copy that only a REFUSED caller reads has no reviewers.

WHY EACH CHECK IS SHAPED THE WAY IT IS
  · The handler is EXECUTED, not grepped. Its function is compiled out of
    main.py's own source and called in a Flask request context, so the
    assertions read the JSON an agent actually receives. main.py cannot be
    imported in a unit test (it needs a database), and a hand-copied handler
    would be a mirror that stays green through its own regression.
  · The typed-literal ban walks the AST with docstrings dropped by identity —
    the comments on this change quote what they remove — and carries a FLOOR
    proving the scanner can find a typed price at all. Its poisoned probe is
    built at runtime, so this file never types the retired figure into code.
  · Ban AND deriver: a handler that quoted no price at all would pass the ban
    and be a worse body, so the registry calls are pinned too.
  · DERIVATION IN BOTH DIRECTIONS, not absence: no annual while it saves
    nothing, and an annual that returns by itself the day ANNUAL_OPTIONS holds a
    real one (the $990 restore path tier_registry documents). A guard asserting
    "no annual" forever would fight that path.
  · The dangerous direction: a Pro with no list price relays NO price.
"""
from __future__ import annotations

import ast
import copy
import functools
import json
import os
import re

from flask import Flask

import tier_registry
from tier_registry import annual_offer, price, price_display

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_ROOT, "main.py")
_HANDLER = "_mcp_upgrade_prompt"
_CONTRACT = os.path.join(_ROOT, "contracts", "api_response_surface.json")
_CONTRACT_KEY = "GET /api/v1/mcp/upgrade-prompt"

#: a displayed price with a period — "$99/mo", "$1,188/year", "$9 / month"
_TYPED_PRICE = re.compile(r"\$\s?[0-9][0-9,]*\s*/\s*(?:mo|month|yr|year)\b")
#: a saving stated as a fixed figure — "50% off", "saves $1,200"
_TYPED_SAVING = re.compile(r"\b[0-9]{1,3}% off\b|\bsaves? \$[0-9]")


@functools.lru_cache(maxsize=1)
def _main_tree():
    with open(_MAIN, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=_MAIN)


def _handler_node():
    fn = next((n for n in _main_tree().body
               if isinstance(n, ast.FunctionDef) and n.name == _HANDLER), None)
    assert fn is not None, (
        f"main.py no longer defines {_HANDLER}() at module scope — every check "
        "in this file would be looking at nothing")
    return fn


def _typed(tree):
    """Typed prices and savings in non-docstring string constants of `tree`."""
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    out = []
    for sub in ast.walk(tree):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and id(sub) not in docstrings):
            out += _TYPED_PRICE.findall(sub.value) + _TYPED_SAVING.findall(sub.value)
    return out


def _served(tool="compare_sites"):
    """The JSON the REAL handler returns — compiled from main.py's source."""
    fn = copy.deepcopy(_handler_node())
    fn.decorator_list = []                      # @app.route needs the app
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {"PAYWALL_PREVIEWS": {}}               # the one module global it reads
    exec(compile(module, _MAIN, "exec"), ns)
    app = Flask(__name__)
    with app.test_request_context("/api/v1/mcp/upgrade-prompt",
                                  query_string={"tool": tool}):
        return ns[_HANDLER]().get_json()


def _dollar_figures(body):
    return {int(m.replace(",", ""))
            for m in re.findall(r"\$([0-9][0-9,]*)", json.dumps(body))}


# ── the floor ───────────────────────────────────────────────────────────────

def test_the_scanner_can_find_a_typed_price_and_a_typed_saving():
    """★ FLOOR. The ban below reports "no offenders"; this proves it CAN."""
    retired = int(price("pro")) + 200
    saving = f"{12 * 100:,}"
    poisoned = ast.parse(
        "def h():\n"
        f'    """The docstring may say ${retired}/month."""\n'
        f'    return {{"pro_monthly": "${retired}/month",\n'
        f'            "m": "saves ${saving}/year (50% off vs monthly)"}}\n')
    found = _typed(poisoned)
    assert f"${retired}/month" in found, found
    assert f"${saving}/year" in found and "50% off" in found, found
    assert len([f for f in found if f == f"${retired}/month"]) == 1, (
        "the docstring was scanned — its copy of the figure must be dropped")

    clean = ast.parse('def h(p):\n    """$99/mo in prose."""\n    return f"Pro is {p}."\n')
    assert _typed(clean) == [], _typed(clean)


def test_the_handler_scan_reaches_real_strings():
    """FLOOR for the scope: an AST walk that reached nothing is also clean."""
    consts = [n for n in ast.walk(_handler_node())
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert len(consts) >= 15, f"{_HANDLER} yielded {len(consts)} string constants"


# ── the ban, and the deriver it must not be satisfied without ──────────────

def test_the_handler_types_no_price_and_no_saving():
    offenders = _typed(_handler_node())
    assert not offenders, (
        f"{_HANDLER}() types {offenders!r}. This body is read only by an agent "
        "whose caller was just refused — a figure typed here outlives every "
        "reprice unseen. Derive it from tier_registry.")


def test_the_handler_reaches_the_registry():
    fn = _handler_node()
    imported = {a.name for n in ast.walk(fn)
                if isinstance(n, ast.ImportFrom) and n.module == "tier_registry"
                for a in n.names}
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    need = {"price_display", "annual_offer"}
    assert need <= imported, f"not imported from tier_registry: {need - imported}"
    assert need <= called, f"imported but never called: {need - called}"


# ── what an agent actually receives ────────────────────────────────────────

def test_the_relayed_monthly_price_is_the_registry_price():
    assert price("pro"), "Pro lost its list price — see the fail-open test below"
    body = _served()
    assert body["pricing"]["pro_monthly"] == price_display("pro", "/month")
    assert _dollar_figures(body) == {int(price("pro"))}, (
        f"the body relays {sorted(_dollar_figures(body))}; the only price it may "
        f"name today is Pro's ${price('pro')}")


def test_a_zero_saving_annual_is_not_advertised(monkeypatch):
    """The SKU that exists: an annual at exactly 12x the monthly list."""
    monkeypatch.setitem(tier_registry.ANNUAL_OPTIONS, "pro",
                        {"annual_usd_year": 12 * int(price("pro"))})
    assert annual_offer("pro") is None
    body = _served()
    assert body["pricing"]["pro_annual"] is None
    assert body["pricing"]["savings_annual"] is None
    assert "annual" not in body["agent_friendly_message"].lower(), (
        body["agent_friendly_message"])


def test_the_live_registry_is_what_decides_the_annual_fields():
    """No monkeypatch: whatever ANNUAL_OPTIONS says today is what is relayed."""
    offer = annual_offer("pro")
    body = _served()
    if offer is None:
        assert body["pricing"]["pro_annual"] is None
        assert body["pricing"]["savings_annual"] is None
    else:
        assert body["pricing"]["pro_annual"].startswith(offer["display"])
        assert body["pricing"]["savings_annual"] == offer["saving_display"]


def test_a_real_annual_comes_back_on_its_own(monkeypatch):
    """The restore path tier_registry documents: a $990/yr annual on a $99
    list. 17% is the same badge routes/mcp_connect.py computes for $990 — the
    two annual predicates must agree on the one case both can express."""
    monkeypatch.setitem(tier_registry.TIER_PRICE_USD_MONTH, "pro", 99)
    monkeypatch.setitem(tier_registry.ANNUAL_OPTIONS, "pro", {"annual_usd_year": 990})
    assert annual_offer("pro") == {
        "usd_year": 990, "display": "$990/year",
        "saved_usd_year": 198, "saving_display": "$198/year (17% off vs monthly)",
    }
    body = _served()
    assert body["pricing"]["pro_annual"] == "$990/year, billed annually"
    assert body["pricing"]["savings_annual"] == "$198/year (17% off vs monthly)"
    assert "$990/year" in body["agent_friendly_message"]


def test_an_annual_that_costs_more_or_is_unpriced_is_not_an_offer(monkeypatch):
    # the withdrawn promo SKU cost 51% MORE than paying monthly
    monkeypatch.setitem(tier_registry.ANNUAL_OPTIONS, "pro",
                        {"annual_usd_year": 12 * int(price("pro")) * 3 // 2})
    assert annual_offer("pro") is None
    assert annual_offer("enterprise") is None       # contact-sales annual: no price
    assert annual_offer("nonsense-tier") is None


def test_a_pro_with_no_list_price_relays_no_price(monkeypatch):
    """★ THE DANGEROUS DIRECTION. Say nothing before saying a wrong price."""
    monkeypatch.setitem(tier_registry.TIER_PRICE_USD_MONTH, "pro", None)
    body = _served()
    assert body["pricing"]["pro_monthly"] is None
    assert body["pricing"]["pro_annual"] is None
    assert not re.search(r"\$[0-9]", json.dumps(body)), body


def test_no_retired_founding_discount_is_relayed():
    """/pricing retired the founding spotlight on 2026-09-05: Pro is $99 and
    founding was never cheaper. If a founding offer is ever relaunched with a
    REAL discount, derive it in tier_registry and change this in the same
    commit."""
    body = _served()
    offer = body["founding_member_offer"]
    assert offer["active"] is False and offer["discount"] is None, offer
    blob = json.dumps(body).lower()
    assert "founding-member discount" not in blob, "the retired offer is still sold"


# ── the published shape ─────────────────────────────────────────────────────

def _contract_keys():
    with open(_CONTRACT, encoding="utf-8") as fh:
        stack = [json.load(fh)]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get(_CONTRACT_KEY), dict):
                return cur[_CONTRACT_KEY].get("keys") or []
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return []


def test_every_contracted_key_is_still_served():
    """Values moved to honest ones; the keys an outside agent may parse did not."""
    keys = _contract_keys()
    assert len(keys) >= 15, f"{_CONTRACT_KEY} not found in the contract ({len(keys)} keys)"
    body = _served()
    missing = []
    for dotted in keys:
        cur = body
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                missing.append(dotted)
                break
            cur = cur[part]
    assert not missing, f"contracted keys no longer served: {missing}"


def test_the_upgrade_link_does_not_point_at_a_withdrawn_anchor():
    url = _served()["upgrade_url"]
    assert url.startswith("https://dchub.cloud/pricing?"), url
    assert url.endswith("#pro"), url
