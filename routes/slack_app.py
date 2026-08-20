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
    """Hit our own internal API and return parsed JSON.

    Sends X-Internal-Key (#2018/#2025 loopback pattern): the metered/teaser
    gates run at before_request and trust loopback remote_addr only fragilely
    (a dual-stack listener reports '::ffff:127.0.0.1'), and /api/site-score
    requires the key outright. The 'dchub-' UA prefix triggers the in-route
    internal bypass on /api/v1/grid/intelligence and buckets these calls as
    self-traffic in analytics. No key in env → header omitted, anon behavior.
    """
    base = "http://localhost:8080"  # internal
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "dchub-slack/1.0"}
    ikey = (os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("DCHUB_SYNC_KEY") or "").strip()
    if ikey:
        headers["X-Internal-Key"] = ikey
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _slack_esc(s) -> str:
    """Escape Slack mrkdwn control chars in interpolated data (&, <, >)."""
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        if not args:
            return _ephemeral("Usage: `/dchub search <city or operator>` — e.g. `/dchub search ashburn`")
        # /api/v1/facilities (anon → free tier): per-facility rows with
        # name/provider/city/profile_url; q= matches city AND operator.
        # NOT /facilities/by-market — that returns market AGGREGATES
        # ({market,count,total_mw}, no id/name), which this renderer
        # once mis-read as facilities: every line was "facility/None|?".
        # Free tier omits power_mw, so no MW column here.
        d = _call_dchub("/api/v1/facilities", {"q": args, "limit": 5})
        items = d.get("data") or []
        if not items:
            if "error" in d:
                return _ephemeral("Facility search is temporarily unavailable — try again in a minute.")
            return _ephemeral(f"No facilities found matching `{args}`. Try a city name or operator.")
        total = d.get("total_matching") or len(items)
        lines = [f"*Top facilities matching `{_slack_esc(args)}`* ({total} tracked):"]
        for f in items[:5]:
            name = _slack_esc(f.get("name") or "Unnamed facility")
            provider = _slack_esc(f.get("provider") or "operator n/a")
            loc = _slack_esc(f.get("location_display") or ", ".join(
                str(x) for x in (f.get("city"), f.get("state") or f.get("country")) if x))
            url = f.get("profile_url")
            link = f"<{url}|{name}>" if url else name
            lines.append(f"• {link} — {provider}" + (f" · {loc}" if loc else ""))
        if total > len(items):
            lines.append(f"_Showing {len(items)} of {total} — "
                          f"<https://dchub.cloud/facilities/directory|browse the full directory>_")
        return _in_channel("\n".join(lines))

    if cmd == "grid":
        iso = args.lower().strip() or "pjm"
        # /api/v1/grid/intelligence/<region> — the surface the /grid/<iso>
        # pages render (see grid_public_routes._fetch_live, the #2025-hardened
        # consumer this mirrors). NOT /api/v1/iso/<iso>/snapshot: that payload
        # has no metrics/demand_mw at any tier (ungated = heartbeat/dcpi/
        # pipeline; teaser-gated `metrics` is a CTA string), so this handler
        # rendered "— MW / 0.0%" at best and crashed on the string at worst.
        d = _call_dchub(f"/api/v1/grid/intelligence/{iso.upper()}")
        inner = d.get("data") if isinstance(d.get("data"), dict) else d
        # demand_mw can arrive as a STRING ('88907') in some EIA shapes.
        try:
            demand = int(float(str(inner.get("demand_mw")
                                    or inner.get("current_demand_mw")).replace(",", "")))
        except (TypeError, ValueError):
            demand = None
        mix = inner.get("generation_mix")
        mix = mix if isinstance(mix, dict) else {}
        if demand is None and not mix:
            return _ephemeral(f"Couldn't get grid data for `{_slack_esc(iso)}`. "
                               "Try: pjm, caiso, ercot, miso, nyiso, spp, iso-ne.")
        lines = [f"*{_slack_esc(iso.upper())} Grid Status*"]
        if demand is not None:
            lines.append(f"• Demand: {demand:,} MW")
        if mix:
            def _mw(v):
                try:
                    return float(v.get("mw") if isinstance(v, dict) else v) or 0.0
                except (TypeError, ValueError):
                    return 0.0
            total = sum(_mw(v) for v in mix.values())
            if total > 0:
                fuels = {"NG": "gas", "NUC": "nuclear", "WND": "wind", "SUN": "solar",
                         "WAT": "hydro", "COL": "coal", "OIL": "oil", "GEO": "geothermal"}
                top = sorted(mix.items(), key=lambda kv: _mw(kv[1]), reverse=True)[:3]
                lines.append("• Mix: " + " · ".join(
                    f"{fuels.get(k, _slack_esc(k).lower())} {round(_mw(v) / total * 100)}%"
                    for k, v in top))
        lines.append(f"<https://dchub.cloud/grid/{iso}|Full grid dashboard →>")
        return _in_channel("\n".join(lines))

    if cmd == "site":
        try:
            lat, lon = args.split(",")
            lat = float(lat.strip()); lon = float(lon.strip())
        except (ValueError, AttributeError):
            return _ephemeral("Usage: `/dchub site <lat>,<lon>` — e.g. `/dchub site 39.0,-77.5`")
        d = _call_dchub("/api/site-score", {"lat": lat, "lon": lon})
        # The payload's field is overall_score (there is no score/
        # composite_score key); legacy names kept as fallbacks only.
        score = d.get("overall_score") or d.get("score") or d.get("composite_score")
        if score is None:
            # /api/site-score is auth-required (X-Internal-Key first); an
            # error envelope here is an upstream/auth failure, not a bad
            # location — say so instead of blaming the coordinates.
            return _ephemeral("Site scoring is temporarily unavailable — try again in a minute.")
        verdict = d.get("interpretation")
        return _in_channel(
            f"*Site Score for ({lat}, {lon})*\n"
            f"Composite: *{score}/100*" + (f" — {_slack_esc(verdict)}" if verdict else "") + "\n"
            f"<https://dchub.cloud/map?lat={lat}&lon={lon}|View on map →>"
        )

    if cmd == "deal":
        # get_deals has NO operator= param (only buyer=/seller=, ANDed), so
        # the old {"operator": args} was silently ignored and the newest
        # GLOBAL deals rendered under "involving <operator>". Fetch the
        # newest slice once and match either side here.
        d = _call_dchub("/api/v1/deals", {"limit": 100})
        deals = d.get("data") or d.get("transactions") or []
        if not deals:
            if "error" in d:
                return _ephemeral("Deal data is temporarily unavailable — try again in a minute.")
            return _ephemeral(f"No recent deals found for `{_slack_esc(args)}`.")
        if args:
            needle = args.lower()
            deals = [x for x in deals
                     if needle in (x.get("buyer") or "").lower()
                     or needle in (x.get("seller") or "").lower()]
            if not deals:
                return _ephemeral(f"No recent deals found for `{_slack_esc(args)}` "
                                   "(searched buyer + seller in the newest 100).")
            header = f"*Recent M&A involving `{_slack_esc(args)}`:*"
        else:
            header = "*Recent data center M&A:*"
        lines = [header]
        for x in deals[:5]:
            # value = USD MILLIONS (masked to None below Pro; the internal
            # key is privileged). Prefer the server-formatted display string.
            val = x.get("value_display")
            if not val:
                v = x.get("value")
                val = f"${v:,.0f}M" if isinstance(v, (int, float)) else "undisclosed"
            when = x.get("date") or x.get("year") or ""
            lines.append(f"• {_slack_esc(x.get('buyer') or '?')} → "
                          f"{_slack_esc(x.get('seller') or '?')} · {_slack_esc(val)}"
                          + (f" · {_slack_esc(when)}" if when else ""))
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


# AUTO-REPAIR: duplicate route '/health' also in main.py:7610 — review and remove one
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
