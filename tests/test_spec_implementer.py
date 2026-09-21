"""Guard the spec→implementation actuator.

The loop this breaks: the code drafter refuses a vague directive → the spec
fallback files a doc → the doc merges → next cycle the landed-spec dedup
declines with "needs an implementation, not another spec" → nothing consumes
that. Measured 2026-09-21: 106 of 111 brain-spec merges changed only docs/,
238 of 376 landed specs carry open obligations, and all 30 declined board
approvals read that same note.

Three things here are load-bearing and each has burned someone already:

1. ★ IDENTIFIERS MUST SURVIVE. The directive is an instruction to a CODE
   drafter. The first version stripped `[*_`]+` as "markdown emphasis" and
   turned "backfill bar_table" into "backfill bartable" — a mangled
   identifier is a wrong edit, not a cosmetic slip.
2. ★ THE DIRECTIVE MUST FORBID ANOTHER SPEC. Filing a spec is the drafter's
   cheapest exit and is precisely what created the loop.
3. ★ ACTUATION NEEDS TWO SWITCHES. apply=True alone must not open a PR.

Harness: the CI unit-tests job installs pytest ONLY, so the module (Flask)
cannot be imported. The pure helpers are AST-extracted and exec'd — the real
ones, not a reimplementation.
"""
import os
import ast
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "brain_spec_implementer.py")
DASH = os.path.join(ROOT, "routes", "brain_innovation_dashboard.py")

_FUNCS = ("unchecked_obligations", "build_directive")
_NAMES = ("_UNCHECKED_ITEM_RE", "_MIN_OBLIGATION_CHARS", "_MAX_PER_CALL")


def _load():
    src = open(MOD, encoding="utf-8").read()
    pieces = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            pieces.append(ast.get_source_segment(src, node))
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), MOD, "exec"), ns)
    missing = [n for n in _FUNCS + _NAMES if n not in ns]
    assert not missing, f"AST extraction missed {missing} — update _FUNCS/_NAMES"
    return ns


NS = _load()

SPEC = """# Brain proposal — make the emitter honest
- [ ] Add a `spec_debt` column to brain_pr_outcomes and backfill it
- [x] this one is already done and must not appear
- [ ] tiny
- [ ] Wire **the** `emitter` so it writes on every tick, not only on change
"""


def test_only_unchecked_obligations_are_taken():
    o = NS["unchecked_obligations"](SPEC)
    assert len(o) == 2, o
    assert not any("already done" in x for x in o), "a CHECKED item leaked in"
    assert not any(x == "tiny" for x in o), "a sub-floor item leaked in"


def test_snake_case_identifiers_survive():
    """The regression that motivated this test: bar_table -> bartable."""
    o = NS["unchecked_obligations"](SPEC)
    joined = " ".join(o)
    assert "spec_debt" in joined, (
        "an underscore identifier was mangled — this string is an instruction "
        "to a code drafter, so that is a wrong edit")
    assert "brain_pr_outcomes" in joined
    assert "bartable" not in joined and "specdebt" not in joined


def test_markdown_emphasis_and_backticks_are_stripped():
    o = NS["unchecked_obligations"](SPEC)
    joined = " ".join(o)
    assert "**" not in joined and "`" not in joined
    assert "the emitter" in joined


def test_empty_and_checklistless_specs_yield_nothing():
    for text in ("", "# Just a title\n\nProse only, no checklist.\n",
                 "- [x] all done\n- [x] also done\n"):
        assert NS["unchecked_obligations"](text) == []


def test_directive_forbids_filing_another_spec():
    """Without this line the drafter's cheapest exit is the spec fallback —
    which is the loop, with an extra step."""
    d = NS["build_directive"]("agenda-41-x.md", "make it honest",
                              ["Add a spec_debt column", "Wire the emitter up"])
    low = d.lower()
    assert "do not file another spec" in low
    assert "implement" in low
    assert "agenda-41-x.md" in d, "the directive must name the spec it implements"
    assert "Add a spec_debt column" in d, "obligations must reach the directive"


def test_directive_is_bounded():
    d = NS["build_directive"]("s.md", "t", [f"obligation number {i} with text"
                                            for i in range(40)])
    assert "obligation number 11" in d
    assert "obligation number 13" not in d, "directive must cap at 12 steps"


def test_sweep_cap_is_small():
    assert 1 <= NS["_MAX_PER_CALL"] <= 5, (
        "an unbounded sweep over 238 open obligations would open hundreds of "
        "PRs on one button press")


# ── The dashboard wiring ─────────────────────────────────────────────────

def test_declined_offers_implement_not_override():
    """★ can_override on `declined` would be an INERT BUTTON: override is only
    consulted against the verdict gate, and this decline comes from the
    drafter's fingerprint dedup. Assert the dedup branch sets can_implement
    and does NOT set can_override."""
    src = open(DASH, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "approval_view")
    seg = ast.get_source_segment(src, fn)
    i = seg.index('pr_state="declined"')
    tail = seg[i:i + 1400]
    assert "can_implement=True" in tail, "declined offers no next action"
    assert "can_override=True" not in tail, (
        "declined sets can_override — that button cannot work, because "
        "override only bypasses the verdict gate")


def test_implement_button_uses_the_pages_own_auth_helpers():
    """An earlier draft called adminKey(), which this page does not define —
    the button would have thrown ReferenceError on first click."""
    src = open(DASH, encoding="utf-8").read()
    # Anchor on the click HANDLER, not the button markup — they are ~80 lines
    # apart and a window from the markup never reaches the fetch.
    i = src.index("act === 'implement'")
    handler = src[i:i + 1800]
    assert "spec-debt/implement" in handler, "anchored on the wrong block"
    assert "authq(" in handler and "authh()" in handler
    assert "adminKey(" not in handler, "adminKey() is not defined on this page"
    for name in ("adminKey", "refresh"):
        assert f"function {name}(" not in src, (
            f"{name}() now exists — re-check the implement handler")
