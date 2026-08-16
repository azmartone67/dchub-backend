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
    for old in ("2.3.3", "2.5.0", "2.11.1"):
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
