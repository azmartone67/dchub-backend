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
    """_wk_canon_version() must return the canon, not a baked string."""
    from ai_surface_canon import PINNED
    src = _func_src(_MAIN, "_wk_canon_version")
    ns: dict = {}
    exec(compile(src, "<_wk_canon_version>", "exec"), ns)
    assert ns["_wk_canon_version"]() == PINNED["version"]


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
