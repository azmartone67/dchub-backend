"""An endpoint url_map does not serve must not be published as contract.

#4152 taught the extractor which of several candidate handlers actually serves a
route, fixing keys that were unioned in from a non-serving file. It deliberately
only ever NARROWS a union — and `len(recs) > 1` means an endpoint with a single
candidate is never examined. Endpoints defined ONLY by files nothing registers
therefore survived intact: 118 of them, 442 keys, including `GET /api/ai-deals`
and `GET /api/companies` from the stale duplicates ai_deals_api.py,
transactions_news_api.py and deal_scraper.py. Nothing has ever served those
paths, so a consumer reading the contract to learn their response shape is
coding against a response that cannot occur.

These tests pin the drop and — more importantly — the refusal to drop.

★ The dangerous failure is not a missed phantom, it is an endpoint-id format
divergence between the surface and the serving map: every endpoint would look
unserved, the whole contract would empty out, and `check` would go green because
there was nothing left to compare. `_unserved_endpoints` refuses to act once the
unserved share passes `_UNSERVED_CEILING`, and the third test is what proves
that refusal works rather than merely being written down.

No boot here on purpose. The map's fidelity to the running app is already
app_contract_gate.py's job, verified there on every run; duplicating a boot in
this suite would cost ~12s to re-answer a question that is answered upstream.
"""
from __future__ import annotations

import importlib.util
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXTRACTOR = os.path.join(_ROOT, "scripts", "api_response_contract.py")
_SURFACE = os.path.join(_ROOT, "contracts", "api_response_surface.json")
_MAP = os.path.join(_ROOT, "contracts", "route_serving_map.json")


def _arc():
    spec = importlib.util.spec_from_file_location("arc", _EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending(*eids):
    return {e: [{"_module": "m", "source": "m.py:1"}] for e in eids}


def test_endpoints_absent_from_url_map_are_dropped():
    """Sized realistically on purpose: with only a handful of endpoints a single
    phantom is >25% of them and trips the divergence ceiling, which is correct
    behaviour and would make a toy fixture read as a bug in the code."""
    arc = _arc()
    eids = [f"GET /r{i}" for i in range(100)]
    pending = _pending(*eids)
    serving = {e: {"modules": ["m"]} for e in eids[:98]}   # 2 unserved = 2%
    assert arc._unserved_endpoints(pending, serving) == frozenset(eids[98:])


def test_a_missing_map_drops_nothing():
    """"Cannot measure" must never be read as "nothing serves it"."""
    arc = _arc()
    assert arc._unserved_endpoints(_pending("GET /a", "GET /b"), None) == frozenset()


def test_an_implausible_unserved_share_drops_nothing():
    """★ The format-divergence guard, exercised rather than asserted.

    If endpoint ids ever stopped matching between the two artifacts, every
    endpoint would look unserved. Emptying the contract must not be reachable
    by that route — a guard with nothing left to check reports PASS.
    """
    arc = _arc()
    pending = _pending(*[f"GET /r{i}" for i in range(100)])
    # A map keyed in some other shape: nothing matches.
    serving = {f"GET|/r{i}": {"modules": ["m"]} for i in range(100)}
    assert arc._unserved_endpoints(pending, serving) == frozenset()

    # And the ceiling is a ceiling, not an off switch: just under it, it acts.
    under = int(arc._UNSERVED_CEILING * 100) - 1
    serving_ok = {f"GET /r{i}": {"modules": ["m"]} for i in range(under, 100)}
    assert len(arc._unserved_endpoints(pending, serving_ok)) == under


def test_the_shipped_surface_contains_no_unserved_endpoint():
    """The artifact-level invariant, on what is actually committed."""
    with open(_SURFACE, encoding="utf-8") as fh:
        surface = json.load(fh)
    with open(_MAP, encoding="utf-8") as fh:
        serving = json.load(fh)["serving"]

    # Floor: a comparison between two collections, one of which is empty,
    # proves nothing.
    assert len(surface["endpoints"]) > 500, "surface implausibly small — unmeasured"
    assert len(serving) > 500, "serving map implausibly small — unmeasured"

    unserved = sorted(e for e in surface["endpoints"] if e not in serving)
    assert not unserved, (
        f"{len(unserved)} endpoint(s) are published as contract but nothing in "
        "url_map serves them:\n  " + "\n  ".join(unserved[:20])
        + "\n\nRegenerate: python3 scripts/api_response_contract.py baseline"
    )
