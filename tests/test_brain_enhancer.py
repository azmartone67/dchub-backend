"""
tests/test_brain_enhancer.py — Brain ENHANCER (propose-only).

MOCKS brain_investigator.investigate AND brain_models entirely — NO real
Anthropic API, NO network, NO DB. We patch the data-source readers so
scan_opportunities runs against canned evidence, and we patch
routes.brain_enhancer (and the investigate it reuses) so propose_enhancements
runs against canned investigations.

Covers:
  · scan_opportunities returns evidence-GROUNDED opportunities (every item names
    a real signal it came from);
  · propose_enhancements runs, calls the MOCKED investigate per opportunity, and
    returns proposals RANKED by leverage x confidence (asserts the order);
  · PROPOSE-ONLY — no merge/send/PR/write helper is invoked (only the store);
  · a fabricated figure in a proposal is FLAGGED against the canon (reuses the
    investigator's fence);
  · cannot_enhance when there's no API key;
  · flag-off short-circuits POST /enhance BEFORE any model call;
  · the async POST returns 202 + run_id + pending and the worker STORES proposals;
  · the grade endpoint records a grade.
"""
import json

import pytest

enh = pytest.importorskip("routes.brain_enhancer")


# ════════════════════════════════════════════════════════════════════
#  scan_opportunities — evidence-grounded
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def _stub_sources(monkeypatch):
    """Stub every data source scan_opportunities reads so it runs with NO DB."""
    # canonical_stats lives at repo root; patch the symbol the scanner imports.
    import canonical_stats
    monkeypatch.setattr(canonical_stats, "get_canonical_stats", lambda *a, **k: {
        "facilities": 21000,
        "facilities_verified": 15000,
        "countries": 178,
        "markets": 232,
        "isos": 7,
    })
    # Recent findings + health baseline + work-plan come via brain_investigator /
    # brain_work_selector; patch those module-level readers.
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "_gather_recent_findings", lambda limit=12: [
        {"claim": "freshness drift on /markets", "source": "brain_findings", "value": 9},
        {"claim": "404 on /report CTA", "source": "brain_findings", "value": 5},
        {"claim": "slow /pricing render", "source": "brain_findings", "value": 1},
    ])
    monkeypatch.setattr(bi, "_gather_health_baseline", lambda max_lines=40: [
        {"claim": "Health baseline", "source": "HEALTH_BASELINE.md", "value": "OK"},
    ])
    import routes.brain_work_selector as ws
    monkeypatch.setattr(ws, "build_work_plan", lambda limit=5: {
        "ranked": [{"class": "stale_restamp", "leverage": 1.42},
                   {"class": "null_guard", "leverage": 0.9}],
    })


def test_scan_opportunities_are_evidence_grounded(_stub_sources):
    opps = enh.scan_opportunities()
    assert opps, "expected opportunities from the stubbed evidence"
    valid_areas = set(enh.AREAS)
    for o in opps:
        assert o["area"] in valid_areas
        assert o["signal"], "every opportunity must name the signal it came from"
        assert o["question"], "every opportunity must carry a sharp question"
    # Grounded in REAL signals, not invented: the canonical gap + a finding + the
    # work-plan all surfaced.
    sigs = " ".join(o["signal"] for o in opps).lower()
    assert "canonical_stats" in sigs            # data_coverage from canon
    assert "brain_findings" in sigs             # reliability/ux from findings
    assert "work-plan" in sigs                  # performance from the plan
    # Areas span more than one bucket.
    assert len({o["area"] for o in opps}) >= 2


def test_scan_opportunities_no_data_returns_empty(monkeypatch):
    """Every source erroring -> [] (never raises)."""
    import canonical_stats
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "_gather_recent_findings",
                        lambda limit=12: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr(bi, "_gather_health_baseline", lambda max_lines=40: [])
    import routes.brain_work_selector as ws
    monkeypatch.setattr(ws, "build_work_plan", lambda limit=5: {"ranked": []})
    assert enh.scan_opportunities() == []


# ════════════════════════════════════════════════════════════════════
#  propose_enhancements — reuses investigate, RANKS by leverage x conf
# ════════════════════════════════════════════════════════════════════
def _fake_investigate_by_confidence(conf_map):
    """Return an investigate() stub that maps a substring of the question to a
    canned verified recommendation with a chosen confidence — so we can assert
    the RANK order is by leverage x confidence."""
    def _inv(question, *, depth="default"):
        for key, conf in conf_map.items():
            if key in (question or ""):
                return {
                    "question": question,
                    "recommendation": f"Improve the {key} area.",
                    "confidence": conf,
                    "caveats": ["a caveat"],
                    "decision_for_human": "decide X",
                    "refutation": {"attempted": True, "survived": True,
                                   "weaknesses_found": ["w"]},
                    "model": "claude-opus-4-8",
                }
        return {"question": question, "recommendation": "generic", "confidence": 0.5,
                "caveats": [], "decision_for_human": None,
                "refutation": {"attempted": True, "survived": True}, "model": "m"}
    return _inv


def test_propose_ranks_by_leverage_times_confidence(monkeypatch):
    """propose_enhancements runs the MOCKED investigate per opportunity and
    returns proposals ranked highest leverage (= confidence here) first."""
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)
    # Three fixed opportunities with distinguishable question keywords.
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [
        {"area": "reliability", "signal": "sig-LOW", "question": "fix LOW thing"},
        {"area": "data_coverage", "signal": "sig-HIGH", "question": "fix HIGH thing"},
        {"area": "performance", "signal": "sig-MID", "question": "fix MID thing"},
    ])
    # Patch the investigate symbol the proposer imports (it does a late import
    # `from routes.brain_investigator import investigate`).
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "investigate", _fake_investigate_by_confidence({
        "LOW": 0.30, "HIGH": 0.90, "MID": 0.60,
    }))

    out = enh.propose_enhancements(max_proposals=3)
    assert "cannot_enhance" not in out
    props = out["proposals"]
    assert len(props) == 3
    # RANK assertion: highest confidence (= leverage, flat impact) first.
    confs = [p["confidence"] for p in props]
    assert confs == sorted(confs, reverse=True), confs
    assert props[0]["confidence"] == pytest.approx(0.90)
    assert props[-1]["confidence"] == pytest.approx(0.30)
    # leverage_rank is surfaced and ordered descending too.
    levs = [p["leverage_rank"] for p in props]
    assert levs == sorted(levs, reverse=True)
    # Each proposal carries the verified evidence + refutation + decision.
    top = props[0]
    assert top["area"] == "data_coverage"
    assert top["refutation"]["survived"] is True
    assert top["decision_for_human"]
    assert top["title"].startswith("[data_coverage]")


def test_propose_is_propose_only_no_action_helpers(monkeypatch):
    """PROPOSE-ONLY: the proposer must NOT invoke any merge/send/PR/write helper.
    The module must not even define such helpers — only storage writes exist."""
    # No action verbs anywhere in the module's public surface.
    banned = ("merge", "send", "open_pr", "draft_pr", "push", "commit",
              "deploy", "apply")
    names = [n for n in dir(enh) if not n.startswith("__")]
    for n in names:
        low = n.lower()
        for verb in banned:
            assert verb not in low, f"propose-only module exposes action helper: {n}"

    # And dynamically: run the proposer and assert the ONLY thing the async
    # worker writes is via the store helpers (not any action).
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [
        {"area": "reliability", "signal": "s", "question": "q"},
    ])
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "investigate", _fake_investigate_by_confidence({"q": 0.7}))

    calls = {"store": 0, "mark": 0, "enqueue": 0}
    monkeypatch.setattr(enh, "_store_proposal",
                        lambda rid, p: calls.__setitem__("store", calls["store"] + 1) or 1)
    monkeypatch.setattr(enh, "_mark_run",
                        lambda rid, status, result=None: calls.__setitem__("mark", calls["mark"] + 1) or True)
    enh._run_enhance_async(99, 3)
    # The worker stored a proposal and marked the run — and did nothing else.
    assert calls["store"] >= 1
    assert calls["mark"] == 1


def test_fabricated_figure_in_proposal_is_flagged(monkeypatch):
    """A banned figure leaking from investigate into a proposal is flagged
    against the canon (reuses the investigator's fence) and confidence capped."""
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [
        {"area": "data_coverage", "signal": "s", "question": "scale?"},
    ])

    def _inv(question, *, depth="default"):
        return {
            "question": question,
            "recommendation": "We track 50,000 facilities worth $324B, so scale.",
            "confidence": 0.9,
            "caveats": [],
            "decision_for_human": "hire",
            "refutation": {"attempted": True, "survived": True},
            "model": "m",
        }
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "investigate", _inv)

    out = enh.propose_enhancements(max_proposals=1)
    prop = out["proposals"][0]
    joined = " ".join(prop["caveats"]).lower()
    assert "fabrication flagged" in joined
    assert "324b" in joined
    assert prop["confidence"] <= 0.3
    assert prop["refutation"].get("fabrication_flagged") is True


def test_cannot_enhance_without_api_key(monkeypatch):
    """No API key -> cannot_enhance, NEVER raises, and never scans/investigates."""
    monkeypatch.setattr(enh, "_has_api_key", lambda: False)

    def _boom():
        raise AssertionError("must not scan when there is no API key")
    monkeypatch.setattr(enh, "scan_opportunities", _boom)

    out = enh.propose_enhancements(max_proposals=3)
    assert out["cannot_enhance"] == "no_api_key"
    assert out["proposals"] == []


def test_cannot_enhance_no_opportunities(monkeypatch):
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)
    monkeypatch.setattr(enh, "scan_opportunities", lambda: [])
    out = enh.propose_enhancements(max_proposals=3)
    assert out["cannot_enhance"] == "no_opportunities"


def test_fence_reuses_investigator_canon():
    """The enhancer's fence delegates to the investigator's canonical fence."""
    hits = enh._fence("we have 50,000 sites worth $324B")
    assert len(hits) == 2
    assert enh._fence("21,000 facilities across 178 countries") == []


# ════════════════════════════════════════════════════════════════════
#  Endpoints (Flask test client)
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(enh.brain_enhancer_bp)
    monkeypatch.setattr(enh, "_admin_ok", lambda: True)
    return app.test_client()


def test_flag_off_short_circuits_before_model(client, monkeypatch):
    """BRAIN_ENHANCER_ENABLED off -> POST /enhance returns enabled:false WITHOUT
    enqueuing a run or calling propose_enhancements / a model."""
    monkeypatch.setattr(enh, "_enabled", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("must NOT propose/enqueue when the flag is off")
    monkeypatch.setattr(enh, "propose_enhancements", _boom)
    monkeypatch.setattr(enh, "_enqueue_run", _boom)

    resp = client.post("/api/v1/brain/enhance", json={"max": 3})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False


def test_enhance_requires_admin(client, monkeypatch):
    monkeypatch.setattr(enh, "_admin_ok", lambda: False)
    resp = client.post("/api/v1/brain/enhance", json={"max": 3})
    assert resp.status_code == 403


def test_enhance_enqueues_async_and_worker_stores(client, monkeypatch):
    """The async POST returns 202 + run_id + pending; the worker stores proposals
    (PROPOSE-ONLY) and marks the run done."""
    monkeypatch.setattr(enh, "_enabled", lambda: True)
    # Stub enqueue so no DB is needed; spawn must NOT run the slow chain inline.
    monkeypatch.setattr(enh, "_enqueue_run", lambda mp: 77)
    spawned = {}

    import threading
    real_thread = threading.Thread

    def _capture_thread(target=None, args=(), daemon=None):
        spawned["target"] = target
        spawned["args"] = args
        return real_thread(target=lambda: None, daemon=daemon)
    monkeypatch.setattr(threading, "Thread", _capture_thread)

    resp = client.post("/api/v1/brain/enhance", json={"max": 2})
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["run_id"] == 77
    assert data["status"] == "pending"
    assert "proposals" not in data            # ASYNC: no inline result
    # The worker the endpoint queued is _run_enhance_async(run_id=77, max=2).
    assert spawned["target"] is enh._run_enhance_async
    assert spawned["args"] == (77, 2)

    # Drive the worker with mocks: it must STORE proposals and mark the run.
    monkeypatch.setattr(enh, "propose_enhancements", lambda max_proposals=3: {
        "opportunities": [{"area": "reliability", "signal": "s", "question": "q"}],
        "proposals": [{"title": "[reliability] s", "area": "reliability",
                       "recommendation": "do X", "confidence": 0.8,
                       "leverage_rank": 0.8, "caveats": [], "refutation": {}}],
        "model": "claude-opus-4-8",
    })
    stored = []
    marked = {}
    monkeypatch.setattr(enh, "_store_proposal",
                        lambda rid, p: stored.append((rid, p)) or (len(stored)))
    monkeypatch.setattr(enh, "_mark_run",
                        lambda rid, status, result=None: marked.update(
                            rid=rid, status=status, result=result) or True)
    enh._run_enhance_async(77, 2)
    assert len(stored) == 1
    assert stored[0][0] == 77
    assert stored[0][1]["recommendation"] == "do X"
    assert marked["rid"] == 77
    assert marked["status"] == "done"
    assert marked["result"]["proposals_stored"] == 1


def test_enhance_storage_unavailable_returns_503(client, monkeypatch):
    monkeypatch.setattr(enh, "_enabled", lambda: True)
    monkeypatch.setattr(enh, "_enqueue_run", lambda mp: None)
    resp = client.post("/api/v1/brain/enhance", json={"max": 3})
    assert resp.status_code == 503


def test_get_run_not_found(client, monkeypatch):
    monkeypatch.setattr(enh, "_get_run", lambda r: None)
    resp = client.get("/api/v1/brain/enhance/999")
    assert resp.status_code == 404


def test_list_enhancements_ranked(client, monkeypatch):
    monkeypatch.setattr(enh, "_recent_proposals", lambda limit=25: [
        {"id": 2, "title": "b", "leverage_rank": 0.9, "confidence": 0.9},
        {"id": 1, "title": "a", "leverage_rank": 0.4, "confidence": 0.4},
    ])
    resp = client.get("/api/v1/brain/enhancements")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["count"] == 2
    assert data["proposals"][0]["leverage_rank"] >= data["proposals"][1]["leverage_rank"]


def test_grade_endpoint_records_grade(client, monkeypatch):
    monkeypatch.setattr(enh, "_grade_proposal", lambda i, g: True)
    resp = client.post("/api/v1/brain/enhancements/5/grade", json={"grade": "good"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] == 5
    assert data["grade"] == "good"


def test_grade_endpoint_requires_grade(client):
    resp = client.post("/api/v1/brain/enhancements/5/grade", json={})
    assert resp.status_code == 400


def test_grade_endpoint_not_found(client, monkeypatch):
    monkeypatch.setattr(enh, "_grade_proposal", lambda i, g: False)
    resp = client.post("/api/v1/brain/enhancements/5/grade", json={"grade": "x"})
    assert resp.status_code == 404
