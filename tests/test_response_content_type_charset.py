"""No `Response(..., mimetype='<type>; charset=...')` anywhere in production code.

THE BUG. Werkzeug appends `; charset=utf-8` to a `text/*` mimetype ITSELF, so
passing a full content-type into `mimetype=` double-appends and the response
ships a malformed header:

    Response(x, mimetype='text/plain; charset=utf-8')
        -> Content-Type: text/plain; charset=utf-8; charset=utf-8

Measured live 2026-09-02, before the sweep, on 10 public paths including
/agent (Agent Concierge), /partners, /state-of-2026, /vertex, /sites/value,
/interconnection-queues and four /reports/*.md surfaces -- all served the
doubled header, several of them alongside `nosniff`.

`content_type=` sets the header verbatim and is the correct parameter. It is
also the safe swap here: every affected call site was `Response(...)`, which
accepts it -- note `send_file()` does NOT, so this rule is about Response.

WHY THE SCAN IS AST-BASED, not a grep. A regex over source text matches its own
pattern literal, the prose in this docstring, and any comment discussing the
bug -- the "never allow-list prose about a pattern into that pattern" trap. An
AST walk sees only real keyword arguments, so the fence can describe itself
without tripping itself.
"""
import ast
import pathlib

import pytest
from flask import Flask, Response

REPO = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "tests", "build", "dist"}

# Types Werkzeug adds a charset to on its own. Kept narrow deliberately: the
# `application/*` and `+xml` forms do NOT double today, so listing them here
# would assert something untrue.
SELF_CHARSET_PREFIXES = ("text/",)


def _production_sources():
    for path in sorted(REPO.rglob("*.py")):
        if SKIP_DIRS.isdisjoint(path.relative_to(REPO).parts):
            yield path


def _mimetype_charset_sites(path):
    """Every `mimetype='...charset...'` keyword in a call, via AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "mimetype"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and "charset" in kw.value.value
            ):
                yield node.lineno, kw.value.value


def test_werkzeug_still_double_appends():
    """Pin the behaviour the rule exists for.

    If this ever fails, Werkzeug changed and the rule below deserves a re-read
    rather than blind obedience.
    """
    app = Flask(__name__)
    with app.test_request_context():
        doubled = Response("x", mimetype="text/plain; charset=utf-8")
        correct = Response("x", content_type="text/plain; charset=utf-8")
    assert doubled.headers["Content-Type"] == "text/plain; charset=utf-8; charset=utf-8"
    assert correct.headers["Content-Type"] == "text/plain; charset=utf-8"


def test_no_mimetype_carries_a_charset():
    offenders = [
        f"{path.relative_to(REPO)}:{lineno}  mimetype={value!r}"
        for path in _production_sources()
        for lineno, value in _mimetype_charset_sites(path)
    ]
    assert not offenders, (
        "mimetype= must not carry parameters -- Werkzeug appends its own charset "
        "to text/* and the response ships 'charset=utf-8; charset=utf-8'. "
        "Use content_type= instead (Response accepts it; send_file does not):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("prefix", SELF_CHARSET_PREFIXES)
def test_the_doubling_is_specific_to_text_types(prefix):
    """Guard the scope claim, so the rule is not over- or under-stated."""
    app = Flask(__name__)
    with app.test_request_context():
        text_ct = Response("x", mimetype=f"{prefix}plain; charset=utf-8").headers["Content-Type"]
        json_ct = Response("x", mimetype="application/json; charset=utf-8").headers["Content-Type"]
    assert text_ct.count("charset") == 2, "text/* should double -- rule premise broken"
    assert json_ct.count("charset") == 1, "application/json unexpectedly doubles"
