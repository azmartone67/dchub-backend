"""
tests/test_brain_model_routing.py — model-routing right-sizing (2026-07-04).

TRACK 1b: the investigator DECOMPOSE step is a mechanical extraction task
(emit 3-6 sub-questions + data-source names as JSON — explicitly NOT
answering the question), so it must ride the cheap "voice" tier, not the
fable/opus "reasoning" tier it launched on. The judgment-shaped steps keep
their tiers: REASON on "reasoning", REFUTE on "challenger".

Also guards the two invariants the re-tier depends on:
  · the voice-tier default model (haiku-4-5) is on the GA structured-outputs
    list, so _DECOMPOSE_SCHEMA still rides along after the re-tier;
  · _call_model(tier="voice") actually resolves the model THROUGH
    brain_model_for("voice") + resolve_chain (fallback semantics preserved)
    and the request body carries output_config (schema applied).

MOCKS everything — NO real Anthropic API, NO network, NO DB.
"""
import io
import json

import pytest

inv = pytest.importorskip("routes.brain_investigator")
bm = pytest.importorskip("routes.brain_models")
so = pytest.importorskip("routes.brain_llm_structured")


_STUB_EVIDENCE = [
    {"claim": "Tracked facilities", "source": "canonical_stats", "value": 21000},
]

_CANNED = {
    "decompose": json.dumps({
        "sub_questions": ["Is reach actually flat?"],
        "data_needed": ["weekly external IPs"],
    }),
    "reason": json.dumps({
        "recommendation": "Focus on retention.",
        "reasoning": "Evidence shows flat IPs.",
        "cited_evidence": ["Tracked facilities: 21000"],
        "confidence": 0.7,
        "caveats": ["Attribution noise."],
        "decision_for_human": "Fund SEO or connectors.",
    }),
    "refute": json.dumps({
        "weaknesses_found": ["IP count is a noisy proxy."],
        "survives_scrutiny": True,
        "confidence_adjustment": -0.1,
        "added_caveats": ["Confirm dedup."],
    }),
}


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(inv, "gather_evidence", lambda: list(_STUB_EVIDENCE))
    monkeypatch.setattr(inv, "_recall_prior_work",
                        lambda question, k=6: [])


# ── tier per call site ───────────────────────────────────────────────
def test_chain_tiers_decompose_voice_reason_reasoning_refute_challenger(monkeypatch):
    """DECOMPOSE rides voice; REASON stays reasoning; REFUTE stays
    challenger. Schemas ride along on every step (the structured-outputs
    kwarg is not dropped by the re-tier)."""
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    calls = []

    def caller(system, prompt, *, tier="reasoning", max_tokens=1500,
               schema=None, **kwargs):
        calls.append({"system": system, "tier": tier, "schema": schema,
                      "max_tokens": max_tokens})
        if "DECOMPOSE" in system:
            return _CANNED["decompose"], None, "claude-haiku-4-5"
        if "ADVERSARIAL CRITIC" in system:
            return _CANNED["refute"], None, "claude-opus-4-8"
        return _CANNED["reason"], None, "claude-fable-5"

    monkeypatch.setattr(inv, "_call_model", caller)
    out = inv.investigate("Why is reach flat?")
    assert "cannot_investigate" not in out

    decompose = [c for c in calls if "DECOMPOSE" in c["system"]]
    reason = [c for c in calls if "Reason FROM THE EVIDENCE" in c["system"]]
    refute = [c for c in calls if "ADVERSARIAL CRITIC" in c["system"]]
    assert len(decompose) == 1 and len(reason) == 1 and len(refute) >= 1

    # The re-tiered site: mechanical extraction → cheap voice tier.
    assert decompose[0]["tier"] == "voice"
    assert decompose[0]["schema"] is inv._DECOMPOSE_SCHEMA
    # Judgment-shaped sites keep their tiers.
    assert reason[0]["tier"] == "reasoning"
    assert reason[0]["schema"] is inv._REASON_SCHEMA
    assert all(c["tier"] == "challenger" for c in refute)
    assert all(c["schema"] is inv._REFUTE_SCHEMA for c in refute)


# ── voice default is structured-outputs capable ──────────────────────
def test_voice_tier_default_model_is_on_ga_structured_list():
    """The re-tier is only safe if the voice default (haiku-4-5) supports
    the GA structured-outputs param — otherwise _DECOMPOSE_SCHEMA would
    silently stop riding. Guard the prefix-list membership."""
    so.reset_runtime_unsupported()
    assert bm._DEFAULT_VOICE.startswith("claude-haiku-4-5")
    assert so.model_supports_structured(bm._DEFAULT_VOICE)


# ── _call_model resolves tier via brain_model_for + resolve_chain ────
class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_model_voice_tier_resolves_via_brain_model_for(monkeypatch):
    """End-to-end through _call_model with brain_model_for MOCKED: the
    voice tier must be resolved through brain_model_for + resolve_chain
    (fallback semantics preserved), the resolved model must land in the
    request body, and output_config (the structured schema) must ride."""
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "1")
    so.reset_runtime_unsupported()

    tiers_asked = []

    def fake_brain_model_for(tier="routine"):
        tiers_asked.append(tier)
        return {"voice": "claude-haiku-4-5"}.get(tier, "claude-opus-4-8")

    monkeypatch.setattr(bm, "brain_model_for", fake_brain_model_for)

    sent_bodies = []

    def fake_urlopen(req, timeout=None):
        sent_bodies.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp(json.dumps({
            "content": [{"type": "text", "text": _CANNED["decompose"]}],
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    text, err, model = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "Operator question: x\n\nDecompose it.",
        tier="voice", max_tokens=2000, schema=inv._DECOMPOSE_SCHEMA)

    assert err is None
    assert tiers_asked == ["voice"]                 # tier reached brain_model_for
    assert model == "claude-haiku-4-5"              # resolved voice model used
    body = sent_bodies[0]
    assert body["model"] == "claude-haiku-4-5"
    # Schema still rides: GA structured-outputs param present with our schema.
    assert body.get("output_config", {}).get("format", {}).get("schema") \
        == inv._DECOMPOSE_SCHEMA
    assert json.loads(text) == json.loads(_CANNED["decompose"])


def test_call_model_voice_walks_resolve_chain_on_404(monkeypatch):
    """Fallback semantics preserved after the re-tier: a 404 on the first
    rung walks resolve_chain to the next model instead of dying."""
    import urllib.error

    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")  # isolate chain-walk
    monkeypatch.setattr(bm, "brain_model_for",
                        lambda tier="routine": "claude-sonnet-4-5")
    # real resolve_chain: sonnet-4-5 → haiku-4-5

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        if body["model"] == "claude-sonnet-4-5":
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {},
                                         io.BytesIO(b"model not found"))
        return _FakeResp(json.dumps({
            "content": [{"type": "text", "text": _CANNED["decompose"]}],
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    text, err, model = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "q", tier="voice", max_tokens=2000)
    assert err is None
    assert model == "claude-haiku-4-5"
    assert text
