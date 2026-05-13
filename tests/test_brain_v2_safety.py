"""Brain v2 Layer 4 safety tests.

The pure-function safety logic in routes/brain_v2_layer4.py guards
us against:
  - Claude returning 1-char `find` strings that would nuke typography
  - The auto-expand grabbing prose em-dashes instead of placeholders
  - Misclassifying explicit refusals as validation failures

These tests cover the exact bug categories that have hit production
this week (PR #49 unblocked the brain; this prevents regression).

Imports the pure functions directly via source extraction so we don't
have to pull in Flask or its blueprint chain just to test 50 lines.
"""
import os
import re
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYER4 = os.path.join(ROOT, "routes", "brain_v2_layer4.py")


def _extract(name: str):
    """Pull a top-level function out of brain_v2_layer4.py by name and
    exec it in an isolated namespace. Avoids importing the Flask app.

    Preloads `re` because _validate_proposal uses re.search internally
    for the JS-payload guard; without it the function body raises
    NameError at first call.
    """
    src = open(LAYER4, encoding="utf-8").read()
    m = re.search(
        rf"^def {name}\(.*?(?=^def |^class |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    if not m:
        raise RuntimeError(f"function {name} not found")
    # Preload the stdlib modules the brain helpers depend on.
    import re as _re
    ns = {"re": _re}
    exec(m.group(0), ns)
    return ns[name]


@pytest.fixture(scope="module")
def validate():
    return _extract("_validate_proposal")


@pytest.fixture(scope="module")
def expand():
    return _extract("_auto_expand_find")


# ── _validate_proposal ─────────────────────────────────────────────

def test_validate_rejects_empty_find_as_refused(validate):
    ok, reason = validate("", "anything")
    assert not ok
    assert reason == "refused"


def test_validate_rejects_short_find(validate):
    """Phase NN bumped floor to 8 chars; '—' (1) and '>—<' (3) must fail."""
    for short in ("—", ">—<", "abc"):
        ok, reason = validate(short, "replacement")
        assert not ok, f"should reject {short!r}"
        assert reason == "find_too_short"


def test_validate_rejects_noop(validate):
    ok, reason = validate('<td class="v">—</td>', '<td class="v">—</td>')
    assert not ok
    assert reason == "noop"


def test_validate_rejects_empty_replace(validate):
    ok, reason = validate('<td class="v">—</td>', "")
    assert not ok
    assert reason == "empty_replace"


def test_validate_rejects_replace_introducing_html(validate):
    """If find has no `<` or `>`, replace can't introduce them either —
    guards against the model inventing tags."""
    ok, reason = validate("Loading...", "<script>x</script>")
    assert not ok
    assert reason == "replace_introduces_html"


def test_validate_rejects_js_payload(validate):
    ok, reason = validate('<td class="v">—</td>',
                          '<td onload="alert(1)">x</td>')
    assert not ok
    assert reason == "replace_has_js"


def test_validate_accepts_well_formed_proposal(validate):
    """Real, safe proposal — should pass cleanly."""
    ok, reason = validate('<td class="v">—</td>',
                          '<td class="v">12.5</td>')
    assert ok, reason
    assert reason == "ok"


# ── _auto_expand_find ──────────────────────────────────────────────

def test_expand_picks_placeholder_not_typography(expand):
    """The single biggest regression risk: snippet contains BOTH a
    typography em-dash (in H1 prose) and a placeholder em-dash
    (sole content of a leaf div). Auto-expand must pick the div."""
    snippet = (
        '<html><body>'
        '<h1>DC Hub Media — the autonomous feed</h1>'
        '<div class="big-num" id="stat-total">—</div>'
        '</body></html>'
    )
    out = expand("—", snippet)
    # Must NOT expand into the H1 (which would break headings on every page)
    assert "DC Hub Media" not in out
    # Must hit the placeholder div
    assert "stat-total" in out


def test_expand_refuses_when_only_prose(expand):
    """If every em-dash is inside prose, refuse to expand — better to
    let validation reject than apply a typography-mangling fix."""
    snippet = (
        '<h1>DC Hub Media — the autonomous feed</h1>'
        '<p>Another sentence with — in it.</p>'
    )
    out = expand("—", snippet)
    # Should pass through unchanged so the validator rejects it.
    assert out == "—"


def test_expand_preserves_already_specific_find(expand):
    """Long, target-specific find — don't expand further."""
    long_find = '<td class="v">—</td>'
    out = expand(long_find, '<table><tr>' + long_find + '</tr></table>')
    assert out == long_find


def test_expand_returns_original_when_no_match(expand):
    """find doesn't even appear in snippet — pass through."""
    out = expand("NOT_IN_SNIPPET", "<html><body><div>foo</div></body></html>")
    assert out == "NOT_IN_SNIPPET"


def test_expand_returns_empty_for_empty_find(expand):
    """Empty find = explicit refusal contract; pass through untouched."""
    out = expand("", "<div>—</div>")
    assert out == ""


def test_expand_handles_span_leaf(expand):
    """Other leaf elements (span) should also expand correctly."""
    snippet = '<p>Total: <span class="v">—</span> kWh</p>'
    out = expand("—", snippet)
    assert out.startswith("<span")
    assert "—" in out
    assert out.endswith("</span>")
