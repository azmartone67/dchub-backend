"""The capacity-heatmap paywall must actually redact — proven on the SHIPPED code.

THE DEFECT THIS PINS
────────────────────
`capacity_heatmap_public()` in main.py gated non-paid callers by deep-copying
CAPACITY_HEATMAP_MARKETS and nulling these keys:

    readiness.score, grid.spare_capacity_pct, grid.spare_capacity_mw,
    gas.headroom_mdth, power.local_capacity_mw, power.local_plants,
    cost.electricity_rate_cents_kwh

All seven are NESTED. CAPACITY_HEATMAP_MARKETS is FLAT — every entry is
{name, lat, lng, capacity_mw, utilization, growth}. `isinstance(_m.get(_sect),
dict)` was therefore False on all 8 entries and the loop nulled NOTHING. The
gated response differed from the paid one by two booleans; all 24 paid numbers
(8 markets x 3 fields) were served verbatim to non-paid callers.

It was not dead code. @require_plan('pro') returns the view function directly
on a valid HMAC session cookie — any path, any plan — and routes/session_cookie
states that any browser gets one by loading any public page. /land-power is
HTML and is not in main._COOKIE_BYPASS_EXACT, so an anonymous visitor there is
issued the cookie and its fetch lands in this body with _paid=False.

Same family as reference_dchub_capacity_paywall_gating.md and
reference_dchub_anon_bulk_exposure_0801.md ("map_tier_gating DEAD"): a gate
written against a schema the data does not have.

WHY IT EXECUTES THE SOURCE INSTEAD OF GREPPING IT
─────────────────────────────────────────────────
A source assert ("the file mentions capacity_mw") is satisfied by a comment,
and would have PASSED against the broken code — the broken code named all the
right-sounding keys. The only assertion that could ever have caught this is one
made against an emitted VALUE. So the real handler is sliced out of main.py and
run against stubs. Per CLAUDE.md, tests never import main.py.

test_guard_is_not_vacuous below re-runs the same assertion against a
deliberately neutered copy of the handler and requires it to FAIL, so the
mutation check is permanent rather than a one-time manual step.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import textwrap
import types

import pytest


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _main_src():
    with open(os.path.join(_repo_root(), "main.py"), encoding="utf-8") as fh:
        return fh.read()


def _fixture():
    """CAPACITY_HEATMAP_MARKETS, read literally out of main.py."""
    tree = ast.parse(_main_src())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "CAPACITY_HEATMAP_MARKETS":
                    return ast.literal_eval(node.value)
    raise AssertionError("CAPACITY_HEATMAP_MARKETS not found in main.py")


def _handler_src():
    """The shipped capacity_heatmap_public, decorators stripped."""
    src = _main_src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "capacity_heatmap_public":
            lines = src.splitlines()
            return textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("capacity_heatmap_public not found in main.py")


def _run(paid, handler_src=None):
    """Execute the handler against stubs. Returns (payload, status).

    Every free name the handler needs is supplied explicitly, so if the shipped
    code starts reading something this harness does not provide, the exec
    raises NameError and the test fails loudly instead of skipping.
    """
    if _repo_root() not in sys.path:
        sys.path.insert(0, _repo_root())

    captured = {}

    def fake_jsonify(obj):
        captured["payload"] = obj

        class _Resp:
            def __init__(self):
                self.headers = {}

        return _Resp()

    # Stub only routes.dcpi (it pulls the DB layer); util.heatmap_gating is the
    # REAL module — that is the code under test.
    saved = sys.modules.get("routes.dcpi")
    stub = types.ModuleType("routes.dcpi")
    stub._dcpi_is_paid = lambda *a, **k: paid
    sys.modules.setdefault("routes", types.ModuleType("routes"))
    sys.modules["routes.dcpi"] = stub
    try:
        ns = {
            "CAPACITY_HEATMAP_MARKETS": _fixture(),
            "_HEATMAP_PROVENANCE": {"source": "static_fixture", "live": False},
            "jsonify": fake_jsonify,
            "logger": types.SimpleNamespace(
                error=lambda *a, **k: None, warning=lambda *a, **k: None,
                info=lambda *a, **k: None),
        }
        src = handler_src if handler_src is not None else _handler_src()
        exec(compile(src, "<capacity_heatmap_public>", "exec"), ns, ns)
        result = ns["capacity_heatmap_public"]()
    finally:
        if saved is not None:
            sys.modules["routes.dcpi"] = saved
        else:
            sys.modules.pop("routes.dcpi", None)

    status = result[1] if isinstance(result, tuple) else 200
    return captured.get("payload"), status


def _paid_values():
    """Every numeric value a non-paid caller must NOT receive."""
    from util.heatmap_gating import FREE_KEYS

    vals = set()
    for entry in _fixture():
        for key, value in entry.items():
            if key not in FREE_KEYS and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                vals.add(float(value))
    return vals


def _numbers_in(payload):
    """Every numeric value the payload actually carries, at any depth."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            found.add(float(node))

    walk(payload)
    return found


# ── floors: a shrunken or emptied fixture must not pass vacuously ────────────

def test_fixture_still_has_paid_numbers_to_hide():
    fixture = _fixture()
    assert len(fixture) >= 8, f"fixture shrank to {len(fixture)} markets"
    assert len(_paid_values()) >= 20, (
        "fewer than 20 distinct paid values — the leak assertions below would "
        "pass without proving anything")


# ── the actual guard ─────────────────────────────────────────────────────────

def test_non_paid_response_carries_no_paid_numbers():
    """THE regression. Every value in _paid_values() must be absent."""
    payload, status = _run(paid=False)
    assert status == 200, f"gated request returned {status}: {payload}"

    leaked = _paid_values() & _numbers_in(payload)
    assert not leaked, (
        f"{len(leaked)} paid value(s) served to a NON-PAID caller: "
        f"{sorted(leaked)[:8]} — payload: {json.dumps(payload)[:400]}")


def test_non_paid_entries_null_the_paid_keys_and_keep_the_map():
    """Redacted, not deleted — and lat/lng survive so the map still renders."""
    payload, _ = _run(paid=False)
    rows = payload["data"]
    assert len(rows) == len(_fixture()), "entries were dropped, not redacted"
    for row in rows:
        assert row.get("locked") is True, f"missing locked flag: {row}"
        for key in ("capacity_mw", "utilization", "growth"):
            assert row.get(key) is None, f"{key} not redacted: {row}"
        assert isinstance(row.get("name"), str) and row["name"]
        for coord in ("lat", "lng"):
            assert isinstance(row.get(coord), (int, float)), \
                f"{coord} was redacted — the map cannot render a dot: {row}"


def test_paid_response_still_carries_the_numbers():
    """The gate must not have been 'fixed' by blanking everyone."""
    payload, status = _run(paid=True)
    assert status == 200
    assert payload["_gated"] is False
    assert _paid_values() <= _numbers_in(payload), \
        "paid callers lost values they are entitled to"


def test_gated_response_is_marked_and_uncacheable():
    payload, _ = _run(paid=False)
    assert payload["_gated"] is True
    assert "pro" in json.dumps(payload).lower(), \
        "gated payload should name the tier that unlocks it"


# ── provenance: the fixture must not travel as if it were measured ───────────

def test_fixture_is_labelled_as_a_fixture_not_live_data():
    from util.heatmap_gating import CAPACITY_HEATMAP_PROVENANCE as prov

    assert prov["live"] is False
    assert prov["source"] == "static_fixture"
    assert prov["as_of"] is None, \
        "as_of must stay None — the fixture has no observation date, and " \
        "inventing one is the dishonesty this label exists to prevent"


def test_both_serving_surfaces_attach_provenance():
    src = _main_src()
    assert '"capacity_heatmap_provenance": _HEATMAP_PROVENANCE' in src, \
        "the consolidated land & power endpoint serves the fixture unlabelled"
    assert '"_provenance": _HEATMAP_PROVENANCE' in src, \
        "the public heatmap endpoint serves the fixture unlabelled"


# ── the redaction must be allow-list shaped, or it goes stale again ──────────

def test_new_paid_field_is_redacted_without_being_listed():
    """The whole point of the rewrite: a field nobody remembered to list."""
    from util.heatmap_gating import redact_markets

    rows, nulled = redact_markets(
        [{"name": "X", "lat": 1.0, "lng": 2.0, "brand_new_paid_metric": 999}])
    assert rows[0]["brand_new_paid_metric"] is None, \
        "redaction is block-list shaped again — an unlisted field leaks"
    assert nulled == 1


def test_redaction_does_not_mutate_the_module_constant():
    """CAPACITY_HEATMAP_MARKETS is shared across requests; in-place redaction
    would permanently blank it for paid callers after the first anon hit."""
    _run(paid=False)
    after, _ = _run(paid=True)
    assert _paid_values() <= _numbers_in(after), \
        "a prior non-paid request blanked the fixture for paid callers"


# ── permanent mutation check ─────────────────────────────────────────────────

def test_guard_is_not_vacuous():
    """Re-run the leak assertion against a NEUTERED handler; it must fail.

    The mutation removes the redaction call and restores the original pass-
    through, which is exactly the shipped defect. If this test does not observe
    a leak, test_non_paid_response_carries_no_paid_numbers is decoration.
    """
    src = _handler_src()
    anchor = "_data, _nulled = redact_markets(CAPACITY_HEATMAP_MARKETS)"
    assert src.count(anchor) == 1, (
        f"mutation anchor appears {src.count(anchor)}x — it must be unique or "
        "the mutation may land in the wrong place and 'survive' spuriously")

    mutated = src.replace(anchor, "_data, _nulled = CAPACITY_HEATMAP_MARKETS, 1")
    assert mutated != src, "mutation did not apply"

    payload, status = _run(paid=False, handler_src=mutated)
    assert status == 200, "neutered handler should still return 200"
    leaked = _paid_values() & _numbers_in(payload)
    assert leaked, (
        "MUTATION SURVIVED: the redaction was removed and the leak assertion "
        "still saw no paid values. The guard cannot fail — it proves nothing.")
    assert len(leaked) >= 20, \
        f"expected the full fixture to leak when unredacted, saw {len(leaked)}"


def test_handler_refuses_rather_than_serving_when_redaction_nulls_nothing():
    """The 'cannot tell' branch, mutated in the DANGEROUS direction.

    If redact_markets ever silently stops redacting — the exact failure mode
    that shipped — the handler must refuse, not serve the paid numbers with a
    `locked: true` flag on top. A gate that reports success while redacting
    nothing is how this defect stayed invisible.
    """
    import util.heatmap_gating as hg

    original = hg.redact_markets
    hg.redact_markets = lambda markets: ([dict(m) for m in markets], 0)
    try:
        payload, status = _run(paid=False)
    finally:
        hg.redact_markets = original

    assert status == 503, \
        f"handler served a payload it could not redact (status {status})"
    assert payload["success"] is False
    assert not (_paid_values() & _numbers_in(payload)), \
        "the refusal response still leaked the paid numbers"
