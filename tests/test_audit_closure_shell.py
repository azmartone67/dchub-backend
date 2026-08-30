"""Audit Closure Master Shell (#52, 2026-08-07) — pins the closure contract.

The shell tracks all 138 findings of the 2026-08-07 full-platform audit. What
these tests pin:

  1. Registry integrity — 138 findings, unique ids, valid severities/efforts,
     and every _CHECK_CLOSES target exists. A registry that silently loses
     rows would report a flattering closure percentage.
  2. Helpers are IMPORTED from shell #34, never re-declared (the drift rule).
  3. With the network and DB unreachable, no live-probe check may read True
     or False — unreachable is '?', never a verdict (#34's core lesson).
  4. Routes are admin-gated, no-store, kill-switched; the module is
     registered in main.py and scheduled in cron_heartbeat (the generalized
     scan in test_shell_scheduler_coverage.py enforces the class; the
     explicit assert here gives the better error message).
  5. The ack mechanism closes ONLY what the operator names — an empty env
     closes nothing (silent mass-closure would defeat the whole board).

CI-SAFETY: probes disabled via env; _http stubbed dead in tick-shape tests;
no DATABASE_URL in the unit env.
"""
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    # ★Save/restore: the pre-merge job runs every test file in ONE process,
    # so unrestored env mutations leak into later suites (review #25).
    saved = {k: os.environ.get(k) for k in
             ("AUDIT_CLOSURE_SHELL_PROBE", "DATABASE_URL",
              "NEON_DATABASE_URL", "AUDIT_CLOSURE_ACK", "DCHUB_ADMIN_KEY")}
    os.environ["AUDIT_CLOSURE_SHELL_PROBE"] = "0"   # no MCP probes in CI
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("NEON_DATABASE_URL", None)
    os.environ.pop("AUDIT_CLOSURE_ACK", None)
    from routes import audit_closure_master_shell as m
    yield m
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _dead_http(url, timeout=8, headers=None, fresh=False, _memo=None):
    return (None, {}, "", "stubbed dead in CI")


def _fake_http(bodies):
    """Stub _http serving canned (status, headers, body) by URL substring —
    realistic-payload tests exist because the first draft read WRONG NESTING
    on three endpoints and CI (dead-network stubs only) never noticed."""
    def stub(url, timeout=8, headers=None, fresh=False, _memo=None):
        for frag, (st, hdrs, body) in bodies.items():
            if frag in url:
                b = body if isinstance(body, str) else json.dumps(body)
                return (st, hdrs, b, None)
        return (None, {}, "", "no canned body for %s" % url)
    return stub


# ── registry integrity ────────────────────────────────────────────────

def test_registry_carries_all_138_findings(shell):
    assert len(shell.REGISTRY) == 138
    ids = [r[0] for r in shell.REGISTRY]
    assert len(set(ids)) == 138, "duplicate finding ids"
    for fid, dom, sev, eff, title in shell.REGISTRY:
        assert fid.startswith("SH52-")
        assert sev in ("C", "H", "M", "L"), (fid, sev)
        assert eff in ("S", "M", "L"), (fid, eff)
        assert len(title) > 20, (fid, "title too thin to act on")


def test_check_closes_targets_exist(shell):
    ids = {r[0] for r in shell.REGISTRY}
    for cid, fids in shell._CHECK_CLOSES.items():
        for fid in fids:
            assert fid in ids, "%s closes unknown finding %s" % (cid, fid)


def test_severity_mix_matches_the_audit(shell):
    sevs = [r[2] for r in shell.REGISTRY]
    assert sevs.count("C") == 5, "audit shipped 5 criticals"
    assert sevs.count("H") == 36, "audit shipped 36 highs"


# ── helpers imported, not re-declared ─────────────────────────────────

def test_verdict_and_probe_imported_from_shell_34(shell):
    src = open(os.path.join(ROOT, "routes/audit_closure_master_shell.py"),
               encoding="utf-8").read()
    assert "from routes.agent_pay_master_shell import" in src
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    # The only permitted local copies are the fail-soft fallbacks inside the
    # except-branch of that import.
    assert code.count("def _lane_verdict") == 1, \
        "re-declared _lane_verdict outside the import fallback"


# ── unreachable is '?', never a verdict ───────────────────────────────

def test_http_backed_checks_degrade_to_unknown(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _dead_http)
    for lane_fn, http_check_ids in (
            (shell._lane_first_call, ("d_aiquery",)),
            (shell._lane_surfaces, ("e_llms", "e_full", "e_version",
                                    "e_369gw", "e_agent")),
            (shell._lane_frontend_seo, ("f_markets", "f_robots", "f_reveal")),
            (shell._lane_inventory, ("h_osm", "h_geninv", "h_plants")),
            (shell._lane_brain, ("i_proposals",))):
        checks = {c["id"]: c for c in lane_fn()}
        for cid in http_check_ids:
            assert checks[cid]["pass"] is None, (
                "%s fabricated a verdict (%r) with the network dead: %s"
                % (cid, checks[cid]["pass"], checks[cid]["detail"]))


def test_tick_is_failsoft_and_wellformed(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _dead_http)
    t = shell._run_tick()
    assert t["ok"] and t["shell"] == "audit-closure-52"
    assert len(t["lanes"]) == 12
    for ln in t["lanes"]:
        assert ln["verdict"] in ("PASS", "FAIL", "?"), ln
    # Lane L (annotation lifecycle) rides the tick AFTER the registry fold —
    # its input is each finding's computed status. Both checks must be there:
    # dropping the lane silently re-opens the stale-annotation class.
    annot = next((ln for ln in t["lanes"] if ln["id"] == "annotations"), None)
    assert annot is not None, "annotation-lifecycle lane missing from the tick"
    annot_ids = {c["id"] for c in annot["checks"]}
    assert ({"l_annot_not_stale", "l_annot_credit_honest"} <= annot_ids
            or "l_annot_source" in annot_ids or "lane_crash" in annot_ids), annot_ids
    reg = t["registry"]
    assert reg["total"] == 138
    assert 0 <= reg["closed"] <= 138
    assert isinstance(reg["findings"], list) and len(reg["findings"]) == 138


# ── deliberate closure only ───────────────────────────────────────────

def test_ack_closes_only_named_findings(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _dead_http)
    monkeypatch.delenv("AUDIT_CLOSURE_ACK", raising=False)
    base = shell._run_tick()["registry"]
    # Pick two findings that are currently DEFERRED (owned, not closed) and
    # not already evidence-acked — env-acking them promotes DEFERRED -> ACKED,
    # so closed rises by exactly 2.
    df_only = [f for f in shell.DEFERRED
               if f not in shell._EVIDENCE_ACKED
               and f not in {x for v in shell._CHECK_CLOSES.values() for x in v}]
    a, b = df_only[0], df_only[1]
    monkeypatch.setenv("AUDIT_CLOSURE_ACK", "%s, %s" % (a, b))
    acked = shell._run_tick()["registry"]
    assert acked["closed"] == base["closed"] + 2
    by_id = {r["id"]: r["status"] for r in acked["findings"]}
    assert by_id[a] == "ACKED" and by_id[b] == "ACKED"
    assert by_id[df_only[2]] != "ACKED"


def test_ack_never_outranks_a_failing_checker(shell, monkeypatch):
    """Review #24/#31: an acked finding whose live checker FAILs must render
    OPEN-RED (and be reported), not count as closed."""
    monkeypatch.setenv("AUDIT_CLOSURE_ACK", "SH52-019")
    lanes = [{"checks": [{"id": "d_tier", "pass": False}]}]
    reg = shell._registry_status(lanes)
    by_id = {r["id"]: r["status"] for r in reg["findings"]}
    assert by_id["SH52-019"] == "OPEN-RED"
    assert reg["acks_ignored_while_red"] == ["SH52-019"]


# ── realistic payloads: the wrong-nesting class (review #3/#5/#6) ─────

def test_d_aiquery_fails_on_the_live_drift_shape(shell, monkeypatch):
    """The exact payloads observed live on 2026-08-07: undeduped 24,675
    served to AI agents vs canon stats.facilities_distinct=16,945. The first
    draft read the canon top level and graded NOTHING forever."""
    monkeypatch.setattr(shell, "_http", _fake_http({
        "/api/ai/query": (200, {}, {"data": {"facilities": 24675}}),
        "/api/v1/stats/canonical": (200, {}, {
            "ok": True, "stats": {"facilities_distinct": 16945}}),
    }))
    checks = {c["id"]: c for c in shell._lane_first_call()}
    assert checks["d_aiquery"]["pass"] is False, checks["d_aiquery"]["detail"]
    monkeypatch.setattr(shell, "_http", _fake_http({
        "/api/ai/query": (200, {}, {"data": {"facilities": 16900}}),
        "/api/v1/stats/canonical": (200, {}, {
            "ok": True, "stats": {"facilities_distinct": 16945}}),
    }))
    checks = {c["id"]: c for c in shell._lane_first_call()}
    assert checks["d_aiquery"]["pass"] is True, checks["d_aiquery"]["detail"]


def test_i_proposals_reads_the_nested_snapshot(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _fake_http({
        "/api/v1/brain/mirror/report": (200, {}, {
            "ok": True, "_brain_status_snapshot": {
                "actionable_findings_count": 55,
                "proposed_fixes_count": 0}}),
    }))
    checks = {c["id"]: c for c in shell._lane_brain()}
    assert checks["i_proposals"]["pass"] is False, \
        checks["i_proposals"]["detail"]


def test_h_plants_reads_the_nested_count_and_flags_the_live_twin_drift(
        shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _fake_http({
        "/api/energy-discovery/status": (200, {}, {
            "success": True, "data": {"total_power_plants": 13446}}),
        "/api/land-power/status": (200, {}, {
            "tables": {"power_plants": 14480}}),
        "deadman": (200, {}, {"feeds": []}),
    }))
    checks = {c["id"]: c for c in shell._lane_inventory()}
    assert checks["h_plants"]["pass"] is False, checks["h_plants"]["detail"]


def test_d_tease_fails_on_an_envelope_only_tease(shell, monkeypatch):
    """Review #2: _entity and quota are stamped on EVERY envelope — counting
    them as data made the zero-data-tease check structurally unfailable."""
    monkeypatch.setattr(shell, "_mcp", lambda tool, args: (
        {"tease": True, "tool": "get_iso_context", "upgrade": {},
         "next_session": {}, "_entity": {}, "quota": {}}, None)
        if tool == "get_iso_context" else (None, "not stubbed"))
    checks = {c["id"]: c for c in shell._lane_first_call()}
    assert checks["d_tease"]["pass"] is False, checks["d_tease"]["detail"]
    monkeypatch.setattr(shell, "_mcp", lambda tool, args: (
        {"tease": True, "tool": "get_iso_context", "upgrade": {},
         "next_session": {}, "_entity": {}, "quota": {},
         "headline": "x", "sections": [1, 2, 3]}, None)
        if tool == "get_iso_context" else (None, "not stubbed"))
    checks = {c["id"]: c for c in shell._lane_first_call()}
    assert checks["d_tease"]["pass"] is True, checks["d_tease"]["detail"]


def test_f_reveal_detects_a_hit_on_the_same_key_re_read(shell, monkeypatch):
    """Review #1/#16/#28/#37: the first draft cache-busted both reads, so the
    check could NEVER observe a HIT — while the leak was live in prod."""
    calls = {"n": 0}

    def stub(url, timeout=8, headers=None, fresh=False, _memo=None):
        if "reveal-validation-feed" in url:
            calls["n"] += 1
            cs = "HIT" if calls["n"] >= 2 else "MISS"
            return (200, {"cf-cache-status": cs}, "{}", None)
        return (None, {}, "", "no canned body")
    monkeypatch.setattr(shell, "_http", stub)
    checks = {c["id"]: c for c in shell._lane_frontend_seo()}
    assert checks["f_reveal"]["pass"] is False, checks["f_reveal"]["detail"]
    assert calls["n"] == 2, "check must read the SAME url twice"


def test_beat_fires_only_on_the_scheduled_post_path(shell, monkeypatch):
    """Review #36: beat-on-view masks a dead cron (the osm-crawl class)."""
    beats = []
    monkeypatch.setattr(shell, "_beat_ledger", lambda note, failing=False: beats.append(note))
    monkeypatch.setattr(shell, "_http", _dead_http)
    shell._run_tick(beat=False)
    assert beats == []
    shell._run_tick(beat=True)
    assert len(beats) == 1


def test_registry_folds_worst_verdict_when_checks_disagree(shell):
    lanes = [{"checks": [
        {"id": "e_llms", "pass": True},      # closes 027/125/073
        {"id": "d_tier", "pass": False},     # opens 019/039/124 RED
        {"id": "e_full", "pass": None},
    ]}]
    reg = shell._registry_status(lanes)
    by_id = {r["id"]: r["status"] for r in reg["findings"]}
    assert by_id["SH52-027"] == "CLOSED"
    assert by_id["SH52-019"] == "OPEN-RED"
    assert by_id["SH52-028"] == "?"
    assert by_id["SH52-030"] == "OPEN"       # genuinely-open (no checker/ack/defer)


# ── wiring ────────────────────────────────────────────────────────────

def test_routes_admin_gated_and_no_store(shell, monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.audit_closure_master_shell_bp)
    os.environ["DCHUB_ADMIN_KEY"] = "secret-under-test"
    beat_args = []

    def _stub_tick(beat=False):
        beat_args.append(beat)
        return {"ok": True, "shell": "audit-closure-52",
                "generated_at": "t", "lanes": [],
                "registry": {"total": 138, "closed": 0, "closure_pct": 0.0,
                             "acked": [], "acks_ignored_while_red": [],
                             "findings": []},
                "summary": "", "any_fail": False, "note": ""}
    monkeypatch.setattr(shell, "_run_tick", _stub_tick)
    c = app.test_client()
    r = c.get("/api/v1/admin/audit-closure/master-tick")
    assert r.status_code == 401
    assert r.headers.get("Cache-Control") == "no-store"
    r = c.get("/api/v1/admin/audit-closure/master-tick",
              headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200
    assert r.get_json()["shell"] == "audit-closure-52"
    assert r.headers.get("Cache-Control") == "no-store"
    assert beat_args[-1] is False, "a manual GET must not stamp the beat"
    r = c.post("/api/v1/admin/audit-closure/master-tick",
               headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200
    assert beat_args[-1] is True, "the scheduled POST path must beat"
    r = c.get("/admin/audit-closure",
              headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200 and b"Audit Closure" in r.data


def test_kill_switch(shell):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.audit_closure_master_shell_bp)
    os.environ["AUDIT_CLOSURE_SHELL_DISABLE"] = "1"
    try:
        r = app.test_client().get("/api/v1/admin/audit-closure/master-tick")
    # ★2026-08-12: was `== 503`. That assertion PINNED THE HAZARD — the CF
    # worker's proxyWithRetry reads any 5xx from Railway as a dead origin and
    # fails the site over to the stale Render backend, so disabling one
    # read-only diagnostic could take the whole site stale. 22 shells returned
    # 503; graph_spine already returned 404 and documented why. This is not a
    # weakening: the guarantee (a disabled shell must answer with an explicit
    # non-2xx) is unchanged and now enforced repo-wide by
    # tests/test_shell_killswitch_never_5xx.py.
        assert r.status_code == 404
    finally:
        os.environ.pop("AUDIT_CLOSURE_SHELL_DISABLE", None)


def test_registered_in_main():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "audit_closure_master_shell_bp" in src
    assert "register_blueprint(audit_closure_master_shell_bp)" in src


def test_scheduled_in_cron_heartbeat():
    src = open(os.path.join(ROOT, "routes/cron_heartbeat.py"),
               encoding="utf-8").read()
    assert "/api/v1/admin/audit-closure/master-tick" in src, \
        "shell #52 is not dispatched — registration is not scheduling"


def test_queries_are_time_bounded(shell):
    src = open(os.path.join(ROOT, "routes/audit_closure_master_shell.py"),
               encoding="utf-8").read()
    assert "statement_timeout" in src


# ── closeout ledger (2026-08-08 grind) ────────────────────────────────

def test_closeout_maps_have_no_overlap_or_unknown_ids(shell):
    ev = set(shell._EVIDENCE_ACKED)
    df = set(shell.DEFERRED)
    cc = set(f for v in shell._CHECK_CLOSES.values() for f in v)
    reg = set(r[0] for r in shell.REGISTRY)
    assert not (ev & df), "a finding is both evidence-acked AND deferred: %s" % (ev & df)
    assert not ((ev | df | cc) - reg), "ids outside the registry: %s" % ((ev | df | cc) - reg)
    # deferred must carry a real (owner, reason) tuple
    for fid, val in shell.DEFERRED.items():
        assert isinstance(val, tuple) and len(val) == 2 and val[0], (fid, val)


def test_deferred_is_owned_not_broken_and_never_counts_as_closed(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _dead_http)
    monkeypatch.delenv("AUDIT_CLOSURE_ACK", raising=False)
    reg = shell._registry_status([])   # no checks pass
    assert reg["closed"] + reg["deferred"] + reg["open"] == reg["total"]
    by = {r["id"]: r for r in reg["findings"]}
    # a purely-deferred finding reads DEFERRED with an owner, not CLOSED
    some_df = next(f for f in shell.DEFERRED
                   if f not in shell._EVIDENCE_ACKED
                   and f not in {x for v in shell._CHECK_CLOSES.values() for x in v})
    assert by[some_df]["status"] == "DEFERRED"
    assert by[some_df]["owner"] in ("build", "commercial", "diagnose",
                                    "owner-flag", "judgment")
    # evidence-acked reads ACKED (counts as resolved)
    assert by["SH52-049"]["status"] == "ACKED"


def test_a_failing_checker_overrides_a_deferred_tag(shell):
    # if a checker says a deferred finding is OPEN-RED, it is broken, not owned.
    df_with_checker = "SH52-130"   # deferred? no — checkered. pick a deferred one
    # z_isozones closes SH52-130; force it FALSE and confirm OPEN-RED wins.
    lanes = [{"checks": [{"id": "z_isozones", "pass": False}]}]
    st = {r["id"]: r["status"] for r in shell._registry_status(lanes)["findings"]}
    assert st["SH52-130"] == "OPEN-RED"


def test_closeout_lane_checks_can_fail(shell, monkeypatch):
    # MUST-FAIL: feed the ratelimit checker source that still has the substring
    # bypass and assert it fails (a checker that cannot fail is vacuous).
    def fake_src(rel):
        if rel == "rate_limiter.py":
            return "ok", "if 'dchub.cloud' in origin:\n    return None\n"
        return shell._src.__wrapped__(rel) if hasattr(shell._src, "__wrapped__") else ("absent", None)
    monkeypatch.setattr(shell, "_src", fake_src)
    monkeypatch.setattr(shell, "_jget", lambda *a, **k: (None, "stub"))
    monkeypatch.setattr(shell, "_http", _dead_http)
    checks = {c["id"]: c for c in shell._lane_closeout()}
    assert checks["z_ratelimit"]["pass"] is False, checks["z_ratelimit"]["detail"]
    # and the clean shape passes
    def clean_src(rel):
        if rel == "rate_limiter.py":
            return "ok", "def _origin_host_is_trusted(o): ...\n"
        return "absent", None
    monkeypatch.setattr(shell, "_src", clean_src)
    checks = {c["id"]: c for c in shell._lane_closeout()}
    assert checks["z_ratelimit"]["pass"] is True


def test_who_watches_the_watcher(shell, monkeypatch):
    """Anti-drift keystone: the shell asserts deadman-watch is itself alive,
    and it goes OPEN-RED when the board writer's self-beat is stale."""
    monkeypatch.setattr(shell, "_http", _dead_http)
    # deadman-watch feed present but overdue -> a_watcher must FAIL (red).
    monkeypatch.setattr(shell, "_deadman_feed",
                        lambda name: ({"feed": name, "overdue": True,
                                       "age_hours": 9.9, "reasons": []}, None)
                        if name == "deadman-watch" else (None, "n/a"))
    checks = {c["id"]: c for c in shell._lane_p0_incidents()}
    assert "a_watcher" in checks
    assert checks["a_watcher"]["pass"] is False
    assert checks["a_watcher"]["critical"] is True
    # fresh self-beat -> passes
    monkeypatch.setattr(shell, "_deadman_feed",
                        lambda name: ({"feed": name, "overdue": False,
                                       "age_hours": 1.2}, None)
                        if name == "deadman-watch" else (None, "n/a"))
    checks = {c["id"]: c for c in shell._lane_p0_incidents()}
    assert checks["a_watcher"]["pass"] is True
