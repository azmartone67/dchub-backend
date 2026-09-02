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


def _complete_week_starts(today: _dt.date) -> list[_dt.date]:
    """Mondays of the two COMPLETE ISO weeks canonical_external_complete_week_sql
    counts — weeks_back=1 then 0 — for a UTC `today`. The SQL's
    date_trunc('week', now()) is the same Monday boundary on a UTC server."""
    this_monday = today - _dt.timedelta(days=today.weekday())
    return [this_monday - _dt.timedelta(weeks=2),
            this_monday - _dt.timedelta(weeks=1)]


def _complete_week_spans(today: _dt.date) -> list[tuple]:
    """Half-open [Mon 00:00Z, next Mon 00:00Z) spans for those two weeks —
    the shape weekly_series.comparability_for_spans takes."""
    return [(_dt.datetime.combine(d, _dt.time.min, tzinfo=_dt.timezone.utc),
             _dt.datetime.combine(d + _dt.timedelta(weeks=1), _dt.time.min,
                                  tzinfo=_dt.timezone.utc))
            for d in _complete_week_starts(today)]


def _complete_week_comparability(today: _dt.date | None = None):
    """weekly_series.comparability_for_spans over the same two windows the
    agents signal divides, or None when the marker list cannot be read.
    Lazy import: weekly_series is a reports module and this feed must not
    fail to import because of it."""
    try:
        from routes.weekly_series import comparability_for_spans
    except Exception:
        return None
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    try:
        return comparability_for_spans(_complete_week_spans(today))
    except Exception:
        return None


def withhold_across_definition_change(sig: dict, comp) -> dict:
    """★ 2026-09-02. Measured live at 00:23Z: agents_complete_week published
    value 35, prior 17, direction "up", improving TRUE — across
    dchub-mcp-server#202 (2026-08-18 06:31Z), which removed DC Hub's own
    GitHub Actions from the population INSIDE the prior week. weekly-series
    refuses that exact pair (comparability.quotable_as_trend=false); this
    feed had no comparability field at all and rendered it as the funnel
    turning.

    Same rule flask_mcp_endpoints._mark_wow_comparability applies to the
    funnel's *_wow_pct keys: when the two windows straddle a definition
    change (or both predate a correction) the MOVEMENT is withheld —
    direction "withheld", improving null — and `comparability` says why. The
    two LEVELS stay published. A comparability that could not be computed is
    withheld too: publishing improving:true on an unchecked pair is the
    defect, so the check fails closed.
    """
    sig["comparability"] = comp
    unsafe = (not isinstance(comp, dict)
              or bool(comp.get("crosses_definition_change"))
              or bool(comp.get("superseded_by_correction")))
    if unsafe:
        sig["direction"] = "withheld"
        sig["improving"] = None
        sig["withheld_reason"] = (
            comp.get("means") if isinstance(comp, dict) else
            "comparability could not be computed (weekly_series marker list "
            "unreadable) — an unchecked pair is not published as a movement")
    return sig


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

        def qrows(sql):
            """Multi-row probe. None on failure — NEVER [], which is a finding.

            Same contract as q() one row up: a failed read and an empty result
            must stay distinguishable, because this feed exists because a flat
            0 was being read as a finding.
            """
            try:
                cur.execute(sql)
                return cur.fetchall() or []
            except Exception as e:
                logger.debug("[ops_activation] rows probe failed: %s -- %s", sql[:70], e)
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

        # ── 5b · WHICH BIND SHAPE, and why 5 alone reads as total failure ──
        #
        # ★2026-08-30. session_upgrades has been 0 all-time since 2026-06-06 and
        # was being read as "the funnel's terminal step has never fired". It is
        # not the terminal step. server.mjs picks ONE of several binders for the
        # Stripe client_reference_id, by what the caller holds:
        #
        #   holds a durable key   -> _stripeWithKey     -> 'pk-' / 'k-'
        #   keyless, has session  -> _stripeWithSession -> bare <sid>
        #   neither (Smithery)    -> _stripeWithAnon    -> 'a-'
        #
        # The call shape is _stripeWithAnon(_stripeWithSession(url, sid)) with
        # _stripeWithKey substituted when a key is present, and EVERY binder is
        # idempotent — so the first to run wins, and only the middle one ever
        # writes mcp_session_upgrades (main.py's Fix E branch explicitly
        # excludes pk-/k-/a-/DCM-/tu-/ref_). A paying customer almost certainly
        # holds a key, which routes them to 'pk-' and never to that table.
        #
        # So a 0 there is consistent with BOTH "nobody converts" and "nobody is
        # ROUTED to this shape", and the signal alone cannot separate them.
        # This block reports the shapes side by side so it can.
        #
        # ★ CLICKS, not conversions, and the basis says so out loud. It counts
        # mcp_checkout_clicks — the signed link being followed — because that is
        # the table that records the ref shape. It answers "which binder is
        # being emitted and clicked", which is the routing question. It does NOT
        # answer "which binder converts"; the only shape with a conversion table
        # is 'session' (mcp_session_upgrades), and that asymmetry is the finding.
        #
        # LEFT(...) not LIKE: no % literal (module docstring, rule 5).
        _shape_case = (
            "CASE "
            "WHEN ref IS NULL OR ref = '' THEN 'unbound' "
            "WHEN LEFT(ref, 3) = 'pk-' THEN 'key_pack' "
            "WHEN LEFT(ref, 2) = 'k-' THEN 'key_sub' "
            "WHEN LEFT(ref, 2) = 'a-' THEN 'anon' "
            "WHEN LEFT(ref, 4) = 'DCM-' THEN 'pair_code' "
            "WHEN LEFT(ref, 3) = 'tu-' THEN 'topup_token' "
            "WHEN LEFT(ref, 4) = 'ref_' THEN 'attribution' "
            "WHEN LEFT(ref, 4) = 'mcp:' THEN 'session_via_agent_link' "
            "ELSE 'session' END"
        )

        def _shapes(since=None):
            where = "sig_ok"
            if since:
                where += f" AND clicked_at >= {since}"
            rows = qrows(f"SELECT {_shape_case} AS shape, COUNT(*) "
                         f"FROM mcp_checkout_clicks WHERE {where} GROUP BY 1")
            if rows is None:
                return None
            return {str(r[0]): int(r[1] or 0) for r in rows}

        binding_cohort = _shapes(C)
        binding_all = _shapes()

        # Which shapes can even reach a conversion table, stated rather than
        # implied — this is what makes the 0 legible instead of alarming.
        out["checkout_binding"] = {
            "cohort_days": COHORT_DAYS,
            "clicks_by_shape_cohort": binding_cohort,
            "clicks_by_shape_all_time": binding_all,
            "writes_session_upgrades": ["session", "session_via_agent_link"],
            "basis": (
                "mcp_checkout_clicks grouped by client_reference_id PREFIX, "
                "sig_ok only. CLICKS, not conversions — this names which binder "
                "server.mjs emitted, not which one paid. null (not {}) means the "
                "probe failed. Only the shapes in writes_session_upgrades can "
                "produce an mcp_session_upgrades row; 'key_pack'/'key_sub' land "
                "on the key-hash branch and 'anon' is excluded by design, so "
                "session_upgrades == 0 is NOT by itself evidence that nothing "
                "converted."
            ),
        }

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
            withhold_across_definition_change(signal(
                   "agents_complete_week",
                   "Distinct real external agents, complete ISO week",
                   agents_now, agents_prev, "up", unit="agents",
                   basis=("mcp_calls_deloop.canonical_external_complete_week_sql — COMPLETE ISO "
                          "weeks, most recent complete vs the one before. NOT a rolling window and "
                          "never the partial current week: comparing windows of unequal "
                          "composition is how the same population read -65% rolling and +37% on "
                          "complete weeks. ★ direction is 'withheld' (improving null) when "
                          "the two weeks straddle a registered definition change — see "
                          "comparability / withheld_reason on this signal.")),
                _complete_week_comparability()),
            signal("session_upgrades",
                   "Checkouts that bound to an MCP session",
                   su_now, su_prev, "up", unit="upgrades",
                   basis=(f"mcp_session_upgrades, last {WINDOW_DAYS}d vs the {WINDOW_DAYS}d "
                          f"before. All-time total: {su_all if su_all is not None else 'unknown'}. "
                          "★ ONE OF SEVERAL BIND SHAPES, not the funnel's terminal "
                          "step: only a keyless caller WITH an MCP session writes this "
                          "table — a caller holding a durable key is bound 'pk-'/'k-' "
                          "and one holding neither is bound 'a-', both by design. See "
                          "top-level checkout_binding before reading a 0 here as "
                          "'nobody converted'.")),
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
                 "session_upgrades_all_time, checkout_binding, verdict, shape}",
    "checkout_binding": "{cohort_days, clicks_by_shape_cohort, "
                        "clicks_by_shape_all_time, writes_session_upgrades[], basis} "
                        "— checkout CLICKS grouped by client_reference_id prefix. Read "
                        "this BEFORE reading session_upgrades as a verdict: only the "
                        "shapes named in writes_session_upgrades can produce a row in "
                        "that table, so a 0 there may mean callers were routed to a "
                        "key- or anon-bound link, not that nobody converted. Maps are "
                        "null (never {}) when the probe failed.",
    "signal": "{signal, label, value, prior, unit, direction, better, improving, basis} "
              "— plus comparability and withheld_reason on agents_complete_week.",
    "direction": "up | down | flat | unknown | withheld — the raw movement between the two "
                 "windows. 'unknown' means a window could not be read; it is NEVER "
                 "reported as 'flat'. 'withheld' means both windows were read but "
                 "straddle a registered definition change (weekly_series "
                 "comparability), so the movement is not a trend and is not published "
                 "as one — the two levels still are.",
    "better": "up | down — which direction is GOOD for this signal. Two of the five "
              "improve by going DOWN, so never read 'direction' as a verdict on its own.",
    "improving": "true | false | null — direction == better. null when the movement is "
                 "flat, unknown or withheld.",
    "comparability": "agents_complete_week only: weekly_series.comparability_for_spans "
                     "over the same two complete weeks — {crosses_definition_change, "
                     "changes[], superseded_by_correction, superseded_by[], "
                     "quotable_as_trend, means}. null when it could not be computed, "
                     "which also withholds.",
    "value": "null means the probe FAILED. 0 means it ran and found nothing. Never "
             "conflate them: this feed exists because a flat 0 was being read as a finding.",
    "verdict": "{improving, worsening, flat, unknown, withheld} — counts of signals by "
               "state. A summary of the five, not a business conclusion.",
}


def _verdict(signals) -> dict:
    v = {"improving": 0, "worsening": 0, "flat": 0, "unknown": 0, "withheld": 0}
    for s in signals:
        if s["direction"] == "unknown":
            v["unknown"] += 1
        elif s["direction"] == "withheld":
            # improving is null here; without this branch it fell through to
            # the else and a withheld movement was COUNTED AS WORSENING.
            v["withheld"] += 1
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
        "checkout_binding": feed.get("checkout_binding"),
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
