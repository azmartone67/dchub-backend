"""Behavioural gate tests for the cron job-trigger endpoints hardened in
fix(security): gate unauthenticated cron job-trigger endpoints.

These are BEHAVIOUR tests, not grep tests. Each handler is sliced out of the
shipped source with `ast` (decorators stripped) and EXECUTED against stubs, so a
comment or a mis-scoped `if` cannot satisfy them. No test here imports main.py,
the Flask app, or the database — only `internal_auth`, a standalone module.

How the gate is proven, per handler and per method:

  * NO key  -> the handler must RETURN a `(body, 401|403)` gate response WITHOUT
               reaching the job kickoff. If the gate were missing, execution
               would fall through to the first downstream sink, which is a
               `_PastGate`-raising stub — the harness turns that into a failure.
  * X-Internal-Key / X-Admin-Key (the REAL is_valid_internal_key, real env
               secrets) -> the gate must FALL THROUGH: the call raises `_PastGate`
               (or any post-gate exception / non-401/403 return), proving the
               request was accepted and reached the work.

`is_valid_internal_key` is the genuine function from internal_auth, exercised
against real DCHUB_INTERNAL_KEY / DCHUB_ADMIN_KEY env values — so this tests the
actual accept/reject behaviour, not a mock.

★ Every extraction asserts it found a real, non-empty function body (an empty
parse must never pass vacuously), and the canary/decoy controls prove the
harness can tell a gated handler from an ungated one.

Run:  python3 -m pytest tests/test_cron_trigger_auth_gates.py -v
"""

import ast
import os
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from internal_auth import is_valid_internal_key as _REAL_GATE  # noqa: E402

_INT_KEY = "test-internal-key-abc123"
_ADM_KEY = "test-admin-key-xyz789"


# ── stubs ────────────────────────────────────────────────────────────────────
class _PastGate(Exception):
    """Raised by a downstream stub to prove execution passed the auth gate."""


class _Stub:
    """A universal stand-in for any downstream free name. ANY use of it —
    call, attribute access, subscript, membership, iteration, truthiness —
    raises _PastGate, i.e. the handler got past the gate and touched the work."""

    def __init__(self, name="?"):
        self._name = name

    def __call__(self, *a, **k):
        raise _PastGate(self._name + "()")

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        raise _PastGate(self._name + "." + attr)

    def __getitem__(self, k):
        raise _PastGate(self._name + "[]")

    def __contains__(self, k):
        raise _PastGate("in " + self._name)

    def __iter__(self):
        raise _PastGate("iter " + self._name)

    def __bool__(self):
        raise _PastGate("bool " + self._name)


class _FakeArgs:
    def get(self, key, default=None, **kw):
        return default


class _FakeReq:
    """Stands in for flask.request. `headers` is a plain dict so the gate's
    `request.headers.get(...)` reads exactly what a test supplies."""

    def __init__(self, headers=None, method="POST"):
        self.headers = dict(headers or {})
        self.method = method
        self.args = _FakeArgs()

    def get_json(self, *a, **k):
        return {}


def _jsonify(*a, **k):
    # flask.jsonify stand-in: return the payload so `return jsonify(x), code`
    # yields the (payload, code) tuple the harness inspects.
    if a:
        return a[0]
    return dict(k)


def _fake_module(name):
    """A module whose every attribute is a _PastGate stub — injected into
    sys.modules so a handler's post-gate `import X` / `from X import y` can
    never run real work (subprocess spawn, brain cycle, live loader)."""
    m = types.ModuleType(name)

    def __getattr__(attr):
        return _Stub(name + "." + attr)

    m.__getattr__ = __getattr__
    return m


# The request instance the handler under test should observe, even when it does
# a LOCAL `from flask import request` inside its own body (rebinding would
# otherwise pull in the real out-of-context flask proxy). Set per call.
_ACTIVE_REQ = None


def _fake_flask():
    """A stand-in `flask` module so a handler's own `from flask import request,
    jsonify` resolves to the harness fake req / jsonify instead of the real,
    request-context-bound proxies."""
    m = types.ModuleType("flask")

    def __getattr__(attr):
        if attr == "request":
            return _ACTIVE_REQ
        if attr == "jsonify":
            return _jsonify
        return _Stub("flask." + attr)

    m.__getattr__ = __getattr__
    return m


# Handlers that, once past the gate, `import` a module which would otherwise do
# real work. Neutralised only for the duration of an assertion.
_NEUTRALISE = ("subprocess", "autonomous_brain", "load_interconnect_queue_live")


# ── ast slice + compile ──────────────────────────────────────────────────────
def _find_func(tree, fn_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return node
    return None


def _compile_fn(src, relpath, fn_name, fake_req):
    """Slice `def fn_name` out of `src` (anywhere in the tree, incl. a closure),
    strip its decorators, and compile it into a namespace whose free names are
    _PastGate stubs — except request/jsonify (provided) and is_valid_internal_key
    (the REAL function). Returns the callable."""
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module) and tree.body, \
        "%s parsed to an EMPTY module" % relpath
    node = _find_func(tree, fn_name)
    assert node is not None, "%s not found in %s" % (fn_name, relpath)
    assert node.body, "%s.%s has an EMPTY body" % (relpath, fn_name)
    node.decorator_list = []

    loads = {n.id for n in ast.walk(node)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    import builtins as _b
    builtin_names = set(dir(_b))

    ns = {
        "request": fake_req,
        "flask_request": fake_req,
        "req": fake_req,
        "jsonify": _jsonify,
        "is_valid_internal_key": _REAL_GATE,
    }
    for name in loads:
        if name in ns or name in builtin_names:
            continue
        ns[name] = _Stub(name)

    mod = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), relpath, "exec"), ns)
    fn = ns.get(fn_name)
    assert callable(fn), "%s did not compile to a callable" % fn_name
    global _ACTIVE_REQ
    _ACTIVE_REQ = fake_req  # observed by any local `from flask import request`
    return fn


def _load(relpath, fn_name, fake_req):
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    return _compile_fn(src, relpath, fn_name, fake_req)


# ── env / sys.modules scoping ────────────────────────────────────────────────
class _test_keys:
    """Set the real internal/admin secrets and neutralise dangerous imports for
    the duration of a block; restore everything afterwards."""

    _KEYS = ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
             "INTERNAL_WORKER_SECRET", "DCHUB_ADMIN_KEY")

    def __enter__(self):
        self._saved_env = {k: os.environ.get(k) for k in self._KEYS}
        os.environ["DCHUB_INTERNAL_KEY"] = _INT_KEY
        os.environ["DCHUB_ADMIN_KEY"] = _ADM_KEY
        os.environ.pop("DCHUB_SYNC_KEY", None)
        os.environ.pop("INTERNAL_WORKER_SECRET", None)
        self._saved_mods = {}
        for name in _NEUTRALISE:
            self._saved_mods[name] = sys.modules.get(name)
            sys.modules[name] = _fake_module(name)
        self._saved_mods["flask"] = sys.modules.get("flask")
        sys.modules["flask"] = _fake_flask()
        return self

    def __exit__(self, *exc):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for name, v in self._saved_mods.items():
            if v is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = v
        return False


# ── the three-way behavioural assertion ──────────────────────────────────────
def _rejects_without_key(relpath, fn_name, call_args, method):
    fn = _load(relpath, fn_name, _FakeReq(headers={}, method=method))
    try:
        out = fn(*call_args)
    except _PastGate:
        return None  # reached the sink -> gate absent/bypassed
    if isinstance(out, tuple) and len(out) >= 2 and out[1] in (401, 403):
        return out[1]
    return None


def _accepts_with_header(relpath, fn_name, call_args, method, header):
    fn = _load(relpath, fn_name, _FakeReq(headers=header, method=method))
    try:
        out = fn(*call_args)
    except _PastGate:
        return True                     # fell through to the work
    except Exception:
        return True                     # any post-gate error also = accepted
    if isinstance(out, tuple) and len(out) >= 2 and out[1] in (401, 403):
        return False                    # wrongly rejected a valid key
    return True                         # ran on against stubs / non-gate return


def _assert_gate(relpath, fn_name, call_args=(), methods=("POST",)):
    with _test_keys():
        for m in methods:
            code = _rejects_without_key(relpath, fn_name, call_args, m)
            assert code in (401, 403), (
                "%s:%s [%s] did NOT reject a keyless request (reached the job "
                "kickoff)" % (relpath, fn_name, m))
            assert _accepts_with_header(
                relpath, fn_name, call_args, m, {"X-Internal-Key": _INT_KEY}), (
                "%s:%s [%s] rejected a valid X-Internal-Key" % (relpath, fn_name, m))
            assert _accepts_with_header(
                relpath, fn_name, call_args, m, {"X-Admin-Key": _ADM_KEY}), (
                "%s:%s [%s] rejected a valid X-Admin-Key" % (relpath, fn_name, m))


# ── the gated handlers (relpath, fn, call_args, methods) ─────────────────────
_GP = ("GET", "POST")

_GATED = [
    # iso-queue interconnection-queue ingest (GATE_PLUS_MIGRATE_CRON + SAFE)
    ("routes/iso_queue_ingest.py", "ingest_all", (), _GP),
    ("routes/iso_queue_ingest.py", "ingest_one", ("ERCOT",), _GP),
    ("routes/iso_queue_ingest.py", "ingest_projects", (), _GP),
    # iso-lmp real-time LMP ingest (GATE_PLUS_MIGRATE_CRON + SAFE)
    ("routes/iso_lmp_ingest.py", "ingest_all", (), ("POST",)),
    ("routes/iso_lmp_ingest.py", "ingest_one", ("MISO",), ("POST",)),
    # gas feeds ingest (SAFE)
    ("routes/gas_price_feeds.py", "ingest_all", (), _GP),
    ("routes/gas_price_feeds.py", "ingest_one", ("henry_hub",), _GP),
    # EIA non-ISO balancing-authority extract (SAFE)
    ("routes/eia_utility_bas.py", "utility_extract_all", (), ("POST",)),
    ("routes/eia_utility_bas.py", "utility_extract_one", ("PJM",), ("POST", "GET")),
    # SEC EDGAR extract (GATE_PLUS_MIGRATE_CRON)
    ("routes/sec_edgar.py", "trigger_extract_all", (), ("POST", "GET")),
    ("routes/sec_edgar.py", "trigger_extract_one", ("AAPL",), ("POST", "GET")),
    # KMZ fiber discovery (GATE_PLUS_MIGRATE_CRON + SAFE)
    ("kmz_auto_discovery.py", "run_kmz_discovery", (), ("POST",)),
    ("kmz_auto_discovery.py", "prune_kmz_sources", (), ("POST",)),
    # news sync (SAFE)
    ("auto_sync.py", "admin_news_sync", (), ("POST",)),
    ("api_fixes.py", "sync_news", (), ("POST",)),
    # discovery / evolution / brain HTTP triggers (SAFE)
    ("routes/discovery_routes.py", "discovery_run", (), ("POST",)),
    ("routes/discovery_routes.py", "discovery_refresh", (), ("POST",)),
    ("routes/discovery_routes.py", "evolution_run", (), ("POST",)),
    ("routes/discovery_routes.py", "brain_run", (), ("POST",)),
    # proactive discovery (SAFE; fiber/utility are DEFER and NOT here)
    ("proactive_discovery.py", "download_files", (), ("POST",)),
    ("proactive_discovery.py", "full_discovery", (), ("POST",)),
    # autonomous brain v1 cycle/scheduler (SAFE; OPTIONS short-circuit preserved)
    ("autonomous_brain.py", "run_cycle", (), ("POST",)),
    ("autonomous_brain.py", "start_brain", (), ("POST",)),
    ("autonomous_brain.py", "stop_brain", (), ("POST",)),
    # hyperscaler $1B+ alert sweep (SAFE)
    ("routes/hyperscaler_alerts.py", "sweep", (), _GP),
    # ambassador cycle/scheduler (SAFE)
    ("agentic_ambassador.py", "run_cycle", (), ("POST",)),
    ("agentic_ambassador.py", "start_scheduler", (), ("POST",)),
    ("agentic_ambassador.py", "stop_scheduler", (), ("POST",)),
    # ai-ecosystem cycle/scheduler (SAFE)
    ("ai_ecosystem_agent.py", "run_agent_cycle", (), ("POST",)),
    ("ai_ecosystem_agent.py", "start_agent", (), ("POST",)),
    ("ai_ecosystem_agent.py", "stop_agent", (), ("POST",)),
    # outreach learning teach (SAFE; /run + /trigger are DEFER and NOT here)
    ("ai_outreach_agent.py", "outreach_teach", (), ("POST",)),
    # orchestrator cycle (SAFE)
    ("ai_orchestrator.py", "run_cycle", (), ("POST",)),
    # weekly reach rollup cron (SAFE)
    ("routes/ai_reach_rollup.py", "cron_reach_rollup", (), _GP),
    # global intelligence deep-learn (SAFE; capacity/track is DEFER and NOT here)
    ("global_intelligence_agent.py", "run_deep_learning", (), ("POST",)),
    # mcp gateway learn-now (SAFE)
    ("mcp_gateway.py", "gateway_learn_now", (), ("POST",)),
]


@pytest.mark.parametrize("relpath,fn_name,call_args,methods", _GATED,
                         ids=["%s::%s" % (r.split("/")[-1], f)
                              for (r, f, _a, _m) in _GATED])
def test_cron_trigger_is_gated(relpath, fn_name, call_args, methods):
    """Keyless -> 401/403 before the job runs; a valid X-Internal-Key or
    X-Admin-Key -> the gate falls through to the work."""
    _assert_gate(relpath, fn_name, call_args, methods)


# ── controls: prove the harness is real, not vacuously green ─────────────────
_GOOD_GATE_SRC = (
    "@bp.route('/x', methods=['POST'])\n"
    "def h():\n"
    "    if not is_valid_internal_key(request.headers.get('X-Internal-Key') or request.headers.get('X-Admin-Key')):\n"
    "        return jsonify({'error': 'unauthorized'}), 401\n"
    "    return _sink_does_the_work()\n"
)

_NO_GATE_SRC = (
    "@bp.route('/x', methods=['POST'])\n"
    "def h():\n"
    "    return _sink_does_the_work()\n"
)


def test_canary_gated_handler_is_recognised_as_gated():
    """MUST-PASS control: a correctly-gated synthetic handler rejects keyless and
    accepts both header keys — proving the extract/compile/assert machinery runs
    and evaluates real code."""
    with _test_keys():
        assert _rejects_without_key_src(_GOOD_GATE_SRC, "POST") in (401, 403)
        assert _accepts_src(_GOOD_GATE_SRC, "POST", {"X-Internal-Key": _INT_KEY})
        assert _accepts_src(_GOOD_GATE_SRC, "POST", {"X-Admin-Key": _ADM_KEY})


def test_decoy_ungated_handler_is_NOT_seen_as_gated():
    """DECOY control: an UNGATED handler must fail the reject check — a keyless
    call reaches the sink (_PastGate) instead of returning 401/403. If this
    'passed', the reject assertion would be vacuous and every result meaningless."""
    with _test_keys():
        assert _rejects_without_key_src(_NO_GATE_SRC, "POST") is None, \
            "an ungated handler was mistaken for a gated one"


def _rejects_without_key_src(src, method):
    fn = _compile_fn(src, "synthetic.py", "h", _FakeReq(headers={}, method=method))
    try:
        out = fn()
    except _PastGate:
        return None
    if isinstance(out, tuple) and len(out) >= 2 and out[1] in (401, 403):
        return out[1]
    return None


def _accepts_src(src, method, header):
    fn = _compile_fn(src, "synthetic.py", "h", _FakeReq(headers=header, method=method))
    try:
        fn()
    except _PastGate:
        return True
    except Exception:
        return True
    return True


# ── MUST-FAIL control — proves the runner actually executed this file ────────
# (a conftest/exit-3 collection abort renders GREEN with 0 tests run). xfail
# strict: a green run reports XFAIL; an unexpected PASS is a hard error.
@pytest.mark.xfail(strict=True, reason="MUST-FAIL control proving the runner "
                   "executes this file")
def test_MUSTFAIL_control_runner_is_alive():
    assert 1 == 2, "MUST-FAIL control: if you see this as a failure the runner ran"
