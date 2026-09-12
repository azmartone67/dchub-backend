"""tests/test_route_auth_shell_transitive_reach.py — lane 3's three blind spots.

routes/route_auth_master_shell.py lane 3 ("UNAUTH OUTBOUND ACTION") could not
see the ungated IndexNow reach it existed to find:

  1. _OUTBOUND_SINKS contained "submit_indexnow" — a name that appears NOWHERE
     else in the repo. The submitters are submit_to_indexnow, ping_indexnow and
     ping_new_facilities, so that arm never matched a handler, ever.
  2. _is_route accepted only `@x.route(...)`, so every `@bp.post("/...")`
     handler was invisible to EVERY lane (241 handlers).
  3. _reaches_sink looked only at DIRECT calls, so a handler reaching a sink
     through a helper was missed — which is what all four of
     /api/cron/daily/preview, /api/v1/media/announcement, /api/outreach/run and
     /api/autopilot/seo/run do.

Widening reach without widening GATE detection turns every handler gated one
call away into a false positive, so the gate side is tested here too.

Run:  python3 -m pytest tests/test_route_auth_shell_transitive_reach.py -q
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _rec(src: str, rel: str) -> dict:
    from routes.route_auth_master_shell import _route_handlers
    src = textwrap.dedent(src)
    tree = ast.parse(src)
    return {"rel": rel, "base": os.path.basename(rel), "src": src,
            "src_lines": src.splitlines(), "tree": tree,
            "handlers": _route_handlers(tree)}


def _rec_file(rel: str) -> dict:
    src = (_ROOT / rel).read_text(encoding="utf-8")
    from routes.route_auth_master_shell import _route_handlers
    tree = ast.parse(src)
    return {"rel": rel, "base": os.path.basename(rel), "src": src,
            "src_lines": src.splitlines(), "tree": tree,
            "handlers": _route_handlers(tree)}


def _l3(recs):
    from routes.route_auth_master_shell import _detect_l3
    return _detect_l3(recs)


# ── 1 · the sink set must name functions that exist ───────────────────

def test_every_outbound_sink_name_exists_in_the_repo():
    """A sink name that matches no definition is a detector arm that can never
    fire. "submit_indexnow" sat in this set matching nothing at all."""
    from routes.route_auth_master_shell import _OUTBOUND_SINKS
    blob = []
    for f in sorted(_ROOT.rglob("*.py")):
        rel = str(f)
        if "/.git/" in rel or "/.claude/" in rel or "route_auth_master_shell" in rel:
            continue
        try:
            blob.append(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
    src = "\n".join(blob)
    # Floor: a reader that finds no source passes the loop below vacuously.
    assert len(src) > 5_000_000, f"source blob too small ({len(src)}) — reader broken"
    # Defined OR called: the detector matches a CALL name, so a sink defined in
    # an unscanned subpackage (services/daily/poster.py::post_to_x) still earns
    # its place, while a name that appears nowhere at all cannot ever match.
    missing = [n for n in sorted(_OUTBOUND_SINKS)
               if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}\s*\(", src)]
    assert not missing, (
        f"sink name(s) that appear nowhere in the repo: {missing} — such a name "
        "reads as coverage while matching no handler, ever (see submit_indexnow)")


def test_the_indexnow_submitters_are_all_in_the_sink_set():
    from routes.route_auth_master_shell import _OUTBOUND_SINKS
    for real in ("submit_to_indexnow", "ping_indexnow", "ping_new_facilities"):
        assert real in _OUTBOUND_SINKS, f"{real} is an IndexNow submitter and is not a sink"
    for dead in ("submit_indexnow", "post_to_moltbook", "tweet"):
        assert dead not in _OUTBOUND_SINKS, \
            f"{dead!r} matches nothing in the repo and is back in the sink set"


# ── 2 · @bp.post handlers are routes ──────────────────────────────────

_POST_DECORATED = '''
    @bp.post("/api/v1/thing/publish")
    def publish_thing():
        return submit_to_indexnow(["https://dchub.cloud/x"])
'''


def test_bp_post_handler_is_scanned_and_flagged():
    off = _l3([_rec(_POST_DECORATED, "synthetic_post.py")])
    assert [o["handler"] for o in off] == ["publish_thing"], \
        "@bp.post handler is invisible to lane 3"


def test_requests_get_is_not_mistaken_for_a_route():
    """@x.get is a route only when its first argument is a URL path. A bare
    requests.get(...) call decorating nothing must not become a handler."""
    from routes.route_auth_master_shell import _route_handlers
    src = textwrap.dedent('''
        @cache.get(timeout=30)
        def not_a_route():
            return 1

        @bp.get("/api/v1/ok")
        def is_a_route():
            return 2
    ''')
    names = {h.name for h in _route_handlers(ast.parse(src))}
    assert names == {"is_a_route"}, f"route detection picked up {names}"


# ── 3 · reach through a helper ────────────────────────────────────────

_VIA_HELPER = '''
    def publish_it(payload):
        submit_to_indexnow([payload["url"]])

    @app.route("/api/v1/x/publish", methods=["POST"])
    def x_publish():
        return publish_it(request.get_json())
'''


def test_reach_through_a_same_module_helper_is_flagged():
    off = _l3([_rec(_VIA_HELPER, "synthetic_helper.py")])
    assert [o["handler"] for o in off] == ["x_publish"], \
        "a sink one call past the handler is not seen"
    assert "publish_it" in off[0]["sink"] and "submit_to_indexnow" in off[0]["sink"], \
        f"the finding should name the path it took, got {off[0]['sink']!r}"


def test_reach_through_an_imported_helper_is_flagged():
    """The real shape: `from dchub_media import run_daily` inside the handler,
    where run_daily lives in another scanned module."""
    lib = _rec('''
        def run_daily():
            submit_to_indexnow(["https://dchub.cloud/news"])
    ''', "synthlib.py")
    app = _rec('''
        @app.route("/api/cron/x/preview", methods=["GET", "POST"])
        def x_preview():
            from synthlib import run_daily
            return run_daily()
    ''', "synthapp.py")
    off = _l3([lib, app])
    handlers = {o["handler"] for o in off}
    assert "x_preview" in handlers, "cross-module helper reach not seen"


def test_direct_reach_still_works():
    off = _l3([_rec('''
        @app.route("/api/v1/y", methods=["POST"])
        def y_publish():
            return submit_to_indexnow(["https://dchub.cloud/y"])
    ''', "synthetic_direct.py")])
    assert [o["handler"] for o in off] == ["y_publish"]


def test_reach_is_depth_bounded():
    """The bound is real, so a finding stays a short, checkable claim."""
    from routes.route_auth_master_shell import _REACH_MAX_DEPTH
    assert _REACH_MAX_DEPTH == 2
    deep = _rec('''
        def h4():
            submit_to_indexnow(["https://dchub.cloud/z"])

        def h3():
            return h4()

        def h2():
            return h3()

        @app.route("/api/v1/deep", methods=["POST"])
        def deep_publish():
            return h2()
    ''', "synthetic_deep.py")
    assert not _l3([deep]), "reach ran past its depth bound"


# ── 4 · gate detection must reach as far as sink detection ────────────

def test_helper_returned_denial_clears_the_handler():
    """`err = _require_admin_key(); if err: return err` — the shape
    routes/jobs_routes.py uses. Not in _GATE_CALLS under that spelling."""
    off = _l3([_rec('''
        def _require_admin_key():
            if not ok():
                return jsonify(error="unauthorized"), 401
            return None

        @app.route("/api/jobs/x", methods=["POST"])
        def job_x():
            auth_err = _require_admin_key()
            if auth_err:
                return auth_err
            return submit_to_indexnow(["https://dchub.cloud/x"])
    ''', "synthetic_gated.py")])
    assert not off, f"handler gated via a helper's returned 401 was flagged: {off}"


def test_decorator_that_denies_clears_the_handler():
    off = _l3([_rec('''
        def _require_admin(fn):
            def w(*a, **kw):
                if bad():
                    return jsonify(error="unauthorized"), 401
                return fn(*a, **kw)
            return w

        @bp.post("/api/v1/marketing/auto-generate")
        @_require_admin
        def auto_generate():
            return ping_indexnow(["https://dchub.cloud/pr"])
    ''', "synthetic_dec.py")])
    assert not off, f"@_require_admin handler was flagged: {off}"


def test_helper_that_raises_the_denial_clears_the_handler():
    off = _l3([_rec('''
        def _require_auth(authorization):
            if authorization != SECRET:
                raise HTTPException(401, "unauthorized")

        @router.post("/all")
        def publish_all(authorization=""):
            _require_auth(authorization)
            return post_to_linkedin("x")
    ''', "synthetic_raise.py")])
    assert not off, f"handler gated by a raising helper was flagged: {off}"


def test_a_downstream_401_does_not_count_as_a_gate():
    """The false-NEGATIVE guard. A handler that merely calls something which
    HANDLES a 401 from an upstream API is not gated. Accepting that shape
    silently cleared main.py::daily_cron, which has no gate at all."""
    off = _l3([_rec('''
        def post_to_linkedin(text):
            r = http("POST", text)
            if r.status == 401:
                refresh_token()
            return r

        @app.route("/api/cron/z", methods=["POST"])
        def z_cron():
            post_to_linkedin("hi")
            return submit_to_indexnow(["https://dchub.cloud/z"])
    ''', "synthetic_notgate.py")])
    assert [o["handler"] for o in off] == ["z_cron"], \
        "a downstream 401 handler was mistaken for this handler's gate"


# ── 5 · the seeds the lane is anchored on ─────────────────────────────

def test_lane3_seeds_still_behave():
    """Anchors from the shell's own test file: cross_post_email's email_best is
    an ungated outbound sender and must stay flagged; linkedin_poster gates the
    same class of sink and must stay clear."""
    off = _l3([_rec_file("routes/cross_post_email.py"),
               _rec_file("linkedin_poster.py")])
    files = {o["file"] for o in off}
    assert any("cross_post_email" in f for f in files), \
        "cross_post_email seed no longer flagged — lane 3 lost its positive anchor"
    assert not any("linkedin_poster" in f for f in files), \
        "linkedin_poster control wrongly flagged"


def test_the_routes_this_pr_gated_are_clear():
    """Each of the six now answers 401 before the sink, so lane 3 must clear
    them — the end-to-end proof that the gates dominate the reach."""
    off = _l3([_rec_file("main.py"),
               _rec_file("ai_outreach_agent.py"),
               _rec_file("routes/autopilot_routes.py"),
               _rec_file("auto_pilot.py"),
               _rec_file("intelligence_engine.py")])
    flagged = {(o["file"], o["handler"]) for o in off}
    for f, h in (("main.py", "_v1_daily_preview"),
                 ("main.py", "_v1_media_publish"),
                 ("ai_outreach_agent.py", "run_outreach"),
                 ("routes/autopilot_routes.py", "seo_run"),
                 ("auto_pilot.py", "seo_run"),
                 ("intelligence_engine.py", "api_daily_intelligence")):
        assert (f, h) not in flagged, f"{f}::{h} still reaches a sink ungated"


def _indexnow_reachers(rel):
    """{handler name} in `rel` that REACH an IndexNow submitter — gated or not.

    Deliberately not lane 3, which drops gated handlers. This asks only "can the
    detector still SEE the call", which is the half that goes silently wrong.
    """
    from routes.route_auth_master_shell import _reaches_sink, _module_index
    rec = _rec_file(rel)
    index = _module_index([rec])
    out = set()
    for fn in rec["handlers"]:
        sink = _reaches_sink(fn, rec, index)
        if sink and any(n in sink for n in _INDEXNOW_SINK_NAMES):
            out.add(fn.name)
    return out


def test_daily_cron_still_reaches_indexnow_and_is_now_gated():
    """The anchor that keeps the repo-wide scan honest now the registry is empty.

    main.py::daily_cron was the last ungated IndexNow reach, held open while its
    only caller (an external cron-job.org job) sent no credential. That caller
    now sends one and the handler is gated, so _ALLOWED_UNGATED_INDEXNOW is
    empty — which makes test_no_new_ungated_indexnow_reach pass on an EMPTY
    offender list, exactly what a blind detector also produces (mutation M17).

    So this pins the two halves apart: daily_cron must still be SEEN reaching
    submit_to_indexnow (it does — the gate refuses the caller, it does not remove
    the call), and must NOT be in the ungated set. Revert the sink names and the
    first assertion fails; delete the gate and the second does.
    """
    reachers = _indexnow_reachers("main.py")
    assert "daily_cron" in reachers, (
        "main.py::daily_cron no longer reads as reaching an IndexNow submitter "
        "— it still calls submit_to_indexnow, so the DETECTOR went blind "
        "(sink names, scan scope, or reach resolution)")
    assert "daily_cron" not in _ungated_indexnow_reach(), \
        "daily_cron is reaching IndexNow ungated again"


# ── 6 · the CI gate: no NEW ungated IndexNow reach ────────────────────

# EMPTY, 2026-09-12. Its one entry, main.py::daily_cron, was gated once the
# cron-job.org job began sending a credential. An empty set makes the "no new
# offenders" test below pass on an empty list — which is also what a BLIND
# detector produces — so test_daily_cron_still_reaches_indexnow_and_is_now_gated
# holds the scan to still seeing that call. Add an entry only with the reason it
# cannot be gated yet.
#
# This is the transitive counterpart of _KNOWN_UNGATED_INDEXNOW_REACH in
# tests/test_route_auth_criticals.py, which keys on DIRECT calls only. Every
# route in the sweep this file documents reached its submitter one or two calls
# past the handler body, so a direct-reach registry cannot see a new one.
_ALLOWED_UNGATED_INDEXNOW = set()

_INDEXNOW_SINK_NAMES = ("submit_to_indexnow", "ping_indexnow", "ping_new_facilities")


def _ungated_indexnow_reach():
    """{"<file>::<handler>"} for every route handler that reaches an IndexNow
    submitter over the WHOLE scanned tree with no gate deciding first."""
    from routes.route_auth_master_shell import _scan_routes
    out = set()
    scan = _scan_routes()
    assert not scan.get("error"), f"scan failed: {scan.get('error')}"
    # Floor: an empty scan would make the assertion below pass vacuously.
    assert scan["handlers"] > 2500, f"only {scan['handlers']} handlers scanned"
    for o in scan["l3"]:
        if any(n in o["sink"] for n in _INDEXNOW_SINK_NAMES):
            out.add(f"{o['file']}::{o['handler']}")
    return out


def test_no_new_ungated_indexnow_reach():
    """The gate that blocks the next one.

    A handler added tomorrow that reaches submit_to_indexnow through a helper
    fails here. Registering it is a deliberate act with a reason, the way
    daily_cron is registered.
    """
    found = _ungated_indexnow_reach()
    new = found - _ALLOWED_UNGATED_INDEXNOW
    assert not new, (
        "route handler(s) reach an IndexNow submitter with no gate deciding "
        f"first: {sorted(new)} — gate them with "
        "internal_auth.require_internal_or_admin as the first statement, or "
        "register them here with the reason they cannot be gated yet")


def test_every_registered_exception_is_still_real():
    """The other direction: a registry nobody prunes is how an accepted exposure
    becomes a permanent one. If an entry stops being an offender — gated, or the
    detector stopped seeing it — it must be deleted in that same change.

    DORMANT while _ALLOWED_UNGATED_INDEXNOW is empty, and deliberately kept: it
    is what makes the next registration safe to add. The live proof that the
    scan is not simply blind is
    test_daily_cron_still_reaches_indexnow_and_is_now_gated, which does not
    depend on this set at all."""
    found = _ungated_indexnow_reach()
    stale = _ALLOWED_UNGATED_INDEXNOW - found
    assert not stale, (
        f"{sorted(stale)} no longer reaches IndexNow ungated — if you gated it, "
        "delete it from _ALLOWED_UNGATED_INDEXNOW in this same change")
