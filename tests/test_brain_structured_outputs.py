"""
tests/test_brain_structured_outputs.py — Anthropic STRUCTURED OUTPUTS wiring
=============================================================================

Mocked, no network. Per fragile-JSON call site (L6 strategic planner,
investigator REASON/REFUTE/DECOMPOSE, feature proposer) we assert:

  1. the request carries the VERIFIED structured-output parameter —
     output_config.format = {type: "json_schema", schema: <site schema>}
     (GA param; the deprecated `output_format` / beta header are never sent);
  2. a structured response parses WITHOUT fence-stripping (bare JSON);
  3. an API 400 on the structured attempt falls back to the legacy free-text
     path on the SAME model, and the legacy fence-strip still works on a
     fenced fixture;
  4. the BRAIN_STRUCTURED_OUTPUTS=0 kill switch forces the legacy path;
  5. every key a downstream consumer reads (.get() introspection of the
     consumer source) is a property of the schema — a schema that drops a
     consumed field would be silent data loss.

Plus: the per-model support gate (Sonnet 4.0 = claude-sonnet-5 does
NOT support structured outputs → always legacy) and schema hygiene against
the documented structured-outputs limitations (additionalProperties: false
everywhere; no numeric/string bound keywords; minItems ∉ {0,1} never used).
"""
import inspect
import io
import json
import re
import urllib.error
import urllib.request

import pytest

import requests as requests_mod

so = pytest.importorskip("routes.brain_llm_structured")
sp = pytest.importorskip("routes.brain_strategic_planner")
inv = pytest.importorskip("routes.brain_investigator")
fp = pytest.importorskip("routes.brain_feature_proposer")
bm = pytest.importorskip("routes.brain_models")
qap = pytest.importorskip("tools.qa_superuser.propose")


# ── shared fixtures / fakes ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_structured_state(monkeypatch):
    """Deterministic structured-output state: kill switch unset (default ON),
    runtime-unsupported memo empty, single-model chain on a supported model."""
    monkeypatch.delenv("BRAIN_STRUCTURED_OUTPUTS", raising=False)
    so.reset_runtime_unsupported()
    monkeypatch.setattr(bm, "brain_model_for", lambda tier="routine": "claude-opus-4-8")
    monkeypatch.setattr(bm, "resolve_chain", lambda m, max_depth=5: [m])
    yield
    so.reset_runtime_unsupported()


class _FakeHTTPResponse:
    """requests.Response stand-in for the planner (requests-based) site."""

    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def _anthropic_ok_body(payload_text):
    return {
        "stop_reason": "end_turn",
        "content": [
            # thinking block first — fable-5-style always-on thinking; all
            # call sites must read the first type=="text" block instead.
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": payload_text},
        ],
    }


class _UrlopenCM:
    """Context manager returned by the fake urllib.request.urlopen."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


def _http_error(code, body: bytes):
    return urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", code, "err", None,
        io.BytesIO(body))


def _sys_text(system):
    """The system field is a cache_control block list since prompt caching
    landed (2026-07-23); it was a bare string before. Compare on the text so
    these assertions pin the PROMPT, not the caching wrapper."""
    if isinstance(system, str):
        return system
    return "".join(b.get("text", "") for b in system)

def _req_body(req) -> dict:
    return json.loads(req.data.decode("utf-8"))


# ═════════════════════════════════════════════════════════════════════
# helper module — model gate + kill switch + boilerplate strip
# ═════════════════════════════════════════════════════════════════════

def test_model_gate_matches_verified_docs():
    # supported (verified live docs 2026-07-04)
    for m in ("claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
              "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5",
              "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5"):
        assert so.model_supports_structured(m), m
    # NOT supported: Sonnet 4.0 (the proposer's static fallback rung),
    # Opus 4.1/4.0, retired 3.x.
    # ★2026-07-25: this list had been rewritten to "claude-sonnet-5" and
    # "claude-opus-4-8" by a blanket model-ID migration — both of which the
    # gate DOES support, so the test asserted a model was and was not
    # supported in the same function and could never pass. The prose above
    # ("Sonnet 4.0", "Opus 4.1/4.0") is the original intent; restored.
    for m in ("claude-sonnet-4-0", "claude-opus-4-1",
              "claude-opus-4-0", "claude-3-5-haiku-20241022", ""):
        assert not so.model_supports_structured(m), m


def test_kill_switch_env(monkeypatch):
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    assert so.structured_active("claude-opus-4-8", schema)
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")
    assert not so.structured_active("claude-opus-4-8", schema)
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "false")
    assert not so.structured_active("claude-opus-4-8", schema)
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "1")
    assert so.structured_active("claude-opus-4-8", schema)


def test_runtime_unsupported_memo():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    assert so.structured_active("claude-opus-4-8", schema)
    so.mark_model_unsupported("claude-opus-4-8")
    assert not so.structured_active("claude-opus-4-8", schema)
    so.reset_runtime_unsupported()
    assert so.structured_active("claude-opus-4-8", schema)


def test_rejection_detector():
    assert so.looks_like_structured_rejection(
        400, '{"error":{"message":"output_config: not supported"}}')
    assert not so.looks_like_structured_rejection(400, "rate stuff")
    assert not so.looks_like_structured_rejection(
        429, "output_config")  # only 400s


def test_boilerplate_strip_leaves_legacy_constants_untouched():
    # The legacy prompts still carry their reply-only-JSON boilerplate…
    assert "Reply with ONLY the JSON object" in sp._SYSTEM_PROMPT
    assert ", no prose outside it" in inv._DECOMPOSE_SYSTEM
    assert " — no prose outside the JSON" in fp._PROPOSE_SYSTEM
    # …and the structured-mode variant strips exactly that boilerplate.
    assert "Reply with ONLY the JSON object" not in \
        so.strip_json_only_boilerplate(sp._SYSTEM_PROMPT)
    assert ", no prose outside it" not in \
        so.strip_json_only_boilerplate(inv._DECOMPOSE_SYSTEM)
    assert " — no prose outside the JSON" not in \
        so.strip_json_only_boilerplate(fp._PROPOSE_SYSTEM)


# ═════════════════════════════════════════════════════════════════════
# (a) L6 strategic planner — routes/brain_strategic_planner.py
# ═════════════════════════════════════════════════════════════════════

_L6_MINIMAL = {
    "summary": "state of play", "top_gaps_4w": [], "competitor_lacks": [],
    "funnel_optimizations": [],
    "wildcard_bet": {"title": "t", "spec": "s", "horizon_months": 6,
                     "confidence": "low"},
    "stop_doing": None, "self_critique": "meh",
}


def _patch_planner(monkeypatch, responses):
    """Patch requests.post with a scripted sequence; capture request bodies."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "body": json})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(sp, "_ANTHROPIC_KEY", "test-key")
    monkeypatch.setattr(requests_mod, "post", fake_post)
    return calls


def test_planner_request_carries_output_config_and_parses_bare_json(monkeypatch):
    ok = _FakeHTTPResponse(200, _anthropic_ok_body(json.dumps(_L6_MINIMAL)))
    calls = _patch_planner(monkeypatch, [ok])

    out = sp._call_claude("ctx")

    assert len(calls) == 1
    body = calls[0]["body"]
    # (1) verified GA param, right schema — and never the deprecated spelling
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": sp._L6_REC_SCHEMA}}
    assert "output_format" not in body
    # structured mode strips the reply-only-JSON boilerplate from system
    assert "Reply with ONLY the JSON object" not in body["system"]
    # (2) bare JSON (no fences) parsed
    assert out["summary"] == "state of play"
    assert out["_model_used"] == "claude-opus-4-8"


def test_planner_400_falls_back_to_legacy_and_fence_strip_still_works(monkeypatch):
    fenced = "```json\n" + json.dumps(_L6_MINIMAL) + "\n```"
    rej = _FakeHTTPResponse(
        400, text='{"error":{"message":"output_config is not supported"}}')
    ok = _FakeHTTPResponse(200, _anthropic_ok_body(fenced))
    calls = _patch_planner(monkeypatch, [rej, ok])

    out = sp._call_claude("ctx")

    # (3) same model retried on the legacy path; legacy body has NO
    # output_config and the ORIGINAL system prompt; fence-strip parsed it.
    assert len(calls) == 2
    assert "output_config" in calls[0]["body"]
    assert "output_config" not in calls[1]["body"]
    assert _sys_text(calls[1]["body"]["system"]) == sp._SYSTEM_PROMPT
    assert calls[0]["body"]["model"] == calls[1]["body"]["model"]
    assert out["summary"] == "state of play"
    # the 400 blamed output_config → model memoized as runtime-unsupported
    assert not so.model_supports_structured("claude-opus-4-8")


def test_planner_kill_switch_forces_legacy(monkeypatch):
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")
    fenced = "```json\n" + json.dumps(_L6_MINIMAL) + "\n```"
    ok = _FakeHTTPResponse(200, _anthropic_ok_body(fenced))
    calls = _patch_planner(monkeypatch, [ok])

    out = sp._call_claude("ctx")

    assert len(calls) == 1
    assert "output_config" not in calls[0]["body"]
    assert _sys_text(calls[0]["body"]["system"]) == sp._SYSTEM_PROMPT
    assert out["summary"] == "state of play"          # legacy fence-strip


def test_planner_schema_covers_every_persisted_field():
    """(5) Introspect _persist_recommendations (+ known ledger read): every
    .get() key the consumer touches must be a schema property."""
    src = inspect.getsource(sp._persist_recommendations)
    props = sp._L6_REC_SCHEMA["properties"]

    item_props = {}
    for field in ("top_gaps_4w", "competitor_lacks", "funnel_optimizations"):
        item_props.update(props[field]["items"]["properties"])

    for key in re.findall(r'item\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', src):
        assert key in item_props, f"consumer reads item.{key} — not in schema"
    for key in re.findall(r'wc\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', src):
        assert key in props["wildcard_bet"]["properties"], \
            f"consumer reads wildcard_bet.{key} — not in schema"
    for key in re.findall(r'payload\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', src):
        if key.startswith("_"):        # injected client-side post-parse
            continue
        assert key in props, f"consumer reads payload.{key} — not in schema"
    # kind→field map targets exist
    for _kind, field in sp._KIND_FIELD_MAP:
        assert field in props, f"_KIND_FIELD_MAP field {field} not in schema"
    # STRATEGIC-OUTCOME LEDGER reads item.target_metric on every rec kind
    for field in ("top_gaps_4w", "competitor_lacks", "funnel_optimizations"):
        assert "target_metric" in props[field]["items"]["properties"]


# ═════════════════════════════════════════════════════════════════════
# (b) investigator — routes/brain_investigator.py
# ═════════════════════════════════════════════════════════════════════

def _patch_urlopen(monkeypatch, script):
    """script: list of bytes (response) or Exception (raised). Captures the
    urllib Request objects."""
    reqs = []

    def fake_urlopen(req, timeout=None):
        reqs.append(req)
        step = script[min(len(reqs) - 1, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        return _UrlopenCM(step)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return reqs


_DECOMP_JSON = json.dumps({"sub_questions": ["a?"], "data_needed": ["b"]})


def test_investigator_request_carries_schema_and_parses_bare_json(monkeypatch):
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    raw = json.dumps(_anthropic_ok_body(_DECOMP_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [raw])

    text, err, model = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "q", tier="reasoning", max_tokens=100,
        schema=inv._DECOMPOSE_SCHEMA)

    assert err is None and model == "claude-opus-4-8"
    body = _req_body(reqs[0])
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": inv._DECOMPOSE_SCHEMA}}
    assert "output_format" not in body
    # (2) bare JSON — parses directly, no fences involved
    assert json.loads(text) == json.loads(_DECOMP_JSON)
    # _parse_json (the universal parser) handles it identically
    assert inv._parse_json(text) == json.loads(_DECOMP_JSON)


def test_investigator_400_falls_back_same_model_then_fence_strip(monkeypatch):
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    fenced = "```json\n" + _DECOMP_JSON + "\n```"
    reqs = _patch_urlopen(monkeypatch, [
        _http_error(400, b'{"error":{"message":"json_schema invalid"}}'),
        json.dumps(_anthropic_ok_body(fenced)).encode(),
    ])

    text, err, model = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "q", tier="reasoning", max_tokens=100,
        schema=inv._DECOMPOSE_SCHEMA)

    assert err is None and model == "claude-opus-4-8"
    assert len(reqs) == 2
    assert "output_config" in _req_body(reqs[0])
    legacy_body = _req_body(reqs[1])
    assert "output_config" not in legacy_body
    assert _sys_text(legacy_body["system"]) == inv._DECOMPOSE_SYSTEM     # untouched
    assert legacy_body["model"] == "claude-opus-4-8"          # SAME model
    # (3) legacy fence-strip still works on the fenced fixture
    assert inv._parse_json(text) == json.loads(_DECOMP_JSON)
    # 400 named json_schema → memoized
    assert not so.model_supports_structured("claude-opus-4-8")


def test_investigator_kill_switch_forces_legacy(monkeypatch):
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    raw = json.dumps(_anthropic_ok_body(_DECOMP_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [raw])

    text, err, _ = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "q", schema=inv._DECOMPOSE_SCHEMA)

    assert err is None
    assert len(reqs) == 1
    body = _req_body(reqs[0])
    assert "output_config" not in body
    assert _sys_text(body["system"]) == inv._DECOMPOSE_SYSTEM


def test_investigator_legacy_chain_walk_preserved(monkeypatch):
    """A non-structured 404 still walks the fallback chain exactly as before."""
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(bm, "resolve_chain",
                        lambda m, max_depth=5: [m, "claude-sonnet-4-5"])
    raw = json.dumps(_anthropic_ok_body(_DECOMP_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [
        _http_error(404, b"not found"),   # model 1 → 404: chain walks at once
        raw,                              # model 2 answers
    ])

    text, err, model = inv._call_model(
        inv._DECOMPOSE_SYSTEM, "q", schema=inv._DECOMPOSE_SCHEMA)

    assert err is None and model == "claude-sonnet-4-5"
    # a 404 never triggers a same-model legacy retry — exactly 2 requests
    assert len(reqs) == 2
    assert _req_body(reqs[1])["model"] == "claude-sonnet-4-5"
    assert json.loads(text) == json.loads(_DECOMP_JSON)


def test_investigator_schemas_cover_consumer_reads():
    """(5) investigate() reads decomp./draft./ref. keys — all schema props."""
    src = inspect.getsource(inv.investigate)
    checks = (
        (r'decomp\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', inv._DECOMPOSE_SCHEMA),
        (r'draft\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', inv._REASON_SCHEMA),
        (r'ref\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', inv._REFUTE_SCHEMA),
    )
    hits = 0
    for pattern, schema in checks:
        for key in re.findall(pattern, src):
            hits += 1
            assert key in schema["properties"], \
                f"investigate() reads {key} — not in schema"
    assert hits >= 10   # the introspection actually matched the consumers


# ═════════════════════════════════════════════════════════════════════
# (c) feature proposer — routes/brain_feature_proposer.py
# ═════════════════════════════════════════════════════════════════════

_SPEC_JSON = json.dumps({
    "feature_name": "Fiber Route Compare",
    "feature_slug": "fiber-route-compare",
    "problem": "p.", "proposed_ux": "u.", "complexity": "small",
    "priority_justification": "j.", "primary_route": "/fiber/compare",
    "reuses": ["plan_fiber_leadin"],
})


def test_proposer_request_carries_schema_and_parses_bare_json(monkeypatch):
    monkeypatch.setattr(fp, "ANTHROPIC_API_KEY", "test-key")
    raw = json.dumps(_anthropic_ok_body(_SPEC_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [raw])

    text, err = fp._call_claude("cluster prompt")

    assert err is None
    body = _req_body(reqs[0])
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": fp._PROPOSE_SCHEMA}}
    assert "output_format" not in body
    assert " — no prose outside the JSON" not in body["system"]
    assert json.loads(text)["feature_name"] == "Fiber Route Compare"
    # downstream parser accepts it unchanged
    assert fp._parse_spec_json(text)["feature_slug"] == "fiber-route-compare"


def test_proposer_400_falls_back_same_model_then_fence_strip(monkeypatch):
    monkeypatch.setattr(fp, "ANTHROPIC_API_KEY", "test-key")
    fenced = "```json\n" + _SPEC_JSON + "\n```"
    reqs = _patch_urlopen(monkeypatch, [
        _http_error(400, b'{"error":{"message":"output_config unsupported"}}'),
        json.dumps(_anthropic_ok_body(fenced)).encode(),
    ])

    text, err = fp._call_claude("cluster prompt")

    assert err is None
    assert len(reqs) == 2
    assert "output_config" in _req_body(reqs[0])
    legacy_body = _req_body(reqs[1])
    assert "output_config" not in legacy_body
    assert _sys_text(legacy_body["system"]) == fp._PROPOSE_SYSTEM
    assert legacy_body["model"] == _req_body(reqs[0])["model"]
    # (3) legacy fence-strip still parses the fenced fixture
    assert fp._parse_spec_json(text)["feature_name"] == "Fiber Route Compare"
    assert not so.model_supports_structured("claude-opus-4-8")


def test_proposer_kill_switch_forces_legacy(monkeypatch):
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")
    monkeypatch.setattr(fp, "ANTHROPIC_API_KEY", "test-key")
    raw = json.dumps(_anthropic_ok_body(_SPEC_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [raw])

    _text, err = fp._call_claude("cluster prompt")

    assert err is None
    assert len(reqs) == 1
    body = _req_body(reqs[0])
    assert "output_config" not in body
    assert _sys_text(body["system"]) == fp._PROPOSE_SYSTEM


def test_proposer_unsupported_fallback_model_stays_legacy(monkeypatch):
    """The static fallback rung claude-sonnet-4-0 (Sonnet 4.0) does NOT
    support structured outputs — the per-model gate must keep it legacy even
    with structured mode globally ON."""
    monkeypatch.setattr(fp, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(bm, "brain_model_for",
                        lambda tier="routine": "claude-sonnet-4-0")
    raw = json.dumps(_anthropic_ok_body(_SPEC_JSON)).encode()
    reqs = _patch_urlopen(monkeypatch, [raw])

    _text, err = fp._call_claude("cluster prompt")

    assert err is None
    assert len(reqs) == 1
    assert "output_config" not in _req_body(reqs[0])


def test_proposer_schema_covers_consumer_reads():
    """(5) propose_feature + _stub_module_content read spec.* keys."""
    src = (inspect.getsource(fp.propose_feature)
           + inspect.getsource(fp._stub_module_content))
    props = fp._PROPOSE_SCHEMA["properties"]
    keys = set(re.findall(r'spec\.get\(\s*[\'"]([A-Za-z_]+)[\'"]', src))
    keys |= set(re.findall(r'spec\[[\'"]([A-Za-z_]+)[\'"]\]', src))
    assert keys        # introspection matched something
    for key in keys:
        assert key in props, f"consumer reads spec.{key} — not in schema"
    # the vague-cluster escape contract survives the schema
    assert props["feature_name"]["type"] == ["string", "null"]
    assert "unclear" in props["complexity"]["enum"]
    assert set(fp._PROPOSE_SCHEMA["required"]) == {"feature_name", "complexity"}


# ═════════════════════════════════════════════════════════════════════
# schema hygiene — documented structured-outputs limitations
# ═════════════════════════════════════════════════════════════════════

_FORBIDDEN_KEYWORDS = {"minimum", "maximum", "multipleOf",
                       "minLength", "maxLength"}


def _walk_schema(node, path="$"):
    if isinstance(node, dict):
        bad = _FORBIDDEN_KEYWORDS & set(node.keys())
        assert not bad, f"{path}: unsupported keyword(s) {bad}"
        if "minItems" in node:
            assert node["minItems"] in (0, 1), f"{path}: minItems>1 unsupported"
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, \
                f"{path}: object without additionalProperties:false"
        for k, v in node.items():
            _walk_schema(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_schema(v, f"{path}[{i}]")


@pytest.mark.parametrize("schema", [
    pytest.param(sp._L6_REC_SCHEMA, id="planner_l6"),
    pytest.param(inv._DECOMPOSE_SCHEMA, id="investigator_decompose"),
    pytest.param(inv._REASON_SCHEMA, id="investigator_reason"),
    pytest.param(inv._REFUTE_SCHEMA, id="investigator_refute"),
    pytest.param(fp._PROPOSE_SCHEMA, id="proposer_spec"),
    # ★ A schema absent from this list is not walked at all — the hygiene test
    #   passes vacuously for it. FIX_SCHEMA shipped without
    #   additionalProperties:false and nothing objected, because nothing looked.
    #   Any NEW schema passed to _call_model belongs here in the same PR.
    pytest.param(qap.FIX_SCHEMA, id="qa_superuser_fix"),
])
def test_schema_is_structured_output_safe(schema):
    _walk_schema(schema)
    assert schema["type"] == "object"


def test_investigate_passes_schemas_to_call_model():
    """The wiring itself: investigate() must send each pass's schema."""
    src = inspect.getsource(inv.investigate)
    assert "schema=_DECOMPOSE_SCHEMA" in src
    assert "schema=_REASON_SCHEMA" in src
    assert src.count("schema=_REFUTE_SCHEMA") == 2   # refute + its retry
