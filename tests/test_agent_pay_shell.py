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

    r = c.get("/api/v1/admin/agent-pay-shell/master-tick")
    assert r.status_code == 401
    # ★CF caches admin GETs ~30min — a stale board is indistinguishable from a
    # failed deploy, which already cost a debugging cycle on 2026-07-25.
    assert r.headers.get("Cache-Control") == "no-store"

    r = c.get("/api/v1/admin/agent-pay-shell/master-tick",
              headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200
    assert r.get_json()["shell"] == "agent-pay-34"
    assert r.headers.get("Cache-Control") == "no-store"

    r = c.get("/admin/agent-pay-shell", headers={"X-Admin-Key": "secret-under-test"})
    assert r.status_code == 200 and b"Agent Pay" in r.data


def test_kill_switch(shell):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.agent_pay_master_shell_bp)
    os.environ["AGENT_PAY_SHELL_DISABLE"] = "1"
    try:
    # ★2026-08-12: was `== 503`. That assertion PINNED THE HAZARD — the CF
    # worker's proxyWithRetry reads any 5xx from Railway as a dead origin and
    # fails the site over to the stale Render backend, so disabling one
    # read-only diagnostic could take the whole site stale. 22 shells returned
    # 503; graph_spine already returned 404 and documented why. This is not a
    # weakening: the guarantee (a disabled shell must answer with an explicit
    # non-2xx) is unchanged and now enforced repo-wide by
    # tests/test_shell_killswitch_never_5xx.py.
        assert c_status(app) == 404
    finally:
        os.environ.pop("AGENT_PAY_SHELL_DISABLE", None)


def c_status(app):
    return app.test_client().get(
        "/api/v1/admin/agent-pay-shell/master-tick").status_code


def test_registered_in_main():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "agent_pay_master_shell_bp" in src
    assert "register_blueprint(agent_pay_master_shell_bp)" in src


def test_does_not_shadow_the_watcher_route(shell):
    """/api/v1/admin/agent-pay/master-tick belongs to funnel_health — it is the
    endpoint the dchub-agent-pay-watcher scheduled task calls. Registering it
    here would shadow the watcher (regression-lint caught this pre-merge)."""
    src = open(os.path.join(ROOT, "routes/agent_pay_master_shell.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert '.route("/api/v1/admin/agent-pay/master-tick"' not in code
    assert '.route("/api/v1/admin/agent-pay-shell/master-tick"' in code


def test_probe_is_memoized_and_budgeted(shell):
    """One tick used to fire THREE handshakes at prod (lane 2, then lane 3
    twice). Memo + a tick-wide budget bound what a wedged gateway can cost a
    single-replica web service."""
    calls = []
    orig = shell._mcp_probe_uncached
    shell._mcp_probe_uncached = lambda t, a: (calls.append(t), ({}, None))[1]
    try:
        shell._probe_memo_clear()
        shell._mcp_probe("analyze_site", {"lat": 1, "lon": 2})
        shell._mcp_probe("analyze_site", {"lat": 1, "lon": 2})
        shell._mcp_probe("analyze_site", {"lat": 1, "lon": 2})
        assert len(calls) == 1, "identical probe must be served from the memo"
        shell._probe_memo_clear()
        shell._mcp_probe("analyze_site", {"lat": 1, "lon": 2})
        assert len(calls) == 2, "a new tick must re-probe for fresh evidence"
    finally:
        shell._mcp_probe_uncached = orig
    assert shell._PROBE_BUDGET_S <= 30.0


# ── the flattering zero (adversarial review, 2026-07-25) ──────────────

class _FailingCursor:
    """A LIVE connection whose queries fail — the case that shipped a green
    lane 4 reading 'clean — 0 verify failures/30d' while the ledger was
    unreadable. _q() swallows the exception and returns None; any caller that
    then treats None as 0 fabricates a healthy answer."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise RuntimeError("SSL connection has been closed unexpectedly")

    def fetchone(self):
        raise AssertionError("unreachable")

    def fetchall(self):
        raise AssertionError("unreachable")


class _FailingConn:
    def cursor(self):
        return _FailingCursor()

    def close(self):
        pass


@pytest.mark.parametrize("lane", ["_lane_demand", "_lane_rail_health",
                                  "_lane_reachability"])
def test_no_lane_fabricates_a_pass_from_a_failed_query(shell, monkeypatch, lane):
    """A live connection whose every query errors must never yield a True
    check. This is the class the review caught on lane 4 — pinned for ALL
    lanes so it cannot reappear in another one."""
    monkeypatch.setattr(shell, "_db", lambda: _FailingConn())
    checks = getattr(shell, lane)()
    greens = [c for c in checks if c["pass"] is True]
    assert not greens, \
        "%s fabricated PASS from a failed query: %s" % (
            lane, [(c["id"], c["detail"]) for c in greens])
    assert shell._lane_verdict(checks) != "PASS"


def test_lane4_says_unknown_not_clean_when_the_ledger_is_unreadable(
        shell, monkeypatch):
    monkeypatch.setattr(shell, "_db", lambda: _FailingConn())
    checks = {c["id"]: c for c in shell._lane_rail_health()}
    c = checks["rh_settle_errors"]
    assert c["pass"] is None
    assert "0 verify failures" not in c["detail"], \
        "must not report a failure count it never read"
    assert "UNKNOWN" in c["detail"]


def test_probe_treats_a_jsonrpc_error_as_unknown(shell, monkeypatch):
    """An MCP error response carries no `result`. Reading it as an empty
    structuredContent made lane 2 report 'the rail is invisible' for what was
    actually a failed probe."""
    class _R:
        status_code = 200
        headers = {"mcp-session-id": "s"}
        content = (b'event: message\ndata: {"jsonrpc":"2.0","id":2,'
                   b'"error":{"code":-32603,"message":"boom"}}\n')
        text = content.decode()

    class _S:
        def post(self, *a, **k):
            return _R()

    monkeypatch.setattr(shell, "_probe_memo_clear", lambda: None)
    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    sc, err = shell._mcp_probe_uncached("analyze_site", {"lat": 1, "lon": 2})
    assert sc is None and err and "MCP error" in err, (sc, err)


def test_queries_are_time_bounded(shell):
    """No statement_timeout on the pooled Neon connection means an unbounded
    scan can hold a request open on a single-replica web service."""
    src = open(os.path.join(ROOT, "routes/agent_pay_master_shell.py"),
               encoding="utf-8").read()
    assert "statement_timeout" in src


def test_beat_has_a_scheduler_that_can_actually_fire(shell):
    """The docstring advertises a daily beat; a shell nothing ever calls is a
    dead-man that never fires.

    ★This test used to grep dchub-scheduler.py for the endpoint and passed —
    while the ONLY entry there sat inside DISABLED_JOBS, a dict no run loop
    iterates, in a file nothing invokes (railway.json runs start_web.sh). So the
    feed beat only when a human opened the board, went overdue on 2026-07-29,
    and this test was green throughout. A substring in a dead file is not a
    scheduler. Assert the live workflow instead (#2027).
    """
    wf = os.path.join(ROOT, ".github/workflows/agent-pay-shell-tick.yml")
    assert os.path.exists(wf), "no workflow schedules the shell tick"
    src = open(wf, encoding="utf-8").read()
    assert "/api/v1/admin/agent-pay-shell/master-tick" in src
    assert "schedule:" in src and "cron:" in src, \
        "workflow exists but has no cron — dispatch-only is not a scheduler"


# ── the lane that flapped on trial state, not on price ────────────────

def _pricing_lane(shell, monkeypatch, flagship_offer, deep_offer):
    """Run _lane_pricing with the MCP probe stubbed to a chosen observability."""
    monkeypatch.setenv("AGENT_PAY_SHELL_PROBE", "1")
    monkeypatch.delenv("MPP_FLAGSHIP_PREMIUM_ACK", raising=False)

    def fake_probe(tool, args, _memo=None):
        offer = flagship_offer if tool == "get_grid_intelligence" else deep_offer
        return ({"agent_payment": offer} if offer else {}), None

    monkeypatch.setattr(shell, "_mcp_probe", fake_probe)
    return shell._lane_verdict(shell._lane_pricing())


def test_pricing_lane_is_unknown_when_the_flagship_offer_was_not_observed(
        shell, monkeypatch):
    """The flap: PASS/FAIL/PASS across three ticks in 24 minutes with no deploy.

    The premium never moved. What moved was whether the anon probe was
    trial-granted that tick — the trial is per-IP, so the first tick in a window
    sees no flagship offer and the rest do. With pr_flagship non-critical,
    pr_floor's True suppressed the '?' and "we didn't get to look" rendered PASS.
    """
    assert _pricing_lane(shell, monkeypatch,
                         flagship_offer=None,
                         deep_offer={"price_usd": 0.50}) == "?"


def test_pricing_lane_still_fails_when_the_premium_is_actually_observed(
        shell, monkeypatch):
    """The control. A check made lenient must still be able to fail — otherwise
    the fix above would have converted a flapping lane into a silent one."""
    assert _pricing_lane(shell, monkeypatch,
                         flagship_offer={"price_usd": 0.50},
                         deep_offer={"price_usd": 0.50}) == "FAIL"


def test_pricing_lane_passes_only_when_the_premium_is_acknowledged(
        shell, monkeypatch):
    monkeypatch.setenv("MPP_FLAGSHIP_PREMIUM_ACK", "1")
    monkeypatch.setenv("AGENT_PAY_SHELL_PROBE", "1")
    monkeypatch.setattr(shell, "_mcp_probe",
                        lambda tool, args, _memo=None:
                        ({"agent_payment": {"price_usd": 0.50}}, None))
    assert shell._lane_verdict(shell._lane_pricing()) == "PASS"


def test_lane5_source_checks_survive_a_dead_db_but_its_live_check_does_not(
        shell, monkeypatch):
    """Lane 5's token checks read the imported predicate's SOURCE, so they are
    genuinely verified with the DB down — but its one query-backed check must
    still degrade to unknown rather than claiming the filter is clean."""
    monkeypatch.setattr(shell, "_db", lambda: _FailingConn())
    checks = {c["id"]: c for c in shell._lane_metric_integrity()}
    assert checks["mi_dchub"]["pass"] is True     # source inspection, not a query
    assert checks["mi_live"]["pass"] is None      # query-backed -> unknown
    assert "clean" not in checks["mi_live"]["detail"]


# ── the offer-status / granted-bucket contract (2026-08-15) ───────────
#
# The gateway OVERWRITES a successful call's status when a passive pay offer
# attaches to it. Every such status therefore has to be re-declared as GRANTED
# here, or the offer working silently drains the bucket it rides on: `tot`
# shrinks, and the gated share — the reachability number this shell publishes —
# goes UP precisely because more agents were reached. That is the 07-28
# mpp_offer_prewall defect; mpp_offer_undercap is the same class and fires far
# more often (once per session+tool on ordinary under-cap calls, versus only at
# the last free call for prewall).

_OFFER_STATUSES = ("mpp_offer_prewall", "mpp_offer_undercap")


@pytest.mark.parametrize("status", _OFFER_STATUSES)
def test_passive_offer_statuses_count_as_granted(shell, status):
    assert status in shell._GRANTED_ST, (
        f"{status} rides a SUCCESSFUL answer but is not in _GRANTED_ST — every "
        f"stamped call leaves the granted bucket and inflates the gated share")


@pytest.mark.parametrize("status", _OFFER_STATUSES)
def test_passive_offer_statuses_are_never_counted_as_gated(shell, status):
    """A granted call must not also be gated, or `tot = gated + granted`
    double-counts it and the share is wrong in the other direction."""
    assert status not in shell._GATED_ST


def test_gated_and_granted_sets_are_disjoint(shell):
    assert not (set(shell._GATED_ST) & set(shell._GRANTED_ST))


def test_undercap_reach_is_projected_not_merely_in_the_universe(shell):
    """Being in _GRANTED_ST puts the rows in the window; it does not report
    them. The prewall surface spent months invisible for exactly that reason,
    so the projection is pinned alongside the set membership."""
    import inspect
    src = inspect.getsource(shell._lane_reachability)
    assert "mpp_offer_undercap" in src, "no projection counts the under-cap offer"
    assert "rc_undercap" in src, "the under-cap reach check is not published"
