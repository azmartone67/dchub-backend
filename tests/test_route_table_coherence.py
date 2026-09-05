"""The route-table coherence ratchet must keep the properties it was fixed for.

scripts/check_route_table_coherence.py replaced two parsers that were BOTH
quietly wrong, and both wrongnesses looked like a clean measurement:

  * a line regex that recorded the DECORATOR path and never resolved blueprint
    url_prefix, so "/click" under url_prefix="/api/v1/redeem" read as an
    uncovered HTML route;
  * a `\\[([^\\]]+)\\]` capture of the worker's arrays, which stops at the first
    "]" in the file — one that sits inside a trailing // comment — reading 6 of
    36 prefixes and pulling comment prose into the path Set via the apostrophe
    in words like "didn't".

Together they reported 385 of 496 routes uncovered. The honest number is 130.
These tests pin the behaviours that produced the honest number, so a future
"simplification" back to a regex fails here instead of silently re-inflating the
baseline until someone turns the gate off again.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_route_table_coherence.py"
BASELINE = REPO / "scripts" / "route_table_baseline.json"


def _load():
    spec = importlib.util.spec_from_file_location("route_table_coherence", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


# ── the extractor ────────────────────────────────────────────────────────────

def test_blueprint_url_prefix_is_resolved(tmp_path, mod):
    """The exact shape that made "/click" look like an uncovered HTML page."""
    (tmp_path / "m.py").write_text(
        'from flask import Blueprint\n'
        'redeem_bp = Blueprint("redeem", __name__, url_prefix="/api/v1/redeem")\n'
        '@redeem_bp.route("/click", methods=["GET", "POST"])\n'
        'def click():\n    return "x"\n'
    )
    (tmp_path / "main.py").write_text(
        'from m import redeem_bp\napp.register_blueprint(redeem_bp)\n'
    )
    paths = mod.extract_flask_paths(tmp_path)
    assert "/api/v1/redeem/click" in paths
    assert "/click" not in paths, (
        "the decorator path leaked through unprefixed — this is the regex bug "
        "that made /api/* routes read as uncovered HTML pages"
    )


def test_register_blueprint_prefix_beats_constructor(tmp_path, mod):
    """Flask applies the registration-site url_prefix; so must the extractor."""
    (tmp_path / "m.py").write_text(
        'from flask import Blueprint\n'
        'bp = Blueprint("b", __name__, url_prefix="/ctor")\n'
        '@bp.route("/x")\ndef x():\n    return "x"\n'
    )
    (tmp_path / "main.py").write_text(
        'from m import bp\napp.register_blueprint(bp, url_prefix="/reg")\n'
    )
    paths = mod.extract_flask_paths(tmp_path)
    assert "/reg/x" in paths and "/ctor/x" not in paths


def test_multiline_blueprint_constructor_is_seen(tmp_path, mod):
    """15 Blueprint() calls in this repo span lines; a line regex sees none."""
    (tmp_path / "m.py").write_text(
        'from flask import Blueprint\n'
        'bp = Blueprint(\n    "b",\n    __name__,\n    url_prefix="/multi",\n)\n'
        '@bp.route("/y")\ndef y():\n    return "y"\n'
    )
    (tmp_path / "main.py").write_text('from m import bp\napp.register_blueprint(bp)\n')
    assert "/multi/y" in mod.extract_flask_paths(tmp_path)


def test_unregistered_blueprint_serves_nothing(tmp_path, mod):
    (tmp_path / "m.py").write_text(
        'from flask import Blueprint\n'
        'dead_bp = Blueprint("dead", __name__)\n'
        '@dead_bp.route("/never")\ndef never():\n    return "n"\n'
    )
    (tmp_path / "main.py").write_text("pass\n")
    assert "/never" not in mod.extract_flask_paths(tmp_path)


def test_route_in_a_string_is_not_a_route(tmp_path, mod):
    """The old regex matched '@bp.route(' inside source strings and comments,
    yielding entries like "..." and " in line and not line.strip().startswith("."""
    (tmp_path / "m.py").write_text(
        'from flask import Blueprint\n'
        'bp = Blueprint("b", __name__)\n'
        'DOC = "use @bp.route(\'/fake\') to add a page"\n'
        '# @bp.route("/alsofake")\n'
        '@bp.route("/real")\ndef real():\n    return "r"\n'
    )
    (tmp_path / "main.py").write_text('from m import bp\napp.register_blueprint(bp)\n')
    paths = mod.extract_flask_paths(tmp_path)
    assert "/real" in paths
    assert "/fake" not in paths and "/alsofake" not in paths


# ── the worker-table parser ──────────────────────────────────────────────────

def test_bracket_inside_a_comment_does_not_truncate_the_array(mod):
    """The exact _worker.js shape: a "]" in a trailing comment mid-array."""
    src = (
        "const PHASE_282_PREFIXES = [\n"
        "  '/first/',   // note [see rfc] which closes a bracket right here\n"
        "  '/second/',\n"
        "  '/third/',\n"
        "];\n"
    )
    got = mod._js_string_list(src, "PHASE_282_PREFIXES")
    assert got == {"/first/", "/second/", "/third/"}, (
        f"array truncated at a comment's bracket: {sorted(got)}"
    )


def test_a_path_quoted_inside_a_comment_is_not_an_entry(mod):
    """_worker.js really contains

        // Listed as EXACT paths, not an '/ai/' prefix — a prefix would sweep

    and the old regex read '/ai/' out of it as a routing entry. It also read
    multi-line comment prose as entries, because "didn't … wasn't" forms a
    quote pair; those got filtered only by luck (they did not start with "/").
    A path-shaped one does not have that luck.
    """
    src = (
        "const PHASE_282_RAILWAY_PATHS = new Set([\n"
        "  '/team',\n"
        "  // Listed as EXACT paths, not an '/ai/' prefix — CF Pages didn't\n"
        "  // forward these and the worker wasn't told about '/phx/' either\n"
        "  '/upgrade',\n"
        "]);\n"
    )
    got = mod._js_string_list(src, "PHASE_282_RAILWAY_PATHS")
    assert got == {"/team", "/upgrade"}, (
        f"comment prose leaked into the routing table: {sorted(got)}"
    )


def test_dispatch_guard_fallthroughs_are_parsed_not_hardcoded(mod):
    """/pockets/<slug> — the route this whole gate was built for — is forwarded
    by a `const _IS_POCKETS` above the guard, not by either named table."""
    src = (
        "const _IS_POCKETS = pathname === '/pockets'\n"
        "                 || pathname === '/pockets.rss'\n"
        "                 || pathname.startsWith('/pockets/');\n"
        "      if (PHASE_282_RAILWAY_PATHS.has(pathname)\n"
        "          || PHASE_282_PREFIXES.some(p => pathname.startsWith(p))\n"
        "          || pathname.startsWith('/unlock/')\n"
        "          || _IS_POCKETS) {\n"
    )
    exact, prefix = mod._worker_fallthrough_prefixes(src)
    assert "/pockets" in exact and "/pockets.rss" in exact
    assert "/pockets/" in prefix and "/unlock/" in prefix


# ── the ratchet ──────────────────────────────────────────────────────────────

def test_a_new_uncovered_route_is_reported_as_added(mod):
    """The ratchet's whole contract, at the unit level."""
    tables = {"routes_json_include": ["/kept"], "worker_paths": ["/kept"],
              "worker_prefixes": []}
    mr, mw = mod._uncovered({"/kept", "/brand-new"}, tables)
    assert "/brand-new" in mr and "/brand-new" in mw
    assert "/kept" not in mr and "/kept" not in mw


def test_baseline_is_two_lists_not_a_union():
    """Union-baselining would let a path migrate from a 404 to a 403 silently."""
    data = json.loads(BASELINE.read_text())
    assert "missing_routes_json" in data and "missing_worker" in data
    assert "uncovered" not in data, "collapsed back to a single union list"
    assert data["_comment"].strip(), "the register must explain itself"


def test_baseline_only_shrinks_is_stated_in_the_register():
    c = json.loads(BASELINE.read_text())["_comment"]
    assert "NEVER GROW" in c.upper() or "never grow" in c


# ── the ledger contract ──────────────────────────────────────────────────────

def test_verdict_strings_the_workflow_greps_are_still_printed():
    """check-route-tables.yml's gate-liveness ledger greps these EXACT strings
    out of the step logs. Change the wording and the board records `unmeasured`
    forever, silently — the failure mode this test exists to prevent."""
    # Read what the script PRINTS, not what it merely mentions: every docstring
    # in the file quotes these strings, so a substring search over the source
    # passes even after the print is deleted.
    tree = ast.parse(SCRIPT.read_text())
    printed = "\n".join(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
    )
    for needle in ("discovered {len(routes)} Flask HTML routes",
                   "route-table coherence ADVISORY",
                   "covered by both tables"):
        assert needle in printed, f"{needle!r} is no longer PRINTED by {SCRIPT.name}"

    wf = (REPO / ".github" / "workflows" / "check-route-tables.yml").read_text()
    for needle in ("discovered [0-9]+ Flask HTML routes",
                   "route-table coherence ADVISORY",
                   "covered by both tables"):
        assert needle in wf, f"the ledger no longer greps {needle!r}"


def test_advisory_token_is_printed_only_on_failure():
    """The ledger maps the ADVISORY token to verdict=fail. If a clean run also
    printed it, the gate-liveness board would read `fail` on every green PR —
    the constant-red non-signal the ratchet exists to end."""
    tree = ast.parse(SCRIPT.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_diff")
    for node in ast.walk(fn):
        if not (isinstance(node, ast.JoinedStr) or isinstance(node, ast.Constant)):
            continue
        text = ast.unparse(node)
        if "route-table coherence ADVISORY" in text:
            assert "::error::" in text, (
                "the ADVISORY token appears outside the ::error:: line; the "
                "ledger would record fail on a clean run"
            )


def test_the_gate_blocks():
    """continue-on-error at job level made this unable to fail a PR at all."""
    # Parse the YAML — a prose mention of the flag in a comment is not the flag.
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "check-route-tables.yml").read_text())
    job = wf["jobs"]["check"]
    assert not job.get("continue-on-error"), (
        "job-level continue-on-error is back — the gate cannot fail a PR"
    )
    steps = {s.get("name", ""): s for s in job["steps"]}
    checkout = next(s for n, s in steps.items() if "canonical frontend" in n)
    assert checkout.get("continue-on-error") is True, (
        "the frontend checkout must KEEP its own continue-on-error so that "
        "'could not check' stays distinguishable from 'coherent'"
    )
    # …and the steps that measure must be gated on that outcome, or an absent
    # checkout reads as 'every route already baselined' and the job goes GREEN.
    for name in ("1 — extract Flask HTML routes",
                 "2 — extract _routes.json include + _worker.js PHASE_282 tables",
                 "3 — ratchet — fail on any NEW uncovered route"):
        assert steps[name]["if"] == "steps.tables.outputs.available == 'true'", name

    # The must-fail control must be able to RUN: its first CI run reported
    # selftest=fail on a green gate because the runner had no pytest. It is
    # gated on its own dep install so "could not run" stays reportable as
    # `absent` rather than masquerading as a failing control.
    assert steps["Must-fail control for this gate"]["if"] == (
        "always() && steps.selftest_deps.outcome == 'success'"
    )
    assert any((s.get("uses") or "").startswith("actions/setup-python")
               for s in job["steps"]), "no python pinned; the control cannot run"


def test_the_frontend_checkout_is_not_scanned_for_backend_routes(tmp_path, mod, monkeypatch):
    """CI checks the frontend out INSIDE the backend workspace. That repo holds
    126 .py files; one @app.route added to a build script there would inject a
    phantom backend route and fail this gate on a change that never touched the
    backend. Skipped by resolved path, not by directory name."""
    fe = tmp_path / "_frontend_canonical"
    fe.mkdir()
    (fe / "build.py").write_text(
        'from flask import Blueprint\n'
        'fe_bp = Blueprint("fe", __name__)\n'
        '@fe_bp.route("/not-a-backend-route")\ndef x():\n    return "x"\n'
    )
    (tmp_path / "main.py").write_text('from x import fe_bp\napp.register_blueprint(fe_bp)\n')
    monkeypatch.setattr(mod, "FRONTEND", fe)
    assert "/not-a-backend-route" not in mod.extract_flask_paths(tmp_path)
