"""POST /api/v1/qa/run must be gated, and ?sync=1 must not run before the gate
(2026-09-12).

WHAT WAS WRONG
--------------
routes/site_qa.py's trigger_run started the full QA suite — ~28 outbound URL
probes — for ANY caller:

    @site_qa_bp.route("/run", methods=["GET", "POST"])
    def trigger_run():
        ...
        if _req.args.get("sync") == "1":
            summary = run_full_qa_suite()      # ON the request thread

Three things compounded:

  1. NO GATE AT ALL. Railway HTTP metrics for the 7 days to 2026-09-12: 608
     requests, 608 of them 2xx, ZERO 4xx — nothing was ever refused, because
     there was nothing to refuse it.
  2. ?sync=1 RAN BEFORE THE IN-PROGRESS GUARD. The _QA_RUN_STATE["running"]
     check sits BELOW the sync branch, so it never saw a sync request. N
     concurrent ?sync=1 calls meant N concurrent 106-second suites, each
     holding a gthread worker. trigger_run's own docstring records what one of
     those did: "HELD the single gthread worker past gunicorn's timeout →
     SIGKILL → site-wide flap. The monitor was the outage." That was
     reproducible on demand by an anonymous caller.
  3. GET WAS ACCEPTED, so no POST was even needed — a crawler, a prefetch or a
     link preview hitting the URL was enough. The dashboard shipped an
     <a href="/api/v1/qa/run"> anchor pointing at it.

THE FIX
-------
require_internal_or_admin as the FIRST statement, before the sync branch, and
GET dropped (the same 7-day window shows zero GETs, so no caller used it).

WHY THE ORDER IS ASSERTED SEPARATELY
------------------------------------
"Gated" and "gated before the expensive branch" are different properties, and
only the second one closes hole 2. A gate placed below the sync check would
still answer 401 to the plain POST test — and still run the suite for
?sync=1. test_sync_without_a_credential_does_not_run_the_suite is the one that
fails in that arrangement, so it asserts the RUNNER WAS NOT CALLED rather than
just reading the status code.

MUTATION-VERIFIED (verify-a-guard) — transcript in the PR body.
"""
import ast
import os
import sys

import pytest

flask = pytest.importorskip("flask")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SOURCE = os.path.join(REPO, "routes", "site_qa.py")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "site-qa.yml")

_SECRET_VARS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
                "INTERNAL_WORKER_SECRET")
_REAL_KEY = "qa-run-real-key-5Hn3pQ8w"
_WRONG_KEY = "qa-run-attacker-key-nope-0"

PATH = "/api/v1/qa/run"


@pytest.fixture
def app_and_calls(monkeypatch):
    """Real blueprint; the suite runner and the thread starter replaced by
    recorders so nothing probes 28 URLs and no daemon thread escapes."""
    mod = pytest.importorskip("routes.site_qa")
    calls = {"suite": [], "threads": []}

    monkeypatch.setattr(mod, "run_full_qa_suite",
                        lambda *a, **k: (calls["suite"].append((a, k)) or
                                         {"failed": 0, "results": []}))

    real_thread = __import__("threading").Thread

    class _RecordingThread(real_thread):
        def start(self):                      # never actually run the target
            calls["threads"].append(self._target)

    monkeypatch.setattr("threading.Thread", _RecordingThread)
    monkeypatch.setattr(mod, "_ensure_tables", lambda *a, **k: None,
                        raising=False)

    app = flask.Flask(__name__)
    app.register_blueprint(mod.site_qa_bp)
    return app, calls


def _clear(monkeypatch):
    for v in _SECRET_VARS:
        monkeypatch.delenv(v, raising=False)


def _assert_did_not_run(resp, calls, why):
    assert resp.status_code == 401, (
        f"POST {PATH} returned {resp.status_code}, not 401 — {why}"
    )
    assert calls["suite"] == [], (
        f"run_full_qa_suite ran ({len(calls['suite'])}x) on a request that was "
        "supposed to be refused — this is the 28-probe, worker-holding path"
    )
    assert calls["threads"] == [], (
        "a background QA thread was started for a refused request"
    )


# ── the gate ─────────────────────────────────────────────────────────────────

def test_no_credential_is_refused_and_nothing_runs(monkeypatch, app_and_calls):
    """The regression itself: anyone could start the suite."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = app_and_calls
    _assert_did_not_run(app.test_client().post(PATH), calls,
                        "an anonymous caller started a QA run")


def test_wrong_credential_is_refused_and_nothing_runs(monkeypatch, app_and_calls):
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = app_and_calls
    resp = app.test_client().post(PATH, headers={"X-Internal-Key": _WRONG_KEY})
    _assert_did_not_run(resp, calls, "a wrong key was accepted")


def test_no_secret_configured_is_refused(monkeypatch, app_and_calls):
    """Fail CLOSED: an unconfigured process must refuse, not wave everyone
    through (the #4411 / #4434 shape)."""
    _clear(monkeypatch)
    app, calls = app_and_calls
    _assert_did_not_run(app.test_client().post(PATH), calls,
                        "the gate disabled itself when no secret was set")


# ── hole 2: the expensive branch must be BELOW the gate ──────────────────────

def test_sync_without_a_credential_does_not_run_the_suite(monkeypatch, app_and_calls):
    """?sync=1 runs the suite ON the request thread and bypasses the
    in-progress guard. If the gate is ever moved below that branch, this is the
    test that fails: the status might still be 401 while the suite has already
    run."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = app_and_calls
    resp = app.test_client().post(PATH + "?sync=1")
    _assert_did_not_run(resp, calls,
                        "the synchronous 28-probe path ran for an anonymous "
                        "caller — this is the documented worker-SIGKILL path")


# ── the caller must keep working ─────────────────────────────────────────────

@pytest.mark.parametrize("slot,header", [
    ("internal", "X-Internal-Key"),
    ("admin", "X-Admin-Key"),
])
def test_a_valid_credential_starts_the_run(monkeypatch, app_and_calls, slot, header):
    """.github/workflows/site-qa.yml sends X-Internal-Key; an operator may use
    X-Admin-Key. Both must authorize or the 15-minute QA cron dies silently."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY" if slot == "internal"
                       else "DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = app_and_calls
    resp = app.test_client().post(PATH, headers={header: _REAL_KEY})
    assert resp.status_code == 202, (
        f"a valid {header} was rejected ({resp.status_code}) — the QA cron "
        f"would 401. body={resp.get_json()!r}"
    )
    assert len(calls["threads"]) == 1, (
        f"authorized request did not start exactly one run: {calls}"
    )


def test_get_is_no_longer_accepted(monkeypatch, app_and_calls):
    """GET let a crawler or a link prefetch fire a run. Railway metrics for the
    7 days to 2026-09-12 show zero GETs, so nothing depended on it."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = app_and_calls
    resp = app.test_client().get(PATH, headers={"X-Internal-Key": _REAL_KEY})
    assert resp.status_code == 405, (
        f"GET {PATH} returned {resp.status_code}, not 405 — a GET that starts "
        "28 outbound probes is reachable by any crawler"
    )
    assert calls["suite"] == [] and calls["threads"] == []


# ── structure ────────────────────────────────────────────────────────────────

def _trigger_run_node():
    with open(SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "trigger_run":
            return n
    raise AssertionError("trigger_run not found in routes/site_qa.py")


def test_the_gate_precedes_the_sync_branch_in_source():
    """Names the invariant the behaviour test enforces, so a reviewer moving
    the gate sees why it is where it is."""
    fn = _trigger_run_node()
    gate_line = sync_line = None
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "require_internal_or_admin"):
            gate_line = n.lineno if gate_line is None else min(gate_line, n.lineno)
        for c in ast.walk(n) if isinstance(n, ast.If) else ():
            if isinstance(c, ast.Constant) and c.value == "sync":
                sync_line = n.lineno if sync_line is None else min(sync_line, n.lineno)
    assert gate_line is not None, (
        "trigger_run no longer calls require_internal_or_admin — it is ungated"
    )
    assert sync_line is not None, (
        "the ?sync= branch was not found; if it was removed, delete this test "
        "with it rather than letting it pass vacuously"
    )
    assert gate_line < sync_line, (
        f"the gate (line {gate_line}) is BELOW the ?sync= branch (line "
        f"{sync_line}) — the synchronous 28-probe run happens before auth"
    )


def test_route_no_longer_registers_get():
    fn = _trigger_run_node()
    methods = []
    for d in fn.decorator_list:
        if not isinstance(d, ast.Call):
            continue
        for kw in d.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                methods = [e.value for e in kw.value.elts
                           if isinstance(e, ast.Constant)]
    assert methods, "no methods= found on trigger_run's route decorator"
    assert "GET" not in methods, (
        f"GET is registered again on {PATH} ({methods}) — a crawler can start "
        "a 28-probe run with no POST"
    )
    assert "POST" in methods, f"POST was dropped from {PATH} ({methods})"


# ── the caller: gating ahead of it is how a cron dies ────────────────────────

def test_the_qa_workflow_sends_a_credential():
    """A gate landed without updating this workflow would 401 every 15 minutes
    and the site QA signal would go dark."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()
    assert "X-Internal-Key" in text, (
        ".github/workflows/site-qa.yml no longer sends X-Internal-Key — its "
        f"POST to {PATH} will 401 on every scheduled run"
    )
    assert "secrets.DCHUB_INTERNAL_KEY" in text, (
        "the workflow's credential no longer comes from the repo secret"
    )


def test_the_dashboard_does_not_link_a_run_trigger():
    """The dashboard is ungated HTML. An anchor to a run trigger is both a
    GET-starts-work vector and, now, a guaranteed 401."""
    with open(SOURCE, encoding="utf-8") as fh:
        text = fh.read()
    assert 'href="/api/v1/qa/run"' not in text, (
        "the QA dashboard links to the run trigger again"
    )
