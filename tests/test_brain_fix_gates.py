"""tests/test_brain_fix_gates.py — r-escalation-ladder (2026-07-18).

The evidence-gated escalation ladder (spec: docs/brain-pr-escalation-gates.md):
Observation → Proposal → Candidate → Draft PR, where Candidate → Draft PR is
the ONLY automated step and it is governed by four positive gates, six hard
blocks, and the exact confidence formula

    0.30*repeatability + 0.25*evaluator_agreement + 0.20*deterministic_evidence
    + 0.15*measured_improvement + 0.10*locality        (auto-open >= 0.85)

Covers:
  · the PURE gate math — each gate pass/fail with reasons, each hard block,
    formula exactness to 4 decimals, the >= threshold edge, determinism,
    junk-input resilience;
  · locality / text screens (classify_path, text_flags, surfaces_from_text)
    and evidence extraction (extract_evidence_kinds);
  · brain_pr_opener.open_spec_pr enforcement + body embedding (mocked GitHub,
    SPEC-ONLY marker + fingerprint-first-line conventions intact);
  · brain_self_director wiring — a passing eval_finding files the gated DRAFT
    spec PR; a failing one stays agenda-only with the verdict stored in the
    agenda row; non-eval_finding kinds never touch the ladder;
  · the verdict log + feedback stub degrade to no-ops with no DB.

FULLY MOCKED GitHub + DB — no network, no Postgres, never imports main.py.
Run:  python3 -m pytest tests/test_brain_fix_gates.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_fix_gates as fg          # noqa: E402
import routes.brain_pr_opener as opener      # noqa: E402
import routes.brain_self_director as sd      # noqa: E402


@pytest.fixture(autouse=True)
def _seal_landed_spec_scan(monkeypatch):
    """★ Hermetic seal (2026-08-16) — open_spec_pr consults
    landed_spec_with_fingerprint, which scans the REAL docs/brain-proposals/
    tree, so any test here that files a spec is silently coupled to live repo
    content. The condition fingerprint is number-INSENSITIVE by design (a
    canon bump must not re-file), so a brain-filed spec only has to restate a
    fixture's condition — at any count — to flip that fixture's 'no dup'
    premise and turn `acted is True` red on main. That is exactly how #2745
    ('320 DCPI markets') broke test_brain_spec_lifecycle.py and blocked every
    PR's required unit-tests (fixed in #2750).

    AUTOUSE on purpose: sealing per-test only fixes the tests that exist
    today, and the next one written here inherits the trap. This file's own
    header promises FULLY MOCKED GitHub + DB — the docs tree is that same
    kind of live input, so it is pinned file-wide.

    The real scan is covered where it belongs and by design, against a
    fingerprint discovered FROM the tree rather than a hardcoded fixture:
    tests/test_brain_heal_fix_learn_rewire.py::
    test_landed_spec_dedup_finds_a_real_merged_fingerprint. Do not seal that.
    """
    monkeypatch.setattr(opener, "landed_spec_with_fingerprint",
                        lambda fp: None)
    yield


def _passing_candidate(**over):
    """A candidate that clears every gate and every hard block."""
    c = {
        "repeat_count": 3,
        "distinct_sources": 3,
        "evidence": ["schema_failure", "http_repro"],
        "files_touched": ["docs/brain-proposals/spec.md",
                          "schemas/envelope.json"],
        "expected_improvement": 1.0,
        "recent_5xx_rate": 0.0,
        "conflicting_recs": False,
        "removes_or_renames_fields": False,
    }
    c.update(over)
    return c


# ════════════════════════════════════════════════════════════════════
#  Confidence formula — exactness
# ════════════════════════════════════════════════════════════════════
def test_confidence_formula_all_ones_is_exactly_1():
    v = fg.evaluate_escalation(_passing_candidate())
    assert v["confidence"] == pytest.approx(1.0)
    assert v["components"] == {
        "repeatability": 1.0, "evaluator_agreement": 1.0,
        "deterministic_evidence": 1.0, "measured_improvement": 1.0,
        "locality": 1.0}


def test_confidence_formula_mixed_case_exact_to_4dp():
    """repeat=3 (.30·1) + sources=2 (.25·2/3) + 1 kind (.20·0.5)
    + imp=0.5 (.15·0.5) + local (.10·1) = 0.7417 (rounded to 4dp)."""
    v = fg.evaluate_escalation(_passing_candidate(
        distinct_sources=2, evidence=["ci_failure"],
        expected_improvement=0.5))
    expected = round(0.30 * 1.0 + 0.25 * (2 / 3.0) + 0.20 * 0.5
                     + 0.15 * 0.5 + 0.10 * 1.0, 4)
    assert v["confidence"] == expected == 0.7417


def test_confidence_components_are_capped_at_1():
    """repeat_count 30 / 7 sources / 4 evidence kinds / imp 5.0 must not push
    any component (or the total) above 1.0."""
    v = fg.evaluate_escalation(_passing_candidate(
        repeat_count=30, distinct_sources=7,
        evidence=sorted(fg.DETERMINISTIC_EVIDENCE_KINDS),
        expected_improvement=5.0))
    assert v["confidence"] == pytest.approx(1.0)
    assert all(c <= 1.0 for c in v["components"].values())


def test_threshold_edge_geq_passes_gt_blocks():
    """The auto-open rule is confidence >= threshold: exactly-at passes;
    one tick above the confidence does not."""
    cand = _passing_candidate(distinct_sources=2, evidence=["ci_failure"],
                              expected_improvement=0.5)   # confidence 0.7417
    at = fg.evaluate_escalation(cand, threshold=0.7417)
    above = fg.evaluate_escalation(cand, threshold=0.7418)
    assert at["state"] == "draft_pr" and at["auto_escalate"] is True
    assert above["state"] != "draft_pr" and above["auto_escalate"] is False


def test_default_threshold_is_085():
    assert fg.evaluate_escalation(_passing_candidate())["threshold"] \
        == pytest.approx(0.85)


# ════════════════════════════════════════════════════════════════════
#  Positive gates — each pass/fail
# ════════════════════════════════════════════════════════════════════
def test_all_gates_pass_on_the_passing_candidate():
    v = fg.evaluate_escalation(_passing_candidate())
    assert v["state"] == "draft_pr"
    assert v["hard_blocks"] == []
    assert all(g["passed"] for g in v["gates"].values())
    assert set(v["gates"]) == {"stability", "verdict_diff",
                               "deterministic_evidence", "contract_locality"}


def test_stability_gate_fails_below_3_repeats():
    v = fg.evaluate_escalation(_passing_candidate(repeat_count=2))
    g = v["gates"]["stability"]
    assert g["passed"] is False and "repeat_count 2" in g["reason"]
    assert v["state"] != "draft_pr"


def test_verdict_diff_gate_fails_without_a_metric():
    """No measurable expected improvement → stays a proposal (spec gate 2)."""
    v = fg.evaluate_escalation(_passing_candidate(expected_improvement=None))
    g = v["gates"]["verdict_diff"]
    assert g["passed"] is False and "no measurable" in g["reason"]
    assert v["state"] != "draft_pr"
    # zero improvement is not an improvement either
    v0 = fg.evaluate_escalation(_passing_candidate(expected_improvement=0.0))
    assert v0["gates"]["verdict_diff"]["passed"] is False


def test_deterministic_evidence_gate_feels_cleaner_never_escalates():
    v = fg.evaluate_escalation(_passing_candidate(evidence=[]))
    g = v["gates"]["deterministic_evidence"]
    assert g["passed"] is False and "never escalates" in g["reason"]
    assert v["state"] != "draft_pr"
    # unknown kinds don't count as evidence
    v2 = fg.evaluate_escalation(
        _passing_candidate(evidence=["feels_cleaner", "vibes"]))
    assert v2["gates"]["deterministic_evidence"]["passed"] is False


def test_contract_locality_gate_fails_on_code_paths():
    """routes/*.py is neither contract-local nor policy — unclassifiable
    surfaces get a human."""
    v = fg.evaluate_escalation(_passing_candidate(
        files_touched=["routes/brain_rag.py"]))
    g = v["gates"]["contract_locality"]
    assert g["passed"] is False and "routes/brain_rag.py" in g["reason"]
    assert v["state"] != "draft_pr"
    assert v["components"]["locality"] == 0.0


def test_contract_locality_gate_fails_on_empty_file_list():
    v = fg.evaluate_escalation(_passing_candidate(files_touched=[]))
    assert v["gates"]["contract_locality"]["passed"] is False


# ════════════════════════════════════════════════════════════════════
#  Hard blocks — each one prevents auto-PR and caps the state
# ════════════════════════════════════════════════════════════════════
def _block_names(v):
    return {b["name"] for b in v["hard_blocks"]}


def test_hard_block_active_5xx_instability():
    v = fg.evaluate_escalation(_passing_candidate(recent_5xx_rate=0.5))
    assert "active_5xx_instability" in _block_names(v)
    assert v["state"] == "proposal"          # capped despite perfect gates
    assert v["auto_escalate"] is False


def test_hard_block_5xx_rate_at_default_threshold_does_not_fire():
    v = fg.evaluate_escalation(_passing_candidate(recent_5xx_rate=0.05))
    assert "active_5xx_instability" not in _block_names(v)


def test_hard_block_conflicting_recommendations():
    v = fg.evaluate_escalation(_passing_candidate(conflicting_recs=True))
    assert "conflicting_recommendations" in _block_names(v)
    assert v["state"] == "proposal"


def test_hard_block_business_policy_surface():
    v = fg.evaluate_escalation(_passing_candidate(
        files_touched=["docs/brain-proposals/spec.md", "policy:pricing"]))
    assert "business_policy_surface" in _block_names(v)
    assert v["state"] == "proposal"
    # and the same surface also fails the locality gate
    assert v["gates"]["contract_locality"]["passed"] is False


def test_hard_block_breaking_change():
    v = fg.evaluate_escalation(
        _passing_candidate(removes_or_renames_fields=True))
    assert "breaking_change" in _block_names(v)
    assert v["state"] == "proposal"


def test_hard_block_low_confidence_provenance_one_model_one_run():
    v = fg.evaluate_escalation(_passing_candidate(
        repeat_count=1, distinct_sources=1))
    assert "low_confidence_provenance" in _block_names(v)
    assert v["state"] == "proposal"
    # two runs or a second source clears the provenance block
    v2 = fg.evaluate_escalation(_passing_candidate(
        repeat_count=2, distinct_sources=1))
    assert "low_confidence_provenance" not in _block_names(v2)


# ════════════════════════════════════════════════════════════════════
#  The ladder states
# ════════════════════════════════════════════════════════════════════
def test_state_observation_on_no_signal():
    v = fg.evaluate_escalation({})
    assert v["state"] == "observation"
    assert v["auto_escalate"] is False


def test_state_proposal_on_single_evaluation():
    v = fg.evaluate_escalation({
        "repeat_count": 1, "distinct_sources": 1,
        "files_touched": ["docs/x.md"]})
    assert v["state"] == "proposal"


def test_state_candidate_when_stable_and_evidenced_but_below_threshold():
    """Stability + deterministic evidence + >=2 agreeing sources but no
    measured improvement → Candidate (queue for synthesis), not Draft PR."""
    v = fg.evaluate_escalation(_passing_candidate(
        distinct_sources=2, expected_improvement=None))
    assert v["state"] == "candidate"
    assert v["auto_escalate"] is False


def test_evaluate_is_deterministic():
    a = fg.evaluate_escalation(_passing_candidate())
    b = fg.evaluate_escalation(_passing_candidate())
    assert a == b


def test_evaluate_never_raises_on_junk_input():
    for junk in (None, {}, {"repeat_count": "lots", "evidence": "nope",
                            "expected_improvement": "much",
                            "recent_5xx_rate": "spiky",
                            "files_touched": None}):
        v = fg.evaluate_escalation(junk)   # must not raise
        assert v["state"] in ("observation", "proposal", "candidate",
                              "draft_pr")
        assert 0.0 <= v["confidence"] <= 1.0


# ════════════════════════════════════════════════════════════════════
#  Locality / text screens + evidence extraction
# ════════════════════════════════════════════════════════════════════
def test_classify_path_tiers():
    assert fg.classify_path("docs/brain-proposals/spec.md") == "local"
    assert fg.classify_path("schemas/envelope.json") == "local"
    assert fg.classify_path(".github/workflows/deploy.yml") == "local"
    assert fg.classify_path("openapi.yaml") == "local"
    assert fg.classify_path("routes/pricing_routes.py") == "human_only"
    assert fg.classify_path("policy:quota") == "human_only"
    assert fg.classify_path("routes/brain_rag.py") == "unknown"
    # token match, not substring: 'author' must NOT trip 'auth'
    assert fg.classify_path("docs/author-guide.md") == "local"


def test_text_flags_business_keywords_and_breaking_language():
    f = fg.text_flags("Raise the free-tier quota and simplify pricing")
    assert "policy:quota" in f["human_only_surfaces"]
    assert "policy:pricing" in f["human_only_surfaces"]
    assert f["breaking"] is False
    f2 = fg.text_flags("Rename the provenance field to `origin`")
    assert f2["breaking"] is True
    f3 = fg.text_flags("Add a machine-readable error envelope to responses")
    assert f3["human_only_surfaces"] == [] and f3["breaking"] is False
    assert fg.surfaces_from_text("adjust ranking weights") == ["policy:ranking"]


def test_extract_evidence_kinds_explicit_signals_only():
    assert fg.extract_evidence_kinds({
        "schema_failures": 2, "ci_failures": 0,
        "deterministic_evidence": ["http_repro", "feels_cleaner"],
    }) == ["http_repro", "schema_failure"]
    assert fg.extract_evidence_kinds({"top_structural_gap": "prose only"}) == []
    assert fg.extract_evidence_kinds(None) == []
    assert fg.extract_evidence_kinds({"benchmark_delta": 0.12}) == ["benchmark"]


# ════════════════════════════════════════════════════════════════════
#  Verdict log + feedback stub — degrade to no-ops with no DB
# ════════════════════════════════════════════════════════════════════
def test_verdict_log_noops_without_db(monkeypatch):
    monkeypatch.setattr(fg, "_conn", lambda: None)
    assert fg.record_gate_verdict("fp", {"state": "proposal",
                                         "confidence": 0.2}) is None
    assert fg.grade_prior_verdicts("fp") == 0
    assert fg.grade_prior_verdicts("") == 0
    assert fg.grade_prior_verdicts(None) == 0


# ════════════════════════════════════════════════════════════════════
#  brain_pr_opener.open_spec_pr — enforcement + body embedding
# ════════════════════════════════════════════════════════════════════
class _FakeGhResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_open_spec_pr_refuses_a_below_bar_verdict(monkeypatch):
    """A non-draft_pr verdict must never reach GitHub — defense in depth even
    if a caller wires the plumbing wrong."""
    import routes.brain_guardrails as guard
    monkeypatch.setattr(guard, "can_open_pr", lambda: (True, "ok"))
    monkeypatch.setattr(
        opener, "_gh",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("GitHub must not be touched on a gated refusal")))
    verdict = fg.evaluate_escalation(_passing_candidate(repeat_count=1,
                                                        distinct_sources=1))
    assert verdict["state"] != "draft_pr"
    res = opener.open_spec_pr("do the thing", "heading", "agenda", 7,
                              gate_verdict=verdict)
    assert res["ok"] is True and res["acted"] is False
    assert res["gated"] is True
    assert res["gate_state"] == verdict["state"]
    assert "low_confidence_provenance" in res["hard_blocks"]


def test_open_spec_pr_embeds_a_passing_verdict_in_the_body(monkeypatch):
    """A passing verdict files the draft spec PR with the gate verdict +
    confidence + evidence summary in the body; the fingerprint stays the FIRST
    line and the SPEC-ONLY marker convention survives."""
    import routes.brain_guardrails as guard
    monkeypatch.setattr(guard, "can_open_pr", lambda: (True, "ok"))
    created = {}

    def fake_gh(method, path, body=None):
        if method == "GET" and "pulls?state=open" in path:
            return _FakeGhResp(200, [])
        if method == "GET" and "/git/refs/heads/main" in path:
            return _FakeGhResp(200, {"object": {"sha": "abc123"}})
        if method == "POST" and path.endswith("/git/refs"):
            return _FakeGhResp(201, {})
        if method == "PUT" and "/contents/" in path:
            return _FakeGhResp(201, {})
        if method == "POST" and path.endswith("/pulls"):
            created.update(body or {})
            return _FakeGhResp(201, {"number": 4243, "html_url": "x"})
        raise AssertionError(f"unexpected GitHub call: {method} {path}")

    monkeypatch.setattr(opener, "_gh", fake_gh)
    verdict = fg.evaluate_escalation(_passing_candidate())
    assert verdict["state"] == "draft_pr"
    res = opener.open_spec_pr("Add the machine-readable error envelope",
                              "agent-eval: envelope ask", "agenda", 9,
                              label="escalation-gated",
                              gate_verdict=verdict)
    assert res["acted"] is True
    body = created.get("body") or ""
    assert body.startswith("<!-- fingerprint:")          # stamp stays line 1
    assert "**SPEC-ONLY**" in body                       # r-spec-honesty
    assert "## Escalation gate verdict" in body
    assert f"**Confidence:** {verdict['confidence']}" in body
    assert "gate `stability`: **PASS**" in body
    assert "hard blocks: none" in body
    assert "repeat_count 3" in body
    assert created.get("draft") is True                  # humans still merge


def test_open_spec_pr_without_verdict_keeps_prelader_behavior(monkeypatch):
    """Callers that pass no gate_verdict (human-approved directives) are
    untouched: no gate section, body otherwise as before."""
    import routes.brain_guardrails as guard
    monkeypatch.setattr(guard, "can_open_pr", lambda: (True, "ok"))
    created = {}

    def fake_gh(method, path, body=None):
        if method == "GET" and "pulls?state=open" in path:
            return _FakeGhResp(200, [])
        if method == "GET" and "/git/refs/heads/main" in path:
            return _FakeGhResp(200, {"object": {"sha": "abc123"}})
        if method == "POST" and path.endswith("/git/refs"):
            return _FakeGhResp(201, {})
        if method == "PUT" and "/contents/" in path:
            return _FakeGhResp(201, {})
        if method == "POST" and path.endswith("/pulls"):
            created.update(body or {})
            return _FakeGhResp(201, {"number": 4244, "html_url": "x"})
        raise AssertionError(f"unexpected GitHub call: {method} {path}")

    monkeypatch.setattr(opener, "_gh", fake_gh)
    res = opener.open_spec_pr("human approved directive", "a heading",
                              "prop", 11, label="prop #11")
    assert res["acted"] is True
    assert "## Escalation gate verdict" not in (created.get("body") or "")


# ════════════════════════════════════════════════════════════════════
#  brain_self_director wiring
# ════════════════════════════════════════════════════════════════════
def _eval_item(**ge_over):
    ge = {"repeat_count": 3, "distinct_sources": 3,
          "evidence": ["schema_failure", "http_repro"],
          "expected_improvement": 0.4, "recent_5xx_rate": 0.0,
          "conflicting_recs": False}
    ge.update(ge_over)
    return {"kind": "eval_finding", "title": "agent-eval: openai's #1 ask",
            "question": "What single change addresses the flagged gap?",
            "area": "developer_ux", "leverage": 1.2,
            "source": "model_relations_runs", "fingerprint": "fp-123",
            "gate_evidence": ge}


def _result():
    return {"question": "q", "confidence": 0.7,
            "recommendation": "Publish the error envelope schema in the docs"}


@pytest.fixture()
def _tick_env(monkeypatch):
    """Happy-path tick plumbing with all externals mocked."""
    import routes.brain_investigator as bi
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 0)
    monkeypatch.setattr(bi, "investigate", lambda q, *a, **k: _result())
    # neutralize the verdict log's DB
    monkeypatch.setattr(fg, "_conn", lambda: None)
    yield


def test_tick_files_gated_spec_pr_when_ladder_passes(monkeypatch, _tick_env):
    item = _eval_item()
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: item)
    stored = []
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda i, r: stored.append((i, r)) or 777)
    opened = []
    monkeypatch.setattr(
        opener, "open_spec_pr",
        lambda directive, heading="", kind="item", item_id=0, label="",
        gate_verdict=None: opened.append(
            {"directive": directive, "kind": kind, "item_id": item_id,
             "gate_verdict": gate_verdict})
        or {"ok": True, "acted": True, "spec_pr": True,
            "pr": {"number": 1700, "url": "u"}})

    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert out["escalation_state"] == "draft_pr"
    assert out["escalation_pr"] == {"number": 1700, "url": "u"}
    # the PR call carried the verdict + the agenda id
    assert opened and opened[0]["gate_verdict"]["state"] == "draft_pr"
    assert opened[0]["kind"] == "agenda" and opened[0]["item_id"] == 777
    assert opened[0]["directive"].startswith("Publish the error envelope")
    # the stored agenda row carries the verdict too
    _, stored_result = stored[0]
    assert stored_result["escalation"]["state"] == "draft_pr"


def test_tick_stays_agenda_only_below_the_bar(monkeypatch, _tick_env):
    """Weak provenance (1 run, 1 source) → hard-blocked → NO PR call; the
    verdict (with the why) is stored in the agenda row."""
    item = _eval_item(repeat_count=1, distinct_sources=1)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: item)
    stored = []
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda i, r: stored.append((i, r)) or 778)
    monkeypatch.setattr(
        opener, "open_spec_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("below-bar verdict must not reach open_spec_pr")))

    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert out["escalation_state"] == "proposal"
    assert "escalation_pr" not in out
    _, stored_result = stored[0]
    esc = stored_result["escalation"]
    assert esc["state"] == "proposal"
    assert any(b["name"] == "low_confidence_provenance"
               for b in esc["hard_blocks"])


def test_tick_active_5xx_hard_blocks_escalation(monkeypatch, _tick_env):
    item = _eval_item(recent_5xx_rate=0.4)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: item)
    monkeypatch.setattr(sd, "_store_agenda", lambda i, r: 779)
    monkeypatch.setattr(
        opener, "open_spec_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("5xx-blocked verdict must not reach open_spec_pr")))
    out = sd.self_direct_tick()
    assert out["ran"] is True and out["escalation_state"] == "proposal"


def test_tick_business_policy_text_hard_blocks(monkeypatch, _tick_env):
    """A recommendation that touches pricing stays human-only even with
    perfect evidence."""
    item = _eval_item()
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: item)
    import routes.brain_investigator as bi
    monkeypatch.setattr(
        bi, "investigate",
        lambda q, *a, **k: {"question": q, "confidence": 0.9,
                            "recommendation": "Change the pricing tiers"})
    stored = []
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda i, r: stored.append((i, r)) or 780)
    monkeypatch.setattr(
        opener, "open_spec_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("policy surface must not reach open_spec_pr")))
    out = sd.self_direct_tick()
    assert out["escalation_state"] == "proposal"
    esc = stored[0][1]["escalation"]
    assert any(b["name"] == "business_policy_surface"
               for b in esc["hard_blocks"])


def test_tick_non_eval_finding_kinds_never_touch_the_ladder(
        monkeypatch, _tick_env):
    """work_plan/opportunity items keep the exact pre-ladder behavior: one
    store, no escalation keys, no gate evaluation."""
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: {
        "kind": "opportunity", "title": "t", "question": "q",
        "area": "reliability", "leverage": 1.0, "source": "s"})
    monkeypatch.setattr(sd, "_store_agenda", lambda i, r: 781)
    monkeypatch.setattr(
        fg, "evaluate_escalation",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ladder must not run for non-eval_finding kinds")))
    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert "escalation_state" not in out


def test_tick_kill_switch_disables_the_ladder(monkeypatch, _tick_env):
    monkeypatch.setenv("BRAIN_ESCALATION_LADDER", "0")
    monkeypatch.setattr(sd, "pick_agenda_item", _eval_item)
    monkeypatch.setattr(sd, "_store_agenda", lambda i, r: 782)
    monkeypatch.setattr(
        opener, "open_spec_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("killed ladder must not reach open_spec_pr")))
    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert "escalation_state" not in out


def test_tick_survives_a_ladder_explosion(monkeypatch, _tick_env):
    """The tick NEVER raises: an exploding gate degrades to agenda-only."""
    monkeypatch.setattr(sd, "pick_agenda_item", _eval_item)
    monkeypatch.setattr(sd, "_store_agenda", lambda i, r: 783)
    monkeypatch.setattr(
        sd, "_evidence_gated_escalation",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gate boom")))
    out = sd.self_direct_tick()      # must not raise
    assert out["ran"] is True
    assert "escalation_state" not in out


def test_feedback_stub_grades_prior_verdicts_on_refire(monkeypatch, _tick_env):
    """The spec's feedback loop: when a condition re-fires, prior gated-PR
    verdicts for the same fingerprint are graded recurred_after_pr BEFORE the
    new verdict is logged (so a fresh row never grades itself)."""
    item = _eval_item(repeat_count=1, distinct_sources=1)  # stays agenda-only
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: item)
    monkeypatch.setattr(sd, "_store_agenda", lambda i, r: 784)
    calls = []
    monkeypatch.setattr(fg, "grade_prior_verdicts",
                        lambda fp: calls.append(("grade", fp)) or 1)
    monkeypatch.setattr(
        fg, "record_gate_verdict",
        lambda fp, v, pr_number=None, agenda_id=None:
        calls.append(("record", fp, pr_number, agenda_id)) or 42)
    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert calls[0] == ("grade", "fp-123")
    assert calls[1] == ("record", "fp-123", None, 784)


def test_gate_candidate_maps_evidence_and_text_surfaces():
    cand = sd._gate_candidate(_eval_item(), _result())
    assert cand["repeat_count"] == 3
    assert cand["distinct_sources"] == 3
    assert cand["evidence"] == ["schema_failure", "http_repro"]
    assert cand["files_touched"] == ["docs/brain-proposals/spec.md"]
    assert cand["expected_improvement"] == 0.4
    # a pricing ask grows a policy pseudo-surface
    cand2 = sd._gate_candidate(
        _eval_item(), {"recommendation": "rework pricing for the free tier"})
    assert "policy:pricing" in cand2["files_touched"]
    # breaking language flips the compat flag
    cand3 = sd._gate_candidate(
        _eval_item(), {"recommendation": "remove the provenance field"})
    assert cand3["removes_or_renames_fields"] is True
