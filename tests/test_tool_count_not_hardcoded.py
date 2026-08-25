"""A public surface must not hand-type the tool count (2026-08-25).

FOUND WHILE CLEANING UP STALE WORKTREES, not while looking for it. An abandoned
branch held a 3-month-old edit changing "40 tools" to "48 tools" across nine
marketing surfaces. The edit was dead — main had already moved past "40" — but
checking whether it still mattered surfaced that FIVE files shipped a literal
tool count while the live server served **82**:

    routes/competitive_vs.py        "YES — 48 tools, 12 AI platforms integrated"
    routes/industry_pulse.py        "auto-discover our 48 tools ..."
    routes/monthly_trend.py         "(48 tools for AI agents)"
    routes/partnerships_page.py     "auto-discover our 48 tools ..."

A competitor-comparison page under-claiming our own surface by 34 tools.

routes/dynamic_hero.py had already been swept on 2026-07-27 and its own comment
names this exact drift ("48 tools (live 80)") — one file fixed, four neighbours
missed. That is the shape this guard exists to stop: a sweep that lands on some
surfaces and silently leaves the rest.

★ A COUNT, NOT A DENYLIST. ai_surface_canon's stale_markers denylist could not
hold this: a denylist matches STRINGS, and the next drift is a different string
("82 tools", once we ship the 83rd). This asserts the SHAPE — no bare numeral
may precede the word "tools" in anything a module can serve.

★ AST, NOT LINE-SCANNING. The first cut of this guard scanned raw lines and
skipped anything starting with '#'. It then flagged mcp_registry_outreach.py,
where a MULTI-LINE comment describing this very drift has continuation lines
that do not start with '#'. A guard that flags the documentation of a fix as
the bug is one people learn to ignore.

Pure: no DB, no network beyond the canon resolver's own fail-open.
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SURFACES = [
    "routes/competitive_vs.py",
    "routes/industry_pulse.py",
    "routes/dynamic_hero.py",
    "routes/monthly_trend.py",
    "routes/partnerships_page.py",
    "routes/mcp_registry_outreach.py",
]

# "48 tools", "82  tools", and — the gap the first cut had — "48 MCP tools",
# where one qualifier sits between the numeral and the noun. monthly_trend.py:216
# shipped exactly that form and the narrow pattern walked straight past it; the
# repo's own stale-count scanner caught it and this one did not.
_HARDCODED = re.compile(r"\b\d{2,3}\s+(?:[A-Za-z][\w-]{0,9}\s+)?tools\b", re.I)


def _rendered_strings(rel):
    """Every string the module can actually SERVE, with its line number.

    Comments are invisible to the AST, which is the point. Docstrings are
    excluded explicitly for the same reason: they explain, they do not ship.
    """
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return None
    tree = ast.parse(open(path, encoding="utf-8").read())

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("rel", _SURFACES)
def test_no_surface_hand_types_the_tool_count(rel):
    strings = _rendered_strings(rel)
    if strings is None:
        pytest.skip(f"{rel} no longer exists")
    offenders = []
    for ln, val in strings:
        m = _HARDCODED.search(val)
        if m:
            ctx = val[max(0, m.start() - 40):m.end() + 20].replace("\n", " ")
            offenders.append(f"{rel}:{ln}: {m.group(0)!r} in ...{ctx!r}")
    assert not offenders, (
        "a bare tool count is being shipped — resolve it from canon instead "
        "({canon_tools} via ai_surface_canon.canon_text, or a local for "
        "f-string bodies):\n" + "\n".join(offenders))


def test_the_detector_would_actually_catch_the_regression():
    """★ NEGATIVE CONTROL. A guard whose pattern does not match the very string
    it was written for protects nothing — and a real one shipped for months."""
    assert _HARDCODED.search('"mcp_native": "YES — 48 tools, 12 AI platforms"')
    assert _HARDCODED.search("auto-discover our 82 tools without manual integration")
    assert not _HARDCODED.search("auto-discover our {canon_tools} tools")
    assert not _HARDCODED.search("auto-discover our {tool_count} tools")
    assert not _HARDCODED.search("a handful of tools")
    # ★ the gap that shipped: one qualifier between the numeral and the noun
    assert _HARDCODED.search("AI-agent native (48 MCP tools) vs human PDF only")
    assert not _HARDCODED.search("native ({canon_tools} MCP tools) vs human PDF")


def test_comments_describing_the_drift_are_not_flagged():
    """★ THE FALSE POSITIVE THAT MADE THE FIRST CUT UNUSABLE. Documenting a fix
    must never read as committing the bug."""
    import tempfile
    src = ('# this was a hardcoded "48 tools" while the live server\n'
           '# served 82 tools — resolved from canon now\n'
           'X = "our {canon_tools} tools"\n')
    with tempfile.NamedTemporaryFile("w", suffix=".py", dir=ROOT, delete=False) as fh:
        fh.write(src); tmp = fh.name
    try:
        found = [v for _, v in _rendered_strings(os.path.basename(tmp))
                 if _HARDCODED.search(v)]
        assert not found, found
    finally:
        os.unlink(tmp)


def test_the_canon_placeholder_actually_resolves():
    """The swap is only a fix if {canon_tools} produces a number. If canon
    cannot be read the contract is a COUNT-FREE sentence, never a wrong one —
    so an empty resolution is acceptable, a literal placeholder is not."""
    from ai_surface_canon import canon_text
    out = canon_text("{canon_tools}")
    assert "{canon_tools}" not in out, "placeholder leaked unresolved"
    assert out == "" or out.isdigit(), f"unexpected canon value: {out!r}"
