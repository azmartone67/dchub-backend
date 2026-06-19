"""
tests/test_brain_investigator.py — Brain Investigator (recommend-only).

MOCKS the brain_models LLM call entirely — NO real Anthropic API, NO network.
We patch routes.brain_investigator._call_model so the 5-step chain runs against
canned responses, and we stub gather_evidence so no DB is needed.

Covers:
  · the 5-step chain runs + returns a structured recommendation with confidence,
    caveats, decision_for_human, and a refutation block;
  · a fabricated figure from the model is caught/flagged against the canon;
  · cannot_investigate when the model helper is unavailable (no API key);
  · the grade endpoint records a grade;
  · flag-off short-circuits POST /ask BEFORE any model call.
"""
import json

import pytest

inv = pytest.importorskip("routes.brain_investigator")

# The autouse _no_db_evidence fixture (below) stubs inv.gather_evidence so the
# 5-step-chain tests never touch a DB. Capture the REAL function here, BEFORE
# any patching, so the GOAL-A tests can exercise the genuine gather_evidence.
_REAL_GATHER_EVIDENCE = inv.gather_evidence


# ── helpers ──────────────────────────────────────────────────────────
def _fake_caller(responses):
    """Build a _call_model replacement that returns canned (text, err, model)
    in order of the system-prompt's role. We dispatch on a substring of the
    system prompt so the decompose / reason / refute passes get the right
    canned JSON regardless of call order."""
    def caller(system, prompt, *, tier="reasoning", max_tokens=1500):
        if "DECOMPOSE" in system:
            return responses["decompose"], None, "claude-opus-4-8"
        if "ADVERSARIAL CRITIC" in system:
            return responses["refute"], None, "claude-sonnet-4-5"
        # default = the REASON pass
        return responses["reason"], None, "claude-opus-4-8"
    return caller


_GOOD_RESPONSES = {
    "decompose": json.dumps({
        "sub_questions": ["Is reach actually flat?", "What is the constraint?"],
        "data_needed": ["weekly external IPs", "facility count"],
    }),
    "reason": json.dumps({
        "recommendation": "Focus on first-touch retention; reach is constrained "
                          "by external IP acquisition, not raw volume.",
        "reasoning": "Evidence shows ~21,000 tracked facilities but flat new IPs.",
        "cited_evidence": ["Tracked facilities: 21000"],
        "confidence": 0.7,
        "caveats": ["Attribution may lose registry traffic."],
        "decision_for_human": "Decide whether to fund SEO vs connector listings.",
    }),
    "refute": json.dumps({
        "weaknesses_found": ["IP count is a noisy proxy for reach."],
        "survives_scrutiny": True,
        "confidence_adjustment": -0.1,
        "added_caveats": ["Confirm the IP metric is deduped."],
    }),
}

_STUB_EVIDENCE = [
    {"claim": "Tracked facilities", "source": "canonical_stats", "value": 21000},
    {"claim": "Countries", "source": "canonical_stats", "value": 178},
]


@pytest.fixture(autouse=True)
def _no_db_evidence(monkeypatch):
    """Stub gather_evidence so the chain never touches a DB."""
    monkeypatch.setattr(inv, "gather_evidence", lambda: list(_STUB_EVIDENCE))


# ── the 5-step chain ────────────────────────────────────────────────
def test_five_step_chain_returns_structured_recommendation(monkeypatch):
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(inv, "_call_model", _fake_caller(_GOOD_RESPONSES))

    out = inv.investigate("Why is reach flat?")

    # Step 1: decomposition present.
    assert out["decomposition"]["sub_questions"]
    assert out["decomposition"]["data_needed"]
    # Step 2: real evidence carried through.
    assert out["evidence"] == _STUB_EVIDENCE
    # Step 3: recommendation.
    assert "retention" in out["recommendation"].lower()
    # Step 5: confidence + caveats + decision_for_human.
    assert 0.0 <= out["confidence"] <= 1.0
    assert isinstance(out["caveats"], list) and out["caveats"]
    assert out["decision_for_human"]
    # Step 4: refutation block.
    ref = out["refutation"]
    assert ref["attempted"] is True
    assert ref["survived"] is True
    assert ref["weaknesses_found"]
    # No cannot_investigate on the happy path.
    assert "cannot_investigate" not in out


def test_refutation_lowers_confidence_via_adjustment(monkeypatch):
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(inv, "_call_model", _fake_caller(_GOOD_RESPONSES))
    out = inv.investigate("Why is reach flat?")
    # draft 0.7 + refute adjustment (-0.1) = 0.6.
    assert out["confidence"] == pytest.approx(0.6, abs=1e-6)
    # The refutation's added caveat folded in.
    assert any("deduped" in c.lower() for c in out["caveats"])


def test_refutation_failure_marks_unstress_tested(monkeypatch):
    """If the refute pass errors, we must NOT pretend it survived — confidence
    drops and a caveat is added."""
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")

    def caller(system, prompt, *, tier="reasoning", max_tokens=1500):
        if "DECOMPOSE" in system:
            return _GOOD_RESPONSES["decompose"], None, "m"
        if "ADVERSARIAL CRITIC" in system:
            return None, "http_500", None      # refute pass fails
        return _GOOD_RESPONSES["reason"], None, "m"

    monkeypatch.setattr(inv, "_call_model", caller)
    out = inv.investigate("Why is reach flat?")
    assert out["refutation"]["attempted"] is False
    assert any("un-stress-tested" in c.lower() or "refutation could not run" in c.lower()
               for c in out["caveats"])
    # confidence docked from 0.7 to ~0.55.
    assert out["confidence"] == pytest.approx(0.55, abs=1e-6)


# ── fabrication fence ───────────────────────────────────────────────
def test_fabricated_figure_is_flagged_against_canon(monkeypatch):
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    bad = dict(_GOOD_RESPONSES)
    bad["reason"] = json.dumps({
        "recommendation": "We track 50,000 facilities and $324B in deals, so "
                          "scale the sales team.",
        "reasoning": "big numbers",
        "cited_evidence": [],
        "confidence": 0.9,
        "caveats": [],
        "decision_for_human": "Hire reps.",
    })
    monkeypatch.setattr(inv, "_call_model", _fake_caller(bad))

    out = inv.investigate("Should we scale sales?")
    # The banned 50,000 + $324B figures must be flagged.
    joined = " ".join(out["caveats"]).lower()
    assert "fabrication flagged" in joined
    assert "50,000" in joined or "21,000" in joined
    assert "324b" in joined
    # Confidence capped low.
    assert out["confidence"] <= 0.3
    assert out["refutation"].get("fabrication_flagged") is True


def test_fence_detects_banned_figures_directly():
    hits = inv._fence_fabricated_figures("we have 50,000 sites worth $324B")
    assert len(hits) == 2


def test_clean_text_passes_the_fence():
    assert inv._fence_fabricated_figures(
        "we track 21,000 facilities across 178 countries") == []


# ── cannot_investigate (model helper unavailable) ───────────────────
def test_cannot_investigate_without_api_key(monkeypatch):
    """No API key -> _call_model returns no_api_key -> cannot_investigate, and
    NEVER raises."""
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "")

    def caller(system, prompt, *, tier="reasoning", max_tokens=1500):
        return None, "no_api_key", None

    monkeypatch.setattr(inv, "_call_model", caller)
    out = inv.investigate("anything")
    assert out["cannot_investigate"] == "no_api_key"
    assert out["recommendation"] is None
    assert out["confidence"] == 0.0


def test_empty_question_cannot_investigate():
    out = inv.investigate("   ")
    assert out["cannot_investigate"] == "empty_question"


# ── GOAL A: growth-funnel evidence ──────────────────────────────────
# A canned honest-dashboard blob shaped like routes.funnel_health._data_cached().
_FAKE_FUNNEL_DATA = {
    "kpis": {
        "mrr_usd": 4735,
        "conversions_30d": 3,
        "active_dev_keys": 412,
        "tool_calls_7d": 35021,
        "dev_keys_by_tier": {"free": 400, "paid": 10, "enterprise": 2},
    },
    "funnel": {
        "distinct_paid_users_30d": 9,
        "stage_drops_pct": {"signals->codes": 80.0, "codes->paid": 95.0},
    },
}
_FAKE_REACH_CACHE = {
    "ts": 1.0,
    "data": {"distinct_agents_7d": 76, "distinct_platforms": 5},
}


def _patch_growth_sources(monkeypatch, *, funnel=_FAKE_FUNNEL_DATA,
                          reach=_FAKE_REACH_CACHE):
    """Inject fake modules for funnel_health + ai_reach so gather_growth_funnel
    runs with NO DB / NO network. We swap the symbols inside sys.modules so the
    function-local `from routes.funnel_health import _data_cached` resolves to
    our fakes."""
    import sys
    import types

    fh = types.ModuleType("routes.funnel_health")
    fh._data_cached = lambda: (dict(funnel) if funnel is not None else None)
    monkeypatch.setitem(sys.modules, "routes.funnel_health", fh)

    reach_mod = types.ModuleType("routes.ai_reach")
    reach_mod._cache = dict(reach) if reach is not None else {}
    monkeypatch.setitem(sys.modules, "routes.ai_reach", reach_mod)


def test_gather_growth_funnel_returns_funnel_items(monkeypatch):
    """The new helper surfaces MRR, conversions, paid keys, reach, retention —
    the measured data that lets growth questions clear 0.25 confidence."""
    _patch_growth_sources(monkeypatch)
    items = inv.gather_growth_funnel()
    assert isinstance(items, list) and items
    # every item is the {claim, source, value} shape the chain expects.
    for it in items:
        assert set(it.keys()) >= {"claim", "source", "value"}

    def _val(needle):
        for it in items:
            if needle in it["claim"].lower():
                return it["value"]
        return None

    # MRR is the users.plan-derived number (~$4.7k), NOT a row count.
    assert _val("mrr") == 4735
    # Conversions from the Stripe-backed table.
    assert _val("conversion") == 3
    # PAID keys = tier in (paid, enterprise) = 10 + 2 = 12 (NOT the 412 total).
    assert _val("paid dev keys") == 12
    # Reach = DISTINCT external IPs (honest, not loop-inflated volume).
    assert _val("external ai agents") == 76
    # Retention proxy: distinct paid users 30d.
    assert _val("distinct paid users") == 9


def test_gather_evidence_includes_growth_funnel(monkeypatch):
    """gather_evidence must APPEND the growth-funnel items to the existing
    canonical/findings/baseline evidence (not replace them)."""
    # Stub the other three sources to empty so we isolate the growth items.
    monkeypatch.setattr(inv, "_gather_recent_findings", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_health_baseline", lambda *a, **k: [])
    # canonical_stats import lives inside gather_evidence; make it a no-op by
    # forcing an empty stats dict via a fake module.
    import sys
    import types
    cs = types.ModuleType("canonical_stats")
    cs.get_canonical_stats = lambda: {}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)
    _patch_growth_sources(monkeypatch)

    # Bypass the autouse stub — exercise the REAL gather_evidence.
    ev = _REAL_GATHER_EVIDENCE()
    sources = " ".join(e.get("source", "") for e in ev)
    assert "funnel_health" in sources
    assert "ai_reach" in sources
    # At least the MRR claim made it through.
    assert any("mrr" in e["claim"].lower() for e in ev)


def test_gather_growth_funnel_degraded_source_yields_empty(monkeypatch):
    """A degraded underlying source (raises on call) must yield [] WITHOUT
    raising — best-effort contract."""
    import sys
    import types

    fh = types.ModuleType("routes.funnel_health")

    def _boom():
        raise RuntimeError("DB down")

    fh._data_cached = _boom
    monkeypatch.setitem(sys.modules, "routes.funnel_health", fh)

    # ai_reach module absent / empty cache → no reach items either.
    reach_mod = types.ModuleType("routes.ai_reach")
    reach_mod._cache = {}
    monkeypatch.setitem(sys.modules, "routes.ai_reach", reach_mod)

    items = inv.gather_growth_funnel()
    assert items == []


def test_gather_evidence_survives_growth_funnel_failure(monkeypatch):
    """If gather_growth_funnel itself blows up, gather_evidence must still
    return the other evidence and NOT crash the investigation."""
    monkeypatch.setattr(inv, "_gather_recent_findings", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_health_baseline", lambda *a, **k: [])
    import sys
    import types
    cs = types.ModuleType("canonical_stats")
    cs.get_canonical_stats = lambda: {"facilities": 21000}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)

    def _boom():
        raise RuntimeError("growth source exploded")

    monkeypatch.setattr(inv, "gather_growth_funnel", _boom)
    ev = _REAL_GATHER_EVIDENCE()  # must not raise (bypass autouse stub)
    # The canonical facilities item still came through.
    assert any("facilities" in e["claim"].lower() for e in ev)


# ── endpoints (Flask test client) ───────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(inv.brain_investigator_bp)
    # Admin gate always passes in tests.
    monkeypatch.setattr(inv, "_admin_ok", lambda: True)
    return app.test_client()


def test_flag_off_short_circuits_before_model(client, monkeypatch):
    """When BRAIN_INVESTIGATOR_ENABLED is off, POST /ask returns enabled:false
    WITHOUT ever calling investigate / a model."""
    monkeypatch.setattr(inv, "_enabled", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("investigate must NOT be called when flag is off")

    monkeypatch.setattr(inv, "investigate", _boom)
    resp = client.post("/api/v1/brain/investigate", json={"question": "anything"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False


def test_ask_runs_sync_and_returns_result(client, monkeypatch):
    monkeypatch.setattr(inv, "_enabled", lambda: True)
    monkeypatch.setattr(inv, "investigate",
                        lambda q, **k: {"question": q, "confidence": 0.6,
                                        "recommendation": "do X"})
    # Stub storage so no DB is needed; the store returns the new row id.
    monkeypatch.setattr(inv, "_store_investigation", lambda q, r: 42)
    resp = client.post("/api/v1/brain/investigate", json={"question": "why flat?"})
    # SYNCHRONOUS + DURABLE: the ~48s chain fits the 120s worker timeout and runs
    # in-request — no daemon thread for a redeploy/worker-recycle to silently kill
    # (which left rows stuck 'pending'). 200 + id + the result returned inline.
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["id"] == 42
    assert data["result"]["recommendation"] == "do X"
    assert data["result"]["confidence"] == 0.6


def test_ask_requires_admin(client, monkeypatch):
    monkeypatch.setattr(inv, "_admin_ok", lambda: False)
    resp = client.post("/api/v1/brain/investigate", json={"question": "x"})
    assert resp.status_code == 403


def test_grade_endpoint_records_grade(client, monkeypatch):
    monkeypatch.setattr(inv, "_grade_investigation", lambda i, g: True)
    resp = client.post("/api/v1/brain/investigate/7/grade", json={"grade": "good"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] == 7
    assert data["grade"] == "good"


def test_grade_endpoint_requires_grade(client):
    resp = client.post("/api/v1/brain/investigate/7/grade", json={})
    assert resp.status_code == 400


def test_get_investigation_not_found(client, monkeypatch):
    monkeypatch.setattr(inv, "_get_investigation", lambda i: None)
    resp = client.get("/api/v1/brain/investigate/999")
    assert resp.status_code == 404
