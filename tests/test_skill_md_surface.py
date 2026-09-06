"""/skill.md must render from canon, and must resolve it PER REQUEST.

Background (2026-09-06). /skill.md was a hard 404 at the public edge — the only
404 among the 14 published discovery surfaces — because the path was absent from
dchub-frontend's `_routes.json` include, so CF Pages answered its own 404 and
main.py's serve_skill_md() never ran. The origin served 200 the whole time, so
every origin-side check looked healthy.

Unreachable code rots quietly. While nothing could read it, static/skill.md drifted
to "21,000+ facilities" — a value on ai_surface_canon's own stale_markers denylist
("the PRE-DEDUP facility floors ... scrub on sight"), roughly a 1.7x over-claim —
and to "$51B+" and "21 GW", both of which the repo already treats as legacy stats
(frontend_stat_normalizer.py:117 and mcp_bug_fixes_and_new_tools.py BUG-028). Its
maintained sibling twin /skill.json served the correct floor the entire time.

Routing the path is therefore only half a fix: publishing a stale body to the agent
platforms that are handed this URL by name is worse than the 404 it replaces. These
tests guard the body, and the WAY it is resolved.
"""
import ast
import inspect
import os
import re

import pytest

import ai_surface_canon as asc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_MD = os.path.join(REPO, "static", "skill.md")

PLACEHOLDER_RE = re.compile(r"\{canon_[a-z_]+\}")


def _serve_skill_md_ast():
    """The AST of main.py's serve_skill_md(), or fail loudly."""
    tree = ast.parse(open(os.path.join(REPO, "main.py")).read())
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "serve_skill_md"
        ),
        None,
    )
    assert fn is not None, "main.py no longer defines serve_skill_md()"
    return fn


def _render():
    """Reproduce serve_skill_md(): read the file, canon_text it, sweep residuals.

    ★ This mirrors the handler because importing main.py in a unit test is against
    this repo's grain — main drags in the whole app and its DB pools, which is why
    only one test in tests/ imports it and the rest stub it (see the note at the top
    of tests/test_crossover_onramp.py).

    A mirror that can drift from the code it mirrors is worth very little: delete the
    sweep from the handler and a mirror-based test stays green. So the drift itself is
    guarded — test_handler_performs_the_steps_this_test_mirrors below asserts, against
    main.py's AST, that the handler still does each step reproduced here. The canon
    resolution is NOT mirrored: asc.canon_text is the same shared resolver the handler
    calls.
    """
    with open(SKILL_MD) as f:
        body = f.read()
    body = asc.canon_text(body)
    return PLACEHOLDER_RE.sub("", body)


def test_source_carries_placeholders_not_hardcoded_counts():
    """The count claims must be placeholders, so they cannot drift again."""
    src = open(SKILL_MD).read()
    found = set(PLACEHOLDER_RE.findall(src))
    assert "{canon_facilities}" in found, (
        "static/skill.md must state its facility count as {canon_facilities}. "
        f"Found placeholders: {sorted(found)}"
    )
    assert "{canon_deals}" in found, "the M&A claim must render from canon"


@pytest.mark.parametrize(
    "marker",
    # Every marker here is one this file actually carried while it was unroutable.
    ["21,000+", "$51B", "21 GW"],
)
def test_retired_claims_are_gone(marker):
    src = open(SKILL_MD).read()
    assert marker not in src, (
        f"static/skill.md carries the retired claim {marker!r}. This file is now "
        "PUBLICLY SERVED at https://dchub.cloud/skill.md and is advertised to agent "
        "platforms by name (ai_agent_discovery.py, ai_outreach_agent.py, "
        "static/.well-known/ai-agents.json) — a stale number here is externally visible."
    )


def test_rendered_body_never_leaks_a_placeholder():
    """A literal '{canon_facilities}' served to an agent is worse than a stale number.

    canon_text()'s own docstring says so. The handler sweeps residuals for exactly
    this reason, so the rendered body must contain none.
    """
    body = _render()
    assert not PLACEHOLDER_RE.search(body), (
        "unresolved canon placeholder in the rendered /skill.md body: "
        f"{PLACEHOLDER_RE.findall(body)}"
    )


def test_rendered_body_carries_the_canonical_facility_floor():
    body = _render()
    canon_fac = asc.canon_nums()["{canon_facilities}"]
    if not canon_fac:
        pytest.skip("canon resolved empty (fail-open); nothing to assert against")
    assert canon_fac in body, (
        f"rendered /skill.md does not state the canonical facility floor {canon_fac!r}"
    )


def test_denylisted_markers_absent_from_rendered_body():
    """The rendered body must not contain any stale_marker, not just the source."""
    body = _render()
    markers = [m for m in asc.PINNED.get("stale_markers", []) if m and m.strip()]
    hits = [m for m in markers if m in body]
    assert not hits, f"rendered /skill.md carries denylisted stale marker(s): {hits}"


def test_canon_is_resolved_per_request_not_latched_at_import():
    """★ The failure this test exists for: canon bound at MODULE scope is a LATCH.

    routes/case_studies_landing.py called canon_text() at module scope and froze the
    value at process import — it served 20,100+ while /llms.txt, on the SAME process,
    served 20,400+. tests/test_canon_placeholders_resolved.py cannot catch that: it
    walks the AST for placeholder strings lexically wrapped in canon_text(), which a
    module-scope call satisfies perfectly. It asks WHERE the resolver sits, never WHEN
    it runs.

    So assert the call site directly: canon_text must be invoked INSIDE the request
    handler's own body.
    """
    fn = _serve_skill_md_ast()

    calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id.lstrip("_") == "canon_text")
            or (
                isinstance(n.func, ast.Attribute)
                and n.func.attr.lstrip("_") == "canon_text"
            )
        )
    ]
    assert calls, (
        "serve_skill_md() does not call canon_text() in its own body. Binding canon "
        "at module scope LATCHES the value for the life of the process and still "
        "passes the AST placeholder guard — resolve per request."
    )


def test_handler_performs_the_steps_this_test_mirrors():
    """Keep _render() honest: if the handler stops sweeping, this must go red.

    _render() reproduces the handler rather than calling it. That is only safe while
    the handler still performs the same steps, so assert them structurally:

      1. it reads static/skill.md
      2. it calls canon_text()          (covered by the per-request test above)
      3. it strips any residual {canon_*} placeholder before returning

    Step 3 is the one a mirror would otherwise hide.
    """
    fn = _serve_skill_md_ast()
    src = ast.dump(fn)

    assert "static/skill.md" in ast.dump(fn), "handler no longer reads static/skill.md"

    sub_calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "sub"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and "canon_" in str(n.args[0].value)
    ]
    assert sub_calls, (
        "serve_skill_md() no longer strips residual {canon_*} placeholders. Either "
        "restore the sweep or stop mirroring it in _render() — as written, this test "
        "file would go on asserting a step the handler does not perform."
    )
