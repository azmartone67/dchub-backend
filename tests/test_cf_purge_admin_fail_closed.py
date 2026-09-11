"""POST /api/v1/cf/purge must FAIL CLOSED (2026-09-11).

WHAT THIS PROVES
----------------
`purge_endpoint` takes CALLER-SUPPLIED urls and hands them to Cloudflare's
purge-by-file API. An open version is an arbitrary-URL zone-eviction primitive
(cheap origin-load amplification against dchub.cloud), which is why it is
admin-gated while the derived-list one-shots (markets-fix, frontend-static,
tier2-export, og-cards) are deliberately public.

The gate used to be the self-disabling shape:

    _ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()   # import time
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

When DCHUB_ADMIN_KEY is absent at import — exactly what a misconfigured process
looks like; dchub-worker was in that state on 2026-08-08 — `_ADMIN_KEY` is "",
the `if` never fires, and ANY caller can purge ANY url. Production currently has
the key set, so this was a LATENT fail-open, not a live one (measured
2026-09-11: unauthenticated POST {"urls":[]} -> 401 on the edge and the Railway
origin). The fix routes through internal_auth.require_internal_or_admin, which
re-reads env PER REQUEST and returns False when no secret is configured.

WHAT THE TESTS COVER  (unset key / wrong key / right key, per the task)
  * no key configured  -> 401, and _purge_urls is NEVER called  (the regression)
  * a key IS set, wrong credential -> 401, _purge_urls never called
  * a key IS set, right X-Admin-Key -> 200, _purge_urls called with the urls
  * internal automation (X-Internal-Key == DCHUB_INTERNAL_KEY) -> 200 (brain L1)

Asserting _purge_urls was NOT called (not merely that the status is 401)
separates "declined" from "declined but ran anyway".

MUTATION-VERIFIED (verify-a-guard). Reintroducing the fail-open gate
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY: ...
in a clean env (all four secret vars unset, so the import-time _ADMIN_KEY == "")
flips test_no_key_configured_rejects_and_does_not_purge from green to red
(200, _purge_urls called). Restoring returns it to green. Recorded in the PR.
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
_REAL_KEY = "cf-purge-real-admin-key-7Qm2xZ"
_WRONG_KEY = "cf-purge-attacker-key-000-nope"


def _app():
    import routes.cf_purge as m
    app = flask.Flask(__name__)
    app.register_blueprint(m.cf_purge_bp)
    return app, m


def _stub_purge(monkeypatch, m):
    """Replace _purge_urls with a recorder so we can prove whether the purge
    actually executed, and never touch the real Cloudflare API from a test."""
    calls = []

    def _fake(urls):
        calls.append(list(urls))
        return {"ok": True, "purged": list(urls)}

    monkeypatch.setattr(m, "_purge_urls", _fake)
    return calls


# ── behaviour: the three cases the task names, plus the internal-cron path ────

def test_no_key_configured_rejects_and_does_not_purge(monkeypatch):
    """THE REGRESSION. With every admin/internal secret unset — a misconfigured
    process — an arbitrary credential must be REFUSED, and the purge must not
    run. The old import-time gate fell open here."""
    for v in _SECRET_VARS:
        monkeypatch.delenv(v, raising=False)
    app, m = _app()
    calls = _stub_purge(monkeypatch, m)
    with app.test_client() as c:
        r = c.post("/api/v1/cf/purge",
                   json={"urls": ["https://dchub.cloud/markets"]},
                   headers={"X-Admin-Key": _WRONG_KEY})
    assert r.status_code == 401, (
        f"POST /api/v1/cf/purge returned {r.status_code} with NO secret in the "
        "env — the gate disabled itself instead of denying"
    )
    assert calls == [], (
        f"the purge executed ({calls}) despite an unauthorized request — a 401 "
        "that still evicts the zone is not fail-closed"
    )


def test_no_key_configured_rejects_even_with_no_credential(monkeypatch):
    for v in _SECRET_VARS:
        monkeypatch.delenv(v, raising=False)
    app, m = _app()
    calls = _stub_purge(monkeypatch, m)
    with app.test_client() as c:
        r = c.post("/api/v1/cf/purge",
                   json={"urls": ["https://dchub.cloud/markets"]})
    assert r.status_code == 401
    assert calls == []


def test_wrong_key_rejected_and_does_not_purge(monkeypatch):
    """A key IS configured; a different credential is refused."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(v, raising=False)
    app, m = _app()
    calls = _stub_purge(monkeypatch, m)
    with app.test_client() as c:
        r = c.post("/api/v1/cf/purge",
                   json={"urls": ["https://dchub.cloud/markets"]},
                   headers={"X-Admin-Key": _WRONG_KEY})
    assert r.status_code == 401, f"wrong key was accepted ({r.status_code})"
    assert calls == []


def test_right_admin_key_is_accepted_and_purges(monkeypatch):
    """The right X-Admin-Key gets through and the urls reach _purge_urls."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _REAL_KEY)
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(v, raising=False)
    app, m = _app()
    calls = _stub_purge(monkeypatch, m)
    with app.test_client() as c:
        r = c.post("/api/v1/cf/purge",
                   json={"urls": ["https://dchub.cloud/markets"]},
                   headers={"X-Admin-Key": _REAL_KEY})
    assert r.status_code == 200, (
        f"the right admin key was rejected ({r.status_code}) — this gate would "
        "lock out the legitimate operator and brain L1 auto-fix"
    )
    assert calls == [["https://dchub.cloud/markets"]], (
        f"the authorized purge did not execute as expected: {calls}"
    )


def test_internal_key_is_accepted(monkeypatch):
    """Internal automation authenticates with X-Internal-Key == DCHUB_INTERNAL_KEY
    (the brain L1 auto-fix path the module docstring describes). require_internal
    _or_admin honors it; admin >= internal."""
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", _REAL_KEY)
    for v in ("DCHUB_ADMIN_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(v, raising=False)
    app, m = _app()
    calls = _stub_purge(monkeypatch, m)
    with app.test_client() as c:
        r = c.post("/api/v1/cf/purge",
                   json={"url": "https://dchub.cloud/markets"},
                   headers={"X-Internal-Key": _REAL_KEY})
    assert r.status_code == 200
    assert calls == [["https://dchub.cloud/markets"]]


# ── structure: the fail-open shape must not come back in this handler ─────────
# AST, not substring: the helper name appears in this module's own comments and
# docstring, so `"require_internal_or_admin" in source` would pass vacuously.

def _purge_endpoint_node():
    src = open(os.path.join(REPO, "routes/cf_purge.py")).read()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "purge_endpoint":
            return n
    raise AssertionError("purge_endpoint not found in routes/cf_purge.py")


def _returns_401(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Return) and n.value is not None:
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and c.value == 401:
                    return True
    return False


def test_handler_calls_the_failclosed_helper():
    fn = _purge_endpoint_node()
    called = {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "require_internal_or_admin" in called, (
        "purge_endpoint no longer calls require_internal_or_admin — the "
        "fail-closed gate was removed or replaced"
    )


def test_handler_has_no_self_disabling_admin_gate():
    """No `if <import-time key snapshot> and ...: return 401` inside the handler.
    That BoolOp(And)-guarded 401 is the shape that falls open when the env var
    is unset."""
    fn = _purge_endpoint_node()
    offenders = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not _returns_401(n):
            continue
        if isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And):
            for v in n.test.values:
                if isinstance(v, ast.Name) and v.id.endswith("ADMIN_KEY"):
                    offenders.append((n.lineno, v.id))
    assert not offenders, (
        f"self-disabling `if <ADMIN_KEY snapshot> and ...: 401` gate is back in "
        f"purge_endpoint at {offenders} — it fails OPEN when the env var is unset"
    )
