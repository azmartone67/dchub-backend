"""The agent front door must name execute_plan, asserted on the SERVED body.

★ THE FAILURE THIS ENCODES (2026-08-31)

`scripts/lint-front-door.mjs` has existed in dchub-frontend since 2026-07-28.
Its third pattern is, verbatim, "puts plan_query behind a START HERE marker".
Live `/llms.txt` line 32 read:

    ## MCP Tools — START HERE: call plan_query first (the orchestration front door)

for weeks, while that lint reported green every run.

It reported green because of WHAT IT SCANNED. Its `targets()` globs
`llms.txt` / `llms-full.txt` / `AGENTS.md` **in the frontend repo**. Those are
dead documents: `/llms.txt` is served INLINE from this file
(`ai_discovery_routes.py`), and the frontend copy is 34,979 b against a 10,093 b
live body — materially different content that reaches no agent. The lint's own
header asserts "the sibling repos run it on their own files via the same rule";
no such guard existed in this repo. `find . -name 'lint-front-door*'` -> nothing.

So the guard scanned an artifact, the artifact was clean, and reality was not.
That is the THIRD recorded instance of this exact shape (see the dead-man beat,
which proved a job RAN rather than INGESTED, and `no_new_data`, which asserted
zero on a structurally-always-zero counter).

★ WHY THIS ONE RENDERS INSTEAD OF GREPPING

Reading the source string would reproduce the original defect one level down:
`serve_llms_txt` builds its body through `canon_text(f"...")`, so the source is
not the served bytes. This registers the real blueprint on a bare Flask app and
asserts against `test_client().get(...)` output — what an agent actually
receives.

★ FAIL-CLOSED. A route that 404s, or renders implausibly short, FAILS. An
unreadable surface is never a pass. And the ban is paired with a POSITIVE
assertion: banning plan_query alone would go green on an empty file.
"""
import re

import flask
import pytest

import ai_discovery_routes

# Ported verbatim in intent from dchub-frontend/scripts/lint-front-door.mjs.
# The rule is NOT "plan_query must not appear" — it is a real, supported
# inspect-only tool and every surface legitimately names it. The rule is that it
# must not be named as the DEFAULT MULTI-STEP PATH.
_STEP = r"(multi-?step|multi ?capability|complex|complex queries|multi-?stage|recommended_sequence|recommended sequence)"
BANNED = [
    (re.compile(rf"\bplan_query\b[^\n]{{0,120}}?\b(call|use|invoke|should|first|for)\b[^\n]{{0,120}}?\b{_STEP}\b", re.I),
     "names plan_query as the multi-step path"),
    (re.compile(rf"\b(call|use|invoke|should|first|for)\b[^\n]{{0,120}}?\bplan_query\b[^\n]{{0,120}}?\b{_STEP}\b", re.I),
     "names plan_query as the multi-step path (verb-first phrasing)"),
    (re.compile(r"\b(start[ _-]?here)\b[^\n]{0,160}?\bplan_query\b", re.I),
     "puts plan_query behind a START HERE marker"),
]

# The sentences whose whole job is to WARN about this anti-pattern.
ALLOW = [
    re.compile(r"if it names .{0,40}plan_query.{0,40} as the multi-?step path", re.I),
    re.compile(r"never call .{0,20}execute_plan", re.I),
    re.compile(r"INSPECT-ONLY", re.I),
]

# path -> minimum plausible rendered size. Below this the surface is broken and
# a clean scan would be vacuous.
SURFACES = {"/llms.txt": 4000, "/llms-full.txt": 4000}


@pytest.fixture(scope="module")
def served():
    app = flask.Flask(__name__)
    ai_discovery_routes.register_discovery_routes(app)
    client = app.test_client()
    out = {}
    for path in SURFACES:
        resp = client.get(path)
        out[path] = (resp.status_code, resp.data.decode("utf-8", "replace"))
    return out


@pytest.mark.parametrize("path", sorted(SURFACES))
def test_surface_renders(served, path):
    """Fail closed: an unreachable or stub surface is not a pass."""
    status, body = served[path]
    assert status == 200, f"{path} rendered {status}; cannot scan a surface that does not serve"
    assert len(body) >= SURFACES[path], (
        f"{path} rendered {len(body)}b, below the {SURFACES[path]}b floor — "
        "too short to be the real document; a clean scan here would be vacuous")


@pytest.mark.parametrize("path", sorted(SURFACES))
def test_no_plan_query_as_multi_step_path(served, path):
    """The rule the frontend lint has always had, applied to what we actually serve."""
    _, body = served[path]
    violations = []
    for lineno, line in enumerate(body.split("\n"), 1):
        if any(a.search(line) for a in ALLOW):
            continue
        for pattern, why in BANNED:
            if pattern.search(line):
                violations.append(f"{path}:{lineno} — {why}\n    {line.strip()[:160]}")
                break
    assert not violations, (
        "plan_query is named as the multi-step path on a SERVED surface:\n"
        + "\n".join(violations)
        + '\n\nRemediation: the multi-step path is execute_plan(intent="..."). '
          "plan_query is inspect-only.")


def test_llms_txt_names_execute_plan_as_the_front_door(served):
    """Positive half. Banning plan_query alone goes green on an empty file."""
    _, body = served["/llms.txt"]
    head = body[:body.index("## MCP Tools — what each RETURNS")] if \
        "## MCP Tools — what each RETURNS" in body else body
    assert "execute_plan" in head, (
        "/llms.txt does not name execute_plan before the tool catalogue. An agent "
        "reading it top-down gets no front door and probes the catalogue instead.")
    assert re.search(r"start[ _-]?here[^\n]{0,160}execute_plan", head, re.I), (
        "/llms.txt has no START HERE marker pointing at execute_plan.")


def test_llms_full_names_execute_plan_as_the_front_door(served):
    """llms-full.txt is the ~4,000-word doc agents load for depth. Before
    2026-08-31 it named NEITHER tool: an agent reading it top-down got the REST
    endpoint list and no orchestration entry point at all."""
    _, body = served["/llms-full.txt"]
    assert "execute_plan" in body, (
        "/llms-full.txt does not name execute_plan anywhere. An agent reading it "
        "gets the endpoint catalogue and no front door.")
    assert re.search(r"start[ _-]?here[^\n]{0,160}execute_plan", body, re.I), (
        "/llms-full.txt has no START HERE marker pointing at execute_plan.")
