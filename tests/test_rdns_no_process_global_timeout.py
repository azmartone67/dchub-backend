"""The /top-users reverse-DNS enrichment must not set the process-global
socket default timeout.

routes/observability_routes.py ran this inside a ThreadPoolExecutor worker:

    def _lookup(ip):
        try:
            socket.setdefaulttimeout(1.5)      # <- process-global, never undone
            hostname = socket.gethostbyaddr(ip)[0]

socket.setdefaulttimeout() is not per-socket and not per-thread. It is the
default for every socket the PROCESS creates afterwards. One admin GET of
/api/v1/observability/top-users?group_by=ip&reverse_dns=1 therefore left
psycopg2 connections, every `requests` call written without an explicit
timeout=, and the brain's and the watchdog's I/O on a 1.5s default for the
rest of that process's life. The worker runs the brain, the watchdog and the
scheduler in sibling threads, so the blast radius was the whole process.

That global did not even bound the lookup it was written for.
setdefaulttimeout() applies when a socket OBJECT is created; gethostbyaddr()
goes to the platform resolver and never consults it. Measured: under
socket.setdefaulttimeout(0.001), gethostbyaddr('1.1.1.1') SUCCEEDED in 0.022s.
So the old line bounded nothing and leaked everything.

gethostbyaddr() takes no timeout argument either, so the bound moved off the
socket and onto the batch WAIT (concurrent.futures.wait(timeout=...)), and
slow lookups are abandoned instead of being bounded globally.

★ The sentinel assertion below is deliberately stricter than "did not leave it
  at 1.5". It fails for the *restore-it-in-a-finally* variant too — see
  routes/email_validation.py:258 — because that pattern clobbers whatever
  default another thread had set, and races any socket created in the window.
"""
import ast
import io
import pathlib
import socket
import threading
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "routes" / "observability_routes.py"

_HELPER = "_reverse_dns_map"
_CALLER = "phase60_top_users"


def _tree():
    return ast.parse(io.open(SRC, encoding="utf-8").read())


@pytest.fixture(scope="module")
def tree():
    assert SRC.is_file(), f"{SRC} is gone — retarget this guard"
    return _tree()


def _toplevel(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name}() is not a module-level function in {SRC.name}. This guard "
        f"cannot pass vacuously — if it was renamed or re-nested, retarget it "
        f"here rather than deleting the check."
    )


def _load_helper(tree):
    """exec the real _reverse_dns_map (plus its module-level constants) into a
    fresh namespace.

    Extraction, not import: importing routes.observability_routes would pull in
    Flask, register a Blueprint and drag in the module's SWR cache globals. The
    helper does all of its own imports inside its body, so the only module
    state it needs is the _RDNS_* constants used as parameter defaults.
    """
    wanted = [_toplevel(tree, _HELPER)]
    consts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("_RDNS_") for t in node.targets
        ):
            consts.append(node)
    assert consts, (
        "no _RDNS_* module constants found — the helper's parameter defaults "
        "come from them, so this extraction is out of date"
    )
    mod = ast.Module(body=consts + wanted, type_ignores=[])
    ns = {}
    exec(compile(ast.fix_missing_locations(mod), str(SRC), "exec"), ns)
    fn = ns.get(_HELPER)
    assert callable(fn), f"{_HELPER} did not exec into a callable"
    return fn, ns


# ── the tripwire ─────────────────────────────────────────────────────────────

def test_the_extraction_actually_loaded_the_real_helper(tree):
    """★ Against a vacuous pass. Every behavioural check below runs the exec'd
    function; if extraction silently produced a stub they would all agree with
    each other and prove nothing."""
    fn, ns = _load_helper(tree)
    assert fn.__name__ == _HELPER
    budget = ns.get("_RDNS_BATCH_BUDGET_S")
    assert isinstance(budget, (int, float)) and 0 < budget <= 30, (
        f"_RDNS_BATCH_BUDGET_S={budget!r} is not a sane wall-clock budget"
    )
    # The helper must really consult the resolver, or the timeout test below
    # would pass because nothing ever blocks.
    src = ast.dump(_toplevel(tree, _HELPER))
    assert "gethostbyaddr" in src, (
        "the helper no longer calls gethostbyaddr — retarget this guard"
    )


# ── the regression itself ────────────────────────────────────────────────────

def test_observability_routes_never_calls_setdefaulttimeout(tree):
    """The literal bug. AST, not grep: the prose above names the call on
    purpose and a text scan would match its own explanation."""
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == "setdefaulttimeout":
            hits.append(n.lineno)
    assert not hits, (
        f"{SRC.name} calls socket.setdefaulttimeout() at line(s) {hits}. It is "
        f"PROCESS-GLOBAL and this process also runs the brain, the watchdog "
        f"and the scheduler. Bound the wait (concurrent.futures.wait) or pass "
        f"an explicit per-call timeout instead."
    )


def test_the_route_actually_uses_the_bounded_helper(tree):
    """A bounded helper nobody calls is a dead scoreboard. The enrichment path
    inside the handler must go through it."""
    caller = _toplevel(tree, _CALLER)
    called = {
        n.func.id for n in ast.walk(caller)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert _HELPER in called, (
        f"{_CALLER}() no longer calls {_HELPER}() — the reverse-DNS enrichment "
        f"has been re-inlined and is unbounded again"
    )


# ── behaviour: the global survives a batch that times out ────────────────────

def test_batch_leaves_the_process_global_default_untouched(tree):
    """Runs the real helper against a resolver that hangs.

    The sentinel (12.5) is a value no production code would choose, so this
    fails loudly for BOTH regressions: leaving it at 1.5, and 'restoring' it to
    None in a finally the way routes/email_validation.py does.
    """
    fn, _ = _load_helper(tree)

    fast = ["10.0.0.1", "10.0.0.2"]
    slow = ["10.0.0.3", "10.0.0.4"]
    release = threading.Event()

    def fake_gethostbyaddr(ip):
        if ip in slow:
            # Bounded so a regression fails in seconds instead of hanging the
            # suite; released immediately on the good path.
            release.wait(timeout=3.0)
            raise OSError("released")
        return (f"host-{ip.replace('.', '-')}.example.net", [], [ip])

    real = socket.gethostbyaddr
    prior = socket.getdefaulttimeout()
    socket.setdefaulttimeout(12.5)
    try:
        socket.gethostbyaddr = fake_gethostbyaddr
        t0 = time.monotonic()
        hostmap = fn(fast + slow, budget_s=0.5)
        elapsed = time.monotonic() - t0

        assert socket.getdefaulttimeout() == 12.5, (
            f"the reverse-DNS batch changed the PROCESS-GLOBAL socket default "
            f"to {socket.getdefaulttimeout()!r}. Every socket this process "
            f"opens afterwards inherits it — psycopg2, requests calls with no "
            f"explicit timeout=, the brain and the watchdog."
        )
        assert elapsed < 2.0, (
            f"the batch took {elapsed:.2f}s against a 0.5s budget — slow "
            f"lookups are being waited on, not abandoned (a "
            f"`with ThreadPoolExecutor(...)` block joins every worker on exit)"
        )
        for ip in fast:
            assert hostmap.get(ip), f"{ip} answered instantly and was still dropped"
        for ip in slow:
            assert ip not in hostmap, (
                f"{ip} never answered but appears in the map"
            )
    finally:
        release.set()
        socket.gethostbyaddr = real
        socket.setdefaulttimeout(prior)


def test_empty_and_falsy_input_does_not_spawn_a_pool(tree):
    fn, _ = _load_helper(tree)
    assert fn([]) == {}
    assert fn([None, "", None]) == {}
