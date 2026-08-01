"""The weekly note must TELL the shipped story — deterministically.

Ascension lane 7 ("evolution story told") was red with the message "goes green
on the next weekly generation". The code could not keep that promise. Four
separate defects, each of which alone leaves the lane red forever:

  1. The section was requested ONLY in a prompt. A prompt instruction is a
     request, not a guarantee — no verification, no retry, no fallback. A
     paraphrase or a capitalisation change and the lane stays red.
  2. The instruction was CONDITIONAL on evolution.merged_prs_7d or
     evolution.snapshot being truthy, so an empty evolution input meant the
     section was never even asked for.
  3. ★ The figure fence built its allowed-numbers set from leads/deals_7d/
     news_7d/rag_context/lessons — `evolution` was MISSING. The section is
     explicitly told to name merged-PR counts and PR numbers, and the fence
     strips the whole SENTENCE carrying an unsourced figure. So the section
     was being asked for, written, then gutted.
  4. The lane grepped for the HEADING. A section gutted by (3) down to a bare
     heading still passed — a vacuous gate over a broken pipeline.

Tests never import main.py — the real functions are pulled out of source with
`ast` and executed against stubs.
"""
import ast
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTE = os.path.join(ROOT, "routes", "analyst_note.py")
SHELL = os.path.join(ROOT, "routes", "brain_ascension_master_shell.py")

WANT = ("EVOLUTION_HEADING", "_evolution_section_has_content",
        "_compose_evolution_section", "_compose_prompt")


def _load():
    src = open(NOTE, encoding="utf-8").read()
    ns = {"re": re, "json": json, "os": os}
    import datetime as _dt
    ns["_dt"] = _dt
    for node in ast.parse(src).body:
        nm = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None):
            nm = node.targets[0].id
        if nm in WANT:
            exec(compile(ast.Module(body=[node], type_ignores=[]), NOTE, "exec"), ns)
    for w in WANT:
        assert w in ns, f"{w} not found in analyst_note.py"
    return ns


EVOLUTION = {
    "merged_prs_7d": 37,
    "recent_prs": [{"pr": 2086, "title": "ONE announcement store"},
                   {"pr": 2075, "title": "ISONE credential leak"}],
    "snapshot": {"evolution_score": 74},
}


# ── 1. the content predicate is not satisfied by a bare heading ───────

def test_bare_heading_is_not_content():
    ns = _load()
    h = ns["EVOLUTION_HEADING"]
    assert ns["_evolution_section_has_content"](f"## {h}\n\n") is False
    assert ns["_evolution_section_has_content"](f"## {h}\n\n## Markets\n\nx" * 1) is False
    assert ns["_evolution_section_has_content"]("") is False
    assert ns["_evolution_section_has_content"](None) is False
    assert ns["_evolution_section_has_content"]("## Markets\n\nlots of prose here") is False


def test_section_with_prose_is_content():
    ns = _load()
    h = ns["EVOLUTION_HEADING"]
    body = (f"## Markets\n\nSomething about markets.\n\n## {h}\n\n"
            "DC Hub merged 37 pull requests into production this week, "
            "including the announcement store consolidation.\n")
    assert ns["_evolution_section_has_content"](body) is True


def test_content_stops_at_the_next_heading():
    """Prose belonging to the NEXT section must not count as this one's."""
    ns = _load()
    h = ns["EVOLUTION_HEADING"]
    body = (f"## {h}\n\n## What it means next week\n\n"
            "A long paragraph that belongs to the following section entirely.")
    assert ns["_evolution_section_has_content"](body) is False


# ── 2. deterministic composition from the same inputs ─────────────────

def test_composer_builds_a_real_section_from_evolution_inputs():
    ns = _load()
    out = ns["_compose_evolution_section"](EVOLUTION)
    assert ns["EVOLUTION_HEADING"] in out
    assert "37" in out, "the merged-PR count must be named"
    assert "2086" in out, "recent PR numbers must be named"
    assert ns["_evolution_section_has_content"](out), "composed section is empty"


def test_composer_returns_empty_when_there_is_nothing_to_tell():
    """No evolution data => no section, rather than an empty promise."""
    ns = _load()
    for empty in ({}, None, {"merged_prs_7d": 0}, {"recent_prs": []},
                  {"snapshot": {}}):
        assert ns["_compose_evolution_section"](empty) == ""


def test_composer_never_raises_on_malformed_input():
    ns = _load()
    for bad in ({"recent_prs": "nope"}, {"recent_prs": [None, 5]},
                {"merged_prs_7d": "x"}, {"snapshot": "x"}):
        ns["_compose_evolution_section"](bad)


# ── 3. every figure the composer emits survives the fence ─────────────

def test_composed_figures_are_all_sourced_by_the_evolution_input():
    """The composed prose must not be strippable by _fence_unsourced_figures:
    every number in it has to appear in the evolution blob itself."""
    ns = _load()
    out = ns["_compose_evolution_section"](EVOLUTION)
    blob = json.dumps(EVOLUTION, default=str)
    for tok in re.findall(r"\d[\d,]*", out):
        assert tok.replace(",", "") in blob.replace(",", ""), (
            f"composed figure {tok!r} is not present in the evolution inputs, "
            "so the figure fence would strip the sentence carrying it")


def test_evolution_is_in_the_fence_allowed_blob():
    """Defect 3: the section names PR numbers, so `evolution` MUST be one of
    the keys the allowed-numbers set is built from."""
    src = open(NOTE, encoding="utf-8").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)
               and n.name == "generate_analyst_note"), None)
    assert fn is not None
    seg = ast.get_source_segment(src, fn) or ""
    m = re.search(r"inputs_blob\s*=\s*json\.dumps\(\s*\{[^}]*?for k in \(([^)]*)\)",
                  seg, re.S)
    assert m, "could not locate the allowed-numbers blob"
    assert "evolution" in m.group(1), (
        "`evolution` missing from the fence's allowed-numbers blob — the "
        "section's PR numbers would be stripped as unsourced")


# ── 4. the prompt asks unconditionally, and generation repairs ────────

def test_required_section_is_not_conditional_on_snapshot_shape():
    ns = _load()
    prompt = ns["_compose_prompt"]({"evolution": {"merged_prs_7d": 0}}, [],
                                   __import__("datetime").date(2026, 8, 3))
    assert ns["EVOLUTION_HEADING"] in prompt, (
        "the REQUIRED SECTION instruction was dropped for an evolution input "
        "that is present but has no merged PRs — it would never be requested")


def test_generate_repairs_a_missing_section():
    """The whole point: generation must not depend on model compliance."""
    src = open(NOTE, encoding="utf-8").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)
               and n.name == "generate_analyst_note"), None)
    seg = ast.get_source_segment(src, fn) or ""
    assert "_evolution_section_has_content" in seg, (
        "generate_analyst_note() never verifies the section landed")
    assert "_compose_evolution_section" in seg, (
        "generate_analyst_note() has no deterministic fallback — the section "
        "still depends on the model emitting the heading verbatim")


# ── 5. the lane grades content, sharing ONE definition with the writer ─

def test_ascension_lane_imports_the_predicate_rather_than_transcribing_it():
    src = open(SHELL, encoding="utf-8").read()
    assert "_evolution_section_has_content" in src, (
        "the lane does not use the composer's content predicate — a "
        "transcribed check is how the two sides drift apart")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)
               and n.name == "_lane_evolution_story"), None)
    assert fn is not None, "_lane_evolution_story not found"
    seg = ast.get_source_segment(src, fn) or ""
    # The shared predicate must be what GRADES the check.
    assert re.search(r"_has_evolution_content\s*\(\s*body\s*\)", seg), (
        "the lane does not grade with the composer's content predicate")
    # ...and heading-presence must not be bound as the verdict in ANY form.
    # (An earlier version of this test matched the literal string with a
    #  trailing comma, so simply dropping the comma slipped past it.)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            val = ast.dump(node.value)
            if "EVOLUTION_HEADING" in val and "Compare" in val:
                raise AssertionError(
                    "the lane binds heading PRESENCE as the verdict — a "
                    "fence-gutted section reduced to a bare heading would pass")


def test_lane_no_longer_promises_a_self_heal_it_cannot_guarantee():
    src = open(SHELL, encoding="utf-8").read()
    assert "goes green on the next weekly generation" not in src, (
        "the lane still claims it self-heals on the next generation — that "
        "was true only if the model happened to comply")
