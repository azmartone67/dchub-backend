"""tests/test_agentic_loop_inspect.py — the owner inspection dashboard
(Agentic Loop master shell #65, part D).

What this page must REFUSE to do is most of what makes it safe:

  - open without a key (403), or 5xx when disabled (404 — the CF worker fails
    the whole site over on any 5xx from Railway)
  - turn model text into markup: an analysis/decision/statement that contains
    a <script> must render inert (mutation: drop the escape -> RED)
  - forward anything but its allow-list: an unknown action is 404 and never
    forwarded; the drain is forced to ?dry_run=1 whatever the caller sent
  - fetch anything the app does not serve: every fetch URL in the page and
    every forwarded target is checked against the BOOTED app's url_map, with
    the not-yet-deployed A/B/C routes pinned in EXPECTED_LATER both ways
  - load anything external (no <script src=, no http(s) <link>, no CDN)

House rules: no DB, main.py is never imported in-process (the url_map check
boots it in a SUBPROCESS the way scripts/app_contract_gate.py does).
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "routes", "agentic_loop_inspect.py")
_MAIN = os.path.join(_ROOT, "main.py")
_STRICT = os.environ.get("DCHUB_CONTRACT_GATE_STRICT") == "1"

# 64-hex shaped like the real DCHUB_ADMIN_KEY; a fake.
_KEY = "a3f" + "9d4e1c" * 10 + "b7"
_XSS = "<script>alert(1)</script>"
_XSS_ESC = "&lt;script&gt;alert(1)&lt;/script&gt;"
_H = {"X-Admin-Key": _KEY}
_BOOT_CACHE: dict = {}


def _mod():
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import routes.agentic_loop_inspect as m
    return m


@pytest.fixture
def app(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _KEY)
    monkeypatch.delenv("AGENTIC_LOOP_SHELL_DISABLE", raising=False)
    m = _mod()
    a = Flask("agentic-loop-inspect-test")
    a.register_blueprint(m.agentic_loop_inspect_bp)
    return a


def _helpers(m):
    return [m.API_PREFIX, m.API_PREFIX + "/metrics", m.API_PREFIX + "/tab/claims"]


def _stub(payloads: dict, calls: list | None = None):
    """A _forward double: known path -> 200 + payload; unknown -> the
    'not registered' 404 shape."""
    def fake(method, path, query=None, body=None):
        if calls is not None:
            calls.append((method, path, dict(query or {}), body))
        if path in payloads:
            return {"ok": True, "status": 200, "data": payloads[path],
                    "unavailable": False, "error": None, "path": path}
        return {"ok": False, "status": 404, "data": None, "unavailable": True,
                "error": None, "path": path}
    return fake


# ── gate: 403 without a key, 404 when disabled, never 5xx ───────────────────

def test_no_key_is_403_on_page_and_every_helper(app):
    m = _mod()
    c = app.test_client()
    r = c.get(m.PAGE_PATH)
    assert r.status_code == 403 and "text/html" in r.content_type
    for p in _helpers(m):
        assert c.get(p).status_code == 403, p
    assert c.post(m.API_PREFIX + "/act/claims_verify", json={}).status_code == 403


def test_wrong_key_is_403(app):
    m = _mod()
    c = app.test_client()
    assert c.get(m.PAGE_PATH, headers={"X-Admin-Key": "nope"}).status_code == 403
    assert c.get(m.PAGE_PATH + "?key=nope").status_code == 403
    assert c.get(m.API_PREFIX, headers={"Cookie": "dchub_admin=nope"}).status_code == 403


def test_query_key_opens_the_page_and_sets_the_bookmark_cookie(app):
    m = _mod()
    c = app.test_client()
    r = c.get(m.PAGE_PATH + "?key=" + _KEY)
    assert r.status_code == 200 and "text/html" in r.content_type
    sc = r.headers.get("Set-Cookie") or ""
    assert sc.startswith("dchub_admin=") and "HttpOnly" in sc and "Secure" in sc
    assert "no-store" in (r.headers.get("Cache-Control") or "")
    # the cookie alone then opens the page AND the helpers (the page's JS has
    # nothing else to send — dchub_admin is httponly)
    cookie = {"Cookie": "dchub_admin=" + _KEY}
    assert c.get(m.PAGE_PATH, headers=cookie).status_code == 200
    assert c.get(m.API_PREFIX, headers=cookie).status_code == 200
    # a bare page visit without ?key= sets no cookie
    assert not (c.get(m.PAGE_PATH, headers=_H).headers.get("Set-Cookie") or "")


def test_header_key_opens_the_index_which_describes_the_contract(app):
    m = _mod()
    r = app.test_client().get(m.API_PREFIX, headers=_H)
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["page"] == m.PAGE_PATH
    assert set(d["tabs"]) == set(m.TABS)
    assert set(d["actions"]) == set(m.ACTIONS)
    targets = {p for p, _ in m.READS.values()} | {s["path"] for s in m.ACTIONS.values()}
    assert set(d["registered"]) == targets
    # this bare test app serves none of the targets — the index says so
    # rather than pretending
    assert not any(d["registered"].values())
    assert set(d["expected_later"]) == set(m.EXPECTED_LATER) <= targets


def test_kill_switch_is_404_everywhere_and_never_5xx(app, monkeypatch):
    m = _mod()
    monkeypatch.setenv("AGENTIC_LOOP_SHELL_DISABLE", "1")
    c = app.test_client()
    for p in [m.PAGE_PATH] + _helpers(m):
        for hdr in (_H, {}):
            code = c.get(p, headers=hdr).status_code
            assert code == 404, (p, hdr, code)
    code = c.post(m.API_PREFIX + "/act/claims_verify", json={}, headers=_H).status_code
    assert code == 404


def test_disabled_branches_return_404_by_ast():
    """Static twin of the live check: every int returned inside an
    `if _disabled():` block is 404. Extraction asserted non-empty."""
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    codes = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and isinstance(n.value, int) \
                        and not isinstance(n.value, bool):
                    codes.append(n.value)
    assert codes, "no `if _disabled():` status codes found — extraction empty"
    assert set(codes) == {404}, codes


# ── escaping: model text renders inert in EVERY tab ─────────────────────────

def _xss_payloads(m):
    claims = {"ok": True, "count": 1, "claims": [{
        "id": 1, "kind": "canon" + _XSS, "subject": "subj " + _XSS,
        "statement": "stmt " + _XSS, "regime": {"as_of": _XSS},
        "surfaces": [_XSS], "expected_metric": _XSS, "expected_value": _XSS,
        "horizon_hours": 24, "shipped_at": "2026-08-20T00:00:00+00:00",
        "outcome": "refuted", "outcome_evidence": {"actual": _XSS, "evidence": _XSS},
        "outcome_at": None, "due_at": None, "superseded_by": None,
        "registered_at": None}]}
    # ★ known=False + error poisons routes/agentic_loop_inspect.py's "registry
    # unreadable" line. Until 2026-08-22 no fixture set it, so replacing that
    # _h() with str() left the whole file at 50 passed.
    classes = {"ok": True, "known": False, "error": "registry " + _XSS,
               "enabled": False,
               "caps": {"per_day": 5, "per_drain": 2, "breaker_after": 3},
               "day_used": 0, "verified_7d": 0,
               "classes": [{"class": "cls" + _XSS, "granted": False,
                            "granted_by": _XSS, "granted_at": None,
                            "bound_params": {"k": _XSS}, "verifier_url": _XSS,
                            "reversible": True, "runs_ok": 0, "runs_failed": 0,
                            "consecutive_failed": 0, "last_run_at": None,
                            "breaker_tripped": True, "notes": _XSS,
                            "grant_ok": False, "grant_reason": _XSS}],
               "inbox_by_class": {},
               # part B rides the graduation READ on this GET (file=False)
               "graduation": {"proposals": [_XSS], "note": _XSS},
               "plan": {"ok": True, "ran": "ran " + _XSS,
                        "day_used": "used " + _XSS, "day_cap": "cap " + _XSS,
                        # the dry-run plan's own note — an upstream string that
                        # reached markup with nothing poisoning it
                        "note": "plan note " + _XSS,
                        "candidates": [{"queue_id": 1, "class": _XSS,
                                        "action_url": _XSS, "skip": _XSS}],
                        "results": [{"note": _XSS}]}}
    inbox = {"ok": True, "counts": {"awaiting_decision": 1}, "rows": [{
        "id": 7, "finding_key": "key " + _XSS, "title": "title " + _XSS,
        "status": "awaiting_decision", "reason": "reason " + _XSS,
        "confidence": 0.4, "analysis": "analysis " + _XSS,
        "decision": "decision " + _XSS,
        "requested_at": "2026-08-20T00:00:00+00:00", "finished_at": None,
        "action_class": "class " + _XSS, "action_url": "/x?" + _XSS,
        "action_method": "POST", "seen_count": 2, "last_seen": None}]}
    whats_new = {"ok": True, "platform": [{"id": "p" + _XSS, "tag": _XSS, "title": _XSS,
                                           "announced": "2026-08-17", "link_href": _XSS}],
                 "platform_pending": 1, "platform_withheld": [{"id": "w" + _XSS, "reason": _XSS}],
                 "platform_as_of": None}
    findings = {"ok": True, "total_rows": 1, "live_columns": ["detector"],
                "by_detector": {_XSS: 3, "stored_slug_404": 2},
                "recent": [{"issue": _XSS, "count": 1, "detail": _XSS, "seen_count": 1}]}
    ops = {"ok": True, "week": {"week_start": _XSS, "as_of": _XSS,
                                "brain_prs_with_detector": {
        "with_detector": 1, "checked": 2, "unknown": 0, "basis": _XSS, "prs": [_XSS]}}}
    shell = {"ok": True, "shell": _XSS, "lanes": [{
        "name": _XSS, "verdict": "FAIL",
        "checks": [{"name": _XSS, "detail": _XSS, "pass": False, "critical": True}]}]}
    enh = {"ok": True, "proposals": [{"id": 1, "status": "rejected", "area": _XSS,
                                      "title": _XSS, "grade": _XSS, "created_at": None}]}
    # part C ships learn_station_status() INSIDE the recall response; the
    # lessons tab reads it from there, so the poisoned value lives there.
    recall = {"ok": True, "lessons": [_XSS], "status": {"corpus": _XSS}}
    R = {k: v[0] for k, v in m.READS.items()}
    return {R["claims"]: claims, R["classes"]: classes, R["inbox"]: inbox,
            R["whats_new"]: whats_new, R["findings"]: findings, R["ops_claims"]: ops,
            R["shell"]: shell, R["enhancements"]: enh, R["learn_recall"]: recall,
            R["queue_ages"]: {"ok": True, "y": _XSS}}


# Per tab: the query to send and a marker proving the payload reached the
# renderer (a vacuous fragment would pass the "no <script>" check for free).
_TAB_CASES = [
    ("claims", "", "stmt "),
    ("classes", "", "grant test"),
    ("inbox", "", "analysis "),
    ("platform", "", "approval mechanism"),
    ("lessons", "?q=deals", "recall for"),
    ("detectors", "", "product detectors"),
    ("shell", "", "FAIL"),
]


@pytest.mark.parametrize("tab,query,marker", _TAB_CASES, ids=[c[0] for c in _TAB_CASES])
def test_model_text_with_a_script_tag_renders_inert(app, monkeypatch, tab, query, marker):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub(_xss_payloads(m)))
    monkeypatch.setattr(m, "_store_entries", lambda: {"w" + _XSS: {"status": _XSS, "announced": None}})
    r = app.test_client().get(m.API_PREFIX + "/tab/" + tab + query, headers=_H)
    assert r.status_code == 200 and "text/html" in r.content_type
    body = r.get_data(as_text=True)
    assert marker in body, "payload never reached the %s renderer — the check below is vacuous" % tab
    assert _XSS not in body, "raw <script> survived in the %s tab" % tab
    assert _XSS_ESC in body, "the escaped form is absent from the %s tab" % tab
    # attribute context too: the text sits inside data-*="" on action buttons
    assert 'alert(1)</script>"' not in body


def test_every_action_button_attribute_is_escaped(app, monkeypatch):
    """A class name or row id with a quote must not break out of data-* —
    the attribute context is where a missing quote=True would bite."""
    m = _mod()
    p = _xss_payloads(m)
    cls_path = m.READS["classes"][0]
    p[cls_path]["classes"][0]["class"] = 'x" onmouseover="alert(2)'
    monkeypatch.setattr(m, "_forward", _stub(p))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes", headers=_H).get_data(as_text=True)
    assert 'onmouseover="alert(2)"' not in body
    assert "x&quot; onmouseover=&quot;alert(2)" in body


# Two keys a renderer reads out of the _forward ENVELOPE rather than out of an
# upstream payload, and the only ones exempt from the census below: `path` is a
# literal copied from READS/ACTIONS (a constant in this repo, never model text)
# and `unavailable` is a bool _forward computes and never renders. Everything
# else a renderer .get()s is upstream text that reaches markup.
_ENVELOPE_ONLY_KEYS = {"path", "unavailable"}

# The functions that turn upstream payloads into HTML. The census is scoped to
# these on purpose: _admin_ok reads request headers, act_post reads the ACTIONS
# table, _store_entries reads a file through its owning module — none of those
# is a renderer and none of their keys is markup.
_RENDERER_FNS = {"_claim_row", "_class_row", "_inbox_row", "_unavail"}
# Names bound to something the PAGE built, not to an upstream payload.
_RENDERER_LOCAL_RECEIVERS = {"args", "q", "spec", "os", "request", "attrs"}

# What _store_entries() is stubbed to return in the escaping tests.
_POISONED_STORE = {"w" + _XSS: {"status": _XSS, "announced": _XSS}}


def _err_stub(err, status: int = 503):
    """A _forward double whose every answer is a READABLE FAILURE — the
    `_unavail` error branch (routes/agentic_loop_inspect.py:374-377). Not
    `unavailable`: that is the not-deployed-yet path and renders no upstream
    string at all."""
    def fake(method, path, query=None, body=None):
        return {"ok": False, "status": status, "data": None,
                "unavailable": False, "error": err, "path": path}
    return fake


def _xss_variants(m):
    """(tab, query, payloads) for branches the main bundle cannot reach.

    A renderer branch that no fixture enters is a branch whose escape is
    unguarded — precisely how routes/agentic_loop_inspect.py:688 shipped with
    the escaping mutation surviving at 50 passed."""
    R = {k: v[0] for k, v in m.READS.items()}
    unmeasured = dict(_xss_payloads(m))
    # platform_unavailable_reason renders ONLY when `platform` is not a list
    unmeasured[R["whats_new"]] = {
        "ok": True, "platform": None,
        "platform_unavailable_reason": "reason " + _XSS,
        "platform_pending": 1, "platform_withheld": [], "platform_as_of": None}
    return [("platform", "", unmeasured, "reason ")]


@pytest.mark.parametrize("tab", sorted(t[0] for t in _TAB_CASES))
def test_an_upstream_error_string_renders_escaped_in_every_tab(app, monkeypatch, tab):
    """An upstream that answers with a poisoned `error` must not put it in the
    markup raw. This is _unavail's error branch, which every tab reaches and
    which NO payload fixture poisoned before 2026-08-22 — the audit applied
    `_h(res.get("error"))` -> `str(...)` and the file stayed 50/50 green."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _err_stub("upstream " + _XSS))
    monkeypatch.setattr(m, "_store_entries", lambda: _POISONED_STORE)
    r = app.test_client().get(m.API_PREFIX + "/tab/" + tab + "?q=x", headers=_H)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "unreadable" in body, "the error branch was never reached in %s" % tab
    assert "upstream " in body, "the poisoned error never reached the %s renderer" % tab
    assert _XSS not in body, "a raw <script> from an upstream error survived in %s" % tab
    assert _XSS_ESC in body, "the escaped form is absent from the %s tab" % tab


@pytest.mark.parametrize("tab,query,payloads,marker", _xss_variants(_mod()),
                         ids=[c[0] + "-variant" for c in _xss_variants(_mod())])
def test_branch_only_upstream_strings_render_escaped(app, monkeypatch, tab, query,
                                                     payloads, marker):
    """platform_unavailable_reason (routes/agentic_loop_inspect.py:688) renders
    only when `platform` is not a list, a shape no fixture produced."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub(payloads))
    monkeypatch.setattr(m, "_store_entries", lambda: _POISONED_STORE)
    body = app.test_client().get(m.API_PREFIX + "/tab/" + tab + query,
                                 headers=_H).get_data(as_text=True)
    assert marker in body, "the branch was never entered — the check below is vacuous"
    assert _XSS not in body
    assert _XSS_ESC in body


def test_the_dry_run_plan_note_and_registry_error_reach_the_page_escaped(app, monkeypatch):
    """Two more upstream strings the audit found unguarded: the plan's own
    `note` (:539) and the class registry's `error` when known=false (:513).
    Both must PROVE they reached the renderer before the escape is asserted."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub(_xss_payloads(m)))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes", headers=_H).get_data(as_text=True)
    assert "registry unreadable" in body and "registry &lt;script&gt;" in body
    assert "plan note &lt;script&gt;" in body
    assert _XSS not in body


def test_every_upstream_key_a_renderer_reads_is_poisoned_by_a_fixture():
    """THE CENSUS. Escaping is only as good as the payloads the tests poison:
    :374, :513, :539 and :688 were each correctly escaped and each survived the
    escaping mutation at 50 passed, because no fixture entered those branches.

    ★ WHAT THIS DOES AND DOES NOT CATCH, precisely — a census that oversells
    itself is the same failure it exists to prevent. It is keyed by KEY NAME,
    so it fails when a renderer reads an upstream key that NO fixture in this
    file poisons anywhere. Of the four sites above it would have caught three
    (`error` at :374 and :513, `platform_unavailable_reason` at :688) and NOT
    the fourth (`note` at :539), because `note` was already poisoned elsewhere
    in the classes payload. That fourth site is covered instead by
    test_the_dry_run_plan_note_and_registry_error_reach_the_page_escaped, which
    asserts a value marker unique to that site ("plan note …"). Between them:
    every named site is mutation-proved, and a NEW key added to any renderer is
    red here on the first run."""
    m = _mod()
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    read_keys: dict = {}
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not (fn.name.startswith("_tab_") or fn.name in _RENDERER_FNS):
            continue
        for n in ast.walk(fn):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr != "get" or not n.args:
                continue
            if getattr(n.func.value, "id", None) in _RENDERER_LOCAL_RECEIVERS:
                continue
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                read_keys.setdefault(a0.value, set()).add(fn.name)
    assert len(read_keys) >= 100, (
        "renderer key census collapsed to %d keys — the extraction broke, and "
        "a census that finds nothing cannot fail" % len(read_keys))

    poisoned: set = set()

    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                poisoned.add(k)
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(_xss_payloads(m))
    for _tab, _q, payloads, _marker in _xss_variants(m):
        _walk(payloads)
    _walk(_POISONED_STORE)
    # the readable-failure envelope _err_stub returns
    _walk({"ok": False, "status": 0, "data": None, "error": _XSS})

    unpoisoned = sorted(k for k in read_keys
                        if k not in poisoned and k not in _ENVELOPE_ONLY_KEYS)
    assert not unpoisoned, (
        "these upstream keys reach the page but no escaping fixture poisons "
        "them, so the escape at each site would survive being deleted: %s\n"
        "Add them to _xss_payloads / _xss_variants (with a marker assertion "
        "proving the value reached the renderer)."
        % [(k, sorted(read_keys[k])) for k in unpoisoned])


# ── fail-soft: unavailable is a rendering, not an error ─────────────────────

@pytest.mark.parametrize("tab", sorted(t[0] for t in _TAB_CASES))
def test_a_missing_upstream_renders_unavailable_not_deployed_yet(app, monkeypatch, tab):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub({}))          # nothing registered
    monkeypatch.setattr(m, "_store_entries", lambda: {})
    r = app.test_client().get(m.API_PREFIX + "/tab/" + tab + "?q=x", headers=_H)
    assert r.status_code == 200
    assert "unavailable (not deployed yet)" in r.get_data(as_text=True)


@pytest.mark.parametrize("tab", sorted(t[0] for t in _TAB_CASES))
def test_an_upstream_exception_never_5xxs_a_tab(app, monkeypatch, tab):
    m = _mod()

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_forward", boom)
    r = app.test_client().get(m.API_PREFIX + "/tab/" + tab + "?q=x", headers=_H)
    assert r.status_code == 200
    assert "unreadable" in r.get_data(as_text=True)


def test_unknown_tab_is_404(app):
    m = _mod()
    assert app.test_client().get(m.API_PREFIX + "/tab/nope", headers=_H).status_code == 404


def test_metrics_is_json_with_nulls_for_unreadable_sources(app, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub({}))
    r = app.test_client().get(m.API_PREFIX + "/metrics", headers=_H)
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["week"] is None and d["deadman_overdue"] is None
    assert d["shell"]["available"] is False
    assert all(v["unavailable"] for v in d["sources"].values())


def test_metrics_keeps_a_measured_zero_as_zero(app, monkeypatch):
    m = _mod()
    R = {k: v[0] for k, v in m.READS.items()}
    monkeypatch.setattr(m, "_forward", _stub({
        R["ops_claims"]: {"ok": True, "week": {"shipped": 0, "confirmed": 0,
                                                 "granted_action_classes": 0}},
        R["deadman"]: {"ok": True, "overdue_count": 0, "tracked": 71},
        R["convergence"]: {"ok": True, "recurrence_rate": 0.0, "closed": 4,
                           "recurred": 0, "window_days": 30},
    }))
    d = app.test_client().get(m.API_PREFIX + "/metrics", headers=_H).get_json()
    assert d["week"]["shipped"] == 0 and d["granted_classes"] == 0
    assert d["deadman_overdue"] == 0 and d["recurrence_30d"]["rate"] == 0.0
    assert m._n(0) == "0" and m._n(None) == "—"


# ── honesty: an unread thing is never rendered as a measured one ────────────

def test_a_failed_classes_read_never_claims_part_b_is_not_deployed(app, monkeypatch):
    """A 500/401/DB-down on the classes GET says NOTHING about whether part B
    is on main. The first cut collapsed 'could not read' and 'read fine, no
    graduation key' into one `grad is None` branch, so any read failure printed
    'part B is not on main yet' directly under an 'action classes: unreadable'
    line that says the opposite — the page contradicting itself about a
    deployment state it never observed."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _err_stub("db down", status=500))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes",
                                 headers=_H).get_data(as_text=True)
    assert "action classes: unreadable" in body, \
        "the read-failure branch was never entered — the checks below are vacuous"
    assert "not on main yet" not in body, \
        "the page claimed a deployment state it never observed"
    assert "not deployed yet" not in body
    assert "graduation report: not read" in body


def test_an_unregistered_classes_path_also_never_claims_part_b_is_not_deployed(app, monkeypatch):
    """Same for the not-registered shape: the classes GET is part of THIS page's
    own contract, so its absence is a broken page, not evidence about B."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub({}))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes",
                                 headers=_H).get_data(as_text=True)
    assert "action classes: unavailable (not deployed yet)" in body
    assert "not on main yet" not in body
    assert "graduation report: not read" in body


def test_a_readable_classes_response_without_graduation_says_not_deployed_yet(app, monkeypatch):
    """The one shape that DOES license the claim: the GET answered, and its
    payload carries no `graduation` key."""
    m = _mod()
    payloads = _xss_payloads(m)
    payloads[m.READS["classes"][0]].pop("graduation")
    monkeypatch.setattr(m, "_forward", _stub(payloads))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes",
                                 headers=_H).get_data(as_text=True)
    assert "not on main yet" in body
    assert "graduation report: not read" not in body


def test_a_null_graduation_value_is_reported_as_null_not_as_undeployed(app, monkeypatch):
    m = _mod()
    payloads = _xss_payloads(m)
    payloads[m.READS["classes"][0]]["graduation"] = None
    monkeypatch.setattr(m, "_forward", _stub(payloads))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes",
                                 headers=_H).get_data(as_text=True)
    assert "with a null value" in body
    assert "not on main yet" not in body


def _inbox_rows_stub(m, rows):
    R = {k: v[0] for k, v in m.READS.items()}
    return _stub({R["inbox"]: {"ok": True, "counts": {"awaiting_decision": len(rows)},
                               "rows": rows}})


def _row(rid, requested_at, cls="cls_a"):
    return {"id": rid, "status": "awaiting_decision", "action_class": cls,
            "requested_at": requested_at, "finished_at": None,
            "title": "t%s" % rid, "finding_key": "k%s" % rid,
            "reason": None, "confidence": None, "analysis": None,
            "decision": None, "action_url": None, "action_method": None,
            "seen_count": 1, "last_seen": None}


def test_an_unreadable_inbox_timestamp_renders_oldest_unknown_not_zero(app, monkeypatch):
    """`max((_age_hours(...) or 0) for r in g)` folded 'could not parse this
    timestamp' into 0 — the FRESHEST value possible — on an OLDEST-item metric.
    A group whose timestamps all fail to parse read 'oldest 0.0h', which is the
    same lie as a not-measured rendered as a measured zero."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _inbox_rows_stub(m, [_row(1, "not-a-timestamp")]))
    body = app.test_client().get(m.API_PREFIX + "/tab/inbox",
                                 headers=_H).get_data(as_text=True)
    assert "cls_a" in body, "the group never rendered — the checks below are vacuous"
    assert "oldest 0.0h" not in body, "an unparseable timestamp rendered as the freshest value"
    assert "oldest unknown" in body
    assert "1 of 1 timestamp(s) unreadable" in body


def test_a_partly_unreadable_group_reports_the_oldest_it_could_read_and_says_so(app, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _inbox_rows_stub(
        m, [_row(1, "not-a-timestamp"), _row(2, "2026-01-01T00:00:00+00:00")]))
    body = app.test_client().get(m.API_PREFIX + "/tab/inbox",
                                 headers=_H).get_data(as_text=True)
    assert re.search(r"oldest \d+\.\dh", body), "the readable timestamp was not used"
    assert "oldest 0.0h" not in body
    assert "1 of 2 timestamp(s) unreadable" in body


def test_a_fully_readable_group_says_nothing_about_unreadable_timestamps(app, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _inbox_rows_stub(
        m, [_row(1, "2026-01-01T00:00:00+00:00")]))
    body = app.test_client().get(m.API_PREFIX + "/tab/inbox",
                                 headers=_H).get_data(as_text=True)
    assert re.search(r"oldest \d+\.\dh", body)
    assert "unreadable" not in body and "oldest unknown" not in body


# ── actions: allow-list only, drain forced dry-run ──────────────────────────

def test_drain_action_is_forced_to_dry_run_whatever_the_caller_sent(app, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "_forward", _stub({"/api/v1/brain/squasher/drain": {"ok": True, "dry_run": True}}, calls))
    r = app.test_client().post(m.API_PREFIX + "/act/class_drain_dry?dry_run=0&limit=5",
                               json={"dry_run": False, "limit": 5}, headers=_H)
    assert r.status_code == 200 and r.get_json()["ok"]
    assert calls == [("POST", "/api/v1/brain/squasher/drain", {"dry_run": "1"}, None)]


def test_no_action_in_the_table_can_reach_a_live_drain():
    m = _mod()
    drains = [(k, s) for k, s in m.ACTIONS.items() if s["path"].endswith("/drain")]
    assert drains, "no drain action in the table — the check below is vacuous"
    for k, s in drains:
        assert s["query"].get("dry_run") == "1", k
    assert all(s["method"] == "POST" for s in m.ACTIONS.values())


def test_unknown_action_is_404_and_never_forwarded(app, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "_forward", _stub({}, calls))
    r = app.test_client().post(m.API_PREFIX + "/act/drop_tables", json={}, headers=_H)
    assert r.status_code == 404 and calls == []


def test_retract_without_a_reason_is_refused_before_forwarding(app, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "_forward", _stub({}, calls))
    c = app.test_client()
    for body in ({"id": 5}, {"id": 5, "reason": "   "}, {"reason": "x"}):
        r = c.post(m.API_PREFIX + "/act/claims_retract", json=body, headers=_H)
        assert r.status_code == 400, body
    assert calls == []


def test_body_is_filtered_to_the_allow_list_and_stamped(app, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "_forward", _stub({
        "/api/v1/brain/claims/retract": {"ok": True, "id": 5},
        "/api/v1/brain/squasher/grant": {"ok": True}}, calls))
    c = app.test_client()
    r = c.post(m.API_PREFIX + "/act/claims_retract",
               json={"id": 5, "reason": " wrong basis ", "evil": "x", "superseded_by": 9},
               headers=_H)
    assert r.status_code == 200
    r = c.post(m.API_PREFIX + "/act/class_grant",
               json={"class": "facility_dedup_apply", "granted": "true", "by": "spoof"},
               headers=_H)
    assert r.status_code == 200
    assert calls[0] == ("POST", "/api/v1/brain/claims/retract", {},
                        {"id": 5, "reason": "wrong basis", "superseded_by": 9})
    assert calls[1] == ("POST", "/api/v1/brain/squasher/grant", {},
                        {"class": "facility_dedup_apply", "granted": True,
                         "by": "inspect-dashboard"})


def test_an_expected_later_action_reports_unavailable_not_an_error(app, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub({}))
    r = app.test_client().post(m.API_PREFIX + "/act/resolve_class",
                               json={"class": "c", "decision": "granted-class handles it"},
                               headers=_H)
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False and d["unavailable"] is True and d["status"] == 404


def test_learn_station_status_comes_from_the_recall_payload_not_a_second_route(app, monkeypatch):
    """Part C serves learn_station_status() INSIDE GET /api/v1/brain/learn/recall
    (routes/brain_rag.learn_recall -> jsonify(..., status=learn_station_status())).
    It serves no separate status route, so a READS entry naming one could only
    ever render "unavailable (not deployed yet)" -- forever, including after C
    lands. An unavailable that can never become available is a lie, and no
    EXPECTED_LATER check catches it (nothing ever registers the path). Pin it
    here: exactly ONE learn target, and the tab reads the status off recall."""
    m = _mod()
    learn = sorted(path for path, _ in m.READS.values() if "/learn/" in path)
    assert learn == ["/api/v1/brain/learn/recall"], (
        "the learn station exposes exactly one route; these are pinned as reads "
        "and any extra one renders 'unavailable' forever: %s" % learn)
    assert not [pth for pth in m.EXPECTED_LATER
                if "/learn/" in pth and pth != "/api/v1/brain/learn/recall"]

    calls = []
    monkeypatch.setattr(m, "_forward", _stub(
        {"/api/v1/brain/learn/recall": {"ok": True, "lessons": ["l1"],
                                        "status": {"corpus": "lesson-corpora-here"}}},
        calls))
    body = app.test_client().get(
        m.API_PREFIX + "/tab/lessons?q=deals", headers=_H).get_data(as_text=True)
    # the status block reached the page, in the status section itself (the
    # other reads in this tab are deliberately unstubbed and DO say unavailable)
    head = "<h3>learn station status (part C)</h3>"
    assert head in body
    section = body.split(head, 1)[1].split("<h3>", 1)[0]
    assert "lesson-corpora-here" in section, "the status block never rendered"
    assert "unavailable (not deployed yet)" not in section, section[:300]
    # ... off the ONE recall call, with no second forward to a learn path
    learn_calls = [c for c in calls if "/learn/" in c[1]]
    assert len(learn_calls) == 1 and learn_calls[0][1] == "/api/v1/brain/learn/recall", learn_calls


def test_lessons_tab_says_status_ships_with_recall_when_no_query(app, monkeypatch):
    """No query = no recall call = no status, and the page says so plainly
    rather than claiming a route is undeployed."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "_forward", _stub({}, calls))
    body = app.test_client().get(m.API_PREFIX + "/tab/lessons", headers=_H).get_data(as_text=True)
    assert "ships inside the recall response" in body
    assert not [c for c in calls if "/learn/" in c[1]], "recall was called without a query"


def test_absent_product_detector_is_not_reported_as_a_measured_zero(app, monkeypatch):
    """brain_findings.by_detector is keyed by the `detector` MODULE column
    (consistency_radar, autonomy_runtime, …). The three product detectors are
    `issue` values on the radar's OWN rows, so they are never keys there —
    measured live 2026-08-22: 10 module keys, none of the three. Rendering
    _n(by.get(name)) would print '—' or, worse, a '0' the endpoint never
    measured. The tab must say what the column cannot answer instead."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub({
        "/api/v1/brain/findings/db-status": {
            "ok": True, "total_rows": 3964, "live_columns": ["detector", "issue"],
            # exactly the live keying: modules, not issues
            "by_detector": {m._RADAR_MODULE: 3599, "autonomy_runtime": 182},
            "recent": [{"issue": "detector_runtime_slow", "count": 28,
                        "seen_count": 1051, "detail": "d"}]},
        "/api/v1/ops/claims": {"ok": True, "week": {}}}))
    body = app.test_client().get(m.API_PREFIX + "/tab/detectors", headers=_H).get_data(as_text=True)
    assert "3599" in body, "the radar bucket never rendered — the checks below are vacuous"
    for name in m._PRODUCT_DETECTORS:
        assert name in body, name
    # the by_detector cell for each is the explanation, never a number or a dash
    assert body.count("by_detector keys are modules, not issues") == len(m._PRODUCT_DETECTORS)


def test_no_read_can_reach_a_filing_or_actuating_endpoint(app, monkeypatch):
    """READS are rendered on every tab view, so a READ path must be a pure GET.
    Part B (#3073) registers POST /api/v1/brain/squasher/graduation =
    graduation_report(file=True), which FILES up to 3 proposal rows, plus
    POST .../actuate/<cls> and POST .../rollback-run. An earlier cut had
    /squasher/graduation in READS and called it with GET on every render of the
    Classes tab -- a 405 after B lands, and one GET->POST typo away from an
    inspection page that writes rows just by being looked at. B puts the read
    where a read belongs: out["graduation"] on the classes GET (file=False,
    "a GET never files"). Pin that no READ names a mutating path."""
    m = _mod()
    read_paths = {p for p, _ in m.READS.values()}
    for mutating in ("/api/v1/brain/squasher/graduation",
                     "/api/v1/brain/squasher/actuate",
                     "/api/v1/brain/squasher/rollback-run",
                     "/api/v1/brain/squasher/grant",
                     "/api/v1/brain/squasher/drain",
                     "/api/v1/brain/squasher/resolve",
                     "/api/v1/brain/claims/verify",
                     "/api/v1/brain/claims/retract"):
        hit = sorted(p for p in read_paths if p.startswith(mutating))
        assert not hit, ("READS names a mutating endpoint %s -- reads render on "
                         "every tab view: %s" % (mutating, hit))
    # and the graduation report still reaches the page, off the classes GET
    calls = []
    monkeypatch.setattr(m, "_forward", _stub(
        {"/api/v1/brain/squasher/classes": {
            "ok": True, "known": True, "classes": [],
            "graduation": {"proposals": ["grad-rode-on-classes"]}}}, calls))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes", headers=_H).get_data(as_text=True)
    assert "grad-rode-on-classes" in body, "the graduation block never rendered"
    assert not [c for c in calls if "graduation" in c[1]], calls


def test_graduation_reads_unavailable_when_part_b_is_absent(app, monkeypatch):
    """No `graduation` key on the classes payload = part B is not on main.
    Say "unavailable (not deployed yet)", never an error, never a blank."""
    m = _mod()
    monkeypatch.setattr(m, "_forward", _stub(
        {"/api/v1/brain/squasher/classes": {"ok": True, "known": True, "classes": []}}))
    body = app.test_client().get(m.API_PREFIX + "/tab/classes", headers=_H).get_data(as_text=True)
    head = "<h3>graduation report (part B)</h3>"
    assert head in body
    section = body.split(head, 1)[1]
    assert "unavailable (not deployed yet)" in section, section[:300]


def test_the_platform_approval_quote_is_verbatim_from_the_module_it_cites(app, monkeypatch):
    """The Platform tab tells the owner there is no approve endpoint and cites
    routes/platform_updates.py for it. A dashboard whose job is to be believed
    must not paraphrase its own source: the first cut rendered
    'routes/platform_updates.py: "merging that PR IS the approval"', a
    lowercased rewrite of a docstring line rather than a string that exists.
    AST-parse that module and require the quote to be one of its literals."""
    m = _mod()
    src = os.path.join(_ROOT, "routes", "platform_updates.py")
    consts = {n.value for n in ast.walk(ast.parse(open(src, encoding="utf-8").read()))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert m._PLATFORM_APPROVAL_QUOTE in consts, (
        "the quote the page attributes to routes/platform_updates.py is not a "
        "string in that file: %r" % (m._PLATFORM_APPROVAL_QUOTE,))
    # and it actually reaches the tab
    monkeypatch.setattr(m, "_forward", _stub({
        "/api/v1/whats-new": {"ok": True, "platform": [], "platform_pending": 0,
                              "platform_withheld": [], "platform_as_of": None}}))
    monkeypatch.setattr(m, "_store_entries", lambda: {})
    body = app.test_client().get(m.API_PREFIX + "/tab/platform", headers=_H).get_data(as_text=True)
    assert m._PLATFORM_APPROVAL_QUOTE in body, "the quote never reached the page"


def test_loopback_carries_the_servers_keys_and_a_dchub_ua(monkeypatch):
    m = _mod()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _KEY)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "int-" + _KEY)
    h = m._self_auth_headers()
    assert h["X-Admin-Key"] == _KEY and h["X-Internal-Key"] == "int-" + _KEY
    assert h["User-Agent"].startswith("dchub-")


def test_forward_reads_a_real_route_in_process_and_maps_404s(app):
    """The loopback against a bare app: a registered route is read with the
    server's headers; an unregistered one is `unavailable`; a registered
    one that answers 404 itself is NOT (its error is reported).

    ★ The app gets main.py's JSON 404 handler on purpose. The first cut
    decided 'unavailable' from the 404 body shape; on the booted app every
    unknown path answers {"error": "404 Not Found"}, so all six A/B/C
    targets rendered 'unreadable' instead of 'not deployed yet' (smoke,
    2026-08-22). Availability is read from url_map now; this pins it."""
    from flask import jsonify, request as _rq
    m = _mod()

    @app.errorhandler(404)
    def _json_404(_e):
        return jsonify(error="404 Not Found"), 404

    @app.route("/__probe")
    def _probe():
        return jsonify(ok=True, ua=_rq.headers.get("User-Agent"),
                       key=_rq.headers.get("X-Admin-Key"), q=_rq.args.get("days"))

    @app.route("/__gone")
    def _gone():
        return jsonify(ok=False, error="no such claim"), 404

    with app.test_request_context("/"):
        got = m._forward("GET", "/__probe", {"days": "7"})
        assert got["ok"] and got["data"]["key"] == _KEY and got["data"]["q"] == "7"
        assert got["data"]["ua"].startswith("dchub-")
        miss = m._forward("GET", "/__nope")
        assert miss["unavailable"] and miss["status"] == 404 and not miss["ok"]
        own = m._forward("GET", "/__gone")
        assert not own["unavailable"] and own["status"] == 404
        assert own["error"] == "no such claim"


# ── the page: no external assets, one trusted insertion point ───────────────

def test_page_loads_no_external_script_or_style():
    page = _mod()._PAGE
    assert "<script src" not in page
    assert not re.search(r"<link[^>]*href\s*=\s*['\"]?https?:", page)
    assert not re.search(r"https?://", page), "something external is referenced"
    assert page.count("innerHTML") == 1, "markup is inserted in more than one place"
    for banned in ("insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in page, banned


def test_page_sets_numbers_via_textContent_only():
    page = _mod()._PAGE
    assert "textContent" in page
    strip = page[page.index('class="il-strip"'):page.index('id="il-mstat"')]
    assert strip.count("data-m=") >= 8
    assert strip.count(">—<") == strip.count("data-m="), "every stat starts as '—' (not measured), never blank or 0"


# ── every fetch URL and every target resolves — STATICALLY, no app boot ─────
#
# ★ WHY THIS SECTION EXISTS AND WHY IT DUPLICATES THE BOOTED CHECK BELOW.
#
# The brief names one guard verbatim: "every fetch URL in the page resolves to
# a registered route". It shipped as the two `_booted_rules` tests below — and
# it NEVER RAN. `unit-tests` (.github/workflows/pre-merge.yml) is the only job
# that runs pytest, and it installs a deliberately light dep set, so importing
# main.py there raises ModuleNotFoundError and `_booted_rules` pytest.skips
# unless DCHUB_CONTRACT_GATE_STRICT=1 — which that job does not set. Evidence,
# not inference: CI run 32604169976 / job 97106689232 logged
#   test_every_fetch_url_in_the_page_resolves_on_the_booted_app SKIPPED
#   test_every_forward_target_is_registered_or_pinned_expected_later SKIPPED
# and the audit reproduced the consequence: with a one-character typo in a
# READS path ("/api/v1/brain/squasher/inbox" -> ".../inboxx") the file was
# 48 passed. A typo in a fetch target would have shipped as a permanent
# "unavailable (not deployed yet)" — the exact lie this page exists to end.
#
# The checks below need NO app boot, so they run in the job that runs pytest.
# They read what the repo DECLARES rather than what the app REGISTERS, which is
# strictly weaker (a module that fails to import still declares its routes) —
# so the booted pair stays, and .github/workflows/app-contract-gate.yml now
# runs this file under the STRICT=1 + requirements.txt environment where it
# cannot skip. Two jobs, two failure modes, no silent green in either.

_ROUTE_VERBS = {"route", "get", "post", "put", "patch", "delete", "add_url_rule"}
_DECLARED_CACHE: dict = {}


def _declared_route_literals() -> set:
    """Every URL rule literal this repo declares, by AST over routes/ + main.py."""
    if "rules" in _DECLARED_CACHE:
        return _DECLARED_CACHE["rules"]
    files = sorted(glob.glob(os.path.join(_ROOT, "routes", "*.py")))
    # PINNED NON-ZERO FLOOR. A scan that finds nothing reports green, and that
    # result is byte-identical to "scanned everything, all clean"
    # (tests/_scan_floors.py). ~791 route modules today.
    assert len(files) >= 600, (
        "route-module scan collapsed to %d files — repoint the glob; a guard "
        "that scans nothing cannot fail" % len(files))
    out = set()
    for path in files + [_MAIN]:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in _ROUTE_VERBS or not n.args:
                continue
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
                    and a0.value.startswith("/"):
                out.add(a0.value)
    assert len(out) >= 2000, (
        "declared-rule inventory collapsed to %d literals — the extraction "
        "broke (~2,625 today)" % len(out))
    _DECLARED_CACHE["rules"] = out
    return out


def _const_path(m, node):
    """Resolve a decorator's path argument through this module's constants:
    PAGE_PATH, API_PREFIX, API_PREFIX + '/tab/<name>'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        v = getattr(m, node.id, None)
        return v if isinstance(v, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_path(m, node.left), _const_path(m, node.right)
        return left + right if isinstance(left, str) and isinstance(right, str) else None
    return None


def _own_declared_rules(m) -> set:
    """The rules THIS blueprint declares, read from its own decorators."""
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for dec in fn.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in _ROUTE_VERBS or not dec.args:
                continue
            got = _const_path(m, dec.args[0])
            if got:
                out.add(got)
    assert len(out) >= 5, (
        "this blueprint declares %d resolvable rules — expected the page, the "
        "index, /metrics, /tab/<name> and /act/<name>" % len(out))
    return out


def test_every_forward_target_is_declared_in_repo_source_or_pinned_expected_later():
    """The booted twin of this runs only in app-contract-gate; this one runs in
    unit-tests. A typo in any READS/ACTIONS path is red HERE."""
    m = _mod()
    declared = _declared_route_literals()
    targets = {p for p, _ in m.READS.values()} | {s["path"] for s in m.ACTIONS.values()}
    assert len(targets) >= 14, "target extraction collapsed to %d" % len(targets)
    missing = sorted(p for p in targets
                     if p not in declared and p not in m.EXPECTED_LATER)
    assert not missing, (
        "forward targets no module in this repo declares, and which are not "
        "pinned as expected-later: %s\nA target the app does not serve renders "
        "as a permanent 'unavailable (not deployed yet)'." % missing)
    landed = sorted(p for p in m.EXPECTED_LATER if p in declared)
    assert not landed, (
        "these EXPECTED_LATER routes are now declared in this repo — the part "
        "that owns them has landed; move them out of EXPECTED_LATER in "
        "routes/agentic_loop_inspect.py: %s" % landed)


def test_every_fetch_url_in_the_page_is_declared_by_this_blueprint():
    """Every fetch() the page makes must hit a rule this module declares."""
    m = _mod()
    own = _own_declared_rules(m)
    page = m._PAGE
    api = re.search(r"var API = '([^']+)'", page).group(1)
    assert api == m.API_PREFIX
    assert m.PAGE_PATH in own and api in own
    suffixes = re.findall(r"fetch\(API \+ '([^']+)'", page)
    assert suffixes, "no fetch(API + '...') calls found — extraction empty"
    assert len(re.findall(r"fetch\(", page)) == len(suffixes), \
        "a fetch() that does not go through API exists in the page"
    for suffix in suffixes:
        if suffix.endswith("/"):                # '/tab/' and '/act/' + <name>
            assert any(r.startswith(api + suffix) and r.endswith("<name>")
                       for r in own), suffix
        else:
            assert api + suffix in own, suffix
    assert set(re.findall(r'data-tab="([a-z]+)"', page)) == set(m.TABS)
    acts = set(re.findall(r'_btn\("([a-z_]+)"', open(_SRC, encoding="utf-8").read()))
    assert acts and acts <= set(m.ACTIONS), acts - set(m.ACTIONS)


# ── every fetch URL and every target resolves on the BOOTED app ─────────────

def _booted_rules(tmp_path) -> set:
    if "rules" in _BOOT_CACHE:
        return _BOOT_CACHE["rules"]
    env = dict(os.environ)
    env.setdefault("JWT_SECRET", "contract-gate-placeholder-not-a-secret")
    env.setdefault("DCHUB_ADMIN_KEY", "contract-gate-placeholder")
    env["DCHUB_AMBASSADOR_STATE_FILE"] = str(tmp_path / "ambassador_state.json")
    code = textwrap.dedent("""
        import json, os, sys
        sys.path.insert(0, %r); sys.path.insert(0, os.path.join(%r, "scripts"))
        import app_contract_gate as g
        app, _ = g.boot()
        rules = sorted({str(r.rule) for r in app.url_map.iter_rules()})
        sys.stdout.write("RULES=" + json.dumps(rules) + "\\n"); sys.stdout.flush()
        os._exit(0)
    """ % (_ROOT, _ROOT))
    proc = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RULES=")]
    if not lines and "ModuleNotFoundError" in out and not _STRICT:
        pytest.skip("full runtime deps absent here; the app-contract-gate "
                    "workflow runs this with requirements.txt installed")
    assert lines, "the app did not boot:\n" + out[-4000:]
    rules = set(json.loads(lines[-1][len("RULES="):]))
    assert len(rules) > 3000, "url_map collapsed to %d rules" % len(rules)
    _BOOT_CACHE["rules"] = rules
    return rules


def test_every_fetch_url_in_the_page_resolves_on_the_booted_app(tmp_path):
    m = _mod()
    rules = _booted_rules(tmp_path)
    page = m._PAGE
    api = re.search(r"var API = '([^']+)'", page).group(1)
    assert api == m.API_PREFIX and api in rules and m.PAGE_PATH in rules
    suffixes = re.findall(r"fetch\(API \+ '([^']+)'", page)
    assert suffixes, "no fetch(API + '...') calls found — extraction empty"
    assert len(re.findall(r"fetch\(", page)) == len(suffixes), \
        "a fetch() that does not go through API exists in the page"
    for s in suffixes:
        if s.endswith("/"):                    # '/tab/' and '/act/' + <name>
            assert any(r.startswith(api + s) and r.endswith("<name>") for r in rules), s
        else:
            assert api + s in rules, s
    # tab names in the page are renderers; action names in renderers are actions
    assert set(re.findall(r'data-tab="([a-z]+)"', page)) == set(m.TABS)
    src = open(_SRC, encoding="utf-8").read()
    acts = set(re.findall(r'_btn\("([a-z_]+)"', src))
    assert acts and acts <= set(m.ACTIONS), acts - set(m.ACTIONS)


def test_every_forward_target_is_registered_or_pinned_expected_later(tmp_path):
    m = _mod()
    rules = _booted_rules(tmp_path)
    targets = {p for p, _ in m.READS.values()} | {s["path"] for s in m.ACTIONS.values()}
    missing = sorted(p for p in targets if p not in rules and p not in m.EXPECTED_LATER)
    assert not missing, (
        "forward targets that the booted app does not serve (and are not "
        "pinned as expected-later): %s" % missing)
    landed = sorted(p for p in m.EXPECTED_LATER if p in rules)
    assert not landed, (
        "these EXPECTED_LATER routes are now registered — the part that owns "
        "them has landed; move them out of EXPECTED_LATER in "
        "routes/agentic_loop_inspect.py: %s" % landed)


# ── main.py wiring: its own try/except ──────────────────────────────────────

def test_main_registers_the_blueprint_in_its_own_try_except():
    tree = ast.parse(open(_MAIN, encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for s in node.body:
            if (isinstance(s, ast.ImportFrom) and s.module == "routes.agentic_loop_inspect"
                    and any(a.name == "agentic_loop_inspect_bp" for a in s.names)):
                hits.append(node)
    assert len(hits) == 1, "expected exactly one registration block, found %d" % len(hits)
    t = hits[0]
    other_imports = [s for s in t.body if isinstance(s, (ast.Import, ast.ImportFrom))
                     and getattr(s, "module", None) != "routes.agentic_loop_inspect"]
    assert not other_imports, "the try block imports other modules — a sibling failure would skip this registration"
    regs = [n for n in ast.walk(t) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "register_blueprint"]
    assert len(regs) == 1 and getattr(regs[0].args[0], "id", None) == "agentic_loop_inspect_bp"
    assert t.handlers and all(h.type is not None for h in t.handlers)
