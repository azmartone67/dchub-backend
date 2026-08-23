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
HUMAN_ACTED_DEFINITION_VERSION = 4

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
LEAK_LADDER = (
    ("paywall_hit", "relay_minted", "paywall→relay_mint"),
    ("relay_minted", "human_acted", "relay_mint→human_acted"),
    ("human_acted", "identified", "human_acted→identified"),
    ("identified", "paid_attributed", "identified→paid"),
)


def _leak_detail(src: str, dst: str, label: str, steps: dict) -> dict:
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
    for src, dst, label in LEAK_LADDER:
        upstream = steps.get(src) or 0
        downstream = steps.get(dst) or 0
        if upstream and downstream < upstream * 0.5:
            return _leak_detail(src, dst, label, steps)
    src, dst, label = LEAK_LADDER[-1]
    return _leak_detail(src, dst, label, steps)


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
