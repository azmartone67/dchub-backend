"""
routes/relay_conversion_watch.py — the agent→human→money funnel (2026-07-29).

WHY THIS EXISTS

For the product's entire history, zero conversions have been attributable to an
AI agent. mcp_conversions.platform is NULL on every real row. relay_opens holds
two rows and BOTH are ours ('dchub-ops-verify/1.0', 'human-simulated/2.0'), so
no real human has ever opened a handoff link.

That was read for months as "the upgrade design does not convert". It was not.
On 2026-07-28 it was measured from the client seat that `for_your_human` — the
handoff link itself — was NEVER EMITTED: an anonymous agent calling a paid tool
is auto-trialled INLINE, so buildPaywallExtras never ran. Four consecutive live
calls to get_grid_intelligence returned for_your_human=false. The design did not
underperform; it was never in the payload. It began emitting 2026-07-28.

The reason that took months to notice is the thing this module fixes:
**nothing counts emission.** relay_opens counts OPENS. With no numerator,
"never emitted" and "emitted and ignored" produce the identical observation —
zero — and the two demand opposite responses. A funnel missing its first stage
cannot distinguish a broken pipe from an unpersuasive offer.

WHAT THIS MEASURES

  stage 1  ELIGIBLE   calls that SHOULD have carried a handoff link  (PROXY)
  stage 2  OPENED     relay_opens, excluding our own probes          (exact)
  stage 3  CONVERTED  mcp_conversions, non-test, MCP-attributed      (exact)

★2026-07-29 STAGE 1 IS NOW EXACT. The proxy is gone.

I originally concluded "nothing records emission" and derived stage 1 from call
status as an upper bound. That was WRONG: mcp_high_intent_sessions has carried
claim_minted_at, claim_used_at, claim_page_opened_at and claim_email the whole
time. The operator's own dashboard was reading them. I searched for tables named
%relay%/%handoff% and missed the one named for the CLAIM, which is what the
handoff token actually is. Searching for the word I expected instead of the thing
it does is the same error as reading the wrong field, one level up.

And the exact columns say something the proxy could never have shown:

    7d   1,271 high-intent -> 1,259 minted -> 1,257 REDEEMED -> 0 human opens
         median time from mint to redeem: 0.85s   (min 0.58s)

Sub-second redemption is not a human clicking. THE AGENT redeems the claim token
itself and mints a free key: 1,025 keys issued that way in 7d. The paywall is not
failing to convert — it is a free-key dispenser, and the human never enters the
loop. 97.5% of redemptions complete inside 5 seconds.

That reframes the funnel: the leak is not persuasion, it is ARBITRAGE. This watch
therefore measures machine redemption as a first-class stage rather than counting
it as progress.

The gateway mints the handoff token LOCALLY (HMAC over
session|tool|tier|unixtime with DCHUB_INTERNAL_KEY) and never calls the backend
to do it, so no server-side row exists at mint time. Emission is therefore
derived from the call statuses under which buildHumanRelay fires
(RELAY_ELIGIBLE_STATUSES). That over-counts if the builder bails — it returns
undefined when DCHUB_HUMAN_RELAY=0 or DCHUB_INTERNAL_KEY is unset — so the
eligible count is an UPPER BOUND on emission, never a measurement of it.

Reporting it as truth would repeat the exact error this module exists to correct:
a number that looks like evidence while resting on an assumption. The only way to
make stage 1 exact is for the gateway to record each mint. Until it does, every
rate derived from stage 1 is reported as `_basis: "proxy_upper_bound"`.

Our own traffic is excluded from every stage. Both existing relay_opens rows are
ours; counting them would have shown a working funnel where none exists.

Run:  GET /api/v1/admin/relay-watch          (admin-gated, read-only)
      GET /api/v1/admin/relay-watch?days=30
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

relay_conversion_watch_bp = Blueprint("relay_conversion_watch", __name__)

# Statuses under which the gateway's buildHumanRelay() attaches a handoff link.
# Kept as data so the proxy's basis is inspectable rather than buried in SQL —
# if the gateway's emission conditions change, this list is the one place that
# has to move, and the response publishes it so a reader can check it.
RELAY_ELIGIBLE_STATUSES = (
    "trial_used", "trial_taste_inline", "trial_taste_bounded",
    "blocked_paid_only", "anon_daily_cap", "depth_teased",
    "mpp_offer_prewall", "mpp_challenge",
)

# Our own probes/shells. Both rows currently in relay_opens match these, so
# omitting this filter reports a funnel that is entirely self-traffic.
_OURS = ("dchub%", "DCHub/%", "Globeholder-%", "human-simulated%", "verify%",
         "audit%", "%probe%", "%validator%", "%certifier%")

# The date the handoff link began emitting. Every stage-1 number before this is
# structurally zero, so a rate spanning it is meaningless — the response carries
# this so nobody averages across the discontinuity.
RELAY_LIVE_SINCE = "2026-07-28"

# A claim redeemed within this many seconds of being minted was taken by the
# agent, not a human. Measured 7d: median 0.85s, 97.5% inside 5s, and the
# distribution is sharply bimodal. A human would have to be told the link,
# switch context and load a page — not in 5 seconds. The histogram ships beside
# the count so this threshold is inspectable rather than load-bearing.
MACHINE_REDEEM_SECONDS = 5


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _conn():
    import psycopg2
    dsn = (os.environ.get("NEON_REPLICA_URL")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not dsn:
        return None
    c = psycopg2.connect(dsn)
    c.set_session(readonly=True, autocommit=True)
    return c


def _not_ours(col: str) -> str:
    """SQL excluding our own user agents, as a PARAMETERISED fragment.

    The patterns are bound, not inlined. Inlining them puts literal % into a
    query that also carries args, and psycopg2 then %-formats the whole SQL
    string — a 500 on every call, and the trap this repo has hit repeatedly.
    Binding sidesteps the escaping question entirely instead of solving it
    correctly once and hoping the next edit remembers.

    Callers MUST append _OURS_PARAM to their args in the same position.
    """
    return f"NOT (COALESCE({col},'') ILIKE ANY(%s))"


# Bind-ready form of _OURS. One list, one definition — the filter and its
# documentation cannot drift.
_OURS_PARAM = list(_OURS)


def _row(c, sql: str, args=None):
    """Single row, or None. Same never-raises contract as _scalar."""
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchone()
    except Exception:
        return None


def _scalar(c, sql: str, args=None):
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def run_relay_watch(days: int = 7) -> dict:
    days = max(1, min(int(days or 7), 90))
    c = _conn()
    if c is None:
        return {"watch": "relay_conversion", "status": "INDETERMINATE",
                "error": "no database URL configured — nothing was measured"}
    try:
        window = f"now() - interval '{days} days'"

        # ── stage 1: minted (EXACT) ──────────────────────────────────
        # first_hit_at scopes the window: a session's stages all belong to the
        # window it STARTED in, so a mint never lands in a different bucket
        # from its own redemption.
        hi = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND {_not_ours('user_agent')}""", (_OURS_PARAM,))
        minted = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND claim_minted_at IS NOT NULL
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))

        # ── stage 2: WHO redeemed it — the arbitrage stage ───────────
        machine = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND claim_used_at IS NOT NULL
               AND claim_minted_at IS NOT NULL
               AND EXTRACT(EPOCH FROM (claim_used_at - claim_minted_at)) < %s
               AND {_not_ours('user_agent')}""", (MACHINE_REDEEM_SECONDS, _OURS_PARAM))
        redeemed = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND claim_used_at IS NOT NULL
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))
        keys_issued = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND COALESCE(minted_api_key,'') <> ''
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))
        # The gap histogram ships with the number so the 5s threshold is
        # inspectable rather than load-bearing. If the distribution ever stops
        # being bimodal, the threshold is the first thing to distrust.
        gaps = _row(c, f"""
            SELECT COUNT(*) FILTER (WHERE g < 2), COUNT(*) FILTER (WHERE g >= 2 AND g < 5),
                   COUNT(*) FILTER (WHERE g >= 5 AND g < 10), COUNT(*) FILTER (WHERE g >= 10 AND g < 60),
                   COUNT(*) FILTER (WHERE g >= 60),
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY g)::numeric, 2)
              FROM (SELECT EXTRACT(EPOCH FROM (claim_used_at - claim_minted_at)) g
                      FROM mcp_high_intent_sessions
                     WHERE first_hit_at > {window} AND claim_used_at IS NOT NULL
                       AND claim_minted_at IS NOT NULL
                       AND {_not_ours('user_agent')}) t""", (_OURS_PARAM,))

        # ── stage 3: human action (EXACT) ────────────────────────────
        human_opened = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND claim_page_opened_at IS NOT NULL
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))
        identified = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_high_intent_sessions
             WHERE first_hit_at > {window} AND COALESCE(claim_email,'') <> ''
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))

        # Secondary, different mechanism: signed /upgrade/h/<token> opens.
        opened = _scalar(c, f"""
            SELECT COUNT(*) FROM relay_opens
             WHERE ts > {window} AND COALESCE(valid,false) = true
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))

        # ── stage 4: converted (EXACT) ───────────────────────────────
        converted = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_conversions
             WHERE created_at > {window} AND COALESCE(is_test,false) = false""")
        converted_mcp = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_conversions
             WHERE created_at > {window} AND COALESCE(is_test,false) = false
               AND COALESCE(platform,'') <> ''""")
        mrr = _scalar(c, f"""
            SELECT COALESCE(SUM(COALESCE(mrr_cents,0) - COALESCE(refunded_cents,0)),0)/100.0
              FROM mcp_conversions
             WHERE created_at > {window} AND COALESCE(is_test,false) = false""")

        def rate(num, den):
            if not den or num is None:
                return None
            return round(100.0 * num / den, 3)

        unreadable = [n for n, v in (("minted", minted), ("human_opened", human_opened),
                                     ("converted", converted)) if v is None]
        return {
            "watch": "relay_conversion",
            "window_days": days,
            "status": "INDETERMINATE" if unreadable else "OK",
            "unreadable_stages": unreadable,
            "relay_live_since": RELAY_LIVE_SINCE,
            "stages": {
                "1_high_intent": {"sessions": hi, "_basis": "exact"},
                "2_minted": {"claims": minted, "_basis": "exact",
                             "_note": "claim_minted_at — a handoff token was created."},
                "3_redeemed_by_MACHINE": {
                    "count": machine, "of_all_redemptions": redeemed,
                    "free_keys_issued": keys_issued,
                    "threshold_seconds": MACHINE_REDEEM_SECONDS,
                    "gap_histogram": {"lt_2s": gaps[0] if gaps else None,
                                      "s2_5": gaps[1] if gaps else None,
                                      "s5_10": gaps[2] if gaps else None,
                                      "s10_60": gaps[3] if gaps else None,
                                      "gt_60s": gaps[4] if gaps else None,
                                      "median_seconds": float(gaps[5]) if gaps and gaps[5] is not None else None},
                    "_basis": "exact",
                    "_note": ("THE ARBITRAGE. A claim redeemed within "
                              f"{MACHINE_REDEEM_SECONDS}s of minting was taken by the AGENT, "
                              "not a human — it mints itself a free key. This is NOT funnel "
                              "progress; it is the paywall acting as a free-key dispenser. "
                              "The histogram ships so the threshold is inspectable."),
                },
                "4_human_opened": {"count": human_opened, "relay_opens": opened,
                                   "identified_email": identified, "_basis": "exact",
                                   "_note": ("claim_page_opened_at. All 4 all-time rows trace to "
                                             "cursor render-verify probes, a Grok probe and an "
                                             "indexer — so the true human count is plausibly 0.")},
                "5_converted": {"conversions": converted, "mcp_attributed": converted_mcp,
                                "net_mrr_usd": float(mrr or 0), "_basis": "exact"},
            },
            "rates": {
                "mint_per_high_intent_pct": rate(minted, hi),
                "machine_arbitrage_pct": rate(machine, minted),
                "human_open_per_mint_pct": rate(human_opened, minted),
                "convert_per_human_open_pct": rate(converted_mcp, human_opened),
                "_note": ("machine_arbitrage_pct is the headline. A HIGH value means every "
                          "paywall hit is being converted into a free key for the agent "
                          "instead of a decision for a human. Minting EARLIER raises it."),
            },
            "next_instrument": (
                "The remaining unknown is whether an agent ever SHOWS the link to its human "
                "before redeeming it. We can see the redemption, not the conversation. A "
                "claim that is minted but deliberately left unredeemed would be the signal, "
                "and today there are ~2 of those a week."
            ),
        }
    finally:
        try:
            c.close()
        except Exception:
            pass


@relay_conversion_watch_bp.route("/api/v1/admin/relay-watch", methods=["GET"])
def relay_watch_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        days = int(request.args.get("days", 7))
    except Exception:
        days = 7
    return jsonify(run_relay_watch(days))


def register_relay_conversion_watch(app) -> None:
    try:
        app.register_blueprint(relay_conversion_watch_bp)
    except Exception:
        pass
