"""The three brain admin POSTs must FAIL CLOSED, and a forged X-DC-Internal-Cron
header must not bypass auth (2026-09-11).

WHAT THIS PROVES
----------------
Three brain cron endpoints mutate state or send email:

    POST /api/v1/brain/metric-observatory/snapshot   (writes metric rows)
    POST /api/v1/brain/outcome-verifier/run          (regression spotter + auto-revert)
    POST /api/v1/brain/weekly-movement-digest/run    (?send=1 emails the operator)

Each gated on the same self-disabling + forgeable shape:

    _ADMIN_KEY = (os.environ.get('DCHUB_ADMIN_KEY')
                  or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()   # import time
    sent = (request.headers.get('X-Admin-Key')
            or request.headers.get('X-Internal-Key') or '').strip()
    if _ADMIN_KEY and sent != _ADMIN_KEY and (request.headers.get('X-DC-Internal-Cron') or '') != '1':
        return jsonify(error='unauthorized'), 401

Two independent holes:
  1. `_ADMIN_KEY and ...` — _ADMIN_KEY is an IMPORT-TIME snapshot of
     DCHUB_ADMIN_KEY. On a process whose env lacks it the snapshot is "", the
     whole condition is False, and the body runs for anyone (fail OPEN — same
     class as #4408 cf_purge, latent because prod has the key set).
  2. `X-DC-Internal-Cron != '1'` — a PLAINTEXT header any caller can forge.
     Sending `X-DC-Internal-Cron: 1` makes the third conjunct False, so the
     whole AND is False and auth is bypassed ENTIRELY, regardless of the key.
     This one is LIVE: neither edge worker strips the header (dchub-frontend
     _worker.js never references it; dchub-backend worker.js forwards it), the
     origin (api_usage_tracker.py) reads it, and the Railway origin is directly
     reachable. An anonymous POST with that header reaches the gate.

The fix routes all three through internal_auth.require_internal_or_admin, which
re-reads env PER REQUEST (no snapshot) and requires a real credential in
X-Internal-Key / X-Admin-Key / ?admin_key (no plaintext-header path exists).

WHAT THE TESTS COVER  (unset key / wrong key / right key, per the task, plus the
forged-header case that the fail-closed helper alone would not exercise):
  * no secret configured, no credential      -> 401, work fn NEVER called (hole 1)
  * a key IS set, wrong credential            -> 401, work fn never called
  * forged X-DC-Internal-Cron:1, key set      -> 401, work fn never called (hole 2)
  * forged X-DC-Internal-Cron:1, no key set   -> 401, work fn never called (1 & 2)
  * a key IS set, right X-Admin-Key           -> 200, work fn called (operator)
  * DCHUB_INTERNAL_KEY set, right X-Internal-Key -> 200, work fn called (cron)
  * the real cron-heartbeat header combo      -> 200 (the crons do not break)

Asserting the work function was NOT called (not merely that the status is 401)
separates "declined" from "declined but ran anyway": the mutation/email lives
behind that call.

MUTATION-VERIFIED (verify-a-guard) — see the PR body for the recorded transcript.
Reintroducing the forgeable conjunct in metric_observatory (with a key set)
flips test_forged_internal_cron_header_is_rejected[metric-observatory] from
green to red (200, snapshot_all called); restoring returns it to green.
"""
import ast
import os
import sys

import pytest

flask = pytest.importorskip("flask")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_SECRET_VARS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
                "INTERNAL_WORKER_SECRET")
# Random, and deliberately NOT either internal_auth legacy hardcoded key
# ("dchub-internal-2024" / "dchub-internal-sync-2026"), so an attacker string
# cannot pass by the legacy path.
_REAL_KEY = "brain-cron-real-key-9Kp4wZr2"
_WRONG_KEY = "brain-cron-attacker-key-nope-0"


class _Spec:
    def __init__(self, id, module, bp_attr, path, endpoint_fn, work_fn,
                 extra_stub_fns=()):
        self.id = id
        self.module = module
        self.bp_attr = bp_attr
        self.path = path
        self.endpoint_fn = endpoint_fn      # for the AST structure tests
        self.work_fn = work_fn              # stubbed recorder = "the body ran"
        self.extra_stub_fns = extra_stub_fns  # neutralised so the 200 path is inert


_SPECS = [
    _Spec("metric-observatory", "routes.metric_observatory",
          "metric_observatory_bp",
          "/api/v1/brain/metric-observatory/snapshot",
          "snapshot_endpoint", "snapshot_all"),
    _Spec("outcome-verifier", "routes.outcome_verifier",
          "outcome_verifier_bp",
          "/api/v1/brain/outcome-verifier/run",
          "run_endpoint", "verify_pending"),
    # run_digest also calls _render_html(payload) and, only when ?send=1, _send().
    # We stub compose() as the "body ran" marker and neutralise _render_html /
    # _send so the authorized path can never render against a real payload or
    # dispatch an email even if a future edit flips the default.
    _Spec("weekly-movement-digest", "routes.weekly_movement_digest",
          "weekly_movement_digest_bp",
          "/api/v1/brain/weekly-movement-digest/run",
          "run_digest", "compose",
          extra_stub_fns=("_render_html", "_send")),
]
_IDS = [s.id for s in _SPECS]


def _clear_secrets(monkeypatch):
    for v in _SECRET_VARS:
        monkeypatch.delenv(v, raising=False)


def _app(spec):
    mod = pytest.importorskip(spec.module)
    app = flask.Flask(__name__)
    app.register_blueprint(getattr(mod, spec.bp_attr))
    return app, mod


def _install_recorder(monkeypatch, mod, spec):
    """Replace the endpoint's work function with a recorder so we can prove
    whether the body executed, and never touch the DB / email / auto-revert."""
    calls = []

    def _fake(*a, **k):
        calls.append((a, k))
        return {"ok": True, "date": "2026-09-11"}  # 'date' keeps run_digest happy

    monkeypatch.setattr(mod, spec.work_fn, _fake)
    for name in spec.extra_stub_fns:
        # _render_html -> "" ; _send -> False (never dispatch)
        monkeypatch.setattr(mod, name, (lambda *a, **k: "")
                            if name == "_render_html" else (lambda *a, **k: False))
    return calls


# ── behaviour ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_no_secret_configured_rejects_and_body_does_not_run(monkeypatch, spec):
    """THE FAIL-OPEN REGRESSION (hole 1). Every secret unset — a misconfigured
    process — and no credential: must 401 and the body must not run."""
    _clear_secrets(monkeypatch)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path)
    assert r.status_code == 401, (
        f"POST {spec.path} returned {r.status_code} with NO secret in the env — "
        "the gate disabled itself instead of denying"
    )
    assert calls == [], f"the body ran ({calls}) despite an unauthorized request"


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_wrong_credential_rejected_and_body_does_not_run(monkeypatch, spec):
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={"X-Admin-Key": _WRONG_KEY})
    assert r.status_code == 401, f"wrong key accepted ({r.status_code})"
    assert calls == []


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_forged_internal_cron_header_is_rejected(monkeypatch, spec):
    """THE FORGEABLE-HEADER HOLE (hole 2). A key IS configured; the caller sends
    the forgeable X-DC-Internal-Cron:1 and NO credential. The old gate treated
    that header as a full auth bypass; it must now be refused."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={"X-DC-Internal-Cron": "1"})
    assert r.status_code == 401, (
        f"POST {spec.path} with a forged X-DC-Internal-Cron:1 header returned "
        f"{r.status_code} — a plaintext header any caller can send bypassed auth"
    )
    assert calls == [], (
        f"the body ran ({calls}) for a forged-header request — the endpoint "
        "mutates state / can send email unauthenticated"
    )


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_forged_internal_cron_header_rejected_even_with_no_key(monkeypatch, spec):
    """Both holes at once: env has no secret AND the caller forges the cron
    header. Still 401."""
    _clear_secrets(monkeypatch)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={"X-DC-Internal-Cron": "1"})
    assert r.status_code == 401
    assert calls == []


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_right_admin_key_is_accepted_and_body_runs(monkeypatch, spec):
    """The documented operator curl (X-Admin-Key == DCHUB_ADMIN_KEY) still
    works — the fix must not lock out the legitimate caller."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={"X-Admin-Key": _REAL_KEY})
    assert r.status_code == 200, (
        f"the right admin key was rejected ({r.status_code}) for {spec.path}"
    )
    assert len(calls) == 1, f"authorized body did not run exactly once: {calls}"


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_right_internal_key_is_accepted_and_body_runs(monkeypatch, spec):
    """Internal automation authenticates with X-Internal-Key == DCHUB_INTERNAL_KEY
    — the exact header the cron heartbeat's _hit() sends."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={"X-Internal-Key": _REAL_KEY})
    assert r.status_code == 200, (
        f"the right internal key was rejected ({r.status_code}) for {spec.path} "
        "— this would break the cron heartbeat that fires these endpoints"
    )
    assert len(calls) == 1


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_cron_heartbeat_header_combo_is_accepted(monkeypatch, spec):
    """routes/cron_heartbeat.py _hit() sends X-Internal-Key=DCHUB_INTERNAL_KEY
    AND X-Admin-Key=DCHUB_ADMIN_KEY (and, harmlessly now, X-DC-Internal-Cron:1).
    That exact combination must authorize, or the daily/6-hourly/hourly crons
    that drive these endpoints break."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "a-different-admin-key-4242")
    app, mod = _app(spec)
    calls = _install_recorder(monkeypatch, mod, spec)
    r = app.test_client().post(spec.path, headers={
        "X-Internal-Key": _REAL_KEY,
        "X-Admin-Key": "a-different-admin-key-4242",
        "X-DC-Internal-Cron": "1",
        "User-Agent": "DCHub-CronHeartbeat/1.0",
    })
    assert r.status_code == 200, (
        f"the cron heartbeat header combo was rejected ({r.status_code}) for "
        f"{spec.path} — the scheduled job would 401"
    )
    assert len(calls) == 1


# ── structure: AST, so a helper name in a comment/docstring cannot pass it ────

def _endpoint_node(spec):
    src = open(os.path.join(REPO, spec.module.replace(".", "/") + ".py")).read()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == spec.endpoint_fn:
            return n
    raise AssertionError(f"{spec.endpoint_fn} not found in {spec.module}")


def _returns_401(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Return) and n.value is not None:
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and c.value == 401:
                    return True
    return False


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_handler_calls_the_failclosed_helper(spec):
    fn = _endpoint_node(spec)
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "require_internal_or_admin" in called, (
        f"{spec.endpoint_fn} no longer calls require_internal_or_admin — the "
        "fail-closed gate was removed or replaced"
    )


@pytest.mark.parametrize("spec", _SPECS, ids=_IDS)
def test_handler_has_no_self_disabling_or_forgeable_gate(spec):
    """No `if <import-time key snapshot> and ...: 401` (fails open) and no gate
    that treats X-DC-Internal-Cron as a credential inside the handler."""
    fn = _endpoint_node(spec)
    snapshot_offenders, cron_offenders = [], []
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not _returns_401(n):
            continue
        if isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And):
            for v in n.test.values:
                if isinstance(v, ast.Name) and v.id.endswith("ADMIN_KEY"):
                    snapshot_offenders.append((n.lineno, v.id))
        for c in ast.walk(n.test):
            if isinstance(c, ast.Constant) and c.value == "X-DC-Internal-Cron":
                cron_offenders.append(n.lineno)
    assert not snapshot_offenders, (
        f"self-disabling `if <ADMIN_KEY snapshot> and ...: 401` is back in "
        f"{spec.endpoint_fn} at {snapshot_offenders} — fails OPEN when env unset"
    )
    assert not cron_offenders, (
        f"{spec.endpoint_fn} gates on the forgeable X-DC-Internal-Cron header "
        f"at line(s) {cron_offenders} — any caller can send it to bypass auth"
    )
