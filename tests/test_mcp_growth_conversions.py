"""Guard for /api/v1/mcp/growth's conversion keys — routes/mcp_growth.py
(2026-09-02).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

Measured live 2026-09-02 00:23Z:

    conversions_7d 190      conversion_ratio "1:3"

and, from the funnel on the same read, honest paid conversions for THIRTY days
were 3, with 0 bridged to any MCP signal. The 190 was 1 row in
mcp_conversions plus 189 auto_trial_keys that had made at least one call —
Phase HH (2026-05-17) folded them together so a working auto-mint pipeline
would stop reading as "0 conversions". A free trial key that fires is an
ACTIVATION; publishing it as a conversion beside a "1:3" ratio is the
fabricated-metric shape this repo keeps finding.

Extracted with `ast` and run in isolation, per the repo rule: the module
opens a DB connection at import time (_ensure_schema) when DATABASE_URL is
set, so it is not imported here.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(REPO_ROOT, "routes", "mcp_growth.py")
_SRC = open(_SRC_PATH, encoding="utf-8").read()


def _publish():
    tree = ast.parse(_SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_publish_conversions")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _SRC_PATH, "exec"), ns)
    return ns["_publish_conversions"]


# The live inputs on 2026-09-02.
_LEDGER, _KEYS, _SIGNALS = 1, 189, 204


def test_trial_keys_are_published_as_activations_not_conversions():
    """★ THE REGRESSION."""
    out = _publish()({}, _LEDGER, _KEYS, _SIGNALS)
    assert out["conversions_7d"] == 1, out
    assert out["keys_activated_7d"] == 189
    assert out["conversion_ratio"] == "1:204", (
        "the ratio must be derived from the ledger count, not the sum")


def test_a_zero_ledger_beside_many_activations_reads_as_zero():
    """The exact case Phase HH was hiding — and the honest reading of it."""
    out = _publish()({}, 0, _KEYS, _SIGNALS)
    assert out["conversions_7d"] == 0
    assert out["keys_activated_7d"] == 189
    assert out["conversion_ratio"] == "1:204+"


def test_the_alias_keeps_its_value_and_is_declared_deprecated():
    out = _publish()({}, _LEDGER, _KEYS, _SIGNALS)
    assert out["auto_trial_conversions_7d"] == 189
    assert out["deprecated_aliases"] == {"auto_trial_conversions_7d": "keys_activated_7d"}
    assert "activations, not conversions" in out["deprecated_aliases_note"]
    assert "mcp_conversions" in out["conversions_basis"]
    assert "mcp_conversions only" in out["conversion_ratio_basis"]


def test_unread_inputs_are_null_not_zero():
    out = _publish()({}, _LEDGER, None, None)
    assert out["keys_activated_7d"] is None
    assert out["conversion_ratio"] is None, "signals unread -> no ratio, not 1:0"
    assert _publish()({}, _LEDGER, _KEYS, 0)["conversion_ratio"] is None


def test_the_compute_path_no_longer_folds_activations_into_conversions():
    """The pure helper is only worth anything if _compute_growth uses it and
    nothing re-adds the trial keys afterwards."""
    tree = ast.parse(_SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_compute_growth")
    body = ast.get_source_segment(_SRC, fn)
    assert "_publish_conversions(out" in body
    assert 'out["conversions_7d"] = (out.get("conversions_7d") or 0) +' not in body
    assert "+ atc" not in body and "+ keys_activated" not in body
    # the default shape declares the new key so an early-return payload has it
    assert '"keys_activated_7d"' in body


def test_the_snapshot_persists_the_ledger_count_under_conversions_7d():
    """mcp_growth_snapshots.conversions_7d is what the brain's WoW detector
    and the growth history read; it must now be the honest number."""
    assert 'payload.get("conversions_7d")' in _SRC
