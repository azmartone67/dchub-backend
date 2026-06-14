"""Guard the press-loop learning core (the measure->learn->draft loop).

brain_press_loop.py recorded ship-win draft stubs in press_brain_outcomes but
nothing ever populated engagement_score, and the draft step ignored what little
data there was — so the "keyword scoring shifts toward winners" loop the module
was built for was inert. The fix adds two PURE functions that are the math of
that loop:
  _engagement_score(press_views, click_outs) -> reach score for one post
  _keyword_weights({keyword: avg_score})     -> soft-greedy draft-priority factors

These assert the loop actually biases toward reach without ever starving a new
or chronically-flopping angle to zero (exploration floor).

NOTE: the CI unit-tests job installs ONLY pytest (not requirements.txt), so a
plain `from routes.brain_press_loop import ...` crashes collection (the module
imports Flask). We AST-extract just the two pure helpers and exec them in an
isolated namespace — the same pattern as test_media_editorial_classify.py — so
the test runs with no Flask/DB import.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BPL = os.path.join(ROOT, "routes", "brain_press_loop.py")


def _load(*names):
    src = open(BPL, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = [
        ast.get_source_segment(src, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    ns: dict = {}
    exec(compile("\n\n".join(pieces), BPL, "exec"), ns)
    return ns


_NS = _load("_engagement_score", "_keyword_weights")
_engagement_score = _NS["_engagement_score"]
_keyword_weights = _NS["_keyword_weights"]


# ── _engagement_score ────────────────────────────────────────────────
def test_score_weights_clicks_above_views():
    # A click-out (reader leaving for the product) is worth far more than a
    # passive view: 6x per the formula.
    assert _engagement_score(0, 1) == 6.0
    assert _engagement_score(6, 0) == 6.0
    assert _engagement_score(10, 2) == 22.0


def test_score_handles_none_and_zero():
    assert _engagement_score(None, None) == 0.0
    assert _engagement_score(0, 0) == 0.0


def test_score_monotonic_in_both_inputs():
    base = _engagement_score(5, 1)
    assert _engagement_score(6, 1) > base      # more views -> higher
    assert _engagement_score(5, 2) > base      # more clicks -> higher


# ── _keyword_weights ─────────────────────────────────────────────────
def test_empty_until_data():
    # No measured engagement anywhere -> no weights (callers fall back to 1.0).
    assert _keyword_weights({}) == {}
    assert _keyword_weights({"a": None, "b": None}) == {}
    assert _keyword_weights({"a": 0, "b": 0}) == {}


def test_winner_outranks_flopper_within_band():
    w = _keyword_weights({"winner": 100.0, "flopper": 10.0})
    # Best keyword pinned at the ceiling, worst stays above the floor — never zero.
    assert w["winner"] == 1.3
    assert w["winner"] > w["flopper"] >= 0.7


def test_floor_never_crushes_to_zero():
    # Even a keyword with ~0 engagement next to a strong winner keeps the 0.7x
    # floor, so it still gets drafted occasionally (exploration preserved).
    w = _keyword_weights({"winner": 500.0, "dead": 0.001})
    assert w["dead"] >= 0.7


def test_untried_keyword_is_omitted_not_penalized():
    # A keyword with no score isn't in the map -> caller treats it as neutral
    # 1.0x, which is HIGHER than a measured flopper's 0.7x. A fresh angle must
    # not be ranked below a proven loser.
    w = _keyword_weights({"flopper": 10.0, "winner": 100.0})
    assert "newangle" not in w
    assert w["flopper"] < 1.0   # measured flopper sits below the neutral 1.0
