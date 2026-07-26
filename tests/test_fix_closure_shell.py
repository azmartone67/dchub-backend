"""Fix Closure Master Shell (#33, 2026-07-26) — pins the wave's contracts.

The centerpiece is the eia_retail_rates repair: broken since 2026-05-14, the
sync's UndefinedColumn exception was the ONLY thing preventing a blanket
DELETE from destroying a mirror whose 2021-2024 annual history the source
cannot regenerate (source monthlies start 2025-04), and whose consumers key
on word-form vocabulary the source doesn't speak. The fix is a bounded
vocabulary-projecting refresh with an explicit zero-row rollback guard — in
BOTH duplicated job blocks. These pins keep a future edit from quietly
re-arming the landmine.

CI-SAFETY: no DATABASE_URL/JWT_SECRET in the unit env; modules import
directly (never via main); DB paths exercised only via fail-soft contracts.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import fix_closure_master_shell as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_shell_registered_and_ticked():
    assert "register_blueprint(fix_closure_master_shell_bp)" in _read("main.py")
    cron = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/fix-closure/master-tick" in cron
    assert "FIX_CLOSURE_SHELL_DISABLE" in cron


def test_shell_house_rules():
    src = _read(os.path.join("routes", "fix_closure_master_shell.py"))
    assert "no-store" in src
    assert "fix-closure-shell-daily" in src
    assert "cb=%d" in src   # zone caches admin GETs — every edge fetch busts


def test_lane_verdict_never_green_by_silence(shell):
    assert shell._lane_verdict(
        [shell._check("x", "x", None, "?", critical=True)]) == "?"
    assert shell._lane_verdict([shell._check("x", "x", False, "bad")]) == "FAIL"


# ── the landmine repair, pinned ──────────────────────────────────────

def test_eia_sync_projects_vocabulary_in_both_blocks():
    """Both duplicated Phase-SS blocks must carry the projecting INSERT
    (state-name map + sector words + annual rollup), never the naive copy."""
    src = _read("main.py")
    # NB: grep the CONTIGUOUS source fragment — the full sentence is split
    # across adjacent string literals (the same fragment-blindness that bit
    # the lint's ON CONFLICT rule twice this week).
    assert src.count("back to preserve the mirror") == 2, \
        "both sync blocks must carry the zero-row rollback guard"
    assert "SELECT state, sector, price_cents_kwh, period, retrieved_at" \
        not in src, "the naive vocabulary-blind copy is back"
    assert src.count("WHEN 'COM' THEN 'commercial'") == 2
    assert src.count("('AK','Alaska')") == 2, \
        "the state-name projection map must exist in both blocks"


def test_eia_sync_delete_is_bounded():
    """A bare DELETE FROM eia_retail_rates destroys irreplaceable 2021-2024
    history. Every DELETE on the mirror must be scoped by period."""
    src = _read("main.py")
    for m in re.finditer(r"DELETE FROM eia_retail_rates(.{0,120})", src,
                         re.S):
        assert "WHERE period IN" in m.group(1), \
            "unbounded DELETE on eia_retail_rates found"


# ── media hygiene, pinned ────────────────────────────────────────────

def test_media_persist_is_daily_idempotent():
    src = _read(os.path.join("routes", "media_growth_master_shell.py"))
    assert "created_at::date = CURRENT_DATE" in src, \
        "the cron-window dedup guard is gone — 8-11 duplicate rows/day return"


# ── zone envelope, pinned ────────────────────────────────────────────

def test_repo_fallback_list_matches_canon():
    src = _read("worker.js")
    m = re.search(r"const MCP_FALLBACK_TOOLS = \[\n(.*?)\n\];", src, re.S)
    assert m, "MCP_FALLBACK_TOOLS array missing from worker.js"
    n = len(re.findall(r'\{ name: "', m.group(1)))
    canon = _read("ai_surface_canon.py")
    want = int(re.search(r'"tools_advertised":\s*(\d+)', canon).group(1))
    assert n == want, ("fallback list has %d entries, canon %d — the /mcp "
                       "envelope reports this array's length" % (n, want))


def test_fallback_list_carries_the_seven_restored_tools():
    src = _read("worker.js")
    for name in ("plan_query", "research_task", "standing_intent",
                 "get_global_power", "get_permitting_intel",
                 "simulate_scenario", "cluster_sites_by_latency"):
        assert '{ name: "%s"' % name in src, name


# ── retention instrument, pinned ─────────────────────────────────────

def test_retention_lane_casts_text_timestamps():
    # api_keys.created_at/last_used_at are TEXT on live — an uncast
    # date_trunc raises UndefinedFunction and the lane goes dark.
    src = _read(os.path.join("routes", "fix_closure_master_shell.py"))
    assert "NULLIF(created_at, '')::timestamptz" in src
    assert "NULLIF(last_used_at, '')::timestamptz" in src


# ── paywall probe contract ───────────────────────────────────────────

def test_paywall_probe_is_canary_scoped_and_failsoft(shell, monkeypatch):
    monkeypatch.delenv("PAYWALL_CANARY_KEY", raising=False)
    checks = shell._lane_paywall_contract()
    assert checks[0]["pass"] is None, \
        "no canary key must render '?', never a green"
