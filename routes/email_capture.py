"""
Phase FF+16 — email capture across the funnel (2026-05-19)
==========================================================

Phase FF+15-outreach revealed the brutal truth: 5,222 paywall signals
in 7 days, only 1 with `user_email` populated. 99.98% of users hit
our paywall as anonymous MCP sessions and walked away unreachable.

This module ships all three capture paths in one coordinated piece:

  OPTION A  Email-on-paywall CTA (low friction)
            GET  /notify              landing page (HTML form)
            POST /api/v1/notify-when-free   AJAX submit

  OPTION B  Auto-trial with email (medium friction)
            POST /api/v1/auto-trial/with-email   mint trial key tied to email
            (extends the existing routes/auto_trial.py path)

  OPTION C  Stripe pre-checkout email capture (highest commercial intent)
            GET  /checkout/start     HTML page with email input → Stripe
            POST /checkout/initiate  creates Stripe Session with
                                     customer_email + client_reference_id

Plus:

  Backfill: POST /api/v1/admin/email-capture/backfill-signals
            joins mcp_email_capture(session_id) → mcp_upgrade_signals
            to retroactively populate user_email on past signals so
            Phase FF+15-outreach now has emails to send to.

  Stats:    GET  /api/v1/email-capture/stats
            captures by source, day-over-day, total addressable pool.

Both paywall builders (mcp_gatekeeper.py + utils/paywall_response.py)
get patched to surface email_capture_url + redirect buy_now_url
through /checkout/start so EVERY paywalled user gets the email
prompt — anonymous and api-keyed alike.
"""
import os
import re
import secrets
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, Response, redirect

email_capture_bp = Blueprint("email_capture", __name__)


# ─── schema ───────────────────────────────────────────────────────────
def _db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    """CREATE TABLE IF NOT EXISTS for mcp_email_capture.
    Idempotent + safe to call repeatedly."""
    conn = _db()
    if conn is None: return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcp_email_capture (
                    id          BIGSERIAL PRIMARY KEY,
                    email       TEXT NOT NULL,
                    session_id  TEXT,
                    tool        TEXT,
                    source      TEXT NOT NULL,
                    -- 'paywall_cta'    = Option A landing page
                    -- 'auto_trial'     = Option B auto-trial claim
                    -- 'checkout_start' = Option C pre-checkout
                    -- 'agent_claim'    = MCP /keys/claim
                    api_key_hint TEXT,
                    referer     TEXT,
                    user_agent  TEXT,
                    ip_address  TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_email_capture_email "
                        "ON mcp_email_capture (LOWER(email))")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_email_capture_sid "
                        "ON mcp_email_capture (session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_email_capture_at "
                        "ON mcp_email_capture (created_at DESC)")
            conn.commit()
        return True
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


try: _ensure_table()
except Exception: pass


# ─── email validation ─────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _valid_email(em: str) -> bool:
    if not em or len(em) > 254 or "@" not in em:
        return False
    em = em.strip().lower()
    if not _EMAIL_RE.match(em): return False
    # Block obvious test/disposable
    bad = {"@example.com", "@test.com", "@dchub.cloud",  # don't capture ourselves
           "@mailinator.com", "@tempmail.io", "@guerrillamail.com",
           "@10minutemail.", "@throwaway."}
    el = em.lower()
    return not any(b in el for b in bad)


def _record_capture(email, source, *, session_id=None, tool=None,
                    api_key_hint=None):
    """Idempotent insert. If the same email+source was captured in the
    last 30 days, return that row id instead of duplicating."""
    if not _valid_email(email): return None
    conn = _db()
    if conn is None: return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM mcp_email_capture
                WHERE LOWER(email) = LOWER(%s)
                  AND source = %s
                  AND created_at > NOW() - INTERVAL '30 days'
                ORDER BY created_at DESC LIMIT 1
            """, (email, source))
            existing = cur.fetchone()
            if existing:
                return int(existing[0])
            cur.execute("""
                INSERT INTO mcp_email_capture
                (email, session_id, tool, source, api_key_hint,
                 referer, user_agent, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                email.strip().lower(),
                (session_id or "")[:200] or None,
                (tool or "")[:80] or None,
                source[:40],
                (api_key_hint or "")[:40] or None,
                (request.headers.get("Referer") or "")[:300] or None,
                (request.headers.get("User-Agent") or "")[:300] or None,
                (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                 or request.remote_addr or "")[:80] or None,
            ))
            new_id = cur.fetchone()
            conn.commit()
            return int(new_id[0]) if new_id else None
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════
#  OPTION A — paywall CTA landing page + AJAX endpoint
# ═══════════════════════════════════════════════════════════════════════

_NOTIFY_LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Get notified — DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;
       background:#0a0e14;color:#e6e6e6;margin:0;
       min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
  .card{background:#161616;border:1px solid #262626;border-radius:14px;
        padding:36px 32px;max-width:480px;width:100%;
        box-shadow:0 16px 48px rgba(0,0,0,0.5)}
  h1{margin:0 0 8px;font-size:24px;font-weight:700;letter-spacing:-0.5px}
  .sub{color:#9ca3af;margin:0 0 24px;font-size:14px;line-height:1.55}
  .tool{display:inline-block;background:rgba(0,200,240,0.10);
        border:1px solid rgba(0,200,240,0.30);color:#5eead4;
        padding:3px 10px;border-radius:5px;font-size:11px;
        font-family:'JetBrains Mono',monospace;letter-spacing:0.3px;
        text-transform:uppercase;font-weight:700;margin-bottom:14px}
  label{display:block;font-size:12px;color:#9ca3af;margin-bottom:6px;font-weight:600}
  input[type="email"]{width:100%;padding:14px 14px;background:#0a0e14;
        border:1px solid #262626;border-radius:8px;color:#e6e6e6;
        font-size:15px;box-sizing:border-box;outline:none;
        font-family:inherit;transition:border-color .15s}
  input[type="email"]:focus{border-color:#5eead4}
  button{width:100%;padding:14px;margin-top:14px;background:#10b981;
         border:none;border-radius:8px;color:#0a0e14;font-weight:700;
         font-size:15px;cursor:pointer;font-family:inherit;transition:filter .15s}
  button:hover{filter:brightness(1.1)}
  button:disabled{opacity:0.5;cursor:not-allowed}
  .ok{color:#10b981;font-weight:600;margin-top:14px;text-align:center}
  .err{color:#ef4444;font-weight:600;margin-top:14px;text-align:center}
  .small{font-size:11px;color:#6b7280;margin-top:20px;text-align:center;line-height:1.5}
  a{color:#5eead4;text-decoration:none} a:hover{text-decoration:underline}
</style></head>
<body><div class="card">
  <span class="tool" id="tool-tag"></span>
  <h1>Get on the list</h1>
  <p class="sub">Drop your email and we'll notify you the moment your free trial
     window opens — plus send the answer to your last request when limits reset
     each day. No password, no spam.</p>
  <form id="f">
    <label for="email">Your email</label>
    <input type="email" name="email" id="email" required placeholder="you@company.com">
    <button type="submit" id="submit-btn">Notify me</button>
  </form>
  <div id="status"></div>
  <p class="small">
    Or skip the wait — <a href="https://dchub.cloud/pricing?utm_source=notify_skip">
    upgrade to Pro</a> for instant access.<br>
    Already a member? <a href="https://dchub.cloud/login">Sign in</a>.
  </p>
</div>
<script>
(function(){
  var p = new URLSearchParams(location.search);
  var tool = p.get('tool') || 'DC Hub Pro tools';
  var sid  = p.get('sid')  || '';
  document.getElementById('tool-tag').textContent = tool.replace(/_/g,' ').replace(/^get /,'');
  document.getElementById('f').addEventListener('submit', function(e){
    e.preventDefault();
    var btn = document.getElementById('submit-btn');
    var st  = document.getElementById('status');
    var em  = document.getElementById('email').value.trim();
    if (!em) return;
    btn.disabled = true; btn.textContent = 'Sending...'; st.textContent = '';
    fetch('/api/v1/notify-when-free', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email: em, tool: tool, session_id: sid, source:'paywall_cta'})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d && d.ok) {
        st.className = 'ok'; st.textContent = "You're on the list. Check your inbox.";
        document.getElementById('f').style.display = 'none';
      } else {
        st.className = 'err'; st.textContent = (d && d.error) || 'Something went wrong. Try again.';
        btn.disabled = false; btn.textContent = 'Notify me';
      }
    }).catch(function(){
      st.className = 'err'; st.textContent = 'Network error. Try again.';
      btn.disabled = false; btn.textContent = 'Notify me';
    });
  });
})();
</script>
</body></html>"""


@email_capture_bp.route("/notify", methods=["GET"])
def notify_landing():
    """Option A: lightweight email-capture landing for paywalled MCP users.
    Renders standalone HTML so it loads instantly without the main app shell."""
    return Response(_NOTIFY_LANDING_HTML, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


@email_capture_bp.route("/api/v1/notify-when-free", methods=["POST"])
def notify_when_free():
    """AJAX submit for the /notify landing page."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    tool  = (data.get("tool") or "").strip()
    sid   = (data.get("session_id") or "").strip()
    src   = (data.get("source") or "paywall_cta").strip()

    if not _valid_email(email):
        return jsonify(ok=False, error="invalid_email"), 400

    rid = _record_capture(email, src, session_id=sid, tool=tool)
    if not rid:
        return jsonify(ok=False, error="capture_failed"), 500

    # Trigger a welcome email so they know we got it. Phase FF+16-v3
    # (2026-05-19): previously this was wrapped in bare except:pass which
    # silently swallowed all errors. User signed up, never got an email,
    # we had no way to know why. Now we surface the actual send result
    # in the JSON response AND log so Railway logs catch the failure mode.
    email_result = None
    try:
        from email_service import send_email
        email_result = send_email(
            email,
            "You're on the DC Hub list",
            f"<p>Thanks — you're on the list for <strong>{tool or 'DC Hub Pro'}</strong>. "
            f"We'll notify you the moment your daily limit resets or your trial window opens.</p>"
            f"<p>Want instant access? "
            f"<a href='https://dchub.cloud/pricing?utm_source=notify_welcome'>"
            f"Upgrade to Pro</a> — $49/mo, cancel anytime.</p>",
            text_content=(f"Thanks — you're on the list for {tool or 'DC Hub Pro'}. "
                          f"We'll notify you the moment your daily limit resets.\n\n"
                          f"Want instant access? https://dchub.cloud/pricing?utm_source=notify_welcome")
        )
        import logging as _lg
        _lg.getLogger(__name__).info(
            "email_capture welcome send: to=%s success=%s err=%s",
            email, (email_result or {}).get("success"),
            (email_result or {}).get("error", "")[:200]
        )
    except Exception as e:
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "email_capture welcome send EXCEPTION: to=%s err=%s", email, str(e)[:200])
        email_result = {"success": False, "error": f"exception: {str(e)[:200]}"}

    return jsonify(
        ok=True,
        captured_id=rid,
        source=src,
        welcome_email_sent=bool((email_result or {}).get("success")),
        welcome_email_error=(email_result or {}).get("error"),
    )


# ═══════════════════════════════════════════════════════════════════════
#  OPTION B — auto-trial with email
# ═══════════════════════════════════════════════════════════════════════

@email_capture_bp.route("/api/v1/auto-trial/with-email", methods=["POST"])
def auto_trial_with_email():
    """Option B: mint an auto-trial key bound to a captured email.
    Extends the existing routes/auto_trial.py flow — if that flow
    minted a key already, we tie it to the email; otherwise we mint
    fresh."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    tool  = (data.get("tool") or "").strip()
    client_name = (data.get("client_name") or "auto-trial-email").strip()[:80]
    sid   = (data.get("session_id") or "").strip()

    if not _valid_email(email):
        return jsonify(ok=False, error="invalid_email"), 400

    _record_capture(email, "auto_trial", session_id=sid, tool=tool)

    # Try to mint via existing flow
    try:
        from routes.auto_trial import mint_trial_for_request
        result = mint_trial_for_request(request, tool, client_name=client_name)
        if result and result.get("ok"):
            # Bind email to the minted key
            api_key = result.get("api_key")
            if api_key:
                conn = _db()
                if conn:
                    try:
                        with conn.cursor() as cur:
                            # Update api_keys.email if the column exists,
                            # otherwise skip silently
                            try:
                                import hashlib
                                kh = hashlib.sha256(api_key.encode()).hexdigest()[:32]
                                cur.execute(
                                    "UPDATE api_keys SET email = %s "
                                    "WHERE key_hash = %s OR key = %s",
                                    (email.lower(), kh, api_key))
                                conn.commit()
                            except Exception:
                                try: conn.rollback()
                                except Exception: pass
                    finally:
                        try: conn.close()
                        except Exception: pass

            return jsonify(
                ok=True,
                api_key=api_key,
                expires_at=result.get("expires_at"),
                daily_calls=result.get("daily_calls", 200),
                email=email.lower(),
                source="auto_trial_with_email",
                message=("Trial key minted + tied to your email. "
                         "Use as X-API-Key header. We'll email you "
                         "before it expires."),
            )
    except Exception as e:
        return jsonify(ok=False, error=f"mint_failed: {str(e)[:120]}"), 500

    return jsonify(ok=False, error="mint_unavailable"), 503


# ═══════════════════════════════════════════════════════════════════════
#  OPTION C — Stripe pre-checkout email capture
# ═══════════════════════════════════════════════════════════════════════

_STRIPE_LINKS = {
    "developer": "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",
    "pro":       "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07",
    "starter":   "https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g",
    "enterprise":"https://buy.stripe.com/fZueVe5sS6Vv7CB41QaZi0a",
}
for _name in list(_STRIPE_LINKS):
    _env = os.environ.get(f"DCHUB_STRIPE_{_name.upper()}_LINK")
    if _env: _STRIPE_LINKS[_name] = _env


_CHECKOUT_START_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Upgrade — DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;
       background:#0a0e14;color:#e6e6e6;margin:0;
       min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
  .card{background:#161616;border:1px solid #262626;border-radius:14px;
        padding:36px 32px;max-width:480px;width:100%;
        box-shadow:0 16px 48px rgba(0,0,0,0.5)}
  h1{margin:0 0 6px;font-size:24px;font-weight:700;letter-spacing:-0.5px}
  .price{font-size:14px;color:#9ca3af;margin:0 0 22px;
         font-family:'JetBrains Mono',monospace}
  .tier{display:inline-block;background:linear-gradient(135deg,#10b981,#06b6d4);
        color:#0a0e14;padding:3px 10px;border-radius:5px;font-size:11px;
        font-weight:800;letter-spacing:0.4px;text-transform:uppercase;
        margin-bottom:14px}
  label{display:block;font-size:12px;color:#9ca3af;margin-bottom:6px;
        font-weight:600;margin-top:14px}
  input{width:100%;padding:14px;background:#0a0e14;border:1px solid #262626;
        border-radius:8px;color:#e6e6e6;font-size:15px;box-sizing:border-box;
        outline:none;font-family:inherit;transition:border-color .15s}
  input:focus{border-color:#5eead4}
  button{width:100%;padding:14px;margin-top:18px;background:#10b981;
         border:none;border-radius:8px;color:#0a0e14;font-weight:700;
         font-size:15px;cursor:pointer;font-family:inherit;transition:filter .15s}
  button:hover{filter:brightness(1.1)}
  button:disabled{opacity:0.5;cursor:not-allowed}
  .err{color:#ef4444;font-weight:600;margin-top:14px;text-align:center;font-size:13px}
  .small{font-size:11px;color:#6b7280;margin-top:20px;text-align:center;line-height:1.5}
  a{color:#5eead4;text-decoration:none} a:hover{text-decoration:underline}
  ul{margin:12px 0 0;padding:0 0 0 18px;font-size:13px;color:#9ca3af}
  ul li{margin-bottom:6px}
</style></head>
<body><div class="card">
  <span class="tier" id="tier-tag">Developer</span>
  <h1>Almost there.</h1>
  <p class="price" id="price-tag">$49/mo · cancel anytime</p>
  <ul>
    <li>1,000 MCP calls/day across all 24 tools</li>
    <li>Full grid &amp; fiber intelligence (no preview limits)</li>
    <li>Email when your tool quota resets</li>
  </ul>
  <form id="f">
    <label for="email">Your email (for receipt + your DC Hub account)</label>
    <input type="email" name="email" id="email" required placeholder="you@company.com">
    <button type="submit" id="submit-btn">Continue to checkout →</button>
  </form>
  <div id="status"></div>
  <p class="small">
    Stripe handles payment. Your email lets us auto-upgrade your API key
    the moment payment clears. <a href="https://dchub.cloud/pricing">See all plans</a>.
  </p>
</div>
<script>
(function(){
  var p = new URLSearchParams(location.search);
  var tier = (p.get('tier') || 'developer').toLowerCase();
  var key  = p.get('key')  || '';
  var tool = p.get('tool') || '';
  var sid  = p.get('sid')  || '';
  var ref  = p.get('client_reference_id') || '';
  var pricing = {developer:'$49/mo', pro:'$199/mo', starter:'$9/mo', enterprise:'custom'};
  document.getElementById('tier-tag').textContent = tier.charAt(0).toUpperCase()+tier.slice(1);
  document.getElementById('price-tag').textContent = (pricing[tier]||'$49/mo')+' · cancel anytime';
  document.getElementById('f').addEventListener('submit', function(e){
    e.preventDefault();
    var btn = document.getElementById('submit-btn');
    var st  = document.getElementById('status');
    var em  = document.getElementById('email').value.trim();
    if (!em) return;
    btn.disabled = true; btn.textContent = 'Redirecting...';
    fetch('/checkout/initiate', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email: em, tier: tier, tool: tool, key: key,
                            session_id: sid, client_reference_id: ref})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d && d.checkout_url) { location.href = d.checkout_url; }
      else {
        st.className = 'err';
        st.textContent = (d && d.error) || 'Could not start checkout. Try again.';
        btn.disabled = false; btn.textContent = 'Continue to checkout →';
      }
    }).catch(function(){
      st.className = 'err'; st.textContent = 'Network error. Try again.';
      btn.disabled = false; btn.textContent = 'Continue to checkout →';
    });
  });
})();
</script>
</body></html>"""


@email_capture_bp.route("/checkout/start", methods=["GET"])
def checkout_start():
    """Option C: HTML landing that collects email BEFORE the Stripe redirect.
    Even users who abandon at this step become an addressable email."""
    return Response(_CHECKOUT_START_HTML, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@email_capture_bp.route("/checkout/initiate", methods=["POST"])
def checkout_initiate():
    """Capture email + redirect to Stripe Payment Link with client_reference_id
    + customer_email pre-filled. Even if the user abandons at Stripe, we
    already captured their email here."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    tier  = (data.get("tier") or "developer").strip().lower()
    tool  = (data.get("tool") or "").strip()
    key   = (data.get("key") or "").strip()
    sid   = (data.get("session_id") or "").strip()
    ref   = (data.get("client_reference_id") or "").strip()

    if not _valid_email(email):
        return jsonify(ok=False, error="invalid_email"), 400

    _record_capture(email, "checkout_start",
                    session_id=sid, tool=tool,
                    api_key_hint=key[:8] if key else None)

    base = _STRIPE_LINKS.get(tier) or _STRIPE_LINKS["developer"]

    # If caller didn't supply a client_reference_id, mint a pair-code
    # from their api_key (if any) so the webhook can attribute on success
    if not ref and key:
        try:
            from routes.pair_code import get_or_create_code
            r = get_or_create_code(key, tool_name=tool)
            if r and r.get("code"): ref = r["code"]
        except Exception: pass

    # r33-identity (2026-05-31): last-resort, thread the MCP session_id into
    # client_reference_id when we still have no ref (the common anonymous
    # case — no api_key, no pre-minted code). The upgrade signal row carries
    # this same session_id, so a Stripe conversion webhook can match the
    # payment back to the signal on session_id. Closes the ~100%-NULL
    # user_email gap on the conversion join, since session_id is populated.
    if not ref and sid:
        ref = sid

    # Build the Stripe Payment Link URL with customer_email + client_reference_id
    from urllib.parse import urlencode
    params = {
        "prefilled_email": email,
        "utm_source":     "checkout_start",
        "utm_tool":       tool or "unknown",
    }
    if ref: params["client_reference_id"] = ref
    sep = "&" if "?" in base else "?"
    checkout_url = base + sep + urlencode(params)

    return jsonify(ok=True, checkout_url=checkout_url,
                   tier=tier, email_captured=True)


# ═══════════════════════════════════════════════════════════════════════
#  Backfill — retroactively populate mcp_upgrade_signals.user_email
# ═══════════════════════════════════════════════════════════════════════

@email_capture_bp.route("/api/v1/admin/email-capture/backfill-signals",
                          methods=["POST"])
def backfill_signals():
    """For each mcp_email_capture row with a session_id, find any
    mcp_upgrade_signals rows with that same session_id (and NULL email)
    and populate user_email. This recovers some of the 99.98% of signals
    that came in anonymous but whose user later left an email at the
    /notify or /checkout/start pages."""
    sent = (request.headers.get("X-Internal-Key") or "").strip()
    allowed = {"dchub-internal-sync-2026"}
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v: allowed.add(v)
    if sent not in allowed:
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403

    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 500
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mcp_upgrade_signals s
                SET user_email = ec.email
                FROM mcp_email_capture ec
                WHERE s.session_id = ec.session_id
                  AND s.session_id IS NOT NULL AND s.session_id <> ''
                  AND (s.user_email IS NULL OR s.user_email = '')
                  AND ec.email IS NOT NULL
            """)
            updated = cur.rowcount or 0
            conn.commit()
        return jsonify(ok=True, signals_backfilled=updated,
                       as_of=datetime.now(timezone.utc).isoformat())
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(error=f"backfill_failed: {str(e)[:200]}"), 500
    finally:
        try: conn.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════
#  Stats
# ═══════════════════════════════════════════════════════════════════════

# ─── Diagnostic ───────────────────────────────────────────────────────
# Phase FF+16-v3 (2026-05-19) — user reported signing up at /notify
# but receiving no email. Two bugs were hiding the cause:
#   1. notify_when_free swallowed all send errors silently (fixed)
#   2. lost_conversion_outreach checked result.get('ok') but
#      email_service.send_email returns result.get('success') (fixed)
# This endpoint surfaces the SMTP config + does a test send so we can
# tell at-a-glance whether O365 SMTP is configured at all.
@email_capture_bp.route("/api/v1/email-capture/test-send", methods=["GET"])
def test_send():
    """Diagnose email delivery. ?to=you@example.com (defaults to admin)."""
    sent = (request.headers.get("X-Internal-Key") or
            request.args.get("admin_key") or "").strip()
    allowed = {"dchub-internal-sync-2026"}
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v: allowed.add(v)
    if sent not in allowed:
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403

    to_email = (request.args.get("to") or "").strip()
    if not to_email or not _valid_email(to_email):
        return jsonify(error="invalid_to_email",
                       hint="?to=you@example.com required"), 400

    # Surface SMTP config so we can see what's missing
    config = {
        "SMTP_USER_set":     bool(os.environ.get("SMTP_USER")),
        "SMTP_PASSWORD_set": bool(os.environ.get("SMTP_PASSWORD")),
        "SMTP_FROM_EMAIL":   os.environ.get("SMTP_FROM_EMAIL", "(default)"),
        "SMTP_FROM_NAME":    os.environ.get("SMTP_FROM_NAME",  "(default)"),
        "SMTP_HOST":         os.environ.get("SMTP_HOST",       "(default)"),
        "SMTP_PORT":         os.environ.get("SMTP_PORT",       "(default)"),
    }

    try:
        from email_service import send_email
    except Exception as e:
        return jsonify(error="email_service_import_failed",
                       detail=str(e)[:300], config=config), 500

    result = send_email(
        to_email,
        "DC Hub email diagnostic — test send",
        ("<p>This is a diagnostic test from <code>/api/v1/email-capture/test-send</code>. "
         "If you got this, SMTP is configured correctly and Phase FF+16 email "
         "capture welcome emails should also work.</p>"),
        text_content=("DC Hub email diagnostic. If you got this, SMTP works and "
                      "Phase FF+16 welcome emails should also fire."),
    )
    return jsonify(
        ok=bool(result.get("success")),
        smtp_result=result,
        smtp_config=config,
        recipient=to_email,
    )


@email_capture_bp.route("/api/v1/email-capture/stats", methods=["GET"])
def stats():
    """How is email capture going? Public read-only."""
    conn = _db()
    if conn is None: return jsonify(error="no_database"), 500
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT LOWER(email)) FROM mcp_email_capture")
            total = int((cur.fetchone() or (0,))[0] or 0)
            cur.execute("""
                SELECT COUNT(DISTINCT LOWER(email))
                FROM mcp_email_capture
                WHERE created_at > NOW() - INTERVAL '7 days'
            """)
            week = int((cur.fetchone() or (0,))[0] or 0)
            cur.execute("""
                SELECT source, COUNT(DISTINCT LOWER(email)) AS n
                FROM mcp_email_capture
                WHERE created_at > NOW() - INTERVAL '30 days'
                GROUP BY source ORDER BY n DESC
            """)
            by_src = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            cur.execute("""
                SELECT COUNT(DISTINCT LOWER(email))
                FROM mcp_email_capture
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """)
            day = int((cur.fetchone() or (0,))[0] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM mcp_upgrade_signals
                WHERE created_at > NOW() - INTERVAL '7 days'
            """)
            sig_7d = int((cur.fetchone() or (0,))[0] or 0)
        capture_rate_7d = (100.0 * week / sig_7d) if sig_7d > 0 else 0.0
        return jsonify(
            ok=True,
            as_of=datetime.now(timezone.utc).isoformat(),
            total_unique_emails=total,
            unique_emails_24h=day,
            unique_emails_7d=week,
            by_source_30d=by_src,
            mcp_signals_7d=sig_7d,
            capture_rate_7d_pct=round(capture_rate_7d, 2),
            target_capture_rate_pct=20.0,
        )
    except Exception as e:
        return jsonify(error=f"stats_failed: {str(e)[:200]}"), 500
    finally:
        try: conn.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════
#  Public helper for the two paywall builders
# ═══════════════════════════════════════════════════════════════════════

def build_email_capture_urls(tool: str, api_key: str | None = None,
                              session_id: str | None = None,
                              tier: str = "developer",
                              client_reference_id: str | None = None) -> dict:
    """Helper used by mcp_gatekeeper + utils/paywall_response to inject
    consistent email-capture URLs into paywall payloads. Returns:
      {
        notify_url: "/notify?tool=X&sid=Y"        — Option A
        auto_trial_with_email_url: ...            — Option B
        checkout_start_url: "/checkout/start?tier=&tool=&key=&sid=..."  — Option C
      }
    All URLs return immediately if hit; the email capture happens server-side.
    """
    from urllib.parse import urlencode
    base = "https://dchub.cloud"
    notify_params = {}
    if tool: notify_params["tool"] = tool
    if session_id: notify_params["sid"] = session_id
    chk_params = {"tier": (tier or "developer").lower()}
    if tool: chk_params["tool"] = tool
    if api_key: chk_params["key"] = api_key
    if session_id: chk_params["sid"] = session_id
    if client_reference_id: chk_params["client_reference_id"] = client_reference_id
    return {
        "notify_url":               f"{base}/notify?{urlencode(notify_params)}",
        "auto_trial_with_email_url": f"{base}/api/v1/auto-trial/with-email",
        "checkout_start_url":       f"{base}/checkout/start?{urlencode(chk_params)}",
    }
