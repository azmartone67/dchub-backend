"""mcp_calls_deloop.py — THE single source of the mcp_tool_calls de-loop.

The honest "real external tool calls" number (`tool_calls_7d_real`) had THREE
disagreeing definitions:

  1. flask_mcp_endpoints.mcp_funnel() — the canonical /api/v1/mcp/funnel
     endpoint — classified each row with a big `_platform_case` CASE and then
     counted rows whose platform was NOT IN a narrow 6-item `_PROBE_PLATFORMS`
     list (curl/python-script/node-script/postman/insomnia/verify).
  2. routes/funnel_health._deloop_calls_where() — a hand-rolled WHERE that
     excluded a broad 18-item client_name list + NOT-LIKE loop/probe/sweep
     families + self-UA patterns.
  3. the mcp_funnel_real VIEW (mcp_upgrade_signals only) — yet another list.

That drift meant funnel_health.tool_calls_7d_real != the funnel endpoint's
tool_calls_7d_real even though both claimed to be "the honest number".

This module collapses (1) and (2) into ONE definition for the
`mcp_tool_calls` table:

  • PLATFORM_CASE      — the canonical UA/client_name → platform classifier.
  • PROBE_PLATFORMS    — the classifier outputs we treat as self/probe traffic.
  • real_calls_predicate()  — boolean SQL `<PLATFORM_CASE> NOT IN (<probes>)`,
                              the exact filter the funnel endpoint FILTERs on.
  • deloop_calls_where()    — the same predicate, with no leading AND, for use
                              as a `... AND <deloop_calls_where()>` fragment.

Both flask_mcp_endpoints.mcp_funnel() and routes/funnel_health import from
here, so the two `tool_calls_7d_real` counts are byte-identical by
construction. tests/test_funnel_health_deloop.py asserts they stay in lock-step.

No DB import-time requirement — this module is pure SQL-string assembly over
trusted hardcoded constants (no bound params, so the literal % in the LIKE /
ILIKE patterns are left alone — see the psycopg2 empty-tuple % trap).
"""
from __future__ import annotations

# ── Canonical platform classifier ─────────────────────────────────────
# Same UA-based classifier the funnel endpoint and mcp_growth use. A row's
# "platform" is its client_name (when it's a real name, not a session UUID),
# else a UA-sniffed bucket, else 'unknown'. The internal-dchub bucket folds
# every one of our own crawler/healer User-Agents (client_name is null/empty
# for ~70% of rows, so the UA catch is load-bearing). 'unknown' is REAL
# external traffic we couldn't sub-classify — it is NOT a probe.
PLATFORM_CASE = r"""
    CASE
        -- ★2026-07-28: "mcp" is the name of the PROTOCOL, not of a client.
        -- 4,283 calls/7d arrive with clientInfo.name="mcp", and the verbatim
        -- branch below trusted it as an identity — which is how 87% of call
        -- volume ended up in a bucket we described as "unattributed". It was
        -- never unattributed; it was one generic string being read as a name.
        --
        -- Trusting it also PRE-EMPTED the internal-dchub branch, so our own
        -- probes (dchub-fixwave-probe/1.0 and friends, 1,072 calls / 132
        -- agent_ids in 7d) were classified 'mcp' rather than as self-traffic.
        -- PROBE_PLATFORMS could not exclude them because they never reached it.
        --
        -- NOTE the trap this avoids. The obvious fix — "don't trust it, fall
        -- through to the UA branches" — sends every one of these rows to
        -- `user_agent ILIKE '%node%'` => 'node-script', which IS in
        -- PROBE_PLATFORMS. That would have silently deleted our LARGEST REAL
        -- COHORT (3,164 calls / 63 agents of genuine agent work: 35 distinct
        -- tools, 11-50 calls per episode, zero single-call episodes) and read
        -- as a traffic collapse. A generic client_name means "unnamed MCP
        -- client", NOT "someone's node script hitting the REST API", so these
        -- rows get their own real bucket and never touch the node heuristics.
        --
        -- Verified against the replica before shipping: the split yields
        -- mcp-generic-client 3,212 calls/66 agents (real, preserved — matches
        -- the 3,207/65 the reach endpoint already reports) and internal-dchub
        -- 1,072/132 (probe, correctly excluded). Headline real numbers do not
        -- move; the classification stops being accidentally right.
        WHEN LOWER(COALESCE(client_name, '')) IN ('mcp', 'mcp-client', 'client', 'default')
             AND (user_agent ILIKE '%dchub-%' OR user_agent ILIKE '%dchubhealer%'
                  OR user_agent ILIKE '%brain-v2-headless%' OR user_agent ILIKE '%brain-radar%'
                  OR user_agent ILIKE '%uptimerobot%' OR user_agent ILIKE '%DCHub/%'
                  OR user_agent ILIKE '%Globeholder-%')
            THEN 'internal-dchub'
        WHEN LOWER(COALESCE(client_name, '')) IN ('mcp', 'mcp-client', 'client', 'default')
            THEN 'mcp-generic-client'
        WHEN NULLIF(LOWER(client_name), '') IS NOT NULL
             AND client_name !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN LOWER(client_name)
        WHEN user_agent ILIKE '%dchub-%' OR user_agent ILIKE '%dchubhealer%'
            OR user_agent ILIKE '%brain-v2-headless%' OR user_agent ILIKE '%brain-radar%'
            OR user_agent ILIKE '%uptimerobot%'
            THEN 'internal-dchub'
        WHEN user_agent ILIKE '%@modelcontextprotocol/sdk%'
            OR user_agent ILIKE '%modelcontextprotocol%'
            THEN 'mcp-sdk'
        WHEN user_agent ILIKE '%mcp-inspector%'
            THEN 'mcp-inspector'
        WHEN user_agent ILIKE '%n8n%'
            THEN 'n8n'
        WHEN user_agent ILIKE '%smithery%'
            THEN 'smithery'
        WHEN user_agent ILIKE '%chatgpt%' OR user_agent ILIKE '%openai%'
            THEN 'chatgpt'
        WHEN user_agent ILIKE '%claude%' OR user_agent ILIKE '%anthropic%'
            THEN 'claude'
        WHEN user_agent ILIKE '%perplexity%'
            THEN 'perplexity'
        WHEN user_agent ILIKE '%gemini%' OR user_agent ILIKE '%googleother%'
            THEN 'gemini'
        WHEN user_agent ILIKE '%groq%'
            THEN 'groq'
        WHEN user_agent ILIKE '%cursor%'
            THEN 'cursor'
        WHEN user_agent ILIKE '%windsurf%' OR user_agent ILIKE '%codeium%'
            THEN 'windsurf'
        WHEN user_agent ILIKE '%continue%'
            THEN 'continue.dev'
        WHEN user_agent ILIKE '%cody%' OR user_agent ILIKE '%sourcegraph%'
            THEN 'sourcegraph-cody'
        WHEN user_agent ILIKE '%copilot%'
            THEN 'github-copilot'
        WHEN user_agent ILIKE '%cline%'
            THEN 'cline'
        WHEN user_agent ILIKE '%phind%'
            THEN 'phind'
        WHEN user_agent ILIKE '%you.com%' OR user_agent ILIKE '%youbot%'
            THEN 'you.com'
        WHEN user_agent ILIKE '%meta-external%' OR user_agent ILIKE '%llama%'
            THEN 'meta-ai'
        WHEN user_agent ILIKE '%applebot-extended%'
            THEN 'apple-intelligence'
        WHEN user_agent ILIKE '%curl%'
            THEN 'curl'
        WHEN user_agent ILIKE '%python%' OR user_agent ILIKE '%requests%'
            THEN 'python-script'
        WHEN user_agent ILIKE '%node-fetch%' OR user_agent ILIKE '%undici%'
            OR user_agent ILIKE '%axios%' OR user_agent ILIKE '%got/%'
            THEN 'node-http-client'
        WHEN user_agent ILIKE '%node%'
            THEN 'node-script'
        WHEN user_agent ILIKE '%postman%'
            THEN 'postman'
        WHEN user_agent ILIKE '%insomnia%'
            THEN 'insomnia'
        ELSE 'unknown'
    END
"""

# ── Probe / self-traffic platforms ────────────────────────────────────
# Classifier outputs that are our own probes / generic scripting clients —
# NOT external AI-agent demand. Counting them headlined a ~35-41k 7d number
# that dipped whenever the selfheal loop changed cadence (a non-decline that
# read like a decline). 'internal-dchub' is the bucket the PLATFORM_CASE folds
# every one of our crawler/healer UAs into — it MUST be excluded or the
# null-client_name self-traffic falls straight through to the count.
#
# This is the union of:
#   • flask_mcp_endpoints _PROBE_PLATFORMS (curl/python-script/node-script/
#     postman/insomnia/verify), and
#   • the self-traffic families funnel_health excluded via UA NOT-LIKE
#     (dchub-/dchubhealer/brain-radar/... → all classify to 'internal-dchub'),
# expressed once over the single PLATFORM_CASE classifier.
PROBE_PLATFORMS = (
    'internal-dchub',
    'curl', 'python-script', 'node-script',
    'node-http-client',
    'postman', 'insomnia', 'verify',
    # AI-Surface sentinel's tools/list probe (ai_surface_canon.py). Its
    # clientInfo.name wins PLATFORM_CASE's first branch (real client_name
    # passes through verbatim), so it must be excluded here by name.
    # 'canon' is the legacy name its rows carried before 2026-07-02.
    'dchub-canon-probe', 'canon',
)


def _probe_in_list() -> str:
    """SQL literal IN-list of the probe platforms (sorted+deduped, escaped).
    Trusted hardcoded constants only — inlined as literals so no bound params
    collide with the literal % in PLATFORM_CASE's ILIKE patterns."""
    return ",".join(
        "'" + str(p).replace("'", "''") + "'"
        for p in sorted(set(PROBE_PLATFORMS)))


# ── Internal/self platforms by the WRITE-TIME `platform` column ───────────
# r-funnel-platform-excl (2026-06-20): the PLATFORM_CASE classifier above keys
# on client_name/user_agent and MISSES our self-heal traffic — those rows carry
# client_name='dchub-selfheal' (or the literal 'unknown'), which wins the first
# CASE branch and classifies as a real platform. So dchub-selfheal (the single
# biggest source, ~93% of raw volume, ~1,878 of the last 7d's "real" 3,166) sailed
# straight into tool_calls_7d_real, making the headline swing with the self-heal
# cadence and read as a funnel collapse (verified 2026-06-20 — empirical: current
# de-loop removed only 25 of 3,191 rows). The reliable signal is the write-time
# `platform` COLUMN, which our own services self-identify in. Exclude those here,
# on TOP of the classifier, but KEEP NULL/empty platform (real anonymous agents
# often have no platform) — only NAMED internal/probe/test platforms are dropped.
INTERNAL_PLATFORM_VALUES = (
    'mcp-probe', 'pipeline_mcp', 'probe', 'verify', 'dchubhealer',
    'capwall2', 'fv', 'v', 't', 'p', 'test',
    # r-platform-tag-excl (2026-07-01): live-DB audit of platform tags passing
    # is_real_external in mcp_calls_identity. value-harness was UA-excluded only
    # (UA 'dchub-value-harness/1.0') — a harness UA change would leak ~100
    # rotating cloud IPs/wk straight into the reach counts, so pin the write-time
    # platform tag too. The rest are observed one-off internal test/QA tags that
    # WERE passing (1 IP each, cumulatively ~15 fake "agents" over 3 weeks).
    'value-harness', 'clawith', 'dbg', 'raw', 'full', 'f5r',
    'mcp-vouch', 'qa-mozilla', 'qa', 'fix2-v2', 'rev', 'final', 'vinline',
    # ── r-registry-crawlers (2026-07-28) ──────────────────────────────────
    # Live audit of ~4.5h of gateway `[MCP] init` logs: the "real agent"
    # population is dominated by MCP REGISTRY CRAWLERS, INDEXERS, SCORERS and
    # HEALTH CHECKERS, not AI platforms. Every one passed is_real_external,
    # because _SYNTHETIC_CLIENT_PREFIXES is a PREFIX match and none of them
    # begin with dchub-/qa-/probe-/monitor-/healthcheck (e.g. 'yellowmcp-health'
    # CONTAINS "health" but does not START with "healthcheck").
    #
    # Corroborating signature in /api/v1/reach: top_tools_7d showed 53-65
    # agents on EACH of twelve different tools, near-uniform — a catalog
    # SWEEP, not work. It also plausibly explains the 5.0% day-2 retention,
    # since a scanner runs once and never returns.
    #
    # Same class as the 2026-07-01 value-harness leak and the 2026-06-30
    # "86 AI agents" LinkedIn over-claim: our own headline counted machines
    # that were cataloguing us, not using us.
    'mcp-gateway-registry', 'catalog-sync', 'yellowmcp-health',
    'manifest-mirror', 'watchdog', 'agentpulse', 'mcpscoringengine',
    'mcpqueen-grader', 'mcpexplorerbot', 'mcpindex-trust',
    'mcp-rugpull-research',
    # ── 2026-08-15: the QA judge fleet's synthetic personas, written as
    # WRITE-TIME platform tags (server.mjs ships clientInfo.name through).
    # 'reviewer-sim' arrived under a Chrome/120.0 two-segment disguise the UA
    # families of the day missed; the exact tag kills it and its siblings
    # regardless of UA costume. Same class as the 07-01 one-off QA tags
    # (10 rows, 2 IPs — both the QA operator's; no real platform carries
    # these names). NOT the real channel tags (copilot/gemini/grok/...) —
    # those stay countable by design.
    'reviewer-sim', 'acme-siting-agent',
)

# ★★ DELIBERATELY **NOT** EXCLUDED — read this before "finishing the list".
# These three look like the family above and are NOT the same thing:
#   · smithery  — a registry AND a hosted GATEWAY that proxies real end-user
#     traffic. `smithery connect` already appears as its own platform tag in
#     /api/v1/reach, which is the fingerprint of proxied users. Excluding it
#     would delete real agents to make a metric prettier.
#   · glama     — a directory that also ships a chat client.
#   · agent-toolscloud — observed presenting an API KEY (`key=8a58cf…`) at
#     initialize. An anonymous crawler does not hold a key; that is the
#     signature of a real integration.
# The honest position is that these are AMBIGUOUS, and the cost of a false
# exclusion (silently deleting a real customer) is far worse than the cost of
# a false inclusion (a slightly generous count we can still see and name).
_AMBIGUOUS_NOT_EXCLUDED = ('smithery', 'glama', 'agent-toolscloud')

# r-registry-crawlers family fragments (2026-07-28), single-sourced here since
# 2026-07-30: external_platform_predicate() (LIKE form) AND
# internal_tag_regex_predicate() (the bound-params-safe regex twin) both render
# from THIS constant. The Agent Success Report build found the twin had
# silently missed this entire block — the LIKE form gained nine families on
# 07-28 while the regex form kept serving the pre-crawler verdict, exactly the
# drift the twin's docstring promised could not happen. A family lives here or
# it does not exist; never add one to a predicate body directly.
# ('validator' was the ninth: found by re-reading the LIVE reach payload after
# shipping the first eight — `mcp-server-validator` matched none of them.)
# None of these may match _AMBIGUOUS_NOT_EXCLUDED — the guard test probes that.
REGISTRY_CRAWLER_FAMILIES = (
    "registry|catalog|mirror|watchdog|health|crawler|scanner|uptime|validator")


def _internal_platform_list() -> str:
    return ",".join("'" + str(p).replace("'", "''") + "'"
                    for p in sorted(set(INTERNAL_PLATFORM_VALUES)))


def external_platform_predicate(col: str = "platform") -> str:
    """TRUE when the write-time `platform` column is NOT one of our own
    internal/self/probe services. `dchub-%` covers selfheal / mcp-test /
    regression-test; the explicit list covers named probes + short test-client
    names. NULL/empty platform is KEPT (real anonymous agents).

    `col` lets other tables reuse THIS verdict on their own tag column
    (e.g. mcp_high_intent_sessions.mcp_client) instead of keeping a second
    hand-maintained exclusion list. NOTE: contains literal % (LIKE) — never
    embed in a psycopg2 query that also passes bound params; use
    internal_tag_regex_predicate() there instead."""
    p = f"COALESCE(LOWER({col}), '')"
    # NOT LIKE patterns catch recurring internal validators/probes whose names
    # vary (p1verify, gate-audit, mcp-probe, diag-*, …) without an ever-growing
    # exact list. Literal % is safe here — this clause is inlined (no bound
    # params), same as PLATFORM_CASE's ILIKE patterns.
    # r-platform-tag-excl (2026-07-01) additions:
    #   · '%dchub%' (was 'dchub-%') — catches devin-dchub-client /
    #     local-agent-mode-dchubviamcp-* style tags anywhere in the name; no
    #     external agent self-tags its platform with our own product name.
    #   · '%harness%' / '%test%' / '%check%' / '%diag%' / '%sweep%' — the
    #     recurring internal QA families (value-harness, paywall-test,
    #     capcheck/fix5-recheck, cap-diag-fresh-*, sweep-test2, …).
    #   · length ≤ 2 tags ('e','c','w','pw', …) are ad-hoc curl test params,
    #     never a real client; NULL/empty stays KEPT via the p='' escape.
    # Family clauses are RENDERED from the two shared constants (QA families +
    # r-registry-crawlers), never hand-listed here, so this LIKE form and the
    # regex twin below cannot drift again. 'health' is safe to match broadly in
    # THIS domain: DC Hub is data-centre infrastructure, and no AI platform
    # names itself with it. See _AMBIGUOUS_NOT_EXCLUDED for what the crawler
    # families must NOT catch.
    families = (INTERNAL_TAG_FAMILIES.split("|")
                + REGISTRY_CRAWLER_FAMILIES.split("|"))
    fam_clauses = "".join(f"AND {p} NOT LIKE '%{fam}%' " for fam in families)
    return (f"({p} NOT IN ({_internal_platform_list()}) "
            + fam_clauses
            + f"AND ({p} = '' OR LENGTH({p}) > 2))")


# ── Raw-scripting / internal UA exclusion (2026-06-24) ────────────────────
# PLATFORM_CASE keys on client_name FIRST, so test/probe traffic that SETS a
# client_name slips past the classifier even though its USER-AGENT is a dead
# giveaway. Live audit (2026-06-24): the top two "platforms" in the de-looped
# 7d reach were value-harness (client='value-harness', UA 'dchub-value-harness/1.0',
# ~69 IPs — OUR OWN test harness) and clawith (UA 'python-httpx', ~28), plus
# rv/final/funnel-diag3 (UA 'curl'). Counting them ~3x-inflated the de-looped
# reach (104 vs the real ~30) on every funnel surface. This guard excludes by
# USER-AGENT regardless of client_name. Bare 'node' is mcp-remote (a real
# transport) and is deliberately NOT matched. Mirrors
# routes/mcp_high_intent_claim._hi_real_sql's UA guard — keep the two aligned.
# 2026-07-19 additions:
#   · dchub/ — the hyphen-less self-UAs ('DCHub/1.0 (+https://dchub.cloud)',
#     'DCHub/3.0 …') slipped the 'dchub-' guard; no external agent carries our
#     product name in its UA.
#   · chrome/126\.0 safari — the fixwave/qa/deepdive master shells' browser
#     disguise. The two-segment version is MALFORMED (real Chrome has been
#     frozen at MAJOR.0.0.0 — 'Chrome/126.0.0.0 Safari' — for years), so this
#     matches ONLY the hand-written probe UA, never a real browser. The same
#     codebase runs on Railway backend+worker AND the stale Render backend, so
#     the fleet spanned Railway egress + AWS + Alibaba IPs — ~97 fake "agents"
#     and ~860 fake real-calls over 2026-07-05..19 — which an IP list can't
#     pin down but this fingerprint kills everywhere, history included.
# 2026-08-15 additions — QA smoke/attribution fingerprints. Live audit: the
# platform tags copilot/gemini/grok were 100% ONE QA smoke agent (agent_id
# 62a465b0…, UAs 'Copilot-QA/1.0 (dchub approval smoke test)', 'GeminiCLI/1.0
# (attribution-test)', 'Cursor/1.0 (attribution-test)', 'Grok-DC-Hub/1.0' —
# ~20 calls across 7 platform tags), and a second QA agent (840d969f…, UAs
# 'qa-judge-probe-…', 'acme-siting-agent/1.0', 'reviewer-sim') inflated the
# claude channel. All passed because the exclusions here match UA FAMILIES and
# none of these strings did. Excluded by UA SIGNATURE only — the bare platform
# tags (copilot/gemini/grok/mistral/cursor) stay countable, so a real agent
# arriving on those channels still registers.
#   · smoke test / attribution-test — the QA harness self-describes its
#     purpose in parentheses; no real client UA narrates its test intent.
#   · -qa/ — the bare 'Copilot-QA/1.0' rows (8 of the 12) carry NO smoke-test
#     suffix; live sweep 2026-08-15 confirmed '-qa/' matches ONLY the two
#     Copilot-QA variants across all of mcp_tool_calls. A client that
#     version-tags itself '<Platform>-QA/n' is announcing QA, not demand.
#   · qa-judge-probe / acme-siting-agent / reviewer-sim — the judge-probe
#     fleet's synthetic personas.
#   · dc-hub — the HYPHENATED product-name form ('Grok-DC-Hub/1.0') slipped
#     both dchub- and dchub/; same rationale as 2026-07-19: no external agent
#     carries our product name in its UA.
#   · chrome/1[2-9][0-9]\.0 safari — generalized from the 07-19 literal
#     (126.0 only): the SAME two-segment malformation shipped as Chrome/120.0
#     (reviewer-sim's disguise, agent 840d969f) and Chrome/124.0 (capcheck).
#     Live sweep 2026-08-15: every match in the table is one of those three
#     hand-written disguises; real Chrome has been MAJOR.0.0.0 for years, so
#     the class match adds zero real-browser risk and stops the next fleet
#     from dodging by decrementing the version.
# 2026-08-16 additions — the "cursor" one-off verification scripts. The
# funnel's calls_by_platform_30d showed cursor = 11 calls; every row is agent
# ff29c4ac… = ONE operator Comcast IP (2601:18e:c003:…), 2026-07-28 only,
# which the same day also ran dchub-attrib-probe/1.0 (tagged cursor/
# claude-code/mistral), dchub-verify-probe/1.0 and keyless-audit-probe/1.0 —
# an attribution-QA session, not Cursor demand. These three never set a
# dchub- prefix so the existing families missed them.
#   · starterpack-verify/ / starterpack-audit/ / order-verify/ — EXACT
#     fingerprints (trailing slash on purpose), never broad verify/audit
#     families: live third-party verifiers are real countable traffic
#     ('LinerMCPVerifier/1.0', 'TrimtabVerifier/0.1', 'SaSame-MCP-Audit/0.1')
#     and a family match would delete them. Live sweep 2026-08-16: the three
#     fingerprints match exactly the 11 rows table-wide, nothing else.
#     client_name AND platform both said 'cursor', so the platform-tag lane
#     cannot carry this exclusion — the channel must stay countable.
#   · keyless-audit-probe/ — same agent ff29c4ac, same 2026-07-28 session,
#     surfaced by sweeping the agent's REMAINING real rows after the three
#     above shipped. 'dchub-' is absent from the name and 'starterpack-audit/'
#     does not match it, so no family caught it. Table-wide: 3 rows, one IP
#     (the operator's), one day. Same exact-fingerprint rule as above.
# 2026-08-16 (r2) — the same sweep run against the OTHER operator agent,
# 840d969f… (72.208.88.69, the qa-judge machine from the 08-15 block). Every
# UA below exists ONLY from that IP across the whole table; the agent's bare
# 'node' / claude-code rows are its genuine MCP clients and stay counted.
#   · chrome/1[2-9][0-9](\.0)? safari + chrome/1[2-9][0-9]$ — the disguise
#     class MUTATED AGAIN: 'Chrome/126 Safari' (no dot at all, 4 calls
#     2026-08-16) and a UA ENDING at bare 'Chrome/120' (2026-08-15) both
#     dodge the \.0-requiring form. Live sweep 2026-08-16: everything
#     matching the widened patterns is a hand-written disguise (the 1,598
#     already-excluded two-segment rows + these 5); real Chrome is always
#     MAJOR.0.0.0 followed by ' Safari/537.36', so neither pattern can touch
#     a real browser.
#   · qa-judge- (widened from qa-judge-probe) — 'qa-judge-keyed-…' dodged
#     the -probe form. All qa-judge-* forms table-wide are operator-only.
#   · adversarial-verify / \(verify2\) — skeptic-fleet costumes (12 + 1
#     calls, 2026-08-13); no real client narrates its verification intent.
#   · cursor-mcp/1\.0 / cline-mcp/1\.0 — 2026-07-05 attribution-era
#     fabrications (3 + 1 calls, operator IP only in ~3.5 months). Version
#     pinned so a future REAL client named cursor-mcp/2.x would still count.
#   · healthcheck/1\.0 — one-off probe (1 call, 2026-07-18); the registry
#     'health' family only covers platform TAGS, not UAs.
# 2026-08-16 (r3) — the smoke agent 62a465b0 + a PREFIX-WIDE close-out. The
# operator's home network is an IPv6 /64 with privacy rotation
# (2601:18e:c003:a60:*), so ONE machine mints a new agent_id per interface
# identifier — sweeping agent-by-agent is whack-a-mole; this round swept
# every still-real row across the whole prefix (+72.208.88.69) at once.
#   · mistral-org-agent — the operator's hand-built Mistral connector relay
#     (mistral-org-agent/1.0 + mistral-org-agent-relay/1.0, 10 calls,
#     07-26/27, three operator host-suffixes, MODELREL era). The REAL
#     Mistral client is 'MistralAI-MCPClient/1.0' (Azure egress) and stays
#     countable — the keep-side test pins it.
#   · ^audit/1\.0 — bare 'audit/1.0' one-offs (3 calls, 07-27). ANCHORED at
#     the string start on purpose: unanchored it would swallow a future real
#     '<Name>-Audit/1.0' third-party auditor (SaSame-MCP-Audit is live
#     today); anchored it can only match a UA that IS 'audit/…'.
# After this round the prefix's only real-external rows are bare 'node'
# (mcp-remote's transport UA, shared with real users globally) and
# claude-code/* — the operator's genuine clients, kept by design.
# 2026-08-16 (r4) — human-simulated : the /upgrade/h relay-open ops-probe
#   costume ('human-simulated/2.0' — relay_opens' only valid all-time row,
#   session_id='', our own probe). Surfaced when handoff-funnel v3 began
#   reading relay_opens through THIS predicate: unswept, that row counts as
#   a human open the moment any blank-sid session exists. Same rationale as
#   reviewer-sim / adversarial-verify — no real client announces itself as a
#   simulated human. Unanchored family on purpose: a versioned or suffixed
#   mutation stays swept.
_SCRIPT_INTERNAL_UA = (
    "python-httpx|python-urllib|urllib|curl/|wget|libwww|node-fetch|undici|axios|"
    "got/|go-http|okhttp|java/|requests/|aiohttp|scrapy|httpie|restsharp|"
    # r-selftraffic-funnel (2026-08-17): was `dchub-|dchub/|dchubhealer`, which
    # required a separator after our own product name. `DCHubProbe/1.0` has
    # neither, so four of our own agent-pay probe rows on 08-17 were counted as
    # external traffic on a funnel whose entire population is 19 rows. Bare
    # `dchub` is the same verdict external_platform_predicate already applies to
    # platform tags ("no external agent self-tags with our own product name"),
    # and it is a strict superset of the three patterns it replaces — nothing
    # that was counted as real before is newly excluded except UAs that embed
    # our name without a separator, which are ours by construction.
    "dchub|self.?heal|value-harness|regression|brain-radar|brain-v2-headless|render-verify|uptimerobot|"
    "chrome/1[2-9][0-9](\\.0)? safari|chrome/1[2-9][0-9]$|"
    "smoke test|attribution-test|-qa/|qa-judge-|acme-siting-agent|reviewer-sim|dc-hub|"
    "starterpack-verify/|starterpack-audit/|order-verify/|keyless-audit-probe/|"
    "adversarial-verify|\\(verify2\\)|cursor-mcp/1\\.0|cline-mcp/1\\.0|healthcheck/1\\.0|"
    "mistral-org-agent|^audit/1\\.0|human-simulated"
)


# ── Operator self-traffic sessions (r-selftraffic-funnel, 2026-08-17) ─────
# WHY THIS IS A DECLARED LIST AND NOT AN INFERENCE.
#
# Every other exclusion in this module derives from something the caller
# WROTE: a platform tag, a UA family, a client name. The operator's own agent
# client writes nothing distinguishable. Measured 2026-08-17 on
# mcp_high_intent_sessions:
#
#     mcp_session_id 88e20dac-…  mcp_client 'claude'  user_agent 'node'
#
# That is byte-identical to a prospect running Claude Code. There is no
# behavioural signal to key on, and inventing one — "too many tools", "too many
# days" — would silently delete the best real leads, which is the exact failure
# mode _AMBIGUOUS_NOT_EXCLUDED exists to prevent.
#
# So the exclusion is a NAMED FACT, not a derivation: we know this session is
# ours because we watched ourselves make it. It is declared here, published in
# the funnel envelope (never applied silently), and every consumer keeps the
# unfiltered figure alongside it. A reader who disagrees with the exclusion can
# see exactly what was removed and add it back.
#
# Seeded with the operator's own Claude Code session. Extend via the env var
# rather than editing this tuple when the operator's client rotates.
_SELF_TRAFFIC_SESSION_SEED = ("88e20dac",)


def self_traffic_session_prefixes():
    """Session-id prefixes known to be OUR OWN traffic. Env var
    DCHUB_SELF_TRAFFIC_SESSIONS (comma-separated) EXTENDS the seed; it never
    replaces it, so a malformed env value cannot silently re-admit a session we
    already know is ours."""
    import os as _os
    extra = [s.strip() for s in (_os.environ.get("DCHUB_SELF_TRAFFIC_SESSIONS") or "").split(",")]
    # Guard the prefix length: a 1-2 char prefix would match a large share of
    # random uuids and quietly delete real sessions.
    return tuple(sorted(set(_SELF_TRAFFIC_SESSION_SEED)
                        | {s for s in extra if len(s) >= 4 and s.isalnum()}))


def external_session_predicate(col: str = "mcp_session_id") -> str:
    """TRUE when the session is NOT known operator self-traffic.

    Inlined SQL literal (no bound params) so it composes with the other
    predicates here. Prefixes are alnum-validated above, so there is nothing to
    escape; a NULL/empty session is KEPT (it is not knowably ours)."""
    prefixes = self_traffic_session_prefixes()
    if not prefixes:
        return "TRUE"
    clauses = " AND ".join(
        "COALESCE(%s,'') NOT LIKE '%s%%'" % (col, p) for p in prefixes)
    return "(" + clauses + ")"


def real_ua_predicate(col: str = "user_agent") -> str:
    """TRUE when user_agent is NOT a raw-scripting client or an internal/self UA.
    Fires regardless of client_name (the gap PLATFORM_CASE leaves). Inlined SQL
    literal — no bound params, so the literal patterns are safe next to ILIKE.
    `col` lets other tables apply the same verdict to their UA column; the
    regex form carries no literal %, so it is bound-params-safe."""
    return f"COALESCE({col},'') !~* '({_SCRIPT_INTERNAL_UA})'"


# Shared QA-tag family fragment (regex alternation, no anchors). Single source
# for internal_tag_regex_predicate() below AND the write-time normalizer, so
# the read-layer verdict and the write-layer rewrite cannot drift.
INTERNAL_TAG_FAMILIES = "dchub|verify|probe|audit|harness|test|check|diag|sweep"

import re as _re_norm  # noqa: E402 — module is otherwise pure SQL-string assembly
_INTERNAL_TAG_FAMILIES_RE = _re_norm.compile(INTERNAL_TAG_FAMILIES, _re_norm.I)


def internal_tag_regex_predicate(col: str) -> str:
    """The REGEX twin of external_platform_predicate() for callers that embed
    the predicate in psycopg2 queries WITH bound params — the LIKE form's
    literal % would be eaten by paramstyle substitution (the empty-tuple %
    trap's sibling). Rendered from the SAME constants (INTERNAL_PLATFORM_VALUES
    + the QA-tag families + REGISTRY_CRAWLER_FAMILIES), so the is_real_external
    verdict and this predicate cannot drift. Same semantics: internal families,
    registry-crawler families and exact tags excluded, length<=2 ad-hoc tags
    excluded, NULL/empty KEPT.

    ★ 2026-07-30: the crawler families were MISSING here for two days — Round 12
    added them to the LIKE form only, so every bound-params caller (high-intent
    claims, signal canonical) kept counting registry crawlers while the identity
    view excluded them. The families now come from the shared constant, and
    tests/test_registry_crawler_exclusion.py pins LIKE↔regex parity per family."""
    exact = "|".join(sorted(set(INTERNAL_PLATFORM_VALUES)))
    fams = INTERNAL_TAG_FAMILIES + "|" + REGISTRY_CRAWLER_FAMILIES
    c = f"COALESCE(LOWER({col}), '')"
    return (f"({c} !~* '({fams})' "
            f"AND {c} !~ '^({exact})$' "
            f"AND {c} !~ '^.{{1,2}}$')")


def normalize_write_platform(platform):
    """WRITE-time twin of external_platform_predicate(): rewrite junk platform
    tags to the self-describing bucket 'dchub-internal' BEFORE they land in
    mcp_tool_calls.

    r-junk-platform (2026-07-04): 'clawith' (3.4k rows/30d, python-httpx
    sweep) and 1-char tags ('v','p','t','w','c','fv', all curl/urllib debug
    clients) were landing VERBATIM in mcp_tool_calls.platform. No writer
    slices anything — server.mjs ships an unrecognized clientInfo.name through
    as the platform tag BY DESIGN ("distinct platforms before we add a rule"),
    and the Flask track/proxy writers faithfully store it. That design is right
    for plausible client names ('opencode', 'devin-…') but wrong for QA tags.

    REGISTRY_CRAWLER_FAMILIES is deliberately NOT in this rewrite's scope:
    a registry crawler is a real third-party service, not one of our own junk
    tags, and rewriting its platform to 'dchub-internal' at write time would
    falsify forensics. The read layer excludes those families regardless.

    Mapping: exact INTERNAL_PLATFORM_VALUES tags, the QA-tag families, and
    1-2 char tags → 'dchub-internal' — the read layer ALREADY excludes all
    three groups (and %dchub% catches the rewritten value), so read-time
    semantics are unchanged; the platform column just stops accumulating a
    new bucket per ad-hoc tag. Everything else passes through untouched,
    including ''/None (real anonymous agents are KEPT at read time — never
    invent a value for them). client_name keeps the raw string for forensics.
    """
    p = (platform or '').strip()
    if not p:
        return platform
    pl = p.lower()
    if pl in INTERNAL_PLATFORM_VALUES or len(pl) <= 2 \
            or _INTERNAL_TAG_FAMILIES_RE.search(pl):
        return 'dchub-internal'
    return platform


# ── Agent-grain guard (2026-07-19) ────────────────────────────────────────
# mcp_calls_identity.agent_id = md5(first X-Forwarded-For token). Until
# zone-worker v4.9.28 (2026-07-10) CF stripped XFF on worker subrequests, so
# that token was the worker's ROTATING Cloudflare egress POP — every POP
# minted a fresh pseudo-agent (132 of the 2026-07-05..12 week's 212 "distinct
# real agents" were POPs). The view now returns agent_id = NULL for these
# rows, so COUNT(DISTINCT agent_id) heals at every consumer; the CALLS stay
# counted (the traffic was real — only the agent grain was unknowable).
# Cloudflare's published IPv4 edge ranges (www.cloudflare.com/ips-v4),
# CIDR-exact. Post-fix rows never match (the worker forwards the real caller
# IP), so this is also a permanent guard if edge IPs leak back into XFF.
CF_POP_IP_REGEX = (
    r"^(104\.(1[6-9]|2[0-7])\.|172\.(6[4-9]|7[01])\.|162\.15[89]\."
    r"|141\.101\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\."
    r"|108\.162\.(19[2-9]|2[0-4][0-9]|25[0-5])\."
    r"|173\.245\.(4[89]|5[0-9]|6[0-3])\."
    r"|188\.114\.(9[6-9]|10[0-9]|11[01])\."
    r"|190\.93\.(24[0-9]|25[0-5])\.|197\.234\.24[0-3]\."
    r"|198\.41\.(12[89]|1[3-9][0-9]|2[0-4][0-9]|25[0-5])\."
    r"|131\.0\.7[2-5]\.|103\.21\.24[4-7]\.|103\.22\.20[0-3]\.|103\.31\.[4-7]\.)"
)


def agent_grain_predicate(col: str = "client_ip") -> str:
    """TRUE when `col` is a real caller IP (not a Cloudflare edge POP) — i.e.
    the row's agent_id is trustworthy as an agent identity. Consumers that
    read the mcp_calls_identity VIEW don't need this (the view already NULLs
    agent_id for POP rows); it exists for queries over raw mcp_tool_calls
    and for belt-and-braces filters. Regex form — bound-params-safe."""
    return f"{col} !~ '{CF_POP_IP_REGEX}'"


def loopback_self_predicate(col: str = "ip_address") -> str:
    """TRUE when the row's FIRST-hop IP is not loopback. A call whose XFF
    chain starts at 127.0.0.1/::1 is our own box calling itself — the
    de-loop's namesake case — yet it passed real_calls_predicate() whenever
    its UA/client_name looked clean. Measured 2026-08-15: 824 rows/30d
    (797 client_name='mcp' → the mcp-generic-client bucket, plus rows wearing
    claude/mistral/grok as client_name) counted as external from loopback;
    the identity view's is_public_ip already zeroed them out of AGENT counts,
    but the funnel's per-platform breakdown and call totals read raw
    mcp_tool_calls and rendered them as platform demand ('grok · 3 calls').
    Loopback ONLY — private/CGNAT ranges stay is_public_ip's job: a private
    first hop can front a real caller behind our proxy topology, a loopback
    first hop cannot. NULL/empty ip_address is KEPT (COALESCE): absence of an
    IP is not evidence of self-traffic, and dropping those rows would shift
    every historical count. Regex-free and bound-params-safe."""
    tok = f"TRIM(BOTH FROM split_part(COALESCE({col}, ''), ',', 1))"
    return f"{tok} NOT IN ('127.0.0.1', '::1', 'localhost')"


def real_calls_predicate() -> str:
    """Boolean SQL fragment that is TRUE for a real external tool call:
    `<PLATFORM_CASE> NOT IN (<probe list>) AND <external platform> AND
    <real UA> AND <not loopback>`. This is the exact filter the
    /api/v1/mcp/funnel endpoint FILTERs `tool_calls_7d_real` on. Reads
    mcp_tool_calls columns client_name + user_agent + platform + ip_address
    (every direct consumer queries mcp_tool_calls unaliased, audited
    2026-08-15: flask_mcp_endpoints, funnel_health, canonical_funnel,
    flywheel_master_shell, render_identity_views)."""
    return (f"(({PLATFORM_CASE.strip()} NOT IN ({_probe_in_list()})) "
            f"AND {external_platform_predicate()} "
            f"AND {real_ua_predicate()} "
            f"AND {loopback_self_predicate()})")


def probe_calls_predicate() -> str:
    """Boolean SQL fragment TRUE for a probe/self call (the complement of
    real_calls_predicate over the same classifier)."""
    return f"({PLATFORM_CASE.strip()} IN ({_probe_in_list()}))"


def deloop_calls_where() -> str:
    """The de-loop WHERE fragment (NO leading AND) used as
    `... WHERE created_at >= ... AND <deloop_calls_where()>`. Identical
    predicate to real_calls_predicate() — same classifier, same probe list —
    so funnel_health.tool_calls_7d_real == the endpoint's tool_calls_7d_real.

    No %s placeholders: the clause is inlined SQL literals over trusted
    constants, so the literal % in the ILIKE patterns are left alone."""
    return real_calls_predicate()


# ── THE canonical external-agent activity count (r-agent-parity 2026-07-31) ──
#
# Three public surfaces published three different "distinct agents (7d)"
# numbers on the same day (measured 2026-07-31): the /ai header badge said 64
# (reach_weekly ISO-week rollup), the /ai tool-use widget said 95
# (mcp_calls_identity view, rolling 7d), and /api/v1/mcp/funnel said 129
# (COUNT(DISTINCT raw ip_address strings) — XFF chains count once per chain
# form — under the live predicate only). Same reader-facing quantity, three
# (identity × exclusion × window) tuples. Shell #44's agent-parity lane names
# the fix: ONE canonical count, imported by every surface.
#
# The canonical definition is the one reference_dchub_agent_count_canonical_
# wiring established 2026-07-01: the mcp_calls_identity VIEW, is_public_ip AND
# is_real_external, identity = agent_id. Surfaces that need a different window
# (e.g. the reach_weekly WoW trend) may keep it, but must label the window and
# publish this rolling-7d figure alongside for parity.

CANONICAL_AGENTS_BASIS = (
    "COUNT(DISTINCT agent_id) / COUNT(*) FROM mcp_calls_identity WHERE "
    "is_public_ip AND is_real_external, rolling window ending now. Identity: "
    "agent_id = md5(first public X-Forwarded-For token), NULL for Cloudflare "
    "POP ranges (edge proxies never count as agents); is_public_ip drops "
    "private/CGNAT ranges. Exclusions: is_real_external = this module's "
    "real_calls_predicate() rendered into the view, plus the view's internal-"
    "IP and scripted-UA backstops — crawlers, probes, QA harnesses and "
    "self-traffic are OUT. calls counts every real-external call (including "
    "CF-POP rows that carry no agent identity). An IP-derived PROXY for "
    "agents: NAT under-counts, rotating egress over-counts."
)


def canonical_external_activity_sql(days: int = 7, offset_days: int = 0) -> str:
    """THE one agent-count query every public 'distinct agents' display must
    run (aliases: agents, calls — works with tuple and dict cursors). Any
    surface counting agents some other way (raw ip_address strings,
    session_id, reach_weekly without a window label) is the drift class this
    helper retires. tests/test_canonical_counts_drift.py pins the emitters.

    offset_days shifts the window BACK by that many days, so the prior period
    can be measured on exactly this basis:
        canonical_external_activity_sql(7)            -> last 7d
        canonical_external_activity_sql(7, 7)         -> the 7d before that
    ★ Added 2026-08-03 because the funnel dashboard rendered `76 agents` from
    THIS query beside `8,652 calls` + `+323.7%` from a DIFFERENT one
    (mcp_tool_calls, complete-days, its own predicate). Two bases on one card
    is the drift this helper exists to retire — but there was no way to state
    a prior window here, so the WoW had to be borrowed from the other basis.
    ★ The offset_days=0 string is byte-identical to the pre-2026-08-03 output
    ON PURPOSE: test_canonical_counts_drift.py pins emitters against it, and a
    whitespace change would read as every caller drifting at once."""
    d = int(days)  # int() so the fragment stays literal-only (no bound params)
    o = int(offset_days)
    head = (
        "SELECT COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS calls "
        "FROM mcp_calls_identity "
        "WHERE is_public_ip AND is_real_external "
    )
    if o:
        return (head
                + f"AND created_at >= now() - interval '{d + o} days' "
                + f"AND created_at < now() - interval '{o} days'")
    return head + f"AND created_at >= now() - interval '{d} days'"


def canonical_external_complete_week_sql(weeks_back: int = 0) -> str:
    """Same population as canonical_external_activity_sql, but a COMPLETE
    ISO week (Mon 00:00 → Sun 24:00) instead of a rolling window.

    ★ WHY THIS EXISTS (2026-08-08). The funnel headlined
      `35 agents · WoW -65.3%` from rolling-7d vs the prior rolling-7d, and it
      was arithmetic, not a business event: the PRIOR window contained an
      outlier day (2026-07-28: 30 distinct agents, ~3x its neighbours) and the
      current one did not. On COMPLETE ISO weeks the same population reads
      62 -> 85 agents = **+37%**. A rolling distinct-count comparison is
      dominated by whichever outlier days happen to sit inside each window, so
      it cannot answer "are we growing"; a complete-week comparison can.

      This is the THIRD surface in one day to publish a scary number produced
      by comparing windows of unequal composition (the others: a trailing-7d
      agent series, and a partial current week). The fix everywhere is the
      same — compare like with like, and never headline a window that includes
      today against one that does not.

    weeks_back=0 -> the most recent COMPLETE week (never the partial current
    one); 1 -> the week before that. The current partial week is excluded by
    construction, which is the point.
    """
    w = int(weeks_back)  # int() so the fragment stays literal-only
    # date_trunc('week', now()) is this week's Monday 00:00 — the boundary the
    # partial current week starts at. Everything below it is complete.
    end_expr = f"date_trunc('week', now()) - interval '{w} weeks'"
    start_expr = f"date_trunc('week', now()) - interval '{w + 1} weeks'"
    return (
        "SELECT COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS calls "
        "FROM mcp_calls_identity "
        "WHERE is_public_ip AND is_real_external "
        f"AND created_at >= {start_expr} "
        f"AND created_at < {end_expr}"
    )


CANONICAL_TOP_CALLER_BASIS = (
    "MAX(calls) per agent_id over agent_id IS NOT NULL / COUNT(*) FROM "
    "mcp_calls_identity WHERE is_public_ip AND is_real_external, rolling "
    "window ending now — the SAME table, window and exclusions as "
    "real_external_calls_7d, so pct is exactly calls / real_external_calls_7d "
    "and a reader who does the division gets the printed number. NUMERATOR "
    "excludes the NULL agent_id bucket (Cloudflare POP ranges are edge "
    "proxies, not a caller), so a POP bucket can never be published as 'the "
    "top caller'. DENOMINATOR keeps every real-external row, CF-POP rows "
    "included, so it stays identical to the published calls figure. callers "
    "= COUNT(DISTINCT agent_id), the same population as "
    "real_external_agents_7d. An IP-derived PROXY: NAT under-counts "
    "concentration, rotating egress over-counts it."
)


# ★ Added 2026-08-05. `real_external_signals_7d` (formerly `real_external_7d`)
# is NOT a call count and never was. It counts rows in the mcp_funnel_real
# VIEW, which selects from **mcp_upgrade_signals** — paywall/upgrade signals.
# main.py runs the byte-identical query under the honest name
# `2_paywall_hits_7d`. Published without a basis string and named four
# characters away from real_external_calls_7d, it read as "the same thing,
# shorter", and the two moved in OPPOSITE directions in one payload
# (2026-08-05: calls 6,868 +87.4% WoW vs signals 1,566 -13.7% WoW). A board
# binding the wrong one reads the week as shrinking instead of doubling.
# This string exists so no reader has to guess which population they hold.
CANONICAL_SIGNALS_BASIS = (
    "COUNT(*) FROM mcp_funnel_real, rolling window ending now. mcp_funnel_real "
    "is a VIEW over **mcp_upgrade_signals** — this counts PAYWALL/UPGRADE "
    "SIGNALS (an agent hit a gated tool), NOT tool calls. It is NOT a subset "
    "of real_external_calls_7d and must never be compared to it: different "
    "table, different unit, different denominator, and the two routinely move "
    "in opposite directions. One call can raise zero or one signal, and a "
    "signal can outlive the call that raised it. For weekly external CALL "
    "volume use real_external_calls_7d (mcp_calls_identity, agent_id, same "
    "rolling window). Exclusions: the view's own mcp_client deny-list "
    "(self-heal, probes, QA harnesses, registry scanners) — NOT this module's "
    "real_calls_predicate(), so the exclusion sets are related but not equal."
)


def canonical_top_caller_sql(days: int = 7, offset_days: int = 0) -> str:
    """THE one single-caller-concentration query every surface must run
    (aliases: top_calls, calls, callers — tuple and dict cursors both work).

    ★ Added 2026-08-05. The funnel card rendered a headline of
    real_external_calls_7d (6,705 — mcp_calls_identity, agent_id, rolling)
    and, ON THE SAME LINE, "top caller 34.6% of external calls (2,478
    calls)" computed from a DIFFERENT lineage (mcp_tool_calls, ip_address,
    complete-days, its own 7,159 denominator). 2,478/6,705 = 37.0%, so the
    line contradicted itself: any reader doing the division got a different
    answer than the line stated. Meanwhile the admin concentration lane
    (routes/agent_retention_master_shell.py) ran a THIRD variant and read
    38.0%. Measured 2026-08-05, the public-vs-admin denominator gap was 453
    calls: 143 (31.6%) window phase, 310 (68.4%) BASIS. Both windows are
    exactly 168h wide — complete-days is phase-shifted, NOT truncated — so
    this is a basis defect, not a window defect, and aligning the basis is
    what fixes it.

    Emitting numerator AND denominator from ONE query is the point: it is
    not possible for a caller to pair this numerator with a denominator
    from somewhere else, which is exactly how the contradiction arose.

    offset_days shifts the window BACK by that many days, matching
    canonical_external_activity_sql's signature so the prior period can be
    measured on this same basis."""
    d = int(days)  # int() so the fragment stays literal-only (no bound params)
    o = int(offset_days)
    if o:
        window = (f"AND created_at >= now() - interval '{d + o} days' "
                  f"AND created_at < now() - interval '{o} days' ")
    else:
        window = f"AND created_at >= now() - interval '{d} days' "
    return (
        "WITH per AS ("
        "  SELECT agent_id, COUNT(*) AS n "
        "  FROM mcp_calls_identity "
        "  WHERE is_public_ip AND is_real_external "
        + window
        + "  GROUP BY agent_id) "
        "SELECT COALESCE(MAX(n) FILTER (WHERE agent_id IS NOT NULL), 0) "
        "         AS top_calls, "
        "       COALESCE(SUM(n), 0) AS calls, "
        "       COUNT(*) FILTER (WHERE agent_id IS NOT NULL) AS callers "
        "FROM per"
    )
