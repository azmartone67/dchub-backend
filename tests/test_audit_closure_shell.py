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
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ["AUDIT_CLOSURE_SHELL_PROBE"] = "0"   # no MCP probes in CI
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("NEON_DATABASE_URL", None)
    os.environ.pop("AUDIT_CLOSURE_ACK", None)
    from routes import audit_closure_master_shell as m
    return m


def _dead_http(url, timeout=8, headers=None, _memo=None):
    return (None, {}, "", "stubbed dead in CI")


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
    assert len(t["lanes"]) == 10
    for ln in t["lanes"]:
        assert ln["verdict"] in ("PASS", "FAIL", "?"), ln
    reg = t["registry"]
    assert reg["total"] == 138
    assert 0 <= reg["closed"] <= 138
    assert isinstance(reg["findings"], list) and len(reg["findings"]) == 138


# ── deliberate closure only ───────────────────────────────────────────

def test_ack_closes_only_named_findings(shell, monkeypatch):
    monkeypatch.setattr(shell, "_http", _dead_http)
    monkeypatch.delenv("AUDIT_CLOSURE_ACK", raising=False)
    base = shell._run_tick()["registry"]
    monkeypatch.setenv("AUDIT_CLOSURE_ACK", "SH52-090, SH52-112")
    acked = shell._run_tick()["registry"]
    assert acked["closed"] == base["closed"] + 2
    by_id = {r["id"]: r["status"] for r in acked["findings"]}
    assert by_id["SH52-090"] == "ACKED" and by_id["SH52-112"] == "ACKED"
    assert by_id["SH52-091"] != "ACKED"


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
    assert by_id["SH52-004"] == "OPEN"       # unmapped finding stays OPEN


# ── wiring ────────────────────────────────────────────────────────────

def test_routes_admin_gated_and_no_store(shell, monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.audit_closure_master_shell_bp)
    os.environ["DCHUB_ADMIN_KEY"] = "secret-under-test"
    monkeypatch.setattr(shell, "_run_tick",
                        lambda: {"ok": True, "shell": "audit-closure-52",
                                 "generated_at": "t", "lanes": [],
                                 "registry": {"total": 138, "closed": 0,
                                              "closure_pct": 0.0, "acked": [],
                                              "findings": []},
                                 "summary": "", "any_fail": False,
                                 "note": ""})
    c = app.test_client()
    r = c.get("/api/v1/admin/audit-closure/master-tick")
    assert r.status_code == 401
    assert r.headers.get("Cache-Control") == "no-store"
    r = c.get("/api/v1/admin/audit-closure/master-tick",
              headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200
    assert r.get_json()["shell"] == "audit-closure-52"
    assert r.headers.get("Cache-Control") == "no-store"
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
        assert r.status_code == 503
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
