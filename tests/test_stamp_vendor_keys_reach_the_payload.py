"""Every key _stamp_vendor RETURNS must be published at EVERY call site.

★ THE DEFECT THIS EXISTS BECAUSE OF (2026-09-07, shipped in #4189 and live for
~40 minutes before anyone noticed). _stamp_vendor gained three keys —
unrecognised_by_class_{ids,requests,basis} — and both call sites copy keys
BY NAME, one line each. The new keys were computed on every request and thrown
away. The endpoint looked exactly as it had before.

Key-by-key copying is deliberate and must stay: one `out.update()` turned
/api/v1/ai/reach from "resolved" to "opaque" and dropped 18 keys out of
contract coverage. So the fix is not update() — it is a guard that notices when
the two lists drift apart.

★ WHY THE FIRST GUARD MISSED IT. It asserted the key NAMES appeared in
routes/ai_reach.py. They did — inside the helper's return statement. Presence
in the file is not publication, and a substring check cannot tell the
difference. This one is AST-bound and compares two derived SETS, so it fails
the moment a key is returned and not copied.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "ai_reach.py"


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _returned_keys(tree: ast.Module) -> set[str]:
    """String keys of the dict _stamp_vendor returns."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_stamp_vendor":
            for r in ast.walk(node):
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Dict):
                    return {k.value for k in r.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("_stamp_vendor or its returned dict literal not found")


def _published_per_site(tree: ast.Module) -> list[set[str]]:
    """For each `_vsum = _stamp_vendor(...)`, the keys copied onto `out` in the
    SAME enclosing function."""
    sites = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigns_vsum = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_vsum" for t in n.targets)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "_stamp_vendor"
            for n in ast.walk(fn))
        if not assigns_vsum:
            continue
        copied = set()
        for n in ast.walk(fn):
            # out["k"] = _vsum["k"]
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1):
                continue
            t, v = n.targets[0], n.value
            if not (isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == "out"):
                continue
            if not (isinstance(v, ast.Subscript) and getattr(v.value, "id", None) == "_vsum"):
                continue
            if isinstance(t.slice, ast.Constant) and isinstance(v.slice, ast.Constant):
                assert t.slice.value == v.slice.value, (
                    "a key is copied under a different name: out[%r] = _vsum[%r]"
                    % (t.slice.value, v.slice.value))
                copied.add(t.slice.value)
        sites.append(copied)
    return sites


def test_every_returned_key_is_published_at_every_call_site():
    tree = _tree()
    returned = _returned_keys(tree)
    sites = _published_per_site(tree)
    assert sites, "no _stamp_vendor call site found — this guard is watching nothing"
    for i, copied in enumerate(sites):
        missing = returned - copied
        assert not missing, (
            "call site %d computes these keys and never publishes them, so the "
            "endpoint silently omits them: %s\n"
            "Copy them onto `out` by name (NOT out.update() — that dropped 18 "
            "keys out of contract coverage)." % (i + 1, sorted(missing)))


def test_the_guard_sees_more_than_one_key_and_more_than_one_site():
    """★ Both halves must be non-trivial or the assertion above proves nothing:
    an empty `returned` set makes every difference empty and passes forever."""
    tree = _tree()
    returned = _returned_keys(tree)
    sites = _published_per_site(tree)
    assert len(returned) >= 4, (
        "only %d key(s) parsed out of _stamp_vendor's return; the extractor has "
        "probably broken and the check above is vacuous" % len(returned))
    assert len(sites) >= 2, (
        "only %d call site(s) found; ai_reach had two on 2026-09-07 and a "
        "missed site is exactly how a key goes unpublished" % len(sites))
