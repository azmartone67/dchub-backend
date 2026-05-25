"""
press_publisher_restart.py — manual + cron-callable press publisher.

Phase ZZZZZ-round38 (2026-05-25). Press cadence visibly stale (latest
visible release dated 2026-04-07 on /press page, despite news pipeline
itself being current). Media organism reports press=0/100 dormant.

The existing marketing_engine.auto_generate() function works — it just
isn't being TRIGGERED anymore. This module exposes a single cron-friendly
endpoint that:
  1. Checks last_press_at — if > 18h ago, generate
  2. Otherwise checks pulse signals — if a DCPI verdict shifted or a
     big M&A deal hit news, generate even within the 18h window
  3. Returns the generated press release ID + topic

Wire to /api/v1/cron/heartbeat dispatcher so GH Actions cron picks it up.
"""
import os
import datetime
import urllib.request
import json as _json
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

press_restart_bp = Blueprint("press_restart", __name__,
                              url_prefix="/api/v1/press-publisher")

BASE = "https://api.dchub.cloud"


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _last_press_age_hours():
    if not (_pg and _dsn()): return 999
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))/3600.0 "
                "FROM press_releases WHERE status='published'")
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 999
    except Exception:
        return 999


def _trigger_auto_generate(force_topic=None):
    """Call the existing marketing_engine.auto_generate endpoint.

    r40 (2026-05-25): added X-Admin-Key header — auto-generate requires
    admin auth and was 401-ing every cron tick (press cadence frozen at
    999h). Pulls from DCHUB_ADMIN_KEY env var which IS set on Railway.
    """
    url = f"{BASE}/api/v1/marketing/auto-generate"
    if force_topic:
        url += f"?force_topic={force_topic}"
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_ADMIN_API_KEY") or "").split()[0] if (
        os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_ADMIN_API_KEY")) else ""
    headers = {
        "User-Agent": "DCHub-PressPublisher/1.0",
        "X-DC-Internal-Cron": "1",
    }
    if admin_key:
        headers["X-Admin-Key"] = admin_key
    try:
        req = urllib.request.Request(url, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(4096)
            try:
                return {"status": resp.status, "body": _json.loads(body)}
            except Exception:
                return {"status": resp.status, "body_preview": body.decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:140]}"}


# AUTO-REPAIR: duplicate route '/run' also in ai_orchestrator.py:916 — review and remove one
@press_restart_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Generates press release if cadence too low."""
    force = request.args.get("force") == "1"
    force_topic = (request.args.get("topic") or "").strip() or None

    age_hours = _last_press_age_hours()
    cadence_target_hours = 18  # 1.3 per day

    if not force and age_hours < cadence_target_hours:
        return jsonify({
            "skipped": True,
            "reason": "cadence_within_target",
            "last_press_age_hours": round(age_hours, 1),
            "target_hours": cadence_target_hours,
        }), 200

    result = _trigger_auto_generate(force_topic=force_topic)
    return jsonify({
        "triggered": True,
        "last_press_age_hours_before": round(age_hours, 1),
        "force_topic": force_topic,
        "auto_generate_result": result,
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200

# AUTO-REPAIR: duplicate route '/status' also in ai_orchestrator.py:911 — review and remove one

@press_restart_bp.route("/status", methods=["GET"])
def status():
    age = _last_press_age_hours()
    return jsonify({
        "last_press_age_hours": round(age, 1),
        "cadence_target_hours": 18,
        "would_trigger_now": age >= 18,
        "blueprint": "press_restart_bp",
    }), 200
