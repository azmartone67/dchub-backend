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
    out = {"agents_wk": None, "agents_prev_wk": None, "conv_30d": None}
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
        out["conv_30d"] = _scalar(
            "SELECT COUNT(*) FROM mcp_conversions "
            "WHERE created_at >= now() - interval '30 days' AND COALESCE(is_test,false)=false")
        try: c.close()
        except Exception: pass
    except Exception as e:
        logger.debug("[growth_digest] north_star failed: %s", e)
    return out


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
    delta = ("" if aw is None or apw is None else
             f" ({'+' if aw>=apw else ''}{aw-apw} WoW)")
    add(f"NORTH STAR · distinct real agents this week: {aw}{delta}  (prev full wk {apw})")
    add(f"Real conversions 30d: {ns.get('conv_30d')}")
    add("")

    # back-of-funnel watch-metrics (the 07-06/07 fix-wave)
    add(f"BACK-OF-FUNNEL ({bf.get('_lanes_pass')}/{bf.get('_lanes_total')} lanes green)")
    _bf_watch = [
        ("reachability", "reach_emails",        "reachability"),
        ("reachability", "reach_optin_audience","opted-in leads"),
        ("attribution",  "attr_claim_to_paid",  "relay claim→paid"),
        ("retention",    "ret_mature_reuse",    "key-reuse"),
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
