"""brain_authored must mean the BRAIN authored it.

MEASURED 2026-09-21 via GET /api/v1/admin/brain/pr-outcomes: of 88 merged rows
all flagged brain_authored=TRUE, only 7 came from a brain pipeline. Checking 25
of the other 81 against each marker:

    24  body: "Co-Authored-By: Claude"   — every Claude Code session PR
     1  body: "brain_pr_opener"          — a PR that merely MENTIONS the module

L6 (brain_strategic_planner) computes its own success rate and reads its
"recent 25 PRs" from these rows, so it was learning from work it did not do.

★ These tests read the marker TUPLE'S VALUES via AST, never by grepping the
source: the module's own comment quotes both removed strings, so a grep would
be tripped by the explanation of why they were removed.

Harness: the CI unit-tests job installs pytest only; the module imports Flask,
so the pure pieces are AST-extracted and exec'd — the real functions.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "brain_pr_outcome_monitor.py")

_NAMES = ("_BRAIN_TITLE_MARKERS", "_BRAIN_BRANCH_MARKERS", "_BRAIN_BODY_MARKERS")
_FUNCS = ("_is_brain_authored", "reclassify_plan")


def _load():
    src = open(MOD, encoding="utf-8").read()
    pieces = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            pieces.append(ast.get_source_segment(src, node))
    ns = {}
    exec(compile("\n\n".join(pieces), MOD, "exec"), ns)
    missing = [n for n in _NAMES + _FUNCS if n not in ns]
    assert not missing, f"AST extraction missed {missing}"
    return ns


NS = _load()
is_brain = NS["_is_brain_authored"]


def _pr(title="", body="", branch=""):
    return {"title": title, "body": body, "head": {"ref": branch}}


# ── The two removed markers must stay removed ────────────────────────────

def test_the_co_author_trailer_is_not_a_brain_marker():
    body = NS["_BRAIN_BODY_MARKERS"]
    assert not any("co-authored-by" in m.lower() for m in body), (
        "a Co-Authored-By trailer is back in the body markers — every Claude "
        "Code session PR carries one, so L6 would again learn from human work")


def test_a_module_name_is_not_a_brain_marker():
    body = NS["_BRAIN_BODY_MARKERS"]
    assert "brain_pr_opener" not in body, (
        "a module NAME matches any PR that mentions it, e.g. a fix to it")


# ── Real shapes, measured on 2026-09-21 ──────────────────────────────────

SESSION_BODY = ("Fixes the thing.\n\n🤖 Generated with [Claude Code]"
                "(https://claude.com/claude-code)\n\n"
                "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>")


def test_a_human_directed_session_pr_is_not_brain_authored():
    """The 24-of-25 case."""
    assert not is_brain(_pr("fix(seo): the canonical tag", SESSION_BODY,
                            "fix/seo-canonical"))


def test_a_pr_that_mentions_brain_pr_opener_is_not_brain_authored():
    """The 1-of-25 case: a fix TO the opener, written by a human session."""
    assert not is_brain(_pr("fix(brain): opener drops the fallback note",
                            "brain_pr_opener returned early on a hold.",
                            "fix/opener-note"))


def test_every_real_pipeline_shape_is_still_detected():
    """★ The removal must not lose real brain PRs. These are the detection
    paths the 7 real pipeline PRs (#4738 #4567 #4220 #4221 #4222 #4202 #3421)
    were verified to take, without the removed markers."""
    assert is_brain(_pr("x", "", "brain/fix-abc")), "brain/fix- branch lost"
    assert is_brain(_pr("[brain-l5 draft] tighten x", "", "wt/x")), "[brain-l5 title lost"
    assert is_brain(_pr("[brain-l6] strategic y", "", "wt/y")), "[brain-l6 title lost"
    assert is_brain(_pr("x", "", "brain-v2/z")), "brain-v2/ branch lost"
    assert is_brain(_pr("x", "Auto-proposed by Brain v2 Layer 5", "wt/q")), (
        "a genuine brain-writer body signature was lost")


# ── The backfill planner ─────────────────────────────────────────────────

def test_reclassify_flips_only_what_the_rule_now_rejects():
    plan = NS["reclassify_plan"]([
        (1, _pr("fix(seo): x", SESSION_BODY, "fix/x")),   # session -> flip
        (2, _pr("[brain-l5 draft] y", "", "wt/y")),        # brain   -> keep
    ])
    assert plan["flip"] == [1] and plan["keep"] == [2]


def test_reclassify_never_flips_what_it_could_not_fetch():
    """★ A failed GitHub fetch is UNMEASURED. Failing to read a PR is not
    evidence it is not the brain's — flipping it would corrupt L6 the other
    way."""
    plan = NS["reclassify_plan"]([(7, None), (8, {}), (9, "not-a-dict")])
    assert plan["flip"] == [], "an unfetchable PR was flipped"
    assert sorted(plan["unmeasured"]) == [7, 8, 9]
