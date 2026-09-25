"""ops_viewer_gate: the ops/admin read gate and its named, read-only viewer keys.

No DB, no network: a bare Flask app with the REAL gate installed through
install(), dummy handlers at the real paths, the key store and the denial
recorder swapped for in-memory ones. Every request comes from a NON-loopback
address — the Flask test client defaults to 127.0.0.1, which the gate trusts
as a self-call, so a default-context test would pass for every caller.

The real app (main.py) is checked separately in scripts/app_contract_gate.py,
which boots it and asserts the gate is the first before_request hook and
refuses a keyless external read in enforce mode.
"""
import json

import pytest
from flask import Flask, jsonify, request

import ops_viewer_gate as ovg

EXT = {"REMOTE_ADDR": "203.0.113.9"}
ADMIN = "test-admin-key-not-a-secret"
VIEWER = "dchv_test-viewer-key-not-a-secret"
SECRET_BODY = {"funnel": "ops-only-numbers", "retention": 0.42}


class _Keys:
    def __init__(self, m):
        self.m = dict(m)

    def lookup(self, sha):
        return self.m.get(sha)

    def invalidate(self):
        pass


class _Rec:
    def __init__(self):
        self.denials, self.used = [], []

    def record_denial(self, row):
        self.denials.append(row)

    def record_use(self, name):
        self.used.append(name)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET",
              "DCHUB_CRON_SECRET", ovg.MODE_ENV, ovg.EXTRA_PATHS_ENV,
              ovg.PUBLIC_PATHS_ENV, "DCHUB_ROLE"):
        monkeypatch.delenv(v, raising=False)
    keys = _Keys({ovg.hash_key(VIEWER): "jonathan-claude-dashboard"})
    rec = _Rec()
    monkeypatch.setattr(ovg, "KEYSTORE", keys)
    monkeypatch.setattr(ovg, "RECORDER", rec)

    app = Flask(__name__)
    ovg.install(app)

    def data():
        return jsonify(SECRET_BODY)

    for i, p in enumerate(("/api/v1/mcp/funnel/diagnostics", "/api/v1/mcp/funnel-stages",
                           "/api/v1/mcp/retention", "/api/v1/mcp/retention/cohorts",
                           "/api/v1/ops/scoreboard", "/api/v1/admin/funnel/leakage",
                           "/api/admin/crm/customers", "/admin/agent-retention",
                           "/ops/dashboard", "/dashboard",
                           "/api/v1/mcp/funnel", "/api/v1/ops/deadman")):
        app.add_url_rule(p, "d%d" % i, data)
    app.add_url_rule("/api/v1/ops/brief", "brief", lambda: jsonify(brief=True))
    app.add_url_rule("/api/v1/ops/claims", "claims", lambda: jsonify(claims=[]))
    app.add_url_rule("/llms.txt", "llms", lambda: "# DC Hub\n")
    app.add_url_rule("/api/v1/facilities", "fac", lambda: jsonify(facilities=[1]))

    # A mutating admin route guarded the way the repo's routes are guarded
    # today: the shared internal_auth check on the admin/internal slots.
    @app.post("/api/v1/admin/test-only-guarded-write")
    def mutate():
        from internal_auth import require_internal_or_admin
        if not require_internal_or_admin(request):
            return jsonify(error="admin key required"), 401
        return jsonify(applied=True)

    # A mutating admin route with NO route-level check (the gate is the only
    # thing between a viewer key and it).
    @app.post("/api/v1/admin/unguarded-write")
    def unguarded():
        return jsonify(wrote=True)

    return app.test_client(), rec, monkeypatch


def _get(client, path, **headers):
    return client.get(path, headers=headers, environ_base=EXT)


GATED_READS = ["/api/v1/mcp/funnel/diagnostics", "/api/v1/mcp/funnel-stages",
               "/api/v1/mcp/retention", "/api/v1/mcp/retention/cohorts",
               "/api/v1/ops/scoreboard", "/api/v1/admin/funnel/leakage",
               "/api/admin/crm/customers", "/admin/agent-retention",
               "/ops/dashboard"]


# ───────────────────────────────────────────────────────── enforce mode ──

@pytest.mark.parametrize("path", GATED_READS)
def test_enforce_keyless_is_401_with_no_data(env, path):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, path)
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}
    assert b"ops-only-numbers" not in r.get_data()
    assert "no-store" in r.headers["Cache-Control"]
    assert rec.denials and rec.denials[-1]["reason"] == "no_credential"
    assert rec.denials[-1]["ip"] == "203.0.113.9"


def test_enforce_wrong_viewer_key_is_401(env):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, "/api/v1/mcp/funnel/diagnostics", **{"X-Admin-Key": "dchv_not-a-real-key"})
    assert r.status_code == 401 and b"ops-only-numbers" not in r.get_data()
    assert rec.denials[-1]["reason"] == "unknown_or_revoked_viewer_key"


def test_enforce_wrong_admin_key_is_401(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, "/api/v1/ops/scoreboard", **{"X-Admin-Key": "guess"})
    assert r.status_code == 401


@pytest.mark.parametrize("path", GATED_READS)
@pytest.mark.parametrize("slot", ["header", "bearer", "query"])
def test_viewer_key_reads_every_gated_path(env, path, slot):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    if slot == "header":
        r = _get(client, path, **{"X-Admin-Key": VIEWER})
    elif slot == "bearer":
        r = _get(client, path, Authorization="Bearer " + VIEWER)
    else:
        r = _get(client, path + "?admin_key=" + VIEWER)
    assert r.status_code == 200 and r.get_json() == SECRET_BODY
    assert rec.used[-1] == "jonathan-claude-dashboard"
    assert not rec.denials


def test_viewer_key_head_passes(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = client.head("/api/v1/mcp/funnel/diagnostics", headers={"X-Admin-Key": VIEWER},
                    environ_base=EXT)
    assert r.status_code == 200


def test_admin_key_still_reads(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    assert _get(client, "/api/v1/mcp/funnel/diagnostics", **{"X-Admin-Key": ADMIN}).status_code == 200
    assert _get(client, "/api/v1/mcp/funnel/diagnostics?admin_key=" + ADMIN).status_code == 200


def test_existing_admin_cookie_still_reads(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    client.set_cookie("dchub_admin_key", ADMIN)
    assert _get(client, "/api/v1/admin/funnel/leakage").status_code == 200


def test_internal_key_and_cron_secret_still_read(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    mp.setenv("DCHUB_INTERNAL_KEY", "internal-test-key")
    mp.setenv("DCHUB_CRON_SECRET", "cron-test-secret")
    assert _get(client, "/api/v1/ops/scoreboard",
                **{"X-Internal-Key": "internal-test-key"}).status_code == 200
    assert _get(client, "/api/v1/ops/scoreboard",
                **{"X-Internal-Cron": "cron-test-secret"}).status_code == 200
    assert _get(client, "/api/v1/ops/scoreboard",
                **{"X-Internal-Cron": "wrong"}).status_code == 401


def test_loopback_self_call_passes_but_only_by_socket_peer(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = client.get("/api/v1/mcp/funnel/diagnostics", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200
    # A forged forwarding header from an external peer is not loopback.
    r = client.get("/api/v1/mcp/funnel/diagnostics", headers={"X-Forwarded-For": "127.0.0.1"},
                   environ_base=EXT)
    assert r.status_code == 401


# ───────────────────────────────────────────── viewer keys are read-only ──

def test_viewer_key_cannot_write_through_the_gate(env):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = client.post("/api/v1/admin/unguarded-write", headers={"X-Admin-Key": VIEWER},
                    environ_base=EXT)
    assert r.status_code == 403 and r.get_json() == {"error": "forbidden"}
    assert rec.denials[-1]["reason"] == "viewer_key_on_write"


@pytest.mark.parametrize("mode", ["log", "enforce", "off"])
def test_viewer_key_is_refused_by_route_level_admin_check_in_every_mode(env, mode):
    """Even when the gate lets a request through (log / off), the route's own
    admin check does not know viewer keys — so a viewer key authorizes no
    write anywhere, whatever the mode."""
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, mode)
    for hdr in ({"X-Admin-Key": VIEWER}, {"X-Internal-Key": VIEWER},
                {"Authorization": "Bearer " + VIEWER}):
        r = client.post("/api/v1/admin/test-only-guarded-write", headers=hdr,
                        environ_base=EXT)
        assert r.status_code in (401, 403), (mode, hdr, r.status_code)
        assert r.get_json() != {"applied": True}
    # Control: the same seat with the real admin key does reach the write.
    r = client.post("/api/v1/admin/test-only-guarded-write",
                    headers={"X-Admin-Key": ADMIN}, environ_base=EXT)
    assert r.status_code == 200 and r.get_json() == {"applied": True}


def test_internal_auth_never_accepts_a_viewer_key(env):
    from internal_auth import is_valid_internal_key
    assert is_valid_internal_key(VIEWER) is False
    assert is_valid_internal_key(ADMIN) is True  # control, same process env


def test_mint_and_revoke_refuse_a_viewer_key(env):
    client, _, mp = env
    for mode in ("log", "enforce"):
        mp.setenv(ovg.MODE_ENV, mode)
        r = client.post("/api/v1/admin/ops-gate/keys", json={"name": "x"},
                        headers={"X-Admin-Key": VIEWER}, environ_base=EXT)
        assert r.status_code in (401, 403)
        assert "key" not in (r.get_json() or {})
        r = client.post("/api/v1/admin/ops-gate/keys/jonathan-claude-dashboard/revoke",
                        headers={"X-Admin-Key": VIEWER}, environ_base=EXT)
        assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────── stays public ──

@pytest.mark.parametrize("path", ["/api/v1/ops/brief", "/api/v1/ops/claims",
                                  "/llms.txt", "/api/v1/facilities", "/dashboard"])
def test_public_paths_stay_public_in_enforce(env, path):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, path)
    assert r.status_code == 200, path
    assert not rec.denials


def test_public_paths_env_exempts_at_flip_time(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    assert _get(client, "/api/v1/ops/scoreboard").status_code == 401
    mp.setenv(ovg.PUBLIC_PATHS_ENV, "/api/v1/ops/scoreboard")
    assert _get(client, "/api/v1/ops/scoreboard").status_code == 200


def test_extra_paths_env_gates_a_subtree(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    assert _get(client, "/dashboard").status_code == 200
    mp.setenv(ovg.EXTRA_PATHS_ENV, "/dashboard")
    assert _get(client, "/dashboard").status_code == 401


def test_cors_preflight_is_not_blocked(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = client.options("/api/v1/mcp/funnel/diagnostics", environ_base=EXT)
    assert r.status_code != 401


# ─────────────────────────────────────────────────────── log-only mode ──

@pytest.mark.parametrize("mode_value", [None, "", "log", "LOG", "enforc", "yes"])
def test_log_mode_is_the_default_and_serves_but_records(env, mode_value):
    client, rec, mp = env
    if mode_value is None:
        mp.delenv(ovg.MODE_ENV, raising=False)
    else:
        mp.setenv(ovg.MODE_ENV, mode_value)
    r = _get(client, "/api/v1/mcp/funnel/diagnostics", **{"User-Agent": "probe/1.0",
                                              "CF-Connecting-IP": "198.51.100.7"})
    assert r.status_code == 200 and r.get_json() == SECRET_BODY
    assert len(rec.denials) == 1
    row = rec.denials[0]
    assert row["mode"] == "log" and row["status"] == 401
    assert row["ip"] == "198.51.100.7" and row["user_agent"] == "probe/1.0"
    assert row["path"] == "/api/v1/mcp/funnel/diagnostics" and row["method"] == "GET"


def test_log_mode_leaves_response_headers_alone(env):
    client, _, mp = env
    mp.delenv(ovg.MODE_ENV, raising=False)
    r = _get(client, "/api/v1/mcp/funnel/diagnostics")
    assert "no-store" not in (r.headers.get("Cache-Control") or "")


def test_off_mode_records_nothing(env):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "off")
    assert _get(client, "/api/v1/mcp/funnel/diagnostics").status_code == 200
    assert rec.denials == []


def test_report_endpoints_require_a_key_even_in_log_mode(env):
    client, _, mp = env
    mp.delenv(ovg.MODE_ENV, raising=False)
    for p in ("/api/v1/admin/ops-gate/denials", "/api/v1/admin/ops-gate/status"):
        r = _get(client, p)
        assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}


# ───────────────────────────────────────────── cookie for the HTML shells ──

def test_shell_opened_with_viewer_key_sets_httponly_cookie_that_reads_api(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, "/ops/dashboard?admin_key=" + VIEWER)
    assert r.status_code == 200
    cookies = [c for c in r.headers.getlist("Set-Cookie")
               if c.startswith(ovg.COOKIE_NAME + "=")]
    assert cookies and "HttpOnly" in cookies[0] and "Secure" in cookies[0]
    assert "SameSite=Strict" in cookies[0]
    # The cookie alone (no key in the request) now reads an API path.
    client.set_cookie(ovg.COOKIE_NAME, VIEWER)
    assert _get(client, "/api/v1/mcp/retention").status_code == 200


def test_api_path_with_viewer_query_sets_no_cookie(env):
    client, _, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, "/api/v1/mcp/funnel/diagnostics?admin_key=" + VIEWER)
    assert not [c for c in r.headers.getlist("Set-Cookie")
                if c.startswith(ovg.COOKIE_NAME + "=")]


# ─────────────────────────────────────────────────────────── predicates ──

@pytest.mark.parametrize("path,gated", [
    ("/api/v1/admin", True), ("/api/v1/admin/x/y", True),
    ("/api/v1/administrator", False),
    ("/api/admin/usage-report", True),
    ("/api/v1/ops", True), ("/api/v1/ops/scoreboard", True),
    ("/api/v1/ops/scoreboard/", True), ("//api/v1//ops/scoreboard", True),
    ("/api/v1/ops/deadman", False), ("/api/v1/ops/deadman/", False),
    ("/api/v1/ops/brief", False), ("/api/v1/ops/brief/", False),
    ("/api/v1/ops/claims", False), ("/api/v1/ops/origin-freshness", False),
    ("/api/v1/mcp/funnel", False), ("/api/v1/mcp/funnel/", False),
    ("/api/v1/mcp/funnel/diagnostics", True),
    ("/api/v1/mcp/funnel-stages", True), ("/api/v1/mcp/handoff-funnel", False),
    ("/api/v1/mcp/retention", True), ("/api/v1/mcp/retention/cohorts", True),
    ("/admin/crm", True), ("/administer", False), ("/ops/dashboard", True),
    ("/dashboard", False), ("/dashboard/usage", False),
    ("/llms.txt", False), ("/api/v1/facilities", False), ("/", False),
])
def test_is_gated_path(env, path, gated):
    assert ovg.is_gated_path(path) is gated


def test_install_puts_the_gate_first(env):
    app = Flask(__name__)

    @app.before_request
    def earlier_hook():
        return None

    ovg.install(app)
    assert app.before_request_funcs[None][0] is ovg.ops_gate


def test_denial_log_line_is_structured_json(env, caplog):
    import logging
    rec = ovg.DbRecorder()
    rec._ensure_thread = lambda: None  # no background thread, no DB
    with caplog.at_level(logging.INFO, logger="ops_viewer_gate"):
        row = {"ip": "203.0.113.9", "user_agent": "ua", "method": "GET",
               "path": "/api/v1/ops/scoreboard", "reason": "no_credential",
               "mode": "log", "status": 401}
        rec.record_denial(row)
        rec.record_denial(row)  # same tuple: aggregated, logged once
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("ops_gate_denial ")]
    assert len(lines) == 1
    assert json.loads(lines[0][len("ops_gate_denial "):]) == row
    assert list(rec._pending.values())[0][2] == 2


# ── owner decision 2026-09-25: funnel and deadman stay public ─────────────

@pytest.mark.parametrize("path", ["/api/v1/mcp/funnel", "/api/v1/ops/deadman"])
def test_funnel_and_deadman_stay_public_in_enforce(env, path):
    client, rec, mp = env
    mp.setenv(ovg.MODE_ENV, "enforce")
    r = _get(client, path)
    assert r.status_code == 200 and r.get_json() == SECRET_BODY, path
    assert not rec.denials
