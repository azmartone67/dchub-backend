"""The /api/v1/heal/* endpoints must not be an anonymous probe-burst button.

The bug (2026-08-08). `/api/v1/heal/force` was registered
`methods=["POST", "GET"]` with **no auth decorator at all**. One anonymous
GET — a crawler, a link prefetcher, anything that follows a URL — called
`heal_cycle_blocking()`, which expands the 10 entries in `PROBES` into ~414
URL fetches through the public edge and runs the write-side fixers in
`FIXES` (SQL patches, cache invalidation). That is the exact probe volume
r71/r72/r78 were built to contain: Cloudflare once measured DCHubHealer/1.0
at ~119k requests/day, ~30% of all traffic to dchub.cloud. Verified on
2026-08-08 that the Cloudflare edge does **not** block or rate-limit the
path family, so the origin route was the only gate and there wasn't one.

`/api/v1/heal/log` was open too, returning up to 200 raw self_heal_events
rows: internal endpoint paths, fixer names, and a `details` column holding
verbatim Postgres errors (a live sample leaked a SQL fragment and a table
name) or up to 1 KB of Python traceback when a fixer raises.

These tests fence the four ways it comes back:
  1. METHOD   — GET must not be a lane into /heal/force.
  2. GATE     — the handlers must call the gate AND return its refusal.
                A gate whose result is computed and dropped is the classic
                vacuous version, so the shape is checked, not the call.
  3. BEHAVIOUR— the gate itself must 401 anonymous, 503 when unconfigured,
                and pass a correct key. Drives the REAL function.
  4. DRIFT    — the forced path must not hand-roll its own copy of the
                cycle body. It did, and the copy had fallen behind: no
                `__cycle__` marker, no by_fixer/by_pattern counters and so
                no livelock warning.

House rules: no DB, never import main (it opens pools and registers ~200
blueprints), nothing at module scope. main.py is read as TEXT and sliced
with ast; dchub_self_heal is stdlib-only at import time so it is driven for
real, with its DB and network edges stubbed.

Run:  python3 -m pytest tests/test_heal_endpoint_auth.py -v
"""

import ast
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"

_HEAL_FORCE = "/api/v1/heal/force"
_HEAL_LOG = "/api/v1/heal/log"
_HEAL_STATUS = "/api/v1/heal/status"


# ── helpers ───────────────────────────────────────────────────────────

def _main_tree():
    return ast.parse(_MAIN.read_text(encoding="utf-8", errors="replace"))


def _routes():
    """{path: {"func": FunctionDef, "methods": [...]}} for @app.route defs."""
    out = {}
    for node in ast.walk(_main_tree()):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if getattr(fn, "attr", None) != "route":
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            methods = None
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts
                               if isinstance(e, ast.Constant)]
            out[dec.args[0].value] = {"func": node, "methods": methods}
    return out


def _route(path):
    routes = _routes()
    assert path in routes, (
        "%s is not registered in main.py any more — this fence is pointing at "
        "a route that no longer exists and would pass vacuously" % path)
    return routes[path]


def _func_src(tree, src, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    raise AssertionError("%s() not found in main.py" % name)


def _load_heal_admin_gate(env, headers):
    """exec the REAL _heal_admin_gate out of main.py against stubs.

    Returns (result, calls). `jsonify` is stubbed to a dict so the refusal
    body can be inspected without a Flask app.
    """
    src = _MAIN.read_text(encoding="utf-8", errors="replace")
    gate_src = _func_src(ast.parse(src), src, "_heal_admin_gate")

    class _Req:
        pass

    req = _Req()
    req.headers = headers

    ns = {"jsonify": lambda **kw: dict(kw), "request": req}
    exec(compile(gate_src, "<_heal_admin_gate>", "exec"), ns)
    return ns["_heal_admin_gate"]


# A 64-hex stand-in shaped like the real DCHUB_ADMIN_KEY. Fake, but it must
# clear the same strength bar util/admin_auth applies to the real one.
def _strong_key():
    return "b4e" + "7c2a9f13" * 7 + "d6"


# ── 1. METHOD ─────────────────────────────────────────────────────────

def test_heal_force_is_post_only():
    """GET must not reach the ~414-fetch burst. A link prefetcher, a
    crawler or an <img src> is enough to fire a GET; none of them can
    fire a POST."""
    methods = _route(_HEAL_FORCE)["methods"]
    assert methods is not None, (
        "%s declares no methods= — Flask defaults to GET, which is exactly "
        "the lane this fence exists to close" % _HEAL_FORCE)
    assert "GET" not in [m.upper() for m in methods], (
        "%s accepts GET (%s). An anonymous GET expands PROBES into ~414 edge "
        "fetches and runs the write-side fixers in FIXES." % (_HEAL_FORCE, methods))
    assert [m.upper() for m in methods] == ["POST"], (
        "%s should be POST-only, got %s" % (_HEAL_FORCE, methods))


def test_heal_log_is_post_only_so_the_edge_cannot_serve_it_unauthenticated():
    """The gate on /heal/log only holds because the response is uncacheable.

    Cloudflare Rule #3 caches /api/v1/* with mode: override_origin, so it
    caches this endpoint regardless of the `Cache-Control: private,
    max-age=0` the origin sends, and the auth header is NOT in the cache
    key. Measured live 2026-08-08: a GET with `X-Admin-Key: <junk>` was
    served `cf-cache-status: HIT, age: 38` off the entry a previous
    unauthenticated GET had populated; the same URL as POST was DYNAMIC.

    So on GET, one authenticated admin call publishes 200 rows of log to
    every anonymous caller for the rest of the TTL and the origin gate
    never runs. Flipping this back to GET re-opens the endpoint even
    though the gate above still reads as present.
    """
    methods = _route(_HEAL_LOG)["methods"]
    assert methods is not None and [m.upper() for m in methods] == ["POST"], (
        "%s must be POST-only: it is edge-cached on GET with the auth header "
        "absent from the cache key, so an origin gate alone does not hold "
        "(got %s)" % (_HEAL_LOG, methods))


# ── 2. GATE — called AND returned ─────────────────────────────────────

def _gate_is_enforced(func):
    """True only if _heal_admin_gate()'s result is bound and then returned.

    Calling the gate and dropping the result is the vacuous version of this
    fix, and it looks identical to a grep for the function name.
    """
    bound = set()
    for node in ast.walk(func):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "_heal_admin_gate"):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    bound.add(tgt.id)
    if not bound:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        for stmt in ast.walk(node):
            if (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name)
                    and stmt.value.id in bound):
                return True
    return False


@pytest.mark.parametrize("path", [_HEAL_FORCE, _HEAL_LOG])
def test_heal_handler_enforces_the_admin_gate(path):
    assert _gate_is_enforced(_route(path)["func"]), (
        "%s does not return _heal_admin_gate()'s refusal — it is either "
        "ungated or calls the gate and ignores the result" % path)


def test_heal_status_stays_public_and_read_only():
    """The control. /heal/status is deliberately NOT gated — get_status()
    is 24 h aggregate counts with no endpoint paths and no error strings.
    Asserting it here keeps that a decision rather than an oversight, and
    proves the gate check above discriminates instead of matching anything."""
    route = _route(_HEAL_STATUS)
    assert [m.upper() for m in route["methods"]] == ["GET"]
    assert not _gate_is_enforced(route["func"]), (
        "/heal/status is now gated. That may be fine, but it is a public "
        "endpoint by decision — update this test deliberately.")


def test_gate_does_not_accept_a_query_param_credential():
    """No ?admin_key= lane: query-string credentials are written to the
    Cloudflare access log and analytics on every hit. That is how
    ADMIN_API_KEY sat exposed for months in scheduler URLs."""
    src = _MAIN.read_text(encoding="utf-8", errors="replace")
    gate_src = _func_src(ast.parse(src), src, "_heal_admin_gate")
    assert "args.get" not in gate_src, (
        "_heal_admin_gate reads a query param; admin credentials must come "
        "from the X-Admin-Key header only")


# ── 3. BEHAVIOUR — drive the real gate ────────────────────────────────

def test_gate_refuses_anonymous(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _strong_key())
    gate = _load_heal_admin_gate(None, headers={})
    result = gate()
    assert result is not None and result[1] == 401, (
        "an anonymous caller was authorised: %r" % (result,))


def test_gate_refuses_a_wrong_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _strong_key())
    gate = _load_heal_admin_gate(None, headers={"X-Admin-Key": "not-the-key"})
    result = gate()
    assert result is not None and result[1] == 401


def test_gate_accepts_the_admin_key(monkeypatch):
    """LOCKOUT lane: the real credential must still work, or this trades an
    open endpoint for an escape hatch nobody can reach."""
    key = _strong_key()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", key)
    gate = _load_heal_admin_gate(None, headers={"X-Admin-Key": key})
    assert gate() is None, "the legitimate admin key was rejected"
    # …and tolerates the trailing whitespace shells add to env vars.
    gate = _load_heal_admin_gate(None, headers={"X-Admin-Key": key + "\n"})
    assert gate() is None, "a trailing newline defeated the gate"


def test_gate_fails_closed_when_unconfigured(monkeypatch):
    """No key configured must mean 503, not 'allow everyone'. The legacy
    `if expected and provided != expected` shape short-circuits to
    fail-OPEN, which is how /admin/sitemap/purge once accepted "3"."""
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    gate = _load_heal_admin_gate(None, headers={"X-Admin-Key": "anything"})
    result = gate()
    assert result is not None and result[1] == 503, (
        "with DCHUB_ADMIN_KEY unset the gate returned %r — an unconfigured "
        "admin endpoint must refuse, not fall open" % (result,))


def test_gate_fails_closed_on_a_weak_key(monkeypatch):
    """util/admin_auth drops weak credentials, so a weakened DCHUB_ADMIN_KEY
    must not become an easy password for the probe-burst button."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "admin2026")
    gate = _load_heal_admin_gate(None, headers={"X-Admin-Key": "admin2026"})
    result = gate()
    assert result is not None and result[1] == 503


# ── 4. DRIFT — one cycle body ─────────────────────────────────────────

def test_heal_cycle_blocking_does_not_hand_roll_the_cycle():
    """The forced path must delegate, not copy.

    The copy silently lost the `__cycle__` marker, the by_fixer/by_pattern
    counters and the livelock warning that heal_cycle() grew after it was
    written. Anything added to heal_cycle() would have gone missing again.
    """
    src = (_ROOT / "dchub_self_heal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "heal_cycle_blocking")
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "PROBES"]
    assert not loops, (
        "heal_cycle_blocking iterates PROBES itself again — it is a second "
        "copy of the cycle body and will drift from heal_cycle() the way the "
        "first copy did")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "heal_cycle"]
    assert calls, "heal_cycle_blocking no longer delegates to heal_cycle()"
    assert any(kw.arg == "force" for c in calls for kw in c.keywords), (
        "heal_cycle_blocking calls heal_cycle() without force= — it would "
        "hit the interval gate and the escape hatch would stop working")


def _stub_heal(monkeypatch, age_hours):
    """Import the real module and stub only its DB/network edges."""
    import dchub_self_heal as sh

    calls = {"blocking_lock": 0, "plain_lock": 0, "events": []}

    # Never let `from main import is_current_leader` pull in main.py.
    fake_main = types.ModuleType("main")
    fake_main.is_current_leader = lambda: True
    monkeypatch.setitem(sys.modules, "main", fake_main)

    monkeypatch.setattr(sh, "_last_cycle_age_hours", lambda: age_hours)
    monkeypatch.setattr(sh, "ensure_log_table", lambda: None)
    monkeypatch.setattr(sh, "release_lock", lambda c: None)
    monkeypatch.setattr(sh, "probe", lambda path, kind: (200, ""))
    monkeypatch.setattr(sh, "detect", lambda body, status: [])

    def _blocking(max_wait_seconds=30):
        calls["blocking_lock"] += 1
        return object()

    def _plain():
        calls["plain_lock"] += 1
        return object()

    monkeypatch.setattr(sh, "acquire_lock_blocking", _blocking)
    monkeypatch.setattr(sh, "acquire_lock", _plain)
    monkeypatch.setattr(sh, "log_event",
                        lambda cid, ep, pat, fix, ok, det: calls["events"].append(
                            {"cycle_id": cid, "endpoint": ep, "details": det}))
    return sh, calls


def test_unforced_cycle_still_honours_the_interval_gate(monkeypatch):
    """The control that makes the bypass test below mean something: with a
    cycle 0.1 h old, an ordinary cycle must refuse to probe."""
    sh, calls = _stub_heal(monkeypatch, age_hours=0.1)
    result = sh.heal_cycle()
    assert result.get("reason") == "interval_gate", (
        "the interval gate did not fire on a 0.1h-old cycle: %r" % (result,))
    assert result.get("probes") is None and calls["events"] == [], (
        "the gate returned but the cycle probed anyway")


def test_forced_cycle_bypasses_the_gates_but_records_itself(monkeypatch):
    """force=True is the documented escape hatch: gates off, lock and
    budget accounting still on."""
    sh, calls = _stub_heal(monkeypatch, age_hours=0.1)
    result = sh.heal_cycle(force=True, max_wait_seconds=5)

    assert result.get("skipped") is not True, (
        "a forced cycle was skipped despite force=True: %r" % (result,))
    assert result["probes"] == len(sh.PROBES), (
        "forced cycle probed %s of %s entries" % (result["probes"], len(sh.PROBES)))
    assert result["forced"] is True
    assert result["cycle_id"].endswith("-force")

    # waits for the lock rather than skipping when another worker holds it
    assert calls["blocking_lock"] == 1 and calls["plain_lock"] == 0, (
        "forced cycle did not use the blocking lock: %r" % (calls,))

    # counts against the next interval window instead of being a silent hole
    markers = [e for e in calls["events"] if e["endpoint"] == "__cycle__"]
    assert len(markers) == 1, (
        "forced cycle wrote %d __cycle__ markers; without exactly one it is "
        "invisible to the interval gate and the probe budget leaks" % len(markers))
    assert "forced" in (markers[0]["details"] or ""), (
        "the forced marker is indistinguishable from a scheduled one")

    # Phase SS instrumentation reaches the forced path now
    assert "by_fixer" in result and "by_pattern" in result, (
        "forced cycle lost the by_fixer/by_pattern counters — the livelock "
        "warning cannot fire without them")


def test_forced_cycle_reports_lock_timeout_instead_of_pretending(monkeypatch):
    sh, calls = _stub_heal(monkeypatch, age_hours=99.0)
    monkeypatch.setattr(sh, "acquire_lock_blocking", lambda max_wait_seconds=30: None)
    result = sh.heal_cycle(force=True, max_wait_seconds=3)
    assert result.get("skipped") is True and result.get("forced") is True
    assert "lock" in result.get("reason", "")
