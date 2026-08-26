"""
ai_surface_canon.py — THE single source of truth for AI-agent-facing surfaces.
==============================================================================

Every AI-agent surface (llms.txt, the .well-known manifests, AGENTS.md,
integration configs, /connect, /ai, robots.txt, the registry) currently
hand-types the same numbers, so they drift + contradict each other (v2.1.22 vs
2.3.3 vs 2.1.0; "24 tools" and "48 tools" on the SAME page; 232 vs 300+ markets;
an inflated five-figure facility claim on /connect). This module is the fix: one
canon, with the MOVING numbers resolved LIVE at read time so it never goes stale.

Used by ai_surface_sentinel.py to audit (and later auto-refresh) every surface.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.request

_BASE = os.environ.get("DCHUB_BACKEND_BASE",
                       "https://dchub-backend-production.up.railway.app")

# The tools/list count MUST be probed from the PUBLIC MCP gate agents actually
# connect to — the Node server.mjs at dchub.cloud/mcp (79 tools) — NOT the Flask
# backend's own /mcp (a different/legacy surface that returns 0 to an external
# tools/list, so the "live" override below silently failed and resolve_canon
# fell back to the hand-maintained PINNED count, which lagged 74 vs live 79).
_MCP_BASE = os.environ.get("DCHUB_MCP_PUBLIC_BASE", "https://dchub.cloud")

# ── Pinned structural canon (changes rarely; edit HERE, nowhere else) ──
PINNED = {
    "version": "2.12.0",                      # == repo canonical (server.mjs/server.json); registry mirror auto-bumps to latest+1. ★2026-08-16: 2.11.1 -> 2.12.0. Probed the live `initialize` serverInfo handshake per the rule below — {"name":"DC Hub Intelligence","version":"2.12.0"} — and it matches mcp-server origin/main server.json 2.12.0. Note /mcp/health STILL echoed 2.5.0 during this probe, which is exactly the CF-synthesized closed loop the 08-08 note warns about; had I trusted it the canon would have gone BACKWARDS. ★2026-08-08 canon-surface audit: 2.5.0 -> 2.11.1. The 2.5.0 pin was probed from /mcp/health + /.well-known/mcp.json — both CF-SYNTHESIZED surfaces that echo THIS value back (a closed loop, ref reference_dchub_mcp_health_topology), so it sat 6 minor versions behind. Re-derived from the real live `initialize` serverInfo.version handshake (2.11.1, verified 2026-08-08) which matches mcp-server origin/main server.json 2.11.1 — NEVER re-copy from /mcp/health or mcp.json. ★2026-07-31: was "2.4.4" — live-probed 2.5.0 at BOTH dchub.cloud/mcp/health and /.well-known/mcp.json, and worker.js MCP_SERVER_INFO.version already carried 2.5.0, so the canon was the only stale copy. That mattered twice over: ai_surface_sentinel.py flags a served manifest whose version != canon["version"] at severity "high", so a stale canon turns every honest surface into a false-positive drift alert (and hides the real one); and #2066 wires main.py's /api/v1/mcp/platforms server_version to THIS key, so pinning to a stale value would only have relocated the bug. Probe before bumping — do not copy from another repo file.
    "tools_advertised": 82,                   # canonical advertised count == live tools/list (82 as of 2026-07-31: +get_power_availability_timeline, gateway v2.10.0; 81 as of 2026-07-29: +get_hosting_capacity; 80 as of 2026-07-26: +execute_plan; 79 as of 2026-07-20: +get_global_power/get_permitting_intel/plan_query/research_task/simulate_scenario/standing_intent). PINNED fallback; resolve_canon() overrides it with the live count probed from _MCP_BASE (the public gate) so consumers never go stale. /AGENTS.md reads PINNED directly, so keep current. ★MUST equal len(tool_manifest) — tests/test_fix_closure_shell.py asserts it, so a count edit that skips the manifest cannot land.
    "mcp_endpoint": "https://dchub.cloud/mcp",
    "registry_id": "cloud.dchub/mcp-server",
    "rest_base": "https://dchub.cloud/api/v1",     # canonical host (NOT api.dchub.cloud)
    "free_tier_calls_per_day": 10,                 # NOT 100
    # ★2026-07-31 — the EMAIL-BOUND free quota, the other half of the free
    # funnel. Pinned for the same reason free_tier_calls_per_day is: every lane
    # agrees on 50 — TIER_LIMITS['identified'] carries rate_limit=50 (REST) AND
    # mcp_daily=50 (MCP), the edge worker's MCP_TIERS.identified.daily_limit is
    # 50, _canonical_pricing()['identified']['calls_per_day'] is 50, and the
    # live bind_email_required gate already tells agents "keeps working FREE (50
    # calls/day)". Surfaces MUST have this number available, because without it
    # the /mcp landing page said a *free key* granted 1k/day — a free key is
    # `free` (10/day, identical to anonymous); only binding an email reaches 50.
    # Do NOT pin a `developer` sibling here: that tier's lanes DISAGREE
    # (rate_limit 1,000 vs mcp_daily 500), so there is no single honest number.
    "identified_calls_per_day": 50,
    # ── MONTHLY quota canon (monthly-quota phase 2, 2026-08-06) ───────────
    # Billing is moving from per-day caps to per-month quotas (starter
    # "200/day" -> 6,000/month). Every monthly number here is
    # TIER_LIMITS[tier]['mcp_daily'] x 30 — the SAME arithmetic
    # monthly_quota.monthly_quota_for() enforces — so served copy and the
    # gate can never quote different numbers. resolve_canon() recomputes
    # them live from tier_registry, so a repriced tier heals itself.
    #
    # ★ The per-day keys above STAY and are NOT deprecated. free and
    # identified are still gated per DAY, and those gates are live
    # (routes/auto_trial.py's daily cap; mcp_gatekeeper's day window), so
    # "10 calls/day" remains the honest free-tier number. It is the PAID
    # tiers whose real ceiling is monthly — their per-day caps were never
    # enforced on the /mcp path (verified 2026-07-30), which is precisely
    # why the monthly quota is being built. So: quote paid tiers MONTHLY,
    # quote free/identified DAILY. Rewriting free copy to "300/month"
    # would advertise a ceiling the free gate does not actually grant.
    "starter_calls_per_month":   6000,
    "developer_calls_per_month": 15000,
    "pro_calls_per_month":       60000,
    "quota_period_note": ("paid tiers (starter/developer/pro) are quoted and "
                          "enforced per MONTH; free and identified stay per DAY"),
    "platforms": ["Claude", "ChatGPT", "Gemini", "Perplexity", "Copilot", "Meta AI", "Grok"],
    # ★2026-07-29 (shell #41 WS5) — the COMPLETE advertised tool set, the
    # membership anchor for the worker.js fallback manifest. Sorted; order is
    # cosmetic (comparisons are set-based).
    #
    # Why this exists as well as tools_advertised: a COUNT check goes green on
    # a doubly-wrong manifest. During this wave worker.js briefly carried 81
    # entries against a live 81 while get_hosting_capacity was MISSING and a
    # different name was EXTRA — the counts matched, the surface was wrong, and
    # the guard passed. get_hosting_capacity had in fact been absent from the
    # fallback list the whole time (live 81 / repo 80) and only the arithmetic
    # ever got compared. Membership is the real invariant; keep this list and
    # tools_advertised moving together.
    #
    # NOT the same list as tool_names below — that is a curated 14-name
    # FLAGSHIP subset keyed to TOOL_RETURNS and rendered into llms.txt.
    #
    # ★find_sites_by_infrastructure is deliberately NOT here: it is not live.
    # Adding it to worker.js is the exact swap this list exists to reject.
    "tool_manifest": [
        "ai_capacity_index", "analyze_parcel", "analyze_site",
        "bind_email", "claim_free_key", "cluster_sites_by_latency",
        "compare_isos", "compare_sites", "deal_autopsy",
        "discover_tools", "execute_plan", "export_dataset", "fetch",
        "find_alternatives", "generate_site_analysis",
        "get_agent_registry", "get_backup_status", "get_changes",
        "get_climate_intel", "get_composite_site_score",
        "get_dchub_recommendation", "get_disaster_risk",
        "get_energy_prices", "get_facility", "get_facility_risk_delta",
        "get_fiber_intel", "get_fiber_readiness", "get_gas_economics",
        "get_gas_index", "get_gas_intelligence", "get_global_power",
        "get_grid_data", "get_grid_intelligence", "get_grid_scoreboard",
        "get_hosting_capacity", "get_infrastructure",
        "get_intelligence_index", "get_interconnection_queue",
        "get_iso_context", "get_market_context", "get_market_dcpi_rank",
        "get_market_intel", "get_metro_fiber", "get_news",
        "get_permitting_intel", "get_pipeline",
        "get_power_availability_timeline", "get_power_pipeline",
        "get_refined_queue", "get_renewable_energy",
        "get_retirement_headroom", "get_shortlist",
        "get_tax_incentives", "get_water_risk", "grid_transition_radar",
        "hyperscaler_deals", "list_saved_sites", "list_transactions",
        "plan_fiber_leadin", "plan_query", "predict_market_trajectory",
        "rank_markets", "rank_sites", "recover_my_key", "research_task",
        "save_site", "save_to_shortlist", "score_facility", "search",
        "search_facilities", "search_intelligence", "semantic_search",
        "set_market_alert", "set_shortlist_alert", "set_site_alert",
        "simulate_scenario", "site_selection_canvas", "standing_intent",
        "subscribe_digest", "suggest_reallocation", "unlock_more_data",
        "why_dchub"
    ],
    "tool_names": [
        "search_facilities", "get_facility", "get_market_intel", "rank_markets",
        "get_grid_intelligence", "get_interconnection_queue",
        # ★2026-07-29: promoted to flagship with the 81st tool. Distribution-
        # FEEDER truth (utility-published) is a different layer from the
        # transmission/queue tools either side of it — agents that only see the
        # queue tools ask for time-to-power and never learn a named feeder's
        # published headroom. Coverage is 18 utilities, NOT nationwide.
        "get_hosting_capacity", "get_fiber_intel",
        "get_gas_intelligence", "list_transactions", "hyperscaler_deals",
        "analyze_site", "compare_sites", "score_facility", "get_news",
    ],
    "fake_tool_denylist": ["get_market_data", "search_deals", "get_transactions"],
    "crawlers_required": ["GrokBot", "xAI-Grok", "Grok-DeepSearch"],
    "public": {                                    # public-facing rounded strings
        # ★2026-07-24 DEDUP REBASE: was "22,000+", which floored RAW discovered_facilities
        # ROWS. A customer audit (Landry, 07-23) found cross-source duplicates — the same
        # physical site listed by multiple providers — so we shipped conservative entity
        # resolution and now publish DISTINCT SITES (live 12,687; AU 616 -> 322). The old
        # floor silently became a ~1.7x over-claim the moment dedup landed, and every
        # downstream consumer (registry submitters, description builders, white-glove
        # propagation) kept pasting it. Meta, Microsoft and Grok have all standardised on
        # this deduped basis — keep them in sync.
        # ★2026-07-28: 12,650+ -> 15,000+. This is the DB-DOWN fallback only —
        # resolve_canon() now overrides it live from
        # canonical_stats.facilities_verified_phrase(). It froze at 12,650+ while
        # the real deduped fleet reached 15,207, and because nothing overrode it
        # this stale floor WAS the public number on every surface.
        # ★A floor must stay <= reality (floors round DOWN, never up): 15,000 is
        # safely under 15,207. Re-floor DOWNWARD if the verified count ever
        # falls below it — an over-stated floor is the
        # canonical_floor_above_live_reality failure this module exists to stop.
        # ★2026-08-01: 15,000+ -> 15,700+ (live facilities_distinct = 15,792).
        # This is the DB-DOWN fallback, but it is NOT harmless when stale:
        # surfaces that render from PINNED directly (routes/agent_concierge.py,
        # enhanced_promotion.py) never see the resolve_canon() override, so this
        # literal WAS the number served on /agent — 82 tools beside a two-floors-
        # old "15,000+". resolve_canon() self-heals the callers that use it; this
        # value must track canon for the ones that don't.
        # ★Floor stays <= reality (round DOWN, never up): 15,700 < 15,792.
        # ★2026-08-08 canon-surface audit: 15,700+ -> 17,000+. resolve_canon()
        #  already served "17,000+" (live facilities_distinct = 17,130) while
        #  /agent, /AGENTS.md and the agent_concierge recipes — which read PINNED
        #  DIRECTLY, not resolve_canon() — still served 15,700+. 17,000 < 17,130.
        # ★2026-08-16: 17,000+ -> 18,000+. SAME lag, one cycle later: resolve_canon()
        #  (i.e. /api/v1/canon/phrases) already served "18,000+" while this PINNED
        #  floor still said 17,000+, so /.well-known/mcp.json — which builds its
        #  description from _canon_text -> PINNED, not resolve_canon() — published
        #  the stale figure to every MCP registry that scrapes it. Probed live:
        #  /api/v1/stats facilities = 18,073. Floor rounds DOWN: 18,000 < 18,073.
        # ★2026-08-18: 18,000+ -> 18,300+. THIRD consecutive cycle of the exact
        #  same lag, which is the point worth recording: this is not an incident,
        #  it is the steady state. resolve_canon() self-heals, PINNED does not, so
        #  every fortnight the PINNED-only surfaces (/.well-known/mcp.json, /agent,
        #  /AGENTS.md, the agent_concierge recipes) fall one floor behind and have
        #  to be walked forward by hand. registry_listing_staleness already had a
        #  row for it — `dchub:well-known`, fault='OURS', "claims 18,000 facilities,
        #  canon floor 18100" — i.e. the drift was DETECTED and sat open, because
        #  detection is not derivation. Probed live 2026-08-18:
        #    /api/v1/canon/phrases  facilities = "18,300+"   <- the SoT, stable
        #                                                       across cache-busted reads
        #    /api/v1/stats          facilities = 18,406
        #    /api/v1/stats          _facility_count_notes.discovered_verified = 18,455
        #  Pinned to the value resolve_canon() actually publishes (18,300+), NOT to
        #  floor(18,455) = 18,400: PINNED is the fallback for surfaces that cannot
        #  reach the resolver, so it must never quote a HIGHER floor than the
        #  resolver itself, or the DB-down path over-claims against the live path.
        #  Floor rounds DOWN and stays <= reality on every count: 18,300 < 18,406.
        # ★2026-08-19: 18,300+ -> 18,400+. FOURTH consecutive cycle, and the first
        #  where the lag was caught from the OUTSIDE rather than by a sweep: an
        #  agent-partner readout put three of our own numbers side by side and they
        #  disagreed — llms.txt "18,300+" (this PINNED value, served live), the
        #  homepage/README "18,400+" (canon_phrases.json, the mcp-server snapshot
        #  of the resolver) and 404.html "18,000+" (the floor before last). A
        #  partner quoting us would have cited whichever surface they happened to
        #  land on. Probed live 2026-08-19, all cache-busted:
        #    /api/v1/canon/phrases  facilities = "18,400+"  source=resolve_canon (live)
        #    /api/v1/stats          facilities = 18,497
        #    dchub-mcp-server canonical/canon_phrases.json = "18,400+"
        #      (retrieved_at 2026-08-18T13:55Z — the resolver had already moved)
        #  So the never-quote-higher-than-the-resolver invariant is SATISFIED at
        #  18,400+: the resolver publishes exactly that today. Floor rounds DOWN
        #  and stays <= reality: 18,400 < 18,497.
        #  ★The steady state noted at 08-18 has not changed — resolve_canon()
        #  self-heals and PINNED does not — so this is the fourth hand-walk. The
        #  derivation fix (PINNED reads the resolver's last-known-good instead of
        #  a literal) is the thing that ends the cycle; this commit only closes
        #  the current gap, deliberately, and does not pretend otherwise.
        # ★2026-08-21: 18,400+ -> 18,500+. FIFTH consecutive cycle. Probed live,
        #  cache-busted: /api/v1/canon/phrases facilities = "18,500+" (source=
        #  resolve_canon live), /api/v1/stats facilities = 18,581, and the
        #  mcp-server snapshot canonical/canon_phrases.json = "18,500+" — while
        #  this PINNED value fed "18,400+" into /llms.txt, /agent, /connect,
        #  /api/v1/ai-agents.json and /.well-known/mcp.json (all backend-served
        #  off PINNED). Never-higher-than-the-resolver holds: 18,500 == resolver,
        #  < 18,581. The derivation fix (PINNED reads the resolver's last-known-
        #  good) is still the thing that ends this; this is the fifth hand-walk.
        "facilities": "18,500+",
        # ★2026-07-29: was the exact literal "311", which had itself drifted ABOVE
        # live canon (306 today — canonical_stats.py:165-167, surfaced as
        # /api/v1/stats top-level `markets`), making this a +5 over-claim on every
        # surface that reads PINNED without calling resolve_canon(). A pinned EXACT
        # count re-drifts every time the market set moves and over-claims the moment
        # it shrinks, so pin the FLOOR instead: "300+" is the form
        # tests/test_honest_numbers.py:151 documents as sanctioned, matches
        # canonical_stats.markets_phrase(), and can never exceed reality.
        # routes/mcp_honest_numbers._floor() parses it to 300 exactly as it already
        # does for the sibling "15,000+"/"1,500+"/"170+" floors, so every downstream
        # consumer keeps working. resolve_canon() overrides it live below, the same
        # way it already does for `deals`.
        "markets": "300+",
        # ★2026-08-01: 1,500+ -> 1,600+ (live deals_tracked = 1,662). Same
        # PINNED-vs-resolve_canon() split as `facilities` above: /agent served
        # "1,500+" while /api/v1/canon/phrases already served "1,600+".
        "deals": "1,900+",   # ★2026-08-23: 1,800+ -> 1,900+ — and this bump is the FIRST one the ledger asked for instead of a hand-walk. Claim 100974 (canon:public.deals, expected "== 1,800+", measured against the live resolve_canon() override) was judged **refuted** at 05:51Z; 100976 carries the same frozen expectation to its 24h horizon. Probed live at 06:57Z: /api/v1/canon/phrases deals = "1,900+", /api/v1/stats deals = 1,931. Floor rounds DOWN and never above the resolver: 1,900 < 1,931. ★The literal ALSO had a second home (mcp_gateway.py data_coverage.deals_tracked fell back to a hardcoded "1,800+" while its siblings called canon_text) — that copy now reads {canon_deals}, so this line is the only place the floor is typed. ★2026-08-16: 1,700+ -> 1,800+, same PINNED-vs-resolve_canon lag as `facilities` above, one cycle later — /api/v1/canon/phrases already served 1,800+ while this floor fed the stale figure into /.well-known/mcp.json. Probed live: /api/v1/stats deals = 1,849. Floor rounds DOWN: 1,800 < 1,849. ★2026-08-08 canon-surface audit: 1,600+ -> 1,700+ (resolve_canon live = 1,700+, deals_tracked = 1,745; same PINNED-vs-resolve_canon lag as `facilities`). ★2026-07-24: live distinct = 1,553, floor raised 1,400 -> 1,500. DISTINCT tracked deals (== canonical_stats.deals_phrase). ★2026-07-17: was "4,000+", itself an over-claim — it floored ROWS, and the AUTO id embeds the ingest date so one deal accrues a row per day (4,275 rows -> ~1,420 distinct). ★NOT the raw `deals` COUNT(*) that /api/v1/stats returns. resolve_canon() overrides this live.
        # ★2026-08-01 NEW KEY. The mapped-asset total was the one headline
        # figure with NO pinned home, so it drifted unchecked: worker.js's
        # why_dchub blurb and the /faq page both still claim "500,000+" while
        # the live MCP instructions blob was corrected to 320,000+ (126k
        # substations + 94k transmission + 55k fiber + 30k gas + 13k plants +
        # 690 subsea + 1.9k landings). An unpinned number cannot be swept,
        # cannot be sentinel-checked, and cannot be healed — pin it.
        "assets": "320,000+",
        "countries": "170+",  # ★2026-07-30 VERIFIED correct: the deduped fleet spans 178 distinct codes (incl. territories) → floor "170+". NOT "180+": /api/v1/stats served countries=186 off the legacy `facilities` table, which double-counts 9 full-name/ISO-code pairs ("USA"+"US"). resolve_canon() now overrides this live (countries_verified_phrase).
    },
    # Values known to be STALE/WRONG on some surface — the sentinel flags these.
    # NB: DeepSeek/Mistral were REMOVED (2026-07-01) — they're legitimate
    # available integrations (DC Hub ships /integrations/mistral/ etc.), so
    # listing them isn't "wrong"; the blunt denylist caused false positives on
    # /ai + llms.txt. "platforms" (the verified-active 7) stays the canon for
    # the ACTIVE roll-call; availability is a broader, valid claim.
    # ★2026-07-17: the "4,000+" deal claims are stale AND an over-claim — they
    # counted duplicate rows (see canonical_stats.deals_phrase). Scrub them.
    "stale_markers": [
                      # ★2026-07-24: the PRE-DEDUP facility floors. These were TRUE
                      # until entity-resolution shipped, then instantly became a
                      # ~1.7x over-claim (raw rows vs distinct sites). Grok caught
                      # them on our own pages before any detector did — scrub on sight.
                      "21,000+", "21,900+", "22,000+", "21k+",
                      # ★2026-07-30: the 07-24..07-28 floor "12,650+" is itself
                      # retired (PINNED rebased to 15,000+, live 15,300+). It sat
                      # on the /ai hero CONTRADICTING the same page's live stat
                      # card, plus ~200 files across both repos — swept 07-30.
                      # Scrub on sight; the zone worker + mcp-server tool
                      # descriptions still carry it until their own deploys.
                      "12,650+",
                      "10,706", "10706", "50,000+", "50000", "317 ", "332 ",
                      "232 ", "100 calls/day", "3,000+ M&A",
                      "2,000+ M&A", "2,000+ tracked deals", "2,000+ deals",
                      "2,000+ tracked M&A", "2,000+ tracked transactions",
                      "4,000+ M&A", "4,000+ tracked deals", "4,000+ deals",
                      "4,000+ tracked M&A", "4,000+ tracked transactions",
                      # ★2026-08-01: the mapped-asset OVER-claim. "50,000+" was
                      # already listed; "500,000+" (10x it) never was, so the
                      # figure survived on worker.js's why_dchub blurb and /faq
                      # after the live MCP blob was corrected to 320,000+.
                      "500,000+",
                      # ★2026-08-01: retired deal floors. 1,400+ is now two
                      # floors stale (live distinct 1,662) and was still on the
                      # /mcp server card. Scoped to "tracked" so bare "1,400+"
                      # in unrelated code (route miles, MW) can't false-positive.
                      "1,400+ tracked",
                      # ★2026-08-01: the exact market literal retired from PINNED
                      # on 07-29 for over-claiming (+4 vs live 307). Still
                      # hardcoded in public_endpoints.py + enhanced_promotion.py.
                      # Scoped to "311 markets" — bare "311" collides with IDs.
                      "311 markets",
                      "24 tools", "48 tools", "49 tools", "51 tools", "53 tools",
                      # ★2026-07-31: "81 tools" retired (live 82,
                      # +get_power_availability_timeline). Non-headline surfaces
                      # went count-free in the same sweep — bare figures freeze.
                      "58 tools", "72 tools", "81 tools",
                      # ★2026-07-31: "2.4.4" retired alongside the bump to
                      # 2.5.0 above. Retiring the OUTGOING value is what makes
                      # a surface still serving it detectable — the list is a
                      # denylist of superseded versions, not a changelog, so a
                      # bump that skips this line leaves the previous canon
                      # invisible to the sentinel.
                      # ★2026-08-08: "2.5.0" retired alongside the bump to
                      # 2.11.1 above — a surface still serving 2.5.0 (e.g. the
                      # CF worker card, SH52-032) is now detectable by the sentinel.
                      # ★2026-08-16: "2.11.1" retired alongside the bump to
                      # 2.12.0 above. "2.3.3" was ALREADY on this list and the
                      # origin manifest was serving it anyway — the denylist
                      # detects, it does not fix. main.py now derives the value
                      # from this canon instead of carrying its own literal.
                      "2.1.22", "2.3.3", "2.1.0", "2.4.3", "2.4.4", "2.5.0",
                      "2.11.1"],
    # ★2026-08-22: WITHDRAWN-CAPABILITY markers (regex, case-insensitive). The
    # literal stale_markers above catch retired NUMBERS; nothing caught a
    # retired CAPABILITY still advertised as live. On 2026-08-08 the DCGI (gas
    # index) was withdrawn, yet listings kept selling "the DCGI: per-state
    # natural-gas suitability" for two weeks and the number-only drift detector
    # never flagged it (it had to be hand-found). These patterns fire when the
    # withdrawn term appears WITHOUT its withdrawal marker nearby — our own
    # corrected copy pairs the term with "withdrawn", so it never self-flags.
    # Add one line here whenever a capability is withdrawn;
    # white_glove_propagation.detect_number_drift reads this list and flags
    # every listing still advertising it.
    #
    # ★★★2026-08-25: THE NEGATION USED TO LIVE IN THE PATTERN AND IT COULD NOT
    # WORK. It was `(?![^.]*[Ww]ithdrawn)` — "not followed by 'withdrawn'
    # BEFORE THE NEXT PERIOD" — written on the assumption (stated in the old
    # comment here) that our corrected copy "always pairs DCGI with withdrawn
    # IN THE SAME SENTENCE". True of the listing marketing copy; FALSE of our
    # own tool descriptions, which registries render alongside it. Measured
    # against the live smithery.ai page on 2026-08-25 — five hits, all FALSE
    # POSITIVES, every one inside a description that DOES disclaim:
    #
    #   get_gas_index  "…natural-gas suitability score. ★ WITHDRAWN 2026-08-08:"
    #                  disclaimer only +4 chars away — but past a period.
    #   get_gas_intelligence  4 hits: one +681 chars ahead of its ★ WITHDRAWN
    #                  note, and THREE that sit AFTER it ("two of the DCGI's
    #                  three terms were measurably wrong", "…or the DCGI score
    #                  alone"). Their disclaimer is BEHIND them.
    #
    # ★No lookahead can fix the last case, and Python has no variable-length
    # lookbehind — so the proximity test moved OUT of the regex and INTO
    # white_glove_propagation.detect_number_drift, which now checks a
    # BIDIRECTIONAL ±WITHDRAWN_NEAR_CHARS window around each match. These
    # entries are therefore plain TERM patterns; the disclaimer logic is the
    # detector's. Do not re-add a `(?!…)` here — it silently stops covering the
    # disclaimer-behind case that motivated this change.
    #
    # Why it mattered: these fired on EVERY daily run, kept smithery (our
    # highest-volume registry) permanently "drifted", and burned the auto_path
    # on a non-problem. Same failure `_official_registry_latest_only` was
    # written for — an always-red registry buries the real drift it exists to
    # surface.
    "stale_markers_regex": [
        {"re": r"\bDCGI\b",
         "label": "DCGI advertised as a live score (withdrawn 2026-08-08)"},
        {"re": r"gas[- ]suitability score",
         "label": "gas-suitability score advertised as live (DCGI, withdrawn 2026-08-08)"},
    ],
}


# ── Per-tool "returns:" one-liners — the answer to the recurring AI-model
# critique (Gemini/Perplexity/Grok, 2026-07-02) that agents can't tell what a
# tool returns without a trial call. Keyed to the flagship tool_names above.
# Rendered into llms.txt/llms-full.txt + read by the agent-usefulness master
# shell. Keep concrete (name the returned fields), not marketing.
TOOL_RETURNS = {
    "search_facilities": "facilities {name, operator, lat/lon, power_mw, fiber_count, market_slug, status}",
    "get_facility": "one facility profile {operator, address, lat/lon, power_mw total/used, cooling, fiber carriers, year, status, DCPI verdict, nearby peers}",
    "get_market_intel": "market supply/demand, pricing, vacancy, comparisons",
    "rank_markets": "markets ranked by power certainty & deliverability with DCPI composite_score + BUILD/CAUTION/AVOID verdict",
    "get_grid_intelligence": "ISO grid headroom, constraint, congestion, reserve margin",
    "get_interconnection_queue": "interconnection-queue depth + typical wait (months) for an ISO",
    # ★Split BY capacity_type on purpose: "gen" is DER export headroom, NOT
    # available load, and must never be relayed as "you can site N MW here".
    "get_hosting_capacity": "utility-published feeder hosting capacity per capacity_type (load / gen / bus_headroom) {distinct_feeders, max + median MW, top feeders with substation, voltage_kv, feeder_id, coords, publish date, publishing utilities} — 18 utilities, not nationwide",
    "get_fiber_intel": "fiber routes, carrier count, lit-building proximity",
    "get_gas_intelligence": "gas-pipeline access + delivered-gas economics",
    "list_transactions": "M&A/deal records {buyer, seller, value_usd, date, type, region}",
    "hyperscaler_deals": "hyperscaler builds/leases with capacity + market",
    "analyze_site": "site suitability score across power/fiber/water/incentives, with sources",
    "compare_sites": "side-by-side 2-4 site comparison across power/fiber/risk/time-to-power",
    "score_facility": "a facility's composite score + component breakdown",
    "get_news": "cited industry news items {title, source, date, relevance}",
}


def _get(path, timeout=15):
    req = urllib.request.Request(_BASE.rstrip("/") + path, method="GET")
    req.add_header("X-DC-Probe", "ai-surface-canon")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _mcp_tool_names(timeout=20):
    """Live tool NAMES from the MCP server (tools/list), or None if the
    response carried no tools/list frame. Raises on transport errors —
    callers decide whether an unreachable gate is fatal.

    ★2026-07-29: split out of _mcp_tool_count because arity is the WEAKER
    half of the contract. worker.js MCP_FALLBACK_TOOLS drifted by MEMBERSHIP
    (get_hosting_capacity live but absent) while its COUNT still matched, and
    a count-only guard reported green. Anything checking this surface should
    compare the name SET.
    """
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  # Self-identify as OUR probe (was "canon", which
                                  # passed through verbatim as a fake 'canon' platform
                                  # in mcp_connections analytics). The dchub- prefix +
                                  # PROBE_PLATFORMS entry keep it out of agent counts.
                                  "clientInfo": {"name": "dchub-canon-probe", "version": "1"}}}).encode()
    req = urllib.request.Request(_MCP_BASE.rstrip("/") + "/mcp", data=init, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id")
    hdr2 = dict(hdr)
    if sid:
        hdr2["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    req2 = urllib.request.Request(_MCP_BASE.rstrip("/") + "/mcp", data=body, headers=hdr2)
    with urllib.request.urlopen(req2, timeout=timeout) as r:
        out = r.read().decode("utf-8", "replace")
    for ln in out.splitlines():
        if ln.startswith("data: "):
            ln = ln[6:]
        if ln.startswith("{") and '"id":2' in ln.replace(" ", ""):
            d = json.loads(ln)
            return [t.get("name") for t in (d.get("result") or {}).get("tools", [])]
    return None


def _mcp_tool_count(timeout=20):
    """Live count from the MCP server (tools/list). Thin arity view over
    _mcp_tool_names() — same return contract as before (int, or None when no
    tools/list frame came back)."""
    names = _mcp_tool_names(timeout=timeout)
    return None if names is None else len(names)


# ── Funnel metrics (canonical identity views — NEVER raw session counts) ──
#
# The reach dashboard was invalidated 2026-07-01 because it counted rotating
# session_ids as "agents". The ONLY honest funnel numbers come from the
# identity views (mcp_calls_identity / mcp_agent_retention_30d), which key on
# agent_id + public-IP + real-external filters. Media generators must read
# THESE via resolve_canon()["funnel"] — never hand-roll their own counts.

_FUNNEL_QUERIES = {
    "real_agents_7d": (
        "SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity "
        "WHERE created_at >= NOW() - INTERVAL '7 days' "
        "AND is_public_ip AND is_real_external"),
    "real_agents_30d": (
        "SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity "
        "WHERE created_at >= NOW() - INTERVAL '30 days' "
        "AND is_public_ip AND is_real_external"),
    "day2_return_pct": (
        "SELECT ROUND(AVG(returned_2nd_day::int) * 100, 1) "
        "FROM mcp_agent_retention_30d"),
    # NB: auto_trial_keys keys on minted_at (there is no created_at column).
    "emails_bound_30d": (
        "SELECT COUNT(*) FROM auto_trial_keys "
        "WHERE COALESCE(signed_up_email, operator_email) IS NOT NULL "
        "AND minted_at >= NOW() - INTERVAL '30 days'"),
}


def _funnel_one(cur, sql):
    """Run one metric query fail-soft. Uses a SAVEPOINT so a failed metric
    can't abort a shared transaction (the market-brief tx-abort trap) and a
    caller-passed cursor stays usable for the NEXT metric."""
    sp = False
    try:
        cur.execute("SAVEPOINT _canon_funnel_sp")
        sp = True
    except Exception:
        pass  # autocommit — no tx block, no savepoint needed
    try:
        cur.execute(sql)
        row = cur.fetchone()
        if sp:
            cur.execute("RELEASE SAVEPOINT _canon_funnel_sp")
        return row[0] if row else None
    except Exception:
        if sp:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT _canon_funnel_sp")
            except Exception:
                pass
        raise


def canon_funnel_metrics(cur=None) -> dict:
    """THE single source for funnel numbers on AI surfaces + media posts.

    Returns {real_agents_7d, real_agents_30d, day2_return_pct,
    emails_bound_30d, computed_at} from the canonical identity views.
    Every metric is individually fail-soft to None (never raises). Accepts an
    optional already-open cursor; otherwise opens/returns its own pooled
    connection (same pattern as ai_surface_sentinel._write_findings)."""
    out = {
        "real_agents_7d": None,
        "real_agents_30d": None,
        "day2_return_pct": None,
        "emails_bound_30d": None,
        "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    own_conn = None
    return_pg_connection = None
    if cur is None:
        try:
            from main import get_pg_connection, return_pg_connection
            own_conn = get_pg_connection()
            cur = own_conn.cursor()
            # Bound the identity-view scans so a slow-Neon window can't
            # hold this pooled connection hostage. SET LOCAL only (plain
            # SET doesn't stick on the pooled endpoint); scoped to our own
            # tx — never applied to a caller-provided cursor, whose
            # transaction settings belong to the caller.
            try:
                cur.execute("SET LOCAL statement_timeout = 8000")
            except Exception:
                pass
        except Exception as e:
            out["_error"] = f"db_connect: {str(e)[:100]}"
            return out
    try:
        for key, sql in _FUNNEL_QUERIES.items():
            try:
                val = _funnel_one(cur, sql)
                if val is not None:
                    out[key] = float(val) if key.endswith("_pct") else int(val)
            except Exception as e:
                out[f"_{key}_error"] = str(e)[:100]
    finally:
        if own_conn is not None:
            try:
                cur.close()
            except Exception:
                pass
            try:
                own_conn.rollback()  # read-only; leave the pooled conn clean
            except Exception:
                pass
            try:
                return_pg_connection(own_conn)
            except Exception:
                try:
                    own_conn.close()
                except Exception:
                    pass
    return out


def canon_nums() -> dict:
    """The canonical agent-facing headline numbers, as ready-to-paste strings.

    Keyed by the LITERAL placeholder so canon_text() below is a plain substring
    substitution, never str.format() — surfaces that carry these placeholders
    also carry CSS and inline JS full of literal `{ }`, which .format() chokes on.

    ★2026-08-16: lifted here from main._canon_text so modules that must NOT
    import main (every route module) can derive counts too. main._canon_text now
    delegates to this — ONE implementation, which is the entire point.

    No integer literal fallback on purpose: PINNED['tool_manifest'] is asserted
    equal to tools_advertised by tests/test_fix_closure_shell.py, so its LENGTH
    is a second canon-DERIVED source, not a second hand-typed number. A literal
    here would be the `or 33` shape that published a count stale by 49 on
    /by-the-numbers for months (#2056).

    On {canon_free_calls} / {canon_identified_calls}: these render because ALL
    enforcement lanes agree (free = 10 across tier_registry rate_limit, mcp_daily
    and the edge MCP_TIERS; identified = 50 across all five). There is
    deliberately NO {canon_developer_calls} — that tier's lanes DISAGREE
    (rate_limit 1,000 vs mcp_daily 500), so surfaces link to /pricing instead of
    hand-picking a winner.
    """
    _p = PINNED
    _pub = _p.get('public') or {}
    _tools = _p.get('tools_advertised') or len(_p.get('tool_manifest') or ())
    try:
        from canonical_stats import _FALLBACK as _cf
    except Exception:
        _cf = {}
    return {
        # ★2026-08-16: {canon_version} added because ai_discovery_routes' MCP
        # server-card was serving the literal "2.1.22" — a value on this module's
        # OWN stale_markers denylist, i.e. already known-retired and shipped
        # anyway. Same lesson as /.well-known/mcp.json: the denylist detects, only
        # derivation fixes.
        '{canon_version}':    _p.get('version') or '',
        '{canon_tools}':      str(_tools) if _tools else '',
        '{canon_facilities}': _pub.get('facilities') or '',
        '{canon_deals}':      _pub.get('deals') or '',
        '{canon_markets}':    _pub.get('markets') or '',
        '{canon_countries}':  _pub.get('countries') or '',
        '{canon_isos}':       str(_cf.get('isos') or ''),
        '{canon_free_calls}': str(_p.get('free_tier_calls_per_day') or ''),
        '{canon_identified_calls}': str(_p.get('identified_calls_per_day') or ''),
    }


def canon_text(s):
    """Substitute every {canon_*} placeholder in `s` with its canonical value.

    Fail-open by construction: if the canon cannot be read at all, every value
    resolves to the empty string, so the worst case is a COUNT-FREE sentence,
    never a wrong one. That asymmetry is deliberate — a stale number is the
    failure this module exists to prevent, and a missing one is visible.

    ★ THE FAILURE MODE TO FEAR is the opposite one: adding a {canon_*}
    placeholder to a string and forgetting to pass it through here, which SERVES
    the literal "{canon_facilities}" to an agent. That is worse than the stale
    number it replaced. tests/test_canon_placeholders_resolved.py walks the AST
    of every swept module and fails if any placeholder-bearing string is not
    inside a canon_text() call.
    """
    if not s:
        return s
    for _ph, _val in canon_nums().items():
        if _ph in s:
            s = s.replace(_ph, _val)
    return s


def resolve_canon() -> dict:
    """Return the canon with the MOVING numbers resolved LIVE, so the canon
    itself is never stale. Falls back to public strings if a resolver fails."""
    c = json.loads(json.dumps(PINNED))  # deep copy
    c["resolved_at_note"] = "moving numbers resolved live"
    # ★ Monthly quotas resolve from the SAME function that enforces them
    # (monthly_quota.monthly_quota_for over tier_registry.TIER_LIMITS), so
    # served copy cannot quote a ceiling the gate does not grant. Local
    # import + fail-soft: on any error the pinned numbers above stand.
    try:
        from monthly_quota import monthly_quota_for as _mq_for
        for _t in ("starter", "developer", "pro"):
            c[f"{_t}_calls_per_month"] = _mq_for(_t)
    except Exception as e:
        c["_monthly_quota_error"] = str(e)[:120]
    # facilities + markets from /api/v1/stats
    try:
        s = _get("/api/v1/stats")
        c["facilities_live"] = s.get("facilities")
        c["markets_live"] = s.get("markets")
    except Exception as e:
        c["_stats_error"] = str(e)[:120]
    # Deals resolve from canonical_stats (curated buyer+seller subset), NOT
    # /api/v1/stats `deals` — that field is the RAW ~11.5K pile (capex/
    # undisclosed/junk) and publishing it as "M&A deals" is a ~2.5x over-claim
    # (2026-07-16 double-count trap). Floors to "4,000+". Overrides the pinned
    # public string so every resolve_canon() consumer self-heals.
    try:
        from canonical_stats import deals_phrase as _deals_phrase
        _dp = _deals_phrase()
        c["deals_live"] = _dp
        c["public"]["deals"] = _dp
    except Exception as e:
        c["_deals_error"] = str(e)[:120]
    # ★★2026-07-28: FACILITIES NOW SELF-HEALS TOO. `deals` and `markets` were
    # overridden live here; `facilities` was NOT — it was the one headline number
    # still served from the PINNED string, so it froze at "12,650+" while the
    # real deduped fleet grew to 15,207. And because /api/v1/canon/phrases is the
    # ONE source the frontend heal, llms.txt and the registry manifests all pull
    # from, that single un-overridden line held every public surface stale at
    # once — we were UNDER-claiming by ~2,500 facilities everywhere.
    #
    # ★Uses facilities_verified_phrase(), NOT facilities_phrase():
    #   facilities_phrase()          = COUNT(*) rows      -> "21,000+"  OVER-claim
    #   facilities_verified_phrase() = DISTINCT slug,     -> "15,200+"  honest
    #                                  non-duplicate
    # The raw row count is the discovery pile INCLUDING flagged duplicates. The
    # March 2026 backfill wrote 21,210 rows for only 13,588 distinct buildings —
    # that gap is exactly the "21.7k dropped to 13k" confusion. Public copy must
    # lead with buildings, never rows. floors round DOWN, so the phrase can never
    # exceed reality.
    try:
        from canonical_stats import facilities_verified_phrase as _fac_phrase
        _fp = _fac_phrase()
        c["facilities_verified_live"] = _fp
        c["public"]["facilities"] = _fp
    except Exception as e:
        c["_facilities_error"] = str(e)[:120]
    # ★2026-07-29: markets self-heal the same way deals does. The pinned "300+" is
    # a FLOOR, so a consumer that never calls resolve_canon() can only under-claim;
    # this override republishes the live floor from the canonical query so the
    # pinned literal can never drift ABOVE reality again (it had: "311" vs live
    # 306). Fail-soft — on any error the pinned floor stands.
    try:
        from canonical_stats import markets_phrase as _markets_phrase
        _mp = _markets_phrase()
        c["markets_phrase_live"] = _mp
        c["public"]["markets"] = _mp
    except Exception as e:
        c["_markets_error"] = str(e)[:120]
    # ★2026-07-30: countries self-heals too — it was the LAST headline number
    # still served only from the pin (deals/facilities/markets/tools all
    # override above), so the pin stayed right by luck, not by wiring. The same
    # day, the mcp-server initialize instructions were bound to "180+
    # countries" off /api/v1/stats `countries` = 186 — but that figure was
    # measured on the LEGACY `facilities` table, which mixes full names
    # ("USA", "Germany") with ISO codes ("US", "DE"); 9 of its 186 distinct
    # values are format duplicates of a code already present. The deduped
    # fleet the "15,300+ facilities" claim counts spans 178 distinct codes
    # (measured 2026-07-30, discovered_facilities, incl. territories) →
    # honest floor "170+", and "180+" was an over-claim. Countries must be
    # measured on the SAME table as the facility count they are paired with —
    # never on the legacy `facilities` table.
    try:
        from canonical_stats import countries_verified_phrase as _countries_phrase
        _cp = _countries_phrase()
        c["countries_phrase_live"] = _cp
        c["public"]["countries"] = _cp
    except Exception as e:
        c["_countries_error"] = str(e)[:120]
    # live tool count from the MCP server — override the pinned fallback so
    # every resolve_canon() consumer tracks tools/list and never goes stale.
    try:
        c["tools_live"] = _mcp_tool_count()
        if isinstance(c["tools_live"], int) and c["tools_live"] > 0:
            c["tools_advertised"] = c["tools_live"]
    except Exception as e:
        c["_tools_error"] = str(e)[:120]
    # funnel metrics from the canonical identity views (fail-soft internally)
    try:
        c["funnel"] = canon_funnel_metrics()
    except Exception as e:
        c["funnel"] = None
        c["_funnel_error"] = str(e)[:120]
    # ★2026-08-22 Claim Loop step 1: every PINNED headline number is a CLAIM.
    # Each pin is registered as its OWN expectation (`== <pin>`, horizon 24h)
    # and judged against the live override resolved above, so a pin that lags
    # what the sources say is REFUTED on the ledger instead of discovered by
    # hand (the four hand-walks documented above). Memoised per process,
    # fail-soft, adds no key on success.
    #
    # ★2026-08-23 — THE DIRECTION WAS BACKWARDS AND THAT MADE IT USELESS. It
    # registered the RESOLVER's value as the expectation, and the ledger then
    # resolved the actual through resolve_canon() too, so actual == expected
    # for every canon key by construction: claim 100945 shipped carrying the
    # exact disagreement it existed to catch (pinned "1,800+", expected
    # "== 1,900+") and was still judged `confirmed` in production. Assert the
    # PIN — it is the one side that is not re-read from the instrument.
    try:
        from routes.claim_ledger import register_canon_claims as _register_canon_claims
        _register_canon_claims(PINNED, c)
    except Exception as e:
        c["_claims_error"] = str(e)[:120]
    return c


# ── is a resolved headline number a MEASUREMENT, or the pinned fallback? ──
# ★2026-08-23. resolve_canon() is fail-soft by design: every override above
# sits in its own try/except, and on any error the PINNED literal stands and an
# `_<key>_error` note is added. That is right for a SERVED surface — under-claim
# rather than break — but it is a trap for anything reading the result as an
# INSTRUMENT: a pool hiccup makes the resolver echo the pin back, so
# "live == pinned" stops meaning "the pin is current" and starts meaning "we
# could not look". A reader that cannot tell those apart confirms the pin
# against itself.
#
# The claim ledger's canon claims are exactly such a reader — they ASSERT the
# pin and MEASURE this override (routes/claim_ledger.register_canon_claims) —
# so the witness table lives HERE, beside the overrides it describes, rather
# than as a second copy of this module's internals in the consumer.
#
# Each entry names the SEPARATE key resolve_canon() writes only on the success
# path: `c["deals_live"]` is assigned in the same try as `c["public"]["deals"]`,
# so its presence witnesses that the value came from canonical_stats and not
# from the deep copy of PINNED. Keep an entry here whenever an override is
# added below — an unmapped key reads as NOT live, which fails closed.
_LIVE_WITNESS = {
    "public.facilities": "facilities_verified_live",
    "public.deals": "deals_live",
    "public.markets": "markets_phrase_live",
    "public.countries": "countries_phrase_live",
    "tools_advertised": "tools_live",
}


def canon_is_live(resolved: dict, key: str) -> bool:
    """True when `key` in a resolve_canon() payload carries a LIVE measurement
    rather than the PINNED fallback.

    Fails CLOSED — an unknown key, a payload that is not a resolve_canon()
    result, and every key whose resolver raised all read False, because the
    caller is asking whether it may treat the value as measured."""
    witness = _LIVE_WITNESS.get(key)
    if not witness or not isinstance(resolved, dict):
        return False
    val = resolved.get(witness)
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return val > 0
    return bool(str(val).strip())
