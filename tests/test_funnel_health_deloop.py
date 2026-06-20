"""tests/test_funnel_health_deloop.py — de-loop definition + weekly trend.

Guards the MCP call-volume reconciliation: ONE honest "real external tool
calls" definition (de-looped) shared by the funnel_health KPI, the weekly
trend, the /api/v1/mcp/funnel endpoint, and brain_investigator. The bug class
this kills: the gross mcp_call_log COUNT(*) (~35-41k, INCLUDES our own
selfheal/probe/sweep loop) made a non-decline look like a decline, while the
honest /api/v1/mcp/funnel endpoint showed ~9k — AND the two "honest" numbers
(funnel_health vs the endpoint) had DRIFTED to different exclusion lists.

The single source now lives in top-level mcp_calls_deloop.py:
  • PLATFORM_CASE        — the UA/client_name → platform classifier
  • PROBE_PLATFORMS      — the classifier outputs treated as self/probe
  • real_calls_predicate() / deloop_calls_where() — the byte-identical filter.

routes/funnel_health._deloop_calls_where() and flask_mcp_endpoints.mcp_funnel()
both import from there, so their `tool_calls_7d_real` counts are identical by
construction. These tests assert they stay in lock-step.

DB-free: we exercise the pure SQL-builders + trend-delta math directly. The
_build_data() probe itself needs Postgres and is covered at integration time.
flask_mcp_endpoints requires DATABASE_URL at IMPORT time, so we assert its
single-sourcing by SOURCE inspection (a grep of the module file), not by
importing it.
"""
import os
import re

import pytest

fh = pytest.importorskip("routes.funnel_health")
deloop = pytest.importorskip("mcp_calls_deloop")


# ── the single-source de-loop clause ─────────────────────────────────
def test_deloop_clause_excludes_loop_selfheal_probe_sweep():
    """The honest filter must reference mcp_tool_calls columns (client_name /
    user_agent) and exclude every loop/selfheal/probe/sweep family — via the
    canonical PLATFORM_CASE classifier + PROBE_PLATFORMS list. matching the
    /api/v1/mcp/funnel tool_calls_7d_real definition."""
    where = fh._deloop_calls_where()
    assert isinstance(where, str) and where.strip()

    low = where.lower()
    # Reads from mcp_tool_calls' columns, NOT mcp_call_log's api_key/platform.
    assert "client_name" in low
    assert "user_agent" in low
    assert "api_key" not in low  # that's the inflated mcp_call_log column

    # The classifier folds our own selfheal/probe/sweep traffic into the
    # 'internal-dchub' bucket via the self-UA WHEN clause, then excludes it in
    # the NOT IN (...) probe list. So those families ARE de-looped even though
    # the literal client-name strings no longer appear verbatim.
    assert "internal-dchub" in low, "probe list must exclude 'internal-dchub'"
    # The self-UA fold is present (catches null-client_name self-traffic).
    for ua in ("dchubhealer", "brain-radar", "uptimerobot"):
        assert ua in low, f"self-UA fold missing: {ua}"
    # The generic scripting probes (curl/python/node/postman/insomnia/verify)
    # are in the excluded probe list.
    for probe in ("curl", "python-script", "node-script", "postman",
                  "insomnia", "verify"):
        assert probe in low, f"probe list missing: {probe}"


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
    introduce psycopg2 placeholders (%s / %d / %(name)s) — those would bind
    against the literal % in the LIKE/ILIKE patterns and trip the driver (the
    empty-tuple % trap). Note: %smithery% / %sourcegraph% are LEGAL LIKE
    literals — '%s' as a substring of '%smithery%' is NOT a placeholder, so we
    match only placeholder-shaped tokens, not every '%s' substring."""
    where = fh._deloop_calls_where()
    # %s / %d / %f placeholders sit at a token boundary: % + conversion-letter
    # followed by a non-letter (space, comma, paren, end-of-string). A LIKE
    # literal like '%smithery%' has the conversion-letter followed by MORE
    # letters, so it won't match this.
    assert not re.search(r"%[sdf](?![a-z])", where), (
        "de-loop clause contains a psycopg2 placeholder — it must be pure "
        "SQL literals executed with NO params tuple")
    assert not re.search(r"%\([a-z_]+\)[sdf]", where), (
        "de-loop clause contains a named psycopg2 placeholder")


# ── GAP 1: the two de-loop definitions must be byte-identical ────────
def test_funnel_health_deloop_is_byte_identical_to_shared_canonical():
    """routes/funnel_health._deloop_calls_where() must return EXACTLY what the
    shared canonical mcp_calls_deloop.deloop_calls_where() returns — character
    for character. This is what makes funnel_health.tool_calls_7d_real equal to
    the /api/v1/mcp/funnel endpoint's tool_calls_7d_real."""
    assert fh._deloop_calls_where() == deloop.deloop_calls_where()


def test_shared_real_predicate_equals_deloop_where():
    """The endpoint FILTERs tool_calls_7d_real on real_calls_predicate(); the
    dashboard ANDs deloop_calls_where(). They must be the same predicate so the
    two counts match exactly."""
    assert deloop.real_calls_predicate() == deloop.deloop_calls_where()
    # probe predicate is the exact logical complement (used for probe_calls).
    assert deloop.probe_calls_predicate() != deloop.real_calls_predicate()
    assert "NOT IN" in deloop.real_calls_predicate()
    assert " IN " in deloop.probe_calls_predicate()


def test_endpoint_sources_tool_calls_real_from_shared_module():
    """flask_mcp_endpoints.mcp_funnel() must build tool_calls_7d_real from the
    shared module (so it can't silently re-fork its own probe list). We assert
    by SOURCE inspection because flask_mcp_endpoints requires DATABASE_URL at
    import time. The grep proves the import + the predicate are wired."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "flask_mcp_endpoints.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    # Imports the shared canonical names.
    assert "from mcp_calls_deloop import" in src
    assert "real_calls_predicate as _deloop_real_calls_predicate" in src
    assert "PLATFORM_CASE as _DELOOP_PLATFORM_CASE" in src
    assert "PROBE_PLATFORMS as _DELOOP_PROBE_PLATFORMS" in src
    # tool_calls_7d_real is FILTERed on the shared predicate, not a re-rolled
    # inline `{_platform_case} NOT IN ({_probe_in})`.
    assert "_deloop_real_calls_predicate()" in src
    assert 'out["tool_calls_7d_real"]' in src


def test_endpoint_and_funnel_health_use_same_probe_list():
    """Both the endpoint's _PROBE_PLATFORMS alias and funnel_health's
    re-exported _PROBE_CLIENT_NAMES resolve to the shared PROBE_PLATFORMS."""
    # funnel_health re-exports the shared list for back-compat.
    assert tuple(fh._PROBE_CLIENT_NAMES) == tuple(deloop.PROBE_PLATFORMS)
    # 'internal-dchub' is the load-bearing addition (our own crawler/healer
    # UAs fold here) — it MUST be in the probe list or self-traffic leaks.
    assert "internal-dchub" in deloop.PROBE_PLATFORMS


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
