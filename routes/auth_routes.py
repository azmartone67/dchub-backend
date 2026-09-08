"""
DC Hub Auth Routes Blueprint (Phase 2 Extract 6)
==================================================
12 routes + 8 helper functions:
  Auth Helpers: hash_password, verify_password, generate_jwt, decode_jwt,
                require_auth, optional_auth
  Auth Routes (7): register, login, google/redirect, google/callback,
                   google (POST), me, update
  User Dashboard (3): GET dashboard, POST dashboard, GET api-keys
  Password Reset (2): forgot-password, reset-password
  Password Helpers: send_password_reset_email, send_admin_alert_email

Extracted from main.py lines 4137-4963, 5345-5444, 5842-5994
"""

import os
import re
import json
import secrets
import hashlib
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, make_response
from routes._swallowed_writes import note_swallowed_write
from utc_clock import utc_iso_z

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)

# Late-binding injected dependencies
_get_db = None
_get_db_connection = None
_pg_connection = None
_rate_limit = None
_JWT_SECRET = 'dchub-super-secret-key-change-in-production'
_JWT_EXPIRY_HOURS = 24 * 7
_GOOGLE_CLIENT_ID = ''
_GOOGLE_CLIENT_SECRET = ''

try:
    import jwt as _jwt_module
except ImportError:
    _jwt_module = None


def init_auth_routes(get_db, get_db_connection, pg_connection, rate_limit,
                     JWT_SECRET, JWT_EXPIRY_HOURS, GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET=''):
    """Late-bind dependencies from main.py."""
    global _get_db, _get_db_connection, _pg_connection, _rate_limit
    global _JWT_SECRET, _JWT_EXPIRY_HOURS, _GOOGLE_CLIENT_ID, _GOOGLE_CLIENT_SECRET
    _get_db = get_db
    _get_db_connection = get_db_connection
    _pg_connection = pg_connection
    _rate_limit = rate_limit
    _JWT_SECRET = JWT_SECRET
    _JWT_EXPIRY_HOURS = JWT_EXPIRY_HOURS
    _GOOGLE_CLIENT_ID = GOOGLE_CLIENT_ID
    _GOOGLE_CLIENT_SECRET = GOOGLE_CLIENT_SECRET


# =============================================================================
# AUTHENTICATION HELPERS (exported for use by other modules)
# =============================================================================

def hash_password(password):
    """Hash password with salt (10k iterations for fast response on autoscale)"""
    salt = secrets.token_hex(16)
    hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 10000)
    return f"{salt}:{hash_obj.hex()}"

def verify_password(password, hash_string):
    """Verify password against hash (tries 10k then 100k iterations for backward compat)"""
    try:
        salt, hash_hex = hash_string.split(':')
        hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 10000)
        if hash_obj.hex() == hash_hex:
            return True
        hash_obj_legacy = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hash_obj_legacy.hex() == hash_hex
    except:
        return False

def generate_jwt(user_id, email, role='user', plan='free'):
    """Generate JWT token"""
    payload = {
        'user_id': user_id,
        'email': email,
        'role': role,
        'plan': plan,
        'exp': datetime.utcnow() + timedelta(hours=_JWT_EXPIRY_HOURS),
        'iat': datetime.utcnow()
    }
    return _jwt_module.encode(payload, _JWT_SECRET, algorithm='HS256')

def decode_jwt(token):
    """Decode and verify JWT token"""
    try:
        payload = _jwt_module.decode(token, _JWT_SECRET, algorithms=['HS256'])
        return payload
    except:
        return None

def _bearer_or_cookie_token():
    """JWT from Authorization: Bearer header OR the login cookie.

    2026-06-12: require_auth previously read ONLY the Bearer header, so a
    user logged in via the `dchub_token` cookie (the access gate sets it on
    login — see set_cookie calls below) got a 401 on /api/auth/me and every
    other @require_auth endpoint even though the nav showed them logged in
    (the /integrations console error). The cookie is samesite='Lax', so
    honoring it here is CSRF-safe for state-changing requests (Lax suppresses
    the cookie on cross-site POST/PUT). Mirrors the cookie-aware tier checks
    used elsewhere in the backend.
    """
    auth_header = request.headers.get('Authorization') or ''
    if auth_header.startswith('Bearer '):
        tok = auth_header.split(' ', 1)[1].strip()
        if tok:
            return tok
    return (request.cookies.get('dchub_token')
            or request.cookies.get('auth_token')
            or request.cookies.get('token') or '')


def require_auth(f):
    """Decorator to require JWT authentication (Bearer header OR login cookie)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _bearer_or_cookie_token()
        if not token:
            return jsonify({'error': 'Authorization required', 'code': 'AUTH_REQUIRED'}), 401
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid or expired token', 'code': 'AUTH_INVALID'}), 401
        request.user = payload
        return f(*args, **kwargs)
    return decorated

def optional_auth(f):
    """Decorator for optional JWT authentication (Bearer header OR login cookie)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        request.user = None
        token = _bearer_or_cookie_token()
        if token:
            payload = decode_jwt(token)
            if payload:
                request.user = payload
        return f(*args, **kwargs)
    return decorated


# =============================================================================
# PASSWORD RESET HELPERS
# =============================================================================

def send_password_reset_email(email, name, reset_url):
    """Send a password-reset email (background thread).

    r-onboarding-fix (2026-07-03): reset delivery was silently broken for a class
    of customers. Two root causes, both fixed here:
      1. It sent from an UNVERIFIED sender (info@dchub.cloud). Every PROVEN Resend
         delivery in prod uses alerts@dchub.cloud (see _resend_email default +
         the welcome path), so resets could be silently dropped while welcomes
         succeeded. Now sends from the proven identity.
      2. Resend (the transport that actually works) was buried INSIDE the
         `if not SENDGRID_API_KEY: return` guard and only reachable after a
         SendGrid attempt. Now Resend is tried FIRST and SendGrid is an optional
         secondary — so a missing/blocked SendGrid can never skip the reset.
    Plus fail-loud telemetry: if BOTH transports fail we alert the admin with the
    reset URL instead of returning a neutral silent success (defect #10)."""
    def _do_send():
        subject = "Reset Your DC Hub Password"
        html = f"""
                    <div style="font-family: system-ui; max-width: 600px; margin: 0 auto;">
                        <h2 style="color: #2563eb;">DC Hub Password Reset</h2>
                        <p>Hi {name},</p>
                        <p>We received a request to reset your password. Click the button below to set a new password:</p>
                        <p style="text-align: center; margin: 30px 0;">
                            <a href="{reset_url}" style="background: #2563eb; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Reset Password</a>
                        </p>
                        <p style="color: #666; font-size: 14px;">This link is valid for 72 hours. If you didn't request this, you can safely ignore this email.</p>
                        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
                        <p style="color: #999; font-size: 12px;">DC Hub — Data Center Market Intelligence</p>
                    </div>
                    """
        sent_via = None
        # 1) Resend FIRST — proven default sender (alerts@dchub.cloud). Lazy import:
        #    main imports routes, so a top-level import would be circular.
        try:
            from main import _resend_email
            if _resend_email(email, subject, html):
                sent_via = "resend"
        except Exception as _re:
            print(f"⚠️ Resend reset send error for {email}: {str(_re)[:120]}")
        # 2) SendGrid as OPTIONAL secondary (only if Resend didn't land)
        if not sent_via:
            try:
                sg_key = os.environ.get('SENDGRID_API_KEY', '')
                if sg_key:
                    import urllib.request, json as _json
                    payload = {
                        "personalizations": [{"to": [{"email": email}]}],
                        "from": {"email": "alerts@dchub.cloud", "name": "DC Hub"},
                        "subject": subject,
                        "content": [{"type": "text/html", "value": html}],
                    }
                    req = urllib.request.Request(
                        'https://api.sendgrid.com/v3/mail/send',
                        data=_json.dumps(payload).encode('utf-8'),
                        headers={'Authorization': f'Bearer {sg_key}',
                                 'Content-Type': 'application/json'},
                        method='POST')
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if 200 <= int(getattr(resp, 'status', 0) or 0) < 300:
                            sent_via = "sendgrid"
            except Exception as _sge:
                print(f"⚠️ SendGrid reset send failed for {email}: {str(_sge)[:120]}")
        # 3) Fail-loud telemetry — never silently strand a customer.
        if sent_via:
            print(f"✅ Password reset email sent to {email} via {sent_via}")
        else:
            print(f"❌ Password reset email FAILED (both transports) for {email}")
            try:
                send_admin_alert_email(
                    f"🚨 Password reset email FAILED for {email}",
                    f"<p>Both Resend and SendGrid failed to deliver a reset link to "
                    f"<b>{email}</b>.</p><p>Reset URL (deliver manually if needed): "
                    f"<a href='{reset_url}'>{reset_url}</a></p>")
            except Exception as _ae:
                print(f"⚠️ Admin alert for failed reset ALSO failed: {str(_ae)[:120]}")

    threading.Thread(target=_do_send, daemon=True).start()


def send_admin_alert_email(subject, body_text):
    """Send admin alert email (non-blocking, background thread)"""
    def _do_send():
        try:
            sg_key = os.environ.get('SENDGRID_API_KEY', '')
            admin_email = os.environ.get('ADMIN_ALERT_EMAIL', 'jaz@dchub.cloud')
            if not sg_key:
                return
            import urllib.request, urllib.error, json as _json
            payload = {
                "personalizations": [{"to": [{"email": admin_email}]}],
                "from": {"email": "info@dchub.cloud", "name": "DC Hub Alerts"},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body_text}]
            }
            req = urllib.request.Request(
                'https://api.sendgrid.com/v3/mail/send',
                data=_json.dumps(payload).encode('utf-8'),
                headers={
                    'Authorization': f'Bearer {sg_key}',
                    'Content-Type': 'application/json'
                },
                method='POST'
            )
            try:
                response = urllib.request.urlopen(req, timeout=5)
                _ok = 200 <= int(getattr(response, 'status', 0) or 0) < 300
                print(f"🚨 Admin alert sent: {subject} (status: {response.status})")
            except Exception as _sge:
                # r-resend-port (2026-06-16): SendGrid out of credits → fall
                # through to Resend so admin recovery alerts still arrive.
                print(f"⚠️ SendGrid admin alert failed: {str(_sge)[:120]}")
                _ok = False
            if not _ok:
                # Lazy import to avoid circular import (main imports routes).
                from main import _resend_email
                # body_text is sent as text/plain to SendGrid but callers pass
                # HTML-ish content; <pre> wrapping keeps either form readable.
                _html = body_text if '<' in (body_text or '') else f"<pre>{body_text}</pre>"
                if _resend_email(admin_email, subject, _html,
                                 from_email="info@dchub.cloud", from_name="DC Hub Alerts"):
                    print(f"🚨 Admin alert sent via Resend fallback: {subject}")
                    return True
            return _ok
        except Exception as e:
            print(f"❌ Failed to send admin alert: {e}")
            return False

    threading.Thread(target=_do_send, daemon=True).start()


# =============================================================================
# AUTH ROUTES (7 routes)
# =============================================================================

# AUTO-REPAIR: duplicate route '/api/auth/register' also in api_server.py:611 — review and remove one
@auth_bp.route('/api/auth/register', methods=['POST'])
def register_user():
    """Register new user account"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    name = data.get('name', '')
    company = data.get('company', '')

    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    if not password or len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if pg_cur.fetchone():
                return jsonify({'error': 'Email already registered'}), 409

            user_id = secrets.token_hex(8)
            password_hash = hash_password(password)

            pg_cur.execute("""
                INSERT INTO users (id, email, password_hash, name, company, plan, role, created_at)
                VALUES (%s, %s, %s, %s, %s, 'free', 'user', %s) ON CONFLICT DO NOTHING
            """, (user_id, email, password_hash, name, company, utc_iso_z()))
            pg_conn.commit()
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

            token = generate_jwt(user_id, email, 'user', 'free')

            resp = make_response(jsonify({
                'success': True,
                'token': token,
                'user': {
                    'id': user_id,
                    'email': email,
                    'name': name,
                    'company': company,
                    'plan': 'free',
                    'role': 'user'
                }
            }), 201)
            resp.set_cookie('dchub_token', token, domain='.dchub.cloud',
                            httponly=False, secure=True, samesite='Lax',
                            max_age=30 * 24 * 60 * 60)  # 30 days (client hint; JWT is 7d)
            issue_refresh_cookie(resp, user_id)  # durable 90-day rotating refresh token
            return resp
    except Exception as e:
        # r43-H (2026-05-27): the SELECT-then-INSERT above isn't atomic, so
        # two simultaneous signups with the same email can both pass the
        # existence check and one hits the email UNIQUE constraint. Detect
        # that specific case and return the proper 409 instead of a
        # confusing generic 500 ("Registration failed") that makes the
        # customer think the service is broken.
        _msg = str(e).lower()
        if 'unique' in _msg or 'duplicate' in _msg or 'already exists' in _msg:
            return jsonify({'error': 'Email already registered'}), 409
        logger.error(f"Registration error: {e}")
        return jsonify({'error': 'Registration failed'}), 500

# AUTO-REPAIR: duplicate route '/api/auth/login' also in api_server.py:698 — review and remove one

@auth_bp.route('/api/auth/login', methods=['POST'])
def login_user():
    """Login user and return JWT token"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    email = data.get('email', '').lower().strip()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("""
                SELECT id, email, password_hash, name, company, plan, role
                FROM users WHERE email = %s
            """, (email,))
            user = pg_cur.fetchone()

            if not user:
                return jsonify({'error': 'Invalid credentials'}), 401

            user_id, user_email, pw_hash, name, company, plan, role = user

            # RFO Fix: Validate hash format before verify attempt
            if pw_hash and ':' not in pw_hash:
                logger.warning(f"HASH_FORMAT_MISMATCH: user {user_email} (id={user_id}) has non-standard password hash (len={len(pw_hash)}, prefix={pw_hash[:10]}). Expected salt:hash PBKDF2 format.")
                return jsonify({'error': 'Invalid credentials. Please reset your password at /forgot-password or contact support.'}), 401
            if not pw_hash or not verify_password(password, pw_hash):
                return jsonify({'error': 'Invalid credentials'}), 401

            token = generate_jwt(user_id, user_email, role or 'user', plan or 'free')

            # Update last login in background
            def _update_last_login_bg(uid):
                try:
                    with _pg_connection() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                                    (utc_iso_z(), uid))
                        conn.commit()
                except:
                    note_swallowed_write("users", where="auth_routes._update_last_login_bg")
                    pass
            threading.Thread(target=_update_last_login_bg, args=(user_id,), daemon=True).start()

            resp = make_response(jsonify({
                'success': True,
                'token': token,
                'user': {
                    'id': user_id,
                    'email': user_email,
                    'name': name or '',
                    'company': company or '',
                    'plan': plan or 'free',
                    'role': role or 'user'
                }
            }))
            # Set cross-subdomain cookie so dchub.cloud picks up auth from dashboard.dchub.cloud.
            resp.set_cookie(
                'dchub_token',
                token,
                domain='.dchub.cloud',
                httponly=False,   # JS must read this for access gate
                secure=True,
                samesite='Lax',
                max_age=30 * 24 * 60 * 60  # 30 days (client "logged in" hint; JWT itself is 7d)
            )
            issue_refresh_cookie(resp, user_id)  # durable 90-day rotating refresh token
            return resp
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500


# =============================================================================
# REFRESH TOKENS (2026-07-13) — durable session across access-JWT expiry
# =============================================================================
# The access JWT lives 7 days (JWT_EXPIRY_HOURS); the dchub_token cookie used to
# live 30 days, so a user returning between day 7 and day 30 carried a DEAD JWT —
# "logged in" client-side but 401/402 on every authed call (the Land & Power map
# 402 flood, 2026-07-13). A rotating, revocable refresh token (httpOnly, 90d,
# hashed at rest) lets the client silently re-mint a fresh JWT on boot, so a
# paying user who returns within 90 days never lapses and the gate never sees
# them as anonymous. Stateful → a stolen/rotated token can be revoked and reuse
# detected. See reference_dchub_map_session_expired_402.
_REFRESH_COOKIE = 'dchub_refresh'
_REFRESH_TTL_DAYS = 90
_refresh_table_ready = False


def _ensure_refresh_table():
    """Lazily create the refresh-token table on first use (NOT at boot — avoids
    the boot-DDL storm; a single CREATE IF NOT EXISTS is cheap)."""
    global _refresh_table_ready
    if _refresh_table_ready:
        return
    try:
        with _pg_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
                    token_hash  TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    issued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ NOT NULL,
                    last_used   TIMESTAMPTZ,
                    revoked_at  TIMESTAMPTZ,
                    ip_prefix   TEXT,
                    replaced_by TEXT
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_art_user ON auth_refresh_tokens(user_id)")
            conn.commit()
        _refresh_table_ready = True
    except Exception as e:
        logger.warning(f"auth_refresh_tokens init failed: {e}")


def _refresh_ip_prefix():
    ip = (request.headers.get('CF-Connecting-IP')
          or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr or '')
    if ':' in ip:
        return ':'.join(ip.split(':')[:4])
    p = ip.split('.')
    return '.'.join(p[:2]) if len(p) == 4 else ip[:16]


def _hash_refresh(raw):
    return hashlib.sha256((raw or '').encode()).hexdigest()


def issue_refresh_cookie(resp, user_id):
    """Mint a fresh refresh token, persist its hash, set the httpOnly cookie on
    `resp`. Best-effort — a failure here must never break the login response."""
    try:
        _ensure_refresh_table()
        raw = secrets.token_urlsafe(48)
        exp = datetime.utcnow() + timedelta(days=_REFRESH_TTL_DAYS)
        with _pg_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO auth_refresh_tokens (token_hash, user_id, expires_at, ip_prefix) "
                "VALUES (%s, %s, %s, %s)",
                (_hash_refresh(raw), str(user_id), exp, _refresh_ip_prefix()))
            conn.commit()
        resp.set_cookie(_REFRESH_COOKIE, raw, domain='.dchub.cloud',
                        httponly=True, secure=True, samesite='Lax',
                        max_age=_REFRESH_TTL_DAYS * 24 * 60 * 60, path='/')
    except Exception as e:
        logger.warning(f"issue_refresh_cookie failed for {user_id}: {e}")
    return resp


@auth_bp.route('/api/auth/refresh', methods=['POST'])
def refresh_token():
    """Exchange a valid refresh cookie for a fresh access JWT, rotating the
    refresh token. 401 if the cookie is absent/expired/revoked/reused. This is
    what lets a returning paid user re-authenticate silently instead of hitting
    the metered map gate as an 'anonymous' caller."""
    raw = request.cookies.get(_REFRESH_COOKIE, '')
    if not raw:
        return jsonify({'error': 'no_refresh_token'}), 401
    th = _hash_refresh(raw)
    try:
        _ensure_refresh_table()
        with _pg_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT user_id, (expires_at < NOW()) AS expired, "
                "       (revoked_at IS NOT NULL) AS revoked, "
                "       (replaced_by IS NOT NULL) AS replaced "
                "FROM auth_refresh_tokens WHERE token_hash = %s", (th,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'invalid_refresh_token'}), 401
            user_id, expired, revoked, replaced = row
            # Reuse detection: a token already rotated/revoked but presented again
            # → likely theft. Revoke the whole live chain for this user.
            if revoked or replaced:
                cur.execute("UPDATE auth_refresh_tokens SET revoked_at = NOW() "
                            "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
                conn.commit()
                logger.warning(f"refresh-token reuse detected for user {user_id} — chain revoked")
                return jsonify({'error': 'refresh_reused'}), 401
            if expired:
                return jsonify({'error': 'refresh_expired'}), 401
            cur.execute("SELECT id, email, name, company, plan, role FROM users WHERE id = %s", (user_id,))
            u = cur.fetchone()
            if not u:
                return jsonify({'error': 'user_not_found'}), 401
            uid, email, name, company, plan, role = u
            # Rotate: mint a new refresh token, mark the old one replaced+revoked.
            new_raw = secrets.token_urlsafe(48)
            new_th = _hash_refresh(new_raw)
            new_exp = datetime.utcnow() + timedelta(days=_REFRESH_TTL_DAYS)
            cur.execute("INSERT INTO auth_refresh_tokens (token_hash, user_id, expires_at, ip_prefix) "
                        "VALUES (%s, %s, %s, %s)", (new_th, str(uid), new_exp, _refresh_ip_prefix()))
            cur.execute("UPDATE auth_refresh_tokens SET revoked_at = NOW(), last_used = NOW(), replaced_by = %s "
                        "WHERE token_hash = %s", (new_th, th))
            conn.commit()
        access = generate_jwt(uid, email, role or 'user', plan or 'free')
        resp = make_response(jsonify({
            'success': True, 'token': access,
            'user': {'id': uid, 'email': email, 'name': name or '', 'company': company or '',
                     'plan': plan or 'free', 'role': role or 'user'}
        }))
        # Fresh access JWT (7d validity); cookie hint kept at 30d for parity with
        # login. Longevity/self-heal is the refresh cookie's job.
        resp.set_cookie('dchub_token', access, domain='.dchub.cloud', httponly=False,
                        secure=True, samesite='Lax', max_age=30 * 24 * 60 * 60)
        resp.set_cookie(_REFRESH_COOKIE, new_raw, domain='.dchub.cloud', httponly=True,
                        secure=True, samesite='Lax',
                        max_age=_REFRESH_TTL_DAYS * 24 * 60 * 60, path='/')
        return resp
    except Exception as e:
        logger.error(f"refresh_token error: {e}")
        return jsonify({'error': 'refresh_failed'}), 500


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout_revoke_refresh():
    """Revoke the presented refresh token and clear both auth cookies."""
    raw = request.cookies.get(_REFRESH_COOKIE, '')
    if raw:
        try:
            _ensure_refresh_table()
            with _pg_connection() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE auth_refresh_tokens SET revoked_at = NOW() "
                            "WHERE token_hash = %s AND revoked_at IS NULL", (_hash_refresh(raw),))
                conn.commit()
        except Exception as e:
            logger.warning(f"logout revoke failed: {e}")
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('dchub_token', '', domain='.dchub.cloud', expires=0, path='/')
    resp.set_cookie(_REFRESH_COOKIE, '', domain='.dchub.cloud', expires=0, path='/')
    return resp


@auth_bp.route('/api/auth/google/redirect', methods=['GET'])
def google_auth_redirect():
    """Redirect to Google OAuth consent screen"""
    if not _GOOGLE_CLIENT_ID:
        return jsonify({'error': 'Google OAuth not configured'}), 503

    redirect_uri = 'https://dchub.cloud/api/auth/google/callback'
    scope = 'openid email profile'

    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={_GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    from flask import redirect as flask_redirect
    return flask_redirect(auth_url)


@auth_bp.route('/api/auth/google/callback', methods=['GET'])
def google_auth_callback():
    """Handle Google OAuth callback — exchange code for token, create/login user"""
    import urllib.request, urllib.error, urllib.parse

    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return f"""<script>window.opener.postMessage({{type:'google-auth-error',error:'{error}'}},'*');window.close();</script>"""

    if not code:
        return f"""<script>window.opener.postMessage({{type:'google-auth-error',error:'no_code'}},'*');window.close();</script>"""

    try:
        # Exchange code for tokens
        token_data = urllib.parse.urlencode({
            'code': code,
            'client_id': _GOOGLE_CLIENT_ID,
            'client_secret': _GOOGLE_CLIENT_SECRET,
            'redirect_uri': 'https://dchub.cloud/api/auth/google/callback',
            'grant_type': 'authorization_code'
        }).encode()

        token_req = urllib.request.Request(
            'https://oauth2.googleapis.com/token',
            data=token_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_response = json.loads(resp.read().decode())

        access_token = token_response.get('access_token')
        if not access_token:
            raise Exception("No access token received")

        # Get user info
        userinfo_req = urllib.request.Request(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        with urllib.request.urlopen(userinfo_req, timeout=10) as resp:
            google_user = json.loads(resp.read().decode())

        email = google_user.get('email', '').lower()
        name = google_user.get('name', '')
        google_id = google_user.get('id', '')

        if not email:
            raise Exception("No email from Google")

        # Find or create user
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT id, email, name, company, plan, role FROM users WHERE email = %s", (email,))
            existing = pg_cur.fetchone()

            if existing:
                user_id = existing[0]
                user_plan = existing[4] or 'free'
                user_role = existing[5] or 'user'
                pg_cur.execute("UPDATE users SET last_login = %s, google_id = %s WHERE id = %s",
                               (utc_iso_z(), google_id, user_id))
            else:
                user_id = secrets.token_hex(8)
                user_plan = 'free'
                user_role = 'user'
                pg_cur.execute("""
                    INSERT INTO users (id, email, name, plan, role, google_id, created_at, last_login)
                    VALUES (%s, %s, %s, 'free', 'user', %s, %s, %s) ON CONFLICT DO NOTHING
                """, (user_id, email, name, google_id,
                      utc_iso_z(), utc_iso_z()))

            pg_conn.commit()
            # Send free welcome email — ONLY for a genuinely new account.
            # r-coldbuy (2026-08-08): this send sat outside the if/else, so
            # EVERY Google sign-in re-welcomed an existing user — including
            # paying ones. `existing` is the new-vs-returning discriminator
            # already computed above.
            if not existing:
                try:
                    from main import send_free_welcome_email_sendgrid
                    send_free_welcome_email_sendgrid(email, name)
                except Exception as email_err:
                    logger.warning(f"Free welcome email failed for {email}: {email_err}")

        jwt_token = generate_jwt(user_id, email, user_role, user_plan)

        _gcb_html = f"""<!DOCTYPE html><html><body><script>
        if (window.opener) {{
            window.opener.postMessage({{
                type: 'google-auth-success',
                token: '{jwt_token}',
                user: {{id:'{user_id}',email:'{email}',name:'{name}',plan:'{user_plan}',role:'{user_role}'}}
            }}, '*');
            window.close();
        }} else {{
            localStorage.setItem('dchub_token', '{jwt_token}');
            localStorage.setItem('dchub_session', JSON.stringify({{email:'{email}',name:'{name}',plan:'{user_plan}',role:'{user_role}'}}));
            localStorage.setItem('dchub_user', JSON.stringify({{id:'{user_id}',email:'{email}',name:'{name}',plan:'{user_plan}',role:'{user_role}'}}));
            window.location.href = '/dashboard.html';
        }}
        </script></body></html>"""
        _gcb_resp = make_response(_gcb_html)
        issue_refresh_cookie(_gcb_resp, user_id)  # durable 90-day rotating refresh token
        return _gcb_resp

    except Exception as e:
        logger.error(f"Google callback error: {e}")
        error_msg = str(e).replace("'", "\\'")
        return f"""<script>if(window.opener){{window.opener.postMessage({{type:'google-auth-error',error:'{error_msg}'}},'*');window.close();}}else{{window.location.href='/login.html?error='+encodeURIComponent('{error_msg}');}}</script>"""
# AUTO-REPAIR: duplicate route '/api/auth/google' also in api_server.py:742 — review and remove one


@auth_bp.route('/api/auth/google', methods=['POST'])
def google_auth():
    """Handle Google OAuth token from frontend (popup or redirect flow)"""
    data = request.get_json()
    token = data.get('token') or data.get('credential') or data.get('id_token') if data else None
    code = data.get('code') if data else None

    if not token and not code:
        return jsonify({'error': 'Token or code required'}), 400

    try:
        import urllib.request, urllib.error, urllib.parse

        google_user = None

        if code:
            # Exchange code for tokens
            token_data = urllib.parse.urlencode({
                'code': code,
                'client_id': _GOOGLE_CLIENT_ID,
                'client_secret': _GOOGLE_CLIENT_SECRET,
                'redirect_uri': 'https://dchub.cloud/api/auth/google/callback',
                'grant_type': 'authorization_code'
            }).encode()

            token_req = urllib.request.Request(
                'https://oauth2.googleapis.com/token',
                data=token_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            with urllib.request.urlopen(token_req, timeout=10) as resp:
                token_response = json.loads(resp.read().decode())
            token = token_response.get('access_token')

        if token:
            # Get user info from access token
            userinfo_req = urllib.request.Request(
                'https://www.googleapis.com/oauth2/v2/userinfo',
                headers={'Authorization': f'Bearer {token}'}
            )
            try:
                with urllib.request.urlopen(userinfo_req, timeout=10) as resp:
                    google_user = json.loads(resp.read().decode())
            except:
                # Try as ID token
                try:
                    verify_url = f'https://oauth2.googleapis.com/tokeninfo?id_token={token}'
                    with urllib.request.urlopen(verify_url, timeout=10) as resp:
                        google_user = json.loads(resp.read().decode())
                except Exception as e2:
                    return jsonify({'error': f'Token verification failed: {e2}'}), 401

        if not google_user or not google_user.get('email'):
            return jsonify({'error': 'Could not verify Google account'}), 401

        email = google_user['email'].lower()
        name = google_user.get('name', '')
        google_id = google_user.get('id') or google_user.get('sub', '')

        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT id, email, name, company, plan, role FROM users WHERE email = %s", (email,))
            existing = pg_cur.fetchone()

            if existing:
                user_id = existing[0]
                user_plan = existing[4] or 'free'
                user_role = existing[5] or 'user'
                user_name = existing[2] or name
                user_company = existing[3] or ''
                pg_cur.execute("UPDATE users SET last_login = %s, google_id = %s WHERE id = %s",
                               (utc_iso_z(), google_id, user_id))
            else:
                user_id = secrets.token_hex(8)
                user_plan = 'free'
                user_role = 'user'
                user_name = name
                user_company = ''
                pg_cur.execute("""
                    INSERT INTO users (id, email, name, plan, role, google_id, created_at, last_login)
                    VALUES (%s, %s, %s, 'free', 'user', %s, %s, %s) ON CONFLICT DO NOTHING
                """, (user_id, email, name, google_id,
                      utc_iso_z(), utc_iso_z()))

            pg_conn.commit()
            # Send free welcome email — ONLY for a genuinely new account.
            # r-coldbuy (2026-08-08): this send sat outside the if/else, so
            # EVERY Google sign-in re-welcomed an existing user — including
            # paying ones. `existing` is the new-vs-returning discriminator
            # already computed above.
            if not existing:
                try:
                    from main import send_free_welcome_email_sendgrid
                    send_free_welcome_email_sendgrid(email, name)
                except Exception as email_err:
                    logger.warning(f"Free welcome email failed for {email}: {email_err}")

        jwt_token = generate_jwt(user_id, email, user_role, user_plan)

        resp = make_response(jsonify({
            'success': True,
            'token': jwt_token,
            'user': {
                'id': user_id,
                'email': email,
                'name': user_name,
                'company': user_company,
                'plan': user_plan,
                'role': user_role
            }
        }))
        resp.set_cookie('dchub_token', jwt_token, domain='.dchub.cloud',
                        httponly=False, secure=True, samesite='Lax',
                        max_age=30 * 24 * 60 * 60)  # 30 days (client hint; JWT is 7d)
        issue_refresh_cookie(resp, user_id)  # durable 90-day rotating refresh token
        return resp
    except Exception as e:
        logger.error(f"Google auth error: {e}")
        import traceback; traceback.print_exc()
# AUTO-REPAIR: duplicate route '/api/auth/me' also in api_server.py:871 — review and remove one
        return jsonify({'error': 'Google authentication failed'}), 500


@auth_bp.route('/api/auth/me', methods=['GET'])
@require_auth
def get_current_user():
    """Get current user profile"""
    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("""
                SELECT id, email, name, company, plan, role, created_at, last_login, plan_updated_at
                FROM users WHERE id = %s
            """, (request.user['user_id'],))
            user = pg_cur.fetchone()

            if not user:
                return jsonify({'error': 'User not found'}), 404

            return jsonify({
                'success': True,
                'user': {
                    'id': user[0],
                    'email': user[1],
                    'name': user[2] or '',
                    'company': user[3] or '',
                    'plan': user[4] or 'free',
                    'role': user[5] or 'user',
                    'created_at': user[6],
                    'last_login': user[7],
                    'plan_updated_at': user[8]
                }
            })
    except Exception as e:
# AUTO-REPAIR: duplicate route '/api/auth/update' also in api_server.py:905 — review and remove one
        logger.error(f"Get user error: {e}")
        return jsonify({'error': 'Failed to retrieve user'}), 500


@auth_bp.route('/api/auth/update', methods=['PUT'])
@require_auth
def update_user():
    """Update user profile"""
    data = request.get_json()

    conn = _get_db()
    try:
        c = conn.cursor()

        updates = []
        params = []

        if 'name' in data:
            updates.append('name = %s')
            params.append(data['name'])
        if 'company' in data:
            updates.append('company = %s')
            params.append(data['company'])
        if 'preferences' in data:
            updates.append('preferences = %s')
            params.append(json.dumps(data['preferences']))
        if 'saved_searches' in data:
            updates.append('saved_searches = %s')
            params.append(json.dumps(data['saved_searches']))
        if 'saved_markets' in data:
            updates.append('saved_markets = %s')
            params.append(json.dumps(data['saved_markets']))

        if updates:
            params.append(request.user['user_id'])
            c.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()
    finally:
        # LEAK FIX: release the pooled conn on every path (was skipped on error)
        try: conn.close()
        except Exception: pass

    return jsonify({'success': True, 'message': 'Profile updated'})


# =============================================================================
# USER DASHBOARD ROUTES (3 routes)
# =============================================================================

@auth_bp.route('/api/user/dashboard', methods=['GET'])
def get_user_dashboard():
    """Get user dashboard data (searches, alerts, watchlist)"""
    user_id = request.args.get('userId')

    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    conn = _get_db_connection()
    try:
        c = conn.cursor()

        c.execute("""
            SELECT id, market, alert_type, enabled, created_at, last_triggered, trigger_count
            FROM user_alerts
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
        """, (user_id,))
        alerts_rows = c.fetchall()

        alerts = [{
            'id': f'alert_{row[0]}',
            'name': f'{row[1]} - {row[2]}',
            'condition': row[2],
            'market': row[1],
            'active': bool(row[3]),
            'triggered': row[5] is not None,
            'created': row[4]
        } for row in alerts_rows]
    finally:
        # LEAK FIX: release the conn on every path (was skipped on error)
        try: conn.close()
        except Exception: pass

    return jsonify({
        'success': True,
        'searches': [],
        'alerts': alerts,
        'watchlist': [],
        'stats': {
# AUTO-REPAIR: duplicate route '/api/user/dashboard' also in routes/auth_routes.py:949 — review and remove one
            'searches': 0,
            'alerts': len(alerts)
        }
    })

@auth_bp.route('/api/user/dashboard', methods=['POST'])
def save_user_dashboard():
    """Save user dashboard data"""
    data = request.get_json()
    user_id = data.get('userId')

    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    return jsonify({
        'success': True,
        'message': 'Dashboard data synced'
    })

@auth_bp.route('/api/user/api-keys', methods=['GET'])
@require_auth
def get_user_api_keys():
    """Get all API keys for the authenticated user"""
    conn = _get_db()
    try:
        c = conn.cursor()

        c.execute("""
            SELECT id, key_prefix, name, plan, rate_limit_tier, is_active,
                   created_at, usage_count, calls_today, calls_total
            FROM api_keys
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (request.user['user_id'],))

        rows = c.fetchall()
    finally:
        # LEAK FIX: release the pooled conn on every path (was skipped on error)
        try: conn.close()
        except Exception: pass

    keys = [{
        'id': row[0],
        'key_prefix': row[1],
        'name': row[2],
        'plan': row[3] or 'free',
        'rate_limit_tier': row[4] or 'free',
        'is_active': bool(row[5]),
        'created_at': row[6],
        'usage_count': row[7] or 0,
        'calls_today': row[8] or 0,
        'calls_total': row[9] or 0
    } for row in rows]

    return jsonify({
        'success': True,
        'keys': keys,
        'count': len(keys)
    })


@auth_bp.route('/api/user/mcp-connector', methods=['GET'])
@require_auth
def get_user_mcp_connector():
    """r-onboarding-fix (2026-07-03): expose the MCP (`dch_live_`) key + a
    ready-to-paste Claude connector URL for the authenticated user.

    Defect #3/#12: the dashboard's /api/user/api-keys reads ONLY the `api_keys`
    table (the `dchub_` REST keys). The key Claude actually needs lives in
    `mcp_dev_keys` — keyed by EMAIL, with no `user_id` column — so it was
    structurally unreachable on every self-serve surface. A paid customer could
    log in and still never find the one credential that connects Claude. This
    endpoint closes that gap; the dashboard 'Connect to Claude' card consumes it.
    """
    email = (getattr(request, 'user', None) or {}).get('email')
    if not email:
        return jsonify({'success': False, 'error': 'No email on session'}), 400
    conn = _get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT api_key, tier, status, created_at, last_used_at
            FROM mcp_dev_keys
            WHERE lower(email) = lower(%s) AND status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
        """, (email,))
        row = c.fetchone()
    finally:
        try: conn.close()
        except Exception: pass

    if not row:
        return jsonify({
            'success': True,
            'has_key': False,
            'message': ("No MCP key on file yet. You can connect anonymously "
                        "(3 calls/day) or claim a free key at dchub.cloud/mcp."),
        })

    api_key = row[0]
    connector_url = f"https://dchub.cloud/mcp?api_key={api_key}"
    return jsonify({
        'success': True,
        'has_key': True,
        'mcp_key': api_key,
        'tier': row[1],
        # Claude.ai web custom-connector has no header field → key goes IN the URL.
        'connector_url': connector_url,
        # Claude Desktop / Cursor / Cline support headers → use this instead.
        'header': f"X-API-Key: {api_key}",
        'last_used_at': row[4],
        'instructions': ("Claude.ai (web): paste connector_url as the custom-connector "
                         "URL — leave auth blank. Claude Desktop / Cursor / Cline: use the "
                         "X-API-Key header instead."),
    })


# =============================================================================
# PASSWORD RESET ROUTES (2 routes)
# =============================================================================

@auth_bp.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    """Send password reset email via SendGrid"""
    data = request.get_json()
    email = data.get('email', '').lower().strip() if data else ''

    if not email:
        return jsonify({'error': 'Email required'}), 400

    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT id, email, name FROM users WHERE email = %s", (email,))
            user_row = pg_cur.fetchone()

            if user_row:
                user_name = user_row[2] or email.split('@')[0]
                token = secrets.token_urlsafe(32)
                # r43-H (2026-05-27): bump TTL from 1h → 72h. Real customers
                # don't always click reset links within the hour — they're
                # at dinner, on vacation, traveling, on mobile checking email
                # the next morning. The Carl Braun incident showed our
                # 1-hour window stranded a paying Pro customer; 72h is the
                # OWASP-recommended ceiling for password reset tokens.
                expires_at = (datetime.utcnow() + timedelta(hours=72)).isoformat()

                pg_cur.execute("UPDATE password_reset_tokens SET used = TRUE WHERE user_email = %s AND used = FALSE", (email,))
                pg_cur.execute(
                    "INSERT INTO password_reset_tokens (user_email, token, expires_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (email, token, expires_at)
                )
                pg_conn.commit()

                reset_url = f"https://dchub.cloud/reset-password.html?token={token}"
                send_password_reset_email(email, user_name, reset_url)
    except Exception as e:
        print(f"❌ Forgot password error: {e}")
        import traceback
        traceback.print_exc()

    return jsonify({'success': True, 'message': 'If an account exists with that email, a reset link has been sent.'})


@auth_bp.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    """Reset password using token from email"""
    data = request.get_json()
    token = data.get('token', '') if data else ''
    new_password = data.get('password', '') if data else ''

    if not token or not new_password:
        return jsonify({'error': 'Token and new password required'}), 400

    if len(new_password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()

            pg_cur.execute(
                "SELECT user_email, expires_at FROM password_reset_tokens WHERE token = %s AND used = FALSE",
                (token,)
            )
            token_row = pg_cur.fetchone()

            if not token_row:
                return jsonify({'error': 'Invalid or expired reset link'}), 400

            expires_at = token_row[1]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)

            if datetime.utcnow() > expires_at:
                return jsonify({'error': 'Reset link has expired. Please request a new one.'}), 400

            email = token_row[0]
            password_hash = hash_password(new_password)

            pg_cur.execute("UPDATE users SET password_hash = %s WHERE email = %s", (password_hash, email))
            pg_cur.execute("UPDATE password_reset_tokens SET used = TRUE WHERE token = %s", (token,))
            pg_conn.commit()
            # r-coldbuy (2026-08-08): NO welcome email on password reset.
            # r43-H (2026-05-27) repaired a `send_free_welcome_email_sendgrid`
            # call here that had been silently NameErroring — which activated a
            # send that should never have existed. A reset is not a signup, and
            # the mail it sent announces a FREE account: a founding customer who
            # paid $49 and then reset their password to get in was told, four
            # minutes later, that their free account was active. The user still
            # gets the reset-confirmation response below; that is the correct
            # and sufficient signal.
            print(f"✅ Password reset successful for {email}")
            return jsonify({'success': True, 'message': 'Password has been reset. You can now log in.'})

    except Exception as e:
        print(f"❌ Reset password error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'An error occurred. Please try again.'}), 500
