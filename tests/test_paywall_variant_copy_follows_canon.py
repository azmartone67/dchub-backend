"""The paywall hint's variant copy resolves canon PER RESPONSE, not at import.

★2026-09-13: _VARIANTS["C"][401] wrapped canon_text() inside the module-level
dict, so its facility count resolved once, at import, while every canon cache
was cold. Variant C, a quarter of 401 callers, quoted that boot-time floor for
the life of the process. Copy that carries canon is a lambda now, rendered
where the hint is built and where the admin preview lists the variants.
"""
import ast
import json
import pathlib
import re

import pytest

m = pytest.importorskip("routes.paywall_hint_middleware")
_PH = re.compile(r"\{canon_[a-z_]+\}")
_PAIRS = [(v, s) for v in "ABCD" for s in (401, 403, 429)]


def _placeholders_in_variants_source():
    tree = ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "_VARIANTS")
    return sum(len(_PH.findall(n.value)) for n in ast.walk(node.value)
               if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _admin_preview(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(m.paywall_ab_admin_bp)
    monkeypatch.setattr(m, "_admin_authorized", lambda: True)
    resp = app.test_client().get("/api/v1/admin/funnel-ab/variants")
    assert resp.status_code == 200, resp.status_code
    return resp.get_json()["variants"]


def test_the_hint_and_the_preview_follow_a_canon_move(monkeypatch):
    expected = _placeholders_in_variants_source()
    assert expected >= 1, "floor: no variant copy carries canon, so this proves nothing"
    monkeypatch.setattr(m, "canon_text", lambda t: _PH.sub("CANON_MOVED", t) if t else t)
    hints = [m._agent_quotable_for(v, s) for v, s in _PAIRS]
    assert sum(h.count("CANON_MOVED") for h in hints) == expected, (
        "a paywall hint ignored a canon move — its copy was resolved at import")
    assert json.dumps(_admin_preview(monkeypatch)).count("CANON_MOVED") == expected, (
        "the admin variant preview ignored a canon move")


def test_no_hint_or_preview_ships_a_raw_placeholder(monkeypatch):
    for v, s in _PAIRS:
        hint = m._agent_quotable_for(v, s)
        assert isinstance(hint, str) and not _PH.search(hint), (v, s, hint)
    assert not _PH.search(json.dumps(_admin_preview(monkeypatch)))
