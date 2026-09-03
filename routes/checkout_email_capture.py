"""
checkout_email_capture.py — capture email before Stripe checkout.

Phase ZZZZZ-round38 (2026-05-25). 3,487 paywall signals → 0 identified.
Stripe checkout itself captures email for the customer record, but if
the user abandons before paying, that data is lost. This module inserts
a lightweight email-capture page BETWEEN the paywall response and the
Stripe redirect:

  MCP paywall → /pricing/checkout/start?tool=X
    ↓ (user enters email)
  POST /pricing/checkout/submit
    ↓ (save → identified_signals table)
  302 → buy.stripe.com/...?prefilled_email=user@co.com

Result: even if user abandons Stripe, we have their email + the tool
that triggered the paywall hit. Outreach cron can send "you tried
get_grid_intelligence — here's a 7-day Pro trial" within hours.

Endpoints:
  GET  /pricing/checkout/start?tool=X[&tier=Y]  → email capture HTML
  POST /pricing/checkout/submit                  → save + 302 to Stripe
  GET  /api/v1/checkout/identified-signals       → admin diagnostic
"""
import os
import datetime
from contextlib import contextmanager
from urllib.parse import quote
from flask import Blueprint, request, redirect, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

checkout_email_bp = Blueprint("checkout_email_capture", __name__)

# r39 (2026-05-25): centralized in routes/_stripe_links.py
from routes._stripe_links import STRIPE_LINKS, TOOL_TIER_MAP
from routes._swallowed_writes import note_swallowed_write


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS identified_checkout_signals (
                    id            SERIAL PRIMARY KEY,
                    email         TEXT NOT NULL,
                    tool          TEXT,
                    tier          TEXT,
                    stripe_url    TEXT,
                    user_agent    TEXT,
                    ip            TEXT,
                    captured_at   TIMESTAMPTZ DEFAULT NOW(),
                    converted     BOOLEAN DEFAULT FALSE,
                    converted_at  TIMESTAMPTZ,
                    outreach_sent BOOLEAN DEFAULT FALSE,
                    outreach_at   TIMESTAMPTZ,
                    notes         TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_checkout_email ON identified_checkout_signals(email);
                CREATE INDEX IF NOT EXISTS ix_checkout_unconverted ON identified_checkout_signals(converted, captured_at)
                  WHERE converted = FALSE;
            """)
            c.commit()
    except Exception:
        pass

_ensure_table()


def _resolve_tier(tool, tier_param):
    """Delegates to the ONE resolver (routes/_stripe_links.resolve_tier).

    2026-09-02: this used to be a private copy whose fall-through was
    'developer' — the same silent-monthly-default the canon resolver just
    lost. Two resolvers with different defaults is how a paywall hit lands
    on a plan nobody named; keep exactly one.
    """
    from routes._stripe_links import resolve_tier
    return resolve_tier(tool, tier_param)


def _price_label(tier):
    """Human price label for the capture page — canon first, never '—' for a
    plan canon can price (the pack used to render as 'Metered — —')."""
    try:
        from routes._stripe_links import TIER_PRICE_LABEL
        lbl = TIER_PRICE_LABEL.get(tier)
        if lbl:
            return lbl
    except Exception:
        pass
    return {"starter":"$9/mo","developer":"$49/mo","pro":"$299/mo","enterprise":"Custom"}.get(tier, "—")


def mark_converted(email, stripe_session_id="", conn_factory=None):
    """Flip every un-converted identified_checkout_signals row for `email`
    to converted. Called from the Stripe checkout.session.completed webhook.

    2026-09-02 (finding 4c): nothing ever set `converted`. tj@karklins.com
    captured 08-08 (tier developer, outreach sent), paid $49 on Stripe the
    same day, and /api/v1/checkout/funnel-stats still read converted:0 on
    09-02 — the outreach loop kept treating a paying customer as a lead.

    Idempotent by construction (WHERE converted = FALSE), never raises —
    a failure here must not touch payment handling. Returns a dict the
    webhook prints so the log line says what happened.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "reason": "no_email"}
    note = ("converted via stripe webhook session=%s" % (stripe_session_id or "?"))[:200]
    try:
        factory = conn_factory or _conn
        with factory() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE identified_checkout_signals
                   SET converted = TRUE,
                       converted_at = NOW(),
                       notes = COALESCE(notes, '') || %s
                 WHERE lower(email) = %s
                   AND converted = FALSE
            """, (" · " + note, email))
            n = cur.rowcount if cur.rowcount is not None else 0
            c.commit()
        return {"ok": True, "updated": int(n)}
    except Exception as e:  # noqa: BLE001 — never break the webhook
        try:
            note_swallowed_write("identified_checkout_signals",
                                 where="checkout_email_capture.mark_converted")
        except Exception:
            pass
        return {"ok": False, "reason": str(e)[:120]}


_CAPTURE_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>One step before checkout — DC Hub</title>
<meta name="robots" content="noindex">
<link rel="icon" href="https://dchub.cloud/favicon.ico" type="image/x-icon">
<link rel="apple-touch-icon" href="https://dchub.cloud/apple-touch-icon.png">
<style>
 body{font:16px/1.55 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;max-width:520px;margin:48px auto;padding:0 24px;color:#0f172a}
 .eyebrow{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600;margin-bottom:8px}
 h1{font-size:1.7rem;margin:0 0 12px;letter-spacing:-.01em}
 .lead{color:#475569;margin-bottom:24px}
 form{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:24px}
 label{display:block;font-weight:600;font-size:.9rem;margin-bottom:6px}
 input[type=email]{width:100%;padding:12px 14px;font-size:1.05rem;border:1px solid #cbd5e1;border-radius:8px;box-sizing:border-box;font-family:inherit}
 input[type=email]:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.15)}
 button{margin-top:14px;width:100%;padding:12px 18px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;font-family:inherit}
 button:hover{filter:brightness(1.05)}
 .small{font-size:.8rem;color:#64748b;margin-top:14px;line-height:1.4}
 .pane{font-size:.85rem;color:#64748b;padding:14px;background:#fff;border:1px dashed #e2e8f0;border-radius:8px;margin-top:14px}
 code{background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}
 .skip{display:block;text-align:center;color:#94a3b8;font-size:.8rem;margin-top:18px;text-decoration:none}
 .skip:hover{color:#6366f1}
</style></head><body>
<div class="eyebrow">__TIER_LABEL__ · __PRICE_LABEL__</div>
<h1>One step before checkout</h1>
<p class="lead">You're upgrading from <code>__TOOL__</code>. Leave your email so we can send your API key + receipt immediately after Stripe completes.</p>

<form method="post" action="https://api.dchub.cloud/pricing/checkout/submit">
  <input type="hidden" name="tool" value="__TOOL__">
  <input type="hidden" name="tier" value="__TIER__">
  <input type="hidden" name="surface" value="__SURFACE__">
  <input type="hidden" name="ref" value="__REF__">
  <label for="email">Work email</label>
  <input type="email" id="email" name="email" required autofocus placeholder="you@company.com" autocomplete="email">
  <button type="submit">Continue to Stripe checkout →</button>
  <p class="small">Same email as your Stripe payment method works best. We'll prefill it.</p>
</form>

<div class="pane">
  Pro tip: keep this tab open. Stripe checkout opens in the same window — once payment completes, you bounce back here and your API key is emailed.
</div>

<a class="skip" href="__STRIPE_URL__">Skip email — go straight to Stripe →</a>

<div class="pane" style="background:#fef3c7;border-color:#fbbf24;margin-top:14px;font-size:.85rem">
  💡 <b>Not ready for __TIER_LABEL__?</b> Start with <b>Starter — $9/mo</b>: same data sample as Developer, lighter cap. <a href="https://api.dchub.cloud/pricing/checkout/start?tier=starter&tool=__TOOL__" style="color:#92400e;font-weight:600">Switch to Starter →</a>
</div>
</body></html>"""


def _esc_attr(v):
    """Minimal HTML-attribute escape for values we inject into the form."""
    from html import escape
    return escape(str(v or ""), quote=True)


@checkout_email_bp.route("/pricing/checkout/start", methods=["GET"], strict_slashes=False)
def start():
    tool = (request.args.get("tool") or "").strip()
    tier_param = (request.args.get("tier") or "").strip()
    # per-surface-attr (2026-06-20): carry the web surface + page slug through
    # the email-capture form so submit() can stamp a parseable
    # web__<surface>__<slug> client_reference_id on the Stripe URL.
    surface = (request.args.get("surface") or "").strip()
    ref     = (request.args.get("ref") or "").strip()
    tier = _resolve_tier(tool, tier_param)
    stripe_url = STRIPE_LINKS[tier]
    price = _price_label(tier)
    html = (_CAPTURE_HTML
            .replace("__TOOL__", _esc_attr(tool) or "MCP")
            .replace("__TIER__", _esc_attr(tier))
            .replace("__TIER_LABEL__", tier.title())
            .replace("__PRICE_LABEL__", price)
            .replace("__SURFACE__", _esc_attr(surface))
            .replace("__REF__", _esc_attr(ref))
            .replace("__STRIPE_URL__", stripe_url))
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store"}


@checkout_email_bp.route("/pricing/checkout/submit", methods=["POST"])
def submit():
    email = (request.form.get("email") or request.json and request.json.get("email") or "").strip().lower()
    tool  = (request.form.get("tool")  or "").strip()
    surface = (request.form.get("surface") or "").strip()  # per-surface-attr
    web_ref = (request.form.get("ref") or "").strip()      # page slug
    # partner-attr (2026-08-07): /pricing/upgrade normally forwards ?surface=
    # into this form, but this endpoint is also reachable directly. Re-resolve
    # against the referral cookie so a partner-driven signup is attributed on
    # either entry path. An explicit surface still wins.
    from routes._attribution_ref import PARTNER_COOKIE, resolve_attribution
    surface, web_ref = resolve_attribution(
        surface, web_ref, request.cookies.get(PARTNER_COOKIE))
    tier  = _resolve_tier(tool, request.form.get("tier") or "")
    if not email or "@" not in email or "." not in email:
        return jsonify({"error": "invalid_email"}), 400

    stripe_url = STRIPE_LINKS[tier]
    ua = request.headers.get("User-Agent", "")[:240]
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""

    # Save the identified signal
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO identified_checkout_signals
                      (email, tool, tier, stripe_url, user_agent, ip)
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (email, tool or None, tier, stripe_url, ua, ip))
                c.commit()
        except Exception:
            note_swallowed_write("identified_checkout_signals", where="checkout_email_capture.submit")
            pass

    # Build Stripe URL with prefilled email + attribution.
    # per-surface-attr (2026-06-20): when a web surface drove this checkout,
    # emit the parseable web__<surface>__<slug> client_reference_id so the
    # webhook records mcp_conversions.web_source/web_tool. Otherwise keep the
    # legacy mcp:… shape (agent paywall; attributed via the funnel).
    if surface:
        from routes._attribution_ref import build_web_ref
        ref = build_web_ref(surface, web_ref)
    else:
        ref = f"mcp:tool={tool or 'none'}:tier={tier}:email_capture=1"
    sep = "&" if "?" in stripe_url else "?"
    final_url = f"{stripe_url}{sep}prefilled_email={quote(email)}&client_reference_id={quote(ref)}"
    return redirect(final_url, code=302)


@checkout_email_bp.route("/api/v1/checkout/identified-signals", methods=["GET"])
def list_identified():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, email, tool, tier, converted, outreach_sent, captured_at
                FROM identified_checkout_signals
                ORDER BY captured_at DESC LIMIT 100
            """)
            rows = cur.fetchall()
        return jsonify({
            "count": len(rows),
            "signals": [{
                "id": r[0], "email": r[1], "tool": r[2], "tier": r[3],
                "converted": r[4], "outreach_sent": r[5],
                "captured_at": r[6].isoformat() if r[6] else None,
            } for r in rows],
        }), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}", "detail": str(e)[:200]}), 500


@checkout_email_bp.route("/api/v1/checkout/funnel-stats", methods=["GET"])
def stats():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    out = {"computed_at": datetime.datetime.utcnow().isoformat() + "Z"}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM identified_checkout_signals")
            out["total_identified"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE captured_at > NOW() - INTERVAL '24 hours'")
            out["identified_24h"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE converted = TRUE")
            out["converted"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE outreach_sent = FALSE AND captured_at < NOW() - INTERVAL '1 hour'")
            out["awaiting_outreach"] = cur.fetchone()[0]
            cur.execute("""
                SELECT tier, COUNT(*) FROM identified_checkout_signals
                WHERE captured_at > NOW() - INTERVAL '7 days'
                GROUP BY tier ORDER BY 2 DESC
            """)
            out["by_tier_7d"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    return jsonify(out), 200
