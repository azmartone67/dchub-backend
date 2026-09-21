"""routes/handoff_definition.py — ONE WRITER for the human_acted definition.

★ WHY THIS MODULE EXISTS (r-definition-one-writer, 2026-08-18).

The agent→human funnel's `human_acted` stage has been redefined four times, and
each time the redefinition was declared honestly in ONE place — the payload's
`definitions.human_acted.{definition_version, definition_changelog}` — while
three other surfaces RESTATED it in prose and were not updated with it:

  · ai.html's handoff-funnel card said "Human acted is DEFINITION v3: it reads
    BOTH human artifacts" on the PUBLIC dashboard, the day after the API
    started publishing v4. It described a superseded definition and omitted the
    v4 exclusion entirely.
  · routes/adoption_master_shell.py said "DEFINITION v3" in its module
    docstring and "definition v2" in its rendered cv_gate detail — two
    different versions in one file, neither of them current.
  · routes/handoff_truth_master_shell.py carried a bracketed
    "[2026-08-16: the funnel is at DEFINITION v3 …]" patch on top of a
    sentence that says v2.

This is the same defect class as the three value-pinned guards found on
2026-08-17 (mpp-consent pinned an arity, mpp-undercap pinned a literal recipe
string, test_handoff_truth_shell pinned '"definition_version": 2'): a value the
system already publishes gets restated somewhere else, and the copy rots
silently because nothing connects it to the original. It is also the same shape
as the stale leak diagnosis this card already carries two comments about — a
stale description on a dashboard does not merely fail to help, it manufactures
wrong conclusions in everyone who reads it (five AI platforms quoted the
2026-08-04 version back verbatim and each recommended work that had already
shipped).

So the version, the changelog and the SQL that COUNTS the stage all live here,
and every surface derives from this module rather than describing it. A
consumer that needs a human-readable sentence BUILDS one with
`human_acted_sentence()`; it does not type a version number.

★ THE GUARD. tests/test_published_definition_not_restated.py fails when a
literal "DEFINITION v<N>" (or "definition v<N>") appears in a user-facing
string in the funnel shells or in ai.html, and asserts each surface's rendered
version equals this module's. Rendering `"DEFINITION v%d" % version` is fine —
there is no digit in the source to go stale. Typing `v4` is not.

This module is a LEAF on purpose: pure data plus SQL-string assembly, no Flask,
no DB connection, no import of flask_mcp_endpoints (which raises at import time
without NEON_DATABASE_URL). Anything can import it, including a test.
"""
from __future__ import annotations

from datetime import date

from mcp_calls_deloop import (
    external_session_predicate as _external_session_predicate,
    real_ua_predicate as _real_ua_predicate,
)

# ── the published definition ────────────────────────────────────────────────
# Bump BOTH together. The guard in tests/test_handoff_truth_shell.py asserts
# every version 1..N carries an entry, so a bump with no explanation fails.
HUMAN_ACTED_DEFINITION_VERSION = 9

HUMAN_ACTED_DEFINITION_CHANGELOG = {
    1: (
        'first GET of the /claim page (claim_page_opened_at). '
        'Structurally unmeasurable: the single-use token was '
        'auto-redeemed by the gateway in median 0.85s, so a human click '
        'could only land on a 410 — fired 0x all-time.'
    ),
    2: (
        'first open of the HUMAN-audience view link (/relay/<token>: '
        '7-day TTL, multi-open, binds nothing on open; '
        'human_view_first_opened_at). Instrument live 2026-07-30 — the '
        'stage measures human attention for the first time, so windows '
        'spanning that date mix an unmeasurable stage with a measurable '
        'one. v1 kept alongside as human_acted_legacy_claim_page.'
    ),
    3: (
        "union of BOTH human artifacts' first-opens, real UAs only. "
        'Instruments: (a) /relay/<token> — '
        'mcp_high_intent_sessions.human_view_first_ua, stamped by '
        'relay_view on the first real-UA open (pre-v3 stamps carry no '
        'UA and are excluded; all 4 all-time were verified probes — '
        'cursor render-verify, Grok probes, an indexer); (b) '
        '/upgrade/h/<payload>.<sig> — the for_your_human link agents '
        'actually show humans — relay_opens rows '
        '(routes/human_relay.py) joined on session_id = mcp_session_id '
        "(the token payload's decoded sid). v2 read only artifact (a), "
        'so a real click on (b) could not move the dashboard. Probe '
        'exclusion: mcp_calls_deloop.real_ua_predicate, the canonical '
        'UA families. Instrument live 2026-08-16; v2 kept alongside as '
        'human_acted_v2_all_view_opens.'
    ),
    4: (
        'v3 minus declared OPERATOR self-traffic. v3 excluded probes by '
        'UA but not the operator: on 2026-08-17 this stage went 0 → 1 '
        'for the first time in its life and the 1 was a deliberate '
        "verification open, from the operator's own browser, on the "
        "operator's own session (88e20dac). A first non-zero on a stage "
        "that has never fired reads as 'the handoff converted', so it "
        'must not be us. The exclusion is a NAMED FACT, not an '
        "inference — the operator's agent client writes "
        "mcp_client='claude' / user_agent='node', byte-identical to a "
        'prospect, and inventing a behavioural rule would delete real '
        'leads. Sessions listed in '
        'mcp_calls_deloop.self_traffic_session_prefixes '
        '(env-extensible); what was removed is published under '
        '`excluded`, and v3 is kept alongside as '
        'human_acted_v3_including_self_traffic.'
    ),
    5: (
        "v4 with the operator's ROTATED session excluded too. v4 named one "
        'session (88e20dac) and the operator\'s client minted a new id on '
        '2026-08-20 — 8c8e1d0d, first call 10.1s after 88e20dac\'s last, '
        'same platform/user_agent/tier — which then opened a relay link for '
        'analyze_site, the same tool as the declared 08-17 operator open. '
        'That single open was publishing human_acted = 1 over 30d: the '
        'first non-zero this stage has ever shown, and v4 existed precisely '
        'to stop that being us. ★ UNLIKE v4 THIS IS AN INFERENCE, NOT A '
        'NAMED FACT — asked whether the 08-20 click was theirs the operator '
        'said they had no idea, so it rests on the 10-second session '
        'rotation and nothing stronger. Recorded as an inference rather '
        'than promoted to a fact. v4 is kept alongside as '
        'human_acted_v4_before_rotation and the removal is published under '
        '`excluded`, so a reader who disagrees can add it back. Declared, '
        'like every other exclusion, in '
        'mcp_calls_deloop.self_traffic_session_prefixes (env-extensible). '
        '★ The seed '
        'is a hand-curated list of session-id prefixes and it went stale '
        'once already; relay_opens records no platform or IP, so nothing '
        'structural catches the next rotation. Treat a 0 on this stage as '
        '"none of the sessions we have named", not as "verified external".'
    ),
    6: (
        'DEFINED, NEVER PROMOTED. human_acted_v6_from_relay_opens (2026-09-07) '
        'counts FROM relay_opens, the table the /upgrade/h/ open is written '
        'to, instead of FROM mcp_high_intent_sessions. Published alongside and '
        'kept off the headline on purpose: a stage that had already published '
        'a wrong first non-zero twice does not get a new anchor before that '
        'anchor is read against live data. Recorded so every version 1..N '
        'stays described; the headline never carried it.'
    ),
    7: (
        'DEFINED, NEVER PROMOTED. human_acted_v7_from_checkout_clicks '
        '(2026-09-10) counts signed, real-UA clicks FROM mcp_checkout_clicks, '
        'the table /go/c/<token> writes, restricted to ref_kind=session. '
        'Published alongside; the headline stayed on v5 until v8.'
    ),
    8: (
        'v5 UNION v7, so the link agents actually relay can move the '
        'headline. Measured 2026-09-09 from outside: one gated tools/call '
        'puts https://dchub.cloud/go/c/<token> in content[0].text, the block '
        'a client renders and a model relays, and the /upgrade/h/ link only in '
        'structuredContent. v5 read relay_opens, which only /upgrade/h/ '
        'writes, so a human clicking the link their agent showed them could '
        'not move this stage. v8 counts DISTINCT session ids over the UNION of '
        'two lanes, each exactly as already published and read against live '
        'data: (a) the v5 lane, sessions in mcp_high_intent_sessions that '
        'opened /relay/<token> or /upgrade/h/ on a real UA, windowed on '
        'first_hit_at and kept alongside as '
        'human_acted_v5_before_relayed_checkout; (b) the v7 lane, signed '
        '/go/c/ clicks on a real UA whose ref is a bare session id, windowed '
        'on clicked_at. A session in both lanes counts once. Both lanes apply '
        'the operator self-traffic exclusion declared in '
        'mcp_calls_deloop.self_traffic_session_prefixes, each on its own '
        'identity column. ★ LEFT OUT ON PURPOSE: pk-/k- key-hash refs and a- '
        'anonymous refs, because that exclusion passes vacuously on them (they '
        'stay published as human_acted_v7_links_clicked); and the v6 '
        'relay_opens anchor, which read 1 over 30d at 15:00 PT on 2026-09-13 '
        'on a session this change did not attribute, so promoting it would '
        'publish an unexamined non-zero. ★ Promoting v7 moved no published number: '
        'on 2026-09-13 it read 0 over 24h, 7d and 30d. This changes what the '
        'stage CAN count, not what it has counted.'
    ),
    9: (
        'v8 with the /go/c/ lane keyed on the SESSION a click is bound to, not '
        'only on a bare session ref. On 2026-09-13 the operator\'s own keyed '
        'test click on /go/c/ carried a durable-key ref (k-/pk-), which v8 '
        'could not count: its lane was restricted to ref_kind=session because '
        'the operator self-traffic exclusion declared in '
        'mcp_calls_deloop.self_traffic_session_prefixes keys on session ids and '
        'passes vacuously on a key hash. The MCP server now appends the '
        "caller's session id to the token beside a key (plan|ref|sid), and "
        'routes/checkout_click_tracker stores it in '
        'mcp_checkout_clicks.session_id. v9 reads ONE identity, '
        'handoff_definition.RELAYED_CHECKOUT_SESSION_ID: that session_id when '
        'present, else the ref when it is a bare session id (the v8 identity). '
        'The lane filter, the exclusion, the distinct count, the headline union '
        'and the per-session predicate all bind to it, so a keyed click counts '
        'exactly when it carries a session the exclusion can test. The relay '
        'lane is unchanged and still published as '
        'human_acted_v5_before_relayed_checkout. ★ STILL LEFT OUT: pk-/k-/a- '
        'clicks WITHOUT a session id, on which the exclusion would pass '
        'vacuously; they stay in human_acted_v7_links_clicked. ★ This moves no '
        'published number until the MCP server mints the session field: rows '
        'written before that carry session_id NULL and fall back to the v8 '
        'identity. relayed_checkout_provenance.'
        'minted_link_clicks_session_from_token shows when token-bound sessions '
        'start arriving.'
    ),
}


def human_acted_definition() -> dict:
    """The block published at `definitions.human_acted` in the funnel payload.

    Copies are handed out so a consumer that mutates what it renders cannot
    edit the canon for everybody else in the same worker process.
    """
    return {
        "definition_version": HUMAN_ACTED_DEFINITION_VERSION,
        "definition_changelog": dict(HUMAN_ACTED_DEFINITION_CHANGELOG),
    }


def human_acted_sentence(block: dict | None = None, *, prefix: str = "") -> str:
    """A human-readable sentence BUILT from the published block.

    `block` lets a caller pass a payload it actually received (the /ai card's
    equivalent is `humanActedSentence()` in ai.html, which reads the fetched
    JSON) so the sentence describes the version that surface is really showing.
    Passing nothing describes this process's canon.

    A version with no changelog entry renders as UNDESCRIBED rather than
    silently dropping to the previous entry: a stage whose definition is
    unexplained is not the same as a stage still on the old definition, and
    substituting one for the other is how the last three copies rotted.
    """
    block = block or human_acted_definition()
    version = block.get("definition_version")
    changelog = block.get("definition_changelog") or {}
    if not isinstance(version, int):
        return (prefix + "human_acted's definition version is UNREADABLE in "
                "the payload — no version is asserted here.")
    # JSON round-trips integer keys to strings; accept either.
    entry = changelog.get(version, changelog.get(str(version)))
    head = prefix + "human_acted is at DEFINITION v%d" % version
    if not entry:
        return (head + " — UNDESCRIBED: the published definition_changelog "
                "carries no entry for this version, so nothing is restated "
                "here in its place.")
    return head + " (as published, not restated): " + entry


# ── the SQL that COUNTS the stage ───────────────────────────────────────────
# Same reasoning one level down: a surface that re-derives the count is a
# second writer, and the adoption shell proved it — its cv_gate reported a bare
# `human_view_first_opened_at IS NOT NULL` count (the v2 instrument) under the
# label "HUMAN ACTED", so the board's headline handoff number and the funnel's
# disagreed by construction while both called themselves human_acted.


def human_acted_relay_predicate(alias: str = "s") -> str:
    """TRUE when session `alias` opened /relay/<token> or /upgrade/h/ on a real UA.

    This is the v3 body — the headline's relay lane — WITHOUT the v4
    self-traffic exclusion, so callers can express both the filtered stage and
    the unfiltered diagnostic from one place. Carries no literal `%`: both predicates are the anchored
    regex forms, which is load-bearing beside `sql % iv` window interpolation
    and beside psycopg2 bound params (see external_session_predicate's docstring
    — the LIKE form took this endpoint down inside one deploy).
    """
    hv_real = _real_ua_predicate("%s.human_view_first_ua" % alias)
    ro_real = _real_ua_predicate("ro.user_agent")
    return (
        "((%(a)s.human_view_first_opened_at is not null and "
        "%(a)s.human_view_first_ua is not null and " + hv_real + ") "
        "or exists (select 1 from relay_opens ro where "
        "ro.session_id = %(a)s.mcp_session_id and ro.session_id <> '' "
        "and " + ro_real + "))"
    ) % {"a": alias}


def human_acted_session_predicate(alias: str = "s") -> str:
    """TRUE when session `alias` acted on ANY human artifact, on a real UA.

    The relay predicate OR a signed /go/c/ click bound to this session — the
    session its token carried, else a bare session ref
    (RELAYED_CHECKOUT_SESSION_ID): the per-session form of the headline, for
    readers that start FROM mcp_high_intent_sessions. adoption_master_shell's
    `abandoned` reads it, and a session that clicked the relayed checkout link
    did not abandon. No window on the click, as the relay_opens exists carries
    none on the open.
    """
    return ("(" + human_acted_relay_predicate(alias)
            + " or exists (select 1 " + _RELAYED_CHECKOUT_FROM + " where "
            + relayed_checkout_session_filters()
            + " and " + RELAYED_CHECKOUT_SESSION_ID + " = "
            + alias + ".mcp_session_id))")


def human_acted_not_self_predicate(alias: str = "s") -> str:
    """The v4 delta: TRUE when the session is not declared operator traffic."""
    return _external_session_predicate("%s.mcp_session_id" % alias)


def _relay_lane_body(interval_sql: str, include_self_traffic: bool) -> str:
    body = ("from mcp_high_intent_sessions s "
            "where s.first_hit_at > now() - interval '" + interval_sql +
            "' and " + human_acted_relay_predicate("s"))
    if not include_self_traffic:
        body += " and " + human_acted_not_self_predicate("s")
    return body


def human_acted_v5_count_sql(interval_sql: str, *,
                             include_self_traffic: bool = False) -> str:
    """The relay lane alone, published as human_acted_v5_before_relayed_checkout.

    `include_self_traffic=True` renders the v3 diagnostic that must stay
    published beside it — never a silent subtraction.
    """
    return ("select count(distinct s.mcp_session_id) "
            + _relay_lane_body(interval_sql, include_self_traffic))


def human_acted_count_sql(interval_sql: str, *,
                          include_self_traffic: bool = False) -> str:
    """Canonical human_acted count over `interval_sql` (e.g. "30 days").

    DISTINCT session ids over the UNION of the relay lane and the
    relayed-checkout lane (see the changelog entry for the current version).
    A session in both counts once; each lane keeps the window its own
    instrument publishes. `include_self_traffic=True` drops the exclusion from
    BOTH lanes, so the difference is exactly what the exclusion removed and
    cannot go negative.
    """
    return ("select count(distinct u.sid) from (select s.mcp_session_id as sid "
            + _relay_lane_body(interval_sql, include_self_traffic)
            + " union select " + RELAYED_CHECKOUT_SESSION_ID + " as sid "
            + _relayed_checkout_lane_body(interval_sql, include_self_traffic)
            + ") u")


# ── the redeem stage: a MACHINE diagnostic, never funnel progress ───────────
# r-redeem-not-a-leak (2026-08-21). `redeemed` counts
# mcp_high_intent_sessions.claim_used_at, and the funnel ranked
# `relay→redeemed` as its biggest_leak. Both halves of that are wrong, and the
# second one is wrong permanently:
#
#   · WHAT THE STAGE MEASURES. claim_used_at's only writer at scale was the
#     mcp-server's _autoRedeemClaim — the server redeeming its OWN token,
#     server-side, with no human and no browser. Measured mint→redeem gap:
#     median 0.72s, 122 of 132 inside 2s (GET /api/v1/admin/relay-watch's own
#     histogram; r-stop-arbitrage measured 95.9% at 0.79s median over 560
#     mints). A stage a machine completes for itself in under a second was
#     never evidence that a human progressed.
#
#   · WHY IT NOW READS AS A CLIFF. r-stop-arbitrage (dchub-mcp-server,
#     2026-08-16) made auto-redeem opt-in: _autoRedeemEnabled() requires
#     DCHUB_AUTO_REDEEM_ENABLE=1, and that variable is not set in production.
#     Stamps went 135 in the week to 08-16 to ZERO from 08-17 on. So
#     `(used or 0) < (minted or 0) * 0.5` is now true for every window that
#     does not reach back before the cutoff, and biggest_leak had degenerated
#     into a CONSTANT that says "relay→redeemed" forever, with
#     relay_to_redeemed_pct: 0.0 beside it reading as catastrophic.
#
# A deliberately disabled machine step, published as the funnel's biggest
# leak, inverts a shipped fix into a regression. It cost a full analysis cycle
# on 2026-08-21 — the same defect class this module already exists to stop, one
# metric over: a surface asserting something the data no longer supports.
#
# The stage is NOT deleted. It stays published, with this basis attached, as
# the machine-arbitrage diagnostic it always was. It is removed from the
# progress ladder only.
REDEEM_STAGE_IS_FUNNEL_PROGRESS = False

REDEEM_INSTRUMENT_DISABLED_ON = date(2026, 8, 16)

REDEEM_STAGE_BASIS = (
    "claim_used_at. NOT funnel progress and never was: its writer at scale was "
    "the mcp-server's own _autoRedeemClaim (median mint-to-redeem gap 0.72s, "
    "122 of 132 inside 2s) — the server redeeming its own token, no human, no "
    "browser. r-stop-arbitrage made auto-redeem opt-in on 2026-08-16 "
    "(DCHUB_AUTO_REDEEM_ENABLE, unset in production), so stamps stop from "
    "2026-08-17 on. Published as a machine-arbitrage diagnostic; excluded from "
    "biggest_leak, because a switched-off machine step ranked as the biggest "
    "leak reads as a regression when it is a fix. The stage that measures "
    "human progress is human_acted."
)


def live_high_intent_threshold():
    """The RUNNING process's high-intent entry threshold, or None.

    r-threshold-drift (2026-09-03). Same defect class as this module's reason
    for existing, one layer down: the published high_intent basis ASSERTED
    "a session that makes exactly one gated call never enters this table",
    true only at threshold >= 2. HIGH_INTENT_THRESHOLD is read from
    DCHUB_HIGH_INTENT_THRESHOLD at MODULE IMPORT, prod overrides the code
    default of 2, and prod ran on 1 — so the sentence was false and nothing
    connected it to the value it restated.
    """
    try:
        from routes.mcp_high_intent_claim import HIGH_INTENT_THRESHOLD as _t
        return int(_t)
    except Exception:
        return None


def high_intent_basis(threshold) -> str:
    """Render the published high_intent basis FROM a threshold value.

    Pure, and it lives HERE rather than beside the endpoint for the reason the
    module docstring gives: a definition that restates a value rots. Keeping it
    importable without a DATABASE_URL also keeps its guard runnable in CI,
    where a DB-coupled test can only SKIP — a silent green, not a verdict.
    """
    return (
        "COUNT(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions WHERE "
        "first_hit_at is in the window. Entry requires paid_call_count_24h >= "
        + (str(threshold) if threshold is not None else "the configured threshold")
        + " in a rolling 24h. "
        # ★ Three cases, not two: an UNREADABLE threshold must assert neither
        # shape. Claiming repeat-use (or its opposite) off a value we could not
        # read is the same defect as hardcoding it.
        + ("The entry threshold could not be read in this process, so this "
           "stage's shape is undeclared here."
           if threshold is None else
           "At this setting a single gated call DOES enter the table — the "
           "stage is paid-tool use, NOT repeat use."
           if threshold <= 1 else
           "The stage is REPEAT paid-tool use, not a second pageview — a "
           "session that makes fewer gated calls than that never enters this "
           "table."))


def redeem_stage_basis() -> dict:
    """The block published beside `redeemed` so no consumer has to infer why
    the number fell off a cliff."""
    return {
        "is_funnel_progress": REDEEM_STAGE_IS_FUNNEL_PROGRESS,
        "instrument_disabled_on": REDEEM_INSTRUMENT_DISABLED_ON.isoformat(),
        "basis": REDEEM_STAGE_BASIS,
    }


# ── biggest_leak, as a PURE function over the published steps ───────────────
# Was an inline conditional chain at the call site. Moved here for the reason
# the rest of this module exists: it encodes which stages count as progress,
# which is a definition, and a definition with two writers rots. Pure — no DB,
# no Flask — so it is directly testable.
#
# ★ The ladder walks stages in funnel ORDER and returns the FIRST transition
# that loses more than half. Order is load-bearing: reporting a later leak
# while an earlier one is worse sends the reader downstream of the real break.
#
# ★ A RUNG IS NOT AUTOMATICALLY A CONVERSION RATE (r-filter-boundary,
# 2026-09-04). Two stages divide cleanly only when they are drawn from the
# SAME population. paywall_hit and relay_minted are not:
#
#   paywall_hit    mcp_upgrade_signals, written by server.mjs signalPaywall()
#                  — posts UNCONDITIONALLY, no bot gate at all
#   relay_minted   mcp_high_intent_sessions, written by trackPaidHit() — gated
#                  TWICE: isBotOrInternalCtx() client-side AND
#                  _is_non_human_client() server-side, and the server-side
#                  drop answers HTTP 200 with {"skipped": ...} while the
#                  caller only logs on !resp.ok, so it leaves no trace
#
# Both gates deliberately drop internal tags, QA harnesses and scripting UAs
# (curl/, python-httpx, urllib, wget…), for a good reason: of 123 claims
# minted in a 30-day audit, ~93 were raw scripts with no human to click.
# So most of that rung's "loss" is traffic correctly excluded from the second
# stage while still counted in the first. It is a FILTER BOUNDARY, and the
# percentage across it is not a conversion rate.
#
# The rung is still REPORTED — suppressing the largest arithmetic drop would
# hide a real number — but it now carries `same_population: false` and the
# reason, so "91.6% lost" cannot be read as 91.6% of prospects giving up. The
# public /ai card printed exactly that as "Biggest leak" and it is the line a
# partner quotes back.
#
# The last three rungs all sit INSIDE mcp_high_intent_sessions (or its relay
# join), so they compare like with like and their percentages are real.
_SAME_POP = None
_PAYWALL_BOUNDARY = (
    "NOT a conversion rate — these two stages are drawn from different "
    "populations. paywall_hit is written by signalPaywall() with no bot gate; "
    "relay_minted comes from mcp_high_intent_sessions, whose writer "
    "trackPaidHit() is gated twice (isBotOrInternalCtx client-side, "
    "_is_non_human_client server-side, the latter answering HTTP 200 with "
    "`skipped` so the drop leaves no trace). Both gates deliberately exclude "
    "internal tags, QA harnesses and raw scripting UAs, which a 30-day audit "
    "measured at ~93 of 123 minted claims. Most of the drop across this rung "
    "is that exclusion, not prospects giving up. Read it as a filter "
    "boundary; the rungs below it compare like with like."
)
LEAK_LADDER = (
    ("paywall_hit", "relay_minted", "paywall→relay_mint", _PAYWALL_BOUNDARY),
    ("relay_minted", "human_acted", "relay_mint→human_acted", _SAME_POP),
    ("human_acted", "identified", "human_acted→identified", _SAME_POP),
    ("identified", "paid_attributed", "identified→paid", _SAME_POP),
)


def _leak_detail(src: str, dst: str, label: str, steps: dict,
                 boundary: str | None = None) -> dict:
    """One rung of LEAK_LADDER, with the numbers it was decided on.

    `lost_pct` is None when the upstream stage is absent or zero — you cannot
    express a loss as a fraction of nothing, and a 0→0 rung rendered as
    "100% lost" is the exact false alarm this module exists to stop.
    """
    up, down = steps.get(src), steps.get(dst)
    known = isinstance(up, (int, float)) and isinstance(down, (int, float))
    return {
        "label": label,
        "from_key": src, "to_key": dst,
        "from_value": up if known else None,
        "to_value": down if known else None,
        "lost_pct": (round((up - down) / up * 100.0, 1)
                     if known and up else None),
        "measured": bool(known),
        # ★ Whether the two stages are even comparable. False means lost_pct
        # is an arithmetic difference between two differently-filtered
        # populations, not a conversion rate — see the LEAK_LADDER comment.
        "same_population": boundary is None,
        "population_basis": boundary,
    }


def biggest_leak_detail(steps: dict) -> dict:
    """The ladder walk, published WITH the stages and numbers it chose.

    ★ WHY THIS EXISTS AND biggest_leak() ALONE DID NOT. The label is a display
      string ("relay_mint→human_acted") whose halves deliberately do NOT match
      the step keys ("relay_minted"/"human_acted"), so a consumer handed only
      the label cannot look up the counts to render a sentence about it. Every
      consumer that wanted the sentence therefore re-derived the cliff itself —
      and ai.html did exactly that, over its own stage array which still
      contained `redeemed`. It printed "Relay minted → Redeemed, 100% lost"
      while this module published "relay_mint→human_acted": a switched-off
      machine step (see REDEEM_STAGE_BASIS) headlined as the funnel's biggest
      problem, on the public page, for two days after the fix.

      That is the defect this module was created to end, one surface further
      out: a definition with two writers rots. The second writer existed
      because the payload was not renderable. This makes it renderable, so
      there is nothing left to re-derive.
    """
    for src, dst, label, boundary in LEAK_LADDER:
        upstream = steps.get(src) or 0
        downstream = steps.get(dst) or 0
        if upstream and downstream < upstream * 0.5:
            return _leak_detail(src, dst, label, steps, boundary)
    src, dst, label, boundary = LEAK_LADDER[-1]
    return _leak_detail(src, dst, label, steps, boundary)


def biggest_leak(steps: dict) -> str:
    """First transition in funnel order that loses >50%, else the last one.

    `redeemed` is deliberately absent from LEAK_LADDER — see
    REDEEM_STAGE_BASIS. A stage is only judged when the stage BEFORE it carried
    something: a 0→0 transition is not a leak, it is a funnel that never
    reached there, and calling it the biggest leak points the reader at the
    wrong end of the pipe.

    Derived from biggest_leak_detail() rather than walking the ladder a second
    time: two walks of the same ladder are two writers of the same definition.
    """
    return biggest_leak_detail(steps)["label"]


# ── THE THIRD HUMAN ARTIFACT: the relayed checkout link ─────────────────────
# r-third-artifact (2026-09-10). v3's changelog entry, still published above,
# says the stage is the "union of BOTH human artifacts' first-opens" and names
# them: /relay/<token> and /upgrade/h/<payload>.<sig>. There are THREE, and the
# one it does not name is the one an agent actually shows a human.
#
# ★ MEASURED FROM OUTSIDE, ANONYMOUSLY, 2026-09-09/10. One `tools/call` to a
# gated tool on https://dchub.cloud/mcp returns an envelope whose two halves
# carry DIFFERENT links:
#
#   content[0].text            the block a client renders and a model relays
#                              "👤 Tell your human: … → https://dchub.cloud/go/c/<token>"
#   structuredContent          .for_your_human.url =
#                              https://dchub.cloud/upgrade/h/<payload>.<sig>
#
# `human_acted` reads relay_opens, which ONLY /upgrade/h/ writes
# (routes/human_relay._log_open). /go/c/<token> is a different endpoint in a
# different module (routes/checkout_click_tracker) writing a different table
# (mcp_checkout_clicks), and the handoff funnel had no read of that table at
# all. So a human who clicks the link their agent actually put in front of
# them cannot move the stage that exists to measure exactly that — not
# "did not", CANNOT, for any click, forever.
#
# This is the v6 anchor error one table over: v6 fixed "counts from the wrong
# table for THIS artifact"; this fixes "does not count the artifact at all".
# Both hops were walked end to end on 2026-09-09 and both preserve the
# session into Stripe's client_reference_id, so this is a read-side blind
# spot and not a broken link:
#   /go/c/<t>       302 → buy.stripe.com/…?client_reference_id=<sid>
#   /upgrade/h/<t>  200 → button → /pricing/upgrade?…&sid=<sid>
#                       302 → buy.stripe.com/…?client_reference_id=mcp:…:sess=<sid>
#
# PUBLISHED ALONGSIDE, NOT PROMOTED — the same rule v6 shipped under.
# `human_acted` stays where it is. A stage that has published a wrong non-zero
# twice does not get a new artifact folded into its headline before that
# artifact's number has been read against live data.

# The one ref_kind the operator self-traffic exclusion can actually bind to.
# routes/checkout_click_tracker._ref_kind mints four:
#   'session'  a bare Mcp-Session-Id      ← keys match self_traffic_session_prefixes
#   'pack_key' 'pk-<sha256>' durable key  ← a key hash; the seed cannot test it
#   'sub_key'  'k-<sha256>'  durable key  ← ditto
#   'anon'     'a-<id>' ephemeral offer occurrence, no person and no session
# ★ THE v6 LESSON, ENCODED. external_session_predicate KEEPS anything that is
# not knowably ours, so on a 'pk-'/'k-'/'a-' ref it passes VACUOUSLY: the
# exclusion looks applied and tests nothing. v6 hit exactly this when its
# identity was widened to the token hash, and the fix was two numbers, not one
# wider number. Same here.
RELAYED_CHECKOUT_DELOOPABLE_REF_KIND = "session"

# ★ 2026-09-13 (v9) — THE ONE IDENTITY THE LANE COUNTS. A keyed caller's ref is
# a key hash, so v8 could not count its click at all. The MCP server now mints
# the caller's session beside the key (`plan|ref|sid`) and
# routes/checkout_click_tracker stores it in mcp_checkout_clicks.session_id. A
# click counts AS that session; a row without one falls back to the v8
# identity, the ref when it is a bare session id. A key or anon ref with no
# session is NULL here, and stays out for the reason above. Every consumer
# reads this one string — the lane filter, the exclusion, the distinct count,
# the headline union, the per-session predicate, the de-loopable subset — so
# the identity cannot be one thing in the count and another in the exclusion.
RELAYED_CHECKOUT_SESSION_ID = (
    "coalesce(nullif(cc.session_id,''), case when cc.ref_kind = '"
    + RELAYED_CHECKOUT_DELOOPABLE_REF_KIND + "' then nullif(cc.ref,'') end)")


_RELAYED_CHECKOUT_FROM = "from mcp_checkout_clicks cc"


def _relayed_checkout_window(interval_sql: str) -> str:
    return (_RELAYED_CHECKOUT_FROM
            + " where cc.clicked_at > now() - interval '" + interval_sql + "'")


def relayed_checkout_real_ua() -> str:
    """The SAME real-UA predicate human_acted applies, on this table's column.

    Built by CALLING mcp_calls_deloop.real_ua_predicate, never by restating
    the family list: a second copy of that regex is a second definition.
    """
    return _real_ua_predicate("cc.user_agent")


def relayed_checkout_signed() -> str:
    """TRUE when the click was on a link WE minted.

    routes/checkout_click_tracker._verify HMACs the token against
    DCHUB_INTERNAL_KEY and `sig_ok` records the verdict, so a scanner walking
    /go/c/<junk> can never land a TRUE here. NULL groups with unsigned on
    purpose: for a field whose only job is to avoid announcing a conversion
    that was not one, the conservative side is the correct side — the same
    call relay_opens.valid makes.
    """
    return "cc.sig_ok is true"


def relayed_checkout_session_filters() -> str:
    """The relayed-checkout lane's row filters: signed, real UA, a session identity.

    No window and no exclusion, so the v7 count, the headline's lane and the
    per-session predicate all read this ONE copy.
    """
    return (relayed_checkout_signed()
            + " and " + relayed_checkout_real_ua()
            + " and " + RELAYED_CHECKOUT_SESSION_ID + " is not null")


def _relayed_checkout_lane_body(interval_sql: str,
                                include_self_traffic: bool) -> str:
    body = (_relayed_checkout_window(interval_sql)
            + " and " + relayed_checkout_session_filters())
    if not include_self_traffic:
        body += " and " + _external_session_predicate(RELAYED_CHECKOUT_SESSION_ID)
    return body


def human_acted_v7_count_sql(interval_sql: str) -> str:
    """DE-LOOPABLE clicks on the relayed checkout link, over `interval_sql`.

    Counts DISTINCT RELAYED_CHECKOUT_SESSION_ID — the session a click is bound
    to — precisely BECAUSE a session id is the only identity the operator
    self-traffic exclusion can bind to. Clicks with no session identity are
    reported by human_acted_v7_links_sql, whose basis says it is
    un-de-loopable, rather than folded in here under a filter that would pass
    vacuously on them.
    """
    return ("select count(distinct " + RELAYED_CHECKOUT_SESSION_ID + ") "
            + _relayed_checkout_lane_body(interval_sql, False))


def human_acted_v7_links_sql(interval_sql: str) -> str:
    """UPPER BOUND: distinct relayed checkout links clicked, any ref_kind.

    Same window, same real-UA predicate, same signature requirement — and NO
    self-traffic exclusion, because on the rows this adds it could only pass
    vacuously. Read it as a ceiling on links clicked, never as a count of
    humans reached.
    """
    return ("select count(distinct cc.ref) "
            + _relayed_checkout_window(interval_sql)
            + " and " + relayed_checkout_signed()
            + " and " + relayed_checkout_real_ua()
            + " and coalesce(cc.ref,'') <> ''")


def relayed_checkout_provenance_branches() -> tuple:
    """The provenance split, as ((name, condition_sql), ...).

    EXHAUSTIVE AND MUTUALLY EXCLUSIVE over (real_ua, signed) by construction:
    every row is either not-real-UA, or real-UA-and-unsigned, or
    real-UA-and-signed. Exposed as parts rather than baked into one string so
    the guard can evaluate the partition instead of reading it — a split that
    is asserted in prose and wrong in SQL is how a zero gets misread.
    """
    ua, sig = relayed_checkout_real_ua(), relayed_checkout_signed()
    return (
        ("probe_ua", "not (" + ua + ")"),
        ("unsigned_clicks", "(" + ua + ") and not (" + sig + ")"),
        ("minted_link_clicks", "(" + ua + ") and (" + sig + ")"),
    )


def relayed_checkout_provenance_subsets() -> tuple:
    """Named SUBSETS of minted_link_clicks, as ((name, condition_sql), ...).

    Separate from the branches on purpose: these partition the same rows by a
    DIFFERENT question (can the exclusion bind? does the row carry an identity
    at all?), so a reader who adds one of these to a branch double-counts. The
    basis says so and test_provenance_names_the_subset_as_a_subset holds it.
    """
    minted = "(" + relayed_checkout_real_ua() + ") and (" + \
        relayed_checkout_signed() + ")"
    return (
        ("minted_link_clicks_deloopable",
         minted + " and " + RELAYED_CHECKOUT_SESSION_ID + " is not null"),
        # ★ 2026-09-10, the FIRST live read of this block: 30d showed
        # minted_link_clicks 1 beside human_acted_v7_links_clicked 0, and
        # nothing published said why. The ceiling counts DISTINCT refs and a
        # ref is the only identity a /go/c row has — routes/human_relay stores
        # a token hash as a fallback, checkout_click_tracker stores none — so a
        # signed click minted with an empty ref is real and permanently
        # uncountable. Unnamed, that pair reads as an arithmetic bug in the
        # split. This is the /go/c analogue of
        # relay_open_provenance.minted_link_opens_no_session_id, which exists
        # for exactly this reason one table over.
        ("minted_link_clicks_no_ref", minted + " and coalesce(cc.ref,'') = ''"),
        # ★ 2026-09-13 (v9): minted clicks whose session came from the token's
        # session field, not from the ref. Contained in the de-loopable subset;
        # reads 0 until the MCP server mints that field.
        ("minted_link_clicks_session_from_token",
         minted + " and coalesce(cc.session_id,'') <> ''"
         " and cc.ref_kind is distinct from '"
         + RELAYED_CHECKOUT_DELOOPABLE_REF_KIND + "'"),
    )


def relayed_checkout_provenance_sql(interval_sql: str) -> str:
    """total + the three-way split + the named subsets, in one pass.

    ★ `minted_link_clicks_deloopable` is a SUBSET of minted_link_clicks, not a
    fourth branch — it partitions by whether the self-traffic exclusion can
    bind, which is ORTHOGONAL to the real-UA/signature split. Adding one field
    from each double-counts. relay_open_provenance carries the same warning
    for the same reason and shell #54 lane G already sat red for a week over a
    shape change to a sibling of it.
    """
    parts = ["count(*) as total"]
    for name, cond in relayed_checkout_provenance_branches():
        parts.append("count(*) filter (where " + cond + ") as " + name)
    for name, cond in relayed_checkout_provenance_subsets():
        parts.append("count(*) filter (where " + cond + ") as " + name)
    return ("select " + ", ".join(parts) + " "
            + _relayed_checkout_window(interval_sql))


# ── paid_attributed: a payment joins the relayed click that sold it ─────────
# r-paid-join (2026-09-14). Through v1, `paid_attributed` read the two tables
# the payment webhook binds to a session: mcp_session_upgrades (Fix E, a bare
# session ref) and mcp_topups.mcp_session_id (the pack grant). A caller that
# holds an API key is sold through a KEY ref instead, `pk-<sha256>` for the $10
# pack and `k-<sha256>` for a subscription, and neither key branch writes a
# session: the pack grant passes mcp_session_id None, and the k- branch stamps
# mcp_dev_keys.tier and nothing else. So a keyed caller paying from an agent
# unlock could not move this stage for any purchase.
#
# The session is on record one table over. The relayed link is a signed
# /go/c/<plan|ref|sid> token; routes/checkout_click_tracker stores its ref and
# session in mcp_checkout_clicks and hands Stripe that same ref as
# client_reference_id. routes/checkout_payment_refs now keeps each paid
# checkout's client_reference_id, so the join is an equality:
#
#     mcp_checkout_payments.client_reference_id = mcp_checkout_clicks.ref
#
# ★ ONE SESSION PER PAYMENT. One key clicked from two sessions and paid once
#   is one purchase, so the payment takes the session of the LATEST qualifying
#   click at or before it, within PAID_RELAYED_CHECKOUT_LOOKBACK.
# ★ THE CLICK QUALIFIES THE WAY human_acted's /go/c lane does (signed, real UA,
#   a session identity), through relayed_checkout_session_filters(), called
#   here and never restated. A payment whose ref matches only clicks without a
#   session has nothing the operator exclusion can test; it is published in
#   relayed_checkout_payments as a ceiling and not counted.
# ★ The exclusion binds ONCE, on the union's identity, so the v1 lanes get it
#   too (they carried none).
# ── `identified` DEFINITION v2 (r-identified-union, 2026-09-17) ─────────────
#
# THE DEFECT, measured the day this shipped. The funnel published
#     paid_attributed 1  and  identified 0
# — a paying customer the funnel said we had never identified, whose email we
# hold (it is on the Stripe Checkout Session). That ordering is not possible for
# a real funnel, and the reason is that the two rungs count DIFFERENT
# POPULATIONS:
#
#   paid_attributed (v2)  DISTINCT session over a UNION of lanes, including one
#                         that reaches sessions the session-bound tables never
#                         held. It found the sale.
#   identified (v1)       mcp_high_intent_sessions.claim_email only.
#
# routes/relay_identify captured the buyer's email and reported
# `identify_captures {captured: 2, reached_the_rung: 0}`: the write succeeded
# and the UPDATE matched zero rows, because A RELAY TOKEN IS MINTED
# STATELESSLY — holding one never implied the session has a high-intent row.
# `attributable_to_a_session: 1` proves the session id itself resolved fine.
#
# ★ WHY NOT JUST INSERT THE MISSING ROW. Because mcp_high_intent_sessions is
# the DENOMINATOR of the three rungs above this one. Creating a row to make
# `identified` move would inflate paywall_hit, high_intent and relay_minted
# by the same act — buying a green number by corrupting the stages that give
# it meaning. The capture deliberately never INSERTs there.
#
# ★ THE FIX IS THE ONE PAID_ATTRIBUTED ALREADY MADE, ONE RUNG UP. v2 counts
# DISTINCT sessions holding a bound email over the UNION of both writers. No
# row is fabricated; a session already counted stays counted once.
#
# ★ `identify_captures.reached_the_rung` STAYS a separate diagnostic and will
# keep reading 0 for a capture whose session has no high-intent row. That is
# the mechanism being reported, not a failure — do not "fix" it to match
# identified.
IDENTIFIED_DEFINITION_VERSION = 2
IDENTIFIED_DEFINITION_CHANGELOG = {
    1: ("COUNT(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions WHERE "
        "claim_email is set and first_hit_at is in the window. Its only writers "
        "were the /claim page form and bind_email's side-write, so no email "
        "captured anywhere else on the human path could reach the stage — and "
        "a session with no row in that table could never reach it at all."),
    2: ("2026-09-17. COUNT(DISTINCT session) over the UNION of that column and "
        "relay_identify_captures.mcp_session_id — the relay page's email form "
        "and the paid checkout's Stripe email, both written by "
        "routes/relay_identify. A session in both lanes counts once. The "
        "operator self-traffic exclusion applies to the union, exactly as it "
        "does for paid_attributed; identified_v1_high_intent_rows publishes "
        "the previous figure beside it. WHY: the funnel read paid_attributed 1 "
        "against identified 0 — a paying customer it claimed never to have "
        "identified — because a relay token is minted statelessly and the v1 "
        "column can only be stamped on a session that already had a row."),
}


def _identified_lanes(interval_sql: str) -> list:
    """The two writers of a bound email, as union lanes.

    Windowed on each table's OWN time column: the high-intent lane on
    first_hit_at (the session's arrival, which is what every rung above it
    uses) and the capture lane on captured_at (when the email was given). Using
    one table's clock for the other would drop captures from sessions that
    arrived before the window.
    """
    return [
        ("select hs.mcp_session_id as sid from mcp_high_intent_sessions hs"
         " where hs.claim_email is not null and hs.claim_email <> ''"
         " and hs.first_hit_at > now() - interval '" + interval_sql + "'"),
        ("select ric.mcp_session_id as sid from relay_identify_captures ric"
         " where ric.email is not null and ric.email <> ''"
         " and ric.captured_at > now() - interval '" + interval_sql + "'"),
    ]


def identified_count_sql(interval_sql: str, *,
                         include_self_traffic: bool = False) -> str:
    """Canonical `identified` over `interval_sql`: both writers, DISTINCT
    sessions, operator exclusion once. `include_self_traffic=True` drops the
    exclusion, so the difference is exactly what it removed."""
    sql = ("select count(distinct u.sid) from ("
           + " union ".join(_identified_lanes(interval_sql))
           + ") u where coalesce(u.sid,'') <> ''")
    if not include_self_traffic:
        sql += " and " + _external_session_predicate("u.sid")
    return sql


def identified_v1_sql(interval_sql: str) -> str:
    """The v1 figure as it was published, kept beside the headline."""
    return ("select count(distinct mcp_session_id) from mcp_high_intent_sessions"
            " where claim_email is not null and claim_email <> ''"
            " and first_hit_at > now() - interval '" + interval_sql + "'")


def identified_capture_lane_sql(interval_sql: str, *,
                                include_self_traffic: bool = False) -> str:
    """The capture lane ALONE, published beside the headline so the reader can
    see which writer moved the stage."""
    sql = ("select count(distinct u.sid) from ("
           + _identified_lanes(interval_sql)[1]
           + ") u where coalesce(u.sid,'') <> ''")
    if not include_self_traffic:
        sql += " and " + _external_session_predicate("u.sid")
    return sql


IDENTIFIED_BASIS = (
    "COUNT(DISTINCT session) over the UNION of the two writers of a bound "
    "email: mcp_high_intent_sessions.claim_email (the /claim form and "
    "bind_email's side-write, windowed on first_hit_at) and "
    "relay_identify_captures.mcp_session_id (the relay page's email form and "
    "the paid checkout's Stripe email, windowed on captured_at). A session in "
    "both lanes counts once. The operator self-traffic exclusion applies to "
    "the union; identified_including_self_traffic drops it. WHY v2 exists: v1 "
    "read only the first column, so the funnel published paid_attributed 1 "
    "against identified 0 — a paying customer whose email we hold. A relay "
    "token is minted statelessly, so a capture can carry a valid session that "
    "has no row in mcp_high_intent_sessions; identify_captures.reached_the_rung "
    "reports exactly that subset and is NOT this number.")


def identified_definition() -> dict:
    return {
        "basis": IDENTIFIED_BASIS,
        "definition_version": IDENTIFIED_DEFINITION_VERSION,
        "changelog": IDENTIFIED_DEFINITION_CHANGELOG,
    }


PAID_ATTRIBUTED_DEFINITION_VERSION = 2
PAID_RELAYED_CHECKOUT_LOOKBACK = "7 days"
PAID_ATTRIBUTED_DEFINITION_CHANGELOG = {
    1: ("COUNT(DISTINCT mcp_session_id) FROM mcp_session_upgrades plus "
        "COUNT(DISTINCT mcp_session_id) FROM mcp_topups where it is set: a sum "
        "of two distinct counts with no operator exclusion. Neither table holds "
        "a session for a purchase made through a durable-key ref (pk- or k-)."),
    2: ("2026-09-14. COUNT(DISTINCT session) over the UNION of those two tables "
        "and the relayed-checkout lane: a paid Checkout Session in "
        "mcp_checkout_payments (livemode not false) whose client_reference_id "
        "equals the ref of a signed, real-UA /go/c/ click with a session "
        "identity, made at or before the payment and within "
        + PAID_RELAYED_CHECKOUT_LOOKBACK + " of it, counted as the latest such "
        "click's session. A session in two lanes counts once. The operator "
        "self-traffic exclusion applies to the whole union; "
        "paid_attributed_v1_session_rows publishes the previous figure."),
}

def _paid_payments_sql(interval_sql: str) -> str:
    """Paid checkouts in the window, as the derived table `pay`.

    One string literal (adjacent literals, no `+`), keywords in UPPERCASE, on
    purpose: scripts/dataset_inventory.py counts a table as read only where a
    single literal carries a SELECT and an uppercase FROM <table>. The first CI
    run of this change failed NEW_WRITE_ONLY on mcp_checkout_payments because
    this read was spelled as a lowercase fragment the scanner cannot see.
    """
    return ("(SELECT p.stripe_session_id, p.client_reference_id, p.paid_at"
            " FROM mcp_checkout_payments p"
            " WHERE p.livemode IS NOT FALSE"
            " AND p.paid_at > now() - interval '" + interval_sql + "') pay")


def _relayed_click_session_for(ref_expr: str, at_expr: str) -> str:
    """THE click→session lookup, once. Both readers of it build from here.

    The latest click on the link that sold a purchase (same ref) that
    human_acted's /go/c/ lane would count — signed, real UA, a session
    identity — made at or before `at_expr` and inside the lookback.

    ★ WHY THIS IS A FUNCTION AND NOT TWO QUERIES. The funnel reads it as a
    correlated subquery over the payments table; routes/relay_identify reads
    it live in the payment webhook, for one ref, to learn which session to
    stamp an email onto. A second copy would let `identified` and
    `paid_attributed` attribute the SAME payment to two different sessions —
    the two stages would disagree about who paid while both looked measured.
    tests/test_identify_rung_shares_the_paid_join.py holds them byte-identical
    under the same substitutions.
    """
    return ("(select " + RELAYED_CHECKOUT_SESSION_ID + " "
            + _RELAYED_CHECKOUT_FROM
            + " where cc.ref = " + ref_expr
            + " and " + relayed_checkout_session_filters()
            + " and cc.clicked_at <= " + at_expr
            + " and cc.clicked_at > " + at_expr + " - interval '"
            + PAID_RELAYED_CHECKOUT_LOOKBACK + "'"
            + " order by cc.clicked_at desc, cc.id desc limit 1)")


def paid_relayed_click_session_sql() -> str:
    """Scalar subquery: the session payment `pay` is attributed to, or NULL."""
    return _relayed_click_session_for("pay.client_reference_id", "pay.paid_at")


def relayed_click_session_for_ref_sql() -> str:
    """The SAME lookup, parameterised, for a live single-ref read.

    One `%s` placeholder (the client_reference_id) and `now()` as the instant,
    which at webhook time IS the payment's paid_at. Callers execute it as
    `select <this>` with one parameter.
    """
    return _relayed_click_session_for("%s", "now()")


def _paid_session_row_lanes(interval_sql: str) -> list:
    """The two v1 tables, as union lanes."""
    return [
        ("select su.mcp_session_id as sid from mcp_session_upgrades su"
         " where su.upgraded_at > now() - interval '" + interval_sql + "'"),
        ("select tp.mcp_session_id as sid from mcp_topups tp"
         " where tp.mcp_session_id is not null"
         " and tp.created_at > now() - interval '" + interval_sql + "'"),
    ]


def _paid_relayed_checkout_lane(interval_sql: str) -> str:
    return ("select " + paid_relayed_click_session_sql() + " as sid from "
            + _paid_payments_sql(interval_sql))


def _paid_count(lanes: list, include_self_traffic: bool) -> str:
    sql = ("select count(distinct u.sid) from (" + " union ".join(lanes)
           + ") u where coalesce(u.sid,'') <> ''")
    if not include_self_traffic:
        sql += " and " + _external_session_predicate("u.sid")
    return sql


def paid_attributed_count_sql(interval_sql: str, *,
                              include_self_traffic: bool = False) -> str:
    """Canonical paid_attributed over `interval_sql`: the v1 tables UNION the
    relayed-checkout lane, DISTINCT sessions, operator exclusion once.
    `include_self_traffic=True` drops the exclusion, so the difference is
    exactly what it removed."""
    return _paid_count(_paid_session_row_lanes(interval_sql)
                       + [_paid_relayed_checkout_lane(interval_sql)],
                       include_self_traffic)


def paid_relayed_checkout_count_sql(interval_sql: str, *,
                                    include_self_traffic: bool = False) -> str:
    """The relayed-checkout lane alone, published beside the headline."""
    return _paid_count([_paid_relayed_checkout_lane(interval_sql)],
                       include_self_traffic)


def paid_attributed_v1_sql(interval_sql: str) -> str:
    """The v1 figure as it was published: a SUM of two distinct counts."""
    return ("select (select count(distinct mcp_session_id) from mcp_session_upgrades"
            " where upgraded_at > now() - interval '" + interval_sql + "')"
            " + (select count(distinct mcp_session_id) from mcp_topups"
            " where mcp_session_id is not null"
            " and created_at > now() - interval '" + interval_sql + "')")


def relayed_checkout_payments_sql(interval_sql: str) -> str:
    """payments / matched_a_relayed_click / attributable_to_a_session, one pass.

    matched_a_relayed_click is a CEILING: the payment's ref equals the ref of a
    signed, real-UA click at or before it, whatever identity that click carried.
    attributable_to_a_session is the subset whose qualifying click carried a
    session, before the operator exclusion. Both are subsets of payments.
    """
    matched = ("exists (select 1 " + _RELAYED_CHECKOUT_FROM
               + " where cc.ref = pay.client_reference_id"
               + " and " + relayed_checkout_signed()
               + " and " + relayed_checkout_real_ua()
               + " and cc.clicked_at <= pay.paid_at)")
    return ("select count(*) as payments,"
            " count(*) filter (where x.matched) as matched_a_relayed_click,"
            " count(*) filter (where x.sid is not null) as attributable_to_a_session"
            " from (select " + matched + " as matched, "
            + paid_relayed_click_session_sql() + " as sid from "
            + _paid_payments_sql(interval_sql) + ") x")


# ── paid -> signal bridge, THIRD LANE (r-paid-signal-bridge, 2026-09-17) ────
#
# Measured after the real $10 prove: paid_signal_attribution_30d read
# paid_total 3, bridged_to_signal 0, unattributable 3, attribution_rate 0.0%.
# Its two existing lanes are attribution_signal_id (the webhook never sets it
# for an agent-channel buy) and a shared caller_id (the Stripe webhook has no
# MCP caller_id to share). So a sale that demonstrably came from a relayed
# /go/c link read as unattributable.
#
# The link exists and is already PROVEN: relayed_checkout_payments reported
# matched_a_relayed_click 1 and attributable_to_a_session 1 on that payment.
# This lane walks the same road — conversion -> its Stripe Checkout Session ->
# the payment row -> the click that sold it -> that click's MCP session -> an
# upgrade signal raised on that session at or before the sale.
#
# ★ IT REUSES _relayed_click_session_for, the SAME builder paid_attributed
# joins on. A second spelling of that join would let paid_attributed and this
# bridge disagree about which session bought, while both looked measured.
#
# ★ p.session_id WINS WHEN SET. If the conversion row already carries a
# session, that is a direct fact and no inference is needed; the click walk is
# the fallback for the agent-channel rows that have none.
_PAID_REF_FROM_CONV = (
    "(select pay2.client_reference_id from mcp_checkout_payments pay2"
    " where pay2.stripe_session_id = p.stripe_session_id"
    " order by pay2.paid_at desc limit 1)")


def paid_signal_relayed_session_sql(conv_at: str = "p.conv_at") -> str:
    """The MCP session a paid `mcp_conversions` row `p` belongs to, or NULL.

    Its own session_id if set, else the session of the signed, real-UA /go/c
    click that sold the Checkout Session this conversion came from.
    """
    return ("coalesce(nullif(p.session_id,''), "
            + _relayed_click_session_for(_PAID_REF_FROM_CONV, conv_at) + ")")


def paid_signal_relayed_bridge_predicate(conv_at: str = "p.conv_at") -> str:
    """TRUE when an upgrade signal was raised on that session AT OR BEFORE the
    sale.

    The ordering matters and is not decoration: a signal raised AFTER the sale
    is the customer hitting a wall they have already paid to pass, and counting
    it would let a bridge point backwards in time.
    """
    return ("exists (select 1 from mcp_upgrade_signals s2"
            " where nullif(s2.session_id,'') = "
            + paid_signal_relayed_session_sql(conv_at)
            + " and s2.created_at <= " + conv_at + ")")


PAID_SIGNAL_RELAYED_BRIDGE_BASIS = (
    "A third bridge lane for paid_signal_attribution. The paid row's MCP "
    "session is its own session_id when set, otherwise the session of the "
    "signed, real-UA /go/c/ click whose ref equals the client_reference_id of "
    "the mcp_checkout_payments row carrying this conversion's "
    "stripe_session_id — resolved by routes/handoff_definition."
    "_relayed_click_session_for, the SAME builder paid_attributed joins on, so "
    "the two cannot disagree about which session bought. The lane requires an "
    "mcp_upgrade_signals row on that session at or before the sale: a signal "
    "raised AFTER it is the customer hitting a wall they already paid to pass. "
    "It is tried LAST, so attribution_signal_id and the caller_id bridge keep "
    "priority and this lane only ever converts rows that were previously "
    "unattributable.")


PAID_ATTRIBUTED_BASIS = (
    "COUNT(DISTINCT session) over the UNION of three lanes: "
    "mcp_session_upgrades.mcp_session_id (the webhook's same-session unlock row "
    "for a bare session ref), mcp_topups.mcp_session_id (a pack granted to a "
    "session), and the RELAYED CHECKOUT lane: a paid Checkout Session "
    "(mcp_checkout_payments, recorded at checkout.session.completed when "
    "payment_status is 'paid'; livemode false excluded) whose "
    "client_reference_id equals the ref of a signed, real-UA /go/c/ click with a "
    "session identity, made at or before the payment and inside the lookback, "
    "counted as the latest such click's session. WHY: a caller holding an API "
    "key is sold through a durable-key ref (pk- for the pack, k- for a "
    "subscription) and the webhook binds no session to either, so through v1 a "
    "keyed caller's purchase from an agent unlock could not reach this stage. "
    "The click that sold it carries the session (the /go/c/ token's third field "
    "since 2026-09-13), so the payment joins that click by ref. The operator "
    "self-traffic exclusion applies to the union; "
    "paid_attributed_including_self_traffic drops it, and "
    "excluded.paid_attributed_removed is the difference. A payment recorded "
    "before 2026-09-14 has no stored client_reference_id and can reach only the "
    "v1 lanes.")

RELAYED_CHECKOUT_PAYMENTS_BASIS = (
    "Paid Checkout Sessions in the window (mcp_checkout_payments, livemode not "
    "false). matched_a_relayed_click: the payment's client_reference_id equals "
    "the ref of a signed, real-UA /go/c/ click made at or before it, whatever "
    "identity that click carried: a CEILING on purchases that followed a "
    "relayed link. attributable_to_a_session: the subset whose matching click, "
    "inside the lookback, carried a session, before the operator exclusion; "
    "paid_attributed_from_relayed_checkout is that set after it. Both are "
    "subsets of payments, not a partition of it.")


def paid_attributed_definition() -> dict:
    """The published paid_attributed definition, for the funnel payload."""
    return {
        "definition_version": PAID_ATTRIBUTED_DEFINITION_VERSION,
        "definition_changelog": dict(PAID_ATTRIBUTED_DEFINITION_CHANGELOG),
        "relayed_checkout_lookback": PAID_RELAYED_CHECKOUT_LOOKBACK,
        "basis": PAID_ATTRIBUTED_BASIS,
    }


HUMAN_ACTED_V7_BASIS = (
    "COUNT(DISTINCT session identity) FROM mcp_checkout_clicks — the table "
    "/go/c/<token> writes (routes/checkout_click_tracker) — with the same "
    "real-UA predicate and the same declared operator self-traffic exclusion "
    "the published stage applies, plus sig_ok, which is TRUE only for a link "
    "we minted and HMAC-verified. WHY IT EXISTS: measured from outside "
    "anonymously on 2026-09-09, one gated tools/call returns TWO different "
    "links — content[0].text, the block a client renders and a model relays, "
    "carries https://dchub.cloud/go/c/<token>, while the /upgrade/h/ link that "
    "human_acted actually counts appears only in structuredContent. relay_opens "
    "is written by /upgrade/h/ alone, and this endpoint had no read of "
    "mcp_checkout_clicks at all, so a human clicking the link their agent put "
    "in front of them could not move this stage for any click, ever. "
    "★ KEYED ON A SESSION on purpose: a session id is the only identity the "
    "self-traffic exclusion can bind to. A click counts AS the session its "
    "token carried (mcp_checkout_clicks.session_id, minted beside a "
    "'pk-'/'k-' durable-key ref since 2026-09-13), else as its ref when that "
    "ref is a bare session id. On a durable-key hash or an 'a-' anonymous offer "
    "id with no session beside it the exclusion would pass vacuously — the v6 "
    "widening hit exactly that and the answer was two numbers, not one wider "
    "number — so those clicks are counted in `human_acted_v7_links_clicked` "
    "instead. Rows written before the token carried a session have session_id "
    "NULL and are read exactly as before. "
    "It is one of the two lanes the headline `human_acted` now unions (see "
    "definitions.human_acted); published alone here so its share of the "
    "headline stays readable.")

HUMAN_ACTED_V7_LINKS_BASIS = (
    "COUNT(DISTINCT ref) FROM mcp_checkout_clicks over the SAME window with "
    "the SAME real-UA predicate and the SAME signature requirement as "
    "human_acted_v7_from_checkout_clicks — but with NO ref_kind restriction "
    "and NO operator self-traffic exclusion, because on a durable-key hash "
    "('pk-'/'k-') or an anonymous offer id ('a-') that exclusion can only pass "
    "vacuously. The unit is DISTINCT LINKS CLICKED, not distinct people: "
    "routes/checkout_click_tracker mints one ref per identity, not per click, "
    "and the table is append-only, so one human clicking twice is one ref and "
    "two rows. Read it as a ceiling on how many relayed checkout links were "
    "opened at all, never as a count of humans reached. "
    "\u2605 IT REQUIRES A REF, and the ref is the only identity this ceiling "
    "reads — routes/human_relay stores a per-mint token hash as a fallback, "
    "routes/checkout_click_tracker stores none — so a signed click minted with "
    "an empty ref is real and permanently uncountable here. Since 2026-09-13 "
    "its unit and the de-loopable count's differ: that count reads the session "
    "a click is bound to, so one durable key clicked from two sessions is two "
    "there and one here — a ceiling on links, not on that count. When this number "
    "is below relayed_checkout_provenance.minted_link_clicks the difference is "
    "published as minted_link_clicks_no_ref; measured on the first live read, "
    "2026-09-10 over 30d, that difference was the whole of it (1 and 0).")

RELAYED_CHECKOUT_PROVENANCE_BASIS = (
    "mcp_checkout_clicks rows in the window, split by whether they can reach "
    "the stage at all. probe_ua = fails the real-UA predicate. "
    "unsigned_clicks = real UA, but the token's HMAC did not verify — a "
    "scanner walking /go/c/<junk>, which can never be a relayed link. "
    "minted_link_clicks = real UA on a link we signed, the only rows that can "
    "count. The three partition `total` exhaustively and are mutually "
    "exclusive. ★ minted_link_clicks_deloopable is a SUBSET of "
    "minted_link_clicks, NOT a fourth branch: it partitions by whether the "
    "self-traffic exclusion can bind (the click carries a session identity: the "
    "session its token named, or a bare session ref), which is ORTHOGONAL "
    "to the split above — do not add a field from each. READ minted_link_clicks "
    "AGAINST THE PUBLISHED STAGE: a non-zero there while the stage reads 0 "
    "means humans are clicking the link agents actually relay and the funnel "
    "was not looking; a zero means no relayed checkout link was opened, and "
    "the constraint is delivery rather than measurement. "
    "\u2605 minted_link_clicks_no_ref is the SECOND subset and it is the one "
    "that reconciles this block with human_acted_v7_links_clicked: the ceiling "
    "counts DISTINCT refs, so a signed click carrying no ref is counted here "
    "and can never be counted there. minted_link_clicks minus "
    "minted_link_clicks_no_ref is the population the ceiling can see. Without "
    "this field the pair (minted_link_clicks 1, links_clicked 0) reads as an "
    "arithmetic error in the split, which is what it looked like on the first "
    "live read of this block. "
    "★ minted_link_clicks_session_from_token is the THIRD subset: minted "
    "clicks whose session came from the token's session field rather than from "
    "the ref (session_id present, ref_kind not 'session'). It is contained in "
    "minted_link_clicks_deloopable and reads 0 until the MCP server mints that "
    "field; a non-zero is the first sign that keyed clicks carry a session the "
    "exclusion can test.")


# ── click→pay per plan and per path (2026-09-21, frontend#1534 (c)) ─────────
# Grok's constraint for P0-C: measure click→pay per PLAN, and give each path
# its own column, so a leak is located on the path where it happens:
#   mcp_go_c           a signed /go/c link an agent relayed (mcp_checkout_clicks)
#   cold_go_p          a /pricing button via /go/p/<plan> (pricing_checkout_clicks)
#   chatgpt_upgrade_h  the ChatGPT/OpenAI relay page: wall → view → identify → pay
#   rest_wall_go_c     a caller-independent /go/c link on a cached REST wall
#                      (rest_wall_ladder): no ref, so nothing
#                      reaches Stripe to join on and its paid is UNMEASURABLE
# The two click paths carry the plan on the click row. A payment is credited to
# the LATEST qualifying click on its ref, at or before it and inside
# PAID_RELAYED_CHECKOUT_LOOKBACK: the rule paid_attributed applies, so the two
# figures cannot credit one payment to two different clicks.
# ★ ChatGPT/OpenAI sessions are left out of mcp_go_c. The relay page's keyed
#   button is itself a /go/c link, and a payment in two columns is two payments
#   on the board.
CLICK_TO_PAY_PLANS = ("metered", "developer", "pro")
# The MCP server's _isCleanPlatform: ctx.platform contains 'chatgpt' or
# 'openai'. signalPaywall stores that platform as mcp_upgrade_signals.mcp_client.
CHATGPT_PLATFORM_RE = "(chatgpt|openai)"


def chatgpt_session_predicate(sid_expr: str) -> str:
    """TRUE when session `sid_expr` hit a paywall from a ChatGPT/OpenAI client.
    A regex, never LIKE: no literal percent sign (external_session_predicate)."""
    return ("exists (select 1 from mcp_upgrade_signals sg where sg.session_id = "
            + sid_expr + " and sg.mcp_client ~* '" + CHATGPT_PLATFORM_RE + "')")


def _click_rows_sql(interval_sql: str, include_cold: bool) -> str:
    """Every qualifying click in the window, as (path, plan, ref, clicked_at, id).
    mcp_go_c: signed, real UA, carries a ref (every link the MCP server mints
    does: a session, a key hash or an anon offer id), not operator traffic, not
    a ChatGPT session.
    rest_wall_go_c: signed, real UA, no ref: in practice the caller-independent
    link rest_wall_ladder puts on a cached REST payload. Counted apart, because a
    click that can never join a payment would drag mcp_go_c's rate down. Together
    the two lanes take every signed real-UA click exactly once (ref or no ref).
    cold_go_p: a plan /pricing sells (known_plan), real UA. It has no session,
    so the operator exclusion cannot apply there, and the basis says so."""
    rows = [
        ("SELECT 'mcp_go_c'::text AS path, cc.plan, cc.ref, cc.clicked_at, cc.id"
         " FROM mcp_checkout_clicks cc WHERE cc.clicked_at > now() - interval '"
         + interval_sql + "' AND " + relayed_checkout_signed()
         + " AND " + relayed_checkout_real_ua()
         + " AND coalesce(cc.ref,'') <> ''"
         + " AND " + _external_session_predicate(RELAYED_CHECKOUT_SESSION_ID)
         + " AND NOT " + chatgpt_session_predicate(RELAYED_CHECKOUT_SESSION_ID)),
        ("SELECT 'rest_wall_go_c'::text AS path, cc.plan, cc.ref, cc.clicked_at, cc.id"
         " FROM mcp_checkout_clicks cc WHERE cc.clicked_at > now() - interval '"
         + interval_sql + "' AND " + relayed_checkout_signed()
         + " AND " + relayed_checkout_real_ua()
         + " AND coalesce(cc.ref,'') = ''"),
    ]
    if include_cold:
        rows.append(
            "SELECT 'cold_go_p'::text AS path, pc.plan, pc.ref, pc.clicked_at, pc.id"
            " FROM pricing_checkout_clicks pc WHERE pc.clicked_at > now() - interval '"
            + interval_sql + "' AND pc.known_plan IS TRUE AND "
            + _real_ua_predicate("pc.user_agent"))
    return " UNION ALL ".join(rows)


def click_to_pay_by_plan_sql(interval_sql: str, *, include_cold: bool = True) -> str:
    """Rows (path, plan, clicks, paid) over `interval_sql`.
    clicks: qualifying presses. paid: distinct paid Checkout Sessions credited
    to the latest qualifying click on the same ref (at or before the payment,
    inside PAID_RELAYED_CHECKOUT_LOOKBACK), under that click's plan.
    `include_cold=False` leaves out pricing_checkout_clicks, for a database
    where GET /go/p/<plan> has not created it yet."""
    return (
        "WITH clk AS (" + _click_rows_sql(interval_sql, include_cold) + "),"
        " credited AS (SELECT DISTINCT ON (pay.stripe_session_id) clk.path, clk.plan"
        " FROM " + _paid_payments_sql(interval_sql)
        + " JOIN clk ON clk.ref = pay.client_reference_id"
        " AND clk.clicked_at <= pay.paid_at AND clk.clicked_at > pay.paid_at - interval '"
        + PAID_RELAYED_CHECKOUT_LOOKBACK + "'"
        " ORDER BY pay.stripe_session_id, clk.clicked_at DESC, clk.id DESC)"
        " SELECT k.path, k.plan, k.clicks, coalesce(p.paid, 0) AS paid"
        " FROM (SELECT path, plan, count(*) AS clicks FROM clk GROUP BY 1, 2) k"
        " LEFT JOIN (SELECT path, plan, count(*) AS paid FROM credited GROUP BY 1, 2) p"
        " USING (path, plan) ORDER BY 1, 2")


def chatgpt_relay_stages_sql(interval_sql: str) -> str:
    """One row (walls, views, identified, paid) over `interval_sql`: DISTINCT
    ChatGPT/OpenAI sessions that hit a paywall, and of those, the ones that
    opened /upgrade/h (a valid token, real UA), bound an email (either writer of
    `identified`), and paid (any lane of paid_attributed). Each stage is read
    from the stage's own canonical lanes, so a session counts at a stage it
    reached even when it skipped the one before it: the page sells the $10 pack
    and a keyed caller's button goes straight to /go/c."""
    chat = ("SELECT DISTINCT sg.session_id AS sid FROM mcp_upgrade_signals sg"
            " WHERE sg.created_at > now() - interval '" + interval_sql + "'"
            " AND sg.mcp_client ~* '" + CHATGPT_PLATFORM_RE + "'"
            " AND coalesce(sg.session_id,'') <> ''"
            " AND " + _external_session_predicate("sg.session_id"))
    views = ("SELECT ro.session_id AS sid FROM relay_opens ro"
             " WHERE ro.ts > now() - interval '" + interval_sql + "'"
             " AND ro.valid IS TRUE AND " + _real_ua_predicate("ro.user_agent"))
    identified = " UNION ".join(_identified_lanes(interval_sql))
    paid = " UNION ".join(_paid_session_row_lanes(interval_sql)
                          + [_paid_relayed_checkout_lane(interval_sql)])
    return ("WITH chat AS (" + chat + ")"
            " SELECT (SELECT count(*) FROM chat),"
            " (SELECT count(DISTINCT v.sid) FROM (" + views + ") v JOIN chat USING (sid)),"
            " (SELECT count(DISTINCT i.sid) FROM (" + identified + ") i JOIN chat USING (sid)),"
            " (SELECT count(DISTINCT p.sid) FROM (" + paid + ") p JOIN chat USING (sid))")


def click_to_pay_basis() -> dict:
    """What each column counts, published beside the numbers."""
    return {
        "mcp_go_c": ("signed, real-UA /go/c clicks (mcp_checkout_clicks), operator "
                     "sessions and ChatGPT/OpenAI sessions excluded; paid = paid "
                     "Checkout Sessions (mcp_checkout_payments, livemode not false) "
                     "credited to the latest such click on the same ref within "
                     + PAID_RELAYED_CHECKOUT_LOOKBACK + ", under that click's plan"),
        "cold_go_p": ("/pricing button presses through /go/p/<plan> "
                      "(pricing_checkout_clicks, a plan /pricing sells, real UA); "
                      "no session, so operator presses are NOT excluded; paid as "
                      "for mcp_go_c, on the page's attribution ref"),
        "chatgpt_upgrade_h": ("DISTINCT ChatGPT/OpenAI sessions (mcp_upgrade_signals."
                              "mcp_client ~* '" + CHATGPT_PLATFORM_RE + "', the MCP "
                              "server's clean-platform rule; DCHUB_CLEAN_PLATFORMS "
                              "additions are not mirrored) at each stage, operator "
                              "sessions excluded: walls, views (relay_opens, valid, "
                              "real UA), identified and paid (their canonical lanes)"),
        "rest_wall_go_c": ("signed, real-UA /go/c clicks with no ref: in practice "
                           "the caller-independent link rest_wall_ladder puts on a "
                           "cached REST payload. No ref reaches Stripe, so a payment "
                           "cannot be joined to it: paid is null (unmeasurable), not 0"),
        "plan_credit": ("a session ref can sit on a $10 link and a subscription "
                        "link at once; the payment takes the plan of the latest "
                        "qualifying click, not the plan it bought"),
    }
