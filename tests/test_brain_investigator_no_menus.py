"""`decision_for_human` must be an ACTION, not a menu.

WHY THIS EXISTS
---------------
The field was specified in the reasoning prompt as:

    "decision_for_human": "<the explicit choice the human must make>"

so the model dutifully produced choices. Four consecutive approved
investigations, verbatim from production on 2026-09-01:

    100419  "Choose the remediation path: (A) ... or (B) ..."
    100418  "Choose the remedy tier: (a) minimal ... or (b) durable ..."
    100417  "Approve (a) ... OR direct a deeper investigation ..."
    100416  "Decide which ... is authoritative, then approve: ..."

The dashboard's approve button hands that text to the Layer-5 drafter, which
refuses anything that is not an exact single-file edit — correctly, since it
cannot pick the operator's branch. Every one of those approvals was a no-op by
construction, and `prs_today` stayed 0.

A prompt change alone is unfalsifiable, so it ships with a fence, in the same
shape as the honest-numbers fence beside it: a menu is FLAGGED into caveats
rather than silently published.

WHAT THIS TEST PROVES
---------------------
  1. all four real production menus are detected — the fence would have caught
     the incident that motivated it;
  2. real single-action directives are NOT flagged (a fence that cries wolf on
     good output gets switched off);
  3. sequential "(a) ... then (b) ..." steps are one action, not a fork;
  4. the word "or" in a single action does not trip it, while a deliberate
     upper-case "OR" fork does;
  5. the prompt itself no longer asks for a choice.

Run:  python3 -m pytest tests/test_investigator_no_menus.py -v
"""

import ast
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SRC = _ROOT / "routes" / "brain_investigator.py"


def _load(name: str):
    src = _SRC.read_text()
    tree = ast.parse(src)
    ns: dict = {}
    for node in tree.body:                       # module constants first
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_MENU_MARKERS"
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(_SRC), "exec"), ns)  # noqa: S102
    assert "_MENU_MARKERS" in ns, "_MENU_MARKERS vanished from the investigator"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty"
            exec(compile(seg, str(_SRC), "exec"), ns)  # noqa: S102
            assert ns[name].__code__.co_code, f"{name} compiled to empty code"
            return ns[name]
    raise AssertionError(f"{name} not found in {_SRC}")


menu_marker = _load("_menu_marker")


# The four, verbatim from the dashboard on 2026-09-01.
_REAL_MENUS = {
    "100419": ("Choose the remediation path: (A) immediately trigger `gh workflow "
               "run press-rss.yml` in dchub-frontend to re-bake press-release pages "
               "and then re-crawl all 8 URLs, or (B) first run a diagnostic crawl + "
               "DB check (do the 6 dead slugs exist in press_releases?) before "
               "touching the pipeline."),
    "100418": ("Choose the remedy tier: (a) minimal — delete the 19,700 slot and "
               "keep 20,100 as a single conservative floor, adding '19,700' to "
               "stale_markers; or (b) durable — wire both slots to the canonical "
               "facilities getter (the #1390 pattern)."),
    "100417": ("Approve (a) an immediate verification pass curling all 8 published "
               "story URLs and checking DB rows for their slugs, then (b) if DB rows "
               "exist but static pages don't, trigger `gh workflow run press-rss.yml` "
               "in dchub-frontend and re-verify — OR direct a deeper investigation "
               "of edge routing."),
    "100416": ("Decide which verified-facility definition is authoritative "
               "post-issue #1539 (the 19,935 fleet-filter count or the 20,019 "
               "breakdown count), then approve repointing the two divergent "
               "publish surfaces to that single canon."),
}

# What the prompt now asks for instead.
_REAL_ACTIONS = [
    "Repoint routes/competitive_seo.py:211 at the canonical facilities getter.",
    "Delete the 19,700 slot from the homepage and add '19,700' to stale_markers.",
    "Trigger `gh workflow run press-rss.yml` in dchub-frontend, then re-crawl "
    "all 8 published story URLs and report which still 404.",
    "Add a constraint_coverage block to get_hosting_capacity naming the feeders "
    "it does not cover.",
    # "or" inside ONE action must not trip the fence
    "Delete the 19,700 slot or the stale marker that shadows it, whichever the "
    "canonical getter contradicts.",
    # sequential steps are one action in two parts, not a fork
    "Do (a) curl all 8 story URLs, then (b) trigger press-rss.yml if rows exist.",
]


@pytest.mark.parametrize("item_id", sorted(_REAL_MENUS))
def test_every_real_production_menu_is_flagged(item_id):
    why = menu_marker(_REAL_MENUS[item_id])
    assert why, f"inv:{item_id} would still slip through as an action"


@pytest.mark.parametrize("text", _REAL_ACTIONS)
def test_real_actions_are_not_flagged(text):
    """A fence that cries wolf on good output gets switched off."""
    assert menu_marker(text) == "", f"false positive on: {text[:70]}"


def test_empty_is_not_a_menu():
    assert menu_marker("") == "" and menu_marker(None) == ""
    assert menu_marker("   \n ") == ""


def test_lowercase_or_is_not_a_fork_but_uppercase_OR_is():
    """The discriminator: 'or' is ordinary English, ' OR ' is a deliberate fork."""
    assert menu_marker("Rebuild the index or rerun the loader, whichever is stale.") == ""
    assert menu_marker("Rebuild the index OR rerun the loader.") != ""


def test_the_marker_says_which_signal_fired():
    assert "choice" in menu_marker(_REAL_MENUS["100418"])
    assert "OR" in menu_marker(_REAL_MENUS["100417"])


# ── the prompt itself ────────────────────────────────────────────────────────
def test_the_prompt_no_longer_asks_for_a_choice():
    """The root cause was the field spec, not the model."""
    src = _SRC.read_text()
    assert "the explicit choice the human must make" not in src, \
        "the prompt still asks the model for a choice"
    assert "NEVER a menu" in src, "the anti-menu instruction is missing"


def test_the_fence_is_wired_into_synthesize():
    """A helper nothing calls is not a fence. Located with ast, so a mention in
    a comment does not count."""
    src = _SRC.read_text()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "investigate"), None)
    assert fn is not None, "investigate() not found"
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_menu_marker" in called, "_menu_marker is defined but never called"
