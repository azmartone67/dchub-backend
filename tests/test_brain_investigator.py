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
    def caller(system, prompt, *, tier="reasoning", max_tokens=1500, **kwargs):
        # **kwargs absorbs the structured-outputs `schema=` kwarg (2026-07-04)
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
    """Stub the evidence gatherers so the chain touches neither DB nor repo.

    ★ investigate() assembles `gather_targeted_evidence(question) +
    gather_evidence()`. Stubbing only the latter was sufficient until #2906
    ("give the investigator the CODE it is asked to patch") made
    gather_targeted_evidence start from _source_evidence(question), which
    resolves real repo source via brain_source_map. That returns hits for a
    question with no path in it, so real source rows appeared ahead of the
    stub rows and `out["evidence"] == _STUB_EVIDENCE` began failing — on main,
    for every PR, because unit-tests only runs on pull_request and main
    therefore never reported it red."""
    monkeypatch.setattr(inv, "gather_evidence", lambda: list(_STUB_EVIDENCE))
    monkeypatch.setattr(inv, "gather_targeted_evidence", lambda _q: [])


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

    def caller(system, prompt, *, tier="reasoning", max_tokens=1500, **kwargs):
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

    def caller(system, prompt, *, tier="reasoning", max_tokens=1500, **kwargs):
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
        # tool_calls_7d = GROSS (incl loop/selfheal/probe) — must NOT be cited.
        "tool_calls_7d": 35021,
        "tool_calls_7d_incl_loops": 35021,
        # tool_calls_7d_real = HONEST de-looped external — the one to cite.
        "tool_calls_7d_real": 9154,
        "tool_calls_30d_real": 38000,
        "dev_keys_by_tier": {"free": 400, "paid": 10, "enterprise": 2},
    },
    # Weekly de-looped trend — lets the brain judge a real decline.
    "calls_week_trend": {
        "last_week_calls": 9154,
        "trailing_4wk_avg_calls": 9500.0,
        "delta_pct": -3.6,
        "last_week_distinct_callers": 42,
    },
    "calls_by_week": [
        {"week_start": "2026-05-26", "calls": 9800, "distinct_callers": 50},
        {"week_start": "2026-06-02", "calls": 9500, "distinct_callers": 48},
        {"week_start": "2026-06-09", "calls": 9400, "distinct_callers": 45},
        {"week_start": "2026-06-16", "calls": 9154, "distinct_callers": 42},
    ],
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
    # ★2026-08-09 ORDER-DEPENDENCE, found by a new test importing routes.ai_reach.
    # gather_growth_funnel reads reach via `from routes import ai_reach` — the
    # ATTRIBUTE on the `routes` package, not a sys.modules lookup. Importing a
    # submodule anywhere sets that attribute, and setitem(sys.modules, ...) does
    # NOT override it. So this stub only worked while nothing else in the suite
    # had ever imported routes.ai_reach: the moment something did, the real
    # module won, _cache was empty, and these tests failed — with the fault
    # landing on whoever added the import, not here. Patch BOTH bindings so the
    # stub holds regardless of collection order.
    import importlib
    _routes_pkg = importlib.import_module("routes")
    monkeypatch.setattr(_routes_pkg, "ai_reach", reach_mod, raising=False)
    monkeypatch.setattr(_routes_pkg, "funnel_health", fh, raising=False)


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
    # Distinct ACTIVE callers 30d (free+paid) — relabeled from the misleading
    # "paid users" (it counts non-internal api_keys, not paying accounts).
    assert _val("active mcp callers") == 9

    # HONEST de-looped tool calls 7d — the brain must cite the EXTERNAL number
    # (9,154), NOT the gross mcp_call_log count (35,021) that lumps in our own
    # loop/selfheal/probe traffic. The whole point of the reconciliation.
    tc_items = [it for it in items
                if "tool calls in last 7 days" in it["claim"].lower()]
    assert len(tc_items) == 1, "exactly one 7d-calls evidence item expected"
    assert tc_items[0]["value"] == 9154, (
        "must cite de-looped tool_calls_7d_real (9154), not the inflated "
        "mcp_call_log count (35021)")
    assert "de-looped" in tc_items[0]["claim"].lower()
    # The gross 35,021 must NOT appear as a value anywhere.
    assert all(it["value"] != 35021 for it in items)

    # WEEKLY TREND evidence — so the brain can judge a decline, not just an
    # absolute. The "this week vs trailing 4-week avg" item must be present
    # with the de-looped last-week value and a delta in the claim text.
    trend_items = [it for it in items
                   if "trailing 4-week avg" in it["claim"].lower()]
    assert len(trend_items) == 1, "exactly one weekly-trend evidence item"
    assert trend_items[0]["value"] == 9154
    assert "-3.6%" in trend_items[0]["claim"]
    assert "de-looped" in trend_items[0]["claim"].lower()


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


# ── retention cohorts (key-based funnel retention) ──────────────────
# Canned cohort items shaped exactly like gather_retention_cohorts() emits.
_FAKE_COHORT_ITEMS = [
    {"claim": "Distinct NEW MCP keys (first-ever call) in last 30d",
     "source": "mcp_call_log (DISTINCT api_key, de-looped)", "value": 74},
    {"claim": "Return rate (new key made a 2nd-DAY call) — distinct returners "
              "/ distinct first-callers",
     "source": "mcp_call_log (DISTINCT api_key, de-looped)", "value": "1.4% (1/74)"},
    {"claim": "Keys used ONCE only (one-and-done) — 70 of 74 new keys",
     "source": "mcp_call_log (DISTINCT api_key, de-looped)", "value": "94.6%"},
    {"claim": "Best-retaining FIRST tool: get_grid_intelligence "
              "(3/8 new keys returned)",
     "source": "mcp_call_log (DISTINCT api_key, first tool, de-looped)",
     "value": "37.5% return rate"},
]


def test_gather_evidence_includes_retention_cohorts(monkeypatch):
    """gather_evidence must APPEND the retention-cohort items as a new Source so
    the next retention investigate() reasons on real key-based funnel data.
    We mock the cohort source (no DB) and assert the items flow through."""
    # Silence the other DB-touching sources so we isolate the cohort items.
    monkeypatch.setattr(inv, "_gather_recent_findings", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_health_baseline", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_paid_reconciliation", lambda *a, **k: [])
    monkeypatch.setattr(inv, "gather_growth_funnel", lambda *a, **k: [])
    import sys
    import types
    cs = types.ModuleType("canonical_stats")
    cs.get_canonical_stats = lambda: {}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)
    # MOCK the cohort source.
    monkeypatch.setattr(inv, "gather_retention_cohorts",
                        lambda *a, **k: list(_FAKE_COHORT_ITEMS))

    ev = _REAL_GATHER_EVIDENCE()  # bypass the autouse stub
    claims = " ".join(e.get("claim", "").lower() for e in ev)
    sources = " ".join(e.get("source", "").lower() for e in ev)
    # The retention cohort items are present.
    assert "distinct new mcp keys" in claims
    assert "return rate" in claims
    assert "one-and-done" in claims
    assert "best-retaining first tool" in claims
    # Sourced from the de-looped mcp_call_log (the honest key-based signal).
    assert "mcp_call_log" in sources
    assert "de-looped" in sources
    # Every cohort item keeps the {claim, source, value} shape the chain expects.
    for it in _FAKE_COHORT_ITEMS:
        assert it in ev


def test_gather_evidence_survives_retention_cohort_failure(monkeypatch):
    """If gather_retention_cohorts blows up, gather_evidence must still return
    the other evidence and NOT crash the investigation (best-effort contract)."""
    monkeypatch.setattr(inv, "_gather_recent_findings", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_health_baseline", lambda *a, **k: [])
    monkeypatch.setattr(inv, "_gather_paid_reconciliation", lambda *a, **k: [])
    monkeypatch.setattr(inv, "gather_growth_funnel", lambda *a, **k: [])
    import sys
    import types
    cs = types.ModuleType("canonical_stats")
    cs.get_canonical_stats = lambda: {"facilities": 21000}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)

    def _boom(*a, **k):
        raise RuntimeError("retention source exploded")

    monkeypatch.setattr(inv, "gather_retention_cohorts", _boom)
    ev = _REAL_GATHER_EVIDENCE()  # must not raise
    assert any("facilities" in e["claim"].lower() for e in ev)


def test_gather_retention_cohorts_delegates_to_canonical_module(monkeypatch):
    """gather_retention_cohorts must REUSE the canonical analyzer
    routes.retention_cohorts.retention_cohort_evidence (do NOT hand-roll a 2nd
    cohort implementation) so the brain evidence and the ops endpoint never
    drift. We inject a fake module and assert the items + window flow through."""
    import sys
    import types

    seen = {}
    rc = types.ModuleType("routes.retention_cohorts")

    def _fake_evidence(days=30):
        seen["days"] = days
        return list(_FAKE_COHORT_ITEMS)

    rc.retention_cohort_evidence = _fake_evidence
    monkeypatch.setitem(sys.modules, "routes.retention_cohorts", rc)

    out = inv.gather_retention_cohorts(window_days=14)
    assert out == _FAKE_COHORT_ITEMS
    assert seen["days"] == 14


def test_gather_retention_cohorts_missing_module_returns_empty(monkeypatch):
    """If the canonical analyzer is unavailable (import fails), the wrapper
    returns [] WITHOUT raising (best-effort contract)."""
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "routes.retention_cohorts":
            raise ImportError("simulated missing module")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert inv.gather_retention_cohorts() == []


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


# ── retention-cohorts ENDPOINT (canonical standalone analyzer) ──────
# The GET /api/v1/mcp/retention/cohorts ops endpoint lives in the canonical
# routes/retention_cohorts.py (a non-contested standalone module, mirroring
# routes/mcp_retention.py), NOT on brain_investigator_bp — defining it on both
# blueprints would collide as a duplicate rule. brain_investigator REUSES that
# module's retention_cohort_evidence() as GATHER Source 6 (tested above).
rc = pytest.importorskip("routes.retention_cohorts")


@pytest.fixture()
def rc_client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(rc.retention_cohorts_bp)
    # Admin gate always passes in tests.
    monkeypatch.setattr(rc, "_admin_ok", lambda: True)
    return app.test_client()


_FAKE_COHORT_SUMMARY = {
    "window_days": 30, "new_keys": 74, "returned_keys": 1, "return_rate": 1.4,
    "multiday_returned_keys": 1, "multiday_rate": 1.4,
    "reuse_buckets": {"1": 70, "2-5": 3, "6+": 1},
    "first_tool_mix": [{"tool": "get_grid_intelligence", "count": 8, "pct": 10.8}],
    "retention_by_first_tool": [
        {"tool": "get_grid_intelligence", "first_callers": 8, "returned": 3,
         "return_rate": 37.5}],
    "median_time_to_2nd_call_seconds": 3600.0,
    "p95_time_to_2nd_call_seconds": 86400.0,
}


def test_retention_cohorts_endpoint_returns_summary(rc_client, monkeypatch):
    """GET /api/v1/mcp/retention/cohorts returns the de-looped cohort summary
    (admin-gated)."""
    monkeypatch.setattr(rc, "compute_retention_cohorts",
                        lambda *a, **k: dict(_FAKE_COHORT_SUMMARY))
    monkeypatch.setattr(rc, "retention_cohort_evidence",
                        lambda *a, **k: list(_FAKE_COHORT_ITEMS))
    resp = rc_client.get("/api/v1/mcp/retention/cohorts?days=30")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["days"] == 30
    assert data["cohorts"]["new_keys"] == 74
    # A retention rate is distinct-returners / distinct-first-callers.
    assert data["cohorts"]["return_rate"] == 1.4
    # The evidence adapter (what the brain reads) is surfaced too.
    assert data["evidence"] == _FAKE_COHORT_ITEMS


def test_retention_cohorts_endpoint_requires_admin(rc_client, monkeypatch):
    monkeypatch.setattr(rc, "_admin_ok", lambda: False)
    resp = rc_client.get("/api/v1/mcp/retention/cohorts")
    assert resp.status_code == 403


def test_retention_cohorts_endpoint_clamps_days(rc_client, monkeypatch):
    """days= is clamped (1..180) and passed through to the analyzer."""
    seen = {}

    def _capture(days=30, conn=None):
        seen["days"] = days
        return dict(_FAKE_COHORT_SUMMARY, window_days=days)

    monkeypatch.setattr(rc, "compute_retention_cohorts", _capture)
    monkeypatch.setattr(rc, "retention_cohort_evidence", lambda *a, **k: [])
    resp = rc_client.get("/api/v1/mcp/retention/cohorts?days=9999")
    assert resp.status_code == 200
    assert seen["days"] == 180


def test_retention_cohort_evidence_adapter_shape(monkeypatch):
    """The adapter the brain reads emits {claim, source, value} items with
    DISTINCT-key / de-looped sourcing and a return rate framed as
    distinct-returners / distinct-first-callers. No DB: mock compute_*."""
    monkeypatch.setattr(rc, "compute_retention_cohorts",
                        lambda *a, **k: dict(_FAKE_COHORT_SUMMARY))
    items = rc.retention_cohort_evidence(days=30)
    assert isinstance(items, list) and items
    for it in items:
        assert set(it.keys()) >= {"claim", "source", "value"}
    blob = " ".join(f"{i['claim']} {i['source']} {i['value']}" for i in items).lower()
    assert "new mcp api_keys" in blob or "new mcp keys" in blob
    assert "return rate" in blob
    assert "distinct api_key" in blob
    # The "which first tool retains?" signal is present.
    assert "get_grid_intelligence" in blob


def test_retention_cohort_evidence_no_data_returns_empty(monkeypatch):
    """No new keys in window → [] (best-effort, never raises)."""
    monkeypatch.setattr(rc, "compute_retention_cohorts", lambda *a, **k: {})
    assert rc.retention_cohort_evidence() == []


def test_targeted_source_evidence_is_prepended_and_counted(monkeypatch):
    """★ Guards #2906's actual contract on this path: question-targeted repo
    source is placed BEFORE the generic platform bundle, and counted.

    The autouse fixture stubs gather_targeted_evidence to [] so the other
    tests assert on the DB bundle alone; this one re-stubs it with content, so
    a regression that dropped targeted evidence — or appended it last, where
    the model reads it after the generic bundle — fails here instead of
    silently degrading every investigation."""
    targeted = [{"claim": "SOURCE CANDIDATE 1 of 1: routes/example.py:1",
                 "source": "repo source (question-targeted, brain_source_map)",
                 "value": 0.55}]
    monkeypatch.setattr(inv, "gather_targeted_evidence", lambda _q: list(targeted))
    monkeypatch.setattr(inv, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(inv, "_call_model", _fake_caller(_GOOD_RESPONSES))

    out = inv.investigate("Why is reach flat?")

    assert out["evidence"] == targeted + _STUB_EVIDENCE
    assert out["evidence"][0]["source"].startswith("repo source")
    assert out["targeted_evidence_count"] == 1
