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
HUMAN_ACTED_DEFINITION_VERSION = 5

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


def human_acted_session_predicate(alias: str = "s") -> str:
    """TRUE when session `alias` opened EITHER human artifact on a real UA.

    This is the v3 body — the union — WITHOUT the v4 self-traffic exclusion, so
    callers can express both the filtered stage and the unfiltered diagnostic
    from one place. Carries no literal `%`: both predicates are the anchored
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


def human_acted_not_self_predicate(alias: str = "s") -> str:
    """The v4 delta: TRUE when the session is not declared operator traffic."""
    return _external_session_predicate("%s.mcp_session_id" % alias)


def human_acted_count_sql(interval_sql: str, *,
                          include_self_traffic: bool = False) -> str:
    """Canonical human_acted count over `interval_sql` (e.g. "30 days").

    `include_self_traffic=True` renders the v3 diagnostic that must stay
    published beside the v4 figure — never a silent subtraction.
    """
    body = ("from mcp_high_intent_sessions s "
            "where s.first_hit_at > now() - interval '" + interval_sql +
            "' and " + human_acted_session_predicate("s"))
    sql = "select count(distinct s.mcp_session_id) " + body
    if not include_self_traffic:
        sql += " and " + human_acted_not_self_predicate("s")
    return sql


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


def _relayed_checkout_window(interval_sql: str) -> str:
    return ("from mcp_checkout_clicks cc "
            "where cc.clicked_at > now() - interval '" + interval_sql + "'")


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


def human_acted_v7_count_sql(interval_sql: str) -> str:
    """DE-LOOPABLE clicks on the relayed checkout link, over `interval_sql`.

    Restricted to ref_kind='session' precisely BECAUSE that is the only ref the
    operator self-traffic exclusion can bind to. Everything else is reported by
    human_acted_v7_links_sql, whose basis says it is un-de-loopable, rather
    than folded in here under a filter that would pass vacuously on it.
    """
    return ("select count(distinct cc.ref) "
            + _relayed_checkout_window(interval_sql)
            + " and " + relayed_checkout_signed()
            + " and " + relayed_checkout_real_ua()
            + " and coalesce(cc.ref,'') <> ''"
            + " and cc.ref_kind = '" + RELAYED_CHECKOUT_DELOOPABLE_REF_KIND + "'"
            + " and " + _external_session_predicate("cc.ref"))


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


def relayed_checkout_provenance_sql(interval_sql: str) -> str:
    """total + the three-way split + the de-loopable subset, in one pass.

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
    parts.append(
        "count(*) filter (where (" + relayed_checkout_real_ua() + ")"
        " and (" + relayed_checkout_signed() + ")"
        " and cc.ref_kind = '" + RELAYED_CHECKOUT_DELOOPABLE_REF_KIND + "')"
        " as minted_link_clicks_deloopable")
    return ("select " + ", ".join(parts) + " "
            + _relayed_checkout_window(interval_sql))


HUMAN_ACTED_V7_BASIS = (
    "COUNT(DISTINCT ref) FROM mcp_checkout_clicks — the table "
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
    "★ RESTRICTED TO ref_kind='session' on purpose: that is the only ref the "
    "self-traffic exclusion can bind to. On a 'pk-'/'k-' durable-key hash or an "
    "'a-' anonymous offer id it would pass vacuously — the v6 widening hit "
    "exactly that and the answer was two numbers, not one wider number. Those "
    "refs are counted in `human_acted_v7_links_clicked` instead. "
    "PUBLISHED ALONGSIDE: the headline stage is unchanged.")

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
    "opened at all, never as a count of humans reached.")

RELAYED_CHECKOUT_PROVENANCE_BASIS = (
    "mcp_checkout_clicks rows in the window, split by whether they can reach "
    "the stage at all. probe_ua = fails the real-UA predicate. "
    "unsigned_clicks = real UA, but the token's HMAC did not verify — a "
    "scanner walking /go/c/<junk>, which can never be a relayed link. "
    "minted_link_clicks = real UA on a link we signed, the only rows that can "
    "count. The three partition `total` exhaustively and are mutually "
    "exclusive. ★ minted_link_clicks_deloopable is a SUBSET of "
    "minted_link_clicks, NOT a fourth branch: it partitions by whether the "
    "self-traffic exclusion can bind (ref_kind='session'), which is ORTHOGONAL "
    "to the split above — do not add a field from each. READ minted_link_clicks "
    "AGAINST THE PUBLISHED STAGE: a non-zero there while the stage reads 0 "
    "means humans are clicking the link agents actually relay and the funnel "
    "was not looking; a zero means no relayed checkout link was opened, and "
    "the constraint is delivery rather than measurement.")
