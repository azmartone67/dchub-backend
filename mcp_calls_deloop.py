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
)


def _internal_platform_list() -> str:
    return ",".join("'" + str(p).replace("'", "''") + "'"
                    for p in sorted(set(INTERNAL_PLATFORM_VALUES)))


def external_platform_predicate() -> str:
    """TRUE when the write-time `platform` column is NOT one of our own
    internal/self/probe services. `dchub-%` covers selfheal / mcp-test /
    regression-test; the explicit list covers named probes + short test-client
    names. NULL/empty platform is KEPT (real anonymous agents)."""
    p = "COALESCE(LOWER(platform), '')"
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
    return (f"({p} NOT LIKE '%dchub%' "
            f"AND {p} NOT IN ({_internal_platform_list()}) "
            f"AND {p} NOT LIKE '%verify%' "
            f"AND {p} NOT LIKE '%probe%' "
            f"AND {p} NOT LIKE '%audit%' "
            f"AND {p} NOT LIKE '%harness%' "
            f"AND {p} NOT LIKE '%test%' "
            f"AND {p} NOT LIKE '%check%' "
            f"AND {p} NOT LIKE '%diag%' "
            f"AND {p} NOT LIKE '%sweep%' "
            f"AND ({p} = '' OR LENGTH({p}) > 2))")


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
_SCRIPT_INTERNAL_UA = (
    "python-httpx|python-urllib|urllib|curl/|wget|libwww|node-fetch|undici|axios|"
    "got/|go-http|okhttp|java/|requests/|aiohttp|scrapy|httpie|restsharp|"
    "dchub-|dchubhealer|self.?heal|value-harness|regression|brain-radar|uptimerobot"
)


def real_ua_predicate() -> str:
    """TRUE when user_agent is NOT a raw-scripting client or an internal/self UA.
    Fires regardless of client_name (the gap PLATFORM_CASE leaves). Inlined SQL
    literal — no bound params, so the literal patterns are safe next to ILIKE."""
    return f"COALESCE(user_agent,'') !~* '({_SCRIPT_INTERNAL_UA})'"


def real_calls_predicate() -> str:
    """Boolean SQL fragment that is TRUE for a real external tool call:
    `<PLATFORM_CASE> NOT IN (<probe list>) AND <external platform> AND <real UA>`.
    This is the exact filter the /api/v1/mcp/funnel endpoint FILTERs
    `tool_calls_7d_real` on. Reads mcp_tool_calls columns client_name +
    user_agent + platform."""
    return (f"(({PLATFORM_CASE.strip()} NOT IN ({_probe_in_list()})) "
            f"AND {external_platform_predicate()} "
            f"AND {real_ua_predicate()})")


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
