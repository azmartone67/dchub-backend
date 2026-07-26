"""Agent-Pay Master Shell (#34, 2026-07-25) — pins the contracts that failed.

Two defects motivated this shell and both are pinned here:

  1. The synthetic-traffic filter did not exclude `%dchub%`. The MCP gateway
     collapses every harness/QA clientInfo into the single platform tag
     `dchub-internal`, so our own probes scored as customer demand and the
     watcher reported a `first_real_challenge_at` milestone for two and a half
     weeks that was never real. The shell IMPORTS that predicate rather than
     re-declaring it, so a copy can never drift; these tests assert the import
     path and the tokens.

  2. A lane that could not check anything must never render green. The shared
     #30-33 `_lane_verdict` returns PASS when every unknown check is
     non-critical — which reads green for a lane whose probe was disabled. This
     shell's verdict is deliberately stricter and that is pinned.

Also pinned: the psycopg2 percent trap. The imported predicate carries doubled
`%%`, which psycopg2 only collapses when it performs substitution — so a query
concatenating it with `params=None` leaves literal `%%` in every LIKE and
silently matches nothing.

CI-SAFETY: no DATABASE_URL in the unit env; the module imports directly (never
via main); the live MCP probe is disabled so no test touches the network.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ["AGENT_PAY_SHELL_PROBE"] = "0"      # never hit the network in CI
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("NEON_DATABASE_URL", None)
    from routes import agent_pay_master_shell as m
    return m


# ── the filter that lied ──────────────────────────────────────────────

def test_synthetic_filter_is_imported_not_redeclared(shell):
    """A local copy would drift from funnel_health and silently re-open the
    bug this shell exists to catch."""
    src = open(os.path.join(ROOT, "routes/agent_pay_master_shell.py"),
               encoding="utf-8").read()
    assert "_SYNTH_PLATFORM_SQL = (" not in src, \
        "shell re-declares the predicate instead of importing it"
    assert "from routes.funnel_health import" in src


def test_filter_excludes_internal_traffic(shell):
    preds = shell._synth_predicates()
    assert preds is not None, "could not import funnel_health predicates"
    low = preds[1].lower()
    for token in ("dchub", "test", "probe", "verify", "harness"):
        assert token in low, \
            "synthetic filter lost %%%s%% — internal traffic would score " \
            "as REAL agent demand" % token


def test_lane5_pins_every_token(shell):
    checks = {c["id"]: c for c in shell._lane_metric_integrity()}
    assert checks["mi_dchub"]["pass"] is True
    assert checks["mi_dchub"]["critical"] is True
    for cid in ("mi_test", "mi_probe", "mi_verify", "mi_harness"):
        assert checks[cid]["pass"] is True, checks[cid]["detail"]


# ── never PASS what you could not check ───────────────────────────────

def test_verdict_never_green_for_an_unverified_lane(shell):
    v = shell._lane_verdict
    assert v([{"pass": None, "critical": False}]) == "?"   # the #30-33 gap
    assert v([{"pass": None, "critical": True}]) == "?"
    assert v([{"pass": False, "critical": False}]) == "FAIL"
    assert v([{"pass": True, "critical": True}]) == "PASS"
    assert v([{"pass": True, "critical": True},
              {"pass": None, "critical": False}]) == "PASS"


@pytest.mark.parametrize("lane", ["_lane_demand", "_lane_rail_health",
                                  "_lane_reachability", "_lane_pricing"])
def test_lanes_degrade_without_db_or_probe(shell, lane):
    checks = getattr(shell, lane)()
    assert checks, "%s returned no checks" % lane
    assert not [c for c in checks if c["pass"] is True], \
        "%s reported PASS with no DB and no probe" % lane


# ── the percent trap ──────────────────────────────────────────────────

def test_no_query_passes_empty_params(shell):
    """The imported predicate carries doubled %% — psycopg2 only collapses it
    when substituting, so None/() params leave literal '%%' in every LIKE."""
    src = open(os.path.join(ROOT, "routes/agent_pay_master_shell.py"),
               encoding="utf-8").read()
    assert not re.findall(r"_q\(cur,[^;]*?,\s*None\)", src, re.S)
    assert not re.findall(r"_q\(cur,[^;]*?,\s*\(\)\)", src, re.S)


def test_predicate_renders_single_percent_likes(shell):
    synth = shell._synth_predicates()[1]
    rendered = re.sub(r"%(%|s)",
                      lambda m: "%" if m.group(1) == "%" else "'P'", synth)
    pats = re.findall(r"LIKE '([^']*)'", rendered)
    assert pats
    for p in pats:
        assert p.startswith("%") and p.endswith("%") and "%%" not in p, p


def test_sse_parser_splits_only_on_newline(shell):
    """str.splitlines() also breaks on U+0085/U+2028, which appear inside DC
    Hub's own JSON payloads — that severs the data: line mid-object."""
    src = open(os.path.join(ROOT, "routes/agent_pay_master_shell.py"),
               encoding="utf-8").read()
    # Comments explain the trap by name — check CODE, not prose.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert ".splitlines()" not in code, \
        "use split('\\n'); splitlines() breaks on U+0085/U+2028 inside JSON"
    assert 'decode("utf-8"' in code, \
        "SSE reply carries no charset — requests falls back to ISO-8859-1"


# ── wiring ────────────────────────────────────────────────────────────

def test_tick_is_failsoft_and_wellformed(shell):
    t = shell._run_tick()
    assert t["ok"] and t["shell"] == "agent-pay-34"
    assert len(t["lanes"]) == 5
    for ln in t["lanes"]:
        assert ln["verdict"] in ("PASS", "FAIL", "?"), ln


def test_routes_admin_gated_and_no_store(shell):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.agent_pay_master_shell_bp)
    os.environ["DCHUB_ADMIN_KEY"] = "secret-under-test"
    c = app.test_client()

    r = c.get("/api/v1/admin/agent-pay/master-tick")
    assert r.status_code == 401
    # ★CF caches admin GETs ~30min — a stale board is indistinguishable from a
    # failed deploy, which already cost a debugging cycle on 2026-07-25.
    assert r.headers.get("Cache-Control") == "no-store"

    r = c.get("/api/v1/admin/agent-pay/master-tick",
              headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200
    assert r.get_json()["shell"] == "agent-pay-34"
    assert r.headers.get("Cache-Control") == "no-store"

    r = c.get("/admin/agent-pay", headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200 and b"Agent Pay" in r.data


def test_kill_switch(shell):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.agent_pay_master_shell_bp)
    os.environ["AGENT_PAY_SHELL_DISABLE"] = "1"
    try:
        assert c_status(app) == 503
    finally:
        os.environ.pop("AGENT_PAY_SHELL_DISABLE", None)


def c_status(app):
    return app.test_client().get(
        "/api/v1/admin/agent-pay/master-tick").status_code


def test_registered_in_main():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "agent_pay_master_shell_bp" in src
    assert "register_blueprint(agent_pay_master_shell_bp)" in src
