"""
tests/test_brain_prompt_cache.py — Anthropic prompt caching on the investigator.

MOCKS urllib entirely — NO network, NO DB. Unlike test_brain_investigator.py
(which patches _call_model away), these tests go THROUGH the real _call_model
and capture the exact wire bodies, because the thing under test IS the request
shape:

  (a) with cache_evidence=True the REASON and REFUTE requests carry system as
      a block LIST whose LAST block has cache_control {"type": "ephemeral"}
      (the cache breakpoint goes on the last stable block);
  (b) the cached block is BYTE-IDENTICAL across consecutive phase calls —
      REASON#1 vs REASON#2 and REFUTE#1 vs REFUTE#2 of a back-to-back batch
      (built from two real investigate() runs, bodies diffed), plus REASON vs
      REFUTE of ONE investigation share the same suffix text. Byte identity is
      the prefix-match precondition: one changed byte = no cache hit;
  (c) volatile values (the operator question, a timestamp, per-question prior
      work) are NOT inside the cached block — they ride the user turn AFTER
      the breakpoint;
  (d) the DEFAULT path (cache_evidence unset) is byte-for-byte the
      pre-caching shape: system is a plain string, no cache_control anywhere;
  (e) the enhancer passes cache_evidence=True only for batches of >=2
      investigations (a single investigation must not pay the 1.25x
      cache-write premium with no reader).
"""
import json

import pytest

inv = pytest.importorskip("routes.brain_investigator")


# ── canned model outputs (keyed off the system prompt, like the API) ──
_DECOMPOSE_JSON = json.dumps({
    "sub_questions": ["Is reach actually flat?"],
    "data_needed": ["weekly external IPs"],
})
_REASON_JSON = json.dumps({
    "recommendation": "Focus on first-touch retention.",
    "reasoning": "Evidence shows flat new IPs.",
    "cited_evidence": ["Tracked facilities: 21000"],
    "confidence": 0.7,
    "caveats": ["Attribution may lose registry traffic."],
    "decision_for_human": "Fund SEO vs connector listings.",
})
_REFUTE_JSON = json.dumps({
    "weaknesses_found": ["IP count is a noisy proxy."],
    "survives_scrutiny": True,
    "confidence_adjustment": 0.0,
    "added_caveats": [],
})

_STUB_EVIDENCE = [
    {"claim": "Tracked facilities", "source": "canonical_stats", "value": 21000},
    {"claim": "Countries", "source": "canonical_stats", "value": 178},
]

# Volatile markers that must NEVER land in the cached block.
_Q_MARKER = "QVOLATILE-123"
_TS_MARKER = "2026-07-04T09:00:00Z"
_PRIOR_MARKER = "PRIORVOLATILE-456"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _flat_system(body: dict) -> str:
    s = body.get("system")
    if isinstance(s, list):
        return " ".join(b.get("text", "") for b in s)
    return s or ""


def _mk_urlopen(captured: list):
    """urlopen replacement: records each decoded request body and answers with
    the canned JSON for whichever phase the system prompt belongs to."""
    def _fake(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append(body)
        sys_flat = _flat_system(body)
        if "DECOMPOSE" in sys_flat:
            text = _DECOMPOSE_JSON
        elif "ADVERSARIAL CRITIC" in sys_flat:
            text = _REFUTE_JSON
        else:
            text = _REASON_JSON
        return _FakeResp({"content": [{"type": "text", "text": text}]})
    return _fake


@pytest.fixture()
def wire(monkeypatch):
    """Full harness: API key set, deterministic models per tier, stubbed
    evidence gather + prior-work recall, urlopen captured. Returns the list
    the request bodies land in."""
    captured: list = []
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")

    # Deterministic tier→model map (reasoning and challenger DIFFER by design
    # — the cross-model challenge; caches are model-scoped so the assertions
    # below never compare across tiers).
    import routes.brain_models as bm
    monkeypatch.setattr(
        bm, "brain_model_for",
        lambda tier="routine": ("model-reasoning" if tier == "reasoning"
                                else "model-challenger"))
    monkeypatch.setattr(bm, "resolve_chain", lambda m, max_depth=5: [m])

    # Evidence: counting stub + a fresh memo per test.
    calls = {"gather": 0}

    def _gather():
        calls["gather"] += 1
        return [dict(e) for e in _STUB_EVIDENCE]

    monkeypatch.setattr(inv, "gather_evidence", _gather)
    monkeypatch.setattr(inv, "_EVIDENCE_MEMO", {"ts": 0.0, "items": None})

    # Prior work varies per question — a volatile that must stay OUT of the
    # cached block.
    monkeypatch.setattr(
        inv, "_recall_prior_work",
        lambda q, k=6: [{"source_table": "brain_findings", "kind": "finding",
                         "text": f"{_PRIOR_MARKER} prior note for: {q}"}])

    monkeypatch.setattr("urllib.request.urlopen", _mk_urlopen(captured))
    return {"captured": captured, "calls": calls}


# ── (a) cache_control on the intended block ─────────────────────────
def test_cached_mode_marks_last_system_block(wire):
    out = inv.investigate("Why is reach flat?", cache_evidence=True)
    assert "cannot_investigate" not in out

    captured = wire["captured"]
    assert len(captured) == 3  # DECOMPOSE, REASON, REFUTE (no retry)

    # DECOMPOSE: unchanged — plain system string, no cache_control anywhere.
    dec = captured[0]
    assert isinstance(dec["system"], str)
    assert "cache_control" not in json.dumps(dec)

    # REASON + REFUTE: system is [stable instruction block, evidence block],
    # breakpoint on the LAST block only.
    for body in (captured[1], captured[2]):
        sys_blocks = body["system"]
        assert isinstance(sys_blocks, list) and len(sys_blocks) == 2
        assert "cache_control" not in sys_blocks[0]
        assert sys_blocks[1]["cache_control"] == {"type": "ephemeral"}
        # The evidence actually lives in the cached block…
        assert "21000" in sys_blocks[1]["text"]
        assert "EVIDENCE (ground-truth" in sys_blocks[1]["text"]
        # …and no longer in the user turn.
        assert "21000" not in body["messages"][0]["content"]


# ── (b) byte-identical cached prefix across consecutive calls ────────
def test_cached_prefix_byte_identical_across_batch(wire):
    """Two back-to-back investigations (the enhancer-batch shape): the full
    system prefix of REASON#2 must equal REASON#1's byte-for-byte on the same
    model, ditto REFUTE — otherwise the cache never hits and the marker is a
    1.25x write premium for nothing."""
    inv.investigate("Why is reach flat?", cache_evidence=True)
    inv.investigate("Is conversion working?", cache_evidence=True)

    captured = wire["captured"]
    assert len(captured) == 6

    reason1, reason2 = captured[1], captured[4]
    refute1, refute2 = captured[2], captured[5]

    # Same model per phase (cache is model-scoped).
    assert reason1["model"] == reason2["model"] == "model-reasoning"
    assert refute1["model"] == refute2["model"] == "model-challenger"

    # Byte-level diff of the cached prefix (serialize and compare).
    assert json.dumps(reason1["system"]) == json.dumps(reason2["system"])
    assert json.dumps(refute1["system"]) == json.dumps(refute2["system"])

    # Within ONE investigation, REASON and REFUTE carry the same suffix text
    # (different models → separate caches, but the block construction is one
    # code path).
    assert reason1["system"][1]["text"] == refute1["system"][1]["text"]

    # The memo made the second gather a no-op — that is WHY the bytes match.
    assert wire["calls"]["gather"] == 1

    # The user turns DO differ (the volatiles ride there).
    assert reason1["messages"] != reason2["messages"]


def test_refute_retry_resends_identical_body(wire, monkeypatch):
    """The REFUTE transient-error retry must re-send a byte-identical body —
    the one guaranteed same-model cache read within a single investigation."""
    captured = wire["captured"]
    state = {"refute_seen": 0}
    orig = _mk_urlopen(captured)

    def _flaky(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        if "ADVERSARIAL CRITIC" in _flat_system(body):
            state["refute_seen"] += 1
            if state["refute_seen"] == 1:
                captured.append(body)
                raise OSError("read timed out")
        return orig(req, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", _flaky)
    out = inv.investigate("Why is reach flat?", cache_evidence=True)
    assert out["refutation"]["attempted"] is True

    # bodies: DECOMPOSE, REASON, REFUTE(fail), REFUTE(retry)
    assert len(captured) == 4
    assert captured[2] == captured[3]  # identical bytes → guaranteed cache read


# ── (c) volatiles stay OUT of the cached block ───────────────────────
def test_volatiles_not_in_cached_block(wire):
    q = f"Why is reach flat as of {_TS_MARKER}? ({_Q_MARKER})"
    inv.investigate(q, cache_evidence=True)

    captured = wire["captured"]
    for body in (captured[1], captured[2]):  # REASON, REFUTE
        cached_text = body["system"][1]["text"]
        user_text = body["messages"][0]["content"]
        for marker in (_Q_MARKER, _TS_MARKER):
            assert marker not in cached_text, marker
            assert marker in user_text, marker
    # Prior work (varies per question) rides the REASON user turn only.
    reason = captured[1]
    assert _PRIOR_MARKER not in reason["system"][1]["text"]
    assert _PRIOR_MARKER in reason["messages"][0]["content"]


# ── (d) default path is byte-for-byte the pre-caching shape ──────────
def test_default_path_has_no_cache_control(wire):
    inv.investigate("Why is reach flat?")

    captured = wire["captured"]
    assert len(captured) == 3
    for body in captured:
        assert isinstance(body["system"], str)
        assert "cache_control" not in json.dumps(body)
    # Evidence still rides the REASON user turn, exactly as before.
    assert "21000" in captured[1]["messages"][0]["content"]
    # Default path does NOT memoize — both callers would re-gather.
    assert wire["calls"]["gather"] == 1


def test_evidence_memo_ttl_and_nonempty_only(monkeypatch):
    monkeypatch.setattr(inv, "_EVIDENCE_MEMO", {"ts": 0.0, "items": None})
    calls = {"n": 0}

    def _gather():
        calls["n"] += 1
        return [] if calls["n"] == 1 else list(_STUB_EVIDENCE)

    monkeypatch.setattr(inv, "gather_evidence", _gather)
    # Empty gather is NOT memoized (a DB blip must not pin an empty block)…
    assert inv.gather_evidence_memoized() == []
    # …the next call re-gathers and memoizes the non-empty result…
    first = inv.gather_evidence_memoized()
    assert first == _STUB_EVIDENCE
    # …and within the TTL the SAME object comes back (byte identity).
    assert inv.gather_evidence_memoized() is first
    assert calls["n"] == 2


# ── (e) the enhancer only opts in for real batches ───────────────────
def test_enhancer_passes_cache_flag_only_for_batches(monkeypatch):
    enh = pytest.importorskip("routes.brain_enhancer")
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)

    seen: list = []

    def _fake_investigate(question, *, depth="default", cache_evidence=False):
        seen.append(cache_evidence)
        return {"question": question, "recommendation": "do X",
                "confidence": 0.6, "caveats": [], "decision_for_human": "d",
                "refutation": {"attempted": True, "survived": True},
                "model": "m"}

    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "investigate", _fake_investigate)

    # Batch of 2 → caching ON for every investigation in the batch.
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [
        {"area": "a", "signal": "s1", "question": "q one"},
        {"area": "b", "signal": "s2", "question": "q two"},
    ])
    out = enh.propose_enhancements(max_proposals=3)
    assert "cannot_enhance" not in out
    assert seen == [True, True]

    # Single opportunity → caching OFF (write premium with no reader).
    seen.clear()
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [
        {"area": "a", "signal": "s1", "question": "q solo"},
    ])
    out = enh.propose_enhancements(max_proposals=3)
    assert "cannot_enhance" not in out
    assert seen == [False]
