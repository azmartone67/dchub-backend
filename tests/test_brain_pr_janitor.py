"""PHASE 3.5 — brain PR-janitor tests. FULLY MOCKED GitHub + DB + alert.

NO real token, NO real close, NO network, NO DB. We monkeypatch the reused
brain_automerge primitives (list_autofix_prs / ci_status_for_sha /
_alert_operator) and the janitor's OWN write primitives (close_pr /
quarantine_recipe) plus the writer's get_logged_proposal_for_branch.

We assert the SAFETY INVARIANTS:
  · DISABLED (env flag off) ⇒ DRY: would_close populated, ZERO closes /
    quarantines / alerts (sentinels that explode if any write is touched).
  · RED + past-grace ⇒ would close (dry) AND, when enabled+act, closes +
    quarantines (file, klass) + alerts.
  · GREEN ⇒ untouched.
  · PENDING / no-CI-signal ⇒ untouched.
  · RED within grace ⇒ untouched.
  · A non-brain/autofix-* PR is never listed (the listing filters it) — and a
    sneaked-in one is re-skipped by the defense-in-depth prefix check.
  · NEVER merges (this module has no merge call at all) and never closes a PR
    we couldn't establish an age for.

The module imports cleanly with NO flask (blueprint built lazily).

Run:  python3 -m pytest tests/test_brain_pr_janitor.py -v
"""
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_pr_janitor as jan  # noqa: E402 (imports w/o flask)
import routes.brain_automerge as am    # noqa: E402
import routes.brain_draft_pr_writer as w  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test with the janitor OFF and a known grace window."""
    monkeypatch.delenv("BRAIN_PR_JANITOR_ENABLED", raising=False)
    monkeypatch.setenv("BRAIN_PR_JANITOR_GRACE_MIN", "60")
    monkeypatch.delenv("BRAIN_PR_JANITOR_MAX_PER_RUN", raising=False)
    yield


@pytest.fixture(autouse=True)
def _flask_shim():
    """Shim flask/internal_auth when absent; drop the classifier so it
    re-imports under the shim (mirrors the automerge test)."""
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


# ── Fixtures: PRs of each CI/age shape ───────────────────────────────
_OLD_ISO = "2020-01-01T00:00:00Z"   # WAY past any grace window


def _pr(number, *, ref=None, sha=None, created_at=_OLD_ISO, draft=True,
        created_epoch=None):
    ref = ref or f"brain/autofix-interval_literal-{number}-abc12345"
    sha = sha or f"sha{number}"
    d = {"number": number, "node_id": f"NODE_{number}", "head_ref": ref,
         "head_sha": sha, "draft": draft, "title": "[brain-autofix] x",
         "body": "b", "created_at": created_at}
    if created_epoch is not None:
        d["created_epoch"] = created_epoch
    return d


class _Boom:
    """A sentinel that EXPLODES if invoked — proves the DRY path makes ZERO
    closes / quarantines / alerts."""
    def __init__(self, label):
        self.label = label

    def __call__(self, *a, **k):
        raise AssertionError(f"write primitive '{self.label}' was called on a "
                             f"path that must make ZERO writes")


def _arm_writes_boom(monkeypatch):
    monkeypatch.setattr(jan, "close_pr", _Boom("close_pr"))
    monkeypatch.setattr(jan, "quarantine_recipe", _Boom("quarantine_recipe"))
    monkeypatch.setattr(jan, "_alert_close", _Boom("_alert_close"))


def _wire(monkeypatch, *, prs, ci_by_sha=None, ci_default=None,
          logged=None):
    """Mock the listing + CI read + the logged-proposal lookup. Returns a
    capture dict for the WRITE primitives (close/quarantine/alert)."""
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": list(prs)})

    ci_by_sha = ci_by_sha or {}
    ci_default = ci_default or {"green": True, "reason": "all_green"}

    def fake_ci(sha):
        return ci_by_sha.get(sha, ci_default)

    monkeypatch.setattr(am, "ci_status_for_sha", fake_ci)

    monkeypatch.setattr(
        w, "get_logged_proposal_for_branch",
        lambda branch: (logged or {"file_path": "routes/example.py",
                                    "klass": "interval_literal"}))

    calls = {"close": [], "quarantine": [], "alert": []}

    def fake_close(num, *, reason=""):
        calls["close"].append({"num": num, "reason": reason})
        return {"ok": True, "number": num}

    def fake_quar(*, file_path, klass, note=""):
        calls["quarantine"].append({"file_path": file_path, "klass": klass,
                                    "note": note})
        return True

    def fake_alert(candidate):
        calls["alert"].append(candidate)
        return True

    monkeypatch.setattr(jan, "close_pr", fake_close)
    monkeypatch.setattr(jan, "quarantine_recipe", fake_quar)
    monkeypatch.setattr(jan, "_alert_close", fake_alert)
    return calls


_RED = {"green": False, "reason": "check_run_failure"}
_PENDING = {"green": False, "reason": "check_run_pending"}
_NO_SIG = {"green": False, "reason": "no_ci_signals", "checks": 0}
_GREEN = {"green": True, "reason": "all_green"}


# ══════════════════════════════════════════════════════════════════════
# 1. _ci_is_red classification (the heart of the safety logic).
# ══════════════════════════════════════════════════════════════════════
def test_ci_is_red_only_for_terminal_failure():
    assert jan._ci_is_red(_RED) is True
    assert jan._ci_is_red({"green": False, "reason": "combined_status_failure"}) is True
    assert jan._ci_is_red({"green": False, "reason": "check_run_error"}) is True
    assert jan._ci_is_red({"green": False, "reason": "check_run_timed_out"}) is True
    # NOT red:
    assert jan._ci_is_red(_GREEN) is False
    assert jan._ci_is_red(_PENDING) is False
    assert jan._ci_is_red(_NO_SIG) is False
    assert jan._ci_is_red({"green": False, "reason": "status_http_500"}) is False
    assert jan._ci_is_red({"green": False, "reason": ""}) is False
    assert jan._ci_is_red({"green": False, "reason": "no_token"}) is False
    assert jan._ci_is_red(None) is False
    assert jan._ci_is_red({}) is False


# ══════════════════════════════════════════════════════════════════════
# 2. DISABLED ⇒ DRY: would_close populated, ZERO writes.
# ══════════════════════════════════════════════════════════════════════
def test_disabled_is_dry_and_zero_writes(monkeypatch):
    monkeypatch.delenv("BRAIN_PR_JANITOR_ENABLED", raising=False)
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": [_pr(101, sha="red")]})
    monkeypatch.setattr(am, "ci_status_for_sha", lambda sha: _RED)
    monkeypatch.setattr(w, "get_logged_proposal_for_branch",
                        lambda b: {"file_path": "routes/example.py",
                                   "klass": "interval_literal"})
    _arm_writes_boom(monkeypatch)  # ANY write would raise

    # Even with dry_run=False, the env flag being off forces DRY.
    res = jan.janitor_sweep(dry_run=False)
    assert res["dry_run"] is True, res
    assert res["enabled"] is False
    assert len(res["would_close"]) == 1, res
    assert res["would_close"][0]["pr"] == 101
    assert res["closed"] == []
    print("DISABLED dry-run, zero writes ->", res["would_close"])


# ══════════════════════════════════════════════════════════════════════
# 3. RED + past-grace + ENABLED + act ⇒ close + quarantine + alert.
# ══════════════════════════════════════════════════════════════════════
def test_red_past_grace_enabled_closes_and_quarantines(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    calls = _wire(monkeypatch, prs=[_pr(202, sha="red")],
                  ci_default=_RED,
                  logged={"file_path": "routes/db_utils.py",
                          "klass": "sqlite_datetime_on_pg"})
    res = jan.janitor_sweep(dry_run=False)
    assert res["dry_run"] is False
    assert len(res["closed"]) == 1, res
    closed = res["closed"][0]
    assert closed["pr"] == 202
    assert closed["file_path"] == "routes/db_utils.py"
    assert closed["klass"] == "sqlite_datetime_on_pg"
    assert closed["quarantined"] is True
    # The write primitives were actually invoked with the right args.
    assert calls["close"] and calls["close"][0]["num"] == 202
    assert calls["quarantine"] and calls["quarantine"][0]["file_path"] == "routes/db_utils.py"
    assert calls["quarantine"][0]["klass"] == "sqlite_datetime_on_pg"
    assert calls["alert"], "operator alert must fire on a real close"
    print("RED past-grace closed+quarantined ->", closed)


# ══════════════════════════════════════════════════════════════════════
# 4. GREEN ⇒ untouched.
# ══════════════════════════════════════════════════════════════════════
def test_green_untouched(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    calls = _wire(monkeypatch, prs=[_pr(303, sha="green")], ci_default=_GREEN)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [], res
    assert res["would_close"] == []
    assert any(s["pr"] == 303 and s["reason"] == "ci_green"
               for s in res["skipped"]), res
    assert calls["close"] == [] and calls["quarantine"] == []
    print("GREEN untouched ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 5. PENDING ⇒ untouched.
# ══════════════════════════════════════════════════════════════════════
def test_pending_untouched(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    calls = _wire(monkeypatch, prs=[_pr(404, sha="pending")], ci_default=_PENDING)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [], res
    assert res["would_close"] == []
    assert any(s["pr"] == 404 and s["reason"].startswith("ci_not_red")
               for s in res["skipped"]), res
    assert calls["close"] == []
    print("PENDING untouched ->", res["skipped"])


def test_no_ci_signal_untouched(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    calls = _wire(monkeypatch, prs=[_pr(414, sha="nosig")], ci_default=_NO_SIG)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [] and res["would_close"] == []
    assert calls["close"] == []
    print("NO-CI-signal untouched ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 6. RED within grace ⇒ untouched.
# ══════════════════════════════════════════════════════════════════════
def test_red_within_grace_untouched(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    # Created 5 min ago, grace is 60 min ⇒ too young.
    young = _pr(505, sha="red", created_epoch=time.time() - 5 * 60)
    calls = _wire(monkeypatch, prs=[young], ci_default=_RED)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [], res
    assert res["would_close"] == []
    assert any(s["pr"] == 505 and s["reason"] == "within_grace"
               for s in res["skipped"]), res
    assert calls["close"] == []
    print("RED within-grace untouched ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 7. DRY (enabled but dry_run=True) ⇒ would_close, ZERO writes.
# ══════════════════════════════════════════════════════════════════════
def test_enabled_but_dry_run_makes_zero_writes(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": True, "prs": [_pr(606, sha="red")]})
    monkeypatch.setattr(am, "ci_status_for_sha", lambda sha: _RED)
    monkeypatch.setattr(w, "get_logged_proposal_for_branch",
                        lambda b: {"file_path": "routes/example.py",
                                   "klass": "interval_literal"})
    _arm_writes_boom(monkeypatch)
    res = jan.janitor_sweep(dry_run=True)
    assert res["dry_run"] is True, res
    assert len(res["would_close"]) == 1 and res["closed"] == []
    print("enabled+dry_run -> would_close only", res["would_close"])


# ══════════════════════════════════════════════════════════════════════
# 8. A non-autofix branch is re-skipped (defense in depth).
# ══════════════════════════════════════════════════════════════════════
def test_non_autofix_branch_skipped(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    human = _pr(707, ref="feature/human-pr", sha="red")
    calls = _wire(monkeypatch, prs=[human], ci_default=_RED)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [] and res["would_close"] == []
    assert any(s["pr"] == 707 and s["reason"] == "not_autofix_branch"
               for s in res["skipped"]), res
    assert calls["close"] == []
    print("non-autofix skipped ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 9. No-age PR ⇒ fail-safe skip (never close without an age).
# ══════════════════════════════════════════════════════════════════════
def test_no_age_fail_safe_skip(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    ageless = _pr(808, sha="red", created_at=None)
    ageless.pop("created_at", None)
    calls = _wire(monkeypatch, prs=[ageless], ci_default=_RED)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [], res
    assert any(s["pr"] == 808 and "no_age" in s["reason"]
               for s in res["skipped"]), res
    assert calls["close"] == []
    print("no-age fail-safe ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 10. Rate cap bounds the number of closes per run.
# ══════════════════════════════════════════════════════════════════════
def test_rate_cap_bounds_closes(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setenv("BRAIN_PR_JANITOR_MAX_PER_RUN", "2")
    prs = [_pr(900 + i, sha=f"red{i}") for i in range(5)]
    ci_by = {f"red{i}": _RED for i in range(5)}
    calls = _wire(monkeypatch, prs=prs, ci_by_sha=ci_by, ci_default=_RED)
    res = jan.janitor_sweep(dry_run=False)
    assert len(res["closed"]) == 2, res
    assert len(calls["close"]) == 2
    assert any("rate_cap_reached" in s.get("reason", "")
               for s in res["skipped"]), res
    print("rate cap honored ->", len(res["closed"]), "closed of 5")


# ══════════════════════════════════════════════════════════════════════
# 11. A failed close does NOT quarantine (don't quarantine if we couldn't act).
# ══════════════════════════════════════════════════════════════════════
def test_failed_close_does_not_quarantine(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    _wire(monkeypatch, prs=[_pr(1001, sha="red")], ci_default=_RED)

    q_called = {"n": 0}

    def boom_quar(**k):
        q_called["n"] += 1
        return True

    monkeypatch.setattr(jan, "close_pr",
                        lambda num, reason="": {"ok": False, "error": "403"})
    monkeypatch.setattr(jan, "quarantine_recipe", boom_quar)
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [], res
    assert q_called["n"] == 0, "must NOT quarantine when the close failed"
    assert any("close_failed" in s.get("reason", "")
               for s in res["skipped"]), res
    print("failed close -> no quarantine ->", res["skipped"])


# ══════════════════════════════════════════════════════════════════════
# 12. Listing failure is handled (never raises out).
# ══════════════════════════════════════════════════════════════════════
def test_listing_failure_handled(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setattr(am, "list_autofix_prs",
                        lambda: {"ok": False, "error": "500"})
    res = jan.janitor_sweep(dry_run=False)
    assert res["closed"] == [] and res["would_close"] == []
    assert "list_failed" in str(res.get("error")), res
    print("listing failure handled ->", res.get("error"))
