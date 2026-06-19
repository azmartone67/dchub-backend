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
    resp = client.post("/api/v1/brain/ask", json={"question": "anything"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False


def test_ask_enqueues_async_when_enabled(client, monkeypatch):
    monkeypatch.setattr(inv, "_enabled", lambda: True)
    monkeypatch.setattr(inv, "investigate",
                        lambda q, **k: {"question": q, "confidence": 0.6,
                                        "recommendation": "do X"})
    # Stub storage so no DB is needed; enqueue returns a fake id.
    monkeypatch.setattr(inv, "_store_investigation", lambda q, r: 42)
    captured = {}
    monkeypatch.setattr(inv, "_update_investigation",
                        lambda i, r: captured.update(id=i, result=r) or True)
    resp = client.post("/api/v1/brain/ask", json={"question": "why flat?"})
    # ASYNC: the slow chain must NOT run in-request (502/flapping) — enqueue +
    # 202 + id + pending, no inline result.
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["id"] == 42
    assert data["status"] == "pending"
    assert "result" not in data
    # The background worker runs the (mocked) verified chain + stores the result.
    inv._run_investigation_async(42, "why flat?", "default")
    assert captured["id"] == 42
    assert captured["result"]["recommendation"] == "do X"


def test_ask_requires_admin(client, monkeypatch):
    monkeypatch.setattr(inv, "_admin_ok", lambda: False)
    resp = client.post("/api/v1/brain/ask", json={"question": "x"})
    assert resp.status_code == 403


def test_grade_endpoint_records_grade(client, monkeypatch):
    monkeypatch.setattr(inv, "_grade_investigation", lambda i, g: True)
    resp = client.post("/api/v1/brain/ask/7/grade", json={"grade": "good"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] == 7
    assert data["grade"] == "good"


def test_grade_endpoint_requires_grade(client):
    resp = client.post("/api/v1/brain/ask/7/grade", json={})
    assert resp.status_code == 400


def test_get_investigation_not_found(client, monkeypatch):
    monkeypatch.setattr(inv, "_get_investigation", lambda i: None)
    resp = client.get("/api/v1/brain/ask/999")
    assert resp.status_code == 404
