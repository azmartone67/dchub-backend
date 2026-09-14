"""The seven /integrations/<platform> recipe pages resolve canon PER REQUEST.

2026-09-13, measured live, cache-busted, same origin, same second:
/api/v1/canon/phrases said 21,800+ facilities while /integrations/grok and
/integrations/cloudflare served 21,500+. Each page was a module constant built
at import by _recipe_page(), over a canon_text()-resolved shared template and
slots wrapped in canon_text(), so canon froze while every cache was cold.

A value comparison against canonical_stats reads the same latched value the
page does and passes either way. These tests MOVE the canon and require every
placeholder the source writes, in the shared template and in the page's own
slots, to follow it into the served body.
"""
import ast
import pathlib
import re

import pytest

il = pytest.importorskip("routes.integrations_landing")

_PAGES = [  # (route, slot dict)
    ("/integrations/bedrock", "_BEDROCK_RECIPE"),
    ("/integrations/cloudflare", "_CLOUDFLARE_PORTAL_RECIPE"),
    ("/integrations/copilot-studio", "_COPILOT_RECIPE"),
    ("/integrations/gemini", "_GEMINI_RECIPE"),
    ("/integrations/grok", "_GROK_RECIPE"),
    ("/integrations/mistral", "_MISTRAL_RECIPE"),
    ("/integrations/perplexity", "_PERPLEXITY_RECIPE"),
]
_PH = re.compile(r"\{canon_[a-z_]+\}")


def _placeholders_in_source(*names):
    """{canon_*} placeholders the SOURCE writes into these module assignments."""
    tree = ast.parse(pathlib.Path(il.__file__).read_text(encoding="utf-8"))
    counts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in names):
            counts[node.targets[0].id] = sum(
                len(_PH.findall(n.value)) for n in ast.walk(node.value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert set(counts) == set(names), f"not assigned in the module: {set(names) - set(counts)}"
    return counts


def _serve(route):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(il.integrations_landing_bp)
    resp = app.test_client().get(route)
    assert resp.status_code == 200, f"{route} answered {resp.status_code}"
    return resp.get_data(as_text=True)


@pytest.mark.parametrize("route,slots", _PAGES)
def test_every_placeholder_follows_a_canon_move(monkeypatch, route, slots):
    counts = _placeholders_in_source("_RECIPE_PAGE_TEMPLATE", slots)
    # Floor: the shared template and the page's own slots both carry canon, so
    # each half of the page is under test, not just one.
    assert counts["_RECIPE_PAGE_TEMPLATE"] >= 1 and counts[slots] >= 1, counts
    monkeypatch.setattr(il, "canon_text",
                        lambda t: _PH.sub("CANON_MOVED", t) if t else t)
    html = _serve(route)
    assert html.count("CANON_MOVED") == sum(counts.values()), (
        f"{route}: the source writes {sum(counts.values())} canon placeholders "
        f"{counts} but only {html.count('CANON_MOVED')} followed a canon move. "
        f"The rest were resolved at import and are frozen for the process.")


@pytest.mark.parametrize("route,slots", _PAGES)
def test_the_served_page_ships_no_placeholder_or_slot_token(route, slots):
    html = _serve(route)
    assert not _PH.search(html), f"{route} served a raw canon placeholder"
    assert not re.findall(r"__[A-Z0-9_]+__", html), f"{route} served an unfilled slot token"
