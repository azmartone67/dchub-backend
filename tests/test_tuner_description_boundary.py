"""The tuned description must never stop mid-word.

`return text[:280]` was a blind slice. Measured 2026-09-06 against the live
/api/v1/mcp/tool-descriptions payload, 100 of 132 stored cells (76%) ended
mid-sentence, across all 11 tuned tools — "Use for single-market b",
"to answer investment feasibility questio". Agents read these strings to
choose between tools, so a fragment reads as a broken tool.

These tests pull the real helper out of the shipped source with `ast` rather
than importing the module (routes/* import flask and open DB pools).
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "ai_platform_tool_tuner.py"


def _load():
    """Execute just the clamp helper and its constants, nothing else."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    keep, ns = [], {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_clamp_description":
            keep.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith("_DESC_"):
                    keep.append(node)
    assert keep, "_clamp_description not found in the shipped source"
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


def _ends_cleanly(s):
    return bool(s) and (s[-1] in ".!?\"')" or not s.endswith(tuple(",;:-")))


def test_never_cuts_mid_word():
    ns = _load()
    clamp, cap = ns["_clamp_description"], ns["_DESC_MAX"]
    word = "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
    for n in range(1, 40):
        out = clamp(word * n)
        assert len(out) <= cap
        # every token in the output must be a whole token of the input
        for tok in out.split():
            assert tok.rstrip(".") in word.split(), f"fragment {tok!r} from {out!r}"


def test_prefers_a_complete_sentence():
    """★ The first version of this test was VACUOUS: its input was 249 chars,
    under the 280 cap, so the clamp returned it untouched and `endswith(".")`
    passed without the sentence branch ever running. Three mutations survived
    it. The input must actually exceed the cap."""
    ns = _load()
    clamp, cap = ns["_clamp_description"], ns["_DESC_MAX"]
    body = "Returns the verdict and the composite score for one market. " * 4
    tail = "Then a trailing clause that runs well past the cap and would be cut apart"
    src = body + tail
    assert len(src) > cap, "input must exceed the cap or the branch never runs"
    out = clamp(src)
    assert len(out) <= cap
    assert out.endswith("."), f"did not end on a sentence: {out[-50:]!r}"
    assert not out.endswith("market. Then"), out


def test_sentence_floor_does_not_gut_the_description():
    """The floor is the reason a very early sentence end does not win. With
    the floor at 0.0 this input collapses to "Short." — 6 chars of a 280-char
    budget. Mutating the tunable must be visible."""
    ns = _load()
    clamp, cap = ns["_clamp_description"], ns["_DESC_MAX"]
    src = "Short. " + "then a long run of clauses with no sentence end at all " * 12
    assert len(src) > cap
    out = clamp(src)
    # ★ The expectation is a FIXED number, deliberately not derived from
    # _DESC_SENTENCE_FLOOR. An earlier version asserted
    # `len(out) >= cap * _DESC_SENTENCE_FLOOR`, which reads the very constant
    # under test — setting the floor to 0.0 turned the assertion into
    # `len(out) >= 0` and the mutation survived. A guard must not take its
    # threshold from the thing it is guarding.
    assert len(out) >= 150, (
        f"clamp discarded most of the budget: kept {len(out)} of {cap} — "
        f"{out!r}")
    assert len(out) <= cap


def test_short_text_is_returned_whole():
    ns = _load()
    assert ns["_clamp_description"]("Short and complete.") == "Short and complete."


def test_output_is_always_within_the_cap():
    ns = _load()
    clamp, cap = ns["_clamp_description"], ns["_DESC_MAX"]
    for s in ("x" * 5000, "word " * 500, "No.Sentence.Breaks." * 40,
              "a" + " b" * 400, "", "   ", "." * 400):
        assert len(clamp(s)) <= cap


def test_no_dangling_punctuation():
    ns = _load()
    clamp = ns["_clamp_description"]
    for s in ("Covers PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISONE, AESO, " * 8,
              "one; two; three; four; five; six; seven; eight; nine; ten; " * 8):
        out = clamp(s)
        assert not out.endswith((",", ";", ":", "-", "—")), repr(out[-40:])


def test_the_ask_is_below_the_hard_cap():
    """If the prompt asks for exactly the cap, the clamp becomes the normal
    path instead of the exception — which is how 76% of cells got cut."""
    ns = _load()
    assert ns["_DESC_ASK"] < ns["_DESC_MAX"], (
        "the number the model is asked for must leave headroom under the cap")


def test_the_blind_slice_is_gone_from_the_source():
    src = SRC.read_text(encoding="utf-8")
    code = re.sub(r"#[^\n]*", "", src)
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    assert "[:280]" not in code, "a blind character slice is back in the source"
    assert "_clamp_description(text)" in code, (
        "the model's output must go through the boundary-aware clamp")
