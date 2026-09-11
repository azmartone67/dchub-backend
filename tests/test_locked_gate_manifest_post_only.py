"""tests/test_locked_gate_manifest_post_only.py — the half the boot canary can't see.

main.py's LOCKED_GATE_MANIFEST is headed "If ANY endpoint in this list is
accessible without auth, server REFUSES to start", and verify_tier_gating()
probes every pro/enterprise/free path with `client.get(path)`.

A route that serves only POST answers 405 to that GET. Werkzeug rejects the
METHOD before routing, so the handler's gate is never consulted — yet 405 was
counted as `passed`. /api/autopilot/seo/run sat in the manifest as 'enterprise'
on the strength of that 405 while an anonymous POST ran the full SEO promotion
cycle (run_seo_promotion -> ping_indexnow).

The canary now reports those paths as unverified instead of passed, and does NOT
re-probe them with POST: on a route that turned out to be ungated, the probe
would execute the action on every boot. This test is the replacement coverage —
statically, with no side effects.

House rules: NO DB, NEVER import main.py — the manifest and the routes are read
out of source with `ast`.

Run:  python3 -m pytest tests/test_locked_gate_manifest_post_only.py -q
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Floors. A scan that resolves nothing passes every assertion below it
# vacuously; these pin it to the surface measured when it was written
# (2026-09-11: 43 manifest paths across pro/enterprise/free; 21 of them resolve
# to a handler in main's app — the rest are registered with app.add_url_rule or
# live behind converters, which this reader does not follow; 2 of the 21 are
# POST-only). Floors sit just under the measured values: they exist to catch a
# reader that silently stops finding anything, not to pin an exact surface.
_MIN_MANIFEST_PATHS = 40
_MIN_RESOLVED = 18
_GATED_TIERS = ("pro", "enterprise", "free")


def _manifest() -> dict:
    """LOCKED_GATE_MANIFEST, read out of main.py's source."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == "LOCKED_GATE_MANIFEST"
                   for t in node.targets):
            continue
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            out[k.value] = [e.value for e in v.elts
                            if isinstance(e, ast.Constant)]
        return out
    raise AssertionError("LOCKED_GATE_MANIFEST not found in main.py")


def _serves_a_different_app(src: str, base: str, main_src: str) -> bool:
    """True for a module that builds its OWN Flask app and is imported by
    nothing in main.py.

    verify_tier_gating probes `app.test_client()` — MAIN's app. api_server.py is
    a legacy standalone Flask app: its own POST /api/reports/generate carries
    @optional_auth, but it is not mounted on main's app and never answers that
    manifest path. Holding it to main's manifest would be testing one app's
    routes against another's contract. (The live handler for that path is
    main.py's, @require_plan('pro').)
    """
    if not re.search(r"^\s*app\s*=\s*Flask\(", src, re.M):
        return False
    mod = base[:-3]
    return not re.search(rf"(^|\n)\s*(from {re.escape(mod)} import|import {re.escape(mod)}\b)",
                         main_src)


def _route_table():
    """{path: (methods, rec, funcdef)} for every scanned route in MAIN's app."""
    from routes.route_auth_master_shell import (
        _iter_route_files, _route_handlers, _route_info)
    table = {}
    recs = []
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    for path, base in _iter_route_files():
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        if _serves_a_different_app(src, base, main_src):
            continue
        import os
        rec = {"rel": os.path.relpath(path, _ROOT), "base": base, "src": src,
               "src_lines": src.splitlines(), "tree": tree,
               "handlers": _route_handlers(tree)}
        recs.append(rec)
        for fn in rec["handlers"]:
            paths, methods = _route_info(fn)
            for p in paths:
                table.setdefault(p, []).append((methods, rec, fn))
    return table, recs


def test_manifest_parses_and_resolves():
    """The floor. Without it every assertion here is green on an empty scan."""
    man = _manifest()
    paths = [p for t in _GATED_TIERS for p in man.get(t, [])]
    assert len(paths) >= _MIN_MANIFEST_PATHS, \
        f"only {len(paths)} manifest paths parsed — the reader is broken"
    table, _ = _route_table()
    resolved = [p for p in paths if p.split("?")[0] in table]
    assert len(resolved) >= _MIN_RESOLVED, \
        f"only {len(resolved)}/{len(paths)} manifest paths resolved to a handler"


def test_every_manifest_path_the_canary_cannot_probe_is_gated():
    """A manifest path with no GET handler is scored on a 405 the gate never
    saw. Each one must carry a real gate in source."""
    from routes.route_auth_master_shell import _handler_is_gated, _module_index
    man = _manifest()
    table, recs = _route_table()
    index = _module_index(recs)

    unprobeable, ungated = [], []
    for tier in _GATED_TIERS:
        for raw in man.get(tier, []):
            p = raw.split("?")[0]
            entries = table.get(p)
            if not entries:
                continue
            methods = set()
            for m, _rec, _fn in entries:
                methods |= set(m or {"GET"})
            if "GET" in methods:
                continue                      # the canary's GET really lands
            unprobeable.append(f"{p} (tier={tier}, methods={sorted(methods)})")
            for _m, rec, fn in entries:
                if not _handler_is_gated(fn, rec, index):
                    ungated.append(f"{p} -> {rec['rel']}::{fn.name} (tier={tier})")

    assert unprobeable, (
        "no POST-only manifest paths found — either the manifest reader or the "
        "route table stopped working; this test would then pass vacuously")
    assert not ungated, (
        "manifest path(s) the boot canary scores on a 405 have NO gate in "
        "source: " + "; ".join(ungated))


def test_the_canary_no_longer_counts_405_as_enforcement():
    """The behavioural half: verify_tier_gating must not score a 405 `passed`."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("def verify_tier_gating(")
    body = src[i:i + 6000]
    assert "r.status_code in (401, 403, 404)" in body, \
        "the gated-tier probe no longer uses the 401/403/404 pass set"
    assert "r.status_code in (401, 403, 404, 405)" not in body, \
        "405 is being counted as a gate again — Werkzeug rejects the method " \
        "before the handler, so the gate was never consulted"
    assert "unverified" in body, \
        "verify_tier_gating no longer reports unprobeable manifest paths"
