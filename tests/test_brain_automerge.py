"""PHASE 3 — autonomous-merge + canary tests. FULLY MOCKED GitHub + health.

NO real token, NO real merge/revert, NO network, NO DB. We monkeypatch the
module-level GitHub / health / DB primitives in routes.brain_automerge:
    list_autofix_prs / ci_status_for_sha / mark_ready_for_review /
    squash_merge_pr / health_db_green / core_endpoint_ok /
    new_runtime_errors_since / record_merge / breaker_tripped / trip_breaker /
    _open_merge_rows / _set_row_status / _alert_operator / _do_auto_revert
    and the writer's get_file_on_main / open_draft_pr_with_content /
    get_logged_proposal_for_branch.

We assert the SAFETY INVARIANTS on the call args:
  · BRAIN_AUTOMERGE_ENABLED unset ⇒ run_automerge returns {disabled} and makes
    ZERO GitHub calls (a sentinel that explodes if ANY primitive is touched).
  · enabled + green mechanical autofix PR ⇒ markReady THEN squash-merge called
    with the right PR/repo/sha.
  · pending / failing / zero CI ⇒ NOT merged (fail-closed).
  · a non-brain/autofix-* PR ⇒ ignored.
  · rate cap respected.
  · canary with simulated regression ⇒ inverse replace→search revert PR +
    breaker tripped + operator alerted.
  · canary healthy ⇒ status=clean.
  · breaker tripped ⇒ run_automerge refuses (zero GitHub calls).
  · NO force-merge / NO admin-override / NO merge with red or pending CI.

The module imports cleanly with NO flask (its blueprint is built lazily).
classify_mechanical lives in a flask-importing module, lazily imported only
when run_automerge actually runs, so we install flask/internal_auth shims.

Run:  python3 -m pytest tests/test_brain_automerge.py -v
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_automerge as am  # noqa: E402  (imports cleanly w/o flask)
import routes.brain_draft_pr_writer as w  # noqa: E402


@pytest.fixture(autouse=True)
def _flask_shim():
    """Install flask/internal_auth shims when absent; restore on teardown.
    Drop the cached classifier so it re-imports under the shim."""
    saved = {k: sys.modules.get(k) for k in
             ("flask", "internal_auth", "routes.brain_mechanical_classifier")}
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


# ── A real mechanical (interval_literal) proposal + live-main content ──
_SEARCH = "NOW() - INTERVAL '%s days'"
_REPLACE = "NOW() - %s * INTERVAL '1 day'"

_MAIN_BEFORE = (
    "def recent(days):\n"
    "    cur.execute(\"SELECT 1 FROM t WHERE ts > " + _SEARCH + "\", (days,))\n"
    "    return cur.fetchall()\n"
)
# post-merge content (the replace landed) — used by the canary's revert path.
_MAIN_AFTER = _MAIN_BEFORE.replace(_SEARCH, _REPLACE, 1)


def _logged_proposal(pid=4242, file_path="routes/example.py"):
    return {
        "id": pid, "file_path": file_path,
        "search_text": _SEARCH, "replace_text": _REPLACE,
        "klass": "interval_literal", "confidence": 0.95,
        "changes_json": None, "status": "pr_opened",
    }


def _autofix_pr(number=999, draft=True, sha="headsha999",
                ref="brain/autofix-interval_literal-4242-abc12345"):
    return {"number": number, "node_id": f"NODE_{number}", "head_ref": ref,
            "head_sha": sha, "draft": draft, "title": "[brain-autofix] x",
            "body": "b"}


class _Boom:
    """A sentinel callable that EXPLODES if ever invoked — proves the
    disabled / breaker paths make ZERO GitHub calls."""
    def __init__(self, label):
        self.label = label

    def __call__(self, *a, **k):
        raise AssertionError(f"GitHub primitive '{self.label}' was called on a "
                             f"path that must make ZERO calls")


def _arm_all_boom(monkeypatch):
    """Wire EVERY GitHub/health/DB-write primitive to explode."""
    for name in ("list_autofix_prs", "ci_status_for_sha", "mark_ready_for_review",
                 "squash_merge_pr", "health_db_green", "record_merge",
                 "_do_auto_revert", "trip_breaker", "core_endpoint_ok"):
        monkeypatch.setattr(am, name, _Boom(name))


def _wire_merge_happy(monkeypatch, *, prs, ci_green=True, ci_reason="all_green",
                      ci_checks=2, health=True, main_content=_MAIN_BEFORE,
                      breaker=False):
    """Mock the full merge dependency surface; return a capture dict."""
    calls = {"mark_ready": [], "merge": [], "record": [], "fetch": []}

    monkeypatch.setattr(am, "breaker_tripped", lambda: breaker)
    monkeypatch.setattr(am, "health_db_green", lambda: health)
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": list(prs)})
    monkeypatch.setattr(am, "ci_status_for_sha",
                        lambda sha: {"green": ci_green, "reason": ci_reason,
                                     "checks": ci_checks})

    def fake_mark(node_id):
        calls["mark_ready"].append(node_id)
        return {"ok": True}

    def fake_merge(pr_number, *, sha=None, commit_title=None):
        calls["merge"].append({"pr": pr_number, "sha": sha,
                               "commit_title": commit_title})
        return {"ok": True, "merge_sha": f"mergesha_{pr_number}"}

    def fake_record(**kw):
        calls["record"].append(kw)
        return True

    monkeypatch.setattr(am, "mark_ready_for_review", fake_mark)
    monkeypatch.setattr(am, "squash_merge_pr", fake_merge)
    monkeypatch.setattr(am, "record_merge", fake_record)

    # Re-verify path: get_file_on_main + the logged-proposal reader.
    def fake_get_file(fp):
        calls["fetch"].append(fp)
        return {"ok": True, "content": main_content, "sha": "s", "head_sha": "h"}

    monkeypatch.setattr(w, "get_file_on_main", fake_get_file)
    monkeypatch.setattr(w, "get_logged_proposal_for_branch",
                        lambda branch: _logged_proposal())
    return calls


# ══════════════════════════════════════════════════════════════════════
# 1. DISABLED ⇒ {disabled} and ZERO GitHub calls.
# ══════════════════════════════════════════════════════════════════════
def test_disabled_returns_disabled_and_zero_calls(monkeypatch):
    monkeypatch.delenv("BRAIN_AUTOMERGE_ENABLED", raising=False)
    _arm_all_boom(monkeypatch)  # any primitive call would raise
    res = am.run_automerge()
    assert res == {"disabled": True, "merged": [], "skipped": []}, res
    # canary too.
    res2 = am.run_canary()
    assert res2 == {"disabled": True, "evaluated": []}, res2
    print("DISABLED: zero GitHub calls confirmed ->", res, res2)


def test_disabled_when_value_not_exactly_one(monkeypatch):
    for val in ("0", "true", "yes", "2", ""):
        monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", val)
        _arm_all_boom(monkeypatch)
        assert am.run_automerge().get("disabled") is True, val


# ══════════════════════════════════════════════════════════════════════
# 2. ENABLED + green mechanical autofix PR ⇒ markReady THEN squash-merge.
# ══════════════════════════════════════════════════════════════════════
def test_enabled_green_merges_marks_ready_then_squash(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    monkeypatch.setenv("BRAIN_AUTOMERGE_MAX_PER_RUN", "5")
    calls = _wire_merge_happy(monkeypatch, prs=[_autofix_pr()])
    res = am.run_automerge()
    assert len(res["merged"]) == 1, res
    m = res["merged"][0]
    assert m["pr"] == 999 and m["merge_sha"] == "mergesha_999", m
    assert m["klass"] == "interval_literal"
    # markReady called with the PR node_id (draft PR), THEN squash-merge.
    assert calls["mark_ready"] == ["NODE_999"], calls["mark_ready"]
    assert len(calls["merge"]) == 1, calls["merge"]
    mc = calls["merge"][0]
    assert mc["pr"] == 999 and mc["sha"] == "headsha999", mc
    # the merge passes the head sha (no force; abort if branch moved).
    assert "interval_literal" in mc["commit_title"], mc
    assert len(calls["record"]) == 1, calls["record"]
    print("MERGE happy-path: markReady ->", calls["mark_ready"],
          "squash-merge ->", mc)


def test_ready_called_before_merge_ordering(monkeypatch):
    """markPullRequestReadyForReview MUST precede the squash merge."""
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    order = []
    monkeypatch.setattr(am, "breaker_tripped", lambda: False)
    monkeypatch.setattr(am, "health_db_green", lambda: True)
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": [_autofix_pr()]})
    monkeypatch.setattr(am, "ci_status_for_sha",
                        lambda sha: {"green": True, "checks": 2})
    monkeypatch.setattr(am, "mark_ready_for_review",
                        lambda nid: order.append("mark_ready") or {"ok": True})
    monkeypatch.setattr(am, "squash_merge_pr",
                        lambda n, **k: order.append("merge") or {"ok": True, "merge_sha": "x"})
    monkeypatch.setattr(am, "record_merge", lambda **k: True)
    monkeypatch.setattr(w, "get_file_on_main",
                        lambda fp: {"ok": True, "content": _MAIN_BEFORE})
    monkeypatch.setattr(w, "get_logged_proposal_for_branch",
                        lambda b: _logged_proposal())
    am.run_automerge()
    assert order == ["mark_ready", "merge"], order


# ══════════════════════════════════════════════════════════════════════
# 3. CI not green (pending / failing / zero) ⇒ NOT merged (fail-closed).
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reason", ["check_run_pending", "combined_status_failure",
                                    "no_ci_signals", "check_run_failure"])
def test_ci_not_green_not_merged(monkeypatch, reason):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    calls = _wire_merge_happy(monkeypatch, prs=[_autofix_pr()],
                              ci_green=False, ci_reason=reason)
    res = am.run_automerge()
    assert res["merged"] == [], res
    assert any("ci_not_green" in s["reason"] for s in res["skipped"]), res
    assert calls["merge"] == [], "must NOT merge on non-green CI"
    assert calls["mark_ready"] == [], "must not even mark-ready on red CI"


def test_ci_zero_checks_fail_closed_unit(monkeypatch):
    """ci_status_for_sha itself fail-closes on zero CI signals."""
    monkeypatch.setattr(am, "_gh_cfg",
                        lambda: {"token": "t", "upstream": "azmartone67/dchub-backend"})

    class _R:
        def __init__(self, code, p): self.status_code = code; self._p = p; self.text = ""
        def json(self): return self._p
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, **k: (
        _R(200, {"state": "success", "statuses": []}) if "/status" in url
        else _R(200, {"check_runs": []})))
    out = am.ci_status_for_sha("abc")
    assert out["green"] is False and out["reason"] == "no_ci_signals", out


def test_ci_checkruns_only_green_when_combined_status_empty(monkeypatch):
    """REGRESSION: GitHub returns combined state='pending' when there are ZERO
    classic statuses (modern CI is all check-RUNS, not legacy commit statuses).
    The engine must NOT fail-close on that empty 'pending' — it must report
    GREEN when the check-runs are all green. This bug blocked EVERY
    check-runs-only PR from ever auto-merging."""
    monkeypatch.setattr(am, "_gh_cfg",
                        lambda: {"token": "t", "upstream": "azmartone67/dchub-backend"})

    class _R:
        def __init__(self, code, p): self.status_code = code; self._p = p; self.text = ""
        def json(self): return self._p
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, **k: (
        _R(200, {"state": "pending", "statuses": []}) if "/status" in url
        else _R(200, {"check_runs": [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "neutral"},
        ]})))
    out = am.ci_status_for_sha("abc")
    assert out["green"] is True and out["reason"] == "all_green", out


# ══════════════════════════════════════════════════════════════════════
# 4. Non-brain/autofix-* PR ⇒ ignored.
# ══════════════════════════════════════════════════════════════════════
def test_non_autofix_pr_ignored(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    # list_autofix_prs already filters, but defense-in-depth: feed a foreign
    # branch and confirm it is skipped, never merged.
    foreign = _autofix_pr(number=12, ref="feature/human-pr")
    calls = _wire_merge_happy(monkeypatch, prs=[foreign])
    res = am.run_automerge()
    assert res["merged"] == [], res
    assert any(s["pr"] == 12 and s["reason"] == "not_autofix_branch"
               for s in res["skipped"]), res
    assert calls["merge"] == []


def test_list_autofix_prs_filters_foreign_branches(monkeypatch):
    """The list primitive itself drops any non-brain/autofix-* head branch."""
    monkeypatch.setattr(am, "_gh_cfg",
                        lambda: {"token": "t", "upstream": "azmartone67/dchub-backend"})

    class _R:
        def __init__(self, code, p): self.status_code = code; self._p = p; self.text = ""
        def json(self): return self._p
    payload = [
        {"number": 1, "node_id": "n1", "draft": True,
         "head": {"ref": "brain/autofix-x-1-aa", "sha": "s1"}},
        {"number": 2, "node_id": "n2", "draft": False,
         "head": {"ref": "feature/random", "sha": "s2"}},
        {"number": 3, "node_id": "n3", "draft": True,
         "head": {"ref": "main", "sha": "s3"}},
    ]
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, **k: _R(200, payload))
    out = am.list_autofix_prs()
    refs = [p["head_ref"] for p in out["prs"]]
    assert refs == ["brain/autofix-x-1-aa"], refs


# ══════════════════════════════════════════════════════════════════════
# 5. Rate cap respected.
# ══════════════════════════════════════════════════════════════════════
def test_rate_cap_respected(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    monkeypatch.setenv("BRAIN_AUTOMERGE_MAX_PER_RUN", "1")
    prs = [_autofix_pr(number=n, ref=f"brain/autofix-interval_literal-{n}-aa",
                       sha=f"sha{n}") for n in (10, 11, 12)]
    calls = _wire_merge_happy(monkeypatch, prs=prs)
    res = am.run_automerge()
    assert len(res["merged"]) == 1, res            # cap=1
    assert len(calls["merge"]) == 1, calls["merge"]
    capped = [s for s in res["skipped"] if "rate_cap_reached" in s["reason"]]
    assert len(capped) == 2, res


# ══════════════════════════════════════════════════════════════════════
# 6. Re-verify: proposal no longer applies exactly once on main ⇒ skip.
# ══════════════════════════════════════════════════════════════════════
def test_skip_when_search_not_exactly_once_in_main(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    # main no longer contains the search text (already fixed/drifted).
    calls = _wire_merge_happy(monkeypatch, prs=[_autofix_pr()],
                              main_content="def recent(days): return []\n")
    res = am.run_automerge()
    assert res["merged"] == [], res
    assert any("not_exactly_once_in_main" in s["reason"] for s in res["skipped"]), res
    assert calls["merge"] == []


def test_skip_when_no_logged_proposal(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    monkeypatch.setattr(am, "breaker_tripped", lambda: False)
    monkeypatch.setattr(am, "health_db_green", lambda: True)
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": [_autofix_pr()]})
    monkeypatch.setattr(am, "ci_status_for_sha", _Boom("ci_status_for_sha"))
    monkeypatch.setattr(am, "squash_merge_pr", _Boom("squash_merge_pr"))
    monkeypatch.setattr(w, "get_logged_proposal_for_branch", lambda b: None)
    res = am.run_automerge()
    assert res["merged"] == [], res
    assert any("no_logged_proposal" in s["reason"] for s in res["skipped"]), res


# ══════════════════════════════════════════════════════════════════════
# 7. Incident-skip: degraded health ⇒ no merge, zero GitHub list/merge calls.
# ══════════════════════════════════════════════════════════════════════
def test_incident_skip_when_health_degraded(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    monkeypatch.setattr(am, "breaker_tripped", lambda: False)
    monkeypatch.setattr(am, "health_db_green", lambda: False)
    monkeypatch.setattr(am, "list_autofix_prs", _Boom("list_autofix_prs"))
    monkeypatch.setattr(am, "squash_merge_pr", _Boom("squash_merge_pr"))
    res = am.run_automerge()
    assert res.get("skipped_reason") == "health_degraded", res
    assert res["merged"] == []


# ══════════════════════════════════════════════════════════════════════
# 8. Breaker tripped ⇒ run_automerge refuses (zero GitHub calls).
# ══════════════════════════════════════════════════════════════════════
def test_breaker_tripped_refuses(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    monkeypatch.setattr(am, "breaker_tripped", lambda: True)
    # everything else explodes — proves we never reach a GitHub call.
    monkeypatch.setattr(am, "health_db_green", _Boom("health_db_green"))
    monkeypatch.setattr(am, "list_autofix_prs", _Boom("list_autofix_prs"))
    monkeypatch.setattr(am, "squash_merge_pr", _Boom("squash_merge_pr"))
    res = am.run_automerge()
    assert res == {"breaker_tripped": True, "merged": [], "skipped": []}, res


# ══════════════════════════════════════════════════════════════════════
# 9. CANARY regression ⇒ inverse replace→search revert + breaker + alert.
# ══════════════════════════════════════════════════════════════════════
def test_canary_regression_reverts_trips_breaker_alerts(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    now = _t.time()
    merged_row = {
        "id": 5, "pr_number": 999, "proposal_id": 4242,
        "merge_sha": "mergesha_999", "file_path": "routes/example.py",
        "search_text": _SEARCH, "replace_text": _REPLACE,
        "klass": "interval_literal", "status": "merged",
        "merged_epoch": now - 300,  # within window, past warmup
    }
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [merged_row])
    # regression: health degraded.
    monkeypatch.setattr(am, "health_db_green", lambda: False)
    monkeypatch.setattr(am, "core_endpoint_ok", lambda: True)
    monkeypatch.setattr(am, "new_runtime_errors_since", lambda ts: [])

    tripped = {"n": 0, "reasons": []}
    alerts = []
    status_updates = []
    monkeypatch.setattr(am, "trip_breaker",
                        lambda r: (tripped.__setitem__("n", tripped["n"] + 1),
                                   tripped["reasons"].append(r), True)[-1])
    monkeypatch.setattr(am, "_alert_operator",
                        lambda subj, html: alerts.append(subj) or True)
    monkeypatch.setattr(am, "_set_row_status",
                        lambda rid, **k: status_updates.append((rid, k)) or True)

    # The revert path: get_file_on_main returns POST-merge content (replace
    # present); open_draft_pr_with_content captures the INVERSE content;
    # resolve + mark-ready + squash-merge the revert.
    revert_capture = {}

    def fake_get_file(fp):
        return {"ok": True, "content": _MAIN_AFTER, "sha": "s", "head_sha": "h"}

    def fake_open(*, file_path, new_content, branch, title, body, commit_msg):
        revert_capture.update(file_path=file_path, new_content=new_content,
                              branch=branch, title=title)
        return {"ok": True, "branch": branch, "pr_url": "https://x/pull/1000"}

    monkeypatch.setattr(w, "get_file_on_main", fake_get_file)
    monkeypatch.setattr(w, "open_draft_pr_with_content", fake_open)
    monkeypatch.setattr(am, "_resolve_pr_by_branch",
                        lambda b: {"ok": True, "number": 1000, "node_id": "NR",
                                   "head_sha": "rsha", "draft": True})
    mr_calls, merge_calls = [], []
    monkeypatch.setattr(am, "mark_ready_for_review",
                        lambda nid: mr_calls.append(nid) or {"ok": True})
    monkeypatch.setattr(am, "squash_merge_pr",
                        lambda n, **k: merge_calls.append((n, k)) or
                        {"ok": True, "merge_sha": "revertsha"})

    res = am.run_canary()
    ev = [e for e in res["evaluated"] if e.get("state") == "reverted"]
    assert ev and ev[0]["revert_sha"] == "revertsha", res

    # INVERSE applied: replace→search restores _MAIN_BEFORE.
    assert revert_capture["new_content"] == _MAIN_BEFORE, revert_capture
    assert _REPLACE not in revert_capture["new_content"], "replace text must be gone"
    assert _SEARCH in revert_capture["new_content"], "search text restored"
    assert revert_capture["branch"].startswith("brain/autofix-revert-999-"), revert_capture
    # revert PR marked ready THEN merged.
    assert mr_calls == ["NR"], mr_calls
    assert merge_calls and merge_calls[0][0] == 1000, merge_calls
    # breaker tripped + operator alerted + row marked reverted.
    assert tripped["n"] == 1, tripped
    assert alerts and "REVERTED" in alerts[0], alerts
    assert any(k.get("status") == "reverted" for _, k in status_updates), status_updates
    print("CANARY revert: inverse content restored; breaker tripped; alert ->",
          alerts[0])


def test_canary_new_runtime_error_is_regression(monkeypatch):
    """A NEW runtime-error pattern after merge is, on its own, a regression."""
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    now = _t.time()
    row = {"id": 6, "pr_number": 7, "file_path": "routes/example.py",
           "search_text": _SEARCH, "replace_text": _REPLACE,
           "merged_epoch": now - 300}
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [row])
    monkeypatch.setattr(am, "health_db_green", lambda: True)
    monkeypatch.setattr(am, "core_endpoint_ok", lambda: True)
    monkeypatch.setattr(am, "new_runtime_errors_since",
                        lambda ts: [{"pattern": "psycopg2 cast error <n>", "count": 9}])
    reverted = {"hit": False}
    monkeypatch.setattr(am, "_do_auto_revert",
                        lambda r: reverted.__setitem__("hit", True) or
                        {"ok": True, "merge_sha": "rv"})
    monkeypatch.setattr(am, "trip_breaker", lambda r: True)
    monkeypatch.setattr(am, "_alert_operator", lambda s, h: True)
    monkeypatch.setattr(am, "_set_row_status", lambda rid, **k: True)
    res = am.run_canary()
    assert reverted["hit"], res
    assert any(e.get("state") == "reverted" for e in res["evaluated"]), res


# ══════════════════════════════════════════════════════════════════════
# 10. CANARY healthy through window ⇒ status=clean.
# ══════════════════════════════════════════════════════════════════════
def test_canary_clean_after_window(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    now = _t.time()
    row = {"id": 8, "pr_number": 5, "file_path": "routes/example.py",
           "search_text": _SEARCH, "replace_text": _REPLACE,
           "merged_epoch": now - (am.CANARY_WINDOW_S + 60)}  # past the window
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [row])
    # No regression evaluated because age > window ⇒ clean. Make revert explode.
    monkeypatch.setattr(am, "_do_auto_revert", _Boom("_do_auto_revert"))
    monkeypatch.setattr(am, "trip_breaker", _Boom("trip_breaker"))
    cleaned = []
    monkeypatch.setattr(am, "_set_row_status",
                        lambda rid, **k: cleaned.append((rid, k.get("status"))) or True)
    res = am.run_canary()
    assert any(e.get("state") == "clean" for e in res["evaluated"]), res
    assert (8, "clean") in cleaned, cleaned


def test_canary_within_window_healthy_keeps_watching(monkeypatch):
    """Within the window + healthy ⇒ 'watching' (re-check next pass), NOT
    clean yet, NO revert."""
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    now = _t.time()
    row = {"id": 9, "pr_number": 5, "file_path": "routes/example.py",
           "search_text": _SEARCH, "replace_text": _REPLACE,
           "merged_epoch": now - 300}
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [row])
    monkeypatch.setattr(am, "health_db_green", lambda: True)
    monkeypatch.setattr(am, "core_endpoint_ok", lambda: True)
    monkeypatch.setattr(am, "new_runtime_errors_since", lambda ts: [])
    monkeypatch.setattr(am, "_do_auto_revert", _Boom("_do_auto_revert"))
    monkeypatch.setattr(am, "trip_breaker", _Boom("trip_breaker"))
    res = am.run_canary()
    states = [e["state"] for e in res["evaluated"]]
    assert states == ["watching"], states


def test_canary_warmup_skips(monkeypatch):
    """Too-fresh merge (< warmup) is left 'warming' — not judged yet."""
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    row = {"id": 10, "pr_number": 5, "file_path": "routes/example.py",
           "search_text": _SEARCH, "replace_text": _REPLACE,
           "merged_epoch": _t.time() - 10}  # 10s < warmup
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [row])
    monkeypatch.setattr(am, "health_db_green", _Boom("health_db_green"))
    monkeypatch.setattr(am, "_do_auto_revert", _Boom("_do_auto_revert"))
    res = am.run_canary()
    assert [e["state"] for e in res["evaluated"]] == ["warming"], res


# ══════════════════════════════════════════════════════════════════════
# 11. CANARY regression + FAILED revert ⇒ breaker STILL trips + LOUD alert.
# ══════════════════════════════════════════════════════════════════════
def test_canary_failed_revert_still_trips_and_pages(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOMERGE_ENABLED", "1")
    import time as _t
    row = {"id": 11, "pr_number": 999, "file_path": "routes/example.py",
           "search_text": _SEARCH, "replace_text": _REPLACE,
           "merge_sha": "ms", "merged_epoch": _t.time() - 300}
    monkeypatch.setattr(am, "_open_merge_rows", lambda: [row])
    monkeypatch.setattr(am, "health_db_green", lambda: False)  # regression
    monkeypatch.setattr(am, "core_endpoint_ok", lambda: True)
    monkeypatch.setattr(am, "new_runtime_errors_since", lambda ts: [])
    # revert itself raises — exception-safe path must still trip + alert.
    monkeypatch.setattr(am, "_do_auto_revert",
                        lambda r: (_ for _ in ()).throw(RuntimeError("gh 500")))
    tripped, alerts, status = [], [], []
    monkeypatch.setattr(am, "trip_breaker", lambda rs: tripped.append(rs) or True)
    monkeypatch.setattr(am, "_alert_operator", lambda s, h: alerts.append(s) or True)
    monkeypatch.setattr(am, "_set_row_status",
                        lambda rid, **k: status.append(k.get("status")) or True)
    res = am.run_canary()
    assert tripped, "breaker must trip even when revert fails"
    assert alerts and "REVERT FAILED" in alerts[0], alerts
    assert "revert_failed" in status, status
    assert any(e.get("state") == "revert_failed" for e in res["evaluated"]), res
    print("FAILED-REVERT: breaker tripped + LOUD page ->", alerts[0])


# ══════════════════════════════════════════════════════════════════════
# 12. SQUASH-MERGE primitive: squash only, passes sha, NO force/admin-override.
# ══════════════════════════════════════════════════════════════════════
def test_squash_merge_primitive_payload(monkeypatch):
    monkeypatch.setattr(am, "_gh_cfg",
                        lambda: {"token": "t", "upstream": "azmartone67/dchub-backend"})
    seen = {}

    class _R:
        def __init__(self, code, p): self.status_code = code; self._p = p; self.text = ""
        def json(self): return self._p
    import requests as _rq

    def fake_put(url, headers=None, json=None, timeout=None):
        seen["url"] = url; seen["payload"] = json
        return _R(200, {"merged": True, "sha": "msha"})
    monkeypatch.setattr(_rq, "put", fake_put)

    out = am.squash_merge_pr(999, sha="headsha", commit_title="t")
    assert out["ok"] and out["merge_sha"] == "msha", out
    assert seen["payload"]["merge_method"] == "squash", seen
    assert seen["payload"]["sha"] == "headsha", seen
    # NO force / admin-override fields are ever sent.
    assert "merge_method" in seen["payload"] and seen["payload"]["merge_method"] == "squash"
    for forbidden in ("force", "admin", "bypass"):
        assert not any(forbidden in str(k).lower() for k in seen["payload"]), seen
    assert "/pulls/999/merge" in seen["url"], seen


# ══════════════════════════════════════════════════════════════════════
# 13. STATIC SOURCE GUARDS — no force / no admin-override / squash only.
# ══════════════════════════════════════════════════════════════════════
def _code_only(src: str) -> str:
    """Return the source with docstrings + comments stripped, so prose like
    'NEVER branch-protection bypass' in a docstring can't trip a guard that's
    meant to catch a real bypass FIELD in code. Uses ast to drop docstrings,
    then a token-based comment strip."""
    import ast
    import io
    import tokenize
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds:
                docstrings.add(ds)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.string[1:-1].strip("\"'") in {
                ds.strip() for ds in docstrings} or (
                tok.type == tokenize.STRING and any(
                    d in tok.string for d in docstrings)):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_static_source_safety_guards():
    raw = open(os.path.join(ROOT, "routes", "brain_automerge.py"),
               encoding="utf-8").read()
    code = _code_only(raw)
    low = code.lower()
    # squash-merge only — the only merge_method literal is 'squash'.
    import re
    methods = set(re.findall(r"merge_method['\"]?\s*[:=]\s*['\"](\w+)['\"]", code))
    assert methods == {"squash"}, f"merge_method must be squash-only, got {methods}"
    # never pass a force flag or admin-override field to the merge (CODE only).
    assert "\"force\"" not in low and "'force'" not in low, "no force flag in code"
    assert "force_merge" not in low
    for bad in ("bypass", "override_protection", "admin_merge"):
        assert bad not in low, f"code must not reference {bad}"
    # The OFF gate + autofix-only invariant are present in the source.
    assert "_enabled()" in raw
    assert "brain/autofix-" in raw
    print("STATIC GUARDS: squash-only, no force, no admin-override (code-only)")


def test_off_gate_is_first_in_action_paths():
    """run_automerge and run_canary check _enabled() as the FIRST executable
    statement (defensive AST check, not just a substring)."""
    import ast
    src = open(os.path.join(ROOT, "routes", "brain_automerge.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("run_automerge", "run_canary"):
        fn = funcs[name]
        # first non-docstring statement must be `if not _enabled(): return ...`
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)):
            body = body[1:]  # skip docstring
        first = body[0]
        assert isinstance(first, ast.If), f"{name}: first stmt not an if-guard"
        cond_src = ast.unparse(first.test)
        assert "_enabled()" in cond_src, f"{name}: first guard not _enabled(): {cond_src}"
        # the guard's body returns immediately (no GitHub work before it).
        assert any(isinstance(s, ast.Return) for s in first.body), name
    print("OFF-gate is the FIRST statement in run_automerge + run_canary")
