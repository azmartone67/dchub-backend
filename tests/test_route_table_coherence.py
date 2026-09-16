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


# ── _redirects: the third table ────────────────────────────────────

def test_redirects_splat_does_NOT_match_the_bare_path(mod):
    """★ THE INVERTED RULE, and the whole reason _redirect_re() is not _glob_re().

    _routes.json's "/x/*" DOES route bare "/x" (test_a_slash_star_include_also_
    routes_the_bare_path pins that). _redirects' "/x/*" does NOT answer bare
    "/x" — dchub-frontend writes a separate bare line every time it needs one.
    Reuse _glob_re() here and every bare "/x" reads as shadowed on the strength
    of a rule that cannot answer it, inventing coverage for a real 404.
    """
    rules = [["/spare-capacity/*", "/listings", 301]]
    assert mod.redirect_match(rules, "/spare-capacity/abc") is not None
    assert mod.redirect_match(rules, "/spare-capacity/") is not None
    assert mod.redirect_match(rules, "/spare-capacity") is None, (
        "_redirect_re() picked up _routes.json's bare-path rule; a splat in "
        "_redirects does not answer the bare path"
    )
    # And the two matchers must genuinely disagree, or the port drifted back.
    assert mod._covers(["/spare-capacity/*"], "/spare-capacity"), (
        "_glob_re() stopped routing the bare path — _routes.json semantics moved"
    )


def test_redirects_placeholder_is_one_segment_and_splat_crosses_slashes(mod):
    """":slug" is a single segment; "*" is not. _routes.json has no placeholder."""
    assert mod.redirect_match([["/press-release/:slug", "/press-release", 200]],
                              "/press-release/abc") is not None
    assert mod.redirect_match([["/press-release/:slug", "/press-release", 200]],
                              "/press-release/abc/def") is None
    assert mod.redirect_match([["/dcip/*", "/dcpi/:splat", 301]],
                              "/dcip/a/b") is not None
    # A splat that is not a whole segment still works: "/images/og-*".
    assert mod.redirect_match([["/images/og-*", "/og-:splat", 200]],
                              "/images/og-x.png") is not None
    assert mod.redirect_match([["/images/og-*", "/og-:splat", 200]],
                              "/images/other.png") is None


def test_first_matching_redirect_wins(mod):
    """Order is the semantics — CF stops at the FIRST match, so a set would lie.

    ★ The two rules must BOTH match the probe path or this proves nothing. An
    earlier version paired "/spare-capacity" with "/spare-capacity/*", which do
    NOT overlap (that is the point of the bare-path rule), so first-vs-last was
    indistinguishable and a last-match-wins mutant survived.
    """
    specific = ["/a/specific", "/first", 301]
    splat = ["/a/*", "/second", 301]
    assert mod.redirect_match([specific, splat], "/a/specific")[1] == "/first"
    assert mod.redirect_match([splat, specific], "/a/specific")[1] == "/second", (
        "reversing the file did not change the winner — order is not being honoured"
    )


def test_redirects_parser_drops_malformed_lines_loudly(mod, tmp_path, capsys):
    """"could not read it" and "no such rule" must never be one outcome."""
    (tmp_path / "_redirects").write_text(
        "# comment\n\n/a /b 301\n/c /d\nnot-a-path /x 301\n/e /f notanumber\n")
    rules = mod.parse_redirects(tmp_path)
    assert ["/a", "/b", 301] in rules
    assert ["/c", "/d", 302] in rules, "a 2-field rule is legal CF; status defaults to 302"
    assert all(r[0] != "not-a-path" for r in rules)
    assert len(rules) == 2
    err = capsys.readouterr().err
    assert "not-a-path" in err and "notanumber" in err, (
        "malformed rules were dropped SILENTLY"
    )


def test_baseline_shadowed_entries_are_really_shadowed(mod):
    """The register must not claim a redirect that is gone.

    A line in shadowed_by_redirects asserts "a _redirects rule answers this".
    If the rule is deleted the path silently 404s while the register still reads
    as the benign case \u2014 so pin the claim against the actual file.
    """
    frontend = mod.FRONTEND  # resolved from ROUTE_COHERENCE_FRONTEND at import
    if not (frontend / "_redirects").is_file():
        pytest.skip(f"no dchub-frontend checkout at {frontend}; this test pins "
                    f"the register against the real file, not the parser")
    rules = mod.parse_redirects(frontend)
    assert rules, "parsed zero rules from a file that exists"
    listed = json.loads(BASELINE.read_text())["shadowed_by_redirects"]
    assert listed, "the third list is empty; it should hold the measured stubs"
    for route in listed:
        assert mod.redirect_match(rules, mod._probe(route)) is not None, (
            f"{route} is baselined as shadowed_by_redirects but no _redirects "
            f"rule answers it \u2014 the register claims a redirect that is gone"
        )


def test_baseline_is_three_lists_not_a_union():
    """Union-baselining would let a path migrate from a 404 to a 403 silently."""
    data = json.loads(BASELINE.read_text())
    assert "missing_routes_json" in data and "missing_worker" in data
    assert "shadowed_by_redirects" in data, (
        "the _redirects half is gone; reachable-but-handler-dead paths would be "
        "back in missing_routes_json recorded as 404s"
    )
    assert not (set(data["missing_routes_json"]) & set(data["shadowed_by_redirects"])), (
        "a path is in both _routes.json debt lists; it would be double-counted"
    )
    assert "uncovered" not in data, "collapsed back to a single union list"
    assert "worker_measured_forwarded" in data, (
        "the measured exception list is gone; the worker model's false positives "
        "would be back in the register as if they were real debt"
    )
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


# ── Cloudflare Pages glob semantics ──────────────────────────────────────────

def test_a_slash_star_include_also_routes_the_bare_path(mod):
    """dchub-frontend/scripts/check-edge-caps.mjs is the authority and its own
    note records getting this backwards: "/redeem/*" DOES route bare "/redeem".
    Measured 2026-09-05: GET /redeem is 200 and carries x-dc-worker-version.
    Reading it the other way puts three reachable paths in the debt register."""
    inc = ["/redeem/*"]
    assert mod._covers(inc, "/redeem")
    assert mod._covers(inc, "/redeem/")
    assert mod._covers(inc, "/redeem/abc")
    assert not mod._covers(inc, "/redeemer")


def test_exclude_claws_a_bare_path_back_out_of_a_wildcard(mod):
    """15 exclude entries exist for exactly this. A checker reading only
    `include` calls /pricing covered; it answers 200 from a static file with NO
    x-dc-worker-version — the silent failure this gate exists to catch."""
    tables = {"routes_json_include": ["/pricing/*", "/iso/*"],
              "routes_json_exclude": ["/pricing", "/iso/*.json"],
              "worker_paths": ["/pricing", "/iso/x", "/iso/x.json"],
              "worker_prefixes": []}
    mr, _ = mod._uncovered({"/pricing", "/pricing/plans", "/iso/x", "/iso/x.json"}, tables)
    assert "/pricing" in mr and "/iso/x.json" in mr
    assert "/pricing/plans" not in mr and "/iso/x" not in mr


def test_a_dynamic_route_is_probed_as_a_concrete_path(mod):
    """"/news/<slug>" truncated at "<" becomes "/news/", which hits the
    deliberate "/news/" exclude — while the pages it serves match "/news/*" and
    ARE worker-routed (GET /news/some-article carries x-dc-worker-version)."""
    tables = {"routes_json_include": ["/news/*"],
              "routes_json_exclude": ["/news", "/news/"],
              "worker_paths": [], "worker_prefixes": ["/news/"]}
    mr, mw = mod._uncovered({"/news/<slug>"}, tables)
    assert mr == [] and mw == [], (mr, mw)


# ── only the sound half blocks ───────────────────────────────────────────────

def _run_diff(tmp_path, mod, monkeypatch, flask, tables, baseline):
    """Drive cmd_diff end to end against synthetic inputs."""
    routes = tmp_path / "flask.json"; routes.write_text(json.dumps(sorted(flask)))
    tbl = tmp_path / "tables.json";   tbl.write_text(json.dumps(tables))
    base = tmp_path / "baseline.json"; base.write_text(json.dumps(baseline))
    monkeypatch.setattr(mod, "FLASK_ROUTES_OUT", routes)
    monkeypatch.setattr(mod, "TABLES_OUT", tbl)
    monkeypatch.setattr(mod, "BASELINE", base)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod.cmd_diff(None)


_TABLES = {"routes_json_include": ["/docs/*"], "routes_json_exclude": [],
           "worker_paths": ["/docs/known"], "worker_prefixes": []}
_EMPTY = {"missing_routes_json": [], "missing_worker": [], "worker_measured_forwarded": []}


def test_a_new_routes_json_miss_FAILS(tmp_path, mod, monkeypatch, capsys):
    """The sound half. _routes.json include/exclude is the COMPLETE answer to
    "does the worker run for this path", so this one is allowed to block."""
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/not-in-any-table"}, _TABLES, _EMPTY)
    assert rc == 1
    out = capsys.readouterr().out
    assert "route-table coherence ADVISORY" in out
    assert "★NEW UNCOVERED [missing_routes_json]: /not-in-any-table" in out


def test_a_new_worker_only_miss_is_REPORTED_but_does_not_fail(tmp_path, mod, monkeypatch, capsys):
    """The unsound half. Measured 2026-09-05, 9 of 47 probed worker-only entries
    were forwarded to Flask anyway — PHASE_282 membership is SUFFICIENT for
    forwarding, not NECESSARY. Failing on a not-necessary condition is how this
    gate got switched off the first time."""
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/docs/new-page"}, _TABLES, _EMPTY)
    assert rc == 0, "a worker-table miss must NOT fail the build"
    out = capsys.readouterr().out
    assert "newly absent from" in out and "/docs/new-page" in out, (
        "…but it must still be REPORTED; silent is not the same as advisory"
    )
    assert "route-table coherence ADVISORY" not in out, (
        "the ADVISORY token is the ledger's fail signal; a worker-only miss "
        "would peg the gate-liveness board at fail"
    )


def test_measured_forwarded_overrides_the_model(tmp_path, mod, monkeypatch, capsys):
    """/poe answers 405 WITH x-railway-request-id: Flask got the request and
    refused the method. The model says PHASE_282 lacks it. The measurement wins."""
    base = dict(_EMPTY, worker_measured_forwarded=["/docs/poe"])
    _run_diff(tmp_path, mod, monkeypatch, {"/docs/poe"}, _TABLES, base)
    out = capsys.readouterr().out
    assert "/docs/poe" not in out, "a path proven to reach the origin is not debt"


def test_the_docstring_states_why_the_worker_half_cannot_block():
    """If someone later makes it blocking, the three measured reasons should be
    sitting right there rather than needing to be rediscovered by probing."""
    doc = SCRIPT.read_text()
    assert "WHY THE WORKER HALF DOES NOT BLOCK" in doc
    for reason in ("proxyWithRetry", "x-dc-worker-version", "worker-mcp-get-health"):
        assert reason in doc, f"the {reason!r} evidence is gone"


# ── _redirects: the third category, at the cmd_diff level ────────────────────
#
# ★ These drive the REAL entry point. A test that calls _uncovered() and asserts
# the path is uncovered passes whether or not the _redirects split exists — it
# would be green on a checker that never read the file at all.

_R_TABLES = dict(_TABLES, redirects=[["/stub", "/somewhere", 301]])
_R_EMPTY = dict(_EMPTY, shadowed_by_redirects=[])


def test_a_shadowed_route_reports_as_shadowed_not_as_covered(tmp_path, mod, monkeypatch, capsys):
    """★ The state that must not collapse into green.

    A _redirects rule makes the path REACHABLE and the Flask handler DEAD.
    Folding it into the covered set blinds the gate to a rule that shadowed a
    real page — the /pockets class it exists to catch.
    """
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/stub"}, _R_TABLES, _R_EMPTY)
    out = capsys.readouterr().out
    # ★ Assert the CATEGORY LABEL, not the bare key name: "shadowed_by_redirects"
    # also appears in the ★NEW error line, so keying on it let a mutant that
    # dropped the category from the report entirely survive.
    assert "ANSWERED BY A _redirects RULE" in out, (
        "the third category is not reported on its own — shadowed paths are "
        "collapsing into another bucket"
    )
    assert "/stub" in out and "/somewhere" in out, (
        "the matching rule is not printed next to the path, so a reviewer cannot "
        "tell an intended stub from a shadowed page"
    )
    assert "NEVER INVOKED and nothing else replies" not in out, (
        "a shadowed path was filed under the plain 404 label"
    )
    assert rc == 1, "a NEW shadowed route stopped failing the build"


def test_a_redirect_does_not_let_a_new_route_dodge_the_ratchet(tmp_path, mod, monkeypatch, capsys):
    """★ THE FATAL SET IS THE UNION OF BOTH _routes.json HALVES.

    Read only the plain half and a route arriving with a _redirects rule already
    in place lands silently — exactly the /spare-capacity shape this gate should
    still make someone look at.
    """
    with_rule = _run_diff(tmp_path, mod, monkeypatch, {"/stub"}, _R_TABLES, _R_EMPTY)
    capsys.readouterr()
    without = _run_diff(tmp_path, mod, monkeypatch, {"/stub"},
                        dict(_TABLES, redirects=[]), _R_EMPTY)
    capsys.readouterr()
    assert without == 1, "control: a new uncovered route must fail"
    assert with_rule == 1, (
        "a new route dodged the ratchet by having a _redirects rule; acquiring a "
        "redirect must not pay off debt the route never had"
    )


def test_baselining_a_shadow_pays_the_debt(tmp_path, mod, monkeypatch, capsys):
    """Or the third list is an allow-list that can never go green."""
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/stub"}, _R_TABLES,
                   dict(_EMPTY, shadowed_by_redirects=["/stub"]))
    out = capsys.readouterr().out
    assert rc == 0, "a baselined shadow still failed the build"
    assert "covered by both tables" in out, "the ledger's verdict string is gone"


def test_a_vanished_redirect_rule_is_reported_as_a_regression(tmp_path, mod, monkeypatch, capsys):
    """shadow → plain means the rule that answered the path is gone: it 404s now.
    Non-fatal, because it is normally a dchub-frontend commit — but it must not
    read as ordinary pre-existing drift."""
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/stub"},
                   dict(_TABLES, redirects=[]),
                   dict(_EMPTY, shadowed_by_redirects=["/stub"]))
    out = capsys.readouterr().out
    assert rc == 0, "a cross-repo rule removal must not fail the build"
    assert "REGRESSION" in out, (
        "a path that lost the redirect answering it was reported as plain drift"
    )


def test_a_baseline_without_the_third_key_still_loads(tmp_path, mod, monkeypatch, capsys):
    """_EMPTY above has no shadowed_by_redirects. An older register must not
    crash the gate — it must simply report no known shadows."""
    rc = _run_diff(tmp_path, mod, monkeypatch, {"/stub"}, _R_TABLES,
                   {"missing_routes_json": ["/stub"], "missing_worker": ["/stub"]})
    capsys.readouterr()
    assert rc == 0, "a pre-existing debt line stopped being honoured"
