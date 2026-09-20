"""tests/test_wellknown_tool_count_derived.py — the manifest's TOOL COUNT must derive (2026-09-20).

/.well-known/mcp-server.json is what every MCP registry scrapes. Its version
was taught to self-heal on 2026-08-30 (see
tests/test_wellknown_manifest_version_derived.py). The tool count one line
below it was not, and read the pin directly:

    _tools = _C.get("tools_advertised", 73)        # _C is PINNED

★ WHY THAT SHAPE IS THE BUG AND NOT A FALLBACK. PINNED always HAS
`tools_advertised`, so the `73` could never fire — it is decoration on a
binding whose only possible value is the pin. resolve_canon() has healed
`tools_advertised` off the live tools/list gate for months; this consumer
simply did not go through the gate. That is the same defect that kept
"assets 320,000+" live against a measured 330,963 for weeks: a value that IS
measured, published from a pin, because the consumer read around the resolver.

★ AND IT WAS NOT DETECTABLE, WHICH IS WHY IT NEEDED FIXING RATHER THAN
WATCHING. ai_surface_sentinel audits this manifest at severity HIGH by
comparing its `version` FIELD to the canon — that is what made the version's
fix buy detection for free. The tool count is not a field. It is prose inside
`description`, so no field comparison reaches it. Nothing was looking, and
nothing could have been.

Ways this regresses, each asserted below:
  (1) THE RULE FORKS AGAIN — resolve_canon() or the resolver grows its own
      inline copy of "when do we believe a live reading", so the two can
      disagree and the manifest quietly becomes the odd one out.
  (2) RE-PINNED CONSUMER — the manifest goes back to reading PINNED, or to any
      integer literal in the count binding (the `or 33` shape that shipped a
      count stale by 49 on /by-the-numbers).
  (3) A DEGRADED GATE PUBLISHES ZERO — _mcp_tool_names() returns [] rather than
      None when a tools/list frame carries no tools, so _mcp_tool_count()
      answers 0 on a degraded server. "0 MCP tools" is the one outcome worse
      than a stale number.
  (4) AN INVENTED COUNT ON THE IMPORT-FAILURE PATH — the except branch goes
      back to naming a number no one will ever see fail.

★ WHAT THIS FILE DOES NOT CLAIM. Deriving does not make the pin irrelevant: a
cold process still serves it for the first few seconds after boot, and unlike
the version (whose adoption is monotonic, so the pin can only LAG) this pin can
be an OVER-claim — 91 against a live 90 — for that window. The pin still has to
be walked when a tool is removed. That is strictly better than a surface whose
only possible value is the pin; it is not "solved", and the docstring on
resolve_tools_advertised_cached() says so too.

House rule, inherited from the sibling file: no DB, no network, and NEVER
import main.

Run:  python3 -m pytest tests/test_wellknown_tool_count_derived.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CATALOG = _ROOT / "routes" / "mcp_tool_catalog.py"
_CANON = _ROOT / "ai_surface_canon.py"

_SENTINEL = 4242            # a count no pin and no live server can produce


def _func_src(path: pathlib.Path, name: str) -> str:
    """Slice `def name(` out by ast — parsed, not regex-guessed.

    ★ A silently-empty extraction passes every downstream assertion, so this
    asserts the parse really found a FunctionDef with a real body.
    """
    text = path.read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            assert node.body, f"{name} parsed with an EMPTY body — extraction failed"
            return ast.get_source_segment(text, node)
    raise AssertionError(f"{name} not found in {path.name}")


# ── Quiesce BOTH request-path caches for every test here ───────────────────
#
# ★ Same reproduced race the sibling file documents, one cache over:
# resolve_tools_advertised_cached() spawns a daemon thread that probes the LIVE
# tools/list gate and writes into the same dict a test just seeded. A thread
# started by an EARLIER test file can land in the middle of a later one here.
# Join anything in flight, then latch the "already refreshing" flag so nothing
# in this file can start a new one — which is also what makes the house rule
# "no network" true by construction rather than by luck of ordering.
@pytest.fixture(autouse=True)
def _quiesce_caches():
    import threading

    import ai_surface_canon as _c

    for _th in threading.enumerate():
        if _th.name in ("tools-advertised-refresh", "server-version-refresh") and _th.is_alive():
            _th.join(timeout=30)

    with _c._tools_advertised_lock:
        saved_tools = dict(_c._tools_advertised_cache)
        saved_tflag = _c._tools_advertised_refreshing
        _c.__dict__["_tools_advertised_refreshing"] = True
    with _c._server_version_lock:
        saved_ver = dict(_c._server_version_cache)
        saved_vflag = _c._server_version_refreshing
        _c.__dict__["_server_version_refreshing"] = True
    try:
        yield
    finally:
        with _c._tools_advertised_lock:
            _c._tools_advertised_cache.clear()
            _c._tools_advertised_cache.update(saved_tools)
            _c.__dict__["_tools_advertised_refreshing"] = saved_tflag
        with _c._server_version_lock:
            _c._server_version_cache.clear()
            _c._server_version_cache.update(saved_ver)
            _c.__dict__["_server_version_refreshing"] = saved_vflag


@pytest.fixture()
def manifest_client():
    """The REAL route, on a bare Flask app — not a re-implementation of it.

    Registering the blueprint alone keeps the no-main house rule: this module
    imports cleanly on its own.
    """
    from flask import Flask

    from routes.mcp_tool_catalog import mcp_tool_catalog_bp

    app = Flask(__name__)
    app.register_blueprint(mcp_tool_catalog_bp)
    return app.test_client()


# ── (1) ONE adoption rule ──────────────────────────────────────────────────

@pytest.mark.parametrize("live,pinned,expected,why", [
    (95,   91,   95,   "live-higher"),   # tools added — publish the new count
    (90,   91,   90,   "live-lower"),    # ★ a tool REMOVED. The over-claim the
                                         #   version's monotonic rule would keep.
    (91,   91,   91,   "agrees"),
    (0,    91,   91,   "non-positive"),  # ★ reachable: [] from a degraded gate
    (-3,   91,   91,   "non-positive"),
    (None, 91,   91,   "not-an-int"),    # no tools/list frame at all
    ("91", 91,   91,   "not-an-int"),
    (True, 91,   91,   "not-an-int"),    # bool is an int in Python; refuse it
])
def test_adoption_rule_is_pure_and_bidirectional(live, pinned, expected, why):
    """★ THE DIRECTION IS THE POINT. _adopt_live_version refuses to go
    backwards because an over-claimed VERSION breaks a registry publish. For a
    COUNT the dangerous direction is the opposite one — publishing 91 tools
    against a server advertising 90 — so this must accept a lower reading. A
    test that only ever moved the number up would pass against a monotonic
    copy-paste of the version rule, which is the mistake this row set exists to
    catch."""
    from ai_surface_canon import _adopt_live_tool_count
    got, got_why = _adopt_live_tool_count(live, pinned)
    assert (got, got_why) == (expected, why)


def test_resolve_canon_has_no_second_copy_of_the_rule():
    """(1) The rule must have ONE home. resolve_canon() carried it inline
    (`isinstance(int) and > 0`); if that comes back, the manifest and the canon
    can answer differently about the same live reading and the disagreement is
    invisible."""
    body = _func_src(_CANON, "resolve_canon")
    assert "_adopt_live_tool_count(" in body, (
        "resolve_canon no longer routes the tool count through "
        "_adopt_live_tool_count — a second inline rule is how the manifest and "
        "the canon drift apart while both look derived."
    )
    assert 'c["tools_live"] > 0' not in body and "tools_live'] > 0" not in body, (
        "resolve_canon has re-grown its own inline `> 0` rule alongside "
        "_adopt_live_tool_count. Two copies, one of which will be edited."
    )


def test_cached_resolver_uses_the_same_rule():
    body = _func_src(_CANON, "_refresh_tools_advertised")
    assert "_adopt_live_tool_count(" in body, (
        "the background refresh no longer uses the shared rule."
    )


# ── (2) the consumer derives ───────────────────────────────────────────────

def test_manifest_tool_count_binding_carries_no_integer_literal():
    """(2) THE SHAPE, not the value — no future stale number can satisfy it.

    `_C.get("tools_advertised", 73)` is the `or 33` shape that shipped a
    /by-the-numbers count stale by 49: a literal that looks like a fallback but
    is decoration on a binding whose only possible value is the pin.
    """
    body = _func_src(_CATALOG, "well_known_mcp_server")
    tree = ast.parse(body.strip())
    binding = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_tools"):
            binding = node.value
    assert binding is not None, (
        "well_known_mcp_server no longer binds `_tools` — if the manifest's "
        "count moved, follow it here."
    )
    src = ast.unparse(binding)
    assert "resolve_tools_advertised_cached" in src or "_wk_tools" in src, (
        f"the manifest count no longer resolves live -> {src!r}. Reading "
        "PINNED here is the defect this file exists for."
    )
    literals = [n.value for n in ast.walk(binding)
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
    assert not literals, (
        f"the manifest count binding carries hard-coded integer(s) {literals} "
        f"-> {src!r}."
    )


def test_manifest_serves_the_resolved_count(manifest_client):
    """The whole point, end to end: seed the resolver's cache and read the
    number out of the SERVED description."""
    import ai_surface_canon as _c

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = _SENTINEL
        _c._tools_advertised_cache["at"] = __import__("time").time()
    d = manifest_client.get("/.well-known/mcp-server.json").get_json()
    assert f"{_SENTINEL} MCP tools" in d["description"], (
        f"the manifest did not render the resolved count -> {d['description']!r}"
    )


def test_manifest_falls_back_to_the_pin_when_cold(manifest_client):
    """A cold process must still publish a count — and it must be the pin, not
    a literal living in the route."""
    from ai_surface_canon import PINNED
    d = manifest_client.get("/.well-known/mcp-server.json").get_json()
    assert f"{PINNED['tools_advertised']} MCP tools" in d["description"]


# ── (3) a degraded gate must not publish zero ───────────────────────────────

def test_cached_resolver_never_answers_zero():
    """(3) _mcp_tool_names() returns [] — NOT None — when a tools/list frame
    comes back carrying no tools, so _mcp_tool_count() answers 0 on a degraded
    gate. This is reachable, and '0 MCP tools' in a registry listing is worse
    than a stale count."""
    import ai_surface_canon as _c
    from ai_surface_canon import PINNED

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = 0
        _c._tools_advertised_cache["at"] = __import__("time").time()
    assert _c.resolve_tools_advertised_cached() == PINNED["tools_advertised"]


def test_zero_is_refused_at_the_rule_too(manifest_client):
    """The same refusal, read off the SERVED surface rather than the resolver,
    because that is where a 0 would actually do the damage."""
    import ai_surface_canon as _c

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = 0
        _c._tools_advertised_cache["at"] = __import__("time").time()
    d = manifest_client.get("/.well-known/mcp-server.json").get_json()
    assert "0 MCP tools" not in d["description"]


# ── (4) no invented count on the import-failure path ───────────────────────

def test_import_failure_path_names_no_count():
    """(4) The except branch published `53` — already stale by 38 when it was
    written, and reachable only on a total import failure, so it was a wrong
    answer nobody would ever see fail. canon_text()'s own rule applies: the
    worst case is a COUNT-FREE sentence, never a wrong one."""
    body = _func_src(_CATALOG, "well_known_mcp_server")
    tree = ast.parse(body.strip())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "well_known_mcp_server no longer has an except branch — extraction failed"
    for h in handlers:
        src = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
        if "_tools" not in src:
            continue
        ints = [n.value for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
        assert not ints, (
            f"the import-failure path invents a tool count {ints} -> {src!r}. "
            "Publish no count instead."
        )


def test_description_omits_the_clause_rather_than_publishing_a_blank(manifest_client):
    """…and the sentence must still READ, not carry a dangling em dash or an
    empty slot. Checked by driving the real route with the count unavailable."""
    import ai_surface_canon as _c

    orig = _c.resolve_tools_advertised_cached
    _c.resolve_tools_advertised_cached = lambda: None          # the cold/failed answer
    try:
        d = manifest_client.get("/.well-known/mcp-server.json").get_json()
    finally:
        _c.resolve_tools_advertised_cached = orig
    desc = d["description"]
    assert "MCP tools" not in desc, f"a countless render still claims tools -> {desc!r}"
    assert "None" not in desc and "—  " not in desc, f"dangling render -> {desc!r}"
    assert desc.startswith("Data center site selection"), desc
    assert "The only DC-intelligence source" in desc, desc


# ── MUST-FAIL CONTROLS — the checks above can see a violation ──────────────

def test_the_ast_extraction_actually_found_the_route():
    """A silently-empty extraction passes every assertion above."""
    body = _func_src(_CATALOG, "well_known_mcp_server")
    assert len(body.splitlines()) > 20, "extraction returned a stub"
    assert "descriptor" in body and "tools_manifest" in body


def test_the_literal_check_would_catch_the_shape_it_bans():
    """Prove the integer-literal detector is not vacuous by running it over the
    exact binding that was there before."""
    binding = ast.parse('_tools = _C.get("tools_advertised", 73)').body[0].value
    literals = [n.value for n in ast.walk(binding)
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
    assert literals == [73], literals


def test_the_sentinel_is_not_a_value_the_system_could_produce():
    """If _SENTINEL ever equalled the pin, test_manifest_serves_the_resolved_count
    would pass against a surface that ignored the resolver entirely."""
    from ai_surface_canon import PINNED
    assert _SENTINEL != PINNED["tools_advertised"]


# ── (5) {canon_tools} — the other consumer of the same number ──────────────
#
# ★ THE MANIFEST WAS NOT THE ONLY ONE. canon_nums() binds the placeholder every
# canon_text() surface renders — /llms.txt, /connect, /api/v1/ai-agents.json,
# /.well-known/mcp.json, /agent, /by-the-numbers — and it read the pin too,
# sitting one line above five floors that all take the LIVE value first. Fixing
# the manifest alone would have left the same number stale on more surfaces
# than it fixed.

def test_canon_tools_placeholder_resolves_live():
    """The seeded resolver must reach the placeholder.

    ★ THIS IS ALSO THE ANTI-SWALLOW GUARD. canon_nums() calls the resolver
    inside a try/except that falls back to the pin, so a NameError, an import
    cycle or a renamed resolver would degrade to exactly the old behaviour and
    look completely healthy. The only way to tell the difference is to seed a
    value the pin cannot produce and demand it comes out.
    """
    import time

    import ai_surface_canon as _c

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = _SENTINEL
        _c._tools_advertised_cache["at"] = time.time()
    assert _c.canon_nums()["{canon_tools}"] == str(_SENTINEL), (
        "{canon_tools} did not follow the resolver — canon_nums has fallen back "
        "to PINNED, which is the defect, not the fallback."
    )


def test_canon_tools_placeholder_falls_back_to_the_pin_when_cold():
    from ai_surface_canon import PINNED, canon_nums
    assert canon_nums()["{canon_tools}"] == str(PINNED["tools_advertised"])


def test_canon_nums_never_renders_an_empty_tool_count():
    """canon_text()'s documented worst case is a COUNT-FREE sentence, but
    {canon_tools} is rendered INTO prose that reads '{canon_tools} MCP tools',
    so an empty string there ships 'analysis —  MCP tools'. The pin is the
    floor; there is no path to blank."""
    import time

    import ai_surface_canon as _c

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = 0          # the degraded-gate answer
        _c._tools_advertised_cache["at"] = time.time()
    assert _c.canon_nums()["{canon_tools}"].strip(), "empty {canon_tools}"


def test_the_floors_beside_it_still_resolve_live_first():
    """MUST-FAIL CONTROL for the premise of this whole file: the asymmetry was
    real. If the floors had never resolved live either, 'the floors healed and
    the tool count did not' would be a story rather than a finding."""
    import ai_surface_canon as _c
    src = _func_src(_CANON, "canon_nums")
    assert "_live.get('facilities')" in src, (
        "canon_nums no longer takes the live floor first — the asymmetry this "
        "file is premised on has moved."
    )
    assert "_live = _live_public_floors()" in src


# ── (6) canon_nums() MUST NOT REACH THE NETWORK ────────────────────────────
#
# ★ THIS IS A REGRESSION TEST FOR THIS PR'S OWN FIRST ATTEMPT, and it is the
# most important assertion in the file.
#
# The first version had canon_nums() call resolve_tools_advertised_cached()
# plainly. That starts a background probe of dchub.cloud/mcp when the cache is
# cold — correct on a request path, catastrophic here: canon_nums() renders
# {canon_*} for every canon_text() surface, so it runs inside thousands of
# tests. CI refused a live fetch in 24 test files at once:
#
#   ##[error]no network: tests/test_honest_numbers.py reached dchub.cloud (4x),
#   and tests/no_network_register.json does not list it. The hook refused it.
#   Stub the fetch in the test; do not add it to the register to make it pass.
#
# Measured as 0 attempts on origin/main and 2 on the branch, over the same two
# files — so it was the change, not the environment. pytest itself stayed GREEN
# on both (35 passed); only the hook's post-run report failed the step, which
# is why reading the pytest summary alone missed it.
#
# The rule was already written down one function below, about _live_public_floors:
# "PEEK-ONLY — it never triggers a query, so there is no round trip to pay for
# on this path." peek=True is that rule, applied to the same path.

def test_canon_nums_starts_no_live_probe():
    """canon_nums() must answer from the cache and start NOTHING."""
    import ai_surface_canon as _c

    called = []
    orig = _c._mcp_tool_count
    _c._mcp_tool_count = lambda *a, **k: called.append(1) or 91
    try:
        with _c._tools_advertised_lock:          # cold cache = the probing case
            _c._tools_advertised_cache["val"] = None
            _c._tools_advertised_cache["at"] = 0.0
            _c.__dict__["_tools_advertised_refreshing"] = False
        _c.canon_nums()
        for _th in __import__("threading").enumerate():      # let any thread land
            if _th.name == "tools-advertised-refresh" and _th.is_alive():
                _th.join(timeout=10)
        assert not called, (
            "canon_nums() reached the live tools/list gate. Every canon_text() "
            "surface calls this, so a probe here puts a dchub.cloud fetch inside "
            "thousands of tests and the no-network hook fails unit-tests. Pass "
            "peek=True."
        )
    finally:
        _c._mcp_tool_count = orig
        with _c._tools_advertised_lock:
            _c.__dict__["_tools_advertised_refreshing"] = True


def test_the_probe_IS_reachable_without_peek():
    """★ MUST-FAIL CONTROL for the test above, which would pass vacuously if the
    probe could never fire for some unrelated reason (import guard, dead code
    path, a thread that never starts). Same cold cache, same stub — without
    peek=True the probe MUST happen, or the assertion above proves nothing."""
    import threading

    import ai_surface_canon as _c

    called = []
    orig = _c._mcp_tool_count
    _c._mcp_tool_count = lambda *a, **k: (called.append(1), 91)[1]
    try:
        with _c._tools_advertised_lock:
            _c._tools_advertised_cache["val"] = None
            _c._tools_advertised_cache["at"] = 0.0
            _c.__dict__["_tools_advertised_refreshing"] = False
        _c.resolve_tools_advertised_cached()               # no peek
        for _th in threading.enumerate():
            if _th.name == "tools-advertised-refresh" and _th.is_alive():
                _th.join(timeout=10)
        assert called, (
            "the non-peek resolver did NOT probe — so "
            "test_canon_nums_starts_no_live_probe is vacuous and proves nothing."
        )
    finally:
        _c._mcp_tool_count = orig
        with _c._tools_advertised_lock:
            _c._tools_advertised_cache["val"] = None
            _c._tools_advertised_cache["at"] = 0.0
            _c.__dict__["_tools_advertised_refreshing"] = True


def test_peek_still_answers_from_a_warm_cache():
    """Peek must not mean 'always the pin' — a warm cache still wins, which is
    what makes {canon_tools} live at all."""
    import time

    import ai_surface_canon as _c

    with _c._tools_advertised_lock:
        _c._tools_advertised_cache["val"] = _SENTINEL
        _c._tools_advertised_cache["at"] = time.time()
    assert _c.resolve_tools_advertised_cached(peek=True) == _SENTINEL
    assert _c.canon_nums()["{canon_tools}"] == str(_SENTINEL)


def test_the_manifest_route_keeps_the_refreshing_resolver():
    """The request path is where a background refresh BELONGS — that is the only
    thing that ever warms the cache canon_nums() peeks at. If this surface also
    went peek-only, nothing would refresh and {canon_tools} would be the pin
    forever while looking derived."""
    body = _func_src(_CATALOG, "well_known_mcp_server")
    assert "_wk_tools()" in body, "the manifest no longer calls the resolver"
    assert "peek=True" not in body and "peek = True" not in body, (
        "the manifest went peek-only — then nothing warms the cache and every "
        "consumer silently serves the pin."
    )
