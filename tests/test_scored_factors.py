"""scored_factors on /api/site-score and rank_sites (owner, 2026-09-25).

site-score composite-v2.4 renormalises its composite over the factors it could
score (risk is null when no state resolves). A 4/5 score is not a 5/5 score:
site-score says how many it scored at the top level, and rank_sites carries it
per row, flags a row missing risk, and ranks it below every complete row so a
renormalised score is never compared as an equal.
"""
import flask
import pytest

from util import site_scoring as ss

FIVE = {"power_infrastructure": 80, "gas_pipeline_access": 70, "fiber_connectivity": 90,
        "market_conditions": 60, "risk_resilience": 72}
NO_RISK = dict(FIVE, risk_resilience=None)


def test_scored_factors_counts_the_five():
    assert ss.scored_factors(FIVE) == "5/5"
    assert ss.scored_factors(NO_RISK) == "4/5"
    assert ss.scored_factors({}) == "0/5"


@pytest.mark.parametrize("row,want", [
    ({"scores": FIVE}, ("5/5", False)),
    ({"scores": NO_RISK}, ("4/5", True)),
    ({**NO_RISK}, ("4/5", True)),                                    # all five flattened
    ({"scored_factors": "4/5", "overall_basis": "renormalised_without:risk_resilience"},
     ("4/5", True)),
    ({"fiber_connectivity": 90, "risk_resilience": 70}, (None, False)),  # caller's own subset
    ({"capacity_mw": 100}, (None, False)),
])
def test_row_completeness(row, want):
    assert ss.row_completeness(row) == want


# ── rank_sites through the real blueprint ──────────────────────────────

@pytest.fixture
def client(monkeypatch):
    import routes.interconnection_queues as iq
    monkeypatch.setattr(iq, "_effective_unsupported", lambda: {})
    app = flask.Flask("rank")
    app.register_blueprint(iq.interconnection_queues_bp)
    return app.test_client()


def _rank(client, cands, objectives, **extra):
    r = client.post("/api/v1/rank-sites",
                    json={"candidates": cands, "objectives": objectives, "top_k": 10, **extra})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def test_a_row_missing_risk_ranks_below_every_complete_row(client):
    # The renormalised row has the higher overall; it still ranks last.
    cands = [
        {"id": "loudoun-renorm", "overall_score": 88.0, "scores": NO_RISK,
         "scored_factors": "4/5", "overall_basis": "renormalised_without:risk_resilience"},
        {"id": "pw-complete", "overall_score": 74.0, "scores": FIVE, "scored_factors": "5/5"},
        {"id": "secaucus-complete", "overall_score": 70.0, "scores": FIVE, "scored_factors": "5/5"},
    ]
    body = _rank(client, cands, {"overall_score": 1})
    order = [r["id"] for r in body["results"]]
    assert order == ["pw-complete", "secaucus-complete", "loudoun-renorm"]
    last = body["results"][-1]
    assert last["scored_factors"] == "4/5" and last["risk_not_scored"] is True
    assert body["results"][0]["scored_factors"] == "5/5"
    assert "risk_not_scored" not in body["results"][0]
    assert body["incomplete_rows"]["ids"] == ["loudoun-renorm"]


def test_rows_that_are_not_site_score_rows_rank_as_before(client):
    cands = [{"id": "a", "capacity_mw": 100}, {"id": "b", "capacity_mw": 300}]
    body = _rank(client, cands, {"capacity_mw": 1})
    assert [r["id"] for r in body["results"]] == ["b", "a"]
    assert "incomplete_rows" not in body
    assert all("scored_factors" not in r for r in body["results"])


def test_a_callers_own_factor_subset_is_not_called_incomplete(client):
    cands = [{"id": "a", "fiber_connectivity": 60, "risk_resilience": 70},
             {"id": "b", "fiber_connectivity": 90, "risk_resilience": 72}]
    body = _rank(client, cands, {"fiber_connectivity": 1, "risk_resilience": 1})
    assert [r["id"] for r in body["results"]] == ["b", "a"]
    assert "incomplete_rows" not in body


def test_site_score_emits_scored_factors_at_the_top(monkeypatch):
    # Reuse the composite-v2.4 handler harness: London has no US state.
    import tests.test_site_score_v24 as v24
    body = v24._run(monkeypatch, "lat=51.5074&lon=-0.1278")
    assert body["scored_factors"] == "4/5"
    body = v24._run(monkeypatch, v24.ASHBURN + "&state=VA", hv=("S", 500.0, 39.05, -77.49))
    assert body["scored_factors"] == "5/5"


def test_the_preview_carries_scored_factors():
    import ast, pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_site_score_preview")
    assert "'scored_factors': full.get('scored_factors')" in ast.get_source_segment(src, fn)
