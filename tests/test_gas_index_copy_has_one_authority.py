"""What we SAY about the DCGI's state must come from the kill switch.

2026-08-30. DCHUB_GAS_INDEX_ENABLED reached three modules. Five others
asserted the withdrawal in hardcoded prose — the agent cookbook, AGENTS.md,
the competitor positioning copy, the AI-surface audit and the MCP server — so
flipping the switch would have served scores from /api/v1/dcgi/scores while
`get_gas_index`'s own routing note still told the agent the index had been
withdrawn on 2026-08-08.

That is worse than a stale string. The agent channel IS the product, and the
whole reason the index came down was to stop the API from contradicting
itself. A restore that leaves the cookbook lying reproduces the defect with
the sign flipped.

Two invariants, because either alone is defeatable:
  1. no user-visible module hardcodes the withdrawal date;
  2. the token actually resolves, at every choke point, in BOTH switch
     positions — a token that leaks is a worse published string than the
     hardcoded sentence it replaced.
"""
import importlib
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# util/gas_index.py is the authority and MAY name the date. routes/dcgi.py
# renders the withdrawal notice itself and is gated by the same switch.
ALLOWED = {"util/gas_index.py", "routes/dcgi.py"}

# Files that carried the hardcoded assertion before this change. Listed
# explicitly so the fence names what it is protecting rather than scanning
# the world and hoping.
SWEPT = [
    "routes/agent_concierge.py",
    "routes/agents_md_fallback.py",
    "routes/competitive_intel.py",
]

_DATE = re.compile(r"withdrawn\s+2026-08-08", re.I)


def _string_constants(path):
    """Served string constants only.

    ★ DOCSTRINGS ARE EXCLUDED. The first version flagged the docstring that
      EXPLAINS this very fix — a fence that cannot tell served copy from the
      comment describing it forces the code to be written around it, which is
      how a guard starts editing the product's own explanation of itself.
    """
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


@pytest.mark.parametrize("rel", SWEPT)
def test_no_swept_module_hardcodes_the_withdrawal_state(rel):
    """A comment may record the history. A served STRING may not assert it."""
    offenders = [c[:110] for c in _string_constants(os.path.join(REPO, rel))
                 if _DATE.search(c) and "@@GAS_INDEX_STATE@@" not in c]
    assert not offenders, (
        f"{rel} hardcodes the DCGI withdrawal in served copy:\n  "
        + "\n  ".join(offenders)
        + "\nUse util.gas_index.GAS_STATE_TOKEN; the sentence is resolved at "
          "serve time so it cannot disagree with DCHUB_GAS_INDEX_ENABLED.")


@pytest.mark.parametrize("enabled", [False, True])
def test_the_token_never_survives_rendering(monkeypatch, enabled):
    """A leaked token is a worse published string than the sentence it
    replaced. Checked in BOTH switch positions — a resolver wired only on the
    path you happened to test is not wired."""
    if enabled:
        monkeypatch.setenv("DCHUB_GAS_INDEX_ENABLED", "1")
    else:
        monkeypatch.delenv("DCHUB_GAS_INDEX_ENABLED", raising=False)

    import util.gas_index as gi
    importlib.reload(gi)
    tok = gi.GAS_STATE_TOKEN

    import routes.agent_concierge as ac
    importlib.reload(ac)
    rendered = [ac._rendered_recipe(r) for r in ac.RECIPES] \
        if hasattr(ac, "RECIPES") else \
        [ac._rendered_recipe(r) for r in getattr(ac, "_COOKBOOK", [])]
    assert rendered, "no recipes found — the fence located nothing to check"
    blob = repr(rendered)
    assert tok not in blob, "an unresolved token reached a served recipe"
    assert "GAS_INDEX_STATE" not in blob

    import routes.agents_md_fallback as amd
    importlib.reload(amd)
    md = amd._render_agents_md()
    assert tok not in md and "GAS_INDEX_STATE" not in md, \
        "an unresolved token reached AGENTS.md"

    import routes.competitive_intel as ci
    importlib.reload(ci)
    diffs = repr(ci._resolved_differentiators())
    assert tok not in diffs and "GAS_INDEX_STATE" not in diffs, \
        "an unresolved token reached the differentiator list"


def test_the_rendered_sentence_tracks_the_switch(monkeypatch):
    import util.gas_index as gi
    monkeypatch.delenv("DCHUB_GAS_INDEX_ENABLED", raising=False)
    importlib.reload(gi)
    off = gi.gas_index_copy()
    monkeypatch.setenv("DCHUB_GAS_INDEX_ENABLED", "1")
    importlib.reload(gi)
    on = gi.gas_index_copy()
    assert off != on
    assert "never a score" in off
    assert "restored" in on.lower()
    # The restore must NOT imply the whole audit closed.
    assert "gas-to-grid" in on and "remains withdrawn" in on, (
        "the restored sentence must still name the $/MWh defect as open — it "
        "is a separate, unfixed defect from the same audit")


# ── the audit that inverts ─────────────────────────────────────────────────

def _markers(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("DCHUB_GAS_INDEX_ENABLED", "1")
    else:
        monkeypatch.delenv("DCHUB_GAS_INDEX_ENABLED", raising=False)
    import util.gas_index as gi
    importlib.reload(gi)
    import routes.white_glove_propagation as wg
    importlib.reload(wg)
    return [e["re"] for e in (wg.load_canon().get("stale_markers_regex") or [])]


def test_dcgi_drift_markers_are_dropped_once_the_index_is_restored(monkeypatch):
    """They fire on any listing MENTIONING the DCGI. Correct while withdrawn;
    exactly wrong once restored — every accurate listing would read as
    drifted, which is the always-red registry the detector exists to avoid."""
    assert any("DCGI" in p for p in _markers(monkeypatch, enabled=False)), \
        "the DCGI marker must still fire while the index is withdrawn"
    assert not any("DCGI" in p for p in _markers(monkeypatch, enabled=True)), \
        "the DCGI marker still fires with the index restored — every correct " \
        "listing is now flagged as drifted"


def test_the_marker_filter_fails_closed(monkeypatch):
    """If the switch cannot be read, KEEP the marker. A false 'drifted' is
    noise; a false 'clean' is the two weeks of unflagged listings that put
    this detector here."""
    import routes.white_glove_propagation as wg
    importlib.reload(wg)
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "util.gas_index":
            raise ImportError("simulated")
        return real(name, *a, **k)

    monkeypatch.setenv("DCHUB_GAS_INDEX_ENABLED", "1")
    monkeypatch.setattr(builtins, "__import__", boom)
    pats = [e["re"] for e in (wg.load_canon().get("stale_markers_regex") or [])]
    monkeypatch.setattr(builtins, "__import__", real)
    assert any("DCGI" in p for p in pats), \
        "an unreadable switch dropped the marker — that fails OPEN"
