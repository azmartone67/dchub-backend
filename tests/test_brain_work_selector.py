"""brain_work_selector tests — the SELF-AWARE work selector. FULLY MOCKED.

NO real DB, NO network, NO flask required at import. We monkeypatch the ONE
DB-reading primitive, _read_class_rate (returns (succeeded_rate|None, samples)),
so the soft-greedy bandit math is exercised deterministically, and assert the
INVARIANTS the autonomy loop depends on:

  · class_success_weight: a class with a LOW historical succeeded-rate is
    down-weighted but NEVER below the exploration FLOOR; an UNTRIED class (no
    data) stays NEUTRAL 1.0; a PROVEN class is boosted (capped).
  · class_success_weight fails NEUTRAL (1.0) on a DB error — a hiccup never
    penalises a class.
  · leverage_score = impact * confidence * class_success_weight, guarded.
  · rank_work RE-ORDERS by leverage, is a PERMUTATION (never drops an item),
    is STABLE for ties, and annotates _leverage / _rationale without mutating
    the caller's dicts.
  · rank_work falls back to the EXISTING scan order on any internal error.
  · build_work_plan (the /work-plan body) returns ranked items + rationale +
    learned class weights.

Run:  python3 -m pytest tests/test_brain_work_selector.py -v
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _flask_shim():
    """Shim flask/internal_auth when absent so the selector (and the classifier
    it lazily imports) load in a no-flask env. Mirrors the sibling brain tests."""
    saved = {k: sys.modules.get(k) for k in
             ("flask", "internal_auth", "routes.brain_mechanical_classifier",
              "routes.brain_work_selector")}
    installed = []
    if "flask" not in sys.modules:
        flask = types.ModuleType("flask")

        class _BP:
            def __init__(self, *a, **k):
                pass

            def _noop(self, *a, **k):
                return lambda fn: fn

            get = post = route = _noop

        flask.Blueprint = _BP
        flask.jsonify = lambda *a, **k: (a, k)
        flask.request = types.SimpleNamespace(
            headers={}, args={}, get_json=lambda *a, **k: {})
        sys.modules["flask"] = flask
        installed.append("flask")
    if "internal_auth" not in sys.modules:
        ia = types.ModuleType("internal_auth")
        ia.accepted_internal_keys = lambda: set()
        ia.is_valid_internal_key = lambda k: False
        sys.modules["internal_auth"] = ia
        installed.append("internal_auth")
    # Force a fresh import of the selector under the shim.
    sys.modules.pop("routes.brain_work_selector", None)
    sys.modules.pop("routes.brain_mechanical_classifier", None)
    try:
        yield
    finally:
        for k in installed:
            sys.modules.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)


@pytest.fixture
def ws():
    import routes.brain_work_selector as mod
    return mod


# ── class_success_weight ─────────────────────────────────────────────
def test_low_succeeded_rate_downweighted_but_above_floor(ws, monkeypatch):
    """A class with a poor historical resolve-rate is DOWN-WEIGHTED below
    neutral but NEVER below the exploration FLOOR (soft-greedy: never starved)."""
    # 0% over a solid sample → maximally down-weighted.
    monkeypatch.setattr(ws, "_read_class_rate",
                        lambda k: (0.0, 20))
    w = ws.class_success_weight("flaky_class")
    assert w < ws.WORK_NEUTRAL, "low-success class must be down-weighted"
    assert w >= ws.WORK_WEIGHT_FLOOR, "must never drop below the FLOOR"
    # Exactly the floor at rate 0 with enough samples.
    assert abs(w - ws.WORK_WEIGHT_FLOOR) < 1e-6


def test_untried_class_is_neutral(ws, monkeypatch):
    """No data (untried) → 1.0 neutral (neither boosted nor penalised)."""
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (None, 0))
    assert ws.class_success_weight("brand_new_class") == ws.WORK_NEUTRAL


def test_proven_class_is_boosted_and_capped(ws, monkeypatch):
    """A class that always lands is boosted above neutral, capped at CAP."""
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (1.0, 50))
    w = ws.class_success_weight("reliable_class")
    assert w > ws.WORK_NEUTRAL
    assert w <= ws.WORK_WEIGHT_CAP


def test_few_samples_blend_toward_neutral(ws, monkeypatch):
    """One unlucky outcome (0% over 1 sample) must NOT crush the class to the
    floor — with few samples we blend toward neutral so exploration is kept."""
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (0.0, 1))
    w_few = ws.class_success_weight("unlucky_once")
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (0.0, 50))
    w_many = ws.class_success_weight("proven_bad")
    assert w_few > w_many, "few-sample 0% must be gentler than many-sample 0%"
    assert w_few >= ws.WORK_WEIGHT_FLOOR


def test_class_weight_fails_neutral_on_db_error(ws, monkeypatch):
    """A DB error in the read must NEVER penalise — fail NEUTRAL (1.0)."""
    def _boom(_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ws, "_read_class_rate", _boom)
    assert ws.class_success_weight("any") == ws.WORK_NEUTRAL


def test_empty_class_is_neutral(ws):
    assert ws.class_success_weight("") == ws.WORK_NEUTRAL
    assert ws.class_success_weight(None) == ws.WORK_NEUTRAL


# ── leverage_score ───────────────────────────────────────────────────
def test_leverage_combines_impact_confidence_classweight(ws, monkeypatch):
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (1.0, 50))
    cand = {"klass": "k", "confidence": 0.9, "rationale": "[HIGH] something"}
    score, rationale = ws.leverage_score(cand)
    # impact = HIGH(1.5); base = 1.5*0.9 = 1.35; final = base * boosted_weight
    cw = ws.class_success_weight("k")
    assert abs(score - round(1.5 * 0.9 * cw, 5)) < 1e-4
    assert rationale["class"] == "k"
    assert rationale["confidence"] == 0.9


def test_bare_candidate_scores_neutralish(ws, monkeypatch):
    """A bare proposal (no severity/occurrence/age) → impact 1.0; with a
    neutral class weight and default confidence it scores a sane positive."""
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (None, 0))
    score, _ = ws.leverage_score({"klass": "k"})
    # impact 1.0 * default-conf 0.8 * neutral 1.0
    assert abs(score - 0.8) < 1e-6


def test_leverage_never_raises(ws, monkeypatch):
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (None, 0))
    score, rationale = ws.leverage_score("not a dict")  # type: ignore[arg-type]
    assert isinstance(score, float)


# ── rank_work ────────────────────────────────────────────────────────
def _candidates():
    # Three proposals: a high-severity proven-class fix should rank above a
    # low-severity neutral one, regardless of original scan order.
    return [
        {"id": 1, "klass": "neutral_k", "confidence": 0.85,
         "rationale": "[LOW] minor", "file_path": "routes/a.py"},
        {"id": 2, "klass": "proven_k", "confidence": 0.95,
         "rationale": "[CRIT] big", "file_path": "routes/b.py"},
        {"id": 3, "klass": "flaky_k", "confidence": 0.85,
         "rationale": "[MED] mid", "file_path": "routes/c.py"},
    ]


def _rate_by_class(klass):
    return {
        "proven_k": (1.0, 40),
        "flaky_k": (0.0, 40),
        "neutral_k": (None, 0),
    }.get(klass, (None, 0))


def test_rank_reorders_by_leverage_and_keeps_all(ws, monkeypatch):
    monkeypatch.setattr(ws, "_read_class_rate", _rate_by_class)
    cands = _candidates()
    ranked = ws.rank_work(cands)
    # Permutation: same set of ids, none dropped.
    assert sorted(r["id"] for r in ranked) == [1, 2, 3]
    assert len(ranked) == len(cands)
    # id=2 (CRIT + proven class) must lead; id=3 (flaky class) must trail id=1
    # only if its down-weight overcomes its higher severity — assert leadership
    # of the clearly-highest-leverage item.
    assert ranked[0]["id"] == 2
    # Each item annotated.
    for r in ranked:
        assert "_leverage" in r and "_rationale" in r
        assert isinstance(r["_leverage"], float)


def test_rank_does_not_mutate_caller_dicts(ws, monkeypatch):
    monkeypatch.setattr(ws, "_read_class_rate", _rate_by_class)
    cands = _candidates()
    ws.rank_work(cands)
    for c in cands:
        assert "_leverage" not in c, "caller's dict must not be mutated"
        assert "_rationale" not in c


def test_rank_is_stable_for_ties(ws, monkeypatch):
    """Equal leverage → original scan order is preserved (stable). With ALL
    classes untried/neutral and identical impact+confidence, order is unchanged."""
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (None, 0))
    cands = [
        {"id": 10, "klass": "k", "confidence": 0.9, "rationale": "[MED] x"},
        {"id": 11, "klass": "k", "confidence": 0.9, "rationale": "[MED] x"},
        {"id": 12, "klass": "k", "confidence": 0.9, "rationale": "[MED] x"},
    ]
    ranked = ws.rank_work(cands)
    assert [r["id"] for r in ranked] == [10, 11, 12]


def test_rank_falls_back_to_scan_order_on_error(ws, monkeypatch):
    """If scoring blows up internally, rank_work returns the input unchanged
    (the EXISTING scan order) — never crashes, never drops."""
    def _boom(_c):
        raise RuntimeError("scoring exploded")
    monkeypatch.setattr(ws, "leverage_score", _boom)
    cands = _candidates()
    ranked = ws.rank_work(cands)
    # leverage_score is caught per-item, so order is preserved (scan order) and
    # nothing is dropped.
    assert [r["id"] for r in ranked] == [1, 2, 3]
    assert len(ranked) == len(cands)


def test_rank_empty_and_none(ws):
    assert ws.rank_work([]) == []
    assert ws.rank_work(None) == []


# ── build_work_plan (the /work-plan body) ────────────────────────────
def test_build_work_plan_returns_ranked_rationale_and_weights(ws, monkeypatch):
    monkeypatch.setattr(ws, "_read_class_rate", _rate_by_class)

    # Stub the open-proposal fetch the plan reads from (no DB).
    import routes.brain_mechanical_classifier as clf
    monkeypatch.setattr(
        clf, "_fetch_open_proposals",
        lambda include_resolved=False, limit=200: (_candidates(), ""))

    plan = ws.build_work_plan(limit=10)
    assert plan["ok"] is True
    assert plan["count"] == 3
    # Ranked, highest-leverage first; each carries a rationale.
    assert plan["ranked"][0]["id"] == 2
    for item in plan["ranked"]:
        assert "rationale" in item and "leverage" in item
    # Learned class weights surfaced for the classes in the plan.
    lcw = plan["learned_class_weights"]
    assert "proven_k" in lcw and "flaky_k" in lcw
    assert lcw["proven_k"]["weight"] > ws.WORK_NEUTRAL
    assert lcw["flaky_k"]["weight"] < ws.WORK_NEUTRAL
    assert lcw["flaky_k"]["weight"] >= ws.WORK_WEIGHT_FLOOR


def test_build_work_plan_measures_unclassified_share(ws, monkeypatch):
    """agenda #38 instrument: the plan reports what SHARE of ranked items the
    selector could not classify ('(unknown)'/'(error)'), because those items
    are invisible to class-success learning. One unclassifiable candidate in
    four ranked → count 1, share 0.25; classified items are NOT counted."""
    monkeypatch.setattr(ws, "_read_class_rate", _rate_by_class)
    import routes.brain_mechanical_classifier as clf
    # The classless candidate must NOT be rescued by the mechanical
    # classifier fallback — stub it to "cannot classify".
    monkeypatch.setattr(clf, "classify_mechanical", lambda c: {})
    cands = _candidates() + [
        {"id": 4, "confidence": 0.9, "file_path": "routes/d.py"},  # no class keys
    ]
    monkeypatch.setattr(
        clf, "_fetch_open_proposals",
        lambda include_resolved=False, limit=200: (cands, ""))

    plan = ws.build_work_plan(limit=10)
    assert plan["ok"] is True
    share = plan["unclassified_share"]
    assert share["count"] == 1
    assert share["of"] == 4
    assert share["share"] == 0.25
    # The unclassified item itself surfaces as '(unknown)', never ''.
    assert sum(1 for it in plan["ranked"] if it["class"] == "(unknown)") == 1


def test_build_work_plan_unclassified_share_unmeasured_when_empty(ws, monkeypatch):
    """An EMPTY plan reports share=None (UNMEASURED) — never a flattering 0."""
    import routes.brain_mechanical_classifier as clf
    monkeypatch.setattr(
        clf, "_fetch_open_proposals",
        lambda include_resolved=False, limit=200: ([], ""))
    plan = ws.build_work_plan()
    assert plan["ok"] is True
    assert plan["count"] == 0
    assert plan["unclassified_share"]["count"] == 0
    assert plan["unclassified_share"]["share"] is None


def test_build_work_plan_handles_fetch_error(ws, monkeypatch):
    import routes.brain_mechanical_classifier as clf
    monkeypatch.setattr(
        clf, "_fetch_open_proposals",
        lambda include_resolved=False, limit=200: ([], "query_failed: boom"))
    plan = ws.build_work_plan()
    assert plan["ok"] is False
    assert "error" in plan


# ── soft-greedy math is monotonic ────────────────────────────────────
def test_soft_greedy_monotonic_in_rate(ws):
    """Higher historical rate → higher (or equal) weight, all within bounds."""
    prev = -1.0
    for r in (0.0, 0.25, 0.5, 0.75, 1.0):
        w = ws._soft_greedy(r, 100)
        assert ws.WORK_WEIGHT_FLOOR <= w <= ws.WORK_WEIGHT_CAP
        assert w >= prev
        prev = w


def test_age_overcomes_class_gap_no_permanent_starvation():
    """Anti-starvation: the soft-greedy FLOOR alone (weight>=0.5) does NOT stop
    relative starvation — a down-weighted class still ranks below fresh
    high-weight work under the rate cap. The AGE boost must be able to OVERCOME
    the max class-weight gap (~1.3/0.5 = 2.6x) for a long-deferred item, so it
    EVENTUALLY rises and is never permanently starved."""
    import datetime as _dt
    import routes.brain_work_selector as ws
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=40)).isoformat()
    _, bd_old = ws.impact_weight({"created_at": old})
    _, bd_fresh = ws.impact_weight({"created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
    assert bd_old["age_mult"] >= 2.6, bd_old          # overcomes the class gap
    assert bd_fresh["age_mult"] < 1.1, bd_fresh        # fresh item not boosted


# ── one rate read per class, and a row that agrees with itself ───────
def test_learned_class_weights_reads_each_class_rate_exactly_once(ws, monkeypatch):
    """★ _read_class_rate opens its OWN psycopg2 connection on every call.

    This loop used to call class_success_weight(k) up to three MORE times per
    class — once for "weight", once per arm of the "status" ternary — and each
    one went back to the database for a number the loop already had. Measured
    2026-08-23 against prod Neon: six classes cost TWENTY-ONE fresh TLS
    connections and 7.5s of a 16.1s shell-#65 tick, the single largest line in
    that tick's budget and invisible to it, because the shell's deadline only
    governs reads that go through its own _q().
    """
    calls = []

    def _rate(k):
        calls.append(k)
        return {"a": (1.0, 50), "b": (0.0, 50), "c": (None, 0)}[k]

    monkeypatch.setattr(ws, "_read_class_rate", _rate)
    out = ws._learned_class_weights(["a", "b", "c"])

    assert sorted(calls) == ["a", "b", "c"], (
        f"_learned_class_weights read {len(calls)} rates for 3 classes ({calls}) — "
        f"every extra read is a fresh DB connection for a number already in hand")
    # and it still classifies all three arms of the ternary
    assert out["a"]["status"] == "boosted" and out["a"]["weight"] > ws.WORK_NEUTRAL
    assert out["b"]["status"] == "down-weighted" and out["b"]["weight"] < ws.WORK_NEUTRAL
    assert out["c"]["status"] == "untried/neutral" and out["c"]["weight"] == ws.WORK_NEUTRAL


def test_learned_class_weight_agrees_with_the_rate_printed_beside_it(ws, monkeypatch):
    """★ The weight and the rate must come from the SAME read.

    They were separate calls on separate connections, so a DB hiccup between
    them published `succeeded_rate: 0.0, samples: 50, weight: 1.0` — a NEUTRAL
    weight sitting next to the evidence that should have down-weighted it, with
    nothing marking the row degraded. Fail-neutral is right for one read; it is
    a lie when a readable rate is printed beside it.
    """
    seq = iter([(0.0, 50), (None, 0), (None, 0), (None, 0)])
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: next(seq))
    row = ws._learned_class_weights(["k"])["k"]
    assert row["succeeded_rate"] == 0.0 and row["samples"] == 50
    assert row["weight"] == ws._soft_greedy(0.0, 50) < ws.WORK_NEUTRAL, (
        f"published weight {row['weight']} does not follow from the rate "
        f"{row['succeeded_rate']} printed beside it — they came from "
        f"different reads")
    assert row["status"] == "down-weighted"
