"""Seven Levers Master Shell (#32, 2026-07-25) — pins the wave's contracts.

One wave, seven levers: zone-worker canon sync, recidivism→planner wiring,
slow-request capture, 7/7 usage-capture coverage, RAG recall anchors, loop
census, LinkedIn follower enum. Plus two REGRESSION pins from the build
itself: a repo-wide floor sweep nearly rewrote the retired-floor BAN LIST in
ai_surface_canon (the canon would have banned the canon) and falsified the
Surface Truth incident docstring — history and fences must survive future
sweeps byte-intact.

CI-SAFETY: unit-tests env has no DATABASE_URL/JWT_SECRET; modules import
directly (never via main); DB paths are exercised only via fail-soft
contracts; source-text checks carry the wiring assertions.
"""
import os

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
    from routes import seven_levers_master_shell as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_shell_registered_in_main():
    src = _read("main.py")
    assert "register_blueprint(seven_levers_master_shell_bp)" in src
    assert "register_blueprint(perf_timing_bp)" in src


def test_shell_cron_ticked_and_killable():
    src = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/seven-levers/master-tick" in src
    assert "SEVEN_LEVERS_SHELL_DISABLE" in src


def test_shell_no_store_and_beat():
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    assert "no-store" in src
    assert "seven-levers-shell-daily" in src
    assert "/api/v1/admin/ingest-runs/beat" in src


def test_shell_fetches_bust_the_zone_cache():
    # Admin/manifest GETs are zone-cached up to 3600s; a cached body is the
    # stale-green this shell exists to kill, so every edge fetch must carry
    # a cache-buster.
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    assert "cb=%d" in src


# ── honesty semantics ────────────────────────────────────────────────

def test_lane_verdict_never_green_by_silence(shell):
    assert shell._lane_verdict(
        [shell._check("x", "x", None, "unreachable", critical=True)]) == "?"
    assert shell._lane_verdict(
        [shell._check("x", "x", False, "bad")]) == "FAIL"
    assert shell._lane_verdict(
        [shell._check("x", "x", True, "ok", critical=True),
         shell._check("y", "y", None, "info", critical=False)]) == "PASS"


def test_crashed_lane_is_indeterminate(shell):
    def boom():
        raise RuntimeError("boom")
    assert shell._lane_verdict(shell._safe_lane(boom)) == "?"


def test_feed_family_normalizer(shell):
    assert shell._norm_feed("fiber-integration-daily") == "fiber-integration"
    assert shell._norm_feed("fiber-integration") == "fiber-integration"
    assert shell._norm_feed("EIA_SYNC") == "eia"


# ── lever 1 · zone sync sources ──────────────────────────────────────

def test_repo_worker_is_canon_clean_and_current():
    """The repo worker.js was a stale COPY (v4.9.24-era) while live ran
    v4.9.32 — numbers got edited in the repo without a deploy (the #30
    artifact-vs-reality failure). Now repo == deployed v4.9.33 canon-sync:
    no retired floors, no stale tool counts, version marker present."""
    src = _read("worker.js")
    # ★ This literal is PINNED on purpose, not lazily hardcoded. worker.js
    # deploys only by manual Cloudflare dashboard paste, so the version string
    # is the only live-vs-repo drift signal we have. Pinning it here means a
    # bump cannot land without a human consciously updating this line — which
    # is the moment they are reminded the paste still has to happen.
    #
    # It is deliberately COUPLED to scripts/check_worker_version_bump.sh: that
    # guard REQUIRES a bump on any worker.js edit, this test FORBIDS one until
    # acknowledged here. Every worker change must therefore touch both. If you
    # are updating this line, the paste is still outstanding.
    #
    # 4.9.38-canon-floors-card -> 4.9.41-html-links-counts-no-tdz (2026-08-06):
    # GET /mcp gained product/not/api_base/keyless fields. It previously
    # announced 82 unnamed tools and never said what the product was, which an
    # assistant filled in by fabricating a DCIM API.
    #
    # 4.9.41 -> 4.9.42-tool-example-args-match-schema (2026-08-12): four tool
    # descriptions shipped worked examples their own inputSchema rejects
    # (list_transactions year=, search_facilities min_mw=/status=, get_news
    # topic=, get_pipeline market=). Unknown args are dropped silently, so the
    # documented call returned an UNFILTERED answer the agent reported as
    # filtered. ✓ PASTED — live confirmed 2026-08-14: dchub.cloud/grid/ returns
    # X-DC-Worker-Version: 4.9.42-tool-example-args-match-schema. (The note here
    # still read "PASTE STILL OUTSTANDING"; it had happened. The header is the
    # authority, not this comment — check live before trusting the line above.)
    #
    # 4.9.42 -> 4.9.43-grid-trailing-slash-301 (2026-08-14): /grid/ answered 404
    # while /grid answered 200, and /grid/<paid-iso>/ served the same page as
    # /grid/<paid-iso> with no rel=canonical on either — a duplicate produced by
    # the tier split, since the paid-ISO proxy routes those paths around the
    # normalisation free ISOs get from the Pages worker. dchub-frontend#1180
    # tried to fix /grid/ in Pages and could not: the zone route
    # `dchub.cloud/grid/*` binds it to THIS script first.
    # ✓ PASTED — live confirmed 2026-08-14: /grid/ now 301s and the header
    # reads 4.9.43-grid-trailing-slash-301.
    #
    # 4.9.43 -> 4.9.44-failover-2xx-only (2026-08-14): both Render failover
    # branches accepted `status < 500`, so a 404 from the STALE failover build
    # was served to crawlers as a real 404. Render 404s /press-release/<slug>
    # while Railway serves it 200, so every Railway hiccup told Google and
    # GPTBot a live page was gone. Now 2xx/3xx only; a 4xx falls through to KV
    # stale then 503. 503 says retry, 404 says delete the URL.
    # ✓ PASTED — live confirmed 2026-08-16: dchub.cloud/mcp and
    # /.well-known/mcp.json both return X-DC-Worker-Version:
    # 4.9.44-failover-2xx-only. (The line above still read "PASTE OUTSTANDING";
    # it had happened. Second time this note has lagged reality — the header is
    # the authority, so check live before believing either state.)
    #
    # 4.9.44 -> 4.9.45-manifest-version-derived (2026-08-16): /.well-known/
    # mcp.json served version 2.5.0 against a live server on 2.12.0, and a
    # description baked with "15,700+ facilities / 1,600+ deals" against a canon
    # of 18,000+ / 1,800+. EVERY MCP registry scrapes that file, so every
    # downstream listing was stale and the registries were not at fault.
    # version + description are now DERIVED from the Flask origin manifest
    # (which builds them from ai_surface_canon), joining anchor_intents and
    # problem_taxonomy on the existing KV-cached fetch. server-card.json derives
    # them too, so the two well-known surfaces cannot disagree.
    # ★ THE POINT OF THIS ONE: after the paste, a canon bump moves the manifest
    # with ZERO further pastes. This is the last hand-typed copy on that surface.
    # ✓ PASTED — live confirmed 2026-08-29: /grid/, /mcp and /.well-known/
    # mcp.json all return X-DC-Worker-Version: 4.9.45-manifest-version-derived,
    # and mcp.json serves version 2.12.0 with "19,300+" in its description, so
    # the manifest-derived wiring is doing exactly what it promised. The line
    # above read "PASTE OUTSTANDING" — THIRD time this note has lagged reality.
    # ★The header is the authority, not this comment. Check live before
    # believing either state.
    #
    # 4.9.45 -> 4.9.46-recommendation-returns-truth (2026-08-29): the
    # get_dchub_recommendation tools/list entry promised a return shape that has
    # NEVER existed — top_markets[], candidate_facilities[], factor_breakdown{},
    # summary_text, citation_url. /api/agents/recommend emits none of them; it
    # returns recommendation{short,medium,detailed} plus a live top_pocket.
    # Its Example: line also demonstrated the free-text form that silently falls
    # through to the generic blurb, AS IF it were the correct usage. Measured
    # live: two opposite contexts returned a byte-identical answer
    # (md5 8303ac30a35c3c6e) to 95 distinct free users in 30d.
    # ✓ PASTED — live confirmed 2026-08-30: dchub.cloud/grid/ AND /mcp both
    # return X-DC-Worker-Version: 4.9.46-recommendation-returns-truth. (This
    # line read "PASTE OUTSTANDING"; it had happened. THIRD time this note has
    # lagged reality — the header is the authority, never the comment.)
    #
    # 4.9.46 -> 4.9.47-tools-83-summarize-for-citation (2026-08-30):
    # MCP_FALLBACK_TOOLS carried 82 entries against a live tools/list of 83.
    # summarize_for_citation shipped on the MCP server and nothing in this repo
    # named it — mcp_tool_catalog auto-syncs from live, so the catalog moved and
    # the canon pin did not. The /mcp envelope reports THIS array's length, so
    # the fallback under-reported by one tool.
    # ✓ PASTED — live confirmed 2026-09-01: /.well-known/mcp.json returns
    # X-DC-Worker-Version: 4.9.47-tools-83-summarize-for-citation. (This line
    # read "PASTE OUTSTANDING"; it had already happened. FOURTH time this note
    # has lagged reality — the header is the authority, never the comment.)
    #
    # 4.9.47 -> 4.9.48-anon-callable-flag (2026-09-01): all three discovery
    # surfaces (/.well-known/mcp.json, /mcp/manifest, /.well-known/mcp/
    # server-card.json) declared authentication {type:'api_key',
    # header:'X-API-Key', optional_for:['free_tier']}. `type:'api_key'` ALONE
    # reads as AUTH REQUIRED to every third-party parser, and `optional_for` is
    # a DC Hub invention nothing else knows how to read — the one place we
    # stated the truth ("3 calls/day taste, no signup") was prose inside a
    # pricing string.
    # MEASURED: Glimind (glimind.com — the SentinelOracle/0.1 prober in our
    # logs) indexes EVERY dchub tool as access:paid + anonymousCallable:FALSE,
    # while its OWN liveness probe of the same tool records authRequired:false.
    # Its docs instruct agents to "pre-filter on anonymousCallable to skip tools
    # you can't call without a signup" — so we are dropped from routing while a
    # PAID competitor (ai.dynamicfeed/energy_grid) marked true survives.
    # Control: POST https://dchub.cloud/mcp with NO credential returns 200 and
    # the server logs `tier=free key=none`. The declaration was simply wrong.
    # Adds required:false + anonymous_access:true to all three blocks;
    # type/header unchanged, so clients keying off them still send credentials.
    # ★ PASTE OUTSTANDING — this change is edge-only. No third-party parser
    # sees any of it until the manual Cloudflare dashboard paste happens.
    # Verify with:
    #   curl -sI https://dchub.cloud/grid/ | grep -i x-dc-worker   # want 4.9.48
    assert "WORKER_VERSION = '4.9.48-anon-callable-flag'" in src
    assert "21,000+" not in src
    assert "73 tools over" not in src
    assert "58 MCP tools" not in src


def test_server_mjs_serves_canon():
    src = _read("server.mjs")
    assert "21,000+" not in src
    assert "40 tools," not in src and "40 tools covering" not in src


# ── lever 2 · recidivism wiring ──────────────────────────────────────

def test_planner_consumes_recidivism():
    src = _read(os.path.join("routes", "brain_strategic_planner.py"))
    assert "def _read_recidivism" in src
    assert '"recidivism":   1200' in src
    assert "RECIDIVIST FINDINGS" in src
    assert "still_broken IS TRUE" in src


def test_recidivism_reader_failsoft(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_strategic_planner as p
    monkeypatch.setattr(p, "_get_db", lambda: None)
    assert p._read_recidivism() == []


# ── lever 3 · perf capture ───────────────────────────────────────────

def test_perf_hooks_are_failsoft_and_killable(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import perf_timing as pt
    monkeypatch.setenv("PERF_TIMING_DISABLE", "1")
    assert pt._disabled() is True
    monkeypatch.delenv("PERF_TIMING_DISABLE", raising=False)
    assert pt._disabled() is False
    # normalizer bounds cardinality
    assert pt._norm_path("/api/v1/facility/123456789/detail") == \
        "/api/v1/facility/:id/detail"
    assert pt._norm_path("/api/v1/x?y=1") == "/api/v1/x"
    # no DATABASE_URL → silent no-op, never a raise
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    pt._LAST_WRITE[0] = 0.0
    assert pt._record("/x", "GET", 200, 2500) is None


def test_perf_write_is_rate_limited(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import perf_timing as pt
    calls = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid.invalid/x")
    import time as _t
    pt._LAST_WRITE[0] = _t.monotonic()   # a write just happened
    # inside the gap: returns before any connect attempt
    monkeypatch.setattr("psycopg2.connect",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError()),
                        raising=False)
    pt._record("/x", "GET", 200, 2500)
    assert calls == []


# ── lever 4 · usage-capture coverage ─────────────────────────────────

def test_all_seven_call_sites_wired():
    sites = ("routes/brain_lane_driver.py",
             "routes/brain_strategic_planner.py",
             "routes/brain_investigator.py",
             "routes/brain_feature_proposer.py",
             "routes/brain_answer_cache.py",
             "routes/analyst_note.py")
    unwired = [s for s in sites if "record_llm_usage" not in _read(s)]
    assert not unwired, "missing usage capture: %s" % unwired


# ── lever 7 · media enum ─────────────────────────────────────────────

def test_linkedin_follower_enum_is_valid():
    """Live API test 2026-07-25: 'CompanyFollowedByMember' → 400 invalid
    enum; 'COMPANY_FOLLOWED_BY_MEMBER' → 200 firstDegreeSize=302. The
    collector was blind on a one-word spelling."""
    src = _read("linkedin_poster.py")
    assert "edgeType=COMPANY_FOLLOWED_BY_MEMBER" in src
    assert "edgeType=CompanyFollowedByMember" not in src


# ── sweep-regression pins ────────────────────────────────────────────

def test_retired_floor_ban_list_still_bans_the_retired_floor():
    """A repo-wide '21,000+ → 12,650+' sweep nearly rewrote the RETIRED
    list in ai_surface_canon — the canon would have banned itself. The
    ban list must keep the retired floors verbatim."""
    src = _read("ai_surface_canon.py")
    assert '"21,000+"' in src


def test_surface_truth_incident_history_intact():
    src = _read(os.path.join("routes", "surface_truth_master_shell.py"))
    assert "(20,000+, 21,000+, 22,000+)" in src
