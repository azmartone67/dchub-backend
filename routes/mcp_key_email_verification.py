"""Paid MCP tier follows a VERIFIED email binding — /api/v1/keys/confirm.

THE DEFECT THIS CLOSES (2026-09-11)
  mcp_dev_keys has no user_id FK, so `email` is the only link between a key and
  a paying account, and paid tier was granted on that link alone. Both public
  doors that write the column — POST /api/v1/keys/claim and POST
  /api/v1/keys/identify — only regex-checked the address before calling
  _inherit_paid_tier, which upgrades on `LOWER(u.email) = LOWER(k.email)`.
  Nothing established that the caller owned the inbox. Anyone who knew a paying
  customer's address could claim a key with it and receive tier 'paid' in the
  same response. The daily billing reconcile and the Stripe checkout webhook
  ran the same email match over EVERY active key, so a key bound to a
  customer's address before or after they paid was promoted too.

THE RULE NOW
  An email match may grant paid tier only to a key whose binding is VERIFIED —
  metadata->>'email_verified_for' equal to the address being granted on. The
  marker NAMES its address rather than being a boolean, so the proof cannot
  outlive the binding it was made about: /keys/identify will overwrite k.email
  with whatever the caller sends, and a boolean would let someone confirm their
  own address and then re-point the key at a payer's. Exactly two things write
  the marker:

    1. a click on the link this module emails, which only ever goes to the
       bound address (the inbox owner is the only one who can act on it);
    2. an OAuth resolve, where the address comes from the authenticated
       identity the IdP asserted, not from the request body.

WHAT IS DELIBERATELY PRESERVED
  r-coldbuy (2026-08-08): a customer who pays BEFORE ever claiming a key must
  still get paid MCP access. That still works — claim/identify with the address
  they paid with, click the link we send to it, and the key is paid. The
  inheritance itself is unchanged; only its precondition is new.

ENUMERATION SAFETY
  The HTTP response never says whether the address matched a paying account.
  Before this change the claim response carried `paid_plan_applied: true`,
  which was itself an oracle: it told anyone who asked whether a given address
  was a paying customer. Paid tier can no longer be applied in that response at
  all, so that key is simply never added to it, and the confirmation email —
  sent only when a match exists — reaches the address itself, never the caller.

WHY A POST, NOT A ONE-CLICK GET
  Corporate mail scanners (Safe Links, Proofpoint) follow links in delivered
  mail. A GET that granted the tier would let the VICTIM's own mail security
  confirm an attacker's binding. So GET renders a page with a button and the
  grant happens on POST, which scanners do not send.

The token is an HMAC over (api_key, email, issued-at) — unforgeable, single
purpose, no token column, and it stops working the moment the key is re-bound
to a different address. It carries developer_id, never the api_key itself, so
the credential never lands in a URL, a browser history or a Referer header.
"""
import hashlib
import hmac
import html as _html
import os
import time
from urllib.parse import quote

from flask import Blueprint, Response, request

mcp_key_verify_bp = Blueprint("mcp_key_email_verification", __name__)

SITE = os.environ.get("DCHUB_SITE", "https://dchub.cloud")

# A confirmation link stays usable for two weeks. Long enough that a customer
# who paid on a Friday and read their mail the following week is not stranded;
# short enough that an address left bound on an abandoned key does not carry an
# indefinitely live grant.
_MAX_AGE_SECONDS = 14 * 24 * 3600

# The MCP plans an inherited tier can come from. Same list _inherit_paid_tier
# matches on — kept here only to decide whether a confirmation email is worth
# sending; the grant itself re-checks in SQL.
_PAID_PLANS = ("developer", "pro", "founding", "enterprise")


def _secret():
    """The signing key, or None when none is configured.

    Same env chain routes/digest.py uses — but NO hardcoded fallback. digest's
    token only unsubscribes an address; this one grants a paid tier, and this
    repository is public, so a literal default here would be a signing key
    anyone could read and mint their own confirmations with. Missing secret =
    no tokens minted and none accepted (#4408 / #4411 fixed the same
    fail-open-when-DCHUB_ADMIN_KEY-is-unset shape on two admin gates).
    """
    for var in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SESSION_SECRET"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val.encode()
    try:
        import logging
        logging.getLogger(__name__).error(
            "key-binding confirmation is DISABLED: no DCHUB_ADMIN_KEY / "
            "DCHUB_INTERNAL_KEY / DCHUB_SESSION_SECRET is set, so paid-tier "
            "confirmation links cannot be signed or verified.")
    except Exception:
        pass
    return None


def _sign(api_key: str, email: str, issued: int) -> str:
    """HMAC over the SECRET key string, the address and the issue time.

    Binding the signature to the api_key is what makes a token single-purpose:
    it cannot be replayed onto another key. Binding it to the email is what
    makes a re-bind revoke it — change the address on the row and every
    outstanding link for it stops verifying.
    """
    secret = _secret()
    if secret is None:
        return ""       # unsignable: confirm_url and _token_ok both refuse below
    msg = "|".join((api_key or "", (email or "").strip().lower(), str(issued)))
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:40]


def _token_ok(api_key: str, email: str, issued: str, token: str) -> bool:
    """Constant-time check, plus the age cap. False on any bad input."""
    try:
        iss = int(str(issued or "0"))
    except (TypeError, ValueError):
        return False
    if iss <= 0 or (time.time() - iss) > _MAX_AGE_SECONDS:
        return False
    if not api_key or not email or not token:
        return False
    expected = _sign(api_key, email, iss)
    if not expected:            # no signing secret — accept nothing
        return False
    try:
        return hmac.compare_digest(str(token), expected)
    except Exception:
        return False


def confirm_url(api_key: str, developer_id: str, email: str, issued=None) -> str:
    """The link we email. Carries developer_id — never the api_key."""
    issued = int(issued if issued is not None else time.time())
    e = (email or "").strip().lower()
    sig = _sign(api_key, e, issued)
    if not sig:
        return ""               # no signing secret — there is no link to send
    # The signature travels as `token=` for a reason beyond readability: CF
    # cache rule 24 ("Bypass cache for CREDENTIALED /api/*") keys off a fixed
    # LIST of credential-shaped query-arg names, and `token` is on it. Rule 2
    # caches everything under /api/v1/ with mode override_origin, which ignores
    # the no-store this page sends — so named anything else (`t`), the
    # interstitial, token and all, would be stored in a shared edge cache
    # against the origin's instruction. Named `token`, the live ruleset already
    # bypasses it, with no zone change to apply and nothing to drift.
    # tests/test_paid_tier_needs_a_verified_email.py pins this against the
    # canon so a rename cannot quietly make the page cacheable again.
    return (f"{SITE}/api/v1/keys/confirm?d={quote(developer_id or '')}"
            f"&e={quote(e)}&s={issued}&token={sig}")


# ── the email ────────────────────────────────────────────────────────────
def _confirm_html(email: str, url: str) -> str:
    # `email` is caller-supplied and the address regex admits <>"'/ — escape
    # before interpolating, or a crafted address injects HTML into our mail.
    email_e = _html.escape(email)
    url_e = _html.escape(url)
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;line-height:1.55">
  <h2 style="font-weight:600;margin:0 0 4px">Link your DC Hub plan to this key 🛰️</h2>
  <p style="color:#666;margin:0 0 18px">Someone connected a DC Hub API key using this address.</p>
  <p>Your account <b>{email_e}</b> is on a paid plan. Confirm that the key is yours and we'll apply your paid MCP tier to it:</p>
  <p style="margin:18px 0">
    <a href="{url_e}" style="background:#111;color:#fff;text-decoration:none;padding:11px 20px;border-radius:7px;font-weight:600;display:inline-block">Confirm this key is mine →</a>
  </p>
  <p style="color:#444;font-size:14px"><b>If this wasn't you, do nothing.</b> The key stays on the free tier and your plan is untouched. We ask because the address alone isn't proof — only this click is.</p>
  <p style="color:#444;font-size:14px">The link works for 14 days. You can always see and rotate your own keys at <a href="https://dchub.cloud/login">dchub.cloud/login</a>.</p>
  <p style="margin-top:20px">— DC Hub<br><span style="color:#888;font-size:13px">dchub.cloud</span></p>
</div>"""


def _send(to_email: str, subject: str, body_html: str) -> bool:
    """Same Resend path routes/keys_recover.py uses. Soft-fail to False."""
    try:
        from routes.keys_recover import _send as _kr_send
        return bool(_kr_send(to_email, subject, body_html))
    except Exception:
        return False


def _dispatch(fn, *args) -> None:
    """Run a send OFF the request path — the shape
    routes/redeem_tracking.send_identify_welcome uses.

    /keys/claim is a hot, agent-facing endpoint and the Resend call carries a
    20-second timeout. Sending inline would put that timeout on the claim's own
    response (and, worse, hold the pooled connection while it ran), so a slow
    mail provider would turn into slow — or edge-timed-out — key claims. The
    offer is best-effort by definition: the decision is already made and
    committed; only the courtesy email is deferred.

    Tests replace this with a direct call so they can assert on what was sent.
    """
    try:
        import threading
        threading.Thread(target=fn, args=args, daemon=True).start()
    except Exception:
        try:
            fn(*args)
        except Exception:
            pass


# ── deciding whether to offer confirmation ───────────────────────────────
def confirmation_is_pending(cur, api_key: str, email: str) -> bool:
    """True when confirming this binding would actually unlock something.

    All three must hold: the address has an ACTIVE paid plan, this key is not
    already paid, and the binding is not already verified. Read-only and
    fail-closed — on any error we simply don't offer, and the caller's HTTP
    response is the same either way.
    """
    if not api_key or not email:
        return False
    try:
        cur.execute(
            """SELECT 1
                 FROM mcp_dev_keys k
                 JOIN users u ON LOWER(u.email) = LOWER(k.email)
                WHERE k.api_key = %s
                  AND LOWER(k.email) = LOWER(%s)
                  AND u.plan = ANY(%s)
                  AND COALESCE(u.subscription_status,'') = 'active'
                  AND COALESCE(k.tier,'free') NOT IN ('paid','enterprise')
                  AND LOWER(COALESCE(k.metadata->>'email_verified_for',''))
                      <> LOWER(%s)
                LIMIT 1""",
            (api_key, email, list(_PAID_PLANS), email),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _may_send(email: str) -> bool:
    """Spend from the SAME per-address daily budget /keys/recover and
    /api/v1/dev-signup share.

    Without this, /keys/claim is an open relay at one email per request: an
    attacker who wants to bury a customer just claims keys with their address
    in a loop. Sharing the counter (rather than adding a third one) is what
    stops alternating the endpoints from multiplying the budget.
    """
    try:
        from routes.keys_recover import _rate_ok, _client_ip
        try:
            ip = _client_ip(request)
        except Exception:
            ip = ""             # outside a request context (the Stripe webhook)
        return bool(_rate_ok(email, ip))
    except Exception:
        return True             # limiter unavailable: send rather than strand


def offer_confirmation(cur, api_key: str, email: str) -> bool:
    """If confirming would unlock a paid tier, email the link to the BOUND
    address. Returns whether a send was attempted — for logging only. NEVER let
    this decide anything a caller can observe: that it ran at all is exactly the
    fact an attacker probing addresses must not learn.
    """
    try:
        if not confirmation_is_pending(cur, api_key, email):
            return False
        if not _may_send(email):
            return False
        cur.execute("SELECT developer_id FROM mcp_dev_keys WHERE api_key = %s",
                    (api_key,))
        row = cur.fetchone()
        developer_id = (row[0] if row else "") or ""
        url = confirm_url(api_key, developer_id, email)
        if not url:
            return False
        _dispatch(_send, email, "Confirm your DC Hub key to apply your paid plan",
                  _confirm_html(email, url))
        return True
    except Exception:
        return False


def _mask(api_key: str) -> str:
    """Enough of the key for its holder to recognise it, not enough to use."""
    k = api_key or ""
    return (k[:13] + "…" + k[-4:]) if len(k) > 20 else "your key"


def _choose_html(email: str, rows) -> str:
    email_e = _html.escape(email)
    items = "".join(
        f'<li style="margin:12px 0">'
        f'<code style="background:#f4f4f5;border:1px solid #e4e4e7;border-radius:5px;'
        f'padding:3px 7px;font-size:13px">{_html.escape(_mask(k))}</code> — '
        f'<a href="{_html.escape(u)}" style="font-weight:600">confirm this one →</a></li>'
        for k, u in rows)
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;line-height:1.55">
  <h2 style="font-weight:600;margin:0 0 4px">Apply your paid plan to your DC Hub key 🛰️</h2>
  <p style="color:#666;margin:0 0 18px">Thanks for subscribing — one click and your agent is on your paid tier.</p>
  <p>These DC Hub API keys are connected to <b>{email_e}</b>. Confirm the one your agent is using and we'll apply your paid MCP tier to it:</p>
  <ul style="padding-left:18px;margin:16px 0">{items}</ul>
  <p style="color:#444;font-size:14px">We ask because an address on its own isn't proof a key is yours — anyone can type it. Only this click links the two.</p>
  <p style="color:#444;font-size:14px"><b>Don't recognise a key above?</b> Leave it alone — it stays on the free tier. You can always see and rotate your own keys at <a href="https://dchub.cloud/login">dchub.cloud/login</a>. Links work for 14 days.</p>
  <p style="margin-top:20px">— DC Hub<br><span style="color:#888;font-size:13px">dchub.cloud</span></p>
</div>"""


def offer_confirmation_for_address(email: str, cur=None, limit: int = 5) -> int:
    """Email the inbox owner a confirm link for each unconfirmed key on their
    address. Returns how many keys were offered (0 on any problem).

    THE ORDER THIS EXISTS FOR: claim a key, THEN pay. The Stripe checkout
    webhook used to promote every active key bound to the paying address; now
    it promotes only confirmed ones, so without this call a customer who
    claimed before paying would have no way to confirm and would sit on the
    free tier — the exact r-coldbuy failure, re-created at the other end of the
    funnel. So when the webhook withholds, it asks instead.

    Sends ONE email listing every candidate, each key masked, so an owner with
    more than one key confirms the one their agent actually holds — and so a
    key someone else bound to their address is visible to them rather than
    silently promoted.
    """
    e = (email or "").strip().lower()
    if not e:
        return 0
    try:
        if cur is not None:
            return _offer_for_address(cur, e, limit)
        import flask_mcp_endpoints as _fme
        with _fme._pool.connection() as conn, conn.cursor() as own_cur:
            return _offer_for_address(own_cur, e, limit)
    except Exception:
        return 0


def _offer_for_address(cur, e: str, limit: int) -> int:
    cur.execute(
        """SELECT k.api_key, k.developer_id
             FROM mcp_dev_keys k
             JOIN users u ON LOWER(u.email) = LOWER(k.email)
            WHERE LOWER(k.email) = LOWER(%s)
              AND COALESCE(k.status,'active') = 'active'
              AND u.plan = ANY(%s)
              AND COALESCE(u.subscription_status,'') = 'active'
              AND COALESCE(k.tier,'free') NOT IN ('paid','enterprise')
              AND LOWER(COALESCE(k.metadata->>'email_verified_for','')) <> LOWER(%s)
            ORDER BY k.created_at DESC NULLS LAST
            LIMIT %s""",
        (e, list(_PAID_PLANS), e, int(limit)),
    )
    rows = [(r[0], confirm_url(r[0], r[1], e)) for r in (cur.fetchall() or [])]
    rows = [(k, u) for k, u in rows if u]        # unsignable links are not links
    if not rows or not _may_send(e):
        return 0
    _dispatch(_send, e, "Confirm your DC Hub key to apply your paid plan",
              _choose_html(e, rows))
    return len(rows)


# ── the grant ────────────────────────────────────────────────────────────
def _resolve(cur, developer_id: str, email: str, issued: str, token: str):
    """Return the api_key this token is valid for, or None.

    Looked up by developer_id + the CURRENT email on the row, then each
    candidate's api_key must reproduce the HMAC. Verifying against the row's own
    api_key (rather than trusting the URL) is what keeps a developer_id
    collision, or a row re-bound to another address, from being confirmable.
    """
    cur.execute(
        """SELECT api_key FROM mcp_dev_keys
            WHERE developer_id = %s
              AND LOWER(COALESCE(email,'')) = LOWER(%s)
              AND COALESCE(status,'active') = 'active'""",
        (developer_id, email),
    )
    for (candidate,) in cur.fetchall() or []:
        if _token_ok(candidate, email, issued, token):
            return candidate
    return None


def _mark_verified(cur, api_key: str, email: str) -> None:
    cur.execute(
        """UPDATE mcp_dev_keys
              SET metadata = COALESCE(metadata,'{}'::jsonb)
                             || jsonb_build_object(
                                  -- Names the address proven, so a later
                                  -- re-bind to a different one revokes it
                                  -- without any writer having to remember to.
                                  'email_verified_for', LOWER(%s::text),
                                  'email_verified_at',
                                  to_char(NOW() AT TIME ZONE 'UTC',
                                          'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                                  'email_verified_via', 'confirm_link')
            WHERE api_key = %s""",
        (email, api_key),
    )


# ── pages ────────────────────────────────────────────────────────────────
def _page(title: str, body: str, form_token=None) -> Response:
    form = ""
    if form_token:
        d, e, s, t = form_token
        form = (
            '<form method="POST" action="/api/v1/keys/confirm" style="margin:22px 0">'
            f'<input type="hidden" name="d" value="{_html.escape(d)}">'
            f'<input type="hidden" name="e" value="{_html.escape(e)}">'
            f'<input type="hidden" name="s" value="{_html.escape(s)}">'
            f'<input type="hidden" name="token" value="{_html.escape(t)}">'
            '<button type="submit" style="background:#111;color:#fff;border:0;'
            'padding:12px 22px;border-radius:7px;font-weight:600;font-size:15px;'
            'cursor:pointer">Confirm this key is mine</button></form>')
    return Response(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{_html.escape(title)} · DC Hub</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#fafafa;margin:0;padding:48px 20px;color:#0f172a">
<div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e4e4e7;border-radius:12px;padding:32px">
<h1 style="font-size:22px;margin:0 0 10px">{_html.escape(title)}</h1>
<p style="line-height:1.6;color:#3f3f46;margin:0">{body}</p>
{form}
<p style="margin:26px 0 0;color:#71717a;font-size:13px">— DC Hub · <a href="https://dchub.cloud/mcp" style="color:#71717a">dchub.cloud/mcp</a></p>
</div></body></html>""",
        status=200, mimetype="text/html",
        # This page is per-token and mutates on POST — it must never sit in a
        # shared cache. CF Rule #3 overrides origin caching on /api/v1/*, so a
        # bypass rule for this path is part of shipping it (see the PR).
        headers={"Cache-Control": "no-store, private", "X-Robots-Tag": "noindex"},
    )


@mcp_key_verify_bp.route("/api/v1/keys/confirm", methods=["GET", "POST"])
def confirm_binding():
    """GET renders the button; POST performs the grant.

    One route, two methods (the shape routes/digest.py uses for its unsubscribe
    link). The split is the point: a GET that granted the tier would let a
    link-prefetching mail scanner in the recipient's OWN org confirm an
    attacker's binding on their behalf, and Safe Links / Proofpoint follow
    every link in delivered mail. Scanners do not submit forms.
    """
    if request.method == "GET":
        d = (request.args.get("d") or "").strip()
        e = (request.args.get("e") or "").strip().lower()
        s = (request.args.get("s") or "").strip()
        t = (request.args.get("token") or "").strip()
        if not (d and e and s and t):
            return _page("Link not valid",
                         "This confirmation link is incomplete. Open the most "
                         "recent confirmation email we sent, or claim a key again "
                         "and we'll send a fresh link.")
        return _page(
            "Confirm your DC Hub key",
            f"Apply the paid plan on <b>{_html.escape(e)}</b> to the DC Hub API "
            "key that was bound to this address? If you didn't connect a key, "
            "close this page — nothing changes and the key stays on the free tier.",
            form_token=(d, e, s, t))

    src = request.form if request.form else (request.get_json(silent=True) or {})
    d = (str(src.get("d") or "")).strip()
    e = (str(src.get("e") or "")).strip().lower()
    s = (str(src.get("s") or "")).strip()
    t = (str(src.get("token") or "")).strip()

    _expired = "This link is no longer valid — it may have expired, or the key may have been bound to a different address since. Claim or identify the key again and we'll send a fresh link."
    if not (d and e and s and t):
        return _page("Link not valid", _expired)

    try:
        import flask_mcp_endpoints as _fme
        with _fme._pool.connection() as conn, conn.cursor() as cur:
            api_key = _resolve(cur, d, e, s, t)
            if not api_key:
                return _page("Link not valid", _expired)
            _mark_verified(cur, api_key, e)
            # Idempotent and upgrade-only: a second click, or a key already on a
            # paid tier, updates nothing and still renders success.
            _fme._inherit_paid_tier(cur, api_key, e)
    except Exception:
        return _page(
            "Almost there",
            "We couldn't finish that just now. Click the button again in a "
            "minute — nothing was changed, and the link still works.")

    return _page(
        "Key confirmed",
        f"Your DC Hub key is now linked to <b>{_html.escape(e)}</b> and carries "
        "your paid MCP tier. Your agent picks it up on its next call — no "
        "reconnect needed.")


def register(app):
    app.register_blueprint(mcp_key_verify_bp)
    return True
