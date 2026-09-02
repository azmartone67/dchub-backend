"""routes/human_relay.py — the agent→human upgrade relay (digest #1, 2026-07-27 week).

THE NUMBER THIS EXISTS TO MOVE
==============================
2,155 upgrade claims minted in 30d · 2,146 consumed by agents · ZERO opened
by a human · claim_to_paid_rate 0.0%. Every paywall interaction dies inside
the agent's context window; no human decision-maker ever sees a payment
surface. Every prior funnel rec assumed this bridge existed — it doesn't.

WHAT SHIPS HERE
===============
GET /upgrade/h/<token> — a short-lived, HUMAN-readable page an agent can
relay verbatim ("open this link"). It shows what the agent was doing, which
tool hit the boundary, what unlocks, and ONE upgrade button that rides the
EXISTING attribution machinery (/pricing/upgrade?from=mcp&tool=&sid=&direct=1
— the sid-preserve → pack-webhook → claim→paid bridge already built).
Every open is logged to relay_opens — the first-ever measurement of
"a human actually saw the payment surface from the agent channel".
Success metric (from the digest): human_open_rate > 3% in 4 weeks and ≥1
attributed paid conversion carrying a relay token.

TOKEN CONTRACT (shared with the mcp-server's for_your_human builder)
====================================================================
  payload  = base64url("<sid>|<tool>|<tier>|<unix_ts>")
  sig      = hex(HMAC_SHA256(DCHUB_INTERNAL_KEY, payload))[:32]
  token    = payload + "." + sig
Stateless at mint (a paywall envelope costs no DB write); validated and
logged only on OPEN. Age cap 14 days. A bad/expired token still renders a
useful generic upgrade page (never a dead end for a paying-curious human) —
it just logs with valid=false.

Read-only except relay_opens (its own append-only table, created on first
write). Kill: DCHUB_HUMAN_RELAY_DISABLE=1 (page keeps rendering; logging
stops — never brick a human-facing payment page on a telemetry flag).
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import logging
import os
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

human_relay_bp = Blueprint("human_relay", __name__)

_MAX_AGE_S = 14 * 86400
_DDL_DONE = [False]

# 2026-09-02: the checkout-integrity master shell (routes/
# checkout_integrity_master_shell.py, lane 3) now reads THIS page live every
# tick to check that its button sells what its copy says. Its costume is an
# exact prefix; an open wearing it is our own audit, not a human, and is not
# written to relay_opens at all — the revenue shell scores that table as
# "real human opens" and a 120-second tick would add ~720 rows a day. Write-
# side, not read-side: the middleware's admin bail-out (test_paywall_funnel_
# excludes_internal) is the precedent — the ordering IS the fix.
_AUDIT_UA_PREFIXES = ("dchub-checkout-integrity/",)


def _secret() -> bytes:
    return (os.environ.get("DCHUB_INTERNAL_KEY") or "").encode()


def make_relay_token(sid: str, tool: str, tier: str,
                     ts: int | None = None) -> str:
    """Mint a token (used by tests + any backend emitter; the mcp-server
    mints its own with the identical contract)."""
    raw = "%s|%s|%s|%d" % (sid or "", tool or "", tier or "",
                           int(ts if ts is not None else time.time()))
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = _hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig


def parse_relay_token(token: str) -> dict | None:
    """→ {sid, tool, tier, ts} iff signature and age check out, else None.
    Never raises."""
    try:
        payload, sig = (token or "").rsplit(".", 1)
        want = _hmac.new(_secret(), payload.encode(),
                         hashlib.sha256).hexdigest()[:32]
        if not _hmac.compare_digest(sig, want):
            return None
        pad = payload + "=" * (-len(payload) % 4)
        sid, tool, tier, ts = base64.urlsafe_b64decode(pad).decode().split("|", 3)
        ts = int(ts)
        if time.time() - ts > _MAX_AGE_S:
            return None
        return {"sid": sid, "tool": tool, "tier": tier, "ts": ts}
    except Exception:  # noqa: BLE001
        return None


def _log_open(info: dict | None, token: str, valid: bool) -> None:
    """Append-only; never raises; never blocks the render."""
    if (os.environ.get("DCHUB_HUMAN_RELAY_DISABLE") or "").strip() == "1":
        return
    if (request.headers.get("User-Agent") or "").startswith(_AUDIT_UA_PREFIXES):
        return
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=4)
        try:
            with conn.cursor() as cur:
                if not _DDL_DONE[0]:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS relay_opens ("
                        " id BIGSERIAL PRIMARY KEY,"
                        " ts TIMESTAMPTZ DEFAULT NOW(),"
                        " session_id TEXT,"
                        " tool TEXT,"
                        " tier TEXT,"
                        " token_ts TIMESTAMPTZ,"
                        " valid BOOLEAN,"
                        " user_agent TEXT,"
                        " referer TEXT)")
                    _DDL_DONE[0] = True
                cur.execute(
                    "INSERT INTO relay_opens (session_id, tool, tier,"
                    " token_ts, valid, user_agent, referer)"
                    " VALUES (%s,%s,%s, to_timestamp(%s), %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    ((info or {}).get("sid"), (info or {}).get("tool"),
                     (info or {}).get("tier"),
                     (info or {}).get("ts") or 0, valid,
                     (request.headers.get("User-Agent") or "")[:300],
                     (request.headers.get("Referer") or "")[:300]))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        logger.debug("relay open log swallowed: %s", e)


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@human_relay_bp.route("/upgrade/h/<token>", methods=["GET"])
def relay_page(token):
    info = parse_relay_token(token)
    _log_open(info, token, valid=info is not None)
    tool = (info or {}).get("tool") or ""
    sid = (info or {}).get("sid") or ""
    # ONE button, riding the existing attribution chain (sid-preserve →
    # pack webhook claim→paid bridge). direct=1 skips the tier wall.
    #
    # ★ SELL THE PACK THIS PAGE ADVERTISES. `resolve_tier`'s `tier` param means
    # "which plan to sell", NOT "what the visitor currently has". We used to
    # pass the visitor's own tier from the token — but `free`/`identified` are
    # not STRIPE_LINKS keys, so it fell through to the `developer` DEFAULT
    # ($49/mo), or to `pro` ($299/mo) when the token carried a pro-gated tool,
    # all under a "$10 one-time" label. `metered` IS a key, so it wins on the
    # first branch. (Not `pack5`: same Stripe URL, but the webhook still reads
    # pack5 as the legacy $5 SKU by amount.)
    from urllib.parse import urlencode
    q = {"from": "mcp_relay", "tier": "metered", "direct": "1"}
    if tool:
        q["tool"] = tool
    if sid:
        q["sid"] = sid
    upgrade = "https://api.dchub.cloud/pricing/upgrade?" + urlencode(q)
    playground = ("https://dchub.cloud/playground?ref=relay"
                  + ("-" + tool if tool else ""))
    tool_line = (
        "Your AI assistant called <b>%s</b> on DC Hub — live data-center, "
        "grid, fiber and M&amp;A intelligence — and hit the paid data "
        "boundary." % _esc(tool)
        if tool else
        "Your AI assistant was using DC Hub — live data-center, grid, fiber "
        "and M&amp;A intelligence — and hit the paid data boundary.")
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='robots' content='noindex'>"
            "<title>Unlock DC Hub for your AI agent</title>"
            "<style>body{font-family:system-ui;max-width:560px;margin:48px auto;"
            "padding:0 20px;line-height:1.55;color:#111}"
            ".btn{display:block;text-align:center;background:#3478f6;color:#fff;"
            "padding:14px 18px;border-radius:10px;text-decoration:none;"
            "font-weight:600;font-size:17px;margin:22px 0 10px}"
            ".alt{color:#666;font-size:14px}h1{font-size:26px}"
            "small{color:#888}</style></head><body>"
            "<h1>Your AI agent found data worth unlocking</h1>"
            "<p>%s</p>"
            "<p>The <b>$10 one-time pack</b> (1,000 API calls, no subscription) "
            "unlocks the full dataset — your agent's very next call returns "
            "complete data, no reconnect needed.</p>"
            "<a class='btn' href='%s'>Unlock full data — $10 one-time</a>"
            "<p class='alt'>Prefer to look first? <a href='%s'>Explore the live "
            "data free in your browser</a> — no signup.</p>"
            "<p><small>DC Hub · dchub.cloud · data licensed CC-BY-4.0 · this "
            "link was generated for your agent's session%s</small></p>"
            "</body></html>"
            % (tool_line, _esc(upgrade), _esc(playground),
               "" if info else " (link expired — the button still works)"))
    from flask import make_response
    resp = make_response(html, 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@human_relay_bp.route("/api/v1/admin/relay/stats", methods=["GET"])
def relay_stats():
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    if not sent or sent != expected:
        return jsonify(ok=False, error="admin key required"), 401
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    out = {"ok": True, "opens_30d": None, "by_tool": [], "by_day": []}
    if url:
        try:
            import psycopg2
            conn = psycopg2.connect(url, connect_timeout=4)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM relay_opens"
                                " WHERE ts > now() - interval '30 days'")
                    out["opens_30d"] = int(cur.fetchone()[0])
                    cur.execute(
                        "SELECT COALESCE(tool,'-'), count(*) FROM relay_opens"
                        " WHERE ts > now() - interval '30 days'"
                        " GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
                    out["by_tool"] = [{"tool": r[0], "opens": int(r[1])}
                                      for r in cur.fetchall()]
                    cur.execute(
                        "SELECT ts::date, count(*) FROM relay_opens"
                        " WHERE ts > now() - interval '14 days'"
                        " GROUP BY 1 ORDER BY 1")
                    out["by_day"] = [{"day": str(r[0]), "opens": int(r[1])}
                                     for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:100]
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp
