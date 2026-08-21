"""get_pool_health() must divide MAIN-pool checkouts by the MAIN pool's ceiling.

Before this guard, `checked_out` was `len(_active_checkouts)` — and
`_active_checkouts` holds checkouts from BOTH pools, because
`get_read_connection` (main.py) and `get_read_db` (main.py) call
`_track_checkout` on the READ-replica success path. The denominator,
`DB_POOL_MAX` (live: 80), is the MAIN pool's ceiling only; the read replica is a
separate ThreadedConnectionPool(maxconn=30). So the ratio was a TWO-pool
numerator over a ONE-pool denominator, and a busy read replica inflated
`pool.utilization_pct` by up to 37.5 points.

That is not a cosmetic metric. Past 90% `pool_status` becomes 'critical', and:

  - /api/health/db returns 503 (main.py db_health_endpoint), and
  - /api/health — Railway's `healthcheckPath` (railway.json), with
    `restartPolicyType: ALWAYS` — returns 503, marks the app degraded, and
    SKIPS its DB counts entirely.

i.e. read-replica load could health-gate the app while the main pool had
headroom the whole time. `read_replica.used` never corrected this: it is
measured independently from `len(_pg_pool_read._used)`.

Tests never import main (it opens pools and connects at module scope). The real
function bodies are ast-extracted and executed against stubs, per
tests/test_queue_delta_capture.py.

pytest functions only; nothing runs at module scope.
"""
import ast
import pathlib
import threading

SRC = pathlib.Path(__file__).resolve().parent.parent / "main.py"

_WANT_FN = ("_track_checkout", "_track_return", "get_pool_health")

# The live Railway value (DB_POOL_MAX=80). Pinned so the percentages below are
# the ones production actually computes, not the 50 default.
_LIVE_POOL_MAX = "80"


class _FakeReadPool:
    """Stands in for the psycopg2 ThreadedConnectionPool(maxconn=30) replica."""
    def __init__(self):
        self._used = {}


def _load(monkeypatch):
    """ast-extract the pool-accounting functions and exec them against stubs.

    Asserts the parse actually produced every function AND that each free name
    they reach for exists in the namespace — an empty extraction passes every
    assertion below while testing nothing.
    """
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert tree.body, "ast.parse produced an empty module — nothing was tested"

    monkeypatch.setenv("DB_POOL_MAX", _LIVE_POOL_MAX)

    ns = {
        "_active_checkouts": {},
        "_checkout_lock": threading.Lock(),
        "_CONN_MAX_HOLD_SECONDS": 60,
        "_HEALTH_MEMORY_THRESHOLD_MB": 3072,
        "_pg_pool_obj": object(),          # truthy: pool IS initialized
        "_pg_pool_read": _FakeReadPool(),
        "_circuit_breaker": {"open": False, "failures": 0, "threshold": 5,
                             "recovery_timeout": 30, "last_failure": 0.0},
        "_pool_stats": {"acquired": 0, "returned": 0, "timeouts": 0,
                        "circuit_trips": 0, "forced_reclaims": 0},
    }
    exec("import os, time, resource, logging, threading", ns)

    got = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FN:
            exec(compile(ast.get_source_segment(src, node), str(SRC), "exec"), ns)
            got.add(node.name)
    assert got == set(_WANT_FN), "missing from main.py: {}".format(
        sorted(set(_WANT_FN) - got))

    # Every module-level free name these bodies load must resolve in `ns`.
    # A missing one is a NameError at runtime or, worse, a silently untested path.
    import builtins
    for name in _WANT_FN:
        fn = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name][0]
        local = {a.arg for a in fn.args.args} | {
            n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        local |= {a.name.split(".")[0] if a.asname is None else a.asname
                  for n in ast.walk(fn) if isinstance(n, ast.Import)
                  for a in n.names}
        local |= {a.asname or a.name for n in ast.walk(fn)
                  if isinstance(n, ast.ImportFrom) for a in n.names}
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id in local or hasattr(builtins, n.id):
                    continue
                assert n.id in ns, "{} references {} — not in the test namespace".format(
                    name, n.id)
    return ns


def _conns(n):
    """n distinct objects, KEPT ALIVE by the caller — _active_checkouts is keyed
    by id(), and CPython reuses the id of a collected object."""
    return [object() for _ in range(n)]


def test_extraction_is_not_vacuous(monkeypatch):
    """MUST-FAIL control: if tracking a main checkout does not move
    pool.checked_out, every assertion below is testing nothing."""
    ns = _load(monkeypatch)
    keep = _conns(1)
    ns["_track_checkout"](keep[0], pool="main")
    assert ns["get_pool_health"]()["pool"]["checked_out"] == 1


def test_read_checkout_does_not_move_the_main_pool_number(monkeypatch):
    """THE defect: a READ-replica checkout must not count against DB_POOL_MAX."""
    ns = _load(monkeypatch)
    keep = _conns(1)
    ns["_track_checkout"](keep[0], pool="read")

    health = ns["get_pool_health"]()
    assert health["pool"]["checked_out"] == 0, (
        "read-replica checkout counted against the MAIN pool: " + repr(health["pool"]))
    assert health["pool"]["utilization_pct"] == 0
    assert health["pool"]["estimated_available"] == 80
    # ...and it is not simply dropped on the floor — it is counted as READ.
    assert health["read_replica"]["used_tracked"] == 1
    assert health["active_checkouts"] == 1


def test_busy_replica_does_not_fake_a_critical_main_pool(monkeypatch):
    """The production scenario, at the live DB_POOL_MAX=80.

    43 main + a FULL read replica (30) = 73 tracked checkouts. The old
    two-pool numerator read 73/80 = 91.2% → 'critical' → /api/health 503 →
    degraded + DB work skipped, with the main pool at 54%.
    """
    ns = _load(monkeypatch)
    keep_main, keep_read = _conns(43), _conns(30)
    for c in keep_main:
        ns["_track_checkout"](c, pool="main")
    for c in keep_read:
        ns["_track_checkout"](c, pool="read")

    assert round(73 / 80 * 100, 1) > 90, "premise: the OLD numerator was critical"

    health = ns["get_pool_health"]()
    pool = health["pool"]
    assert pool["checked_out"] == 43, repr(pool)
    assert pool["utilization_pct"] == 53.8, repr(pool)
    assert pool["estimated_available"] == 37
    assert pool["status"] == "healthy", (
        "a busy read replica self-degraded the app: " + repr(pool))
    assert health["read_replica"]["used_tracked"] == 30
    assert health["active_checkouts"] == 73


def test_main_pool_still_reaches_critical_on_its_own(monkeypatch):
    """The fix must not make 'critical' unreachable — that would trade a false
    alarm for a blind spot. 73 MAIN checkouts is genuinely 91.2%."""
    ns = _load(monkeypatch)
    keep = _conns(73)
    for c in keep:
        ns["_track_checkout"](c, pool="main")

    pool = ns["get_pool_health"]()["pool"]
    assert pool["checked_out"] == 73
    assert pool["utilization_pct"] == 91.2
    assert pool["status"] == "critical", repr(pool)


def test_untagged_call_site_counts_as_main(monkeypatch):
    """_track_checkout's SIGNATURE default stays 'main': a caller that forgets
    the kwarg must land in the number it belongs in, not vanish from it."""
    ns = _load(monkeypatch)
    keep = _conns(1)
    ns["_track_checkout"](keep[0])
    health = ns["get_pool_health"]()
    assert health["pool"]["checked_out"] == 1
    assert health["read_replica"]["used_tracked"] == 0


def test_entry_with_no_pool_key_counts_as_main(monkeypatch):
    """get_pool_health's `.get('pool', 'main')` fallback is a SECOND default,
    distinct from _track_checkout's signature default — _track_checkout always
    writes the key, so nothing above reaches this branch.

    It must fail toward 'main'. Reading an untagged entry as 'read' is the
    dangerous direction: a real main-pool checkout would drop out of
    utilization_pct and a saturated pool would report healthy.
    """
    ns = _load(monkeypatch)
    keep = _conns(1)
    ns["_active_checkouts"][id(keep[0])] = {
        "checked_out_at": __import__("time").time(),
        "thread": "raw-entry",
        "endpoint": None,
        "stack": "",
    }
    health = ns["get_pool_health"]()
    assert health["pool"]["checked_out"] == 1, (
        "an entry with no pool key vanished from the MAIN numerator: "
        + repr(health["pool"]))
    assert health["read_replica"]["used_tracked"] == 0
    assert health["active_checkouts"] == 1


def test_track_return_pops_both_pools_and_never_raises(monkeypatch):
    """_track_return runs on the connection-return path BEFORE putconn. If it
    raises, the connection leaks. It pops by id, pool-agnostically."""
    ns = _load(monkeypatch)
    keep_main, keep_read = _conns(1), _conns(1)
    ns["_track_checkout"](keep_main[0], pool="main")
    ns["_track_checkout"](keep_read[0], pool="read")
    assert ns["get_pool_health"]()["active_checkouts"] == 2

    ns["_track_return"](keep_read[0])
    health = ns["get_pool_health"]()
    assert health["pool"]["checked_out"] == 1
    assert health["read_replica"]["used_tracked"] == 0

    ns["_track_return"](keep_main[0])
    assert ns["get_pool_health"]()["active_checkouts"] == 0

    # never raises: None, and an object that was never checked out
    ns["_track_return"](None)
    ns["_track_return"](object())
    assert ns["get_pool_health"]()["active_checkouts"] == 0


def test_every_track_checkout_call_site_names_its_pool():
    """The behaviour tests above cover the function; this covers the CALLERS.

    A new read-path call site that omits pool= defaults to 'main' and silently
    reintroduces the conflation, with every test above still green.
    """
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # function name -> the pool its _track_checkout calls must declare
    expected = {
        "try_get_pg_connection": "main",
        "get_pg_connection": "main",
        "get_read_connection": "read",
        "get_read_db": "read",
    }
    seen = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_track_checkout"):
                continue
            kw = {k.arg: k.value for k in call.keywords}
            assert "pool" in kw, (
                "main.py:{} in {}() calls _track_checkout without pool= — it "
                "will be counted against the MAIN pool".format(call.lineno, fn.name))
            assert isinstance(kw["pool"], ast.Constant), (
                "main.py:{} passes a non-literal pool=".format(call.lineno))
            seen.setdefault(fn.name, set()).add(kw["pool"].value)

    assert seen, "found no _track_checkout call sites — this guard is vacuous"
    for fn_name, want in expected.items():
        assert seen.get(fn_name) == {want}, (
            "{}() should check out from the {!r} pool, got {}".format(
                fn_name, want, sorted(seen.get(fn_name) or [])))
    assert set(seen) == set(expected), (
        "new _track_checkout call site(s) not pinned by this guard: {}".format(
            sorted(set(seen) - set(expected))))
