"""routes/marketing_engine.py's @_require_admin must FAIL CLOSED and read the
env PER REQUEST (2026-09-11).

WHAT THIS PROVES
----------------
One decorator, `_require_admin`, gates five routes in that module:

    POST /api/v1/marketing/auto-generate              (reaches ping_indexnow)
    POST /api/v1/marketing/publish-now                (reaches _post_to_linkedin
    POST /api/v1/marketing/repost-now                  and the X publisher)
    POST /api/v1/marketing/linkedin/send-daily-email  (sends mail via Resend)
    GET  /api/v1/marketing/linkedin/whoami            (reads the LinkedIn token)

It was written as:

    ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()   # import time
    ...
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

Two defects, both the class #4411 fixed in the brain crons:

  1. FAIL OPEN. `if ADMIN_KEY and ...` — ADMIN_KEY is an IMPORT-TIME snapshot.
     On a process whose env lacks DCHUB_ADMIN_KEY the snapshot is "", the whole
     condition is False, and all five bodies run for anyone. Latent in prod
     (the key IS set there), live the moment it is not — dchub-worker was in
     exactly that state on 2026-08-08.
  2. STALE SNAPSHOT. The value is frozen at import, so a rotated or late-bound
     DCHUB_ADMIN_KEY is never picked up: the new key is refused and the retired
     one keeps working until the process restarts.

The fix routes the wrapper through internal_auth.require_internal_or_admin,
which re-reads os.environ per request and denies unless a real credential
arrives in X-Internal-Key / X-Admin-Key / ?admin_key.

WHY THE TESTS LOOK LIKE THIS
----------------------------
Every rejection test SETS a key in os.environ and then sends a bad credential,
rather than relying on an unconfigured box. Against the old code that is enough
on its own: _app() imports the module lazily, inside the first test to run and
after _clear_secrets has emptied the env, so the import-time snapshot is "" for
the whole session however the process was launched, the gate is permanently
open, and each decline test gets the handler's own status back instead of 401
(measured: 36 failures).

The rotation test carries the second defect. It changes os.environ BETWEEN two
requests against the SAME app object, which is the only shape that can tell a
per-request read from a value cached at import or on first use. Forcing the old
code to take a POPULATED snapshot (import the module before any fixture clears
the env) makes every fail-open test above pass and leaves exactly this test red:
the rotated key is refused and the retired one still works.

Rejections assert the body did not run, not merely that the status was 401 —
"declined" and "declined but ran anyway" are different outcomes and the
IndexNow submit / social post lives behind that call. _conn is the marker: it
is the first side-effecting thing four of the five handlers do, and stubbing it
to None makes the authorized path answer 503 no_database without a database, a
network call or a token.

MUTATION-VERIFIED (verify-a-guard), transcript in the PR body:
  M1 — restore ADMIN_KEY + `if ADMIN_KEY and provided != ADMIN_KEY`, snapshot
       empty: 36 failed / 49 passed. Every decline test returns 503 (or whoami's
       500), i.e. the body ran for an unauthenticated caller, and
       test_admin_gate_fail_closed.py::test_no_new_self_disabling_gates goes red
       on the baseline removal.
  M2 — same mutation, snapshot POPULATED at import: 13 failed / 52 passed. The
       fail-open tests recover; the five rotation tests and the five
       X-Internal-Key tests stay red, isolating the stale snapshot from the
       fail-open hole.
  Restored from a byte-level snapshot, __pycache__ purged: 85 passed, exit 0.
"""
import ast
import os
import sys

import pytest

flask = pytest.importorskip("flask")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

MODULE = "routes.marketing_engine"
SOURCE = os.path.join(REPO, "routes", "marketing_engine.py")

# Every env var internal_auth.is_valid_internal_key consults.
_SECRET_VARS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
                "INTERNAL_WORKER_SECRET")
# Random, and deliberately NOT either internal_auth legacy hardcoded string,
# so nothing here can pass by the legacy path.
_REAL_KEY = "marketing-real-key-7Qv2mX9t"
_WRONG_KEY = "marketing-attacker-key-nope-0"
_ROTATED_KEY = "marketing-rotated-key-3Ld8bN1c"
# internal_auth._LEGACY_KEYS — public strings anyone with repo history has.
_LEGACY_KEY = "dchub-internal-sync-2026"


class _Route:
    def __init__(self, id, path, method, view, body_status, body_error,
                 hits_conn=True, setattrs=()):
        self.id = id
        self.path = path
        self.method = method
        self.view = view                # function name, for the AST tests
        self.body_status = body_status  # what the handler answers once let in,
        self.body_error = body_error    # in a process with no DB and no tokens
        self.hits_conn = hits_conn      # _conn() recorder is the "body ran" mark
        self.setattrs = setattrs        # module constants to set so the handler
                                        # reaches _conn instead of an earlier exit


_ROUTES = [
    _Route("auto-generate", "/api/v1/marketing/auto-generate", "post",
           "auto_generate", 503, "no_database"),
    _Route("publish-now", "/api/v1/marketing/publish-now", "post",
           "publish_now", 503, "no_database"),
    _Route("repost-now", "/api/v1/marketing/repost-now", "post",
           "repost_now", 503, "no_database"),
    # Returns 503 DCHUB_RESEND_API_KEY not configured BEFORE _conn() unless the
    # module constant is set; set it so this route uses the same _conn marker as
    # its siblings. The Resend call is much further down, past the DB read.
    _Route("send-daily-email", "/api/v1/marketing/linkedin/send-daily-email",
           "post", "linkedin_send_daily_email", 503, "no_database",
           setattrs=(("RESEND_API_KEY", "resend-key-never-used-here"),)),
    # whoami never reaches _conn: it reads LINKEDIN_ACCESS_TOKEN and, with the
    # token absent, answers its own 500 before any HTTP call. That distinctive
    # 500 is its "the body ran" marker.
    _Route("whoami", "/api/v1/marketing/linkedin/whoami", "get",
           "linkedin_whoami", 500, "LINKEDIN_ACCESS_TOKEN not set",
           hits_conn=False),
]
_IDS = [r.id for r in _ROUTES]


def _clear_secrets(monkeypatch):
    for v in _SECRET_VARS:
        monkeypatch.delenv(v, raising=False)


def _app(monkeypatch, route):
    """A Flask app carrying the real blueprint, with every side effect stubbed:
    no database, no LinkedIn token, no Resend dispatch."""
    mod = pytest.importorskip(MODULE)
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(mod, "DATABASE_URL", None)
    for name, value in route.setattrs:
        monkeypatch.setattr(mod, name, value)

    conn_calls = []

    def _fake_conn(*a, **k):
        conn_calls.append((a, k))
        return None            # every handler answers 503 no_database on None

    monkeypatch.setattr(mod, "_conn", _fake_conn)

    app = flask.Flask(__name__)
    app.register_blueprint(mod.marketing_bp)
    return app, conn_calls


def _call(app, route, headers=None, query=""):
    client = app.test_client()
    return getattr(client, route.method)(route.path + query,
                                         headers=headers or {})


def _assert_declined(resp, calls, route, why):
    assert resp.status_code == 401, (
        f"{route.method.upper()} {route.path} returned {resp.status_code}, "
        f"not 401 — {why}"
    )
    assert (resp.get_json() or {}).get("error") == "unauthorized", (
        f"401 came from somewhere other than _require_admin: "
        f"{resp.get_json()!r}"
    )
    if route.hits_conn:
        assert calls == [], (
            f"the body ran ({calls}) on a declined request to {route.path} — "
            "this route reaches an IndexNow submit or a social publisher"
        )


def _assert_admitted(resp, calls, route):
    assert resp.status_code == route.body_status, (
        f"{route.method.upper()} {route.path} answered {resp.status_code}, not "
        f"the handler's own {route.body_status} — the legitimate caller was "
        f"locked out. body={resp.get_json()!r}"
    )
    assert (resp.get_json() or {}).get("error") == route.body_error, (
        f"expected the handler's own {route.body_error!r}, got "
        f"{resp.get_json()!r}"
    )
    if route.hits_conn:
        assert len(calls) == 1, f"authorized body did not run exactly once: {calls}"


# ── the fail-open defect ─────────────────────────────────────────────────────

@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_no_secret_configured_declines_and_body_does_not_run(monkeypatch, route):
    """DEFECT 1. Every secret unset — a misconfigured process — and no
    credential. The old gate skipped auth entirely here."""
    _clear_secrets(monkeypatch)
    app, calls = _app(monkeypatch, route)
    _assert_declined(_call(app, route), calls, route,
                     "with NO secret in the env the gate disabled itself")


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_key_configured_but_no_credential_declines(monkeypatch, route):
    """A key IS configured and the caller sends nothing: proves the gate
    rejects a missing credential, not merely that the box is unconfigured."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    _assert_declined(_call(app, route), calls, route,
                     "an anonymous caller reached the handler")


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_wrong_credential_declines(monkeypatch, route):
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    resp = _call(app, route, headers={"X-Admin-Key": _WRONG_KEY})
    _assert_declined(resp, calls, route, "a wrong key was accepted")


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_wrong_credential_in_the_query_slot_declines(monkeypatch, route):
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    resp = _call(app, route, query=f"?admin_key={_WRONG_KEY}")
    _assert_declined(resp, calls, route, "a wrong ?admin_key was accepted")


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_empty_credential_declines(monkeypatch, route):
    """An empty header must not read as "no comparison to make". This is the
    shape that fell open when the snapshot was empty on both sides."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    resp = _call(app, route, headers={"X-Admin-Key": ""})
    _assert_declined(resp, calls, route, "an empty X-Admin-Key was accepted")


def test_legacy_hardcoded_key_does_not_authorize(monkeypatch):
    """X-Internal-Key is a NEW credential slot on these routes — the old gate
    ignored that header entirely. Opening it must not open the two public
    legacy strings baked into internal_auth for the migration window."""
    import internal_auth
    assert internal_auth.LEGACY_OK is False, (
        "INTERNAL_AUTH_LEGACY_OK=1 is set in this environment, so the legacy "
        "hardcoded keys are accepted by design and this test cannot judge"
    )
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    route = _ROUTES[0]
    app, calls = _app(monkeypatch, route)
    resp = _call(app, route, headers={"X-Internal-Key": _LEGACY_KEY})
    _assert_declined(resp, calls, route,
                     "a legacy hardcoded key authorized an IndexNow-reaching route")


# ── the callers must keep working ────────────────────────────────────────────

@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_admin_key_header_is_accepted(monkeypatch, route):
    """The header every real caller sends: evolve-cron.yml, publish-verify.yml,
    repost-now.yml, linkedin-whoami.yml, dchub-scheduler.py api_call() and
    routes/press_publisher_restart.py all send X-Admin-Key: $DCHUB_ADMIN_KEY."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    _assert_admitted(_call(app, route, headers={"X-Admin-Key": _REAL_KEY}),
                     calls, route)


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_admin_key_query_param_is_accepted(monkeypatch, route):
    """?admin_key= was the other slot the old gate read. It must survive."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    _assert_admitted(_call(app, route, query=f"?admin_key={_REAL_KEY}"),
                     calls, route)


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_admin_key_falls_back_to_dchub_internal_key(monkeypatch, route):
    """The old snapshot read DCHUB_ADMIN_KEY *or* DCHUB_INTERNAL_KEY, so an
    operator whose box only has the latter could authenticate with X-Admin-Key.
    That still works."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    _assert_admitted(_call(app, route, headers={"X-Admin-Key": _REAL_KEY}),
                     calls, route)


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_internal_key_header_is_accepted(monkeypatch, route):
    """X-Internal-Key is the slot internal automation uses; dchub-scheduler.py
    already sends it alongside X-Admin-Key on every call."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)
    _assert_admitted(_call(app, route, headers={"X-Internal-Key": _REAL_KEY}),
                     calls, route)


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_scheduler_header_combo_is_accepted(monkeypatch, route):
    """dchub-scheduler.py api_call() sends X-Admin-Key AND X-Internal-Key, and
    press_publisher_restart.py adds X-DC-Internal-Cron:1. That combination must
    authorize or the crons that drive these routes 401."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "a-different-internal-key-4242")
    app, calls = _app(monkeypatch, route)
    _assert_admitted(_call(app, route, headers={
        "X-Admin-Key": _REAL_KEY,
        "X-Internal-Key": "a-different-internal-key-4242",
        "X-DC-Internal-Cron": "1",
        "User-Agent": "DCHub-Scheduler/3.9",
    }), calls, route)


def test_forged_internal_cron_header_alone_does_not_authorize(monkeypatch):
    """press_publisher_restart.py sends X-DC-Internal-Cron:1 next to its real
    credential. That plaintext header is forgeable — #4411 found three brain
    routes treating it as a full bypass — so assert it carries no authority
    here, on the route that reaches ping_indexnow."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    route = _ROUTES[0]
    app, calls = _app(monkeypatch, route)
    resp = _call(app, route, headers={"X-DC-Internal-Cron": "1"})
    _assert_declined(resp, calls, route,
                     "a forgeable plaintext header bypassed auth")


# ── the stale-snapshot defect ────────────────────────────────────────────────

@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_key_rotation_takes_effect_without_a_restart(monkeypatch, route):
    """DEFECT 2. One app object, one process; only os.environ changes between
    requests. A gate reading an import-time (or first-request-cached) snapshot
    cannot answer all three of these correctly."""
    _clear_secrets(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    app, calls = _app(monkeypatch, route)

    _assert_admitted(_call(app, route, headers={"X-Admin-Key": _REAL_KEY}),
                     calls, route)

    # Ops rotates the key on Railway. No restart.
    calls.clear()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _ROTATED_KEY)
    _assert_admitted(_call(app, route, headers={"X-Admin-Key": _ROTATED_KEY}),
                     calls, route)

    calls.clear()
    resp = _call(app, route, headers={"X-Admin-Key": _REAL_KEY})
    _assert_declined(resp, calls, route,
                     "the RETIRED key still authorized after rotation — the "
                     "gate is reading a snapshot, not the environment")


# ── structure: AST, so a name in a comment or docstring cannot satisfy it ────

def _tree():
    with open(SOURCE, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _function(name):
    for n in ast.walk(_tree()):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in {SOURCE}")


def _returns_401(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Return) and n.value is not None:
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and c.value == 401:
                    return True
    return False


@pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
def test_route_is_decorated_with_the_gate(route):
    """The behaviour tests above only mean something while these five views
    actually wear @_require_admin. A sixth route added without it is the next
    version of this bug, so read the decorator list off the source."""
    fn = _function(route.view)
    names = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
    assert "_require_admin" in names, (
        f"{route.view} ({route.path}) is no longer decorated with "
        f"@_require_admin — it is ungated. decorators: {names}"
    )


def test_gate_calls_the_failclosed_helper():
    fn = _function("_require_admin")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "require_internal_or_admin" in called, (
        "_require_admin no longer calls internal_auth.require_internal_or_admin "
        f"— the fail-closed gate was removed or replaced. calls: {sorted(called)}"
    )


def test_gate_has_no_import_time_key_snapshot():
    """No module-level name bound to an os.environ read of an admin/internal
    key may exist for the gate to close over. This is the AST half of the
    rotation test: the defect is a name reference, and a literal-string audit
    cannot see a gate that compares through a variable."""
    snapshots = []
    for n in _tree().body:
        if not isinstance(n, ast.Assign):
            continue
        dumped = ast.dump(n.value)
        if "environ" not in dumped:
            continue
        if not any(k in dumped for k in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY")):
            continue
        snapshots += [t.id for t in n.targets if isinstance(t, ast.Name)]
    assert not snapshots, (
        f"module-level admin-key snapshot(s) are back: {snapshots}. A value "
        "captured at import cannot see a rotated env var, and `if <snapshot> "
        "and ...` skips auth entirely when the var is unset."
    )


def test_gate_is_not_self_disabling():
    """No `if <key snapshot> and <comparison>: return 401` inside the wrapper."""
    fn = _function("_require_admin")
    offenders = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not _returns_401(n):
            continue
        if isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And):
            for v in n.test.values:
                if isinstance(v, ast.Name) and v.id.endswith("ADMIN_KEY"):
                    offenders.append((n.lineno, v.id))
    assert not offenders, (
        f"self-disabling `if <ADMIN_KEY> and ...: 401` is back at {offenders} "
        "— it fails OPEN on any process whose env lacks the key"
    )
