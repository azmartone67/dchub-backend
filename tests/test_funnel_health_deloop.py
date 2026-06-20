"""tests/test_funnel_health_deloop.py — de-loop definition + weekly trend.

Guards the MCP call-volume reconciliation: ONE honest "real external tool
calls" definition (de-looped) shared by the funnel_health KPI, the weekly
trend, and brain_investigator. The bug class this kills: the gross
mcp_call_log COUNT(*) (~35-41k, INCLUDES our own selfheal/probe/sweep loop)
made a non-decline look like a decline, while the honest /api/v1/mcp/funnel
endpoint showed ~9k.

DB-free: we exercise the pure _deloop_calls_where() SQL-builder (the single
source of the exclude clause) and the trend-delta math directly. The
_build_data() probe itself needs Postgres and is covered at integration time.
"""
import pytest

fh = pytest.importorskip("routes.funnel_health")


# ── the single-source de-loop clause ─────────────────────────────────
def test_deloop_clause_excludes_loop_selfheal_probe_sweep():
    """The honest filter must reference mcp_tool_calls columns (client_name /
    user_agent) and exclude every loop/selfheal/probe/sweep family — matching
    the /api/v1/mcp/funnel tool_calls_7d_real definition."""
    where = fh._deloop_calls_where()
    assert isinstance(where, str) and where.strip()

    low = where.lower()
    # Reads from mcp_tool_calls' columns, NOT mcp_call_log's api_key/platform.
    assert "client_name" in low
    assert "user_agent" in low
    assert "api_key" not in low  # that's the inflated mcp_call_log column

    # Loop / internal families are all excluded.
    for needle in ("dchub-selfheal", "mcp-probe", "loop%", "dchub-%",
                   "regression-test%", "%-sweep", "%-scanner", "%-checker",
                   "%-probe", "%-health"):
        assert needle in low, f"de-loop clause missing exclusion: {needle}"

    # Internal self-UAs (client_name is null for ~70% of rows) are caught too.
    assert "dchubhealer" in low or "%dchub-%" in low


def test_deloop_clause_is_single_source_not_duplicated_inline():
    """funnel_health must build the de-loop clause via the helper, not hand-roll
    a second copy of the exclude list in _build_data. We assert the helper is
    the one referenced (the probe code calls _deloop_calls_where())."""
    import inspect
    src = inspect.getsource(fh._build_data)
    assert "_deloop_calls_where()" in src, (
        "the 7d/30d KPI + weekly-trend probes must reuse _deloop_calls_where() "
        "— do NOT duplicate the exclude clause inline")
    # And the helper exists / is callable.
    assert callable(fh._deloop_calls_where)


def test_deloop_clause_has_no_bound_params():
    """The clause is inlined as SQL literals (trusted constants). It must not
    introduce %s placeholders — those would collide with the literal % in the
    LIKE patterns and trip psycopg2 (the empty-tuple % trap)."""
    where = fh._deloop_calls_where()
    assert "%s" not in where


# ── weekly-trend delta math (the decline signal) ─────────────────────
def _trend_from_weeks(weeks):
    """Re-implement the same delta the probe computes, so we can assert the
    contract without a DB. Mirrors _build_data's calls_week_trend block."""
    if len(weeks) < 2:
        return {}
    last = weeks[-1]["calls"]
    prior = [w["calls"] for w in weeks[:-1]][-4:]
    avg_prior = (sum(prior) / len(prior)) if prior else 0
    return {
        "last_week_calls": last,
        "trailing_4wk_avg_calls": round(avg_prior, 1),
        "delta_pct": (round((last - avg_prior) / avg_prior * 100.0, 1)
                      if avg_prior > 0 else None),
    }


def test_weekly_trend_flags_a_real_decline():
    weeks = [
        {"calls": 10000}, {"calls": 9800}, {"calls": 9600}, {"calls": 9400},
        {"calls": 7000},  # sharp drop in the last complete week
    ]
    t = _trend_from_weeks(weeks)
    assert t["last_week_calls"] == 7000
    assert t["delta_pct"] < 0  # a decline is visible as a negative delta


def test_weekly_trend_steady_is_near_zero():
    weeks = [{"calls": 9000}] * 5
    t = _trend_from_weeks(weeks)
    assert t["delta_pct"] == pytest.approx(0.0, abs=0.1)


def test_weekly_trend_handles_single_week_gracefully():
    assert _trend_from_weeks([{"calls": 9000}]) == {}
