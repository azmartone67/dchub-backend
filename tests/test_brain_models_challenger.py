"""Cross-model challenge invariant (2026-07-01): with DCHUB_BRAIN_PREFER_FABLE
on, the challenger tier must resolve to a DIFFERENT model than reasoning —
an independent second opinion, not fable grading its own homework."""
import importlib


def _fresh(monkeypatch, reach):
    import routes.brain_models as bm
    importlib.reload(bm)
    monkeypatch.setattr(bm, "_load_reachability", lambda: reach)
    return bm


ALL_REACHABLE = {
    "claude-fable-5": "reachable",
    "claude-opus-4-8": "reachable",
    "claude-sonnet-4-5": "reachable",
    "claude-haiku-4-5": "reachable",
}


def test_prefer_fable_keeps_challenger_cross_model(monkeypatch):
    monkeypatch.setenv("DCHUB_BRAIN_PREFER_FABLE", "1")
    monkeypatch.delenv("DCHUB_BRAIN_MODEL_CHALLENGER", raising=False)
    bm = _fresh(monkeypatch, ALL_REACHABLE)
    assert bm.brain_model_for("reasoning") == "claude-fable-5"
    assert bm.brain_model_for("inspector") == "claude-fable-5"
    ch = bm.brain_model_for("challenger")
    assert ch == "claude-opus-4-8", ch
    assert ch != bm.brain_model_for("reasoning")


def test_prefer_fable_challenger_env_pin_wins(monkeypatch):
    monkeypatch.setenv("DCHUB_BRAIN_PREFER_FABLE", "1")
    monkeypatch.setenv("DCHUB_BRAIN_MODEL_CHALLENGER", "claude-sonnet-4-5")
    bm = _fresh(monkeypatch, ALL_REACHABLE)
    assert bm.brain_model_for("challenger") == "claude-sonnet-4-5"
