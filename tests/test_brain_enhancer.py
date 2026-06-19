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
#  LEARNING-DRIVEN scan reorder (GOAL B) — mine where the brain wins first
# ════════════════════════════════════════════════════════════════════
def test_learned_scan_favors_high_success_area(monkeypatch):
    """With a stubbed learned-state signal that favors a chosen AREA,
    scan_opportunities ranks that area's opportunities AHEAD of the others —
    instead of the fixed canonical->findings->baseline->work_plan order."""
    monkeypatch.setattr(enh, "_learned_scan_enabled", lambda: True)
    # Three opportunities in three areas, in a FIXED non-learned order where the
    # winning area is LAST (so a real reorder must move it to the front).
    # Patch the four scanners so scan_opportunities assembles a known list.
    def _seed(out):
        out.append(enh._opportunity("performance", "sig-perf", "q perf"))
        out.append(enh._opportunity("developer_ux", "sig-ux", "q ux"))
        out.append(enh._opportunity("data_coverage", "sig-cov", "q cov"))
    monkeypatch.setattr(enh, "_scan_canonical", lambda out: _seed(out))
    monkeypatch.setattr(enh, "_scan_findings", lambda out, limit=12: None)
    monkeypatch.setattr(enh, "_scan_health_baseline", lambda out: None)
    monkeypatch.setattr(enh, "_scan_work_plan", lambda out: None)

    # Stub the learned-state signal: data_coverage is the historically-winning
    # area (high weight), the others NEUTRAL.
    def _fake_area_weight(area):
        return {"data_coverage": 1.30}.get(area, 1.0)
    monkeypatch.setattr(enh, "_area_success_weight", _fake_area_weight)

    opps = enh.scan_opportunities()
    assert [o["area"] for o in opps][0] == "data_coverage", \
        "the historically-winning area must be mined FIRST"
    # REORDER-ONLY: same set, nothing dropped or invented.
    assert sorted(o["area"] for o in opps) == ["data_coverage", "developer_ux", "performance"]
    assert len(opps) == 3


def test_learned_scan_is_reorder_only_stable_on_ties(monkeypatch):
    """When all areas have EQUAL learned weight the order is UNCHANGED (stable —
    a zero-evidence run is identical to the old fixed order)."""
    monkeypatch.setattr(enh, "_learned_scan_enabled", lambda: True)
    monkeypatch.setattr(enh, "_area_success_weight", lambda area: 1.0)

    seed = [
        enh._opportunity("reliability", "s1", "q1"),
        enh._opportunity("performance", "s2", "q2"),
        enh._opportunity("data_coverage", "s3", "q3"),
    ]
    monkeypatch.setattr(enh, "_scan_canonical",
                        lambda out: out.extend(dict(o) for o in seed))
    monkeypatch.setattr(enh, "_scan_findings", lambda out, limit=12: None)
    monkeypatch.setattr(enh, "_scan_health_baseline", lambda out: None)
    monkeypatch.setattr(enh, "_scan_work_plan", lambda out: None)

    opps = enh.scan_opportunities()
    assert [o["area"] for o in opps] == ["reliability", "performance", "data_coverage"]


def test_learned_scan_falls_back_when_source_errors(monkeypatch):
    """When the learned-state source RAISES, scan_opportunities falls back to the
    PRIOR (fixed) order WITHOUT raising — best-effort, never crashes."""
    monkeypatch.setattr(enh, "_learned_scan_enabled", lambda: True)

    seed = [
        enh._opportunity("reliability", "s1", "q1"),
        enh._opportunity("performance", "s2", "q2"),
        enh._opportunity("data_coverage", "s3", "q3"),
    ]
    monkeypatch.setattr(enh, "_scan_canonical",
                        lambda out: out.extend(dict(o) for o in seed))
    monkeypatch.setattr(enh, "_scan_findings", lambda out, limit=12: None)
    monkeypatch.setattr(enh, "_scan_health_baseline", lambda out: None)
    monkeypatch.setattr(enh, "_scan_work_plan", lambda out: None)

    # The learned-state read blows up — the reorder must swallow it and keep the
    # original order.
    def _boom(area):
        raise RuntimeError("learned-state DB down")
    monkeypatch.setattr(enh, "_area_success_weight", _boom)

    opps = enh.scan_opportunities()  # must NOT raise
    assert [o["area"] for o in opps] == ["reliability", "performance", "data_coverage"]


def test_learned_scan_disabled_keeps_fixed_order(monkeypatch):
    """BRAIN_ENHANCER_LEARNED_SCAN off -> the reorder is SKIPPED; the fixed
    canonical->findings->baseline->work_plan order is preserved even if the
    learned signal would have favored a later area."""
    monkeypatch.setattr(enh, "_learned_scan_enabled", lambda: False)

    seed = [
        enh._opportunity("performance", "s1", "q1"),
        enh._opportunity("data_coverage", "s2", "q2"),
    ]
    monkeypatch.setattr(enh, "_scan_canonical",
                        lambda out: out.extend(dict(o) for o in seed))
    monkeypatch.setattr(enh, "_scan_findings", lambda out, limit=12: None)
    monkeypatch.setattr(enh, "_scan_health_baseline", lambda out: None)
    monkeypatch.setattr(enh, "_scan_work_plan", lambda out: None)

    # Even though the learned signal STRONGLY favors data_coverage, the disabled
    # flag means it is never consulted → fixed order.
    def _should_not_be_called(area):
        raise AssertionError("learned weight must not be read when flag is off")
    monkeypatch.setattr(enh, "_area_success_weight", _should_not_be_called)

    opps = enh.scan_opportunities()
    assert [o["area"] for o in opps] == ["performance", "data_coverage"]


def test_area_success_weight_neutral_for_unmapped_area(monkeypatch):
    """An AREA with no mechanical fix-class proxy is NEUTRAL (1.0) — explored,
    never starved. And a DB error in the vetted reader is fail-neutral too."""
    # conversion_revenue / developer_ux have no class proxy -> neutral, no DB hit.
    assert enh._area_success_weight("conversion_revenue") == 1.0
    assert enh._area_success_weight("developer_ux") == 1.0
    # Unknown area -> neutral.
    assert enh._area_success_weight("totally_unknown_area") == 1.0
    # A mapped area whose vetted reader RAISES -> fail-neutral (1.0), not a crash.
    import routes.brain_work_selector as ws
    monkeypatch.setattr(ws, "_read_class_rate",
                        lambda k: (_ for _ in ()).throw(RuntimeError("db")))
    assert enh._area_success_weight("reliability") == 1.0


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


def test_enhance_runs_sync_and_stores_proposals(client, monkeypatch):
    """SYNC POST runs propose_enhancements in-request (under the time budget),
    STORES proposals (PROPOSE-ONLY), marks the run done, and returns 200 with the
    proposals inline."""
    monkeypatch.setattr(enh, "_enabled", lambda: True)
    monkeypatch.setattr(enh, "_enqueue_run", lambda mp: 77)
    monkeypatch.setattr(enh, "propose_enhancements", lambda max_proposals=2: {
        "opportunities": [{"area": "reliability", "signal": "s", "question": "q"}],
        "proposals": [{"title": "[reliability] s", "area": "reliability",
                       "recommendation": "do X", "confidence": 0.8,
                       "leverage_rank": 0.8, "caveats": [], "refutation": {}}],
        "model": "claude-opus-4-8",
    })
    stored = []
    marked = {}
    monkeypatch.setattr(enh, "_store_proposal",
                        lambda rid, p: stored.append((rid, p)) or len(stored))
    monkeypatch.setattr(enh, "_mark_run",
                        lambda rid, status, result=None: marked.update(
                            rid=rid, status=status, result=result) or True)
    resp = client.post("/api/v1/brain/enhance", json={"max": 2})
    # SYNC + DURABLE: 200, proposals inline, run stored + marked done.
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["run_id"] == 77
    assert data["status"] == "done"
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["recommendation"] == "do X"
    # PROPOSE-ONLY: the only writes are storing the proposal + marking the run.
    assert len(stored) == 1 and stored[0][0] == 77
    assert stored[0][1]["recommendation"] == "do X"
    assert marked["rid"] == 77 and marked["status"] == "done"
    assert marked["result"]["proposals_stored"] == 1


def test_enhance_storage_unavailable_still_returns_proposals(client, monkeypatch):
    """No DB to track the run -> still PROPOSE-ONLY answers inline (run_id None,
    200) rather than failing the caller."""
    monkeypatch.setattr(enh, "_enabled", lambda: True)
    monkeypatch.setattr(enh, "_enqueue_run", lambda mp: None)
    monkeypatch.setattr(enh, "propose_enhancements", lambda max_proposals=2: {
        "opportunities": [],
        "proposals": [{"title": "t", "area": "reliability", "recommendation": "do Y",
                       "confidence": 0.5, "leverage_rank": 0.5, "caveats": [],
                       "refutation": {}}],
        "model": "m"})
    monkeypatch.setattr(enh, "_store_proposal", lambda rid, p: None)
    resp = client.post("/api/v1/brain/enhance", json={"max": 2})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["run_id"] is None
    assert data["proposals"][0]["recommendation"] == "do Y"


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
