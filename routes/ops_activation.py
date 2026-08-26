"""routes/ops_activation.py — the PUBLIC, keyless activation-signal feed.

WHY THIS EXISTS (2026-08-26)
----------------------------
The lagging numbers (paid, MRR, conversions) have read ZERO for the whole
30-day window and cannot say whether anything is turning. Asking "is the funnel
in the right direction" therefore got answered by re-reading a flat zero.

These are the five signals that move FIRST, in the order they would move if the
2026-08-26 changes (data-first envelope, `a-` anon attribution, the Smithery
cohort joining the BYO/connector-URL class) are working:

  1 anon_checkout_clicks   the no-key/no-session cohort's clicks became
                           attributable at all on 2026-08-26. It reads 0
                           honestly today; the FIRST non-zero is the single
                           most informative event available.
  2 remint_ratio           redemption events per distinct agent. ~22x means
                           agents re-mint instead of persisting a key. Falls
                           first if the connector-URL lead works. LOWER IS BETTER.
  3 key_activation_pct     keys that made >= 1 call / keys issued. The largest
                           absolute loss in the funnel.
  4 agents_complete_week   distinct real external agents, COMPLETE ISO weeks.
  5 session_upgrades       a checkout that bound to a session. 0 all-time.

Sibling of /api/v1/ops/deadman and /api/v1/ops/claims: same /api/v1/ops/ prefix
(already edge-bypassed), keyless, `Cache-Control: no-store`, and it documents
its own shape IN the response so nobody has to guess a field name. Keyless
matters here for a second reason: a scheduled cloud agent can read it, and a
stored routine prompt must never carry an admin key.

THE RULES
  * NEVER A FABRICATED ZERO. A probe that fails returns null and says so in
    `basis`. `ok:false` with null values, never 0 — 0 is a finding, null is a
    broken read, and this whole surface exists because a flat 0 was being
    read as a finding.
  * `direction` is computed ONLY where a like-for-like prior window exists.
    Otherwise it is "unknown", never "flat".
  * agents_complete_week compares COMPLETE ISO weeks via
    mcp_calls_deloop.canonical_external_complete_week_sql — NOT a rolling
    window and NEVER the partial current week. That helper exists because
    three separate surfaces published scary numbers produced by comparing
    windows of unequal composition (a rolling-7d agent series, a partial
    current week, and a prior window containing an outlier day). The same
    population read -65% rolling and +37% on complete weeks.
  * `better` states the direction of GOODNESS per signal, because two of the
    five improve by going DOWN. A reader (or an agent) must not have to infer
    that remint_ratio falling is good news.
  * No `%` literal in any SQL here. These run through cur.execute(sql) with no
    params today, but the sibling waterfall's executors bind params
    inconsistently and both fail soft to 0 — LEFT(col,n) needs no escaping and
    cannot silently match nothing.
  * Kill switch OPS_ACTIVATION_DISABLE=1 answers 404, never 5xx: the CF worker
    reads any 5xx from Railway as a dead origin and fails the site over to the
    stale Render backend.

Surface:
  GET /api/v1/ops/activation      keyless · no-store
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
ops_activation_bp = Blueprint("ops_activation", __name__)

WINDOW_DAYS = 7
COHORT_DAYS = 30


def _disabled() -> bool:
    return str(os.environ.get("OPS_ACTIVATION_DISABLE", "")).strip() in ("1", "true", "yes", "on")


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


def _dsn() -> str:
    return (os.environ.get("NEON_REPLICA_URL")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "")


def direction_of(value, prior, better: str):
    """'up' | 'down' | 'flat' | 'unknown' — the raw movement, not a verdict.

    `better` says which way is good; it is reported alongside so a reader never
    has to infer that a falling remint_ratio is an improvement.
    """
    if value is None or prior is None:
        return "unknown"
    try:
        if float(value) > float(prior):
            return "up"
        if float(value) < float(prior):
            return "down"
        return "flat"
    except Exception:
        return "unknown"


def signal(name, label, value, prior, better, basis, unit=None):
    d = direction_of(value, prior, better)
    improving = None
    if d in ("up", "down"):
        improving = (d == better)
    elif d == "flat":
        improving = None
    return {
        "signal": name,
        "label": label,
        "value": value,
        "prior": prior,
        "unit": unit,
        "direction": d,
        "better": better,
        "improving": improving,
        "basis": basis,
    }


def _ratio(num, den, places=1):
    if num is None or den in (None, 0):
        return None
    try:
        return round(float(num) / float(den), places)
    except Exception:
        return None


def read_signals() -> dict:
    """Compute the five signals. Never raises; returns ok=False with nulls."""
    out = {"ok": False, "signals": [], "error": None}
    url = _dsn()
    if not url:
        out["error"] = "no_database_url"
        return out
    try:
        import psycopg2
    except Exception as e:  # pragma: no cover - import guard
        out["error"] = "psycopg2_unavailable: %s" % str(e)[:60]
        return out

    try:
        from mcp_calls_deloop import (real_ua_predicate,
                                      canonical_external_complete_week_sql)
        ua_ok = real_ua_predicate("cc.user_agent")
    except Exception:
        # Fail CLOSED on the self-filter: counting our own probes is how a QA
        # run becomes a customer in a published number.
        ua_ok = ("COALESCE(cc.user_agent,'') !~* "
                 "'(dchub|curl/|probe|verify|audit|harness|human-simulated)')")
        canonical_external_complete_week_sql = None  # type: ignore

    conn = None
    try:
        conn = psycopg2.connect(url, connect_timeout=8)
        conn.autocommit = True
        cur = conn.cursor()

        def q(sql):
            """Scalar probe. None on failure — NEVER 0, which is a finding."""
            try:
                cur.execute(sql)
                r = cur.fetchone()
                return r[0] if r else None
            except Exception as e:
                logger.debug("[ops_activation] probe failed: %s -- %s", sql[:70], e)
                try:
                    conn.rollback()
                except Exception:
                    pass
                return None

        W = f"NOW() - INTERVAL '{WINDOW_DAYS} days'"
        W_PRIOR_LO = f"NOW() - INTERVAL '{WINDOW_DAYS * 2} days'"
        C = f"NOW() - INTERVAL '{COHORT_DAYS} days'"
        C_PRIOR_LO = f"NOW() - INTERVAL '{COHORT_DAYS * 2} days'"

        # ── 1 · anon checkout clicks ──────────────────────────────────────
        # LEFT(...) not LIKE: no % literal (see the module docstring).
        anon_base = ("SELECT COUNT(*) FROM mcp_checkout_clicks cc "
                     f"WHERE cc.sig_ok AND LEFT(cc.ref, 2) = 'a-' AND {ua_ok} ")
        anon_now = q(anon_base + f"AND cc.clicked_at >= {W}")
        anon_prev = q(anon_base + f"AND cc.clicked_at >= {W_PRIOR_LO} AND cc.clicked_at < {W}")

        # ── 2 · re-mint ratio ─────────────────────────────────────────────
        def remint(lo, hi=None):
            where = f"claim_used_at IS NOT NULL AND claim_used_at >= {lo}"
            if hi:
                where += f" AND claim_used_at < {hi}"
            ev = q("SELECT COUNT(*) FROM mcp_high_intent_sessions "
                   f"WHERE {where} AND claim_email IS NULL")
            ag = q("SELECT COUNT(DISTINCT minted_api_key) FROM mcp_high_intent_sessions "
                   f"WHERE {where} AND claim_email IS NULL AND minted_api_key IS NOT NULL")
            return _ratio(ev, ag)

        remint_now = remint(C)
        remint_prev = remint(C_PRIOR_LO, C)

        # ── 3 · key activation ────────────────────────────────────────────
        def activation(lo, hi=None):
            where = f"claim_used_at IS NOT NULL AND claim_used_at >= {lo}"
            if hi:
                where += f" AND claim_used_at < {hi}"
            issued = q("SELECT COUNT(DISTINCT minted_api_key) FROM mcp_high_intent_sessions "
                       f"WHERE {where} AND claim_email IS NULL AND minted_api_key IS NOT NULL")
            called = q("SELECT COUNT(DISTINCT h.minted_api_key) FROM mcp_high_intent_sessions h "
                       f"WHERE {where.replace('claim_used_at', 'h.claim_used_at')} "
                       "AND h.claim_email IS NULL AND h.minted_api_key IS NOT NULL "
                       "AND EXISTS (SELECT 1 FROM mcp_call_log l "
                       "            WHERE l.api_key = h.minted_api_key)")
            r = _ratio(called, issued, places=4)
            return None if r is None else round(r * 100.0, 1)

        act_now = activation(C)
        act_prev = activation(C_PRIOR_LO, C)

        # ── 4 · agents, COMPLETE ISO weeks ────────────────────────────────
        agents_now = agents_prev = None
        if canonical_external_complete_week_sql:
            agents_now = q(canonical_external_complete_week_sql(0))
            agents_prev = q(canonical_external_complete_week_sql(1))

        # ── 5 · session upgrades ──────────────────────────────────────────
        # ★ upgraded_at, NOT created_at. The first cut guessed created_at; the
        # column is upgraded_at (main.py DDL + routes/schema_repair.py), so both
        # windowed probes failed and the feed published `null / unknown` for
        # this signal on its first live read. That is the null-is-not-zero rule
        # doing its job — it surfaced the bug instead of printing a confident 0
        # — but the probe still has to be right.
        su_all = q("SELECT COUNT(*) FROM mcp_session_upgrades")
        su_now = q(f"SELECT COUNT(*) FROM mcp_session_upgrades WHERE upgraded_at >= {W}")
        su_prev = q("SELECT COUNT(*) FROM mcp_session_upgrades "
                    f"WHERE upgraded_at >= {W_PRIOR_LO} AND upgraded_at < {W}")

        out["signals"] = [
            signal("anon_checkout_clicks",
                   "Attributable clicks from the no-key/no-session cohort",
                   anon_now, anon_prev, "up", unit="clicks",
                   basis=(f"mcp_checkout_clicks, sig_ok, ref prefix 'a-', last {WINDOW_DAYS}d vs "
                          f"the {WINDOW_DAYS}d before. Script/self UAs excluded via the canonical "
                          "real-UA predicate. The 'a-' ref only started being minted 2026-08-26, "
                          "so 0 here means 'not yet', not 'nobody clicks'.")),
            signal("remint_ratio",
                   "Claim redemptions per distinct agent (lower is better)",
                   remint_now, remint_prev, "down", unit="redemptions/agent",
                   basis=(f"mcp_high_intent_sessions, agent branch (claim_email IS NULL), last "
                          f"{COHORT_DAYS}d vs the {COHORT_DAYS}d before. High means agents re-mint "
                          "keys instead of persisting one.")),
            signal("key_activation_pct",
                   "Issued keys that made at least one call",
                   act_now, act_prev, "up", unit="percent",
                   basis=(f"distinct minted_api_key with >=1 mcp_call_log row / distinct keys "
                          f"issued, last {COHORT_DAYS}d vs the {COHORT_DAYS}d before.")),
            signal("agents_complete_week",
                   "Distinct real external agents, complete ISO week",
                   agents_now, agents_prev, "up", unit="agents",
                   basis=("mcp_calls_deloop.canonical_external_complete_week_sql — COMPLETE ISO "
                          "weeks, most recent complete vs the one before. NOT a rolling window and "
                          "never the partial current week: comparing windows of unequal "
                          "composition is how the same population read -65% rolling and +37% on "
                          "complete weeks.")),
            signal("session_upgrades",
                   "Checkouts that bound to an MCP session",
                   su_now, su_prev, "up", unit="upgrades",
                   basis=(f"mcp_session_upgrades, last {WINDOW_DAYS}d vs the {WINDOW_DAYS}d "
                          f"before. All-time total: {su_all if su_all is not None else 'unknown'}.")),
        ]
        out["session_upgrades_all_time"] = su_all
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


SHAPE = {
    "top_level": "{ok, generated_at, window_days, cohort_days, signals[], "
                 "session_upgrades_all_time, verdict, shape}",
    "signal": "{signal, label, value, prior, unit, direction, better, improving, basis}",
    "direction": "up | down | flat | unknown — the raw movement between the two "
                 "windows. 'unknown' means a window could not be read; it is NEVER "
                 "reported as 'flat'.",
    "better": "up | down — which direction is GOOD for this signal. Two of the five "
              "improve by going DOWN, so never read 'direction' as a verdict on its own.",
    "improving": "true | false | null — direction == better. null when the movement is "
                 "flat or unknown.",
    "value": "null means the probe FAILED. 0 means it ran and found nothing. Never "
             "conflate them: this feed exists because a flat 0 was being read as a finding.",
    "verdict": "{improving, worsening, flat, unknown} — counts of signals by state. A "
               "summary of the five, not a business conclusion.",
}


def _verdict(signals) -> dict:
    v = {"improving": 0, "worsening": 0, "flat": 0, "unknown": 0}
    for s in signals:
        if s["direction"] == "unknown":
            v["unknown"] += 1
        elif s["direction"] == "flat":
            v["flat"] += 1
        elif s["improving"]:
            v["improving"] += 1
        else:
            v["worsening"] += 1
    return v


@ops_activation_bp.route("/api/v1/ops/activation", methods=["GET"])
def ops_activation():
    """PUBLIC, keyless. The shape is in the response — read `shape`."""
    if _disabled():
        return _no_store(jsonify(ok=False, error="disabled",
                                 note="OPS_ACTIVATION_DISABLE=1")), 404
    feed = read_signals()
    sigs = feed.get("signals") or []
    body = {
        "ok": bool(feed.get("ok")),
        "generated_at": utcnow().isoformat(),
        "window_days": WINDOW_DAYS,
        "cohort_days": COHORT_DAYS,
        "signals": sigs if sigs else None,
        "session_upgrades_all_time": feed.get("session_upgrades_all_time"),
        "verdict": _verdict(sigs) if sigs else None,
        "shape": SHAPE,
    }
    if not feed.get("ok"):
        body["error"] = feed.get("error")
        body["basis"] = "read failed — signals and verdict are null, not 0"
    return _no_store(jsonify(body)), 200


def register_ops_activation(app) -> bool:
    app.register_blueprint(ops_activation_bp)
    return True
