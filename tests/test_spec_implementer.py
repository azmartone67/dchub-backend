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

_FUNCS = ("unchecked_obligations", "build_directive",
          "approved_recommendation", "is_blocked")
_NAMES = ("_UNCHECKED_ITEM_RE", "_MIN_OBLIGATION_CHARS", "_MAX_PER_CALL",
          "_BOILERPLATE_OBLIGATIONS", "_REC_RE", "_BLOCKED_RE")


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
                              "Move the emitter onto the tick path.",
                              ["Add a spec_debt column", "Wire the emitter up"])
    low = d.lower()
    assert "do not file another spec" in low
    assert "implement" in low
    assert "agenda-41-x.md" in d, "the directive must name the spec it implements"
    assert "Add a spec_debt column" in d, "obligations must reach the directive"


def test_directive_is_bounded():
    d = NS["build_directive"]("s.md", "t", "rec",
                              [f"obligation number {i} with text"
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


# ── The boilerplate correction (found by the dry run, 2026-09-21) ────────

REAL_SPEC = """<!-- fingerprint:abc -->
# Brain proposal — [reliability] something broke

> Auto-captured from an **approved** brain agenda item (#50).

_Filed 2026-07-03T00:00:00Z · agenda #50_

## The approved recommendation

Move /admin/* drift monitoring from content-hash to structural-diff with
volatile-region exclusions, and add signature-based dedup to the findings
pipeline.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
"""


def test_the_template_checklist_is_stripped():
    """★ THE BUG THE DRY RUN CAUGHT. 242 of 242 open specs carry ONLY these
    four lines. Building a directive from them produced 'Make the smallest
    real code change that satisfies: 1. Confirm this is still worth doing'."""
    assert NS["unchecked_obligations"](REAL_SPEC) == [], (
        "boilerplate checklist items reached the directive")


def test_the_recommendation_is_what_carries_the_content():
    rec = NS["approved_recommendation"](REAL_SPEC)
    assert "structural-diff" in rec and "signature-based dedup" in rec
    assert "Auto-captured" not in rec, "blockquote framing leaked in"
    assert "_Filed" not in rec, "the filed stamp leaked in"
    assert "Confirm this is still worth doing" not in rec, "checklist leaked in"


def test_directive_carries_the_recommendation_not_the_template():
    d = NS["build_directive"]("s.md", "t",
                              NS["approved_recommendation"](REAL_SPEC),
                              NS["unchecked_obligations"](REAL_SPEC))
    assert "structural-diff" in d
    assert "Confirm this is still worth doing" not in d
    assert "OUTSTANDING OBLIGATIONS" not in d, (
        "an empty obligation list must not render an empty section")


def test_blocked_specs_are_detected():
    assert NS["is_blocked"]("## Triage\n\n**BLOCKED** — needs an owner call")
    assert not NS["is_blocked"](REAL_SPEC)


def test_boilerplate_set_still_matches_the_real_corpus():
    """★ PINNED AGAINST THE CORPUS. If the spec filer rewords its checklist,
    the set stops matching, boilerplate flows into directives again, and every
    other test here still passes because they use a fixture. This is the only
    guard that would notice."""
    corpus = os.path.join(ROOT, "docs", "brain-proposals")
    if not os.path.isdir(corpus):
        import pytest
        pytest.skip("corpus not present in this checkout")
    names = sorted(n for n in os.listdir(corpus) if n.endswith(".md"))
    assert len(names) >= 200, (
        f"only {len(names)} specs found — the corpus moved and this guard "
        "would pass vacuously")
    leaked, scanned_open = [], 0
    for n in names[:400]:
        try:
            t = open(os.path.join(corpus, n), encoding="utf-8",
                     errors="replace").read()
        except Exception:
            continue
        if "- [ ]" not in t:
            continue
        scanned_open += 1
        for item in NS["unchecked_obligations"](t):
            if item.lower().startswith(("confirm this is", "scope it to",
                                        "implement + verify", "or close this",
                                        "or discard this")):
                leaked.append(f"{n}: {item[:60]}")
    assert scanned_open >= 50, (
        f"only {scanned_open} open specs scanned — floor not met")
    assert not leaked, (
        "the spec filer's checklist wording changed and boilerplate is "
        f"reaching directives again: {leaked[:3]}")


def test_a_blockquote_inside_the_section_is_dropped():
    """The filter exists for a quote INSIDE the recommendation, not the
    auto-capture banner above it — the banner is outside the section and
    never reaches this code. Without a fixture that puts one inside, the
    filter is defensive code on an unreachable path and a mutation removing
    it survives. This is that fixture."""
    spec = ("# t\n\n## The approved recommendation\n\n"
            "Move drift monitoring to structural-diff.\n"
            "> quoted evidence line that is not part of the instruction\n"
            "_Filed 2026-07-03T00:00:00Z_\n"
            "Add signature-based dedup.\n\n## Human checklist\n\n- [ ] x\n")
    rec = NS["approved_recommendation"](spec)
    assert "structural-diff" in rec and "signature-based dedup" in rec
    assert "quoted evidence" not in rec, "an inline blockquote leaked in"
    assert "_Filed" not in rec, "an inline filed stamp leaked in"


def test_boilerplate_set_is_not_empty():
    """An empty set silently disables the strip; every other test here uses a
    fixture and would still pass."""
    assert len(NS["_BOILERPLATE_OBLIGATIONS"]) >= 4
    assert "implement + verify" in NS["_BOILERPLATE_OBLIGATIONS"]
