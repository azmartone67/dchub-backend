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
from flask import Blueprint, request, jsonify

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

def generate_jwt(user_id, email, role='user'):
    """Generate JWT token"""
    payload = {
        'user_id': user_id,
        'email': email,
        'role': role,
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

def require_auth(f):
    """Decorator to require JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authorization required', 'code': 'AUTH_REQUIRED'}), 401
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid or expired token', 'code': 'AUTH_INVALID'}), 401
        request.user = payload
        return f(*args, **kwargs)
    return decorated

def optional_auth(f):
    """Decorator for optional JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        request.user = None
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            payload = decode_jwt(token)
            if payload:
                request.user = payload
        return f(*args, **kwargs)
    return decorated


# =============================================================================
# PASSWORD RESET HELPERS
# =============================================================================

def send_password_reset_email(email, name, reset_url):
    """Send password reset email via SendGrid (with 5s timeout, runs in background thread)"""
    def _do_send():
        try:
            sg_key = os.environ.get('SENDGRID_API_KEY', '')
            if not sg_key:
                print(f"⚠️ SENDGRID_API_KEY not set, skipping reset email for {email}")
                return
            import urllib.request, urllib.error, json as _json
            payload = {
                "personalizations": [{"to": [{"email": email}]}],
                "from": {"email": "noreply@dchub.cloud", "name": "DC Hub"},
                "subject": "Reset Your DC Hub Password",
                "content": [{
                    "type": "text/html",
                    "value": f"""
                    <div style="font-family: system-ui; max-width: 600px; margin: 0 auto;">
                        <h2 style="color: #2563eb;">DC Hub Password Reset</h2>
                        <p>Hi {name},</p>
                        <p>We received a request to reset your password. Click the button below to set a new password:</p>
                        <p style="text-align: center; margin: 30px 0;">
                            <a href="{reset_url}" style="background: #2563eb; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Reset Password</a>
                        </p>
                        <p style="color: #666; font-size: 14px;">This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
                        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
                        <p style="color: #999; font-size: 12px;">DC Hub — Data Center Market Intelligence</p>
                    </div>
                    """
                }]
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"✅ Password reset email sent to {email} (status: {resp.status})")
        except Exception as e:
            print(f"❌ Failed to send reset email to {email}: {e}")

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
                "from": {"email": "alerts@dchub.cloud", "name": "DC Hub Alerts"},
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
            response = urllib.request.urlopen(req, timeout=5)
            print(f"🚨 Admin alert sent: {subject} (status: {response.status})")
            return True
        except Exception as e:
            print(f"❌ Failed to send admin alert: {e}")
            return False

    threading.Thread(target=_do_send, daemon=True).start()


# =============================================================================
# AUTH ROUTES (7 routes)
# =============================================================================

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
                VALUES (%s, %s, %s, %s, %s, 'free', 'user', %s)
            """, (user_id, email, password_hash, name, company, datetime.utcnow().isoformat()))
            pg_conn.commit()
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

            token = generate_jwt(user_id, email, 'user')

            return jsonify({
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
            }), 201
    except Exception as e:
        logger.error(f"Registration error: {e}")
        return jsonify({'error': 'Registration failed'}), 500


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

            if not pw_hash or not verify_password(password, pw_hash):
                return jsonify({'error': 'Invalid credentials'}), 401

            token = generate_jwt(user_id, user_email, role or 'user')

            # Update last login in background
            def _update_last_login_bg(uid):
                try:
                    with _pg_connection() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                                    (datetime.utcnow().isoformat(), uid))
                        conn.commit()
                except:
                    pass
            threading.Thread(target=_update_last_login_bg, args=(user_id,), daemon=True).start()

            return jsonify({
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
            })
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500


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
                               (datetime.utcnow().isoformat(), google_id, user_id))
            else:
                user_id = secrets.token_hex(8)
                user_plan = 'free'
                user_role = 'user'
                pg_cur.execute("""
                    INSERT INTO users (id, email, name, plan, role, google_id, created_at, last_login)
                    VALUES (%s, %s, %s, 'free', 'user', %s, %s, %s)
                """, (user_id, email, name, google_id,
                      datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))

            pg_conn.commit()
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

        jwt_token = generate_jwt(user_id, email, user_role)

        return f"""<!DOCTYPE html><html><body><script>
        window.opener.postMessage({{
            type: 'google-auth-success',
            token: '{jwt_token}',
            user: {{id:'{user_id}',email:'{email}',name:'{name}',plan:'{user_plan}',role:'{user_role}'}}
        }}, '*');
        window.close();
        </script></body></html>"""

    except Exception as e:
        logger.error(f"Google callback error: {e}")
        error_msg = str(e).replace("'", "\\'")
        return f"""<script>window.opener.postMessage({{type:'google-auth-error',error:'{error_msg}'}},'*');window.close();</script>"""


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
                               (datetime.utcnow().isoformat(), google_id, user_id))
            else:
                user_id = secrets.token_hex(8)
                user_plan = 'free'
                user_role = 'user'
                user_name = name
                user_company = ''
                pg_cur.execute("""
                    INSERT INTO users (id, email, name, plan, role, google_id, created_at, last_login)
                    VALUES (%s, %s, %s, 'free', 'user', %s, %s, %s)
                """, (user_id, email, name, google_id,
                      datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))

            pg_conn.commit()
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

        jwt_token = generate_jwt(user_id, email, user_role)

        return jsonify({
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
        })
    except Exception as e:
        logger.error(f"Google auth error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': 'Google authentication failed'}), 500


@auth_bp.route('/api/auth/me', methods=['GET'])
@require_auth
def get_current_user():
    """Get current user profile"""
    try:
        with _pg_connection() as pg_conn:
            pg_cur = pg_conn.cursor()
            pg_cur.execute("""
                SELECT id, email, name, company, plan, role, created_at, last_login
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
                    'last_login': user[7]
                }
            })
    except Exception as e:
        logger.error(f"Get user error: {e}")
        return jsonify({'error': 'Failed to retrieve user'}), 500


@auth_bp.route('/api/auth/update', methods=['PUT'])
@require_auth
def update_user():
    """Update user profile"""
    data = request.get_json()

    conn = _get_db()
    c = conn.cursor()

    updates = []
    params = []

    if 'name' in data:
        updates.append('name = ?')
        params.append(data['name'])
    if 'company' in data:
        updates.append('company = ?')
        params.append(data['company'])
    if 'preferences' in data:
        updates.append('preferences = ?')
        params.append(json.dumps(data['preferences']))
    if 'saved_searches' in data:
        updates.append('saved_searches = ?')
        params.append(json.dumps(data['saved_searches']))
    if 'saved_markets' in data:
        updates.append('saved_markets = ?')
        params.append(json.dumps(data['saved_markets']))

    if updates:
        params.append(request.user['user_id'])
        c.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    conn.close()

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
    c = conn.cursor()

    c.execute("""
        SELECT id, market, alert_type, enabled, created_at, last_triggered, trigger_count
        FROM user_alerts
        WHERE user_id = ?
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

    conn.close()

    return jsonify({
        'success': True,
        'searches': [],
        'alerts': alerts,
        'watchlist': [],
        'stats': {
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
    c = conn.cursor()

    c.execute("""
        SELECT id, key_prefix, name, plan, rate_limit_tier, is_active,
               created_at, usage_count, calls_today, calls_total
        FROM api_keys
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (request.user['user_id'],))

    rows = c.fetchall()
    conn.close()

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
                expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()

                pg_cur.execute("UPDATE password_reset_tokens SET used = TRUE WHERE user_email = %s AND used = FALSE", (email,))
                pg_cur.execute(
                    "INSERT INTO password_reset_tokens (user_email, token, expires_at) VALUES (%s, %s, %s)",
                    (email, token, expires_at)
                )
                pg_conn.commit()
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

                reset_url = f"https://dchub.cloud/reset-password?token={token}"
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
            # Send free welcome email
            try:
                from main import send_free_welcome_email_sendgrid
                send_free_welcome_email_sendgrid(email, name)
            except Exception as email_err:
                logger.warning(f"Free welcome email failed for {email}: {email_err}")

            print(f"✅ Password reset successful for {email}")
            return jsonify({'success': True, 'message': 'Password has been reset. You can now log in.'})

    except Exception as e:
        print(f"❌ Reset password error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'An error occurred. Please try again.'}), 500
