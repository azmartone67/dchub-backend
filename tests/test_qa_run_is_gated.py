"""Every /api/v1/qa/* surface must be gated, and ?sync=1 must not run before
the gate (2026-09-12).

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

THE READ SURFACES (added in the follow-up)
------------------------------------------
/report, /regressions, /dashboard and /health were open too, and they are not
inert: /report publishes, per test, the url, http_code, error_detail and
proposed_fix of every currently-failing surface; /regressions the same for open
alerts; /health the count of open alerts. That is a live "what is broken here
right now, and how" map.

★ Traffic did NOT enumerate the callers. Railway metrics for the 7 days to
2026-09-12 show 0 requests to /regressions — not because nothing calls it, but
because its caller (.github/workflows/site-qa.yml's issue step) runs ONLY when
a P0 has already fired, and none had. Gating on the metric alone would have
401'd that fetch at exactly the moment the signal mattered. Its credential is
added in the same change, and test_the_p0_issue_step_sends_a_credential pins it.

/api/v1/qa/health is QA-suite health, not service liveness — the service health
check is /api/health and is untouched.

MUTATION-VERIFIED (verify-a-guard) — transcript in the PR body.
"""
import ast
import os
import re
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
    # ★ Assert the CODE form, not the bare header name. This file also
    # MENTIONS X-Internal-Key in the P0 issue body's explanatory prose, so a
    # substring check over the file passes even with the real header deleted —
    # measured: mutation M8 removed the live header and this suite stayed green
    # until the assertion was tightened to the binding below.
    assert re.search(r"""["']X-Internal-Key["']\s*:\s*INTERNAL_KEY""", text), (
        ".github/workflows/site-qa.yml no longer BINDS X-Internal-Key to the "
        f"secret in its request headers — its POST to {PATH} will 401 on every "
        "scheduled run"
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


# ── the read surfaces ────────────────────────────────────────────────────────

_READ_PATHS = [
    ("report",      "/api/v1/qa/report"),
    ("regressions", "/api/v1/qa/regressions"),
    ("dashboard",   "/api/v1/qa/dashboard"),
    ("health",      "/api/v1/qa/health"),
]
_READ_IDS = [i for i, _ in _READ_PATHS]


@pytest.fixture
def read_app(monkeypatch):
    """Real blueprint with the DB stubbed. _conn is a recorder: a refused
    request must not reach it, which also proves _ensure_tables' DDL never
    ran for an unauthenticated caller."""
    mod = pytest.importorskip("routes.site_qa")
    calls = []

    class _Boom:
        def __enter__(self, *a):
            raise AssertionError("the handler queried the DB on a refused request")

        def __exit__(self, *a):
            return False

    def _fake_conn(*a, **k):
        calls.append((a, k))
        return _Boom()

    monkeypatch.setattr(mod, "_conn", _fake_conn)
    monkeypatch.setattr(mod, "_ensure_tables", lambda *a, **k: None, raising=False)
    app = flask.Flask(__name__)
    app.register_blueprint(mod.site_qa_bp)
    return app, calls


@pytest.mark.parametrize("name,path", _READ_PATHS, ids=_READ_IDS)
def test_read_surface_refuses_without_a_credential(monkeypatch, read_app, name, path):
    """Each read publishes internal QA state. None may answer an anonymous
    caller, and none may touch the DB before deciding."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = read_app
    resp = app.test_client().get(path)
    assert resp.status_code == 401, (
        f"GET {path} returned {resp.status_code}, not 401 — it publishes which "
        "of our surfaces are failing, and how"
    )
    assert calls == [], (
        f"{path} opened a DB connection for a refused request ({calls})"
    )


@pytest.mark.parametrize("name,path", _READ_PATHS, ids=_READ_IDS)
def test_read_surface_refuses_a_wrong_credential(monkeypatch, read_app, name, path):
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = read_app
    resp = app.test_client().get(path, headers={"X-Internal-Key": _WRONG_KEY})
    assert resp.status_code == 401, f"{path} accepted a wrong key"
    assert calls == []


@pytest.mark.parametrize("name,path", _READ_PATHS, ids=_READ_IDS)
def test_read_surface_refuses_when_no_secret_is_configured(monkeypatch, read_app,
                                                           name, path):
    """Fail CLOSED on a misconfigured process (the #4411 / #4434 shape)."""
    _clear(monkeypatch)
    app, calls = read_app
    resp = app.test_client().get(path)
    assert resp.status_code == 401, (
        f"{path} returned {resp.status_code} with NO secret in the env — the "
        "gate disabled itself instead of denying"
    )
    assert calls == []


@pytest.mark.parametrize("name,path", _READ_PATHS, ids=_READ_IDS)
def test_read_surface_lets_a_valid_credential_through(monkeypatch, read_app,
                                                      name, path):
    """Past the gate the handler reaches _conn — which this fixture makes
    explode. Reaching the explosion is the proof the credential was accepted;
    a 401 would mean the legitimate caller was locked out."""
    _clear(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = read_app
    client = app.test_client()
    try:
        resp = client.get(path, headers={"X-Internal-Key": _REAL_KEY})
    except AssertionError:
        return                      # reached the DB == past the gate
    assert resp.status_code != 401, (
        f"a valid X-Internal-Key was rejected on {path} — the QA cron and the "
        "P0 issue step read these"
    )
    assert calls, f"{path} answered {resp.status_code} without reaching _conn"


@pytest.mark.parametrize("name,path", _READ_PATHS, ids=_READ_IDS)
def test_read_surface_gate_is_the_first_statement(name, path):
    """Ahead of _ensure_tables(), which runs DDL. A gate below it would let an
    anonymous request take table locks before being refused."""
    fname = {"report": "latest_report", "regressions": "regressions",
             "dashboard": "dashboard", "health": "health"}[name]
    with open(SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == fname), None)
    assert fn is not None, f"{fname} not found in routes/site_qa.py"
    body = [b for b in fn.body
            if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
    while body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        body = body[1:]
    assert body, f"{fname} has no executable body"
    calls = {c.func.id for c in ast.walk(body[0])
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "require_internal_or_admin" in calls, (
        f"{fname}'s first statement is {type(body[0]).__name__} calling "
        f"{sorted(calls)} — the gate is not first, so work happens before auth"
    )


def test_the_p0_issue_step_sends_a_credential():
    """This step runs only when a P0 has ALREADY fired, so it is the one fetch
    that must not 401. Its path showed zero traffic in the sample that motivated
    the gate — because no P0 fired, not because nothing calls it."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()
    issue_step = text.split("Open issue if P0 regressed", 1)
    assert len(issue_step) == 2, "the P0 issue step was renamed or removed"
    step = issue_step[1]
    # ★ The code form, for the reason spelled out in
    # test_the_qa_workflow_sends_a_credential: this very step's issue body
    # mentions the header name in prose, which satisfied a substring check
    # while the real header was gone (mutation M8).
    assert re.search(r"""headers\s*:\s*\{\s*["']X-Internal-Key["']\s*:\s*internalKey""",
                     step), (
        "the P0 issue step no longer passes X-Internal-Key in its fetch headers "
        "— it will 401 exactly when a regression has fired, and open an empty "
        "issue"
    )
    assert "secrets.DCHUB_INTERNAL_KEY" in step, (
        "the P0 issue step's credential no longer comes from the repo secret"
    )


def test_no_credential_is_embedded_in_the_public_issue_body():
    """This repo and its issues are PUBLIC. The dashboard link must stay a bare
    URL — never ?admin_key=<value> interpolated into issue text."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()
    body_region = text.split("Dashboard: https://dchub.cloud/api/v1/qa/dashboard", 1)
    assert len(body_region) == 2, "the dashboard link in the issue body moved"
    assert "admin_key=${" not in text and "admin_key=' +" not in text, (
        "a credential is being interpolated into the public issue body"
    )
