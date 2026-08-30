"""relay_closure_master_shell.py — master shell #64: RELAY CLOSURE (2026-08-21).

The agent→human relay was built (#44), instrumented twice (v2 07-30, v3 08-16),
had its arbitrage removed (r-stop-arbitrage 08-16) and its transport made
flippable (mcp #208, 08-20). It has never produced a single external human
open. `relay_opens` holds 29 rows all-time and EVERY ONE IS OURS — 24
dchub-qa-superuser, the operator's Claude desktop app, two operator browser
opens on session 88e20dac, two 07-27 probes.

check_relay_opens.py pre-registered the exit condition in July, before the
answer was known:

    if real opens are STILL 0 with the block present AND top-level, the
    constraint is agent BEHAVIOUR, not envelope shape — and the answer is a
    human-present channel rather than more envelope tuning. […] Do not read a
    still-zero result as "the fix failed". Read it as the experiment returning
    its other answer, which is just as useful and should STOP further envelope
    work.

Both preconditions have held since 0aab503 (07-28). The rule has fired. This
shell is what a closed experiment leaves behind: the conditions that closed it,
re-checked every tick, so the close is auditable and so a genuine change of
state reopens it instead of being missed.

★ WHY A SHELL AND NOT A NOTE. Three separate readings of this funnel have now
reached a wrong conclusion from a correct number, because the number's
INSTRUMENT changed underneath the prose (#44's "the human buyer does not
exist", #54's press level, and on 2026-08-21 "mint→redeem is the biggest leak"
— which was a machine step switched off on purpose). A note rots. A lane that
re-derives its verdict from the data every tick cannot.

  A  redeem_declared_vs_writer — routes/handoff_definition declares the redeem
     stage's writer OFF since 2026-08-16. Lane A checks that declaration
     against the data every tick, so if anyone sets DCHUB_AUTO_REDEEM_ENABLE=1
     the canon goes RED instead of quietly lying. CONTROL: the MINT writer must
     be live, or "no redeems" is vacuous and the lane says ? instead of PASS.
  B  relay_demand_verdict — the pre-registered stopping rule, re-derived.
     Probe- and operator-excluded via IMPORTED mcp_calls_deloop predicates.
     Two absences, two verdicts: no rows AT ALL is an unproven write path (?),
     not evidence about demand.
  C  mint_attributability — can a per-platform transport experiment run at
     all? 77% of minted relays arrive through a gateway that strips the end
     client. Lane C publishes the targetable cohort per platform, so the
     experiment's REOPEN condition is a number on a dashboard rather than a
     memory.
  D  typed_params_window — mcp #207 typed execute_plan's params on 2026-08-18.
     Any 7d window reaching back past that measures the OLD schema. Lane D
     refuses to report planner selection until a clean window exists (same
     time-gate #44 put on its demand verdict, for the same reason).
  E  schema_selection_asks — the three converged asks, named and NOT built
     here. Every one of seven platforms asked for schema and selection; none
     asked for a better human handoff. That is the same finding lane B reaches
     from the other side.

Report-only. Heals nothing, sends nothing, flips no flag — C's whole point is
that the flag must NOT be flipped.

Endpoints (admin-keyed, read-only):
  GET /api/v1/admin/relay-closure-shell
  GET /api/v1/admin/relay-closure-shell/master-tick
Kill: RELAY_CLOSURE_SHELL_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, request

# IMPORTED, never restated — the externality verdict must be the same one every
# other real-traffic read uses, or this shell disagrees with the funnel it
# exists to check. Both are the anchored REGEX forms: they carry no literal
# '%', which is load-bearing beside psycopg2 paramstyle (see
# external_session_predicate's docstring — the LIKE form took the live
# handoff-funnel endpoint down inside one deploy).
from mcp_calls_deloop import (
    external_session_predicate as _external_session_predicate,
    real_ua_predicate as _real_ua_predicate,
    self_traffic_session_prefixes as _self_traffic_prefixes,
)

# Same reasoning: the redeem stage's status is PUBLISHED canon. This shell
# verifies it, so it must read the canon rather than a copy of it — a copy is
# exactly the defect the lane exists to catch.
from routes.handoff_definition import (
    REDEEM_INSTRUMENT_DISABLED_ON,
    REDEEM_STAGE_IS_FUNNEL_PROGRESS,
    redeem_stage_basis as _redeem_stage_basis,
)

logger = logging.getLogger("relay_closure")
relay_closure_master_shell_bp = Blueprint("relay_closure_master_shell", __name__)

SHELL_NUMBER = 64
SHELL_NAME = "relay-closure"

# ── declared facts the lanes judge against ──────────────────────────────────

# The relay block has been present AND top-level since this commit. Both are
# check_relay_opens.py's stated preconditions for its stopping rule; lane B
# renders them so nobody has to take the precondition on trust.
RELAY_TOP_LEVEL_SINCE = date(2026, 7, 28)

# mcp #207 — market, capacity_mw, iso, state, lat, lon typed at execute_plan's
# top level. Landed 2026-08-18 22:23 PDT = 2026-08-19 05:23 UTC, so 08-19 is
# the first UTC day whose calls saw the new schema, and the first clean 7d
# window ends 2026-08-26. Before that, a selection number measures the schema
# that was REPLACED: previously these lived in an untyped `context` blob, so
# tools/list published an EMPTY schema and no planner could select on them.
TYPED_PARAMS_FIRST_CLEAN_DAY = date(2026, 8, 19)
PLANNER_WINDOW_DAYS = 7

# A client string that names a real AI platform. Substring match against BOTH
# mcp_client and user_agent, because the platform can arrive in either (the
# grok connectors-manager writes mcp_client='connectors-manager' and
# user_agent='grok-connectors-manager/0.1.0' — targetable, and only the UA
# says so).
NAMED_PLATFORMS = (
    "claude", "chatgpt", "openai", "gemini", "grok", "mistral", "cursor",
    "copilot", "perplexity", "cline", "windsurf", "deepseek", "meta", "you.com",
)

# Clients that are a GATEWAY: the string names the PROXY, and the agent behind
# it is not recoverable from any field we receive. Measured 2026-08-21: all
# 1,360 Smithery calls in the 7d window report platform=smithery /
# client_name='Smithery Connect' / user_agent='node', from ONE Cloudflare
# egress (2a06:98c0:3600::103). Nothing distinguishes Claude-behind-Smithery
# from Grok-behind-Smithery — which is precisely what a per-platform transport
# experiment would need to know.
GATEWAY_CLIENTS = ("smithery", "toolrouter", "mcphub", "mcp-generic-client")

# The smallest weekly cohort in which a per-platform transport flip could
# return an informative answer. Derivation, so this is arguable rather than
# magic: human_acted has been 0 for its entire life, so the test is whether a
# flip produces ANY open. At a plausible 5% open rate, 1/0.05 = 20 sessions is
# the smallest cohort where one open is the EXPECTED outcome rather than a
# surprise. Below it a null result is uninformative — it cannot distinguish
# "transport was not the problem" from "we did not look at enough sessions".
MIN_TARGETABLE_COHORT_7D = 20

# A machine redeem completes in about a second; #44 measured 0.85s median,
# r-stop-arbitrage 0.79s over 560 mints, and this session 0.72s with 122 of 132
# inside 2s. 5s is the same threshold /api/v1/admin/relay-watch uses, kept
# identical on purpose so the two instruments cannot disagree about what a
# machine redeem is.
MACHINE_REDEEM_MAX_SECONDS = 5

# Writer-liveness window. Wide enough that a quiet night is not a signal.
WRITER_LIVENESS_HOURS = 72


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _disabled() -> bool:
    return (os.environ.get("RELAY_CLOSURE_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _one(cur, sql):
    """Fail-soft single-row read. None means UNREADABLE, which every verdict
    below distinguishes from zero — a failed read is not a zero (the
    flattering-zero trap, shell #34)."""
    try:
        cur.execute(sql)
        return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay_closure] read failed: %s", e)
        return None


# ── PURE verdict functions ──────────────────────────────────────────────────
# Separated from the reads on purpose. The pre-merge suite installs no database
# and sets no DATABASE_URL, so a verdict entangled with its query is a verdict
# that CI can only skip — and a skipped guard is a silent green. Everything
# below is a pure function of numbers, and tests/test_relay_closure_shell.py
# exercises each one directly.

def verdict_redeem_writer(stamps_recent, mints_recent, median_gap_s):
    """Does the published declaration still match the data?

    ★ CONTROL FIRST. "no redeems" is only evidence that the redeem writer is
    off if the pipeline UPSTREAM is still running. If nothing is minting
    either, the whole funnel is dark and a PASS here would be passing for the
    wrong reason — the exact failure shell #54 lane F shipped with.
    """
    if stamps_recent is None or mints_recent is None:
        return "?", "unreadable — a failed read is not a zero"
    if not mints_recent:
        return "?", ("CONTROL FAILED: no claim_minted_at stamps in the last "
                     "%dh either, so the funnel is dark upstream and 'no "
                     "redeems' says nothing about the redeem writer"
                     % WRITER_LIVENESS_HOURS)
    if not stamps_recent:
        return "PASS", ("declaration holds: mint writer live (%d in %dh), "
                        "redeem writer silent — consistent with auto-redeem "
                        "off since %s"
                        % (mints_recent, WRITER_LIVENESS_HOURS,
                           REDEEM_INSTRUMENT_DISABLED_ON.isoformat()))
    if median_gap_s is not None and median_gap_s < MACHINE_REDEEM_MAX_SECONDS:
        return "FAIL", ("CANON STALE: %d redeems in %dh at a %.2fs median gap "
                        "— that is machine auto-redeem, so "
                        "DCHUB_AUTO_REDEEM_ENABLE has been set back to 1 while "
                        "routes/handoff_definition still declares the writer "
                        "off. Fix the env or the canon; they disagree."
                        % (stamps_recent, WRITER_LIVENESS_HOURS, median_gap_s))
    return "ESCALATE", ("%d redeems in %dh at a %s median gap — SLOWER than a "
                        "machine redeem, so these may be real humans "
                        "submitting the claim form. That has never happened. "
                        "Verify before assuming instrumentation drift."
                        % (stamps_recent, WRITER_LIVENESS_HOURS,
                           ("%.2fs" % median_gap_s) if median_gap_s is not None
                           else "unknown"))


def verdict_relay_demand(rows_total, rows_real_external, sessions=None):
    """The pre-registered stopping rule, plus the two ways it can be misread.

    Two absences, two verdicts (shell #54): zero rows AT ALL cannot tell a dead
    write path from absent demand, and check_relay_opens.py is explicit that a
    zero only means something once the write path is PROVEN.

    ★ AND A PRESENCE THAT IS NOT DEMAND. An open that survives the probe and
    named-operator filters is NOT automatically a prospect. The operator's own
    client writes mcp_client='claude' / user_agent='node' — byte-identical to a
    prospect running Claude Code — so the exclusion in mcp_calls_deloop is a
    NAMED FACT (a list of session prefixes), not a derivation. The operator's
    MCP session ROTATES: the server mints a fresh session id per tool call, so
    naming one prefix cannot hold, and the next operator sitting reappears as a
    brand-new "real external" session.

    Measured 2026-08-21: relay_opens id=28 (session 8c8e1d0d…, Claude Desktop
    UA, referer dchub.cloud) is not in the seed, so the funnel publishes
    human_acted=1 under definition v4 — a stage that has never fired reading as
    a conversion, which is the exact misreading v4 was created to prevent. The
    session's first hit lands EIGHT SECONDS after the named operator session
    88e20dac's last call, on the identical client fingerprint.

    So a first non-zero returns NEEDS_ATTRIBUTION, never a demand verdict. A
    human names it — add the prefix to DCHUB_SELF_TRAFFIC_SESSIONS if it is
    ours, or treat it as demand if it is not. Guessing either way is the failure
    this whole module exists to prevent: invent a behavioural rule and you
    delete real leads; count it silently and you announce a conversion that was
    yourself.
    """
    if rows_total is None or rows_real_external is None:
        return "?", "unreadable — a failed read is not a zero"
    if not rows_total:
        return "?", ("relay_opens is EMPTY — the write path is unproven, so "
                     "this says nothing about demand. Not a stopping-rule "
                     "trigger.")
    if not rows_real_external:
        return "STOP_ENVELOPE_WORK", (
            "write path PROVEN (%d rows) and real external opens = 0. Both of "
            "check_relay_opens.py's preconditions have held since %s, so its "
            "pre-registered rule fires: the constraint is agent BEHAVIOUR, not "
            "envelope shape. Do not tune the envelope again; do not flip the "
            "wall transport flag. This is the experiment returning its other "
            "answer, not the fix failing."
            % (rows_total, RELAY_TOP_LEVEL_SINCE.isoformat()))
    return "NEEDS_ATTRIBUTION", (
        "%d relay open(s) survive the probe and named-operator filters%s — and "
        "the funnel is publishing them as human_acted under definition v4. "
        "This is NOT yet demand evidence: the operator's client is "
        "byte-identical to a prospect's and the operator's session id ROTATES, "
        "so an unnamed session is unattributed, not external. NAME each one "
        "before it counts: add the prefix to DCHUB_SELF_TRAFFIC_SESSIONS if it "
        "is ours, otherwise it is the first real human open in this "
        "instrument's life and the stopping rule is void."
        % (rows_real_external,
           (" (" + ", ".join(sessions) + ")") if sessions else ""))


def verdict_attributability(by_platform, unattributable, gateway_note=""):
    """Can a per-platform transport experiment target anything?

    `by_platform` maps a NAMED platform to its 7d minted-session count;
    `unattributable` is the count whose end client no field reveals.
    """
    if by_platform is None or unattributable is None:
        return "?", "unreadable — a failed read is not a zero", None
    total = sum(by_platform.values()) + unattributable
    if not total:
        return "?", "no relays minted in the window — nothing to judge", None
    best = max(by_platform.items(), key=lambda kv: kv[1]) if by_platform else None
    unattr_pct = round(100.0 * unattributable / total, 1)
    if best and best[1] >= MIN_TARGETABLE_COHORT_7D:
        return "PASS", (
            "'%s' has %d minted sessions in 7d, at or above the %d floor — a "
            "per-platform transport flip on it could return an informative "
            "answer. %s%% of the window is still unattributable."
            % (best[0], best[1], MIN_TARGETABLE_COHORT_7D, unattr_pct)), best[0]
    biggest = ("largest named cohort '%s' at %d" % best) if best else "no named platform present"
    return "FAIL", (
        "EXPERIMENT UNRUNNABLE: %s%% of minted relays (%d of %d) name no end "
        "client, and %s — under the %d floor. DCHUB_WALL_SUCCESS_PLATFORMS has "
        "no useful setting: a gateway substring flips transport for an unknown "
        "mixture of clients and attributes the result to none of them, and "
        "every named cohort is too small for a null result to mean anything. "
        "%sReopens by itself when a named cohort crosses the floor."
        % (unattr_pct, unattributable, total, biggest,
           MIN_TARGETABLE_COHORT_7D,
           (gateway_note + " ") if gateway_note else "")), None


def verdict_typed_params_window(today=None):
    """Refuse to report planner selection on a window that spans the schema
    change. Returns (status, note, readable_bool)."""
    today = today or datetime.now(timezone.utc).date()
    first_readable = TYPED_PARAMS_FIRST_CLEAN_DAY.toordinal() + PLANNER_WINDOW_DAYS
    days_left = first_readable - today.toordinal()
    readable_on = date.fromordinal(first_readable)
    if days_left > 0:
        return "ACCUMULATING", (
            "planner selection is NOT readable yet. mcp #207 typed "
            "execute_plan's params on %s, so a %dd window ending today still "
            "reaches back into the OLD schema — where these params sat in an "
            "untyped `context` blob and tools/list published an EMPTY schema. "
            "Any number now measures the thing that was replaced. First clean "
            "read: %s (%d day(s) away)."
            % (TYPED_PARAMS_FIRST_CLEAN_DAY.isoformat(), PLANNER_WINDOW_DAYS,
               readable_on.isoformat(), days_left)), False
    return "MEASURED", (
        "window is clean — it lies entirely after the %s schema change, so "
        "the selection rate below reflects the typed params."
        % TYPED_PARAMS_FIRST_CLEAN_DAY.isoformat()), True


# ── lane E: the converged asks, MEASURED ────────────────────────────────────
# ★★★ THIS LANE SHIPPED WRONG. Its first version asserted all three of the
# 2026-08-20 seven-platform asks were NAMED_NOT_BUILT, from a hand-written list.
# TWO OF THEM WERE ALREADY SHIPPED. That is precisely the defect the rest of
# this shell exists to catch — a surface asserting something the data no longer
# supports — committed by the ONE lane that asserted from prose instead of
# re-deriving, in a module whose own docstring says "A note rots. A lane that
# re-derives its verdict from the data every tick cannot." The #44 lane-D
# static shape was copied without its justification: #44's items were outbound
# artifacts awaiting a HUMAN decision, which nothing can measure. These are
# properties of a live surface, which anything can measure.
MCP_ENDPOINT = "https://dchub.cloud/mcp"

# Verified shipped, with the evidence that settled it. Kept so the lane never
# re-opens closed work — the failure that put "typed execute_plan params" on
# two agents' pending lists after #207 had already landed.
SHIPPED_ASKS = (
    {"ask": "constraint_coverage on the free tier",
     "state": "SHIPPED",
     "evidence": "dchub-mcp-server server.mjs: 'constraint_coverage is real: "
                 "rank_sites returns it keyless (verified live 2026-07-30)'"},
    {"ask": "as_of on every collection response",
     "state": "SHIPPED",
     "evidence": "server instructions publish it as a contract ('Every "
                 "collection response carries a provenance as_of'; responses "
                 "carry a collection-level provenance block with an as_of "
                 "date). 25 as_of sites in server.mjs."},
)

# A description that says WHEN TO PICK THIS TOOL in the user's own words, not
# in call syntax. `Try: get_power_pipeline state=VA` is syntax — it helps an
# agent that has already chosen. `Answers "when is new capacity landing in
# Ohio"` is selection, which is what all seven platforms asked for.
TRIGGER_MARKER = 'Answers "'
EXAMPLE_MARKER = "Try: "


def verdict_trigger_phrases(total, with_trigger, with_example):
    """★ The floor is an INVARIANT, not a target number: a tool curated enough
    to carry a call example should be curated enough to say when to pick it.
    That moves with the repo and cannot rot into a stale quota."""
    if total is None or with_trigger is None or with_example is None:
        return "?", "tools/list unreadable — a failed probe is not a zero"
    if not total:
        return "?", "tools/list returned no tools — not a measurement"
    if with_trigger >= with_example:
        return "PASS", (
            "%d of %d tools carry a selection trigger, at or above the %d that "
            "carry a call example — every curated tool says when to pick it."
            % (with_trigger, total, with_example))
    return "FAIL", (
        "%d of %d tools carry a selection trigger (%s) while %d carry a call "
        "example (%s) — %d tools show an agent HOW to call them but never WHEN "
        "to pick them. Selection happens at call time from the descriptor; "
        "5 of 7 platforms said they hold no durable preference across "
        "sessions, so the descriptor is the whole lever."
        % (with_trigger, total, TRIGGER_MARKER.strip(), with_example,
           EXAMPLE_MARKER.strip(), with_example - with_trigger))


def sse_first_data_frame(text: str) -> str:
    """First `data:` payload of an SSE body.

    ★ Splits on "\\n" ONLY. str.splitlines() also breaks on \\v, \\f, \\x85,
    \\u2028 and \\u2029 — and a tool description carrying one of those cuts the
    JSON mid-string, which is exactly how the first version of this probe
    failed (json: unterminated string at char 43471, against a payload that
    curl + the same parse handled fine). SSE framing is \\n-delimited by spec,
    so anything else in the payload is DATA, not a frame boundary.
    """
    for line in (text or "").split("\n"):
        if line.startswith("data: "):
            return line[6:]
    return text or ""


def _probe_tools():
    """tools/list off the LIVE surface. Self-identifying UA so the call
    classifies internal and this shell can never count itself as agent demand
    (mcp_calls_deloop.real_ua_predicate keys on the UA, not the platform —
    the server overwrites platform). Returns (total, with_trigger,
    with_example) or (None, None, None)."""
    try:
        import json as _json
        import requests
        r = requests.post(
            MCP_ENDPOINT, timeout=8,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "dchub-shell64-relay-closure/1.0"},
            data=_json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": "tools/list", "params": {}}))
        body = sse_first_data_frame(r.content.decode("utf-8", "replace"))
        tools = ((_json.loads(body).get("result") or {}).get("tools")) or []
        if not tools:
            return None, None, None
        descs = [(t.get("description") or "") for t in tools]
        return (len(tools),
                sum(1 for d in descs if TRIGGER_MARKER in d),
                sum(1 for d in descs if EXAMPLE_MARKER in d))
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay_closure] tools/list probe failed: %s", e)
        return None, None, None


# ── lanes ───────────────────────────────────────────────────────────────────

def _lane_a_redeem(cur) -> dict:
    lane = {"lane": "A/redeem_declared_vs_writer",
            "declared": _redeem_stage_basis()}
    r = _one(cur, """
        SELECT
          count(*) FILTER (WHERE claim_used_at   > now() - interval '%d hours'),
          count(*) FILTER (WHERE claim_minted_at > now() - interval '%d hours')
        FROM mcp_high_intent_sessions
    """ % (WRITER_LIVENESS_HOURS, WRITER_LIVENESS_HOURS))
    stamps, mints = (int(r[0] or 0), int(r[1] or 0)) if r else (None, None)

    g = _one(cur, """
        SELECT percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(epoch FROM (claim_used_at - claim_minted_at)))
        FROM mcp_high_intent_sessions
        WHERE claim_used_at IS NOT NULL AND claim_minted_at IS NOT NULL
          AND claim_used_at > now() - interval '30 days'
    """)
    gap = float(g[0]) if (g and g[0] is not None) else None

    status, note = verdict_redeem_writer(stamps, mints, gap)
    lane.update(status=status, note=note,
                redeem_stamps_recent=stamps, mint_stamps_recent=mints,
                liveness_window_hours=WRITER_LIVENESS_HOURS,
                median_mint_to_redeem_seconds=(round(gap, 2) if gap is not None else None),
                machine_threshold_seconds=MACHINE_REDEEM_MAX_SECONDS,
                is_funnel_progress=REDEEM_STAGE_IS_FUNNEL_PROGRESS)
    return lane


def _lane_b_demand(cur) -> dict:
    lane = {"lane": "B/relay_demand_verdict",
            "preconditions": {
                "relay_block_present_and_top_level_since":
                    RELAY_TOP_LEVEL_SINCE.isoformat(),
                "rule_source": "check_relay_opens.py, written before the "
                               "answer was known",
            },
            "excluded_self_traffic_sessions": list(_self_traffic_prefixes())}
    total = _one(cur, "SELECT count(*) FROM relay_opens")
    total = int(total[0] or 0) if total else None

    # Real = a real UA (not our probe/script families) AND not a declared
    # operator session. Both predicates IMPORTED and CALLED, never pinned —
    # a pinned copy would drift from the funnel's own exclusion.
    # Real = a real UA (not our probe/script families) AND not a NAMED
    # operator session. Both predicates IMPORTED and CALLED, never pinned — a
    # pinned copy would drift from the funnel's own exclusion, and this lane
    # exists to agree with the funnel, not to second-guess it.
    #
    # ★ Blank and NULL sids are EXCLUDED, not kept. The token contract mints
    # with sid='' happily, so relay_opens carries a valid blank-sid probe row;
    # the funnel's own join warns that a blank sid would otherwise flip any
    # blank-sid session to human_acted.
    rows = None
    try:
        cur.execute("""
            SELECT DISTINCT left(ro.session_id, 8) FROM relay_opens ro
            WHERE COALESCE(ro.valid, FALSE)
              AND ro.session_id IS NOT NULL AND ro.session_id <> ''
              AND %s AND %s
            ORDER BY 1
        """ % (_real_ua_predicate("ro.user_agent"),
               _external_session_predicate("ro.session_id")))
        rows = [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay_closure] lane B read failed: %s", e)
    real = len(rows) if rows is not None else None

    status, note = verdict_relay_demand(total, real, rows)
    lane.update(status=status, note=note,
                relay_opens_all_time=total, real_external_opens=real,
                unattributed_sessions=rows)
    return lane


def _lane_c_attributability(cur) -> dict:
    lane = {"lane": "C/mint_attributability",
            "floor": MIN_TARGETABLE_COHORT_7D,
            "floor_basis": "at a plausible 5% open rate, the smallest weekly "
                           "cohort in which one open is the expected outcome"}
    rows = None
    try:
        cur.execute("""
            SELECT DISTINCT ON (mcp_session_id)
                   mcp_session_id,
                   LOWER(COALESCE(mcp_client, '')),
                   LOWER(COALESCE(user_agent, ''))
            FROM mcp_high_intent_sessions
            WHERE claim_minted_at IS NOT NULL
              AND first_hit_at > now() - interval '7 days'
              AND %s
            ORDER BY mcp_session_id, first_hit_at
        """ % _external_session_predicate("mcp_session_id"))
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay_closure] lane C read failed: %s", e)

    if rows is None:
        lane.update(status="?", note="unreadable — a failed read is not a zero")
        return lane

    by_platform, gateways, unattributable = {}, {}, 0
    for _sid, client, ua in rows:
        blob = "%s %s" % (client, ua)
        hit = next((p for p in NAMED_PLATFORMS if p in blob), None)
        if hit:
            by_platform[hit] = by_platform.get(hit, 0) + 1
        else:
            unattributable += 1
            gw = next((g for g in GATEWAY_CLIENTS if g in client), None)
            key = gw or (client or "(unnamed)")
            gateways[key] = gateways.get(key, 0) + 1

    top_gw = max(gateways.items(), key=lambda kv: kv[1]) if gateways else None
    gw_note = ("Largest unattributable source is '%s' at %d." % top_gw) if top_gw else ""
    status, note, target = verdict_attributability(by_platform, unattributable, gw_note)
    lane.update(status=status, note=note,
                minted_sessions_7d=len(rows),
                by_named_platform=dict(sorted(by_platform.items(),
                                              key=lambda kv: -kv[1])),
                unattributable=unattributable,
                unattributable_by_client=dict(sorted(gateways.items(),
                                                     key=lambda kv: -kv[1])),
                targetable_platform=target,
                actuator="DCHUB_WALL_SUCCESS_PLATFORMS (dchub-mcp-server) — "
                         "NAMED, deliberately NOT fired; see status")
    return lane


def _lane_d_typed_params(cur) -> dict:
    status, note, readable = verdict_typed_params_window()
    lane = {"lane": "D/typed_params_window", "status": status, "note": note,
            "schema_live_from": TYPED_PARAMS_FIRST_CLEAN_DAY.isoformat(),
            "window_days": PLANNER_WINDOW_DAYS}
    if not readable:
        return lane
    r = _one(cur, """
        SELECT count(DISTINCT session_id) FILTER (WHERE tool_name = 'execute_plan'),
               count(DISTINCT session_id)
        FROM mcp_tool_calls
        WHERE created_at > now() - interval '%d days' AND %s
    """ % (PLANNER_WINDOW_DAYS, _real_ua_predicate("user_agent")))
    if not r:
        lane["note"] += " (selection read FAILED — reported as unknown, not 0)"
        return lane
    planner, total = int(r[0] or 0), int(r[1] or 0)
    lane.update(planner_sessions=planner, total_sessions=total,
                planner_selection_pct=(round(100.0 * planner / total, 2)
                                       if total else None))
    return lane


def _lane_e_asks() -> dict:
    total, trig, ex = _probe_tools()
    status, note = verdict_trigger_phrases(total, trig, ex)
    return {
        "lane": "E/schema_selection_asks",
        "status": status,
        "note": note,
        "basis": "7-platform convergence, 2026-08-20. Every ask was schema and "
                 "selection; not one asked for more data, more coverage, more "
                 "tools, or a better human handoff — which is the same "
                 "conclusion lane B reaches from the human side.",
        "tools_total": total,
        "tools_with_selection_trigger": trig,
        "tools_with_call_example": ex,
        "already_shipped": list(SHIPPED_ASKS),
        "actuator": "tool description strings in dchub-mcp-server — NAMED, "
                    "not fired here; this shell is read-only",
    }


def _state(include_db: bool = True) -> dict:
    out = {
        "ok": True,
        "shell": SHELL_NUMBER,
        "name": SHELL_NAME,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subject": "the agent→human relay is CLOSED by its own pre-registered "
                   "stopping rule; these lanes keep the close auditable and "
                   "reopen it on a real change of state",
        "actuators": "NONE — read-only. Lane C's actuator is named and "
                     "deliberately not fired.",
    }
    if _disabled():
        out.update(status="DISABLED",
                   note="RELAY_CLOSURE_SHELL_DISABLE=1", lanes=[])
        return out
    lanes = []
    c = _conn() if include_db else None
    if c is None:
        out["db"] = "UNAVAILABLE"
        # Lane D's verdict is a pure function of the CALENDAR, so it still
        # answers with no database. The other three are reads; "?" is their
        # honest answer, never 0.
        d_status, d_note, _ = verdict_typed_params_window()
        lanes = [{"lane": "A/redeem_declared_vs_writer", "status": "?",
                  "note": "no database connection"},
                 {"lane": "B/relay_demand_verdict", "status": "?",
                  "note": "no database connection"},
                 {"lane": "C/mint_attributability", "status": "?",
                  "note": "no database connection"},
                 {"lane": "D/typed_params_window", "status": d_status,
                  "note": d_note}]
    else:
        try:
            with c.cursor() as cur:
                lanes = [_lane_a_redeem(cur), _lane_b_demand(cur),
                         _lane_c_attributability(cur), _lane_d_typed_params(cur)]
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    lanes.append(_lane_e_asks())
    out["lanes"] = lanes
    # NEEDS_ATTRIBUTION counts as red: an unattributed open is currently being
    # published as human_acted, so leaving it amber would hide the live
    # misreading this lane exists to surface.
    out["reds"] = [ln["lane"] for ln in lanes
                   if ln.get("status") in ("FAIL", "ESCALATE",
                                           "NEEDS_ATTRIBUTION")]
    return out


# ── ledger ──────────────────────────────────────────────────────────────────
# ★ A closure shell that never ticks is the exact failure tests/
# test_shell_scheduler_coverage.py exists to end (#50/#51 shipped
# tick-on-demand and were never read). This shell's whole claim is that the
# close is RE-CHECKED, so it must beat, and cron_heartbeat._DISPATCH must
# drive it. Registration is not scheduling.

def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises."""
    try:
        body = json.dumps({
            "feed": "relay-closure-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-relay-closure-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[relay_closure] ledger beat failed: %s", e)

# Admin GETs are cached at the EDGE on this zone (CF Rule #3 caches /api/v1/*
# with mode: override_origin, which ignores no-store on the response). A closure
# board that renders a stale tick is worse than no board — its whole claim is
# that the verdicts are re-derived NOW.
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
             "Surrogate-Control": "no-store", "Pragma": "no-cache"}


# Both paths, matching every other shell: the short /admin/<name> form is what
# the vault-map generator and the operator reach for, and scripts/
# generate_vault_map.py detects a shell's cron by searching cron_heartbeat for
# that short form — a shell exposing ONLY /api/v1/... documents itself as
# unrouted and uncronned even when both are wired (handoff_truth reads that way
# today).
@relay_closure_master_shell_bp.route("/admin/relay-closure-shell", methods=["GET"])
@relay_closure_master_shell_bp.route("/api/v1/admin/relay-closure-shell",
                                     methods=["GET"])
def relay_closure_state():
    # ★404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
    # Railway as a dead origin and fails the whole site over to the stale
    # Render backend. Turning off a diagnostic shell must not be able to do
    # that.
    if _disabled():
        return jsonify(ok=False, error="RELAY_CLOSURE_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_state()), 200, _NO_STORE


@relay_closure_master_shell_bp.route(
    "/api/v1/admin/relay-closure-shell/master-tick", methods=["GET", "POST"])
def relay_closure_tick():
    if _disabled():
        return jsonify(ok=False, error="RELAY_CLOSURE_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    st = _state()
    reds = st.get("reds") or []
    _beat_ledger("lanes: %s | reds: %s"
                 % (len(st.get("lanes") or []), ",".join(reds) or "none"),
                 failing=bool(reds))
    logger.info("[relay_closure] tick reds=%s", reds)
    return jsonify(st), 200, _NO_STORE
