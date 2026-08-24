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

def _fallback_tool_names():
    """The tool names declared in worker.js MCP_FALLBACK_TOOLS, source order."""
    src = _read("worker.js")
    m = re.search(r"const MCP_FALLBACK_TOOLS = \[\n(.*?)\n\];", src, re.S)
    assert m, "MCP_FALLBACK_TOOLS array missing from worker.js"
    names = re.findall(r'\{ name: "([^"]+)"', m.group(1))
    assert names, "MCP_FALLBACK_TOOLS parsed to ZERO entries — the array shape "
    return names


def _canon_pinned():
    """PINNED itself, not a regex over its source. ai_surface_canon is a leaf
    module (constants + functions, no import-time network or DB), so importing
    it is safe under the no-main rule — and a regex cannot honestly parse an
    81-element list literal."""
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import ai_surface_canon
    return ai_surface_canon.PINNED


def test_repo_fallback_list_matches_canon():
    """ARITY — the /mcp envelope reports this array's length."""
    names = _fallback_tool_names()
    want = _canon_pinned()["tools_advertised"]
    assert len(names) == want, ("fallback list has %d entries, canon %d — the "
                                "/mcp envelope reports this array's length"
                                % (len(names), want))


def test_canon_manifest_is_self_consistent():
    """The count and the name list must move TOGETHER.

    Without this, bumping tools_advertised and forgetting tool_manifest (or
    vice versa) leaves the two canon fields disagreeing, and whichever one a
    given consumer reads decides whether it sees the truth.
    """
    p = _canon_pinned()
    manifest = p["tool_manifest"]
    dupes = sorted({n for n in manifest if manifest.count(n) > 1})
    assert not dupes, "duplicate names in canon tool_manifest: %s" % dupes
    assert len(manifest) == p["tools_advertised"], (
        "canon disagrees with itself: tool_manifest has %d names, "
        "tools_advertised says %d" % (len(manifest), p["tools_advertised"]))


def test_repo_fallback_membership_matches_canon_manifest():
    """MEMBERSHIP — the half a count check cannot see.

    ★2026-07-29 (shell #41 WS5): len(MCP_FALLBACK_TOOLS) == canon was the ONLY
    assertion here, and it is green on a doubly-wrong manifest. The array
    briefly held 81 entries against a live 81 while one member was MISSING and
    a different one was EXTRA — arity matched, the served surface was wrong,
    and the guard passed. Separately, get_hosting_capacity was live but absent
    from the fallback list (live 81 / repo 80) and only arithmetic was ever
    compared. Compare the SET.
    """
    names = _fallback_tool_names()
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, ("duplicate entries in MCP_FALLBACK_TOOLS: %s — a dupe "
                       "inflates the count and can mask a missing tool" % dupes)

    manifest = set(_canon_pinned()["tool_manifest"])
    got = set(names)
    missing = sorted(manifest - got)
    extra = sorted(got - manifest)
    assert not missing and not extra, (
        "MCP_FALLBACK_TOOLS membership has drifted from canon tool_manifest.\n"
        "  missing from worker.js: %s\n"
        "  not in canon (extra)  : %s\n"
        "Re-probe the live gate (POST %s, method tools/list) and sync BOTH "
        "worker.js and PINNED[tool_manifest]/[tools_advertised]."
        % (missing or "none", extra or "none", _canon_pinned()["mcp_endpoint"]))


def test_flagship_tool_names_are_real_advertised_tools():
    """tool_names is a curated FLAGSHIP subset (keyed to TOOL_RETURNS, rendered
    into llms.txt) — every name in it must still be a tool we actually serve,
    or the docs surface advertises something that no longer exists."""
    p = _canon_pinned()
    orphans = sorted(set(p["tool_names"]) - set(p["tool_manifest"]))
    assert not orphans, ("flagship tool_names not in the advertised manifest: "
                         "%s" % orphans)


@pytest.mark.skipif(
    os.environ.get("DCHUB_LIVE_MCP_CHECK") != "1",
    reason=("live tools/list probe is opt-in (DCHUB_LIVE_MCP_CHECK=1); the unit "
            "env is offline, so the pinned canon is the in-CI guard. NOTE: this "
            "skip means repo+canon drifting TOGETHER from live is NOT covered "
            "here — that is what the opt-in run catches."))
def test_pinned_manifest_matches_live_tools_list():
    """The pinned pair's blind spot: worker.js and canon can agree with each
    other and both be behind the live gate. Run on demand after any tool ships:

        DCHUB_LIVE_MCP_CHECK=1 python3 -m pytest tests/test_fix_closure_shell.py -k live
    """
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import ai_surface_canon

    try:
        live = ai_surface_canon._mcp_tool_names()
    except Exception as e:                       # transport, not a contract break
        pytest.skip("MCP gate unreachable: %s" % str(e)[:120])
    assert live, "tools/list returned no tools — probe shape broke, not the canon"

    pinned = set(_canon_pinned()["tool_manifest"])
    live_set = set(live)
    assert live_set == pinned, (
        "canon tool_manifest is behind the LIVE gate.\n"
        "  live but not pinned: %s\n"
        "  pinned but not live: %s"
        % (sorted(live_set - pinned) or "none",
           sorted(pinned - live_set) or "none"))


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
    #
    # 2026-08-24: the cast must go through ::text. These columns are scheduled
    # for TEXT -> timestamptz (migrations/2026-08-24_text_timestamps_tranche_ab
    # .sql), and the old `NULLIF(created_at, '')` form breaks the DAY that
    # lands — verified live against press_releases.published_at, which is
    # already timestamptz: `invalid input syntax for type timestamp with time
    # zone: ""`. NULLIF(col::text, '') is correct on BOTH types, so this lane
    # survives the migration untouched. Measured equivalent on api_keys
    # (106 of 154 blank): both forms return 48.
    src = _read(os.path.join("routes", "fix_closure_master_shell.py"))
    assert "NULLIF(created_at::text, '')::timestamptz" in src
    assert "NULLIF(last_used_at::text, '')::timestamptz" in src
    assert "NULLIF(created_at, '')::timestamptz" not in src, \
        "the pre-migration form is back — it raises once the column is timestamptz"


# ── paywall probe contract ───────────────────────────────────────────

def test_paywall_probe_is_canary_scoped_and_failsoft(shell, monkeypatch):
    monkeypatch.delenv("PAYWALL_CANARY_KEY", raising=False)
    checks = shell._lane_paywall_contract()
    assert checks[0]["pass"] is None, \
        "no canary key must render '?', never a green"
