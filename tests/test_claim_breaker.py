"""tests/test_claim_breaker.py — the claim-breaker gate (Claim Loop step 3).

ONE gate, five lie classes, a must-stay-green control. Two-sided by design: a
guard that cannot fail is not a guard, so every class is proved to REFUSE its
own known-bad replay AND to PASS the control.

Mutation-gated (verify-a-guard): mutating ANY class function to `return []`
turns its `test_<class>_refuses_its_replay` assertion RED while the control test
stays green — recorded in the PR body.

Pure functions: no DB, no network, never imports main.py. `_live_facility_counts`
is pinned to the 2026-08-17 production reading so rows!=buildings is deterministic.

Run:  python3 -m pytest tests/test_claim_breaker.py -v
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cb = pytest.importorskip("routes.claim_breaker")  # noqa: E402


# The verbatim 2026-08-17 LinkedIn post — raw source ROWS published as buildings.
POST_26K = ("26,000 data-center facilities are now live in DC Hub's index, "
            "spanning 179 countries. That is up from the 18,000+ this cycle.")

# Live canon that day: distinct BUILDINGS vs raw source ROWS.
DISTINCT, RECORDS = 18406, 26137

# The live weekly series (verbatim from the endpoint, 2026-08-11): the 2026-07-27
# calls spike is ~3.6x its neighbours — the outlier-baseline lie class.
LIVE_SPIKE = [
    {"week_start": "2026-06-15", "agents": 0, "calls": 38, "status": "measured", "partial": False},
    {"week_start": "2026-06-22", "agents": 0, "calls": 84, "status": "measured", "partial": False},
    {"week_start": "2026-06-29", "agents": 1, "calls": 281, "status": "measured", "partial": False},
    {"week_start": "2026-07-06", "agents": 43, "calls": 3514, "status": "measured", "partial": False},
    {"week_start": "2026-07-13", "agents": 81, "calls": 2701, "status": "measured", "partial": False},
    {"week_start": "2026-07-20", "agents": 62, "calls": 1971, "status": "measured", "partial": False},
    {"week_start": "2026-07-27", "agents": 85, "calls": 8334, "status": "measured", "partial": False},
    {"week_start": "2026-08-03", "agents": 38, "calls": 2381, "status": "measured", "partial": False},
]


@pytest.fixture(autouse=True)
def pin_live_counts(monkeypatch):
    """rows!=buildings deterministically sees the 2026-08-17 reading."""
    from routes import media_fact_check_guard as g
    monkeypatch.setattr(g, "_live_facility_counts", lambda: (DISTINCT, RECORDS))


@pytest.fixture(autouse=True)
def breaker_enabled(monkeypatch):
    """Every test starts with the kill switch OFF unless it flips it."""
    monkeypatch.delenv(cb.KILL_SWITCH_ENV, raising=False)


# ── THE PIN ──────────────────────────────────────────────────────────────────

def test_the_published_post_is_refused_with_rows_ne_buildings():
    """THE PIN — the exact post that shipped is refused, and the class that
    catches it is rows_ne_buildings (guards 1-3 corroborated it against the raw
    row count and passed it)."""
    res = cb.breaker(POST_26K, "post")
    assert res["ok"] is False, "the raw-rows-as-buildings post must not ship"
    assert res["trusted"] is True, "the control must be green on this call"
    assert res["fail_closed"] is True
    classes = {v["cls"] for v in res["violations"]}
    assert "rows_ne_buildings" in classes


def test_the_control_stays_green_on_a_clean_post():
    """The corrected #1827 copy is count-free of any canon metric, so it clears
    every text class in both a DB-up and DB-down environment."""
    res = cb.breaker(cb._CONTROL_POST_TEXT, "post")
    assert res["ok"] is True and res["trusted"] is True
    assert res["violations"] == []
    assert res["control"]["ok"] is True


# ── each class REFUSES its replay (mutation targets) ────────────────────────

def test_rows_ne_buildings_refuses_its_replay():
    assert cb._cls_rows_ne_buildings(POST_26K, {}), "26,000 buildings must flag"
    assert cb._cls_rows_ne_buildings(cb._CONTROL_POST_TEXT, {}) == []


def test_canon_overclaim_refuses_its_replay():
    bad = "DC Hub now tracks " + "50," + "000 facilities and $" + "324B in deals."
    assert cb._cls_canon_numbers(bad, {}), "banned over-claims must flag"
    assert cb._cls_canon_numbers(cb._CONTROL_POST_TEXT, {}) == []


def test_renamed_sentinel_refuses_its_replay():
    # A drifted sentinel set missing the generic bucket.
    assert cb._cls_renamed_sentinel(None, {"sentinels": ("unknown", "")}), \
        "a dropped generic sentinel must flag"
    # The frozen good control set passes.
    assert cb._cls_renamed_sentinel(None, {"sentinels": cb._CONTROL_SENTINELS}) == []


def test_renamed_sentinel_blocks_a_real_live_drift(monkeypatch):
    """End-to-end: a real drift in the LIVE _GENERIC_PLATFORMS makes the gate
    BLOCK (fail closed) while the frozen-set control keeps it TRUSTED — the
    class contributes a real refusal, not merely an untrusted shrug."""
    import routes.growth_funnel_master_shell as gf
    monkeypatch.setattr(gf, "_GENERIC_PLATFORMS", ("mcp", "unknown", "", None))
    res = cb.breaker(cb._CONTROL_POST_TEXT, "canon")
    assert res["trusted"] is True, "the frozen-set control must stay green"
    assert res["ok"] is False
    assert "renamed_sentinel" in {v["cls"] for v in res["violations"]}


def test_partial_week_refuses_its_replay():
    replay = {"current_week_partial": {"partial": True,
                                       "excluded_from_delta": False,
                                       "week_start": "2026-08-10"}}
    assert cb._cls_partial_week(replay, {}), "a partial week in a delta must flag"
    assert cb._cls_partial_week(cb._CONTROL_PAYLOAD, {}) == []


def test_partial_week_uses_the_canonical_complete_week_sql():
    """A declared basis SQL that diverges from the canonical builder flags."""
    from mcp_calls_deloop import canonical_external_complete_week_sql
    good = {"complete_week_sql": canonical_external_complete_week_sql(0),
            "weeks_back": 0}
    assert cb._cls_partial_week(good, {}) == []
    bad = {"complete_week_sql": "SELECT 1 -- bespoke window", "weeks_back": 0}
    assert cb._cls_partial_week(bad, {})


def test_wow_outlier_refuses_its_replay():
    replay = {"weeks": LIVE_SPIKE}  # no robust_wow -> headlining the naive WoW
    assert cb._cls_wow(replay, {}), "an outlier baseline w/o robust_wow must flag"
    # The same series WITH robust_wow published is honest -> no violation.
    assert cb._cls_wow({"weeks": LIVE_SPIKE, "robust_wow": {"x": 1}}, {}) == []
    assert cb._cls_wow(cb._CONTROL_PAYLOAD, {}) == []


def test_wow_outlier_fires_on_a_non_calls_metric(monkeypatch):
    """The extension to EVERY metric: a spike in `agents` (not calls) must also
    be catchable — the whole point of extending wow_baseline_check."""
    weeks = [
        {"week_start": "2026-06-01", "agents": 10, "calls": 100, "status": "measured", "partial": False},
        {"week_start": "2026-06-08", "agents": 11, "calls": 110, "status": "measured", "partial": False},
        {"week_start": "2026-06-15", "agents": 9, "calls": 105, "status": "measured", "partial": False},
        {"week_start": "2026-06-22", "agents": 12, "calls": 108, "status": "measured", "partial": False},
        {"week_start": "2026-06-29", "agents": 90, "calls": 112, "status": "measured", "partial": False},  # agents spike
        {"week_start": "2026-07-06", "agents": 13, "calls": 109, "status": "measured", "partial": False},
    ]
    import routes.weekly_series as ws
    flag = ws._baseline_outlier_flag(weeks)
    assert flag["per_metric"]["agents"]["is_outlier"] is True
    assert flag["per_metric"]["calls"]["is_outlier"] is False
    assert flag["any_metric_outlier"] is True
    assert cb._cls_wow({"weeks": weeks}, {}), "agents-only spike must flag"


@pytest.mark.parametrize("sample", [
    {"platform": "dchub-internal"},
    {"platform": "verify-x"},
    {"session": "88e20dac-abcd-0001"},
    {"user_agent": "python-httpx/0.27"},
    {"origin": "github-actions"},
    {"ip_is_ci": True},
])
def test_self_traffic_refuses_each_lane(sample):
    assert cb._cls_self_traffic({}, {"sample": sample}), \
        f"self-traffic lane not caught: {sample}"


def test_self_traffic_passes_a_real_external_call():
    ctx = {"sample": {"platform": "claude", "session": "agent-xyz-9999",
                      "user_agent": "Claude-User/1.0", "origin": "external"}}
    assert cb._cls_self_traffic({}, ctx) == []


def test_self_traffic_basis_audit_flags_missing_exclusions():
    assert cb._cls_self_traffic({"basis": "COUNT(DISTINCT agent_id)"}, {}), \
        "a basis omitting the exclusions must flag"
    complete = {"basis": ("is_real_external AND external_platform AND "
                          "external_session AND real_ua AND a_ci_origin excluded")}
    assert cb._cls_self_traffic(complete, {}) == []


# ── report-only vs fail-closed posture ──────────────────────────────────────

def test_fact_payload_is_report_only_never_blocks_the_caller():
    """A fact/score violation is REPORTED, not blocked: report_only is set and
    the caller (not the gate) decides to stamp the ledger and ship."""
    res = cb.breaker({"weeks": LIVE_SPIKE}, "fact")
    assert res["report_only"] is True and res["fail_closed"] is False
    assert res["trusted"] is True
    assert "wow_outlier" in {v["cls"] for v in res["violations"]}


# ── break the control -> UNTRUSTED, not RED ─────────────────────────────────

def test_broken_text_control_reports_untrusted_and_does_not_block(monkeypatch):
    """If the control input is broken, the gate reports trusted=False and
    ok=True — it must not block shipping on its own say-so."""
    monkeypatch.setattr(cb, "_CONTROL_POST_TEXT", POST_26K)  # a KNOWN-BAD control
    res = cb.breaker(POST_26K, "post")
    assert res["trusted"] is False, "a failing control must mark the gate untrusted"
    assert res["ok"] is True, "untrusted is UNTRUSTED, not RED — callers ship"
    assert res["disabled"] is False
    assert res["control"]["ok"] is False


def test_broken_payload_control_reports_untrusted(monkeypatch):
    monkeypatch.setattr(cb, "_CONTROL_PAYLOAD",
                        {"current_week_partial": {"partial": True,
                                                  "excluded_from_delta": False,
                                                  "week_start": "x"}})
    res = cb.breaker({"weeks": LIVE_SPIKE}, "fact")
    assert res["trusted"] is False and res["ok"] is True


# ── kill switch ─────────────────────────────────────────────────────────────

def test_kill_switch_ships_and_flags_disabled(monkeypatch):
    monkeypatch.setenv(cb.KILL_SWITCH_ENV, "1")
    res = cb.breaker(POST_26K, "post")
    assert res == {**res, "ok": True, "trusted": False, "disabled": True}
    assert res["violations"] == []


# ── breaker_summary (Step 5's page reads this) ──────────────────────────────

def test_breaker_summary_shape_and_counts():
    cb.breaker(POST_26K, "post")               # blocked
    cb.breaker(cb._CONTROL_POST_TEXT, "post")  # clean
    s = cb.breaker_summary()
    assert set(s) >= {"disabled", "counts", "recent", "classes"}
    assert s["counts"]["calls"] >= 2
    assert isinstance(s["recent"], list) and s["recent"]
    assert "rows_ne_buildings" in s["classes"]


def test_summary_never_raises_when_empty():
    # Fresh import path is already populated by other tests; just prove it runs.
    assert isinstance(cb.breaker_summary(limit=5), dict)


# ── admin status endpoint ───────────────────────────────────────────────────

flask = pytest.importorskip("flask")


def _app():
    app = flask.Flask(__name__)
    cb.register_claim_breaker(app)
    return app.test_client()


def test_status_is_404_when_disabled(monkeypatch):
    """Disabled -> 404, never 5xx: the surface is simply absent."""
    monkeypatch.setenv(cb.KILL_SWITCH_ENV, "1")
    r = _app().get("/api/v1/brain/claim-breaker/status")
    assert r.status_code == 404
    assert r.get_json()["disabled"] is True


def test_status_is_401_without_a_credential(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "a-real-key-is-set")
    r = _app().get("/api/v1/brain/claim-breaker/status")
    assert r.status_code == 401


def test_status_is_200_with_a_credential(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "a-real-key-is-set")
    r = _app().get("/api/v1/brain/claim-breaker/status",
                   headers={"X-Admin-Key": "a-real-key-is-set"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert "summary" in r.get_json()


def test_main_registers_the_breaker():
    """main.py must call register_claim_breaker(app) (AST, not a grep)."""
    import ast
    src = open("main.py", encoding="utf-8").read()
    tree = ast.parse(src)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "register_claim_breaker" in called, \
        "main.py never calls register_claim_breaker(app)"


# ── the wow_baseline_check extension stays additive ─────────────────────────

def test_baseline_outlier_flag_keeps_its_legacy_shape():
    """The calls-based headline flag is UNCHANGED; per_metric is purely added."""
    import routes.weekly_series as ws
    f = ws._baseline_outlier_flag(LIVE_SPIKE)
    assert f["metric"] == "calls" and f["is_outlier"] is True
    assert f["ratio_to_median"] == 3.57
    assert f["baseline_week_start"] == "2026-07-27"
    # additive block
    assert f["per_metric"]["calls"]["is_outlier"] is True
    assert f["per_metric"]["agents"]["is_outlier"] is False
    assert f["any_metric_outlier"] is True
