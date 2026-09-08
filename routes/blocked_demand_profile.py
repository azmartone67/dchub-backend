"""blocked_demand_profile.py — what agents are refused, and how deep the
refusal runs. Admin-gated, read-only, and it holds no PII by construction.

WHY THIS EXISTS (2026-09-08). Prospecting asked for "the 172 callers who hit
the wall 10+ times, with platform, tool, hit count and any email" so it could
draft outreach. Three of those four are publishable. The fourth is not, and
the reason is structural rather than a policy preference:

  · ★ A CALLER IS NOT A COMPANY. `caller_id` comes from
    mcp_signal_canonical._compute_caller_id, a preference cascade: lowercased
    email, else session_id, else anon:md5(mcp_client|user_agent[:120]|ip).
    Session ids ROTATE — hosted clients mint one per tool call — so tier 2
    fragments one caller into many. And on the trial_preview path, 82.2% of
    rows (5,255 of 6,267 in the 30d to 2026-08-17) carry the literal generic
    'mcp' as mcp_client, so the tier-3 fingerprint collapses to roughly the
    IP: NAT undercounts it, rotating egress overcounts it. Every count here
    is a count of BUCKETS. This module never calls one a customer.

  · ★ THERE IS ALMOST NOTHING TO CONTACT. `contactability` computes that
    live instead of restating a measurement that rots, and it separates
    `with_email` from `mailable`: nurture outreach is marketing, and
    routes/lost_conversion_outreach.py requires an explicit
    mcp_dev_keys.metadata->>'marketing_opt_in' = 'true'. A caller whose
    email we hold but may not mail is not a lead, and reporting the two as
    one number is how a list gets worked that should not be.

★ THE DEPTH DISTRIBUTION IS THE FINDING, NOT THE CALLER LIST. A caller
refused twenty times is doing live siting diligence; a caller refused once
is browsing. That shape is what Pro positioning is built from, and it needs
no identity at all — which is why this endpoint returns no caller_id, no
email, no IP and no session id, only aggregates over them.

★ NO `%` IN ANY QUERY STRING. Every window is a BOUND PARAMETER
(`NOW() - (INTERVAL '1 day' * %s)`), never interpolated. A literal % in a
predicate embedded in a `sql % args` call site took the live handoff-funnel
endpoint down inside one deploy; keeping the window bound means these
queries cannot be pasted into that trap.

Source is `mcp_funnel_real` — mcp_funnel_canonical WHERE is_synthetic = FALSE
— so our own smoke harness, probes and QA sweeps are already excluded. That
matters more than it sounds: the raw per-day cap counters on the funnel were
99.25% our own harness over 30d, and reading a level off the wrong table is
how a business conclusion gets overstated ~1,200x.

Run:  GET /api/v1/admin/blocked-demand-profile
      GET /api/v1/admin/blocked-demand-profile?days=7
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

blocked_demand_profile_bp = Blueprint("blocked_demand_profile", __name__)

# Depth buckets, widest last. Published in this order ALWAYS, including the
# empty ones: a distribution that silently omits a bucket reads as "nobody is
# there" when it means "no rows landed in it this window", and the deep
# buckets are exactly the ones a short window empties. Same reason a repo-wide
# scan that can return nothing needs a floor.
DEPTH_BUCKETS = (
    ("1-2", 1, 3),
    ("3-9", 3, 10),
    ("10-19", 10, 20),
    ("20-49", 20, 50),
    ("50+", 50, None),
)

# A caller at or above this many refusals in the window. Named once, published
# in the response, and used by every query that reports a "deep" count — so
# the number in `by_tool.deep_callers` and the number implied by the depth
# buckets cannot drift apart.
DEEP_AT = 10

MAX_DAYS = 365
MAX_TOOLS = 25


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


def _int(v):
    """Coerce a COUNT position to int, or None.

    ★ DEFENCE IN DEPTH, and it is cheap. Every field this module publishes
    except `tool` is a count, and counts are assembled by INDEX off the row
    the driver hands back. Nothing stops a future edit to one of these SELECT
    lists from moving an identity column into a count position — and it would
    publish silently, because a caller_id is a perfectly good dict value. This
    makes that outcome None (unmeasured, the vocabulary this module already
    uses for a read it could not make) instead of a leak.
    tests/test_blocked_demand_profile.py::test_payload_carries_no_caller_identity
    drives exactly that case, so the guard tests behaviour rather than a
    promise.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _bucket_case(col: str) -> str:
    """SQL CASE mapping a hit count to a DEPTH_BUCKETS label.

    Built from the tuple rather than typed out, so the labels the response
    publishes and the boundaries the SQL applies cannot disagree. Emits no
    `%` and no user input.
    """
    parts = []
    for label, lo, hi in reversed(DEPTH_BUCKETS):
        parts.append("WHEN %s >= %d THEN '%s'" % (col, lo, label))
    return "CASE " + " ".join(parts) + " ELSE '%s' END" % DEPTH_BUCKETS[0][0]


def run_profile(days: int = 30) -> dict:
    days = max(1, min(int(days or 30), MAX_DAYS))
    out = {
        "ok": True,
        "window_days": days,
        "deep_at": DEEP_AT,
        "source": "mcp_funnel_real",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    c = _conn()
    if c is None:
        # ★ None, never 0. An unreadable database is UNMEASURED; publishing
        # zeros would look like an absence of demand, which is the single
        # wrong conclusion this whole endpoint exists to prevent.
        out.update({"ok": False, "error": "no database url",
                    "totals": None, "caller_depth": None,
                    "by_tool": None, "contactability": None})
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COUNT(DISTINCT caller_id),"
                "       COUNT(DISTINCT session_id), COUNT(DISTINCT tool_requested)"
                "  FROM mcp_funnel_real"
                " WHERE created_at >= NOW() - (INTERVAL '1 day' * %s)", (days,))
            r = cur.fetchone() or (0, 0, 0, 0)
            out["totals"] = {"signals": _int(r[0]), "callers": _int(r[1]),
                             "sessions": _int(r[2]), "tools": _int(r[3])}

            # ── depth distribution ───────────────────────────────────────
            cur.execute(
                "WITH c AS ("
                "  SELECT caller_id, COUNT(*) AS hits,"
                "         MAX(NULLIF(user_email,'')) AS email"
                "    FROM mcp_funnel_real"
                "   WHERE created_at >= NOW() - (INTERVAL '1 day' * %s)"
                "     AND caller_id IS NOT NULL"
                "   GROUP BY caller_id)"
                " SELECT " + _bucket_case("hits") + " AS bucket,"
                "        COUNT(*) AS callers, SUM(hits) AS signals,"
                "        COUNT(*) FILTER (WHERE email IS NOT NULL) AS with_email"
                "   FROM c GROUP BY 1", (days,))
            seen = {row[0]: row for row in cur.fetchall()}
            tot_sig = sum((_int(seen[k][2]) or 0) for k in seen) or 0
            out["caller_depth"] = []
            for label, _lo, _hi in DEPTH_BUCKETS:
                row = seen.get(label) or (None, 0, 0, 0)
                sig = _int(row[2]) or 0
                out["caller_depth"].append({
                    "bucket": label,
                    "callers": _int(row[1]) or 0,
                    "signals": sig,
                    # None, not 0.0 — a share off an empty denominator is a
                    # fabricated statistic, and this stage publishes plenty
                    # of honest zeros already.
                    "signal_share_pct": (round(100.0 * sig / tot_sig, 1)
                                         if tot_sig else None),
                    "callers_with_email": _int(row[3]) or 0,
                })

            # ── per tool ─────────────────────────────────────────────────
            # ★ `callers` here is COUNT(DISTINCT caller_id). The funnel's
            # `top_signal_tools_30d` publishes COUNT(DISTINCT ip_address)
            # under the SAME field name. Those are different id spaces and
            # will not match; joining or comparing them by eye is the
            # declared-vs-classified trap that silently dropped a third of
            # the rows on /ai/reach. The basis says so in the response.
            cur.execute(
                "SELECT tool_requested, COUNT(*), COUNT(DISTINCT caller_id),"
                "       COUNT(DISTINCT session_id)"
                "  FROM mcp_funnel_real"
                " WHERE created_at >= NOW() - (INTERVAL '1 day' * %s)"
                "   AND tool_requested IS NOT NULL"
                " GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT %s",
                (days, MAX_TOOLS))
            # `tool` is the ONE string published verbatim, and it is the
            # subject of the endpoint. It is written server-side by
            # signalPaywall() from the tool being invoked, not supplied as
            # free text by the caller, so it is not an identity carrier.
            tools = [{"tool": t, "signals": _int(n), "callers": _int(ca),
                      "sessions": _int(se)}
                     for t, n, ca, se in cur.fetchall()]
            cur.execute(
                "WITH ct AS ("
                "  SELECT tool_requested AS tool, caller_id, COUNT(*) AS hits"
                "    FROM mcp_funnel_real"
                "   WHERE created_at >= NOW() - (INTERVAL '1 day' * %s)"
                "     AND tool_requested IS NOT NULL AND caller_id IS NOT NULL"
                "   GROUP BY 1,2)"
                " SELECT tool, COUNT(*) FILTER (WHERE hits >= %s)"
                "   FROM ct GROUP BY tool", (days, DEEP_AT))
            deep = {t: d for t, d in cur.fetchall()}
            for row in tools:
                row["deep_callers"] = _int(deep.get(row["tool"], 0)) or 0
            out["by_tool"] = tools

            # ── contactability: the number that closes the outreach idea ──
            cur.execute(
                "WITH c AS ("
                "  SELECT caller_id, COUNT(*) AS hits,"
                "         MAX(NULLIF(user_email,'')) AS email"
                "    FROM mcp_funnel_real"
                "   WHERE created_at >= NOW() - (INTERVAL '1 day' * %s)"
                "     AND caller_id IS NOT NULL"
                "   GROUP BY caller_id)"
                " SELECT COUNT(*),"
                "        COUNT(*) FILTER (WHERE email IS NOT NULL),"
                "        COUNT(*) FILTER (WHERE email IS NOT NULL AND EXISTS ("
                "            SELECT 1 FROM mcp_dev_keys k"
                "             WHERE lower(k.email) = lower(c.email)"
                "               AND k.metadata->>'marketing_opt_in' = 'true')),"
                "        COUNT(*) FILTER (WHERE hits >= %s),"
                "        COUNT(*) FILTER (WHERE hits >= %s AND email IS NOT NULL)"
                "   FROM c", (days, DEEP_AT, DEEP_AT))
            r = cur.fetchone() or (0, 0, 0, 0, 0)
            out["contactability"] = {
                "callers": _int(r[0]),
                "with_email": _int(r[1]),
                "mailable": _int(r[2]),
                "deep_callers": _int(r[3]),
                "deep_with_email": _int(r[4]),
                "basis": (
                    "with_email = caller buckets carrying any user_email on a "
                    "signal row in the window. mailable = that subset whose "
                    "email maps to an mcp_dev_keys row with "
                    "metadata->>'marketing_opt_in' = 'true' — the SAME gate "
                    "routes/lost_conversion_outreach.py applies, read from "
                    "here rather than restated, so the two cannot drift. "
                    "★ mailable is the only one of these numbers that is a "
                    "lead count. with_email minus mailable is a set of people "
                    "who never agreed to hear from us."),
            }
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": str(exc)[:300]})
        for k in ("totals", "caller_depth", "by_tool", "contactability"):
            out.setdefault(k, None)
    finally:
        try:
            c.close()
        except Exception:
            pass

    out["basis"] = (
        "Aggregates over mcp_funnel_real (mcp_upgrade_signals with "
        "is_synthetic = FALSE), rolling window ending now. Every figure is a "
        "count of caller_id BUCKETS, not of people or companies: caller_id is "
        "email, else session_id, else anon:md5(mcp_client|user_agent|ip), and "
        "session ids rotate while mcp_client is the literal generic 'mcp' on "
        "the large majority of trial_preview rows — so the fingerprint is "
        "effectively IP-shaped, undercounted behind NAT and overcounted "
        "behind rotating egress. Read the SHAPE of the depth distribution, "
        "never a bucket as a customer count.")
    out["not_measured_here"] = (
        "Platform. mcp_funnel_real.mcp_client is the degraded copy of caller "
        "identity (82.2% literal 'mcp' on trial_preview as of 2026-08-17), so "
        "a tool x platform cross-tab off this table would be ~82% one "
        "meaningless cell. The platform dimension is published honestly and "
        "separately as signals_by_platform_30d on /api/v1/mcp/funnel. "
        "Also NOT here: any caller_id, email, IP or session id — this "
        "endpoint is aggregates only, by design and not by redaction.")
    out["compare_carefully"] = (
        "by_tool.callers is COUNT(DISTINCT caller_id). "
        "/api/v1/mcp/funnel's top_signal_tools_30d publishes COUNT(DISTINCT "
        "ip_address) under the same field name. Different id spaces — the two "
        "will not match and neither is wrong.")
    return out


@blocked_demand_profile_bp.route("/api/v1/admin/blocked-demand-profile",
                                 methods=["GET"])
def blocked_demand_profile_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        days = int(request.args.get("days", 30))
    except Exception:
        days = 30
    return jsonify(run_profile(days))


def register_blocked_demand_profile(app) -> None:
    try:
        app.register_blueprint(blocked_demand_profile_bp)
    except Exception:
        pass
