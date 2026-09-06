"""
routes/growth_ops_digest.py — Growth-Ops Digest (2026-07-07).

ONE scheduled operator email that consolidates the health of the back-of-funnel
+ flywheel master shells so validating the 07-06/07 funnel fix-wave is PASSIVE —
no manual dashboard-checking. It reads both master shells IN-PROCESS (their
_run_tick is pure-DB via a short-lived connection — NO self-request; mirrors the
pool-saturation lesson) plus a couple of headline queries, and emails a concise
wins / watch / red-lane report to the OPERATOR inbox.

Recipient: BRAIN_DIGEST_EMAIL → DCHUB_BRIEFING_EMAIL → ADMIN_INBOX_EMAIL. Operator
address only — never a customer email, so there is NO consent surface here.

Endpoints:
  GET  /api/v1/admin/growth-digest/preview   dry-run (build, don't send) — JSON+text
  POST /api/v1/admin/growth-digest/send       build + email (?confirm=true)

Scheduled daily via crawler_scheduler (_run_growth_ops_digest, worker-only).
Auth: X-Admin-Key/X-Internal-Key vs DCHUB_ADMIN_KEY/DCHUB_INTERNAL_KEY. Kill:
GROWTH_DIGEST_DISABLE=1.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

growth_ops_digest_bp = Blueprint("growth_ops_digest", __name__)

_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM = "DC Hub Growth <press@dchub.cloud>"


def _disabled() -> bool:
    return (os.environ.get("GROWTH_DIGEST_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key") or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    ok = {(os.environ.get("DCHUB_ADMIN_KEY") or "").strip(),
          (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()}
    ok.discard("")
    return bool(sent) and sent in ok


def _recipient() -> str:
    return (os.environ.get("BRAIN_DIGEST_EMAIL")
            or (os.environ.get("DCHUB_BRIEFING_EMAIL") or "").split(",")[0].strip()
            or os.environ.get("ADMIN_INBOX_EMAIL")
            or "azmartone@gmail.com").strip()


# ── read the master shells IN-PROCESS (pure-DB, no self-request) ──────

def _shell_lanes(mod_name: str) -> dict:
    """Import a master shell and run its tick in-process → {lane_key: {pass, checks:{id:detail}}}.
    Fail-soft: returns {} on any import/tick error."""
    try:
        import importlib
        mod = importlib.import_module(mod_name)
        payload = mod._run_tick()  # pure-DB, short-lived conn
        out = {}
        for lane in (payload.get("lanes") or []):
            out[lane.get("lane")] = {
                "pass": lane.get("pass"),
                "label": lane.get("label"),
                "checks": {ch.get("id"): {"pass": ch.get("pass"), "detail": ch.get("detail")}
                           for ch in (lane.get("checks") or [])},
            }
        out["_lanes_pass"] = payload.get("lanes_pass")
        out["_lanes_total"] = payload.get("lanes_total")
        return out
    except Exception as e:
        logger.warning("[growth_digest] shell %s failed: %s", mod_name, e)
        return {}


def _north_star() -> dict:
    """A couple of headline numbers not on the shells: distinct real agents this
    full week + prior week (the growth signal), and real conversions 30d."""
    out = {"agents_wk": None, "agents_prev_wk": None,
           "agents_prev_wtd": None, "conv_30d": None}
    try:
        import psycopg2
        url = os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return out
        c = psycopg2.connect(url, connect_timeout=8); c.autocommit = True
        cur = c.cursor()

        def _scalar(sql):
            try:
                cur.execute(sql); r = cur.fetchone()
                return r[0] if r else None
            except Exception:
                try: c.rollback()
                except Exception: pass
                return None
        out["agents_wk"] = _scalar(
            "SELECT count(DISTINCT agent_id) FROM mcp_calls_identity "
            "WHERE is_real_external AND is_public_ip "
            "AND created_at >= date_trunc('week', now())")
        out["agents_prev_wk"] = _scalar(
            "SELECT count(DISTINCT agent_id) FROM mcp_calls_identity "
            "WHERE is_real_external AND is_public_ip "
            "AND created_at >= date_trunc('week', now()) - interval '7 days' "
            "AND created_at < date_trunc('week', now())")
        # ★ agents_wk is WEEK-TO-DATE (>= date_trunc('week', now())), so on
        # Monday it holds a few hours. Differencing it against the FULL prior
        # week printed a double-digit collapse every Monday that recovered by
        # Friday with nothing underneath it -- a decline-and-recovery cycle
        # manufactured by the window, once a week, in the headline of a digest
        # whose subject line carries the number. The comparable quantity is the
        # SAME elapsed slice of the previous week.
        out["agents_prev_wtd"] = _scalar(
            "SELECT count(DISTINCT agent_id) FROM mcp_calls_identity "
            "WHERE is_real_external AND is_public_ip "
            "AND created_at >= date_trunc('week', now()) - interval '7 days' "
            "AND created_at < date_trunc('week', now()) - interval '7 days' "
            "          + (now() - date_trunc('week', now()))")
        out["conv_30d"] = _scalar(
            "SELECT COUNT(*) FROM mcp_conversions "
            "WHERE created_at >= now() - interval '30 days' AND COALESCE(is_test,false)=false")
        try: c.close()
        except Exception: pass
    except Exception as e:
        logger.debug("[growth_digest] north_star failed: %s", e)
    return out


def _activation_signals() -> dict:
    """The five LEADING signals (routes/ops_activation.read_signals).

    2026-08-26: the digest headlined agents + conversions_30d, both LAGGING.
    Conversions has read 0 for the whole window, so the digest could report a
    flat zero every day without ever saying whether anything was turning.
    These are the five that move first. Same computation the public keyless
    feed serves at /api/v1/ops/activation — ONE source, so the brain's report
    and the public feed can never disagree.
    """
    try:
        from routes.ops_activation import read_signals
        return read_signals()
    except Exception as e:
        logger.debug("[growth_digest] activation signals failed: %s", e)
        return {"ok": False, "signals": [], "error": str(e)[:80]}


def _planner_adoption() -> dict:
    """plan_query FRONT-DOOR adoption (2026-07-21). Front door shipped 2026-07-20
    at a 0% baseline; the digest ALERT fires when planner-first sessions cross a
    real threshold (not a single stray call) — the signal to build Phase 2 (the
    event-driven successful-workflow funnel). See reach-vs-tooluse memory + the
    growth-memo planner_adoption block for the full definition."""
    out = {"sessions": None, "planner_first": None, "rate_pct": None,
           "avg_planner": None, "avg_direct": None}
    try:
        import psycopg2
        url = os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return out
        c = psycopg2.connect(url, connect_timeout=8); c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "WITH sess AS ("
            " SELECT session_id, (array_agg(tool_name ORDER BY created_at))[1] AS first_tool, count(*) AS n"
            " FROM mcp_calls_identity WHERE is_public_ip AND is_real_external"
            " AND created_at >= now() - interval '7 days'"
            " AND session_id IS NOT NULL AND btrim(session_id) <> '' GROUP BY session_id) "
            "SELECT count(*), count(*) FILTER (WHERE first_tool='plan_query'),"
            " round((avg(n) FILTER (WHERE first_tool='plan_query'))::numeric,1),"
            " round((avg(n) FILTER (WHERE first_tool<>'plan_query'))::numeric,1) FROM sess")
        r = cur.fetchone()
        if r:
            s, pf = int(r[0] or 0), int(r[1] or 0)
            out.update(sessions=s, planner_first=pf,
                       rate_pct=(round(100.0 * pf / s, 1) if s else None),
                       avg_planner=(float(r[2]) if r[2] is not None else None),
                       avg_direct=(float(r[3]) if r[3] is not None else None))
        try: c.close()
        except Exception: pass
    except Exception as e:
        logger.debug("[growth_digest] planner_adoption failed: %s", e)
    return out


# >= N distinct planner-first sessions in 7d = real adoption, not a stray call —
# the trigger to build Phase 2 (see _planner_adoption / reach-vs-tooluse memory).
_PLANNER_PHASE2_THRESHOLD = 3


# ── build ─────────────────────────────────────────────────────────────

def _chip(v):
    return {True: "🟢", False: "🔴", None: "🟡"}.get(v, "🟡")


def _build_digest() -> dict:
    bf = _shell_lanes("routes.backfunnel_master_shell")
    fw = _shell_lanes("routes.flywheel_master_shell")
    ns = _north_star()

    lines = []  # plain text
    rows = []   # html rows

    def add(txt, html=None):
        lines.append(txt)
        rows.append(html if html is not None else "<div>" + txt.replace("&", "&amp;").replace("<", "&lt;") + "</div>")

    # headline — the growth signal
    aw, apw = ns.get("agents_wk"), ns.get("agents_prev_wk")
    apwtd = ns.get("agents_prev_wtd")
    # Difference like with like: week-to-date against the same elapsed slice of
    # last week. `apw` stays on the line as context, NOT as the comparand.
    delta = ("" if aw is None or apwtd is None else
             f" ({'+' if aw >= apwtd else ''}{aw - apwtd} vs same point last wk)")
    add(f"NORTH STAR · distinct real agents week-to-date: {aw}{delta}"
        f"  (same point last wk {apwtd} · prev full wk {apw})")
    add(f"Real conversions 30d: {ns.get('conv_30d')}")
    add("")

    # LEADING SIGNALS (2026-08-26). The two lines above are lagging and have
    # read flat for the whole window. These five move first; each states which
    # way is GOOD, because two of them improve by going DOWN.
    _act = _activation_signals()
    if _act.get("ok") and _act.get("signals"):
        add("LEADING SIGNALS · the five that move before revenue does")
        # ★ Counted HERE, off the same ladder that picks _mark. The summary
        # used to read _act["verdict"], a key read_signals() has never set
        # (it sets ok/error/signals/session_upgrades_all_time/checkout_binding),
        # so `.get("verdict") or {}` was always {} and the line printed five
        # zeros directly beneath five signals that had just said IMPROVING or
        # WORSENING. One classification, one place -- a second implementation
        # is how the two drifted apart in the first place.
        _counts = {"improving": 0, "worsening": 0, "flat": 0,
                   "unknown": 0, "withheld": 0}
        for _s in _act["signals"]:
            _v = _s.get("value")
            _p = _s.get("prior")
            _vs = "n/a (probe failed)" if _v is None else str(_v)
            _ps = "n/a" if _p is None else str(_p)
            if _s.get("improving") is True:
                _mark, _bucket = "IMPROVING", "improving"
            elif _s.get("improving") is False:
                _mark, _bucket = "WORSENING", "worsening"
            elif _s.get("direction") == "withheld":
                _mark = ("WITHHELD — the two weeks straddle a definition change; "
                         "not a trend (see comparability)")
                _bucket = "withheld"
            elif _s.get("direction") == "flat":
                _mark, _bucket = "flat", "flat"
            else:
                _mark, _bucket = "no read", "unknown"
            _counts[_bucket] += 1
            add(f"  · {_s['label']}: {_vs} (prev {_ps}, better={_s['better']}) — {_mark}")
        add(f"  = {_counts['improving']} improving · {_counts['worsening']} worsening · "
            f"{_counts['flat']} flat · {_counts['unknown']} unread · "
            f"{_counts['withheld']} withheld")
    else:
        # A failed read is NOT zero. Say so rather than printing five zeros.
        add(f"LEADING SIGNALS · unavailable this run ({_act.get('error') or 'unknown'}) — "
            "not zero, unread")
    add("")

    # plan_query FRONT DOOR adoption — the Phase-2 build trigger (2026-07-21).
    # Shows the trajectory every day (baseline 0%); a loud ALERT fires only when
    # planner-first crosses the real-adoption threshold, so Phase 2 gets built at
    # the right moment instead of slipping.
    pa = _planner_adoption()
    pf, ps = pa.get("planner_first"), pa.get("sessions")
    if pf is not None:
        add(f"PLANNER FRONT DOOR · planner-first {pf}/{ps} sessions ({pa.get('rate_pct')}%) · "
            f"avg tools planner-first {pa.get('avg_planner')} vs direct {pa.get('avg_direct')}")
        if pf >= _PLANNER_PHASE2_THRESHOLD:
            add(f"  🚀 ALERT — plan_query adoption crossed {_PLANNER_PHASE2_THRESHOLD}+ sessions/7d. "
                f"BUILD PHASE 2: the event-driven successful-workflow funnel (define 'completed' first, "
                f"'instrument once/derive forever'). Trigger + spec in the reach-vs-tooluse memory.")
        add("")

    # back-of-funnel watch-metrics (the 07-06/07 fix-wave)
    add(f"BACK-OF-FUNNEL ({bf.get('_lanes_pass')}/{bf.get('_lanes_total')} lanes green)")
    _bf_watch = [
        ("reachability", "reach_emails",        "reachability"),
        ("reachability", "reach_optin_audience","opted-in leads"),
        ("attribution",  "attr_claim_to_paid",  "relay claim→paid"),
        ("retention",    "ret_mature_reuse",    "key-reuse (raw)"),
        ("retention",    "ret_claim_carry",     "post-claim key-carry (activation leak)"),
        ("demand",       "dem_relay_converts",  "relay conversions"),
    ]
    for lane, cid, label in _bf_watch:
        ch = (bf.get(lane) or {}).get("checks", {}).get(cid) or {}
        add(f"  {label}: {ch.get('detail','?')}")
    add("")

    # flywheel operational lanes — surface the RED ones
    add(f"FLYWHEEL OPS ({fw.get('_lanes_pass')}/{fw.get('_lanes_total')} lanes green)")
    for key, info in fw.items():
        if key.startswith("_"):
            continue
        p = info.get("pass")
        mark = "OK " if p else ("RED" if p is False else " ? ")
        # for RED lanes, name the failing checks
        detail = ""
        if p is False:
            fails = [c.get("detail") for cid, c in (info.get("checks") or {}).items() if c.get("pass") is False]
            detail = " — " + "; ".join(f for f in fails[:2] if f)
        add(f"  [{mark}] {info.get('label','?')}{detail}")
    add("")

    # the standing decision trigger
    add("WATCH: reachable-email + bind-rate climbing vs adoption north-star.")
    add("  If adoption dips w/o bind gains → revert DCHUB_TRIAL_TOOL_DAILY_FULL → 8 (one env change).")

    text = "\n".join(lines)
    html = ("<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
            "max-width:640px;color:#0f172a;font-size:14px;line-height:1.5'>"
            "<h2 style='margin:0 0 8px'>DC Hub · Growth-Ops Digest</h2>"
            "<div style='color:#64748b;font-size:12px;margin-bottom:12px'>"
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") +
            " · reads /admin/backfunnel + /admin/flywheel</div>"
            + "".join("<div style='margin:2px 0'>" + r + "</div>" for r in
                      [l.replace("&", "&amp;").replace("<", "&lt;") for l in lines])
            + "</div>")
    subject = f"DC Hub Growth-Ops · agents {aw} · conv30d {ns.get('conv_30d')} · " \
              f"bf {bf.get('_lanes_pass')}/{bf.get('_lanes_total')} · fw {fw.get('_lanes_pass')}/{fw.get('_lanes_total')}"
    return {"subject": subject, "text": text, "html": html,
            "recipient": _recipient(), "north_star": ns,
            "bf_lanes": [bf.get("_lanes_pass"), bf.get("_lanes_total")],
            "fw_lanes": [fw.get("_lanes_pass"), fw.get("_lanes_total")]}


def _send_resend(to: str, subject: str, text: str, html: str) -> tuple[bool, str]:
    if not _RESEND_KEY:
        return False, "no_resend_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_KEY}", "Content-Type": "application/json"},
            json={"from": _FROM, "to": [to], "subject": subject, "text": text, "html": html},
            timeout=12)
        if r.status_code in (200, 202):
            return True, (r.json() or {}).get("id", "")
        return False, f"resend_{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


# ── routes ────────────────────────────────────────────────────────────

@growth_ops_digest_bp.route("/api/v1/admin/growth-digest/preview", methods=["GET"])
def growth_digest_preview():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    d = _build_digest()
    return jsonify(ok=True, dry_run=True, subject=d["subject"], recipient=d["recipient"],
                   north_star=d["north_star"], bf_lanes=d["bf_lanes"], fw_lanes=d["fw_lanes"],
                   text=d["text"])


@growth_ops_digest_bp.route("/api/v1/admin/growth-digest/send", methods=["POST"])
def growth_digest_send():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("confirm") or "") != "true":
        return jsonify(ok=False, error="confirm_required",
                       hint="POST ?confirm=true to actually send"), 400
    d = _build_digest()
    ok, rid = _send_resend(d["recipient"], d["subject"], d["text"], d["html"])
    logger.info("[growth_digest] send to=%s ok=%s id=%s", d["recipient"], ok, rid)
    return jsonify(ok=ok, sent_to=d["recipient"] if ok else None,
                   resend_id=rid if ok else None, error=None if ok else rid,
                   subject=d["subject"]), (200 if ok else 502)
