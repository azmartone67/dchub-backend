"""routes/render_build_monitor.py — 2026-06-11.

The operator hit a Render BUILD failure (httpcore pip timeout) with NO alert,
because the brain monitors the LIVE SITE (Railway), not Render's BUILD status —
and Render is the failover host, so nothing went down. This adds a lightweight
poll of the Render deploy API + a Resend email alert on a failed build (deduped
per deploy_id) so a broken Render build pings the operator instead of going
silent.

Endpoint:
  GET/POST /api/v1/admin/render/build-check   — admin-gated; cron-callable.

Env:
  RENDER_API_KEY        — Render API key (required; no-op without it).
  RENDER_SERVICE_ID     — defaults to the dchub-backend-render service id.
  DCHUB_ALERT_EMAIL     — recipient (default azmartone@gmail.com).
  DCHUB_RESEND_API_KEY / RESEND_API_KEY — sender key.
"""
import os
import json
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

render_build_monitor_bp = Blueprint("render_build_monitor", __name__)

_FAIL_STATUSES = {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}
_DEFAULT_SID = "srv-d86g7g6gvqtc73dlpojg"  # dchub-backend-render


def _admin_ok():
    k = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    prov = request.headers.get("X-Admin-Key") or request.args.get("admin_key") or ""
    return (not k) or prov == k


def _conn():
    try:
        import psycopg2
        du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
        return psycopg2.connect(du, connect_timeout=8) if du else None
    except Exception:
        return None


def _already_alerted(deploy_id):
    """True if this deploy_id was already alerted (dedup). Records it when new.
    Fail-OPEN (returns False on DB error) so a DB hiccup never suppresses a
    real alert."""
    c = _conn()
    if not c:
        return False
    try:
        with c, c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS render_build_alerts ("
                        "deploy_id TEXT PRIMARY KEY, alerted_at TIMESTAMPTZ DEFAULT NOW())")
            cur.execute("INSERT INTO render_build_alerts (deploy_id) VALUES (%s) ON CONFLICT DO NOTHING "
                        "ON CONFLICT (deploy_id) DO NOTHING", (deploy_id,))
            return cur.rowcount == 0  # 0 rows inserted => conflict => already alerted
    except Exception:
        note_swallowed_write("render_build_alerts", where="render_build_monitor._already_alerted")
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def _send_alert(subject, html):
    rk = (os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("DCHUB_ALERT_EMAIL") or "azmartone@gmail.com").strip()
    frm = os.environ.get("DCHUB_ALERT_FROM") or "DC Hub Alerts <alerts@dchub.cloud>"
    if not rk:
        return False, "no_resend_key"
    try:
        body = json.dumps({"from": frm, "to": [to], "subject": subject, "html": html}).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=body, method="POST",
            headers={"Authorization": "Bearer " + rk, "Content-Type": "application/json",
                     "User-Agent": "dchub-render-monitor/1.0"})  # UA: dodge CF 1010
        with urllib.request.urlopen(req, timeout=12) as r:
            return True, "sent_%d" % r.status
    except urllib.error.HTTPError as e:
        return False, "http_%d" % e.code
    except Exception as e:
        return False, "err:%s" % str(e)[:60]


def check_render_build():
    """Poll the latest Render deploy; alert (once) if it's a failed build.
    Returns a JSON-able dict. Never raises."""
    rk = (os.environ.get("RENDER_API_KEY") or "").strip()
    if not rk:
        return {"ok": False, "error": "no_render_api_key"}
    sid = (os.environ.get("RENDER_SERVICE_ID") or _DEFAULT_SID).strip()
    try:
        req = urllib.request.Request(
            "https://api.render.com/v1/services/%s/deploys?limit=3" % sid,
            headers={"Authorization": "Bearer " + rk, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            deploys = json.load(r)
    except Exception as e:
        return {"ok": False, "error": "render_api: %s" % str(e)[:80]}
    if not deploys:
        return {"ok": True, "status": "no_deploys"}
    latest = deploys[0].get("deploy") or deploys[0]
    status = latest.get("status")
    did = latest.get("id")
    if status not in _FAIL_STATUSES:
        return {"ok": True, "status": status, "alerted": False}
    if _already_alerted(did):
        return {"ok": True, "status": status, "deploy_id": did, "alerted": "already"}
    commit = ((latest.get("commit") or {}).get("message") or "")[:140]
    html = ("<h2>\U0001F6A8 Render build FAILED</h2>"
            "<p><b>Service:</b> %s<br><b>Status:</b> %s<br><b>Deploy:</b> %s<br>"
            "<b>Commit:</b> %s</p>"
            "<p>Render is the <b>failover</b> host — the live site (Railway) is "
            "unaffected, but the Render mirror won't update until this build is "
            "fixed.</p>"
            "<p>Dashboard: https://dashboard.render.com/web/%s</p>"
            % (sid, status, did, commit, sid))
    ok, info = _send_alert("\U0001F6A8 DC Hub: Render build failed (%s)" % status, html)
    return {"ok": True, "status": status, "deploy_id": did, "alerted": ok, "send": info}


@render_build_monitor_bp.route("/api/v1/admin/render/build-check", methods=["GET", "POST"])
def render_build_check_endpoint():
    if not _admin_ok():
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    return jsonify(check_render_build())
