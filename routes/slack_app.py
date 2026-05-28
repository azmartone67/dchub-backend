"""
slack_app.py — Slack /dchub slash command + bot events.

Phase ZZZZZ-round34 (2026-05-24). Sticky integration — once a user wires
the DC Hub bot into their channel, the team starts querying from inside
their workflow. Churn drops by ~60% per round-32 research.

ROUTES:
  POST /api/v1/slack/command   — slash command handler (/dchub <subcommand>)
  POST /api/v1/slack/events    — bot events (app_mention, link_shared)
  POST /api/v1/slack/interact  — block_actions / view_submission
  GET  /api/v1/slack/oauth/callback  — OAuth install callback
  GET  /api/v1/slack/health

SUBCOMMANDS (called as /dchub <subcommand> <args>):
  /dchub search <city or operator>   — search facilities
  /dchub grid <iso>                  — get grid status
  /dchub deal <operator>             — recent M&A
  /dchub site <lat,lon>              — site score
  /dchub help                        — list subcommands

Slack requires:
  - SLACK_SIGNING_SECRET env var (for request verification)
  - SLACK_CLIENT_ID + SLACK_CLIENT_SECRET (for OAuth)
  - SLACK_BOT_TOKEN (xoxb-...) for posting back into channels
"""
import os
import hmac
import hashlib
import time
import json
import urllib.request
import urllib.parse
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, request, jsonify, Response, redirect

slack_app_bp = Blueprint("slack_app", __name__, url_prefix="/api/v1/slack")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn(), connect_timeout=8)
    try: yield c
    finally:
        try: c.close()
        except Exception: pass


def _verify_slack_signature(req) -> bool:
    """Verify Slack request signature per https://api.slack.com/authentication/verifying-requests-from-slack"""
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        # Dev mode — allow if no secret set
        return True
    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:  # 5 min replay window
            return False
    except ValueError:
        return False
    body = req.get_data(as_text=True)
    basestring = f"v0:{ts}:{body}".encode()
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _ephemeral(text: str, blocks=None) -> Response:
    """Return a response visible only to the user who invoked the command."""
    payload = {"response_type": "ephemeral", "text": text}
    if blocks:
        payload["blocks"] = blocks
    return jsonify(payload)


def _in_channel(text: str, blocks=None) -> Response:
    payload = {"response_type": "in_channel", "text": text}
    if blocks:
        payload["blocks"] = blocks
    return jsonify(payload)


def _call_dchub(path: str, params: dict = None, timeout: float = 8) -> dict:
    """Hit our own internal API and return parsed JSON."""
    base = "http://localhost:8080"  # internal
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                     "User-Agent": "slack-app/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


# ─── Slash command handler ───────────────────────────────────────────
@slack_app_bp.route("/command", methods=["POST"])
def slack_command():
    if not _verify_slack_signature(request):
        return _ephemeral("⚠️ Signature verification failed."), 401

    text = (request.form.get("text") or "").strip()
    user = request.form.get("user_name", "user")

    if not text or text.lower() in ("help", "?"):
        return _ephemeral(
            "*DC Hub Slack commands:*\n"
            "• `/dchub search <city or operator>` — find facilities\n"
            "• `/dchub grid <iso>` — live grid status (pjm, caiso, ercot, …)\n"
            "• `/dchub site <lat,lon>` — score a location\n"
            "• `/dchub deal <operator>` — recent M&A activity\n"
            "• `/dchub help` — this message\n\n"
            "_Free tier: 10 commands/day. Paid: $49/mo for 1,000/day._"
        )

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "search":
        d = _call_dchub("/api/v1/facilities/by-market", {"market": args, "limit": 5})
        items = d.get("data", d.get("facilities", []))
        if not items:
            return _ephemeral(f"No facilities found matching `{args}`. Try a city name or operator.")
        lines = [f"*Top facilities matching `{args}`:*"]
        for f in items[:5]:
            mw = f.get("power_mw") or "—"
            lines.append(f"• <https://dchub.cloud/facility/{f.get('id')}|{f.get('name','?')}> — {f.get('provider','?')}, {mw} MW")
        return _in_channel("\n".join(lines))

    if cmd == "grid":
        iso = args.lower().strip() or "pjm"
        d = _call_dchub(f"/api/v1/iso/{iso}/snapshot")
        if "error" in d:
            return _ephemeral(f"Couldn't get grid data for `{iso}`. Try: pjm, caiso, ercot, miso, hydroquebec.")
        m = d.get("metrics", {})
        demand = m.get("demand_mw", {}).get("value") if isinstance(m.get("demand_mw"), dict) else m.get("demand_mw", "—")
        renew = m.get("renewable_pct", {}).get("value") if isinstance(m.get("renewable_pct"), dict) else m.get("renewable_pct", 0)
        return _in_channel(
            f"*{iso.upper()} Grid Status*\n"
            f"• Demand: {demand} MW\n"
            f"• Renewable mix: {round(float(renew or 0) * 100, 1)}%\n"
            f"<https://dchub.cloud/grids/{iso}|Full grid dashboard →>"
        )

    if cmd == "site":
        try:
            lat, lon = args.split(",")
            lat = float(lat.strip()); lon = float(lon.strip())
        except (ValueError, AttributeError):
            return _ephemeral("Usage: `/dchub site <lat>,<lon>` — e.g. `/dchub site 39.0,-77.5`")
        d = _call_dchub("/api/site-score", {"lat": lat, "lon": lon})
        if "error" in d:
            return _ephemeral("Couldn't compute site score for that location.")
        score = d.get("score") or d.get("composite_score") or "?"
        return _in_channel(
            f"*Site Score for ({lat}, {lon})*\n"
            f"Composite: *{score}/100*\n"
            f"<https://dchub.cloud/map?lat={lat}&lon={lon}|View on map →>"
        )

    if cmd == "deal":
        d = _call_dchub("/api/v1/deals", {"operator": args, "limit": 5})
        deals = d.get("data", d.get("deals", []))
        if not deals:
            return _ephemeral(f"No recent deals found for `{args}`.")
        lines = [f"*Recent M&A involving `{args}`:*"]
        for x in deals[:5]:
            val = x.get("value_usd") or x.get("value") or "?"
            lines.append(f"• {x.get('buyer','?')} → {x.get('seller','?')} · ${val}M · {x.get('date','?')}")
        return _in_channel("\n".join(lines))

    return _ephemeral(f"Unknown subcommand `{cmd}`. Try `/dchub help`.")


# ─── Bot events (app_mention, link_shared) ───────────────────────────
@slack_app_bp.route("/events", methods=["POST"])
def slack_events():
    if not _verify_slack_signature(request):
        return jsonify({"error": "bad signature"}), 401
    data = request.get_json(silent=True) or {}
    # URL verification challenge
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})
    # Acknowledge other events; handlers can be added later
    return jsonify({"ok": True})


@slack_app_bp.route("/interact", methods=["POST"])
def slack_interact():
    """Block actions (button clicks etc.)"""
    if not _verify_slack_signature(request):
        return jsonify({"error": "bad signature"}), 401
    return jsonify({"ok": True})


# ─── OAuth install callback ───────────────────────────────────────────
@slack_app_bp.route("/oauth/callback", methods=["GET"])
def slack_oauth_callback():
    """Exchange code for bot token after user installs the app."""
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "no code"}), 400

    client_id = os.environ.get("SLACK_CLIENT_ID", "")
    client_secret = os.environ.get("SLACK_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return jsonify({"error": "SLACK_CLIENT_ID/SECRET not configured"}), 500

    try:
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        }).encode()
        req = urllib.request.Request("https://slack.com/api/oauth.v2.access",
                                       data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode())
    except Exception as e:
        return jsonify({"error": f"token exchange failed: {e}"}), 502

    if not result.get("ok"):
        return jsonify({"error": result.get("error", "unknown")}), 400

    # In production: persist team_id + bot_token in a Slack installations table
    # For now, just acknowledge install and redirect to a success page
    team = result.get("team", {}).get("name", "your workspace")
    return Response(
        f"<html><body style='font-family:sans-serif;max-width:600px;margin:60px auto;padding:0 20px;'>"
        f"<h1>✓ DC Hub installed in {team}!</h1>"
        f"<p>Try <code>/dchub help</code> in any channel to get started.</p>"
        f"<p><a href='https://dchub.cloud'>dchub.cloud</a></p></body></html>",
        mimetype="text/html"
    )


# AUTO-REPAIR: duplicate route '/health' also in main.py:3708 — review and remove one
@slack_app_bp.route("/health", methods=["GET"])
def slack_health():
    return jsonify({
        "ok": True,
        "blueprint": "slack_app_bp",
        "version": "round-34-v1",
        "configured": {
            "signing_secret": bool(os.environ.get("SLACK_SIGNING_SECRET")),
            "client_id":     bool(os.environ.get("SLACK_CLIENT_ID")),
            "client_secret": bool(os.environ.get("SLACK_CLIENT_SECRET")),
            "bot_token":     bool(os.environ.get("SLACK_BOT_TOKEN")),
        },
        "endpoints": [
            "POST /api/v1/slack/command",
            "POST /api/v1/slack/events",
            "POST /api/v1/slack/interact",
            "GET  /api/v1/slack/oauth/callback",
        ],
    }), 200
