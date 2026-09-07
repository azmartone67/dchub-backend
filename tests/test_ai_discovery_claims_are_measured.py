"""tests/test_ai_discovery_claims_are_measured.py — a discovery surface may not
assert what it has not measured (2026-09-06).

/api/v1/discovery published a claim that did not match reality — a surface
TEACHING agents something false, which is worse than one that says nothing,
because an agent has no way to tell a confident wrong answer from a right one.

  /api/v1/discovery    file_status from os.path.exists('static/<name>')
      Every entry read "active". But static/llms.txt is NOT what serves
      /llms.txt (ai_discovery_routes.serve_llms_txt renders it, and there is a
      third dead copy at the repo root), so the endpoint asserted availability
      off an artifact with no serving path. Wrong in BOTH directions: delete the
      route that serves the surface and it still says "active"; delete the dead
      static file and it says "missing" while the surface is healthy.

What these guards fence, and what they deliberately do not
----------------------------------------------------------
NOT which surfaces happen to be up — that moves. The guards fence the BINDING:
that the published status is reached through a MEASUREMENT rather than a
filesystem stat, and that a surface's URL and its status describe the same
thing. A status check goes green whenever the surfaces are healthy; a binding
check does not.

Why this file execs a slice of main.py instead of importing it
--------------------------------------------------------------
tests/ must not import main (the green-main convention: it opens a DB pool at
import). But a lexical scan cannot prove a STATE MACHINE behaves — and the
three-state contract below is the whole point of the rewrite. So the
reachability helpers are located BY NAME in main.py's AST and exec'd on their
own, with the network probe stubbed. That runs the real shipped code offline.
Binding by AST name, never by a source offset, is deliberate: a fixed slice
measures length, not content, and drifts silently when the file moves.

Run:  python3 -m pytest tests/test_ai_discovery_claims_are_measured.py -v
"""
from __future__ import annotations

import ast
import io
import logging
import pathlib
import threading
import time
import types
from datetime import datetime, timezone

import pytest
import requests as _requests

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"

# The complete set of surfaces /api/v1/discovery publishes. A floor as well as a
# list: a rewrite that quietly drops half the map would otherwise leave every
# assertion below green over a much smaller surface.
_PUBLISHED = {
    "llms.txt", "llms-full.txt", "AGENTS.md", "skill.md", "skill.json",
    "ai.txt", "robots.txt", "agent.json", "ai-agents.json", "ai-plugin.json",
    "mcp.json", "openapi.json", "copilot-agent.json", "security.txt",
}


_TREE_CACHE = []


def _tree():
    """main.py's AST, parsed once per session.

    48k lines is a ~3s parse, and every guard in this file needs it — the AST
    guards directly, the reachability guards to locate the helpers by name. One
    parse, shared.
    """
    if not _TREE_CACHE:
        _TREE_CACHE.append(ast.parse(io.open(_MAIN, encoding="utf-8").read()))
    return _TREE_CACHE[0]


@pytest.fixture(scope="module")
def tree():
    return _tree()


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"main.py no longer defines {name}() — this guard is now blind")


def _dict_value(node, key):
    """The AST node for `key` inside the first dict literal under `node`."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    return v
    return None


# DEFECT 1 (/ai/learn's hand-typed counts) is deliberately NOT guarded here.
# It was fixed on main by #4044/#4049 while this branch was under test — and
# further than this branch had it: `sources: 40` gained a canon owner
# ({canon_news_sources}), was measured ~61x low against a live
# COUNT(DISTINCT source)=2,442, and was renamed to `news_sources` because a bare
# `sources` between `facilities` and `countries` reads as "data sources".
# tests/test_ai_learn_capabilities_derived.py already fences every capability
# value against hand-typed figures and requires each to reach a resolver, so a
# second copy here would be duplicate coverage that drifts out of step with the
# guard that owns it.

# ─────────────────────────────────────────────────────────────────────────────
# DEFECT 2 — /api/v1/discovery file_status
# ─────────────────────────────────────────────────────────────────────────────

def test_discovery_index_does_not_stat_the_filesystem(tree):
    """os.path.exists must not be the oracle for a SERVING claim."""
    fn = _func(tree, "ai_discovery_index")
    src = ast.unparse(fn)
    assert "os.path.exists" not in src, (
        "/api/v1/discovery is back to stating file presence as availability. "
        "static/llms.txt exists and serves nothing; /llms.txt is rendered by "
        "ai_discovery_routes.serve_llms_txt(). Presence is not visibility."
    )
    assert "_discovery_status" in src, "file_status no longer derives from a measurement"


def _protocol_entries(tree):
    """Each `protocols` entry as {key: {field: value-node}}.

    ★2026-09-07: these were built by a `_proto()` helper until the API
    response-key contract reported data.protocols.* UNMEASURED — that guard
    reads dict LITERALS out of the handler, and a factored-out helper made ten
    keys on a published agent surface invisible to it. The literals came back
    and the DERIVATION was factored out instead, so this guard follows the
    values rather than the helper.
    """
    fn = _func(tree, "ai_discovery_index")
    protocols = _dict_value(fn, "protocols")
    assert protocols is not None and isinstance(protocols, ast.Dict), (
        "ai_discovery_index no longer builds `protocols` as a dict literal — the "
        "API response contract cannot see a dynamic one, and neither can this guard")
    out = {}
    for k, v in zip(protocols.keys, protocols.values):
        assert isinstance(k, ast.Constant), "a protocol key is not a literal"
        assert isinstance(v, ast.Dict), (
            f"protocol {k.value!r} is not a dict literal — it would drop out of the "
            "response-key contract (that is what #4055 did and #4056 undid)")
        out[k.value] = {kk.value: vv for kk, vv in zip(v.keys, v.values)
                        if isinstance(kk, ast.Constant)}
    assert out, "no protocol entries found — this guard is blind"
    return out


def test_every_protocol_field_names_the_same_surface(tree):
    """★ THE invariant: url, exists and status must describe ONE surface.

    The defect this whole endpoint was rewritten to remove is a status published
    beside the wrong thing. A protocol entry whose `url` says AGENTS.md and whose
    `exists` reads llms.txt is that defect in miniature, and no amount of correct
    measurement upstream would save it.
    """
    for key, fields in _protocol_entries(tree).items():
        for required in ("url", "exists", "status"):
            assert required in fields, f"protocol {key!r} has no {required!r}"
        named = {}
        for field in ("url", "exists", "status"):
            args = [c.value for c in ast.walk(fields[field])
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            assert len(args) == 1, (
                f"protocol {key!r} field {field!r} does not name exactly one "
                f"surface: {args}")
            named[field] = args[0]
        assert len(set(named.values())) == 1, (
            f"protocol {key!r} mixes surfaces across its own fields: {named} — a "
            "status reported for one thing beside a URL for another")
        assert named["url"] in _PUBLISHED, (
            f"protocol {key!r} names {named['url']!r}, which is not in the surface map")


def test_protocol_urls_are_built_from_the_map(tree):
    """The advertised URL must be the map's path, not a second hand-typed one.

    ★ Added because the first version of this guard SURVIVED its mutation.
    Rewriting the URL to "https://dchub.cloud/.well-known/" + name still
    referenced _DISCOVERY_SURFACES elsewhere in the function and still named
    valid surfaces, so every assertion passed — while the endpoint advertised
    https://dchub.cloud/.well-known/AGENTS.md (a 404) beside AGENTS.md's real
    "active" status.
    """
    fn = _func(tree, "ai_discovery_index")
    for key, fields in _protocol_entries(tree).items():
        src = ast.unparse(fields["url"])
        assert "_durl(" in src, f"protocol {key!r} url is not built from the map: {src}"
        typed = [c.value for c in ast.walk(fields["url"])
                 if isinstance(c, ast.Constant) and isinstance(c.value, str) and "/" in c.value]
        assert not typed, (
            f"protocol {key!r} url carries a hand-typed path fragment {typed}")
    # ...and the one indirection those go through must itself read the map.
    durl = next((n for n in ast.walk(fn)
                 if isinstance(n, ast.FunctionDef) and n.name == "_durl"), None)
    assert durl is not None, "_durl is gone — the url guard above proves nothing"
    body = ast.unparse(durl)
    assert "_paths[" in body, f"_durl no longer reads the surface map: {body}"
    typed = [c.value for c in ast.walk(durl)
             if isinstance(c, ast.Constant) and isinstance(c.value, str) and "/" in c.value]
    assert not typed, f"_durl carries a hand-typed path fragment {typed}"


def test_surface_map_covers_every_published_name():
    """The map is also a floor: a shrunk map must fail, not quietly narrow."""
    g = _load_helpers()
    got = {name for name, _ in g["_DISCOVERY_SURFACES"]}
    assert got == _PUBLISHED, (
        f"surface map drifted: missing {_PUBLISHED - got}, unexpected {got - _PUBLISHED}")
    for _name, path in g["_DISCOVERY_SURFACES"]:
        assert path.startswith("/"), f"{_name} maps to {path!r}, not a public path"


# ── the state machine, run offline against the real shipped helpers ──────────

_WANT_FN = ("_probe_discovery_surface", "_refresh_discovery_reach",
            "_discovery_reachability", "_discovery_status", "_discovery_exists")
_WANT_VAR = ("_DISCOVERY_SURFACES", "_DISCOVERY_PUBLIC", "_DISCOVERY_REACH_TTL_S",
             "_DISCOVERY_REACH_RETRY_S", "_discovery_reach_cache",
             "_discovery_reach_next", "_discovery_reach_running",
             "_discovery_reach_lock")


_PICKED_CACHE = []


def _load_helpers():
    """Exec main.py's reachability helpers, located by AST name, in isolation.

    The parse is shared with the AST guards above (see _tree) but the GLOBALS
    are rebuilt per call — each test must start from a cold cache or
    they leak state into one another and the last-known-good test passes for the
    wrong reason.
    """
    picked = _PICKED_CACHE
    if picked:
        return _exec_picked(picked)
    picked = []
    for node in _tree().body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FN:
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_VAR for t in node.targets):
            picked.append(node)
    found_fn = {n.name for n in picked if isinstance(n, ast.FunctionDef)}
    missing = set(_WANT_FN) - found_fn
    assert not missing, (
        f"main.py no longer defines {sorted(missing)} at module scope — the "
        "reachability guards below would silently test nothing")
    _PICKED_CACHE.extend(picked)
    return _exec_picked(picked)


def _exec_picked(picked):
    mod = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(mod)
    g = {"time": time, "threading": threading, "datetime": datetime,
         "timezone": timezone, "requests": _requests,
         "logger": logging.getLogger("discovery-reach-test")}
    exec(compile(mod, str(_MAIN), "exec"), g)
    return g


def _refresh_with(g, answers):
    """Drive one refresh cycle with a stubbed probe. No network."""
    g["_probe_discovery_surface"] = lambda path, timeout=8: answers(path)
    g["_refresh_discovery_reach"]()
    return dict(g["_discovery_reach_cache"])


def test_a_two_hundred_is_active_and_a_four_oh_four_is_missing():
    g = _load_helpers()
    cache = _refresh_with(g, lambda p: 404 if p == "/skill.md" else 200)
    status = {n: g["_discovery_status"](cache.get(n)) for n, _ in g["_DISCOVERY_SURFACES"]}
    assert status["skill.md"] == "missing", (
        "a 404 must be reported as missing — /skill.md really is 404 at the edge "
        "(the origin serves it 200 and a route-registration check would call it "
        "active, which is exactly the false claim being removed)")
    assert status["llms.txt"] == "active"
    assert g["_discovery_exists"](cache["skill.md"]) is False
    assert g["_discovery_exists"](cache["llms.txt"]) is True


def test_a_probe_that_could_not_run_is_unmeasured_not_missing():
    """★ The failure this rewrite must not introduce.

    If one blocked egress path turned every surface "missing", the endpoint
    would confidently announce that the whole discovery layer is dead — a worse
    lie than the one it replaced. Could-not-run is not ran-and-failed.
    """
    g = _load_helpers()
    cache = _refresh_with(g, lambda p: None)
    for name, _ in g["_DISCOVERY_SURFACES"]:
        assert g["_discovery_status"](cache.get(name)) == "unmeasured", name
        assert g["_discovery_exists"](cache.get(name)) is None, name


def test_a_failed_probe_keeps_the_last_real_measurement():
    g = _load_helpers()
    _refresh_with(g, lambda p: 200)
    cache = _refresh_with(g, lambda p: None)
    assert g["_discovery_status"](cache["llms.txt"]) == "active", (
        "a transport failure overwrote a real measurement; canonical_stats keeps "
        "its last-known-good for the same reason")


def test_a_run_that_measured_nothing_retries_soon():
    """A dead probe must not pin the surface to unmeasured for the full TTL."""
    g = _load_helpers()
    _refresh_with(g, lambda p: None)
    wait_none = g["_discovery_reach_next"] - time.time()
    _refresh_with(g, lambda p: 200)
    wait_some = g["_discovery_reach_next"] - time.time()
    assert wait_none <= g["_DISCOVERY_REACH_RETRY_S"] + 5, wait_none
    assert wait_some > g["_DISCOVERY_REACH_RETRY_S"] + 5, wait_some
    assert wait_some <= g["_DISCOVERY_REACH_TTL_S"] + 5, wait_some


def test_reader_never_blocks_and_never_probes_inline():
    """_discovery_reachability() returns immediately on a cold cache."""
    g = _load_helpers()
    calls = []

    def _boom(path, timeout=8):
        # Sleep past the assertion, then abort the run so a mutated (inline)
        # reader fails in ~2s instead of grinding through all 14 surfaces.
        calls.append(path)
        time.sleep(2.0)
        raise RuntimeError("probe must not run on the request path")

    g["_probe_discovery_surface"] = _boom
    t0 = time.time()
    snap = g["_discovery_reachability"]()
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"the request path blocked for {elapsed:.1f}s on a probe"
    assert snap == {}, "a cold cache must report nothing, not guess"


def test_snapshot_is_a_copy():
    g = _load_helpers()
    _refresh_with(g, lambda p: 200)
    g["_discovery_reach_next"] = time.time() + 10_000     # don't re-kick
    snap = g["_discovery_reachability"]()
    snap["llms.txt"]["code"] = 999
    assert g["_discovery_reach_cache"]["llms.txt"]["code"] == 200


def test_an_http_status_is_a_measurement_and_a_transport_error_is_not():
    """A 404 the server SENT is data; a request that never landed is not.

    Collapsing these is how "unmeasured" would leak back into "missing" through
    the probe rather than through _discovery_status — the same lie, one layer
    down, where the status-mapping tests above cannot see it.
    """
    class _Resp:
        def __init__(self, code):
            self.status_code = code

    g = _load_helpers()
    g["requests"] = types.SimpleNamespace(get=lambda url, **kw: _Resp(404))
    assert g["_probe_discovery_surface"]("/skill.md") == 404

    def _blow_up(url, **kw):
        raise OSError("Network is unreachable")

    g["requests"] = types.SimpleNamespace(get=_blow_up)
    assert g["_probe_discovery_surface"]("/skill.md") is None, (
        "a transport error must be None (unmeasured), never a status code")


def test_the_probe_follows_redirects_and_busts_the_cache():
    """/.well-known/openapi.json 301s to a live /openapi.json — that is reachable.

    And an edge-cached 200 would freeze this endpoint's answer, so the probe
    must not be answerable from cache.
    """
    seen = {}

    class _Resp:
        status_code = 200

    def _get(url, **kw):
        seen["url"] = url
        seen["kw"] = kw
        return _Resp()

    g = _load_helpers()
    g["requests"] = types.SimpleNamespace(get=_get)
    g["_probe_discovery_surface"]("/.well-known/openapi.json")
    assert seen["kw"].get("allow_redirects") is True, (
        "a 301 to a live sibling still serves the agent; not following it "
        "reports a reachable surface as missing")
    assert "cb=" in seen["url"], "the probe is answerable from the edge cache"
    assert seen["kw"]["headers"]["User-Agent"].startswith("dchub-"), (
        "self-traffic must be identifiable in the logs")
