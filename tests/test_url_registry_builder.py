"""Guard routes.url_registry.build_public_url — the single chokepoint every
public dchub.cloud URL must flow through (kills the linkedin_404 bug class).

url_registry.py imports Flask at module load, and the CI unit-tests job
installs only pytest, so we AST-extract just slugify + _KIND_PATH + _BASE +
build_public_url and exec them in an isolated namespace seeded with re — same
pattern as test_marketing_topic_picker.py. Pure-function test, no Flask import.
"""
import os
import re
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UR = os.path.join(ROOT, "routes", "url_registry.py")

_WANT = {"slugify", "build_public_url", "_KIND_PATH", "_BASE", "_VALID_KINDS"}


def _load_builder():
    src = open(UR, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT:
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), UR, "exec"), ns)
    return ns["build_public_url"]


build_public_url = _load_builder()


def test_basic_kinds():
    assert build_public_url("dcpi", "santa-clara") == "https://dchub.cloud/dcpi/santa-clara"
    assert build_public_url("news", "big-news") == "https://dchub.cloud/news/big-news"
    assert build_public_url("markets", "phoenix") == "https://dchub.cloud/markets/phoenix"
    assert build_public_url("facility", "acme-dc") == "https://dchub.cloud/facility/acme-dc"


def test_press_release_uses_hyphenated_path():
    assert build_public_url("press_release", "kkr-deal") == "https://dchub.cloud/press-release/kkr-deal"


def test_slugify_is_idempotent_on_clean_slugs():
    # The whole migration relies on this: a column already holding a clean
    # market slug must round-trip unchanged through the builder.
    for s in ("santa-clara", "northern-virginia", "dallas-fort-worth"):
        assert build_public_url("dcpi", s) == f"https://dchub.cloud/dcpi/{s}"


def test_slugify_handles_non_string_input():
    # facility ids can be ints — must not crash (str() hardening).
    assert build_public_url("facility", 12345) == "https://dchub.cloud/facility/12345"
    # None falls back to 'update', never crashes or emits .../None
    assert build_public_url("dcpi", None) == "https://dchub.cloud/dcpi/update"


def test_subpath_appended_not_slugified():
    assert build_public_url("markets", "phoenix", subpath="brief") == \
        "https://dchub.cloud/markets/phoenix/brief"
    assert build_public_url("markets", "phoenix", subpath="brief.pdf") == \
        "https://dchub.cloud/markets/phoenix/brief.pdf"
    assert build_public_url("markets", "phoenix", subpath="/deep-dive/") == \
        "https://dchub.cloud/markets/phoenix/deep-dive"
    assert build_public_url("markets", "phoenix", subpath="brief/embed") == \
        "https://dchub.cloud/markets/phoenix/brief/embed"


def test_query_string_and_dict():
    assert build_public_url("markets", "phoenix", subpath="brief", query="embed=1") == \
        "https://dchub.cloud/markets/phoenix/brief?embed=1"
    assert build_public_url("markets", "phoenix", subpath="brief", query="?utm_source=embed") == \
        "https://dchub.cloud/markets/phoenix/brief?utm_source=embed"
    assert build_public_url("markets", "phoenix", subpath="brief", query={"embed": 1}) == \
        "https://dchub.cloud/markets/phoenix/brief?embed=1"


def test_invalid_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        build_public_url("bogus", "x")
