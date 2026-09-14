"""/api/v1/surfaces resolves canon PER REQUEST, not at import.

2026-09-13, measured live, cache-busted, same origin, same second:
/api/v1/canon/phrases said 21,800+ facilities while /api/v1/surfaces described
the map surface with "21,500+ distinct data centers".
register_surface(Surface(description=canon_text(...))) is a module-scope
EXPRESSION, not an assignment, so it resolved once, at import, and the
assignment scan in tests/test_canon_resolved_per_request.py could not see it.
The description is a lambda now, rendered by Surface.to_dict(), which is the
dict list_surfaces() spreads into every surface it serves.
"""
import ast
import importlib.util
import json
import pathlib
import re

import pytest

def _surface_brain_from_disk():
    """routes/surface_brain.py loaded FROM DISK, bypassing sys.modules.

    tests/test_market_brief_guard.py installs a stub ModuleType under
    routes.surface_brain at collection time, and an import that finds it tests a
    fake (here, it raised AttributeError in the full suite).
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "routes" / "surface_brain.py"
    spec = importlib.util.spec_from_file_location("_surface_brain_from_disk", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sb = _surface_brain_from_disk()
_PH = re.compile(r"\{canon_[a-z_]+\}")


def _registered_here():
    """{surface_id: placeholders the SOURCE writes} for this module's own registrations."""
    tree = ast.parse(pathlib.Path(sb.__file__).read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        call = getattr(node, "value", None)
        if (isinstance(node, ast.Expr) and isinstance(call, ast.Call)
                and getattr(call.func, "id", None) == "register_surface"):
            sid = next(k.value.value for k in call.args[0].keywords if k.arg == "surface_id")
            out[sid] = sum(len(_PH.findall(n.value)) for n in ast.walk(node)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return out


def test_every_surface_description_follows_a_canon_move(monkeypatch):
    here = _registered_here()
    assert sum(here.values()) >= 1, f"floor: no surface registered here carries canon: {here}"
    monkeypatch.setattr(sb, "canon_text", lambda t: _PH.sub("CANON_MOVED", t) if t else t)
    for sid, expected in here.items():
        got = json.dumps(sb.SURFACES[sid].to_dict()).count("CANON_MOVED")
        assert got == expected, (
            f"surface {sid!r}: the source writes {expected} canon placeholder(s), "
            f"{got} followed a canon move")


def test_every_registered_surface_serialises_without_a_placeholder():
    assert sb.SURFACES, "floor: no surface is registered"
    for sid, surface in sb.SURFACES.items():
        body = json.dumps(surface.to_dict())      # a leaked lambda raises here
        assert not _PH.search(body), f"surface {sid!r} serves a raw placeholder"
