"""
tests/test_brain_self_director.py — Brain SELF-DIRECTOR (propose-only).

MOCKS brain_investigator.investigate AND the candidate sources (work-plan +
scan_opportunities) entirely — NO real Anthropic API, NO network, NO DB. The DB
helpers (_today_count, _store_agenda, _recent_titles, ...) are monkeypatched so
nothing touches Postgres.

THE WHOLE POINT is the SAFETY of a self-running LLM loop. Covers:
  · GUARD ORDER (cost-first) — flag OFF / no-api-key / over-the-daily-cap each
    return ran:false with the right skip reason and make ZERO calls to a
    boom-investigate (asserted via a stub that raises if ever called);
  · happy path — flag on + key + under cap + a candidate -> the MOCKED
    investigate is called EXACTLY ONCE and EXACTLY ONE agenda row is stored;
  · PROPOSE-ONLY — the module exposes NO merge/send/PR/automerge helper, and the
    only write a tick performs is the agenda store;
  · pick_agenda_item dedupes against recently-surfaced items;
  · the GET /agenda + POST /agenda/<id>/grade endpoints;
  · admin gate 403 on every endpoint.
"""
import pytest

sd = pytest.importorskip("routes.brain_self_director")


# A sentinel investigate that BLOWS UP if it is ever called — used to PROVE the
# cost guards short-circuit BEFORE any model work.
def _boom_investigate(*a, **k):
    raise AssertionError("investigate() must NOT be called — the guard should "
                         "have short-circuited before any model work")


@pytest.fixture()
def _patch_investigate(monkeypatch):
    """Patch the investigate symbol the tick late-imports
    (`from routes.brain_investigator import investigate`)."""
    import routes.brain_investigator as bi

    def _set(fn):
        monkeypatch.setattr(bi, "investigate", fn)
    return _set


@pytest.fixture()
def _no_db(monkeypatch):
    """Neutralize every DB helper so nothing touches Postgres in any test."""
    monkeypatch.setattr(sd, "_recent_titles", lambda days: set())
    monkeypatch.setattr(sd, "_today_count", lambda: 0)
    monkeypatch.setattr(sd, "_store_agenda", lambda item, result: 4242)


# ════════════════════════════════════════════════════════════════════
#  GUARD ORDER — the cost ceiling. Each guard makes ZERO model calls.
# ════════════════════════════════════════════════════════════════════
def test_tick_flag_off_is_noop_zero_model_calls(monkeypatch, _patch_investigate, _no_db):
    """Flag OFF -> {ran:false, skipped:disabled} and investigate is NEVER
    called (it ships dark — a tick is a no-op that makes ZERO model calls)."""
    monkeypatch.setattr(sd, "_enabled", lambda: False)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    _patch_investigate(_boom_investigate)

    out = sd.self_direct_tick()
    assert out["ran"] is False
    assert out["skipped_reason"] == "disabled"


def test_tick_no_api_key_is_noop_zero_model_calls(monkeypatch, _patch_investigate, _no_db):
    """No ANTHROPIC_API_KEY -> {ran:false, skipped:no_api_key}, NO investigate
    call (degrades gracefully, never crashes)."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: False)
    _patch_investigate(_boom_investigate)

    out = sd.self_direct_tick()
    assert out["ran"] is False
    assert out["skipped_reason"] == "no_api_key"


def test_tick_over_daily_cap_is_noop_zero_model_calls(monkeypatch, _patch_investigate):
    """At/over the daily cap -> {ran:false, skipped:daily_cap}, NO investigate
    call. The cap is enforced SERVER-SIDE in the tick handler."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 4)   # already at cap
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda item, result: (_ for _ in ()).throw(
                            AssertionError("must not store when capped")))
    _patch_investigate(_boom_investigate)

    out = sd.self_direct_tick()
    assert out["ran"] is False
    assert out["skipped_reason"] == "daily_cap"
    assert out["used_today"] == 4
    assert out["daily_cap"] == 4


def test_tick_no_candidate_is_noop_zero_model_calls(monkeypatch, _patch_investigate, _no_db):
    """All guards pass but pick_agenda_item returns None -> {ran:false,
    skipped:no_candidate}, NO investigate call."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: None)
    _patch_investigate(_boom_investigate)

    out = sd.self_direct_tick()
    assert out["ran"] is False
    assert out["skipped_reason"] == "no_candidate"


# ════════════════════════════════════════════════════════════════════
#  HAPPY PATH — investigate ONCE, store ONE agenda row, NOTHING else
# ════════════════════════════════════════════════════════════════════
def test_tick_happy_path_investigates_once_stores_one_row(monkeypatch, _patch_investigate):
    """Flag on + key + under cap + a candidate -> the MOCKED investigate is
    called EXACTLY ONCE and EXACTLY ONE agenda row is stored. The ONLY write the
    tick performs is the agenda store — no merge/send/PR/automerge."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 0)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: {
        "kind": "opportunity", "title": "[reliability] flapping",
        "question": "What stops the single-replica flapping?",
        "area": "reliability", "leverage": 1.4,
        "source": "brain_enhancer.scan_opportunities",
    })

    calls = {"investigate": 0, "store": 0}

    def _inv(question, *a, **k):
        calls["investigate"] += 1
        assert question == "What stops the single-replica flapping?"
        return {
            "question": question,
            "recommendation": "Cap the synchronous self-calls.",
            "confidence": 0.72,
            "caveats": ["c"],
            "decision_for_human": "decide X",
            "refutation": {"attempted": True, "survived": True},
            "model": "claude-opus-4-8",
        }
    _patch_investigate(_inv)

    stored = []

    def _store(item, result):
        calls["store"] += 1
        stored.append((item, result))
        return 4242
    monkeypatch.setattr(sd, "_store_agenda", _store)

    out = sd.self_direct_tick()

    # Exactly one investigation, exactly one agenda row.
    assert calls["investigate"] == 1
    assert calls["store"] == 1
    assert out["ran"] is True
    assert out["agenda_id"] == 4242
    assert out["confidence"] == pytest.approx(0.72)
    # The stored row carried the verified investigation result.
    stored_item, stored_result = stored[0]
    assert stored_item["title"] == "[reliability] flapping"
    assert stored_result["recommendation"] == "Cap the synchronous self-calls."


def test_tick_is_propose_only_no_action_helpers(monkeypatch, _patch_investigate):
    """PROPOSE-ONLY: the module exposes NO merge/send/PR/automerge/apply helper,
    and a happy-path tick's ONLY write is the agenda store (asserted by failing
    if any other write-shaped helper is invoked)."""
    # 1) Static surface: no action verbs anywhere in the public namespace.
    banned = ("merge", "send", "open_pr", "draft_pr", "push", "commit",
              "deploy", "apply", "automerge", "revert")
    for n in [x for x in dir(sd) if not x.startswith("__")]:
        low = n.lower()
        for verb in banned:
            assert verb not in low, \
                f"propose-only module exposes an action helper: {n}"

    # 2) Dynamic: run the happy path; the ONLY write helper invoked is
    #    _store_agenda. (There is no other write helper on the module — this
    #    asserts the store is hit exactly once and nothing else mutates.)
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 0)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: {
        "kind": "work_plan", "title": "t", "question": "q", "area": "reliability",
        "leverage": 1.0, "source": "s",
    })
    _patch_investigate(lambda q, *a, **k: {
        "question": q, "recommendation": "r", "confidence": 0.5,
        "caveats": [], "refutation": {}, "model": "m"})
    writes = {"store": 0}
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda item, result: writes.__setitem__("store", writes["store"] + 1) or 9)
    out = sd.self_direct_tick()
    assert out["ran"] is True
    assert writes["store"] == 1


def test_tick_cannot_investigate_does_not_store(monkeypatch, _patch_investigate):
    """If investigate returns cannot_investigate (e.g. model unavailable), the
    tick surfaces no agenda row and reports the skip — never a half-baked row."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 0)
    monkeypatch.setattr(sd, "pick_agenda_item", lambda: {
        "kind": "work_plan", "title": "t", "question": "q", "area": "reliability",
        "leverage": 1.0, "source": "s"})
    _patch_investigate(lambda q, *a, **k: {
        "question": q, "cannot_investigate": "no_api_key"})
    monkeypatch.setattr(sd, "_store_agenda",
                        lambda item, result: (_ for _ in ()).throw(
                            AssertionError("must not store a non-investigation")))
    out = sd.self_direct_tick()
    assert out["ran"] is False
    assert out["skipped_reason"] == "cannot_investigate"


def test_tick_never_raises_on_internal_error(monkeypatch):
    """A self-running loop must NEVER raise. If pick_agenda_item explodes, the
    tick degrades to a skip, not a crash."""
    monkeypatch.setattr(sd, "_enabled", lambda: True)
    monkeypatch.setattr(sd, "_has_api_key", lambda: True)
    monkeypatch.setattr(sd, "_daily_cap", lambda: 4)
    monkeypatch.setattr(sd, "_today_count", lambda: 0)

    def _boom():
        raise RuntimeError("assessment blew up")
    monkeypatch.setattr(sd, "pick_agenda_item", _boom)
    out = sd.self_direct_tick()  # must NOT raise
    assert out["ran"] is False
    assert out["skipped_reason"] == "no_candidate"


# ════════════════════════════════════════════════════════════════════
#  pick_agenda_item — ASSESS (reuse sources) + DEDUPE
# ════════════════════════════════════════════════════════════════════
def test_pick_chooses_highest_leverage(monkeypatch):
    """pick_agenda_item pools the work-plan + opportunity candidates and returns
    the single highest-leverage one."""
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [
        {"kind": "work_plan", "title": "wp-low", "question": "qa",
         "area": "reliability", "leverage": 0.4, "source": "wp"},
    ])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [
        {"kind": "opportunity", "title": "op-high", "question": "qb",
         "area": "data_coverage", "leverage": 1.45, "source": "op"},
    ])
    monkeypatch.setattr(sd, "_recent_titles", lambda days: set())

    item = sd.pick_agenda_item()
    assert item is not None
    assert item["title"] == "op-high"
    assert item["leverage"] == pytest.approx(1.45)


def test_pick_dedupes_against_recent_items(monkeypatch):
    """pick_agenda_item SKIPS candidates whose title/question was surfaced in the
    last ~3 days — so the loop doesn't re-investigate the same thing every tick.
    The highest-leverage candidate is deduped, so the NEXT one wins."""
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [
        {"kind": "work_plan", "title": "already-done", "question": "qa",
         "area": "reliability", "leverage": 2.0, "source": "wp"},
    ])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [
        {"kind": "opportunity", "title": "fresh-one", "question": "qb",
         "area": "performance", "leverage": 1.0, "source": "op"},
    ])
    # The high-leverage item's title is in the recent set -> it must be skipped.
    monkeypatch.setattr(sd, "_recent_titles", lambda days: {"already-done"})

    item = sd.pick_agenda_item()
    assert item is not None
    assert item["title"] == "fresh-one", \
        "the recently-surfaced high-leverage item must be deduped out"


def test_pick_returns_none_when_all_deduped(monkeypatch):
    """If every candidate was recently surfaced, pick returns None (nothing new
    to think about) -> the tick skips with no_candidate."""
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [
        {"kind": "work_plan", "title": "a", "question": "qa",
         "area": "reliability", "leverage": 1.0, "source": "wp"},
    ])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [])
    monkeypatch.setattr(sd, "_recent_titles", lambda days: {"a", "qa"})
    assert sd.pick_agenda_item() is None


def test_pick_returns_none_on_no_data(monkeypatch):
    """No candidates from either source -> None (never raises)."""
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [])
    monkeypatch.setattr(sd, "_recent_titles", lambda days: set())
    assert sd.pick_agenda_item() is None


def test_pick_never_raises_when_a_source_errors(monkeypatch):
    """A source RAISING must not crash the assessment — best-effort."""
    monkeypatch.setattr(sd, "_work_plan_candidates",
                        lambda: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [
        {"kind": "opportunity", "title": "ok", "question": "q",
         "area": "performance", "leverage": 1.0, "source": "op"},
    ])
    monkeypatch.setattr(sd, "_recent_titles", lambda days: set())
    item = sd.pick_agenda_item()  # must NOT raise
    assert item is not None
    assert item["title"] == "ok"


# ════════════════════════════════════════════════════════════════════
#  Endpoints (Flask test client) — admin-gated
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(sd.brain_self_director_bp)
    monkeypatch.setattr(sd, "_admin_ok", lambda: True)
    return app.test_client()


def test_tick_endpoint_returns_skip_reason(client, monkeypatch):
    """POST /self-direct/tick surfaces the tick's skip reason so cron logs are
    legible (here: dark)."""
    monkeypatch.setattr(sd, "self_direct_tick",
                        lambda: {"ran": False, "skipped_reason": "disabled"})
    resp = client.post("/api/v1/brain/self-direct/tick")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["ran"] is False
    assert data["skipped_reason"] == "disabled"


def test_tick_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setattr(sd, "_admin_ok", lambda: False)

    def _boom():
        raise AssertionError("tick must not run for a non-admin")
    monkeypatch.setattr(sd, "self_direct_tick", _boom)
    resp = client.post("/api/v1/brain/self-direct/tick")
    assert resp.status_code == 403


def test_agenda_endpoint_lists_highest_leverage_first(client, monkeypatch):
    monkeypatch.setattr(sd, "_recent_agenda", lambda limit=25: [
        {"id": 2, "title": "b", "confidence": 0.9, "area": "reliability"},
        {"id": 1, "title": "a", "confidence": 0.4, "area": "performance"},
    ])
    resp = client.get("/api/v1/brain/agenda")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["count"] == 2
    assert data["agenda"][0]["confidence"] >= data["agenda"][1]["confidence"]


def test_agenda_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setattr(sd, "_admin_ok", lambda: False)
    resp = client.get("/api/v1/brain/agenda")
    assert resp.status_code == 403


def test_grade_endpoint_records_grade(client, monkeypatch):
    monkeypatch.setattr(sd, "_grade_agenda", lambda i, g: True)
    resp = client.post("/api/v1/brain/agenda/5/grade", json={"grade": "good"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] == 5
    assert data["grade"] == "good"


def test_grade_endpoint_requires_grade(client):
    resp = client.post("/api/v1/brain/agenda/5/grade", json={})
    assert resp.status_code == 400


def test_grade_endpoint_not_found(client, monkeypatch):
    monkeypatch.setattr(sd, "_grade_agenda", lambda i, g: False)
    resp = client.post("/api/v1/brain/agenda/5/grade", json={"grade": "x"})
    assert resp.status_code == 404


def test_grade_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setattr(sd, "_admin_ok", lambda: False)
    resp = client.post("/api/v1/brain/agenda/5/grade", json={"grade": "good"})
    assert resp.status_code == 403


# ── wiring guard ─────────────────────────────────────────────────────
def test_register_helper_wires_routes_onto_a_real_app():
    """Guard against the false-DONE where the module + its tests pass but the
    app never registers the blueprint (every endpoint 404s, the cron heartbeat
    hits a dead route). register_brain_self_director must put the three routes
    in a real app's URL map — the one thing testing the module in isolation does
    NOT otherwise prove."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    sd.register_brain_self_director(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/brain/self-direct/tick" in rules
    assert "/api/v1/brain/agenda" in rules
    assert "/api/v1/brain/agenda/<int:agenda_id>/grade" in rules
