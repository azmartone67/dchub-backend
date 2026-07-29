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

★ STAGE 1 IS A PROXY AND IS LABELLED AS ONE EVERYWHERE IT APPEARS.

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

        # ── stage 1: eligible (PROXY — upper bound on emission) ──────
        eligible = _scalar(c, f"""
            SELECT COUNT(*) FROM mcp_call_log
             WHERE timestamp > {window}
               AND status = ANY(%s)
               AND COALESCE(platform,'') NOT ILIKE %s
               AND {_not_ours('user_agent')}""", (list(RELAY_ELIGIBLE_STATUSES), 'dchub%', _OURS_PARAM))

        eligible_agents = _scalar(c, f"""
            SELECT COUNT(DISTINCT COALESCE(NULLIF(api_key,''),
                                           'sess:'||COALESCE(session_id,'')))
              FROM mcp_call_log
             WHERE timestamp > {window}
               AND status = ANY(%s)
               AND COALESCE(platform,'') NOT ILIKE %s
               AND {_not_ours('user_agent')}""", (list(RELAY_ELIGIBLE_STATUSES), 'dchub%', _OURS_PARAM))

        # ── stage 2: opened (exact) ──────────────────────────────────
        opened = _scalar(c, f"""
            SELECT COUNT(*) FROM relay_opens
             WHERE ts > {window} AND COALESCE(valid,false) = true
               AND {_not_ours('user_agent')}""", (_OURS_PARAM,))
        opened_all_time = _scalar(c, f"""
            SELECT COUNT(*) FROM relay_opens
             WHERE COALESCE(valid,false) = true AND {_not_ours('user_agent')}""", (_OURS_PARAM,))

        # ── stage 3: converted (exact) ───────────────────────────────
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

        unreadable = [n for n, v in (("eligible", eligible), ("opened", opened),
                                     ("converted", converted)) if v is None]
        return {
            "watch": "relay_conversion",
            "window_days": days,
            # A stage that could not be read must not average into a rate and
            # must not read as zero.
            "status": "INDETERMINATE" if unreadable else "OK",
            "unreadable_stages": unreadable,
            "relay_live_since": RELAY_LIVE_SINCE,
            "stages": {
                "1_eligible": {
                    "calls": eligible, "agents": eligible_agents,
                    "_basis": "proxy_upper_bound",
                    "_note": ("Derived from call status, NOT from a record of emission — "
                              "the gateway mints handoff tokens locally and logs nothing. "
                              "This OVER-counts when buildHumanRelay bails "
                              "(DCHUB_HUMAN_RELAY=0 or DCHUB_INTERNAL_KEY unset), so treat "
                              "it as a ceiling on emission, never as emission."),
                    "_statuses": list(RELAY_ELIGIBLE_STATUSES),
                },
                "2_opened": {"opens": opened, "opens_all_time": opened_all_time,
                             "_basis": "exact",
                             "_note": "valid=true only; our own probes excluded."},
                "3_converted": {"conversions": converted,
                                "mcp_attributed": converted_mcp,
                                "net_mrr_usd": float(mrr or 0),
                                "_basis": "exact",
                                "_note": ("mcp_attributed counts rows with a non-empty "
                                          "platform. It has been 0 for every real "
                                          "conversion in the product's history.")},
            },
            "rates": {
                "open_per_eligible_pct": rate(opened, eligible),
                "convert_per_open_pct": rate(converted_mcp, opened),
                "_basis": ("proxy_upper_bound — both rates inherit stage 1's basis, so a "
                           "LOW open rate may mean the link is unpersuasive OR that fewer "
                           "links were emitted than eligible calls suggest. These rates "
                           "cannot separate those until emission is recorded."),
            },
            "next_instrument": (
                "Record each handoff mint gateway-side (session, tool, tier, ts). That "
                "single row turns stage 1 from a ceiling into a measurement and makes "
                "both rates trustworthy. Until then this watch can prove the funnel "
                "MOVED, but not why it did not."
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
