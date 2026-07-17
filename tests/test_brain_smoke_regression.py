"""smoke_regression recipe tests — FULLY MOCKED GitHub + LLM + DB + probes.

NO real token, NO network, NO DB, and NEVER imports main.py. We monkeypatch
the module-level primitives in routes.brain_smoke_regression:
    recent_smoke_runs / _smoke_checks / _probe_once / _hard_burn_probe_paths /
    compare_window / _current_excerpts / _route_error_patterns / _call_llm /
    open_fix_pr internals (open_draft_pr_with_content) / _pr_state /
    _close_pr / dispatch_smoke_workflow / _dispatch_run_since / escalate GH
    primitives (_gh_get/_gh_post) / all _db-backed stores.

SAFETY INVARIANTS asserted:
  · kill switch ⇒ tick returns {disabled}, touches NOTHING (exploding stubs).
  · dry_run ⇒ zero writes (exploding write stubs).
  · streak detection: >=2 consecutive completed failures triggers; a success
    run breaks the streak; first_red = OLDEST red in the streak; last_green =
    newest green before it (the deploy window is last_green..first_red).
  · fix validation fail-closes: file outside deploy window, forbidden path,
    non-unique search_text, oversize diff, broken AST, added import,
    forbidden tokens — all rejected.
  · schema-drift signature (the 07-16 'operator does not exist: text =
    integer' root cause) is classified so the prompt carries the guidance.
  · merge path: gauntlet red (terminal check failure) ⇒ PR closed + attempt
    failed; CI pending ⇒ wait; automerge flags not armed ⇒ held, NO merge.
  · landing: drain window honored before dispatch; red landing retries once
    then fails the attempt; green landing requires the probe ALSO live-green.
  · escalate-and-STOP: attempts >= cap ⇒ single high-priority issue, state
    escalated; a <24h escalated incident blocks re-detection for the probe.

Run:  python3 -m pytest tests/test_brain_smoke_regression.py -v
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── flask/psycopg2-free import shims (mirrors test_brain_automerge) ──
for _name in ("flask", "psycopg2", "requests"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

import routes.brain_smoke_regression as sr  # noqa: E402


def _explode(*_a, **_k):
    raise AssertionError("this primitive must NOT be called on this path")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SMOKE_REGRESSION_DISABLE", "DCHUB_L22_REAL_PR",
                "SMOKEREG_CONSEC_FAILS", "SMOKEREG_MAX_ATTEMPTS",
                "SMOKEREG_DAILY_CAP", "SMOKEREG_LANDING_WAIT_S",
                "SMOKEREG_LANDING_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    yield


# ═════════════════════════════════════════════════════════════════════
# Streak detection (pure)
# ═════════════════════════════════════════════════════════════════════
def _run(rid, concl, sha="s" + "0" * 7, event="schedule"):
    return {"id": rid, "conclusion": concl, "created_at": f"t{rid}",
            "head_sha": sha, "event": event}


def test_streak_triggers_on_two_consecutive_failures():
    runs = [_run(5, "failure", "red2"), _run(4, "failure", "red1"),
            _run(3, "success", "grn"), _run(2, "failure"), _run(1, "success")]
    s = sr.detect_streak(runs, min_consec=2)
    assert s["triggered"] is True
    assert s["consec_fails"] == 2
    # first_red = OLDEST red in the leading streak; last_green = newest green.
    assert s["first_red"]["head_sha"] == "red1"
    assert s["newest_red"]["head_sha"] == "red2"
    assert s["last_green"]["head_sha"] == "grn"


def test_streak_broken_by_success():
    runs = [_run(3, "failure"), _run(2, "success"), _run(1, "failure")]
    s = sr.detect_streak(runs, min_consec=2)
    assert s["triggered"] is False
    assert s["consec_fails"] == 1


def test_streak_ignores_cancelled_runs():
    runs = [_run(4, "failure", "red2"), _run(3, "cancelled"),
            _run(2, "failure", "red1"), _run(1, "success", "grn")]
    s = sr.detect_streak(runs, min_consec=2)
    assert s["triggered"] is True
    assert s["consec_fails"] == 2
    assert s["first_red"]["head_sha"] == "red1"
    assert s["last_green"]["head_sha"] == "grn"


def test_streak_no_green_yet_still_triggers():
    runs = [_run(2, "failure", "r2"), _run(1, "failure", "r1")]
    s = sr.detect_streak(runs, min_consec=2)
    assert s["triggered"] is True
    assert s["last_green"] is None


# ═════════════════════════════════════════════════════════════════════
# Live probe identification
# ═════════════════════════════════════════════════════════════════════
_CHECKS = [
    ("health", "/health", "GET", False, 10, 200),
    ("search", "/api/v1/search?q=x", "GET", False, 15, (200, 429)),
    ("map", "/api/v1/map?limit=2", "GET", False, 15, 200),
    ("fiber", "/api/fiber/routes?limit=2", "GET", False, 15, 200),
]


def test_probe_failures_picks_first_failing(monkeypatch):
    monkeypatch.setattr(sr, "_smoke_checks", lambda: _CHECKS)

    def probe(path, timeout=6.0):
        if "map" in path:
            return 500, "operator does not exist: text = integer"
        if "fiber" in path:
            return 500, "also red but later in the table"
        return 200, "ok"
    monkeypatch.setattr(sr, "_probe_once", probe)
    fails = sr.live_probe_failures()
    assert len(fails) == 1  # one incident at a time
    assert fails[0]["name"] == "map"
    assert fails[0]["status"] == 500


def test_probe_tuple_expected_status_is_honored(monkeypatch):
    monkeypatch.setattr(sr, "_smoke_checks", lambda: _CHECKS)
    monkeypatch.setattr(sr, "_probe_once",
                        lambda p, timeout=6.0:
                        (429, "cap") if "search" in p else (200, "ok"))
    assert sr.live_probe_failures() == []  # 429 is allowed for search


# ═════════════════════════════════════════════════════════════════════
# Schema-drift signature (issue #1604 folded in)
# ═════════════════════════════════════════════════════════════════════
def test_schema_drift_signature_classifies_0716_root_cause():
    assert sr.classify_error_text(
        "psycopg2.errors.UndefinedFunction: operator does not exist: "
        "text = integer") == "schema_drift"
    assert sr.classify_error_text(
        'column "carrier_id" does not exist') == "schema_drift"
    assert sr.classify_error_text("some 500 with no signature") is None


# ═════════════════════════════════════════════════════════════════════
# Fix validation (fail-closed)
# ═════════════════════════════════════════════════════════════════════
_GOOD_CONTENT = (
    "def facility_by_slug(slug):\n"
    "    q = 'SELECT * FROM carriers WHERE facility_id = f.legacy_id'\n"
    "    return q\n")

_PACK = {
    "window": {"ok": True, "files": [{"filename": "routes/facility.py",
                                      "patch": "@@ -1 +1 @@"}]},
    "probe": {"name": "map", "path": "/api/v1/map?limit=2"},
}


def _patch_main_fetch(monkeypatch, content=_GOOD_CONTENT, ok=True):
    mod = types.ModuleType("routes.brain_draft_pr_writer")
    mod.get_file_on_main = lambda fp: (
        {"ok": True, "content": content} if ok
        else {"ok": False, "error": "contents_404"})
    mod._gh_config = lambda: {"token": "", "upstream": "o/r", "base": "main"}
    monkeypatch.setitem(sys.modules, "routes.brain_draft_pr_writer", mod)


def _patch_classifier(monkeypatch, hits=None):
    mod = types.ModuleType("routes.brain_mechanical_classifier")
    mod._forbidden_path_hits = lambda p: list(hits or [])
    mod._admin_ok = lambda: True
    monkeypatch.setitem(sys.modules,
                        "routes.brain_mechanical_classifier", mod)


def test_validate_accepts_minimal_unique_fix(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    fix = {"file_path": "routes/facility.py",
           "search_text": "WHERE facility_id = f.legacy_id",
           "replace_text": "WHERE facility_id = f.id",
           "rationale": "join in the true id-space", "confidence": 0.9}
    v = sr.validate_fix(fix, _PACK)
    assert v["ok"], v["reasons"]
    assert "f.id" in v["new_content"]


def test_validate_rejects_file_outside_window(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    v = sr.validate_fix({"file_path": "routes/other.py",
                         "search_text": "WHERE facility_id = f.legacy_id",
                         "replace_text": "x = 1  # changed"}, _PACK)
    assert not v["ok"]
    assert any("file_not_in_deploy_window" in r for r in v["reasons"])


def test_validate_rejects_forbidden_path(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch, hits=["billing"])
    v = sr.validate_fix({"file_path": "routes/facility.py",
                         "search_text": "WHERE facility_id = f.legacy_id",
                         "replace_text": "WHERE facility_id = f.id"}, _PACK)
    assert not v["ok"]
    assert any(r.startswith("forbidden_path:") for r in v["reasons"])


def test_validate_rejects_non_unique_search(monkeypatch):
    _patch_main_fetch(monkeypatch,
                      content=_GOOD_CONTENT + _GOOD_CONTENT)
    _patch_classifier(monkeypatch)
    v = sr.validate_fix({"file_path": "routes/facility.py",
                         "search_text": "WHERE facility_id = f.legacy_id",
                         "replace_text": "WHERE facility_id = f.id"}, _PACK)
    assert not v["ok"]
    assert any("not_exactly_once" in r for r in v["reasons"])


def test_validate_rejects_oversize_diff(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    big = "\n".join(f"line{i}" for i in range(40))
    v = sr.validate_fix({"file_path": "routes/facility.py",
                         "search_text": "WHERE facility_id = f.legacy_id",
                         "replace_text": big}, _PACK)
    assert not v["ok"]
    assert any(r.startswith("diff_too_large") for r in v["reasons"])


def test_validate_rejects_broken_ast(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    v = sr.validate_fix({"file_path": "routes/facility.py",
                         "search_text": "def facility_by_slug(slug):",
                         "replace_text": "def facility_by_slug(slug):((("},
                        _PACK)
    assert not v["ok"]
    assert any(r.startswith("ast_parse_failed") for r in v["reasons"])


def test_validate_rejects_new_import_and_forbidden_tokens(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    v = sr.validate_fix({"file_path": "routes/facility.py",
                         "search_text": "WHERE facility_id = f.legacy_id",
                         "replace_text": "import os  # sneak"}, _PACK)
    assert not v["ok"]
    assert "adds_an_import" in v["reasons"]
    v2 = sr.validate_fix({"file_path": "routes/facility.py",
                          "search_text": "WHERE facility_id = f.legacy_id",
                          "replace_text": "DELETE FROM carriers"}, _PACK)
    assert not v2["ok"]
    assert any("forbidden_diff_token" in r for r in v2["reasons"])


def test_validate_rejects_workflow_files(monkeypatch):
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    pack = {"window": {"ok": True, "files": [
        {"filename": ".github/workflows/x.yml", "patch": ""}]}}
    v = sr.validate_fix({"file_path": ".github/workflows/x.yml",
                         "search_text": "cron: '0 0 * * *'",
                         "replace_text": "cron: '5 0 * * *'"}, pack)
    assert not v["ok"]
    assert "workflow_files_off_limits" in v["reasons"]


# ═════════════════════════════════════════════════════════════════════
# Kill switch + dry-run make ZERO writes
# ═════════════════════════════════════════════════════════════════════
def test_kill_switch_touches_nothing(monkeypatch):
    monkeypatch.setenv("SMOKE_REGRESSION_DISABLE", "1")
    for prim in ("recent_smoke_runs", "_insert_incident", "_update_incident",
                 "_ensure_table", "_active_incidents", "_call_llm",
                 "dispatch_smoke_workflow", "_gh_post"):
        monkeypatch.setattr(sr, prim, _explode)
    out = sr.smoke_regression_tick()
    assert out["disabled"] is True


def test_dry_run_is_read_only(monkeypatch):
    monkeypatch.setattr(sr, "recent_smoke_runs",
                        lambda limit=30: [_run(2, "failure", "r"),
                                          _run(1, "failure", "r")])
    monkeypatch.setattr(sr, "_smoke_checks", lambda: _CHECKS)
    monkeypatch.setattr(sr, "_probe_once",
                        lambda p, timeout=6.0:
                        (500, "boom") if "map" in p else (200, "ok"))
    monkeypatch.setattr(sr, "_recent_incident_for_probe", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_active_incidents", lambda: [])
    for prim in ("_insert_incident", "_update_incident", "_ensure_table",
                 "_call_llm", "dispatch_smoke_workflow", "_gh_post"):
        monkeypatch.setattr(sr, prim, _explode)
    out = sr.smoke_regression_tick(dry_run=True)
    assert out["dry_run"] is True
    assert out["detection"]["detected"] is True
    assert out["detection"]["would_create"]["probe_name"] == "map"


# ═════════════════════════════════════════════════════════════════════
# Detection guards: STOP after escalation, one active incident per probe
# ═════════════════════════════════════════════════════════════════════
def _arm_detection(monkeypatch):
    monkeypatch.setattr(sr, "recent_smoke_runs",
                        lambda limit=30: [_run(9, "failure", "red2"),
                                          _run(8, "failure", "red1"),
                                          _run(7, "success", "grn")])
    monkeypatch.setattr(sr, "_smoke_checks", lambda: _CHECKS)
    monkeypatch.setattr(sr, "_probe_once",
                        lambda p, timeout=6.0:
                        (500, "err") if "map" in p else (200, "ok"))


def test_escalated_incident_blocks_redetection(monkeypatch):
    _arm_detection(monkeypatch)
    monkeypatch.setattr(sr, "_recent_incident_for_probe",
                        lambda *a, **k: {"state": "escalated"})
    monkeypatch.setattr(sr, "_insert_incident", _explode)
    out = sr.detect_new_incident()
    assert out["detected"] is False
    assert "STOP" in out["note"]


def test_active_incident_blocks_duplicate(monkeypatch):
    _arm_detection(monkeypatch)
    monkeypatch.setattr(sr, "_recent_incident_for_probe",
                        lambda *a, **k: {"state": "pr_opened"})
    monkeypatch.setattr(sr, "_insert_incident", _explode)
    out = sr.detect_new_incident()
    assert out["detected"] is False


def test_detection_creates_incident_with_window(monkeypatch):
    _arm_detection(monkeypatch)
    monkeypatch.setattr(sr, "_recent_incident_for_probe", lambda *a, **k: None)
    created = {}
    monkeypatch.setattr(sr, "_insert_incident",
                        lambda inc: created.update(inc) or 1)
    out = sr.detect_new_incident()
    assert out["detected"] and out["created"]
    assert created["last_green_sha"] == "grn"
    assert created["first_red_sha"] == "red1"
    # Compare window head = NEWEST red (superset containing the guilty
    # commit even when an earlier, different probe started the streak).
    assert created["newest_red_sha"] == "red2"
    assert created["incident_key"] == "map:8"


def test_workflow_red_but_probes_green_no_incident(monkeypatch):
    monkeypatch.setattr(sr, "recent_smoke_runs",
                        lambda limit=30: [_run(2, "failure"), _run(1, "failure")])
    monkeypatch.setattr(sr, "_smoke_checks", lambda: _CHECKS)
    monkeypatch.setattr(sr, "_probe_once", lambda p, timeout=6.0: (200, "ok"))
    monkeypatch.setattr(sr, "_insert_incident", _explode)
    out = sr.detect_new_incident()
    assert out["detected"] is False


# ═════════════════════════════════════════════════════════════════════
# Merge stage: gauntlet-gated + armed-flags-gated
# ═════════════════════════════════════════════════════════════════════
_INC = {"id": 1, "incident_key": "map:8", "probe_name": "map",
        "probe_path": "/api/v1/map?limit=2", "attempts": 1,
        "pr_number": 42, "file_path": "routes/facility.py",
        "state": "pr_opened"}


def _patch_automerge(monkeypatch, *, ci_green=True, ci_reason="all_green",
                     enabled=True, dry=False):
    mod = types.ModuleType("routes.brain_automerge")
    mod.ci_status_for_sha = lambda sha: {"green": ci_green,
                                         "reason": ci_reason}
    mod.mark_ready_for_review = lambda nid: {"ok": True}
    merged = {}
    mod.squash_merge_pr = lambda n, sha=None, commit_title=None: (
        merged.update(n=n, title=commit_title) or
        {"ok": True, "merge_sha": "m" * 8})
    mod._enabled = lambda: enabled
    mod._dry_run = lambda: dry
    mod.breaker_tripped = lambda: False
    mod.health_db_green = lambda: True
    monkeypatch.setitem(sys.modules, "routes.brain_automerge", mod)
    return merged


def test_merge_happens_when_gauntlet_green_and_armed(monkeypatch):
    merged = _patch_automerge(monkeypatch)
    monkeypatch.setattr(sr, "_pr_state",
                        lambda n: {"ok": True, "head_sha": "h" * 8,
                                   "draft": True, "state": "open",
                                   "merged": False, "node_id": "N1"})
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    step = sr._advance_pr_opened(dict(_INC))
    assert step["action"] == "merged"
    assert merged["n"] == 42
    assert "smoke_regression" in merged["title"]
    assert updates["state"] == "merged"


def test_gauntlet_red_closes_pr_and_fails_attempt(monkeypatch):
    _patch_automerge(monkeypatch, ci_green=False,
                     ci_reason="check_run_failure")
    monkeypatch.setattr(sr, "_pr_state",
                        lambda n: {"ok": True, "head_sha": "h" * 8,
                                   "draft": True, "state": "open",
                                   "merged": False, "node_id": "N1"})
    closed = {}
    monkeypatch.setattr(sr, "_close_pr",
                        lambda n, comment="": closed.update(n=n) or {"ok": True})
    monkeypatch.setattr(sr, "_update_incident", lambda i, **f: True)
    step = sr._advance_pr_opened(dict(_INC))
    assert closed["n"] == 42
    assert step["action"] == "retry_queued"  # attempt 1 of 2 → retry


def test_ci_pending_waits_no_merge(monkeypatch):
    _patch_automerge(monkeypatch, ci_green=False,
                     ci_reason="check_run_pending")
    monkeypatch.setattr(sr, "_pr_state",
                        lambda n: {"ok": True, "head_sha": "h" * 8,
                                   "draft": True, "state": "open",
                                   "merged": False, "node_id": "N1"})
    monkeypatch.setattr(sr, "_close_pr", _explode)
    step = sr._advance_pr_opened(dict(_INC))
    assert step["action"] == "waiting"


def test_disarmed_flags_hold_merge(monkeypatch):
    _patch_automerge(monkeypatch, enabled=False)
    monkeypatch.setattr(sr, "_pr_state",
                        lambda n: {"ok": True, "head_sha": "h" * 8,
                                   "draft": True, "state": "open",
                                   "merged": False, "node_id": "N1"})
    step = sr._advance_pr_opened(dict(_INC))
    assert step["action"] == "held"
    assert "BRAIN_AUTOMERGE_ENABLED" in step["why"]


# ═════════════════════════════════════════════════════════════════════
# Landing verification: drain wait, retry-once, probe must be green too
# ═════════════════════════════════════════════════════════════════════
def test_landing_waits_for_drain_window(monkeypatch):
    import time as _t
    inc = {**_INC, "state": "merged", "merged_epoch": _t.time() - 10}
    monkeypatch.setattr(sr, "dispatch_smoke_workflow", _explode)
    step = sr._advance_merged(inc)
    assert step["action"] == "waiting"
    assert "drain_window" in step["why"]


def test_landing_dispatches_after_wait(monkeypatch):
    import time as _t
    inc = {**_INC, "state": "merged",
           "merged_epoch": _t.time() - sr._landing_wait_s() - 5}
    monkeypatch.setattr(sr, "dispatch_smoke_workflow", lambda: {"ok": True})
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    step = sr._advance_merged(inc)
    assert step["action"] == "landing_dispatched"
    assert updates["state"] == "landing_dispatched"


def test_landing_green_requires_probe_green(monkeypatch):
    import time as _t
    inc = {**_INC, "state": "landing_dispatched",
           "dispatched_epoch": _t.time() - 600, "landing_retries": 0,
           "merge_sha": "m" * 8}
    monkeypatch.setattr(sr, "_dispatch_run_since",
                        lambda s: {"found": True, "completed": True,
                                   "conclusion": "success", "id": 99})
    monkeypatch.setattr(sr, "_probe_now_green", lambda i: True)
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    step = sr._advance_landing(inc)
    assert step["action"] == "resolved"
    assert updates["state"] == "resolved"
    assert "LANDED" in updates["detail"]


def test_landing_red_retries_once_then_fails(monkeypatch):
    import time as _t
    inc = {**_INC, "state": "landing_dispatched",
           "dispatched_epoch": _t.time() - 600, "landing_retries": 0}
    monkeypatch.setattr(sr, "_dispatch_run_since",
                        lambda s: {"found": True, "completed": True,
                                   "conclusion": "failure", "id": 99})
    redispatched = {}
    monkeypatch.setattr(sr, "dispatch_smoke_workflow",
                        lambda: redispatched.update(x=1) or {"ok": True})
    monkeypatch.setattr(sr, "_update_incident", lambda i, **f: True)
    step = sr._advance_landing(inc)
    assert step["action"] == "landing_retry"          # drain-guard retry
    assert redispatched.get("x") == 1

    inc2 = {**inc, "landing_retries": 1, "attempts": 1}
    monkeypatch.setattr(sr, "dispatch_smoke_workflow", _explode)
    step2 = sr._advance_landing(inc2)
    assert step2["action"] == "retry_queued"          # attempt failed → retry


# ═════════════════════════════════════════════════════════════════════
# Escalate-and-STOP after the attempt cap
# ═════════════════════════════════════════════════════════════════════
def test_fail_attempt_escalates_at_cap(monkeypatch):
    inc = {**_INC, "attempts": 2}   # cap default = 2
    issue = {}
    monkeypatch.setattr(sr, "escalate",
                        lambda i, r: issue.update(reason=r) or
                        {"ok": True, "issue_url": "https://x/1"})
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    step = sr._fail_attempt(inc, "landed_red_after_2_runs")
    assert step["action"] == "escalated"
    assert updates["state"] == "escalated"
    assert updates["escalation_issue_url"] == "https://x/1"
    assert issue["reason"] == "landed_red_after_2_runs"


def test_escalate_dedupes_existing_open_issue(monkeypatch):
    monkeypatch.setattr(sr, "_gh_get",
                        lambda path, params=None, timeout=20: {
                            "ok": True, "json": [{
                                "title": "[smoke-regression] ESCALATION: map "
                                         "probe regression — autofix stopped (x)",
                                "html_url": "https://x/old"}]})
    monkeypatch.setattr(sr, "_gh_post", _explode)
    res = sr.escalate(dict(_INC), "whatever")
    assert res["deduped"] is True
    assert res["issue_url"] == "https://x/old"


# ═════════════════════════════════════════════════════════════════════
# Authoring stage guards: daily cap, external heal, preview-only
# ═════════════════════════════════════════════════════════════════════
def test_daily_cap_holds_authoring(monkeypatch):
    monkeypatch.setattr(sr, "_prs_opened_today", lambda: 2)
    monkeypatch.setattr(sr, "_call_llm", _explode)
    monkeypatch.setattr(sr, "_probe_now_green", _explode)
    import time as _t
    step = sr._advance_detected({**_INC, "state": "detected"}, _t.time())
    assert step["action"] == "held"
    assert "daily_cap" in step["why"]


def test_externally_healed_incident_resolves_without_pr(monkeypatch):
    monkeypatch.setattr(sr, "_prs_opened_today", lambda: 0)
    monkeypatch.setattr(sr, "_probe_now_green", lambda i: True)
    monkeypatch.setattr(sr, "_call_llm", _explode)
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    import time as _t
    step = sr._advance_detected({**_INC, "state": "detected"}, _t.time())
    assert step["action"] == "resolved"
    assert updates["state"] == "resolved"


def test_preview_only_without_real_pr_flag(monkeypatch):
    monkeypatch.setattr(sr, "_prs_opened_today", lambda: 0)
    monkeypatch.setattr(sr, "_probe_now_green", lambda i: False)
    monkeypatch.setattr(sr, "build_context_pack", lambda i: dict(_PACK))
    monkeypatch.setattr(sr, "author_fix",
                        lambda p: {"file_path": "routes/facility.py",
                                   "search_text": "WHERE facility_id = f.legacy_id",
                                   "replace_text": "WHERE facility_id = f.id",
                                   "rationale": "r", "confidence": 0.9})
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    monkeypatch.setattr(sr, "open_fix_pr", _explode)  # must NOT open
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    import time as _t
    step = sr._advance_detected({**_INC, "state": "detected"}, _t.time())
    assert step["action"] == "preview_only"
    assert "PREVIEW" in updates["detail"]


def test_pr_opened_records_state_and_attempt(monkeypatch):
    monkeypatch.setenv("DCHUB_L22_REAL_PR", "1")
    monkeypatch.setattr(sr, "_prs_opened_today", lambda: 0)
    monkeypatch.setattr(sr, "_probe_now_green", lambda i: False)
    monkeypatch.setattr(sr, "build_context_pack", lambda i: dict(_PACK))
    monkeypatch.setattr(sr, "author_fix",
                        lambda p: {"file_path": "routes/facility.py",
                                   "search_text": "WHERE facility_id = f.legacy_id",
                                   "replace_text": "WHERE facility_id = f.id",
                                   "rationale": "r", "confidence": 0.9})
    _patch_main_fetch(monkeypatch)
    _patch_classifier(monkeypatch)
    monkeypatch.setattr(sr, "open_fix_pr",
                        lambda i, p, v: {"ok": True, "branch": "brain/smokefix-x",
                                         "pr_url": "https://gh/x/pull/77"})
    updates = {}
    monkeypatch.setattr(sr, "_update_incident",
                        lambda i, **f: updates.update(f) or True)
    import time as _t
    step = sr._advance_detected({**_INC, "state": "detected", "attempts": 0},
                                _t.time())
    assert step["action"] == "pr_opened"
    assert updates["state"] == "pr_opened"
    assert updates["pr_number"] == 77
    assert updates["attempts"] == 1
