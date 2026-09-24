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
— the sid-preserve → pack-webhook → claim→paid bridge already built). For a
keyed caller the button is the signed /go/c/ link bound to its key instead
(2026-09-13, see relay_page).
Every open is logged to relay_opens — the first-ever measurement of
"a human actually saw the payment surface from the agent channel".
Success metric (from the digest): human_open_rate > 3% in 4 weeks and ≥1
attributed paid conversion carrying a relay token.

TOKEN CONTRACT (shared with the mcp-server's for_your_human builder)
====================================================================
  payload  = base64url("<sid>|<tool>|<tier>|<unix_ts>")
         or  base64url("<sid>|<tool>|<tier>|<unix_ts>|pk-<sha256 hex>")
  sig      = hex(HMAC_SHA256(DCHUB_INTERNAL_KEY, payload))[:32]
  token    = payload + "." + sig
Stateless at mint (a paywall envelope costs no DB write); validated and
logged only on OPEN. Age cap 14 days. A bad/expired token still renders a
useful generic upgrade page (never a dead end for a paying-curious human) —
it just logs with valid=false. The optional fifth field (2026-09-13) is a
keyed caller's durable-key reference; a fifth field of any other shape is
ignored and the open stays valid.

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
import re
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

human_relay_bp = Blueprint("human_relay", __name__)

_MAX_AGE_S = 14 * 86400
_DDL_DONE = [False]

# The only fifth token field we mint: `pk-` + the sha256 hex of the caller's
# API key, the durable-key pack ref routes/checkout_click_tracker passes to
# Stripe as client_reference_id.
_KREF_OK = re.compile(r"pk-[0-9a-f]{64}")

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
                     ts: int | None = None, kref: str | None = None) -> str:
    """Mint a token (used by tests + any backend emitter; the mcp-server
    mints its own with the identical contract).

    `kref` is appended as the fifth field exactly as given: parse_relay_token
    is the gate, so a test can mint a malformed one and watch it be ignored."""
    raw = "%s|%s|%s|%d" % (sid or "", tool or "", tier or "",
                           int(ts if ts is not None else time.time()))
    if kref:
        raw += "|" + kref
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = _hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig


def parse_relay_token(token: str) -> dict | None:
    """→ {sid, tool, tier, ts, kref} iff signature and age check out, else None.
    Never raises.

    Four fields is the original contract. A fifth is kept as `kref` only when
    it is exactly `pk-<64 hex>`; any other fifth field reads as kref "" and the
    open stays valid — the signature already proved we minted the link, so a
    bad key reference costs the key binding, not the measurement. Any other
    field count is None."""
    try:
        payload, sig = (token or "").rsplit(".", 1)
        want = _hmac.new(_secret(), payload.encode(),
                         hashlib.sha256).hexdigest()[:32]
        if not _hmac.compare_digest(sig, want):
            return None
        pad = payload + "=" * (-len(payload) % 4)
        fields = base64.urlsafe_b64decode(pad).decode().split("|")
        if len(fields) not in (4, 5):
            return None
        sid, tool, tier, ts = fields[:4]
        kref = fields[4] if len(fields) == 5 else ""
        if not _KREF_OK.fullmatch(kref):
            kref = ""
        ts = int(ts)
        if time.time() - ts > _MAX_AGE_S:
            return None
        return {"sid": sid, "tool": tool, "tier": tier, "ts": ts, "kref": kref}
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
                        " referer TEXT,"
                        " token_hash TEXT)")
                    # CREATE TABLE IF NOT EXISTS is a no-op on the live table,
                    # so the new column needs its own ALTER or it exists only
                    # for a fresh database.
                    cur.execute("ALTER TABLE relay_opens"
                                " ADD COLUMN IF NOT EXISTS token_hash TEXT")
                    _DDL_DONE[0] = True
                cur.execute(
                    "INSERT INTO relay_opens (session_id, tool, tier,"
                    " token_ts, valid, user_agent, referer, token_hash)"
                    " VALUES (%s,%s,%s, to_timestamp(%s), %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    ((info or {}).get("sid"), (info or {}).get("tool"),
                     (info or {}).get("tier"),
                     (info or {}).get("ts") or 0, valid,
                     (request.headers.get("User-Agent") or "")[:300],
                     # ★★★ `referer` IS NOT PROVENANCE — DO NOT FILTER ON IT.
                     # The Cloudflare worker injects `Referer: https://dchub.cloud`
                     # on every proxied request (see the same warning on
                     # main.py's _fiber_full_access: trusting it is what leaked
                     # the 645KB dataset to anonymous curl). 148 of the 150
                     # relay_opens rows in the 30d to 2026-09-03 carry that
                     # exact value, and the other 2 are empty — so the column
                     # looks like "every open came from our own site" and means
                     # nothing of the kind. PROVEN 2026-09-03: a curl to
                     # /upgrade/h/<bogus> sending NO Referer header landed row
                     # id=152 with referer='https://dchub.cloud'. A self-traffic
                     # rule keyed on this would have excluded every real human
                     # open the moment one arrived. The column is kept because
                     # it is free and a future edge change could make it real;
                     # tests/test_relay_open_provenance.py fails if any read
                     # path starts treating it as a signal.
                     (request.headers.get("Referer") or "")[:300],
                     # ★ THE IDENTITY AN OPEN HAS WHEN IT HAS NO SESSION.
                     # Measured 2026-09-07 over 30d: of 178 relay opens, 32
                     # passed the real-UA filter and 30 of those 32 carried NO
                     # session_id — so human_acted could count 2. The sid is
                     # baked into the token at MINT time
                     # (`${sessionId || ''}|tool|tier|ts` in buildHumanRelay);
                     # when the minting path had no session, the link is born
                     # without one and nothing at open time can recover it.
                     #
                     # But every link carries a token that is UNIQUE PER MINT —
                     # the HMAC covers sid|tool|tier|ts. Hashing it gives each
                     # open a stable identity even with an empty sid, so the
                     # funnel can count DISTINCT LINKS OPENED instead of
                     # discarding the row. Stored as a hash, not the token: the
                     # token is a working credential for /upgrade/h/<token> and
                     # this table has no business holding one.
                     hashlib.sha256((token or "").encode()).hexdigest()[:32]))
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


# ── the IDENTIFY rung (r-identify-rung, 2026-09-17) ───────────────────────
# Measured the day this shipped, 30d: human_acted 7, identified 0,
# paid_attributed 0. `identified` counts mcp_high_intent_sessions.claim_email
# and nothing on the human's path had ever written it — the human opened the
# link, decided, and left, and the only thing we ever learned was that a click
# happened. See routes/relay_identify for the full note.
#
# ★ THE EMAIL IS OPTIONAL AND THE BUTTON STAYS. A gate in front of a payment
# surface trades a measurable rung for unmeasured lost revenue, on a page whose
# entire job is that 7 humans a month reach it. The form submits to checkout;
# the button below it skips the form and goes to the same place.
#
# ★ THE SESSION COMES FROM THE SIGNED TOKEN, NEVER FROM THE FORM. A posted sid
# would let anyone stamp any session's email — the token's HMAC is the only
# thing proving the poster holds a link we minted.


def _upgrade_target(info: dict | None):
    """(url, keyed) — the ONE checkout this page sells.

    Extracted 2026-09-17 so the button and the email form cannot send the same
    human to two different checkouts: the form redirects here after capturing,
    and a second copy of this logic would be a second price.

    ONE button, riding the existing attribution chain (sid-preserve → pack
    webhook claim→paid bridge). direct=1 skips the tier wall.

    ★ SELL THE PACK THIS PAGE ADVERTISES. `resolve_tier`'s `tier` param means
    "which plan to sell", NOT "what the visitor currently has". We used to
    pass the visitor's own tier from the token — but `free`/`identified` are
    not STRIPE_LINKS keys, so it fell through to the `developer` DEFAULT
    ($49/mo), or to `pro` ($299/mo) when the token carried a pro-gated tool,
    all under a "$10 one-time" label. `metered` IS a key, so it wins on the
    first branch. (Not `pack5`: same Stripe URL, but the webhook still reads
    pack5 as the legacy $5 SKU by amount.)

    ★ 2026-09-13 — A KEYED CALLER'S PACK IS BOUND TO ITS KEY. /pricing/upgrade
    binds the purchase to the session alone. When the token carries the
    caller's `pk-` key reference, the button is the signed /go/c/ link
    instead: ref = that key, the client_reference_id the durable-key pack
    binds, with the session beside it so the click is measurable against a
    session. If no link can be minted (no DCHUB_INTERNAL_KEY) the session
    button is served, and it still sells the pack.
    """
    from urllib.parse import urlencode
    tool = (info or {}).get("tool") or ""
    sid = (info or {}).get("sid") or ""
    kref = (info or {}).get("kref") or ""
    upgrade = None
    if kref:
        from routes.checkout_click_tracker import mint_checkout_token
        _go = mint_checkout_token("metered", kref, sid)
        if _go:
            upgrade = "https://dchub.cloud/go/c/" + _go
    keyed = upgrade is not None
    if not keyed:
        q = {"from": "mcp_relay", "tier": "metered", "direct": "1"}
        if tool:
            q["tool"] = tool
        if sid:
            q["sid"] = sid
        upgrade = "https://api.dchub.cloud/pricing/upgrade?" + urlencode(q)
    return upgrade, keyed


def _identify_form(token: str, info: dict | None) -> str:
    """The email field, or '' when the token carries no session to bind to."""
    if not (info or {}).get("sid"):
        return ""
    return (
        "<form method='post' action='/upgrade/h/%s' class='cap'>"
        "<label for='e'>Email me the receipt and the key</label>"
        "<input id='e' type='email' name='email' required "
        "placeholder='you@company.com' autocomplete='email'>"
        # frontend#1534 (2026-09-22): marketing consent is a separate, unticked
        # choice. Ticking it asks routes/marketing_opt_in for the double
        # opt-in (one confirmation email, requested by this person); nothing
        # else is ever sent unless they confirm it.
        "<label class='optin'><input type='checkbox' name='marketing_opt_in' "
        "value='1'> Also email me DC Hub product updates. I will get one email "
        "to confirm first, and can unsubscribe anytime.</label>"
        "<button type='submit'>Continue &rarr;</button>"
        "<span class='hint'>So the credits and the API key reach you — your "
        "agent has no inbox. We do not sell or share it.</span>"
        "</form>" % _esc(token))


def _relay_identify(token: str, info: dict | None):
    """POST handler: capture, then continue to the SAME checkout the button
    carries. Never blocks the purchase — a failed or refused capture still
    redirects."""
    from flask import redirect
    dest, _keyed = _upgrade_target(info)
    sid = (info or {}).get("sid") or ""
    if sid:
        try:
            from routes.relay_identify import capture
            capture(sid, (request.form.get("email") or ""), "relay_page",
                    tool=(info or {}).get("tool") or "")
        except Exception:  # noqa: BLE001
            logger.warning("relay identify capture failed", exc_info=True)
        # Consent only when the box was ticked, and only through the double
        # opt-in: request_opt_in validates, honours suppression and its
        # per-address cooldown, and sends the one confirmation email. The
        # address becomes marketable only when that email's link is clicked.
        # An unticked box sends nothing and records nothing.
        if request.form.get("marketing_opt_in") == "1":
            try:
                from routes.marketing_opt_in import request_opt_in
                request_opt_in((request.form.get("email") or ""), source="relay_page")
            except Exception:  # noqa: BLE001
                logger.warning("relay marketing opt-in request failed", exc_info=True)
    return redirect(dest, code=302)


# ── the POST-PAY page (r-post-pay-identify, 2026-09-24) ────────────────────
# Owner decision 2026-09-21: /upgrade/h never stands in front of checkout; it
# moves AFTER payment. Measured 2026-09-24: the checkout webhook already binds
# the buyer's Stripe email to the paying session (relay_identify
# capture_from_checkout, 2 of 2 MCP-link payments), while the pre-checkout
# form caught one address in 241 sessions — ours. So this page asks nothing
# the webhook already knows. It confirms where receipts go and offers ONE
# thing only the buyer can give: explicit marketing consent.
#
# Stripe Payment Links redirect here via their dashboard setting
# (?cs={CHECKOUT_SESSION_ID}); no code path owns that redirect.
#
# ★ THE EMAIL NEVER COMES FROM THE REQUEST. It is read server-side from the
# paid Checkout Session the cs id names, and shown masked. Knowing a cs id
# lets a visitor at most trigger the double opt-in email to that buyer, which
# request_opt_in rate-limits and which does nothing until the buyer clicks it.
#
# ★ ROUTE ORDER. '/upgrade/h/done' also matches '/upgrade/h/<token>'. Werkzeug
# ranks a static rule above a converter rule whatever the registration order;
# tests/test_post_pay_page.py pins that 'done' reaches this handler.
_CS_OK = re.compile(r"cs_(live|test)_[A-Za-z0-9]{8,250}")
_PENDING_RETRIES = 3


def _mask_email(email: str) -> str:
    local, _, dom = (email or "").partition("@")
    if not local or not dom:
        return ""
    return local[0] + "***@" + dom


def _paid_checkout(cs: str) -> dict | None:
    """{'ref_kind', 'email'} for a recorded paid Checkout Session, else None.

    Email: the webhook's capture first (what identify stamped), else the
    conversion row's payer. Never raises.
    """
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=4)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT p.ref_kind,"
                    " (SELECT r.email FROM relay_identify_captures r"
                    "   WHERE r.stripe_session_id = p.stripe_session_id"
                    "   ORDER BY r.captured_at DESC LIMIT 1),"
                    " (SELECT COALESCE(NULLIF(c.user_email, ''), c.caller_id)"
                    "   FROM mcp_conversions c"
                    "   WHERE c.stripe_session_id = p.stripe_session_id"
                    "   ORDER BY c.id DESC LIMIT 1)"
                    " FROM mcp_checkout_payments p"
                    " WHERE p.stripe_session_id = %s", (cs,))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("post-pay lookup swallowed: %s", e)
        return None
    if not row:
        return None
    email = next((str(x).strip().lower() for x in row[1:]
                  if x and "@" in str(x)), "")
    return {"ref_kind": row[0] or "", "email": email}


def _post_pay_html(body: str, refresh: str = "") -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='robots' content='noindex'>" + refresh +
            "<title>Payment received — DC Hub</title>"
            "<style>body{font-family:system-ui;max-width:560px;margin:48px auto;"
            "padding:0 20px;line-height:1.55;color:#111}h1{font-size:26px}"
            "form.cap{margin:22px 0 4px}"
            "form.cap label.optin{display:block;font-size:15px;margin-bottom:10px}"
            "form.cap button{width:100%;background:#3478f6;color:#fff;border:0;"
            "padding:13px 18px;border-radius:10px;font-weight:600;font-size:16px;"
            "cursor:pointer}small{color:#888}</style></head><body>"
            + body +
            "<p><small>DC Hub · dchub.cloud · questions: reply to your receipt."
            "</small></p></body></html>")


def _post_pay_response(html: str):
    from flask import make_response
    resp = make_response(html, 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@human_relay_bp.route("/upgrade/h/done", methods=["GET", "POST"])
def post_pay_page():
    src = request.form if request.method == "POST" else request.args
    cs = (src.get("cs") or "").strip()
    paid = _paid_checkout(cs) if _CS_OK.fullmatch(cs) else None
    kind = (paid or {}).get("ref_kind") or ""
    where = ("The credits are on the API key your agent already uses — its "
             "next call returns full data, no reconnect needed."
             if kind in ("pack_key", "sub_key") else
             "Your agent's next call returns full data.")

    if request.method == "POST":
        # Consent only on an explicit tick, only through the double opt-in,
        # only to the address the paid session carries. The reply is the same
        # whether a confirmation went out or was refused (suppressed, cooling
        # down), so the page reveals nothing about the address.
        if paid and paid.get("email") and request.form.get("marketing_opt_in") == "1":
            try:
                from routes.marketing_opt_in import request_opt_in
                request_opt_in(paid["email"], source="post_pay_page")
            except Exception:  # noqa: BLE001
                logger.warning("post-pay opt-in request failed", exc_info=True)
            msg = ("If that address can receive it, one confirmation email is "
                   "on its way. Nothing else is sent unless you confirm.")
        else:
            msg = "No problem — you will only get receipts and key emails."
        return _post_pay_response(_post_pay_html(
            "<h1>Thanks — you're all set</h1><p>%s</p><p>%s</p>"
            % (_esc(msg), _esc(where))))

    if not paid:
        # The redirect can beat the webhook by a few seconds. Retry briefly,
        # then stop: a thank-you that never resolves is still a thank-you.
        try:
            tries = int(request.args.get("r") or 0)
        except ValueError:
            tries = _PENDING_RETRIES
        refresh = ""
        if _CS_OK.fullmatch(cs) and tries < _PENDING_RETRIES:
            refresh = ("<meta http-equiv='refresh' content='3;url=/upgrade/h/done"
                       "?cs=%s&r=%d'>" % (_esc(cs), tries + 1))
        return _post_pay_response(_post_pay_html(
            "<h1>Payment received</h1><p>Thanks. We are finishing setup; your "
            "receipt comes from Stripe by email.</p>", refresh))

    email = paid.get("email") or ""
    to_line = ("Receipts and key details go to <b>%s</b>." % _esc(_mask_email(email))
               if email else "Your receipt comes from Stripe by email.")
    form = ("" if not email else
            "<form method='post' action='/upgrade/h/done' class='cap'>"
            "<input type='hidden' name='cs' value='%s'>"
            "<label class='optin'><input type='checkbox' name='marketing_opt_in' "
            "value='1'> Also email me DC Hub product updates. I will get one email "
            "to confirm first, and can unsubscribe anytime.</label>"
            "<button type='submit'>Done</button></form>" % _esc(cs))
    return _post_pay_response(_post_pay_html(
        "<h1>Thanks — payment received</h1><p>%s</p><p>%s</p>%s"
        % (_esc(where), to_line, form)))


@human_relay_bp.route("/upgrade/h/<token>", methods=["GET", "POST"])
def relay_page(token):
    info = parse_relay_token(token)
    # ★ 2026-09-17 (r-identify-rung) — THE EMAIL FORM POSTS BACK TO THIS PATH.
    # Not a new route: _routes.json sits at 98/98 rules, the deploy cap, and a
    # 99th is dropped silently, so a new top-level path would never reach the
    # worker. '/upgrade*' already forwards here, POST included (verified
    # through the edge before this shipped: POST /upgrade/h/<junk> returned
    # Flask's own 405, i.e. it reached the origin).
    #
    # A POST is a submit, not an open. _log_open on it would double-count the
    # human who filled the form in against the one who only looked, and
    # human_acted reads that table.
    if request.method == "POST":
        return _relay_identify(token, info)
    _log_open(info, token, valid=info is not None)
    # ── r-go-click-identify (2026-09-18) ──────────────────────────────────
    # The same join as the /go/c click, one hop earlier. A KEYED token carries
    # pk-<sha256(the caller's api key)> beside its session, and that key often
    # already has an address bound to it — so this human is identified the
    # moment they open the page, without being asked to type anything the
    # form below would ask for anyway. The form still renders: it is the only
    # path for a keyless caller, and capture() never overwrites an address
    # that is already there, so the two cannot fight.
    _kref = (info or {}).get("kref") or ""
    _ksid = (info or {}).get("sid") or ""
    if _kref and _ksid:
        try:
            from routes.relay_identify import capture_from_key_ref
            capture_from_key_ref(_kref, _ksid,
                                 tool=(info or {}).get("tool") or "")
        except Exception:  # noqa: BLE001
            logger.debug("relay-open identify swallowed", exc_info=True)
    tool = (info or {}).get("tool") or ""
    upgrade, keyed = _upgrade_target(info)
    # Empty string when the token carries no session: with nothing to
    # bind an email to, an email field would collect a lead we could
    # not attach to anything, and the button below would be labelled
    # "Skip" with nothing above it to skip.
    form = _identify_form(token, info)
    playground = ("https://dchub.cloud/playground?ref=relay"
                  + ("-" + tool if tool else ""))
    tool_line = (
        "Your AI assistant called <b>%s</b> on DC Hub — live data-center, "
        "grid, fiber and M&amp;A intelligence — and hit the paid data "
        "boundary." % _esc(tool)
        if tool else
        "Your AI assistant was using DC Hub — live data-center, grid, fiber "
        "and M&amp;A intelligence — and hit the paid data boundary.")
    pack_line = (
        "The <b>$10 one-time pack</b> (1,000 API calls, no subscription) "
        "pays for full data on every call, and the credits land on the API "
        "key your agent is already using."
        if keyed else
        "The <b>$10 one-time pack</b> (1,000 API calls, no subscription) "
        "pays for full data on every call — your agent's very next call "
        "returns complete data, no reconnect needed.")
    # r-relay-teaser (2026-09-24): ONE withheld number, free, looked up by the
    # token (routes/relay_teaser: stored server-side so the agent never sees it).
    teaser_html = ""
    if info is not None:
        try:
            from routes.relay_teaser import get_teaser
            _t = get_teaser(token)
        except Exception:  # noqa: BLE001
            _t = None
        if _t:
            teaser_html = ("<p class='teaser'>One number your agent's preview held back: "
                           "<b>%s: %s</b>. The full answer has the rest.</p>"
                           % (_esc(_t[0]), _esc(_t[1])))
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='robots' content='noindex'>"
            "<title>DC Hub: full data for your AI agent</title>"
            "<style>body{font-family:system-ui;max-width:560px;margin:48px auto;"
            "padding:0 20px;line-height:1.55;color:#111}"
            ".btn{display:block;text-align:center;background:#3478f6;color:#fff;"
            "padding:14px 18px;border-radius:10px;text-decoration:none;"
            "font-weight:600;font-size:17px;margin:22px 0 10px}"
            ".alt{color:#666;font-size:14px}h1{font-size:26px}"
            "form.cap{margin:22px 0 4px}"
            "form.cap label{display:block;font-weight:600;font-size:15px;"
            "margin-bottom:7px}"
            "form.cap input{width:100%%;box-sizing:border-box;padding:12px 13px;"
            "font-size:16px;border:1px solid #ccd;border-radius:9px}"
            "form.cap button{width:100%%;margin-top:9px;background:#3478f6;"
            "color:#fff;border:0;padding:13px 18px;border-radius:10px;"
            "font-weight:600;font-size:16px;cursor:pointer}"
            "form.cap .hint{display:block;color:#888;font-size:12.5px;"
            "margin-top:8px}"
            # The primary action is the form. The button below it is the way
            # PAST the form, so it reads as secondary only when a form is
            # actually above it — a sibling rule, because the button's bytes
            # are pinned by test_relay_sells_what_it_says and parsed live by
            # checkout-integrity lane 3. Restyling it inline would have
            # changed the very anchor those two read.
            "form.cap ~ a.btn{background:#eef1f6;color:#333;font-weight:500}"
            "small{color:#888}</style></head><body>"
            "<h1>Your AI agent found data worth the full answer</h1>"
            "<p>%s</p>"
            "%s"
            "<p>%s</p>"
            "%s"
            "<a class='btn' href='%s'>Get full data — $10 one-time</a>"
            "<p class='alt'>Prefer to look first? <a href='%s'>Explore the live "
            "data free in your browser</a> — no signup.</p>"
            "<p><small>DC Hub · dchub.cloud · data licensed CC-BY-4.0 · this "
            "link was generated for your agent's session%s</small></p>"
            "</body></html>"
            % (tool_line, teaser_html, pack_line, form, _esc(upgrade), _esc(playground),
               "" if info else " (link expired — the button still works)"))
    from flask import make_response
    resp = make_response(html, 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@human_relay_bp.route("/api/v1/relay/teaser", methods=["POST"])
def relay_teaser_store():
    """r-relay-teaser: the MCP server stores ONE withheld number for a relay token
    it minted, so the page can show it free without the agent ever seeing it.
    Internal key only; the token must verify (routes/relay_teaser.store_teaser)."""
    from routes.mcp_high_intent_claim import _internal_ok
    if not _internal_ok(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    from routes.relay_teaser import store_teaser
    body = request.get_json(silent=True) or {}
    out = store_teaser(str(body.get("token") or ""), str(body.get("label") or ""),
                       str(body.get("value") or ""))
    if out.get("ok"):
        return jsonify(out), 200
    code = 400 if out.get("error") in ("invalid_token", "invalid_teaser") else 503
    return jsonify(out), code


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
