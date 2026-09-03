"""tests/test_wellknown_manifest_version_derived.py — the manifest must DERIVE, not transcribe (2026-08-16).

/.well-known/mcp.json is scraped by every MCP registry. While it is stale, every
downstream listing is stale and the registries are not at fault.

Three copies of the same facts existed, and all three had rotted apart:

    live MCP `initialize` serverInfo   2.12.0   <- the SoT
    CF zone worker MCP_SERVER_INFO      2.5.0
    Flask origin manifest (main.py)     2.3.3   <- what the worker FETCHES

★ "2.3.3" had ALREADY been on ai_surface_canon's stale_markers denylist while
the origin was serving it. That is the lesson this file locks in: **a denylist
makes a stale surface DETECTABLE, it cannot make it correct.** Only derivation
can. So main.py now reads the canon instead of carrying its own literal, and the
worker takes version/description from the origin instead of its own constants.

★ The counts had rotted the same way — a description baked with "15,700+
facilities / 1,600+ deals" against a canon of 18,000+ / 1,800+.

Ways this regresses, each asserted below:
  (1) SELF-CONTRADICTORY CANON — the live version appears on its own
      stale_markers denylist, so the sentinel flags every honest surface.
  (2) OUTGOING VERSION NOT RETIRED — bumping without retiring the previous
      value leaves a surface still serving it invisible to the sentinel.
  (3) RE-HARDCODED ORIGIN — main.py goes back to a literal version.
  (4) WORKER STOPS DERIVING — the string keys drop out of the whitelist, or the
      extras spread stops being last so the literals silently win again.

House rule: no DB, no network, and NEVER import main — the helper is pulled out
with `ast` and executed against a stub.

Run:  python3 -m pytest tests/test_wellknown_manifest_version_derived.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"
_WORKER = _ROOT / "worker.js"


def _func_src(path: pathlib.Path, name: str) -> str:
    """Slice `def name(` out by ast — parsed, not regex-guessed.

    ★ A silently-empty extraction passes every downstream assertion, so this
    asserts the parse really found a FunctionDef with a real body.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            assert node.body, f"{name} parsed with an EMPTY body — extraction failed"
            return ast.get_source_segment(path.read_text(), node)
    raise AssertionError(f"{name} not found in {path.name}")


# ── Quiesce the version cache for EVERY test in this file (2026-09-02) ─────
#
# ★ WHY THIS EXISTS — A REAL, REPRODUCED RACE, NOT A PRECAUTION.
# Most tests here seed _server_version_cache with the sentinel "99.9.9" and
# assert a surface renders it. That seed is only honoured while no BACKGROUND
# REFRESH is in flight: resolve_server_version_cached() spawns a daemon thread
# that probes the LIVE `initialize` handshake and writes the result into the
# same dict. A thread started by an EARLIER test can land AFTER a later test
# seeds, overwriting 99.9.9 with the live version.
#
# Reproduced 2026-09-02, and it is order-dependent, which is the worst kind:
#     pytest tests/test_agents_md_live_floors.py \
#            tests/test_wellknown_manifest_version_derived.py
#   -> test_mcp_server_card_route_serves_the_resolved_version
#      assert '2.12.3' == '99.9.9'
# test_agents_md_live_floors.py renders /AGENTS.md, /AGENTS.md now resolves its
# version through the accessor, the accessor starts a probe, and the probe's
# write lands in the middle of a LATER test in this file. Reversing the two
# files hides it again. CI runs `pytest tests/` in one process, so this is
# exactly the shape that shows up as an unreproducible red once a week.
#
# So: join any in-flight refresh, then latch the "already refreshing" flag so no
# test in this file can start a new one. Also makes this file honestly offline —
# the house rule at the top says no network, and without this the rule was true
# only by luck of ordering.
@pytest.fixture(autouse=True)
def _quiesce_server_version_cache():
    import threading

    import ai_surface_canon as _c

    for _th in threading.enumerate():
        if _th.name == "server-version-refresh" and _th.is_alive():
            _th.join(timeout=30)

    with _c._server_version_lock:
        saved_cache = dict(_c._server_version_cache)
        saved_flag = _c._server_version_refreshing
        _c.__dict__["_server_version_refreshing"] = True
    try:
        yield
    finally:
        with _c._server_version_lock:
            _c._server_version_cache.clear()
            _c._server_version_cache.update(saved_cache)
            _c.__dict__["_server_version_refreshing"] = saved_flag


# ── (1)+(2) the canon must not contradict itself ───────────────────────────

def test_canon_version_is_not_on_its_own_denylist():
    """THE PIN: a canon that lists its own current version as stale turns every
    honest surface into a high-severity false positive (and hides the real one)."""
    from ai_surface_canon import PINNED
    assert PINNED["version"] not in PINNED["stale_markers"], (
        f'canon version {PINNED["version"]} is also in stale_markers — '
        "ai_surface_sentinel would flag every correct surface"
    )


def test_previous_versions_are_retired():
    """Retiring the OUTGOING value is what makes a surface still serving it
    detectable. 2.3.3 is the proof case — it was retired AND still served."""
    from ai_surface_canon import PINNED
    for old in ("2.3.3", "2.5.0", "2.11.1", "2.12.0"):
        assert old in PINNED["stale_markers"], f"{old} must stay retired"


def test_canon_version_is_a_plain_semver():
    from ai_surface_canon import PINNED
    assert re.fullmatch(r"\d+\.\d+\.\d+", PINNED["version"]), PINNED["version"]


# ── (3) the origin manifest must derive ────────────────────────────────────

def test_origin_manifest_version_is_canon_derived():
    """_wk_canon_version() must read the RESOLVER, not a baked string and not
    the raw pin.

    ★ This assertion used to be `== PINNED["version"]`, which passed only while
    the pin and the live server happened to agree. Once the manifest self-heals
    (2026-08-30) that equality becomes a LATENT FLAKE: the first time the server
    moves ahead of the pin — the exact event this wiring exists to handle — it
    would red every open branch with no commit responsible, which is the trap
    be#3361 had to undo a day earlier.

    So: seed the cache with a value the pin does not have. A pin-reader returns
    PINNED; a resolver-reader returns the seeded value. Seeding a FRESH entry
    also keeps this offline — resolve_server_version_cached() only starts a
    background refresh when its value is stale.
    """
    import time

    import ai_surface_canon as canon
    src = _func_src(_MAIN, "_wk_canon_version")
    ns: dict = {}
    exec(compile(src, "<_wk_canon_version>", "exec"), ns)
    sentinel = "99.9.9"
    assert sentinel != canon.PINNED["version"], "sentinel must differ from the pin"
    saved = dict(canon._server_version_cache)
    try:
        with canon._server_version_lock:
            canon._server_version_cache["val"] = sentinel
            canon._server_version_cache["at"] = time.time()
        got = ns["_wk_canon_version"]()
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)
    assert got == sentinel, (
        f"_wk_canon_version() returned {got!r}, not the resolved {sentinel!r} — "
        "it is reading the pin, so /.well-known/mcp.json cannot self-heal"
    )


def test_served_version_is_never_blank_and_never_blocks_when_cold():
    """A cold process must answer from the pin INSTANTLY. The whole reason this
    is a cached resolver and not resolve_canon() is that resolve_canon() probes
    live per call (sibling floors resolver measured at a 10.3s mean) against a
    15s edge ROUTE_TIMEOUTS DEFAULT — a handler that blocks on it trades a stale
    number for a 503, and a 503 tells a registry scraper nothing at all."""
    import time

    import ai_surface_canon as canon
    saved = dict(canon._server_version_cache)
    saved_flag = canon._server_version_refreshing
    try:
        with canon._server_version_lock:
            canon._server_version_cache.update({"at": 0.0, "val": None})
        canon._server_version_refreshing = True      # suppress the refresh thread
        t0 = time.time()
        got = canon.resolve_server_version_cached()
        elapsed = time.time() - t0
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)
        canon._server_version_refreshing = saved_flag
    assert got == canon.PINNED["version"], got
    assert isinstance(got, str) and got.strip(), "cold answer must never be blank"
    assert elapsed < 1.0, f"cold path took {elapsed:.2f}s — it must never probe inline"


def test_cache_refresh_cannot_bypass_the_monotonic_guard():
    """The cache is the thing SERVED, so a refresh that wrote the raw probe
    result would let a lagging replica publish a version BEHIND the pin and
    undo _adopt_live_version entirely."""
    import inspect

    import ai_surface_canon as canon
    src = inspect.getsource(canon._refresh_server_version)
    assert "_adopt_live_version(" in src, (
        "_refresh_server_version writes the probe result directly — it must go "
        "through the monotonic/denylist decision first"
    )


def test_mcp_server_json_route_derives_the_version():
    """★ LISTED != DELIVERED. /.well-known/mcp-server.json is the surface that
    actually served 2.12.0 against a live 2.12.1 for four days; a resolver it
    imports but does not USE would change nothing.

    ★ The first draft of this test asserted `"resolve_server_version_cached" in
    block` and a mutation proved it VACUOUS: reverting the assignment to
    `_ver = _C["version"]` while leaving the import line in place kept the
    string present and the test green. Substring presence is not wiring. This
    walks the AST and requires the imported name to be CALLED and its result
    bound to the version variable the descriptor serves.
    """
    import ast
    src = (_ROOT / "routes" / "mcp_tool_catalog.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "well_known_mcp_server"), None)
    assert fn is not None, "well_known_mcp_server not found"

    # the local name the resolver is bound to (import ... as X)
    alias = None
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "resolve_server_version_cached":
                    alias = a.asname or a.name
    assert alias, "well_known_mcp_server does not import the resolver at all"

    # ...and that name must be CALLED, with the result reaching `_ver`
    wired = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "_ver" not in targets:
                continue
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == alias:
                    wired = True
    assert wired, (
        f"`{alias}` is imported but its result never reaches _ver — the route "
        "still serves the pin, so this surface cannot self-heal"
    )

    # and _ver must actually be what the descriptor publishes
    assert '"version":      _ver' in src, "descriptor no longer serves _ver"


def test_origin_manifest_has_no_hardcoded_version_literal():
    """The manifest builder must not carry its own version string again."""
    text = _MAIN.read_text()
    assert '"version": "2.3.3"' not in text, (
        "main.py re-hardcoded the manifest version — derive it from the canon"
    )
    assert '"version": _wk_canon_version()' in text


def test_wk_canon_version_is_fail_soft():
    """A broken canon import must degrade to the old literal, never to '' or None —
    an empty version field is worse than a stale one for a registry scraper."""
    src = _func_src(_MAIN, "_wk_canon_version")
    assert "except Exception" in src
    ns: dict = {}
    exec(compile(src, "<f>", "exec"), ns)
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "ai_surface_canon":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    builtins.__import__ = boom
    try:
        got = ns["_wk_canon_version"]()
    finally:
        builtins.__import__ = real_import
    assert isinstance(got, str) and got.strip(), "must not fall back to empty"


# ── (4) the worker must keep deriving ──────────────────────────────────────

def test_worker_derives_version_and_description_from_origin():
    w = _WORKER.read_text()
    m = re.search(r"const MANIFEST_DERIVED_STR_KEYS\s*=\s*\[([^\]]*)\]", w)
    assert m, "MANIFEST_DERIVED_STR_KEYS is gone — the worker is transcribing again"
    keys = set(re.findall(r"'([^']+)'", m.group(1)))
    assert {"version", "description"} <= keys, keys


def test_worker_extras_spread_stays_last_in_mcp_json():
    """Ordering IS the behaviour: mcpExtras must be spread AFTER the
    MCP_SERVER_INFO literals or the hand-typed values silently win again."""
    w = _WORKER.read_text()
    i = w.index("if (pathname === '/.well-known/mcp.json')")
    block = w[i:i + 4000]
    v_at = block.index("version:")
    x_at = block.index("...mcpExtras")
    assert x_at > v_at, "...mcpExtras must come AFTER version: in the mcp.json object"


def test_worker_server_card_also_derives():
    """Fixing only mcp.json would leave two well-known surfaces on the same zone
    disagreeing about the server's own version."""
    w = _WORKER.read_text()
    i = w.index("if (pathname === '/.well-known/mcp/server-card.json')")
    block = w[i:i + 1200]
    assert "cardExtras.version" in block
    assert "cardExtras.description" in block


def test_worker_string_merge_rejects_blank():
    """A null/missing origin field must never blank a served value."""
    w = _WORKER.read_text()
    assert "typeof v === 'string' && v.trim()" in w


# ── (5) the canon must DERIVE the version, not only pin it (2026-08-30) ─────
#
# resolve_canon() healed `tools_advertised` from the live gate and left
# `version` pinned, so the two neighbouring fields had different truth models.
# That asymmetry is what let mcp #262 bump four publish surfaces to 2.12.1,
# miss server.mjs, and go unnoticed for four days: nothing on this side was
# looking at the server's own answer.
#
# The decision is deliberately a PURE function (_adopt_live_version) so it can
# be tested without the network this file's house rule forbids; the probe
# (_mcp_server_version) is separate and exercised only for its SOURCE.

def _canon():
    import ai_surface_canon
    return ai_surface_canon


def test_version_resolver_adopts_a_forward_reading():
    """The point of the resolver: a server that moved ahead of the pin wins."""
    c = _canon()
    got, why = c._adopt_live_version("2.13.0", "2.12.1", [])
    assert got == "2.13.0", (got, why)
    assert why is None


def test_version_resolver_refuses_to_walk_backwards():
    """★ MONOTONIC. Measured 2026-08-30 during the #267 rollout: the fleet
    answered 2.12.0 and 2.12.1 within the same minute. A reading BEHIND the pin
    is a stale replica far more often than a real rollback, and 2026-08-16
    records /mcp/health echoing 2.5.0 against a live 2.12.0 — trusting that
    downward would have walked the canon backwards."""
    c = _canon()
    got, why = c._adopt_live_version("2.11.5", "2.12.1", [])   # behind, NOT retired
    assert got == "2.12.1", got
    assert "BEHIND" in why, why


def test_version_resolver_refuses_a_retired_value():
    """A canon that resolves to its own retired version flags every honest
    surface at once (ai_surface_sentinel scans bodies for stale_markers AND
    compares manifests to canon['version'] at severity high), which buries the
    real drift. Forward-but-retired must still be refused."""
    c = _canon()
    got, why = c._adopt_live_version("9.9.9", "2.12.1", ["9.9.9"])
    assert got == "2.12.1", got
    assert "denylist" in why, why


def test_version_resolver_never_yields_a_blank_or_junk_version():
    """An empty version field is worse than a stale one for a registry scraper
    — the same rule _wk_canon_version() is held to above."""
    c = _canon()
    for bad in (None, "", "   ", "garbage", "2.12", "v2.12.1", "2.12.1-rc1", 3):
        got, why = c._adopt_live_version(bad, "2.12.1", [])
        assert got == "2.12.1", (bad, got)
        assert why, bad
        assert isinstance(got, str) and got.strip()


def test_version_comparison_is_numeric_not_lexical():
    """'10.0.0' < '9.0.0' as strings. A lexical compare would refuse every
    release after 9.x as 'behind'."""
    c = _canon()
    got, why = c._adopt_live_version("10.0.0", "9.0.0", [])
    assert got == "10.0.0", (got, why)


def test_resolve_canon_actually_wires_the_resolver():
    """★ LISTED != DELIVERED. A resolver nothing calls is a no-op, and this repo
    has shipped that shape before. Assert resolve_canon() really probes, really
    routes through the decision, and really assigns the result."""
    import ast
    import inspect
    c = _canon()
    src = inspect.getsource(c.resolve_canon)
    assert "_mcp_server_version(" in src, "resolve_canon never probes the server"
    assert "_adopt_live_version(" in src, "resolve_canon bypasses the decision"
    assert 'c["version"] = _adopted' in src, "resolve_canon never assigns the result"
    # the probe result must be recorded even when it is refused
    assert 'c["version_live"]' in src


def test_version_resolver_is_fail_soft_inside_resolve_canon():
    """An unreachable MCP gate must leave the pin standing, not blank the canon.
    Asserted on the AST so it cannot pass by a comment that says 'fail-soft'."""
    import ast
    import inspect
    c = _canon()
    tree = ast.parse(inspect.getsource(c.resolve_canon).lstrip())
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
             and "_mcp_server_version" in ast.dump(n)]
    assert tries, "the version probe is not inside a try/except at all"
    handlers = [h for t in tries for h in t.handlers]
    assert handlers, "the version probe's try has no except handler"
    assert any("_version_error" in ast.dump(h) for h in handlers), \
        "a failed probe must be RECORDED as _version_error, not swallowed"


def _code_only(fn) -> str:
    """Function source with the docstring REMOVED.

    ★ Written because the first draft of the test below banned the strings
    "/mcp/health" and "well-known" and then failed on its own docstring, which
    names them precisely in order to forbid them. This repo has hit that exact
    shape before (test_no_fake_push_reintroduced, 2026-07-27): a test that
    matches its own explanation is a bad test. Assert against the CODE.
    """
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(fn))
    body = ast.parse(src).body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                      # drop the docstring
    assert body, "function parsed to an empty body — extraction failed"
    return "\n".join(ast.get_source_segment(src, n) or "" for n in body)


def test_version_probe_reads_the_authoritative_handshake_only():
    """★ THE CLOSED LOOP. /mcp/health and /.well-known/mcp.json are
    CF-synthesized surfaces that echo THIS canon back; deriving from either
    makes the canon confirm its own stale value, which is how the pin sat six
    minor versions behind until 2026-08-08. The probe must read serverInfo from
    the real `initialize` handshake and nothing else."""
    c = _canon()
    code = _code_only(c._mcp_server_version)
    assert '"method": "initialize"' in code, "probe does not perform a handshake"
    assert "serverInfo" in code, "probe does not read serverInfo"
    assert "_MCP_BASE" in code, "probe must target the MCP gate, not the backend"
    # the ONLY path this probe may request is the MCP endpoint itself
    assert '"/mcp"' in code
    for echo in ("/mcp/health", "well-known", "mcp.json"):
        assert echo not in code, f"probe reads {echo} — a CF-synthesized echo surface"


# ── (6) the two PINNED-reading surfaces (2026-09-02) ───────────────────────
#
# The 2026-08-30 self-heal wired the resolver into /.well-known/mcp.json,
# /.well-known/mcp-server.json and /openapi.json. TWO public surfaces were
# missed and kept publishing the cold-start pin. Measured live 2026-09-02,
# same minute, against the Railway origin:
#
#     live `initialize` serverInfo   2.12.3   <- the SoT
#     /openapi.json                  2.12.3   OK
#     /.well-known/mcp-server.json   2.12.3   OK
#     /mcp-server-card.json          2.12.1   STALE  <- test A
#     /api/v1/mcp/platforms          2.12.1   STALE  <- test B
#
# ★ Neither is watched by ai_surface_sentinel: `/api/v1/mcp/platforms` is not
# in its _SURFACES at all, and the entry it does carry for the card is the
# CANONICAL /.well-known/mcp/server-card.json path, which on dchub.cloud is
# answered by the CF zone worker off the origin's already-healed
# /.well-known/mcp.json. The worker masked the origin's pin on the only path
# the sentinel watches. That blind spot is why this sat, and it is why these
# two tests exist instead of an alert.

def test_mcp_server_card_route_serves_the_resolved_version():
    """/mcp-server-card.json must render the RESOLVER, not canon_text's pin.

    Behavioural, not substring: render the real route through a bare Flask app
    and read the JSON. Mirrors test_origin_manifest_version_is_canon_derived —
    seed a FRESH cache entry with a value the pin does not have, so a pin-reader
    returns PINNED["version"] and fails while a resolver-reader returns the
    sentinel. A fresh entry also keeps this offline: the resolver only starts
    its background refresh when the cached value is stale.
    """
    import json
    import time

    import flask

    import ai_discovery_routes
    import ai_surface_canon as canon

    sentinel = "99.9.9"
    assert sentinel != canon.PINNED["version"], "sentinel must differ from the pin"

    app = flask.Flask(__name__)
    ai_discovery_routes.register_discovery_routes(app)

    saved = dict(canon._server_version_cache)
    try:
        with canon._server_version_lock:
            canon._server_version_cache["val"] = sentinel
            canon._server_version_cache["at"] = time.time()
        resp = app.test_client().get("/mcp-server-card.json")
        body = resp.get_data(as_text=True)
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)

    assert resp.status_code == 200, f"server card returned {resp.status_code}"
    got = json.loads(body).get("version")
    assert got == sentinel, (
        f"/mcp-server-card.json served {got!r}, not the resolved {sentinel!r} — "
        "it is reading ai_surface_canon.PINNED, which is only the cold-start "
        "fallback, so this surface cannot self-heal (it served 2.12.1 against a "
        "live 2.12.3 on 2026-09-02)"
    )
    # ...and the fix must not have been bought by abandoning canon_text() for
    # the REST of the card: every other {canon_*} placeholder still resolves.
    assert "{canon_" not in body, (
        "an unresolved {canon_*} placeholder is being served in the card body — "
        "serving the literal placeholder is worse than the stale number"
    )


def test_mcp_platforms_endpoint_derives_the_version():
    """/api/v1/mcp/platforms must CALL the resolver, not read PINNED.

    AST-anchored, not substring. The house rule forbids importing main.py, and
    mcp_platforms_status opens a DB besides, so this walks the function the same
    way test_mcp_server_json_route_derives_the_version does — and for the same
    reason recorded in that test's docstring: a mutation proved substring
    presence VACUOUS, because reverting the assignment while leaving the import
    line in place kept the string there and the test green.
    """
    import ast
    src = _func_src(_MAIN, "mcp_platforms_status")
    fn = ast.parse(src).body[0]

    alias = None
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "resolve_server_version_cached":
                    alias = a.asname or a.name
    assert alias, "mcp_platforms_status does not import the resolver at all"

    wired = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "_server_version" not in targets:
                continue
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == alias:
                    wired = True
    assert wired, (
        f"`{alias}` is imported but its result never reaches _server_version — "
        "the endpoint still publishes the pin"
    )

    # the derived value must actually reach the response body
    assert '"server_version": _server_version' in _MAIN.read_text(), (
        "the platforms response no longer serves _server_version"
    )

    # never blocks, never raises: every rung stays inside a try/except, and the
    # fallback chain still ends at the pin (cold start) rather than at blank.
    calls_in_try = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id == alias:
                calls_in_try = True
    assert calls_in_try, (
        "the resolver call is not inside a try/except — this is a request path "
        "and the accessor's contract is that it never breaks one"
    )


# ── (7) the two WORKER-served surfaces that v4.9.45 missed (2026-09-02) ────
#
# Section (4) above fences /.well-known/mcp.json and /.well-known/mcp/
# server-card.json. Two MORE well-known docs on the same zone worker were never
# in that scope and kept transcribing MCP_SERVER_INFO. Measured live 2026-09-02
# in ONE cache-busted sweep, ONE worker (x-dc-worker-version 4.9.51 on all four):
#
#     /.well-known/mcp.json              20,100+ / 2,000+   v2.12.3   canon
#     /.well-known/mcp/server-card.json  20,100+ / 2,000+   v2.12.3   canon
#     /.well-known/ai-plugin.json        15,700+ / 1,600+             FROZEN
#     /.well-known/agent.json            15,700+ / 1,600+   v2.5.0    FROZEN
#
# ★ WHY NOTHING CAUGHT IT. ai_surface_sentinel watches the CANONICAL server-card
# path, which the worker answers off the origin's already-healed manifest. The
# healed pair MASKED the frozen pair on the only path anything was reading —
# the same blind spot section (6) records one surface earlier.
#
# ★ AND WHY THE FIX IS DELETION, NOT A NEW LITERAL. Re-typing 20,100+/2,000+
# into MCP_SERVER_INFO.description is the `_TOOL_COUNT = 59` refreeze — right on
# the day of the paste, stale at the next canon bump, and this worker deploys by
# MANUAL dashboard paste so it stays stale for months. It is also un-landable:
# BANNED_STALE in tests/test_canonical_counts_drift.py retired the PREVIOUS
# floors with patterns that now match the CURRENT canon (`(19|20|21|22|23),\d{3}\+`
# near "facilit"; `2,000+ ... deals`), so the honest literal reds the drift
# fence. Derivation is the only thing that lands, which is the point.

_POPULATION_MAGNITUDE = re.compile(r"\d{1,3}(?:,\d{3})+")


def _worker_handler_block(path_literal: str) -> str:
    """The body of one `if (pathname === '<path>')` handler in worker.js.

    Sliced to the NEXT handler so an assertion cannot accidentally read a
    neighbouring block's wiring and pass — the failure mode this whole section
    exists to catch is one handler deriving while its sibling transcribes.
    """
    w = _WORKER.read_text()
    start = w.index(f"if (pathname === '{path_literal}')")
    nxt = w.find("\n  if (pathname ===", start + 1)
    assert nxt > start, f"could not find the handler after {path_literal}"
    block = w[start:nxt]
    assert len(block) > 200, f"{path_literal} handler sliced to {len(block)}b — extraction failed"
    return block


def test_ai_plugin_json_derives_its_description():
    """★ THE DEFECT. ai-plugin.json is the doc the OpenAI-style plugin crawlers
    read, and it published a description frozen two canon generations back.

    Asserted as a NEGATIVE as well as a positive: the revert is one character —
    putting `MCP_SERVER_INFO.description` back on the served key — and a test
    that only checks "resolveManifestExtras appears somewhere in the block"
    stays green through it (this repo has shipped that vacuous shape before;
    see the `alias is imported but never reaches _ver` note in section 6).
    """
    block = _worker_handler_block("/.well-known/ai-plugin.json")
    assert "resolveManifestExtras(" in block, (
        "ai-plugin.json no longer reads the origin's canon manifest at all"
    )
    for key in ("description_for_human", "description_for_model"):
        assert not re.search(rf"{key}:\s*(?:`\$\{{)?MCP_SERVER_INFO\.description", block), (
            f"ai-plugin.json serves MCP_SERVER_INFO.description on `{key}` again — "
            "that literal is the OFFLINE FALLBACK, not the served value"
        )
    assert "pluginExtras.description || MCP_SERVER_INFO.description" in block, (
        "the fail-open fallback is gone — a missing origin field must degrade to "
        "the literal, never to a blank description"
    )


def test_agent_json_derives_its_description_and_version():
    """agent.json carried BOTH halves: the frozen description AND version 2.5.0,
    while the server card on the SAME worker served the derived 2.12.3 in the
    same second. Fixing only ai-plugin.json would leave exactly the split-brain
    test_worker_server_card_also_derives refuses one section up."""
    block = _worker_handler_block("/.well-known/agent.json")
    assert "resolveManifestExtras(" in block, (
        "agent.json no longer reads the origin's canon manifest at all"
    )
    assert not re.search(r"\bdescription:\s*MCP_SERVER_INFO\.description", block), (
        "agent.json serves the MCP_SERVER_INFO description literal again"
    )
    assert not re.search(r"\bversion:\s*MCP_SERVER_INFO\.version", block), (
        "agent.json serves the MCP_SERVER_INFO version literal again — it froze "
        "at 2.5.0 against a live 2.12.3"
    )
    assert "agentExtras.description || MCP_SERVER_INFO.description" in block
    assert "agentExtras.version || MCP_SERVER_INFO.version" in block


def test_worker_card_literal_carries_no_population_count():
    """★ THE ANTI-REFREEZE FENCE. The fallback literal must state NO facility,
    deal or asset magnitude — deleted, not updated.

    test_worker_mcp_card_is_canonical (test_canonical_counts_drift.py) already
    pins the TOOL count in this same string, and that guard works: the count is
    83 and correct. It has never checked a population count, which is how
    "15,700+ facilities / 1,600+ deals" sat in the served bytes underneath a
    green fence for a month.

    Any comma-grouped number is refused, rather than a denylist of the specific
    retired values — a value-denylist catches the LAST wrong number, never the
    next one, which is the lesson `facilities_retired_12650` records in
    BANNED_STALE. The tool count survives because it is not comma-grouped.
    """
    w = _WORKER.read_text()
    m = re.search(r"MCP_SERVER_INFO\s*=\s*\{.*?\bdescription:\s*'([^']*)'", w, re.S)
    assert m, "MCP_SERVER_INFO.description not found — update this guard to follow it"
    desc = m.group(1)
    assert "canon/phrases" in desc, (
        "the card no longer links the canonical counts — if the numbers are not "
        "in the string, the URL that has them must be"
    )
    hits = _POPULATION_MAGNITUDE.findall(desc)
    assert not hits, (
        f"MCP_SERVER_INFO.description states population count(s) {hits} again. "
        "This worker deploys by manual dashboard paste, so a literal here "
        "re-freezes for months (it shipped 15,700+/1,600+ against canon "
        "20,100+/2,000+). Link https://dchub.cloud/api/v1/canon/phrases instead."
    )


def test_fallback_tool_descriptions_carry_no_facility_count():
    """MCP_FALLBACK_TOOLS is what the manifest serves when the origin tools/list
    is unreachable, so its descriptions are agent-facing bytes too. `why_dchub`
    carried the SAME "15,700+ facilities" literal as the card — one edit, two
    homes, and only one of them was ever looked at.

    Scoped to the array so the `*`-prefixed changelog header above it, which
    RECORDS retired counts on purpose, is never scanned.
    """
    w = _WORKER.read_text()
    m = re.search(r"const MCP_FALLBACK_TOOLS = \[\n(.*?)\n\];", w, re.S)
    assert m, "MCP_FALLBACK_TOOLS array missing from worker.js"
    body = m.group(1)
    assert len(body) > 10_000, f"array sliced to {len(body)}b — extraction failed"
    hits = re.findall(r"\d{1,3}(?:,\d{3})+\+?\s*(?:distinct\s+)?facilit\w*", body)
    assert not hits, (
        f"a fallback tool description states a facility count {hits} — the "
        "backend catalog builds the same text through canon_text('{canon_facilities}') "
        "and the worker cannot, so the worker copy must state no count at all."
    )


# ── (8) the two AGENT-FACING surfaces still on the pin (2026-09-02) ────────
#
# #3628 (section 6) healed /mcp-server-card.json and /api/v1/mcp/platforms.
# The same sweep, same minute, same Railway origin, found two MORE surfaces
# still publishing the cold-start pin — and these two are the ones AI agents
# actually read first:
#
#     live `initialize` serverInfo        2.12.3   <- the SoT
#     /.well-known/mcp-server.json        2.12.3   OK
#     /openapi.json                       2.12.3   OK
#     /mcp/health                         2.12.3   OK
#     /api/v1/agents/capabilities.json    2.12.1   STALE  <- test A
#     /AGENTS.md                          2.12.1   STALE  <- test B
#
# ★★★ THESE TESTS ARE THE ONLY GUARD. BOTH SURFACES ARE SENTINEL-BLIND.
#   · /AGENTS.md IS in ai_surface_sentinel._SURFACES — but as kind "text"
#     (a deliberate choice: the CF zone worker runs one patch AHEAD by design,
#     so a json check would false-RED). The version comparison lives under
#     `if kind == "json"`, so it NEVER RUNS for this surface. Text mode scans
#     stale_markers only, and 2.12.1 is not a stale marker — it is the PIN.
#   · /api/v1/agents/capabilities.json is not in _SURFACES AT ALL.
# Nothing alerts if either regresses. Do not delete these without replacing
# them with an alert that actually fires.

def _pin_floors():
    """Offline stand-in for resolve_public_floors().

    ★ NOT a convenience. resolve_public_floors() is live-probing and UNCACHED,
    and was measured at 12.9s on a cold call with no DATABASE_URL while writing
    this test. The house rule at the top of this file is no DB and no network,
    and a 13s unit test is how a suite starts getting skipped.
    """
    import ai_surface_canon as canon
    # PINNED["public"] is exactly the dict resolve_public_floors() starts from
    # before the live overlay, so this stub IS the "no live value raised a
    # floor" branch of the real function — not an invented shape.
    return dict(canon.PINNED["public"])


def test_agents_md_serves_the_resolved_version_not_the_pin():
    """/AGENTS.md must render the RESOLVER, not PINNED["version"].

    Behavioural: renders the real handler and reads the served markdown. Seeds
    a FRESH cache entry holding a value the pin does not have, so a pin-reader
    returns PINNED["version"] and FAILS while a resolver-reader returns the
    sentinel. Fresh keeps it offline — the resolver only starts its background
    refresh when the cached value is stale.
    """
    import time

    import ai_surface_canon as canon
    import routes.agents_md_fallback as amd

    sentinel = "99.9.9"
    pin = canon.PINNED["version"]
    assert sentinel != pin, "sentinel must differ from the pin"

    saved = dict(canon._server_version_cache)
    saved_floors = amd.resolve_public_floors
    try:
        amd.resolve_public_floors = _pin_floors
        with canon._server_version_lock:
            canon._server_version_cache["val"] = sentinel
            canon._server_version_cache["at"] = time.time()
        body = amd._render_agents_md()
    finally:
        amd.resolve_public_floors = saved_floors
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)

    assert f"version {sentinel}," in body, (
        f"/AGENTS.md did not render the resolved version {sentinel!r} — it is "
        f"reading PINNED['version'], which is the COLD-START FLOOR, so the "
        f"primary agent-discovery surface cannot self-heal (it served {pin} "
        f"against a live 2.12.3 on 2026-09-02). ai_surface_sentinel audits this "
        f"surface as kind='text' and runs NO version comparison on it, so "
        f"nothing else will ever tell you."
    )
    assert f"version {pin}," not in body, (
        "the pin is STILL being rendered as the version alongside the resolved "
        "value — the fix added a rung instead of replacing one"
    )


def test_agents_md_changed_ONLY_the_version():
    """The version fix must not have quietly swapped the rest of the page.

    ★ THE DANGEROUS FIX HERE IS THE PLAUSIBLE ONE. This file's docstring records,
    measured 2026-08-28, that resolve_canon() with no DATABASE_URL returns PUBLIC
    STRINGS — facilities "400+" against a pinned "18,500+", a 46x UNDER-claim on
    the primary agent-discovery surface — WITHOUT raising, while canon_is_live()
    reads True for it. A "just use resolve_canon" cleanup would look like a
    generalisation of the version fix and would be a regression that reports
    healthy. So: `c = PINNED` stands, and the floors keep coming from
    resolve_public_floors(), which applies live values ONLY where they RAISE.
    """
    src = _func_src(_ROOT / "routes" / "agents_md_fallback.py", "_render_agents_md")
    fn = ast.parse(src).body[0]

    # `c = PINNED` — untouched
    assert any(
        isinstance(n, ast.Assign)
        and {t.id for t in n.targets if isinstance(t, ast.Name)} == {"c"}
        and isinstance(n.value, ast.Name) and n.value.id == "PINNED"
        for n in ast.walk(fn)
    ), "`c = PINNED` is gone — every non-version field just changed basis"

    # floors still come from resolve_public_floors(), not resolve_canon()
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "resolve_public_floors" in called, (
        "resolve_public_floors() is no longer called — the floors stopped "
        "self-healing upward"
    )
    assert "resolve_canon" not in called, (
        "resolve_canon() reached _render_agents_md. Measured 2026-08-28 with no "
        "DATABASE_URL it returns public strings ('400+' facilities) without "
        "raising while canon_is_live() reads True — a REGRESSION that looks "
        "healthy. resolve_server_version_cached() is version-only and monotonic; "
        "resolve_canon() is neither. See this module's docstring."
    )
    assert "resolve_server_version_cached" in called, (
        "the version resolver is not called — /AGENTS.md is back on the pin"
    )


def test_agent_capabilities_feed_derives_and_has_no_rotting_literal():
    """/api/v1/agents/capabilities.json: resolver first, PIN as the deepest rung.

    Two defects, one function. It read canon_text("{canon_version}") — the pin —
    and its except/or fallback was the hand-typed "2.1.10", ELEVEN minors behind
    the pin it was supposedly backing up. On a CC-BY-4.0 feed built to be quoted,
    that made the failure path publish a citable claim WORSE than the one it was
    protecting.
    """
    import time

    import ai_surface_canon as canon
    from routes.agent_capabilities_feed import _canon_version

    sentinel = "99.9.9"
    pin = canon.PINNED["version"]
    assert sentinel != pin, "sentinel must differ from the pin"

    saved = dict(canon._server_version_cache)
    try:
        with canon._server_version_lock:
            canon._server_version_cache["val"] = sentinel
            canon._server_version_cache["at"] = time.time()
        warm = _canon_version()
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)

    assert warm == sentinel, (
        f"_canon_version() returned {warm!r}, not the resolved {sentinel!r} — the "
        f"feed is publishing ai_surface_canon.PINNED, the cold-start floor "
        f"(it served {pin} against a live 2.12.3 on 2026-09-02). This endpoint is "
        f"NOT in ai_surface_sentinel._SURFACES, so nothing else watches it."
    )

    # ★ AST-anchored, not `"2.1.10" in text`: a substring check would pass on a
    # mention inside a comment or docstring, and this file's neighbours record
    # that exact vacuity being proved by mutation. Only real string CONSTANTS in
    # the executable body count.
    src = _func_src(_ROOT / "routes" / "agent_capabilities_feed.py", "_canon_version")
    fn = ast.parse(src).body[0]
    body_wo_doc = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                                  and isinstance(fn.body[0].value, ast.Constant)
                                  and isinstance(fn.body[0].value.value, str)) else fn.body
    consts = {n.value for stmt in body_wo_doc for n in ast.walk(stmt)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    versionish = {c for c in consts if re.fullmatch(r"\d+\.\d+\.\d+", c)}
    assert not versionish, (
        f"_canon_version() carries hand-typed version literal(s) {sorted(versionish)}. "
        "Every rung must derive: the deepest one is PINNED['version'], which is "
        "chased upward in ai_surface_canon.py and leaves a diff. A literal here "
        "rots silently — '2.1.10' sat eleven minors behind the pin."
    )

    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "resolve_server_version_cached" in called, (
        "the resolver is not called — the feed is back on the pin"
    )


def test_both_surfaces_are_blank_proof_when_the_cache_is_cold():
    """COLD START MUST SERVE THE PIN — never None, never "".

    ★ MEASURED, NOT ASSUMED. The cache is emptied AND _server_version_refreshing
    is latched True so the background refresh cannot start: this is the state of
    a process in the first moments after boot, before any probe has landed. An
    empty version field is worse than a stale one for a registry scraper, and a
    literal `None` on a CC-BY card is worse still.
    """
    import ai_surface_canon as canon
    import routes.agents_md_fallback as amd
    from routes.agent_capabilities_feed import _canon_version

    pin = canon.PINNED["version"]
    saved = dict(canon._server_version_cache)
    saved_flag = canon._server_version_refreshing
    saved_floors = amd.resolve_public_floors
    try:
        amd.resolve_public_floors = _pin_floors
        with canon._server_version_lock:
            canon._server_version_cache["val"] = None
            canon._server_version_cache["at"] = 0.0
            canon.__dict__["_server_version_refreshing"] = True
        cold_feed = _canon_version()
        cold_md = amd._render_agents_md()
    finally:
        amd.resolve_public_floors = saved_floors
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)
            canon.__dict__["_server_version_refreshing"] = saved_flag

    assert cold_feed, f"capabilities.json cold-start version is falsy: {cold_feed!r}"
    assert cold_feed == pin, (
        f"cold start served {cold_feed!r}, expected the pin {pin!r} — the "
        f"cold-start answer must be byte-identical to what canon_text("
        f"'{{canon_version}}') produced before this change"
    )
    assert f"version {pin}," in cold_md, (
        "/AGENTS.md cold start did not fall back to the pin"
    )
    for bad in ("version ,", "version None,", "version ."):
        assert bad not in cold_md, f"/AGENTS.md cold start served {bad!r}"


def test_capabilities_json_RESPONSE_BODY_carries_the_resolved_version():
    """★ THE GAP THE HELPER TEST LEAVES OPEN. Testing _canon_version() proves
    the HELPER derives; it does not prove the FEED serves it. Mutation, run
    2026-09-02: with _canon_version() left perfectly wired and
    `"version": _canon_version()` in _gather() replaced by the literal
    "2.1.10", the whole section-7 suite AND all 95 tests in
    tests/test_canonical_counts_drift.py stayed GREEN while the served body
    published 2.1.10 — the exact rotting literal this PR exists to kill. That
    is #3628's mutation (b) one level out.

    So: render the real route and read the response. Offline and DB-free by
    construction — _gather() guards every query with `if _pg and _dsn():`
    inside try/except (measured: 0.019s, zero outbound sockets, no
    DATABASE_URL).

    ★ _CAPS_CACHE is a PROCESS-LOCAL memo and pre-merge runs every test file in
    ONE process, so a sentinel version left in it would poison
    _render_canon_routes() in tests/test_canonical_counts_drift.py, which
    renders this same path. Cleared before AND in the finally — the house
    pattern from that file.
    """
    import json
    import time

    import flask

    import ai_surface_canon as canon
    from routes import agent_capabilities_feed as feed

    sentinel = "99.9.9"
    assert sentinel != canon.PINNED["version"], "sentinel must differ from the pin"

    app = flask.Flask(__name__)
    app.register_blueprint(feed.agent_capabilities_bp)

    saved = dict(canon._server_version_cache)
    try:
        with canon._server_version_lock:
            canon._server_version_cache["val"] = sentinel
            canon._server_version_cache["at"] = time.time()
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})
        resp = app.test_client().get("/api/v1/agents/capabilities.json")
        body = resp.get_data(as_text=True)
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})

    assert resp.status_code == 200, f"capabilities feed returned {resp.status_code}"
    got = json.loads(body).get("version")
    assert got == sentinel, (
        f"/api/v1/agents/capabilities.json SERVED {got!r}, not the resolved "
        f"{sentinel!r}. The helper may derive correctly and still not reach the "
        "response body — that is the mutation this test exists for. This "
        "endpoint is not in ai_surface_sentinel._SURFACES, so nothing alerts."
    )


def test_capabilities_feed_deepest_rung_is_the_PIN_not_None_or_blank(monkeypatch):
    """The DEEPEST fallback rung must be PINNED['version'] — not None, not "".

    ★ THIS TEST EXISTS BECAUSE TWO MUTATIONS SURVIVED WITHOUT IT (2026-09-02).
    test_both_surfaces_are_blank_proof_when_the_cache_is_cold empties the cache,
    but rung 1 — resolve_server_version_cached() — is itself blank-proof and
    hands back PINNED["version"] on a cold cache. So the cold-start test never
    reaches rungs 2 and 3 at all: changing the deepest rung to `return None` or
    `return ""` left the whole file GREEN. The rung the PR is specifically about
    ("deepest fallback is the PIN, never a hand-typed literal") was untested.

    So drive the ladder down on purpose: make rung 1 (the resolver) and rung 2
    (canon_text) both RAISE, which is the only state in which rung 3 runs, and
    assert it still publishes the pin. This is the state that replaced the old
    hand-typed "2.1.10" — a fallback ELEVEN minors behind the pin it was
    supposedly backing up, on a CC-BY-4.0 feed built to be quoted.
    """
    import ai_surface_canon as canon
    from routes.agent_capabilities_feed import _canon_version

    def _boom(*a, **k):
        raise RuntimeError("canon is down")

    # Both rungs import from the module INSIDE the function, so patching the
    # module attribute reaches them at call time.
    monkeypatch.setattr(canon, "resolve_server_version_cached", _boom)
    monkeypatch.setattr(canon, "canon_text", _boom)

    got = _canon_version()
    assert got == canon.PINNED["version"], (
        f"with the resolver and canon_text both down, _canon_version() returned "
        f"{got!r}. The deepest rung must be PINNED['version'] "
        f"({canon.PINNED['version']!r}) — the pin is chased upward in "
        f"ai_surface_canon.py and leaves a diff, while None/'' publishes a null "
        f"or empty version on a CC-BY-4.0 card and a hand-typed literal rots "
        f"unwatched (the old '2.1.10' sat eleven minors behind the pin)."
    )


# ── (8) the DAY-LONG MEMO must not latch the cold-start pin ────────────────
#
# ★#3636 FIXED THE DERIVATION AND THE SURFACE STILL SERVED THE PIN. Measured
# on its own deploy, 12 cache-busted requests to the live endpoint at
# 01:31-01:34Z on 2026-09-03:
#
#     7 served  "version": "2.12.1"   computed_at 01:30:41.677362Z
#     5 served  "version": "2.12.3"   computed_at 01:30:28.189231Z
#
# Two replicas, two process-local memos, computed 13 seconds apart — one
# before the background refresh landed and one after — and BOTH latched until
# data_version flips at 00:00 UTC. The same deploy fixed /AGENTS.md completely
# (8/8 correct) because that surface renders per request and has no memo.
#
# ★WHY THE EXISTING RESPONSE-BODY TEST COULD NOT SEE IT: it clears _CAPS_CACHE
# immediately before issuing the request, so the memo is always COLD and the
# resolver's answer always flows through. That is the right test for the
# derivation and it is blind to the latch by construction. The bug lives
# entirely in the case that test resets away — a memo that is already warm and
# already wrong.
#
# So this test does the opposite: it PRIMES the memo with a stale version, the
# way a real replica does moments after boot, and asserts the response is
# fresh anyway.

def test_capabilities_json_memo_does_not_latch_a_stale_version():
    """A WARM memo holding yesterday's answer must not outrank the resolver."""
    import datetime
    import json
    import time

    import flask

    import ai_surface_canon as canon
    from routes import agent_capabilities_feed as feed

    stale = "0.0.1"          # what a cold resolver froze into the memo
    fresh = "99.9.9"          # what the resolver says NOW
    assert stale != canon.PINNED["version"] != fresh

    app = flask.Flask(__name__)
    app.register_blueprint(feed.agent_capabilities_bp)

    today = int(datetime.date.today().strftime("%Y%m%d"))
    saved = dict(canon._server_version_cache)
    try:
        with canon._server_version_lock:
            canon._server_version_cache["val"] = fresh
            canon._server_version_cache["at"] = time.time()
        # ★Prime the memo the way a freshly-booted replica does: a complete
        # payload for TODAY whose version is the cold-start answer. _gather()
        # will NOT run again today, so nothing else can correct this.
        feed._CAPS_CACHE.update({
            "data_version": today,
            "payload": {"name": "DC Hub", "version": stale, "data_version": today},
            "computed_at": time.time(),
        })
        resp = app.test_client().get("/api/v1/agents/capabilities.json")
        body = resp.get_data(as_text=True)
        # The shared memo must not have been mutated in place — other readers
        # (and the ETag/data_version contract) still hold it.
        memo_after = dict(feed._CAPS_CACHE["payload"])
    finally:
        with canon._server_version_lock:
            canon._server_version_cache.clear()
            canon._server_version_cache.update(saved)
        feed._CAPS_CACHE.update({"data_version": None, "payload": None,
                                 "computed_at": 0.0})

    assert resp.status_code == 200, f"capabilities feed returned {resp.status_code}"
    got = json.loads(body).get("version")
    assert got == fresh, (
        f"/api/v1/agents/capabilities.json SERVED {got!r} from its day-long memo "
        f"while the resolver said {fresh!r}. This is the LIVE 2026-09-03 defect: "
        "the memo froze the cold-start pin for up to 24h on 7 of 12 requests. "
        "The version must be re-read per request — the memo exists for the "
        "COUNTS (a cold-DB hop), and the resolver is already in-memory."
    )
    assert memo_after["version"] == stale, (
        "the overlay mutated the SHARED memo in place; it must shallow-copy")


def test_capabilities_version_overlay_degrades_to_the_memo():
    """If the resolver has nothing to say, keep serving the memo — never blank.

    The overlay must not be able to turn a working feed into one publishing
    null/"" for a field that is a citable claim on a CC-BY card."""
    from routes import agent_capabilities_feed as feed

    memo = {"name": "DC Hub", "version": "1.2.3"}
    for answer in (None, ""):
        # Drive the real helper with the resolver stubbed at the call site.
        orig = feed._canon_version
        try:
            feed._canon_version = lambda: answer
            out = feed._with_live_version(dict(memo))
        finally:
            feed._canon_version = orig
        assert out["version"] == "1.2.3", (
            f"resolver answered {answer!r} and the overlay dropped the memo's "
            f"version; got {out.get('version')!r}")
