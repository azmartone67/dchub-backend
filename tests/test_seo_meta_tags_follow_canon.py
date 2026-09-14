"""/api/seo/meta-tags/* and /api/seo/head-snippet resolve canon PER REQUEST.

2026-09-13, measured live, cache-busted, same origin, same second:
/api/v1/canon/phrases said 21,800+ facilities while /api/seo/meta-tags/home,
/api/seo/meta-tags/all and /api/seo/head-snippet served 21,500+. HOME_META and
TOOL_META wrapped canon_text() around their strings at module scope, which runs
once, at import, while every canon cache is cold.

A value comparison against canonical_stats reads the same latched value and
passes either way. These tests MOVE the canon and require every placeholder
the source writes to follow it into the served JSON.
"""
import ast
import json
import pathlib
import re

import pytest

smt = pytest.importorskip("seo_meta_tags")
_PH = re.compile(r"\{canon_[a-z_]+\}")


def _source_counts():
    """{page: placeholders the SOURCE writes} for HOME_META and each TOOL_META slug."""
    tree = ast.parse(pathlib.Path(smt.__file__).read_text(encoding="utf-8"))

    def count(node):
        return sum(len(_PH.findall(n.value)) for n in ast.walk(node)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))

    counts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "HOME_META":
                counts["home"] = count(node.value)
            elif node.targets[0].id == "TOOL_META":
                for k, v in zip(node.value.keys, node.value.values):
                    counts[f"tool/{k.value}"] = count(v)
    return counts


def _client():
    from flask import Flask
    app = Flask(__name__)
    smt.setup_meta_routes(app)
    return app.test_client()


def test_every_meta_placeholder_follows_a_canon_move(monkeypatch):
    counts = _source_counts()
    # Floor: home and the tool pages both carry canon, or this proves nothing.
    assert counts.get("home") and sum(counts.values()) > counts["home"], counts
    monkeypatch.setattr(smt, "canon_text",
                        lambda t: _PH.sub("CANON_MOVED", t) if t else t)
    client = _client()
    for page, expected in counts.items():
        meta = client.get(f"/api/seo/meta-tags/{page}").get_json()["meta"]
        got = json.dumps(meta).count("CANON_MOVED")
        assert got == expected, (
            f"/api/seo/meta-tags/{page}: the source writes {expected} canon "
            f"placeholder(s), {got} followed a canon move")
    pages = client.get("/api/seo/meta-tags/all").get_json()["pages"]
    assert json.dumps(pages).count("CANON_MOVED") == sum(counts.values())
    assert "CANON_MOVED" in client.get("/api/seo/head-snippet?page=home").get_data(as_text=True)


def test_the_meta_routes_serve_no_raw_placeholder():
    client = _client()
    paths = ["/api/seo/meta-tags/all", "/api/seo/head-snippet?page=home"]
    paths += [f"/api/seo/meta-tags/{page}" for page in _source_counts()]
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} answered {resp.status_code}"
        assert not _PH.search(resp.get_data(as_text=True)), f"{path} served a raw placeholder"
