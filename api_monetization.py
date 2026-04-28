"""
DC HUB - API Monetization System v1
====================================
Rate limiting, API key management, usage tracking, and Stripe billing integration.

Features:
  - API key generation and validation
  - Tiered rate limiting (Free, Pro, Enterprise)
  - Usage tracking and analytics
  - Stripe subscription integration
  - Usage-based billing support

Installation:
  1. Add this file to your Replit project
  2. Add to main.py: from api_monetization import register_monetization_routes
  3. Call: register_monetization_routes(app)

Endpoints:
  API Keys:
    GET  /api/v2/keys              - List user's API keys
    POST /api/v2/keys              - Generate new API key
    DELETE /api/v2/keys/:key_id    - Revoke API key
    POST /api/v2/keys/:key_id/regenerate - Regenerate key
  
  Usage:
    GET  /api/v2/usage             - Get usage stats
    GET  /api/v2/usage/history     - Get usage history
  
  Plans:
    GET  /api/v2/plans             - List available plans
    GET  /api/v2/plans/current     - Get user's current plan
"""

import os
import json
import secrets
import hashlib
import time
import threading
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict
from flask import Blueprint, request, jsonify, g
from db_utils import get_db

# =============================================================================
# CONFIGURATION
# =============================================================================

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# Rate limit tiers (requests per day)
RATE_LIMITS = {
    'free': {
        'requests_per_day': 100,
        'requests_per_minute': 10,
        'max_keys': 1,
        'features': ['basic_search', 'facility_list', 'news'],
        'name': 'Free',
        'price': 0
    },
    'pro': {
        'requests_per_day': 10000,
        'requests_per_minute': 100,
        'max_keys': 5,
        'features': ['basic_search', 'facility_list', 'news', 'market_data', 'energy_data', 'exports', 'alerts'],
        'name': 'Pro',
        'price': 299  # $299/month
    },
    'enterprise': {
        'requests_per_day': 100000,
        'requests_per_minute': 1000,
        'max_keys': 20,
        'features': ['all'],
        'name': 'Enterprise',
        'price': 999  # $999/month
    }
}

# Endpoints that require API key authentication
PROTECTED_ENDPOINTS = [
    '/api/v1/facilities',
    '/api/v1/markets',
    '/api/v1/energy',
    '/api/v1/news',
    '/api/v1/deals',
    '/api/v1/pipeline',
    '/api/v1/queue',
    '/api/intelligence',
    '/api/infrastructure',
]

# Endpoints exempt from rate limiting
EXEMPT_ENDPOINTS = [
    '/health',
    '/api/v2/plans',
    '/api/auth',
    '/api/stripe',
]

# =============================================================================
# DATABASE
# =============================================================================


def init_monetization_tables():
    """Initialize API monetization tables"""
    conn = get_db()
    c = conn.cursor()
    
    # Check if api_keys table exists (PostgreSQL catalog)
    c.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_keys')")
    table_exists = c.fetchone()[0]
    
    if table_exists:
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'api_keys'")
        columns = [col[0] for col in c.fetchall()]
        
        if 'user_id' not in columns:
            print("⚠️ Migrating old api_keys table...")
            try:
                c.execute("ALTER TABLE api_keys RENAME TO api_keys_old")
                conn.commit()
            except Exception:
                conn.rollback()
    
    # API Keys table
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL,
            name TEXT,
            permissions TEXT DEFAULT '[]',
            rate_limit_tier TEXT DEFAULT 'free',
            is_active INTEGER DEFAULT 1,
            last_used_at TEXT,
            created_at TEXT,
            expires_at TEXT,
            usage_count INTEGER DEFAULT 0
        )
    """)
    
    # Check if api_usage table exists (PostgreSQL catalog)
    c.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_usage')")
    usage_table_exists = c.fetchone()[0]
    
    if usage_table_exists:
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'api_usage'")
        columns = [col[0] for col in c.fetchall()]
        
        if 'user_id' not in columns or 'api_key_id' not in columns:
            print("⚠️ Migrating old api_usage table...")
            try:
                c.execute("ALTER TABLE api_usage RENAME TO api_usage_old")
                conn.commit()
            except Exception:
                conn.rollback()
    
    # API Usage table (for tracking)
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            api_key_id INTEGER,
            endpoint TEXT,
            method TEXT,
            status_code INTEGER,
            response_time_ms INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            timestamp TEXT,
            date TEXT,
            FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
        )
    """)
    
    # Daily usage aggregates (for fast queries)
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_daily (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            api_key_id INTEGER,
            date TEXT NOT NULL,
            request_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            avg_response_time_ms INTEGER DEFAULT 0,
            endpoints_used TEXT DEFAULT '{}',
            UNIQUE(user_id, api_key_id, date)
        )
    """)
    
    # User plan/subscription info (extends users table)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_plans (
            user_id TEXT PRIMARY KEY,
            plan TEXT DEFAULT 'free',
            stripe_subscription_id TEXT,
            billing_cycle_start TEXT,
            billing_cycle_end TEXT,
            usage_this_cycle INTEGER DEFAULT 0,
            overage_charges REAL DEFAULT 0,
            updated_at TEXT
        )
    """)
    
    # Indexes for performance
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_user_date ON api_usage(user_id, date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_daily_user ON api_usage_daily(user_id, date)")
    except Exception as e:
        print(f"⚠️ Index creation warning: {e}")
    
    conn.commit()
    conn.close()
    print("✅ API Monetization tables initialized")

# =============================================================================
# API KEY MANAGEMENT
# =============================================================================

def generate_api_key():
    """Generate a new API key"""
    # Format: dchub_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
    random_part = secrets.token_hex(24)
    key = f"dchub_live_{random_part}"
    return key

def hash_api_key(key):
    """Hash API key for storage"""
    return hashlib.sha256(key.encode()).hexdigest()

def get_key_prefix(key):
    """Return a stable prefix we store in DB (first 16 chars, no trailing ellipsis).

    Previously this returned `key[:16] + "..."`, which polluted the stored value
    and made `LIKE key_prefix || '%'` matching break. We now store a clean prefix;
    dashboards can render the ellipsis at display time.
    """
    return key[:16] if len(key) > 16 else key

def validate_api_key(key):
    """Validate API key and return key info"""
    if not key or not key.startswith('dchub_'):
        return None
    
    key_hash = hash_api_key(key)
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT ak.*, up.plan
        FROM api_keys ak
        LEFT JOIN user_plans up ON ak.user_id = up.user_id
        WHERE ak.key_hash = %s AND ak.is_active = 1
    """, (key_hash,))
    
    row = c.fetchone()
    conn.close()
    
    if not row:
        return None
    
    # Check expiration
    if row['expires_at']:
        if datetime.fromisoformat(row['expires_at']) < datetime.utcnow():
            return None
    
    return dict(row)

def update_key_usage(key_id):
    """Update last used timestamp and usage count"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        UPDATE api_keys 
        SET last_used_at = %s, usage_count = usage_count + 1
        WHERE id = %s
    """, (datetime.utcnow().isoformat(), key_id))
    
    conn.commit()
    conn.close()

# =============================================================================
# RATE LIMITING
# =============================================================================

class RateLimiter:
    """In-memory rate limiter with Redis-like functionality"""
    
    def __init__(self):
        self.minute_counts = defaultdict(lambda: defaultdict(int))  # user_id -> {minute_key: count}
        self.day_counts = defaultdict(lambda: defaultdict(int))     # user_id -> {day_key: count}
        self.lock = threading.Lock()
        
        # Start cleanup thread
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()
    
    def _get_minute_key(self):
        return datetime.utcnow().strftime('%Y-%m-%d-%H-%M')
    
    def _get_day_key(self):
        return datetime.utcnow().strftime('%Y-%m-%d')
    
    def check_rate_limit(self, user_id, plan='free'):
        """Check if request is within rate limits"""
        limits = RATE_LIMITS.get(plan, RATE_LIMITS['free'])
        
        minute_key = self._get_minute_key()
        day_key = self._get_day_key()
        
        with self.lock:
            minute_count = self.minute_counts[user_id][minute_key]
            day_count = self.day_counts[user_id][day_key]
            
            # Check minute limit
            if minute_count >= limits['requests_per_minute']:
                return False, {
                    'error': 'rate_limit_exceeded',
                    'message': f"Rate limit exceeded. Max {limits['requests_per_minute']} requests per minute.",
                    'retry_after': 60,
                    'limit_type': 'minute'
                }
            
            # Check daily limit
            if day_count >= limits['requests_per_day']:
                return False, {
                    'error': 'daily_limit_exceeded',
                    'message': f"Daily limit exceeded. Max {limits['requests_per_day']} requests per day.",
                    'retry_after': self._seconds_until_midnight(),
                    'limit_type': 'daily',
                    'upgrade_url': 'https://dchub.cloud/pricing'
                }
            
            # Increment counters
            self.minute_counts[user_id][minute_key] += 1
            self.day_counts[user_id][day_key] += 1
            
            return True, {
                'remaining_minute': limits['requests_per_minute'] - minute_count - 1,
                'remaining_daily': limits['requests_per_day'] - day_count - 1,
                'limit_minute': limits['requests_per_minute'],
                'limit_daily': limits['requests_per_day']
            }
    
    def get_usage(self, user_id, plan='free'):
        """Get current usage stats for user"""
        limits = RATE_LIMITS.get(plan, RATE_LIMITS['free'])
        day_key = self._get_day_key()
        
        with self.lock:
            day_count = self.day_counts[user_id].get(day_key, 0)
        
        return {
            'used_today': day_count,
            'limit_daily': limits['requests_per_day'],
            'remaining': max(0, limits['requests_per_day'] - day_count),
            'percentage_used': round((day_count / limits['requests_per_day']) * 100, 1)
        }
    
    def _seconds_until_midnight(self):
        now = datetime.utcnow()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int((midnight - now).total_seconds())
    
    def _cleanup_loop(self):
        """Clean up old rate limit data periodically"""
        while True:
            time.sleep(300)  # Every 5 minutes
            self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """Remove data older than 2 days"""
        cutoff = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d')
        
        with self.lock:
            # Clean minute counts (keep last 10 minutes)
            current_minute = self._get_minute_key()
            for user_id in list(self.minute_counts.keys()):
                old_keys = [k for k in self.minute_counts[user_id] if k < current_minute[:13]]
                for k in old_keys:
                    del self.minute_counts[user_id][k]
            
            # Clean day counts (keep last 2 days)
            for user_id in list(self.day_counts.keys()):
                old_keys = [k for k in self.day_counts[user_id] if k < cutoff]
                for k in old_keys:
                    del self.day_counts[user_id][k]

# Global rate limiter
monetization_rate_limiter = RateLimiter()

# =============================================================================
# USAGE TRACKING
# =============================================================================

def track_api_usage(user_id, api_key_id, endpoint, method, status_code, response_time_ms, ip_address, user_agent):
    """Track API usage asynchronously"""
    def _track():
        try:
            conn = get_db()
            c = conn.cursor()
            
            now = datetime.utcnow()
            date = now.strftime('%Y-%m-%d')
            
            # Insert detailed log
            c.execute("""
                INSERT INTO api_usage (user_id, api_key_id, endpoint, method, status_code, 
                                       response_time_ms, ip_address, user_agent, timestamp, date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, api_key_id, endpoint, method, status_code, 
                  response_time_ms, ip_address, user_agent, now.isoformat(), date))
            
            # Update daily aggregate
            c.execute("""
                INSERT INTO api_usage_daily (user_id, api_key_id, date, request_count, error_count, avg_response_time_ms)
                VALUES (%s, %s, %s, 1, %s, %s)
                ON CONFLICT(user_id, api_key_id, date) DO UPDATE SET
                    request_count = request_count + 1,
                    error_count = error_count + %s,
                    avg_response_time_ms = (avg_response_time_ms * request_count + %s) / (request_count + 1)
            """, (
                user_id, api_key_id, date, 
                1 if status_code >= 400 else 0, 
                response_time_ms,
                1 if status_code >= 400 else 0,
                response_time_ms
            ))
            
            # Update user plan usage
            c.execute("""
                UPDATE user_plans 
                SET usage_this_cycle = usage_this_cycle + 1, updated_at = %s
                WHERE user_id = %s
            """, (now.isoformat(), user_id))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Usage tracking error: {e}")
    
    # Run in background thread
    threading.Thread(target=_track, daemon=True).start()

# =============================================================================
# MIDDLEWARE
# =============================================================================

def require_api_key(f):
    """Decorator to require valid API key"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check for API key in header or query param
        api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
        
        if not api_key:
            return jsonify({
                'success': False,
                'error': 'api_key_required',
                'message': 'API key is required. Get one at https://dchub.cloud/dashboard'
            }), 401
        
        # Validate key
        key_info = validate_api_key(api_key)
        
        if not key_info:
            return jsonify({
                'success': False,
                'error': 'invalid_api_key',
                'message': 'Invalid or expired API key'
            }), 401
        
        # Check rate limit
        plan = key_info.get('plan') or 'free'
        allowed, limit_info = monetization_rate_limiter.check_rate_limit(key_info['user_id'], plan)
        
        if not allowed:
            return jsonify({
                'success': False,
                **limit_info
            }), 429
        
        # Store key info in request context
        g.api_key_info = key_info
        g.rate_limit_info = limit_info
        
        # Update key usage
        update_key_usage(key_info['id'])
        
        return f(*args, **kwargs)
    
    return decorated

def _resolve_plan_for_user(user_id):
    """Look up a user's plan from the users table. Defaults to 'free' if missing/unknown."""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
        row = c.fetchone()
        conn.close()
        plan = (row['plan'] if row else None) or 'free'
        if plan not in RATE_LIMITS:
            plan = 'free'
        return plan
    except Exception:
        return 'free'


def _ensure_user_row(jwt_payload):
    """Make sure a row exists in `users` for this JWT's user_id.

    This prevents the orphan-key bug where `POST /api/v2/keys` would happily
    INSERT an `api_keys` row for a user_id that didn't exist in `users`. Every
    subsequent lookup (`/api/me`, `_get_request_tier`, require_api_key) joins
    api_keys -> users, so a missing users row made the key unrecognizable.

    We seed plan from the JWT claim but only on first-insert; we never overwrite
    an existing users row (that would be an easy privilege-escalation vector if
    the JWT claim were ever stale).
    """
    try:
        user_id = jwt_payload.get('user_id')
        if not user_id:
            return
        email = jwt_payload.get('email') or ''
        claim_plan = jwt_payload.get('plan') or 'free'
        if claim_plan not in RATE_LIMITS:
            claim_plan = 'free'
        now = datetime.utcnow().isoformat()
        conn = get_db()
        c = conn.cursor()
        # Only insert if missing. Existing rows keep their stored plan — DB is truth.
        # `password_hash` is NOT NULL in Neon; use an unusable sentinel so no
        # password can ever authenticate this row (OAuth / JWT still work fine).
        c.execute(
            """
            INSERT INTO users (id, email, password_hash, plan, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (user_id, email, '!oauth-only-no-password!', claim_plan, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Never block key creation on a seed failure; log and continue.
        print(f"⚠️  _ensure_user_row({jwt_payload.get('user_id')!r}) failed: {e}")


def _enforce_limit(user_id, plan):
    """Apply rate limit. Returns (429_response_or_None, limit_info_dict).

    On success, sets g.user_id, g.plan, g.rate_limit_info and returns (None, info).
    On failure, returns (jsonify-429-response, info).
    """
    allowed, limit_info = monetization_rate_limiter.check_rate_limit(user_id, plan)
    if not allowed:
        return jsonify({'success': False, **limit_info}), limit_info
    g.user_id = user_id
    g.plan = plan
    g.rate_limit_info = limit_info
    return None, limit_info


def api_rate_limit_middleware():
    """Middleware to apply rate limiting to all API requests.

    Identity resolution (in order):
      1. X-API-Key header or ?api_key=   -> that key's owner + plan
      2. Authorization: Bearer dchub_... -> that key's owner + plan (API key via Bearer)
      3. Authorization: Bearer <JWT>     -> JWT subject + plan from users table
      4. No/empty credentials            -> anonymous IP-bucketed free tier

    Invalid or malformed tokens do NOT silently fall through to a higher tier.
    They either return 401 (caller explicitly offered a credential that failed)
    or drop to anonymous free-tier limits (empty Bearer).
    """
    # Skip exempt endpoints
    for exempt in EXEMPT_ENDPOINTS:
        if request.path.startswith(exempt):
            return None

    # (1) API key via X-API-Key header or ?api_key= query param
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key:
        key_info = validate_api_key(api_key)
        if not key_info:
            return jsonify({
                'success': False,
                'error': 'invalid_api_key',
                'message': 'Invalid or expired API key'
            }), 401
        plan = key_info.get('plan') or 'free'
        resp, _ = _enforce_limit(key_info['user_id'], plan)
        if resp is not None:
            return resp, 429
        g.api_key_info = key_info
        update_key_usage(key_info['id'])
        return None

    # (2) Authorization: Bearer ...
    auth_header = (request.headers.get('Authorization') or '').strip()
    if auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()

        # (2a) API key passed as a Bearer token (e.g. `Bearer dchub_live_...`)
        if token.startswith('dchub_'):
            key_info = validate_api_key(token)
            if not key_info:
                return jsonify({
                    'success': False,
                    'error': 'invalid_api_key',
                    'message': 'Invalid or expired API key'
                }), 401
            plan = key_info.get('plan') or 'free'
            resp, _ = _enforce_limit(key_info['user_id'], plan)
            if resp is not None:
                return resp, 429
            g.api_key_info = key_info
            update_key_usage(key_info['id'])
            return None

        # (2b) JWT bearer token
        if token:
            payload = None
            try:
                from main import decode_jwt
                payload = decode_jwt(token)
            except Exception:
                payload = None
            if not payload or not payload.get('user_id'):
                return jsonify({
                    'success': False,
                    'error': 'invalid_token',
                    'message': 'Invalid or expired token'
                }), 401
            user_id = payload['user_id']
            plan = _resolve_plan_for_user(user_id)
            resp, _ = _enforce_limit(user_id, plan)
            if resp is not None:
                return resp, 429
            return None
        # Empty Bearer -> fall through to anonymous

    # (3) Anonymous request - apply free tier limits (IP-bucketed)
    ip = request.headers.get('CF-Connecting-IP') or \
         request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or \
         request.remote_addr or 'unknown'
    user_id = f"anon_{hashlib.md5(ip.encode()).hexdigest()[:12]}"
    resp, _ = _enforce_limit(user_id, 'free')
    if resp is not None:
        return resp, 429
    return None

def track_request_middleware(response):
    """Middleware to track API usage after request completes"""
    # Only track API endpoints
    if not request.path.startswith('/api/'):
        return response
    
    # Skip tracking endpoints
    skip_paths = ['/api/v2/usage', '/api/v2/keys', '/api/auth', '/api/stripe']
    for skip in skip_paths:
        if request.path.startswith(skip):
            return response
    
    # Get timing
    response_time = getattr(g, 'request_start_time', None)
    if response_time:
        response_time_ms = int((time.time() - response_time) * 1000)
    else:
        response_time_ms = 0
    
    # Track usage
    user_id = getattr(g, 'user_id', None)
    api_key_info = getattr(g, 'api_key_info', None)
    
    if user_id:
        track_api_usage(
            user_id=user_id,
            api_key_id=api_key_info['id'] if api_key_info else None,
            endpoint=request.path,
            method=request.method,
            status_code=response.status_code,
            response_time_ms=response_time_ms,
            ip_address=request.headers.get('CF-Connecting-IP') or request.remote_addr,
            user_agent=request.headers.get('User-Agent', '')[:200]
        )
    
    # Add rate limit headers (authoritative — this file owns tier assignment)
    rate_info = getattr(g, 'rate_limit_info', None)
    plan = getattr(g, 'plan', None)
    if rate_info and isinstance(rate_info, dict):
        response.headers['X-RateLimit-Limit'] = str(rate_info.get('limit_daily', 100))
        response.headers['X-RateLimit-Remaining'] = str(rate_info.get('remaining_daily', 0))
        response.headers['X-RateLimit-Limit-Minute'] = str(rate_info.get('limit_minute', 10))
        response.headers['X-RateLimit-Remaining-Minute'] = str(rate_info.get('remaining_minute', 0))
    if plan:
        response.headers['X-RateLimit-Tier'] = plan

    return response

# =============================================================================
# FLASK ROUTES
# =============================================================================

monetization_bp = Blueprint('monetization', __name__)

# --- Plans ---

@monetization_bp.route('/api/v2/plans', methods=['GET'])
def list_plans():
    """List available API plans"""
    plans = []
    for key, plan in RATE_LIMITS.items():
        plans.append({
            'id': key,
            'name': plan['name'],
            'price': plan['price'],
            'price_display': f"${plan['price']}/month" if plan['price'] > 0 else 'Free',
            'requests_per_day': plan['requests_per_day'],
            'requests_per_minute': plan['requests_per_minute'],
            'max_api_keys': plan['max_keys'],
            'features': plan['features']
        })
    
    return jsonify({
        'success': True,
        'plans': plans
    })

@monetization_bp.route('/api/v2/plans/current', methods=['GET'])
def get_current_plan():
    """Get user's current plan and usage"""
    # Get user from JWT
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    conn = get_db()
    c = conn.cursor()
    
    # Get user plan
    c.execute("SELECT * FROM user_plans WHERE user_id = %s", (user_id,))
    plan_row = c.fetchone()
    
    # Get from users table if not in user_plans
    if not plan_row:
        c.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
        user_row = c.fetchone()
        plan = user_row['plan'] if user_row else 'free'
        
        # Create user_plans entry
        c.execute("""
            INSERT INTO user_plans (user_id, plan, updated_at)
            VALUES (%s, %s, %s)
        """, (user_id, plan, datetime.utcnow().isoformat()))
        conn.commit()
        
        plan_info = {'plan': plan, 'usage_this_cycle': 0}
    else:
        plan_info = dict(plan_row)
    
    conn.close()
    
    # Get current usage from rate limiter
    plan = plan_info.get('plan') or 'free'
    usage = monetization_rate_limiter.get_usage(user_id, plan)
    limits = RATE_LIMITS.get(plan, RATE_LIMITS['free'])
    
    return jsonify({
        'success': True,
        'plan': {
            'id': plan,
            'name': limits['name'],
            'price': limits['price']
        },
        'usage': usage,
        'limits': {
            'requests_per_day': limits['requests_per_day'],
            'requests_per_minute': limits['requests_per_minute'],
            'max_api_keys': limits['max_keys']
        },
        'billing': {
            'cycle_start': plan_info.get('billing_cycle_start'),
            'cycle_end': plan_info.get('billing_cycle_end'),
            'usage_this_cycle': plan_info.get('usage_this_cycle', 0)
        }
    })

# --- API Keys ---

@monetization_bp.route('/api/v2/keys', methods=['GET'])
def list_api_keys():
    """List user's API keys"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT id, key_prefix, name, is_active, created_at, last_used_at, usage_count
        FROM api_keys
        WHERE user_id = %s
        ORDER BY created_at DESC
    """, (user_id,))
    
    keys = []
    for row in c.fetchall():
        keys.append({
            'id': row['id'],
            'key_prefix': row['key_prefix'],
            'name': row['name'],
            'is_active': bool(row['is_active']),
            'created_at': row['created_at'],
            'last_used_at': row['last_used_at'],
            'usage_count': row['usage_count']
        })
    
    conn.close()
    
    return jsonify({
        'success': True,
        'keys': keys,
        'count': len(keys)
    })

@monetization_bp.route('/api/v2/keys', methods=['POST'])
def create_api_key():
    """Generate a new API key"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401

    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except Exception:
        return jsonify({'error': 'Auth error'}), 401

    data = request.get_json() or {}
    name = data.get('name', 'Default Key')

    # Make sure there's a users row for this JWT before we INSERT the key.
    # Without this, a valid JWT whose user_id has no matching users row would
    # create an orphan api_keys row that no auth path can ever resolve.
    _ensure_user_row(payload)

    conn = get_db()
    c = conn.cursor()

    # Check user's plan and key limit
    c.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()
    plan = (user_row['plan'] if user_row else None) or 'free'
    if plan not in RATE_LIMITS:
        plan = 'free'
    
    max_keys = RATE_LIMITS.get(plan, RATE_LIMITS['free'])['max_keys']
    
    c.execute("SELECT COUNT(*) as count FROM api_keys WHERE user_id = %s AND is_active = 1", (user_id,))
    current_count = c.fetchone()['count']
    
    if current_count >= max_keys:
        conn.close()
        return jsonify({
            'success': False,
            'error': 'key_limit_reached',
            'message': f'Maximum {max_keys} API keys allowed on {plan} plan. Upgrade for more.',
            'upgrade_url': 'https://dchub.cloud/pricing'
        }), 403
    
    # Generate key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = get_key_prefix(api_key)
    
    now = datetime.utcnow().isoformat()
    
    c.execute("""
        INSERT INTO api_keys (user_id, key_hash, key_prefix, name, rate_limit_tier, plan, is_active, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s) RETURNING id
    """, (user_id, key_hash, key_prefix, name, plan, plan, now))
    
    row = c.fetchone()
    key_id = row['id'] if row else None
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'key': api_key,
        'api_key': api_key,
        'key_data': {
            'id': key_id,
            'api_key': api_key,
            'key_prefix': key_prefix,
            'name': name,
            'plan': plan,
            'created_at': now
        },
        'message': 'Save this API key - it will not be shown again!'
    }), 201

@monetization_bp.route('/api/v2/keys/<int:key_id>', methods=['DELETE'])
def revoke_api_key(key_id):
    """Revoke an API key"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    conn = get_db()
    c = conn.cursor()
    
    # Verify ownership
    c.execute("SELECT id FROM api_keys WHERE id = %s AND user_id = %s", (key_id, user_id))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'API key not found'}), 404
    
    # Soft delete (mark inactive)
    c.execute("UPDATE api_keys SET is_active = 0 WHERE id = %s", (key_id,))
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'API key revoked'
    })

@monetization_bp.route('/api/v2/keys/<int:key_id>/regenerate', methods=['POST'])
def regenerate_api_key(key_id):
    """Regenerate an API key (creates new key, revokes old)"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    conn = get_db()
    c = conn.cursor()
    
    # Verify ownership and get old key info
    c.execute("SELECT * FROM api_keys WHERE id = %s AND user_id = %s", (key_id, user_id))
    old_key = c.fetchone()
    
    if not old_key:
        conn.close()
        return jsonify({'error': 'API key not found'}), 404
    
    # Generate new key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = get_key_prefix(api_key)
    now = datetime.utcnow().isoformat()
    
    # Update with new key
    c.execute("""
        UPDATE api_keys 
        SET key_hash = %s, key_prefix = %s, created_at = %s, last_used_at = NULL, usage_count = 0
        WHERE id = %s
    """, (key_hash, key_prefix, now, key_id))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'key': {
            'id': key_id,
            'api_key': api_key,
            'key_prefix': key_prefix,
            'name': old_key['name'],
            'created_at': now
        },
        'message': 'API key regenerated. Save this key - it will not be shown again!'
    })

# --- Usage Stats ---

@monetization_bp.route('/api/v2/usage', methods=['GET'])
def get_usage_stats():
    """Get usage statistics for current user"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    conn = get_db()
    c = conn.cursor()
    
    # Get user's plan
    c.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()
    plan = user_row['plan'] if user_row else 'free'
    
    # Get today's usage from rate limiter
    today_usage = monetization_rate_limiter.get_usage(user_id, plan)
    
    # Get last 30 days from database
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')
    
    c.execute("""
        SELECT date, SUM(request_count) as requests, SUM(error_count) as errors
        FROM api_usage_daily
        WHERE user_id = %s AND date >= %s
        GROUP BY date
        ORDER BY date DESC
    """, (user_id, thirty_days_ago))
    
    daily_stats = [dict(row) for row in c.fetchall()]
    
    # Get total stats
    c.execute("""
        SELECT 
            SUM(request_count) as total_requests,
            SUM(error_count) as total_errors,
            AVG(avg_response_time_ms) as avg_response_time
        FROM api_usage_daily
        WHERE user_id = %s
    """, (user_id,))
    
    totals = c.fetchone()
    
    # Get top endpoints
    c.execute("""
        SELECT endpoint, COUNT(*) as count
        FROM api_usage
        WHERE user_id = %s AND date >= %s
        GROUP BY endpoint
        ORDER BY count DESC
        LIMIT 10
    """, (user_id, thirty_days_ago))
    
    top_endpoints = [dict(row) for row in c.fetchall()]
    
    conn.close()
    
    limits = RATE_LIMITS.get(plan, RATE_LIMITS['free'])
    
    return jsonify({
        'success': True,
        'plan': plan,
        'today': today_usage,
        'limits': {
            'requests_per_day': limits['requests_per_day'],
            'requests_per_minute': limits['requests_per_minute']
        },
        'totals': {
            'total_requests': totals['total_requests'] or 0,
            'total_errors': totals['total_errors'] or 0,
            'avg_response_time_ms': round(totals['avg_response_time'] or 0)
        },
        'daily': daily_stats,
        'top_endpoints': top_endpoints
    })

@monetization_bp.route('/api/v2/usage/history', methods=['GET'])
def get_usage_history():
    """Get detailed usage history"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Authorization required'}), 401
    
    try:
        from main import decode_jwt
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401
        user_id = payload['user_id']
    except:
        return jsonify({'error': 'Auth error'}), 401
    
    # Get query params
    days = request.args.get('days', 7, type=int)
    days = min(days, 90)  # Max 90 days
    
    since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT endpoint, method, status_code, response_time_ms, timestamp
        FROM api_usage
        WHERE user_id = %s AND date >= %s
        ORDER BY timestamp DESC
        LIMIT 1000
    """, (user_id, since))
    
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify({
        'success': True,
        'history': history,
        'count': len(history),
        'days': days
    })

# =============================================================================
# REGISTRATION
# =============================================================================

def register_monetization_routes(app, apply_middleware=True):
    """
    Register monetization routes with Flask app
    
    Args:
        app: Flask application
        apply_middleware: Whether to apply rate limiting middleware globally
    """
    # Initialize tables
    init_monetization_tables()
    
    # Register blueprint
    app.register_blueprint(monetization_bp)
    
    # Apply middleware
    if apply_middleware:
        @app.before_request
        def monetization_before_request():
            if request.path in ('/api/health', '/api/version', '/ping', '/api/press-releases'):
            return  # skip rate limiting for health/internal endpoints
            g.request_start_time = time.time()
            return api_rate_limit_middleware()
        
        @app.after_request
        def monetization_after_request(response):
            return track_request_middleware(response)
    
    print("✅ API Monetization System registered")
    print("   💳 GET  /api/v2/plans - List plans")
    print("   🔑 GET  /api/v2/keys - List API keys")
    print("   🔑 POST /api/v2/keys - Generate API key")
    print("   📊 GET  /api/v2/usage - Usage stats")
    print("   ⚡ Rate limiting: Active")
    
    return monetization_rate_limiter

# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == '__main__':
    from flask import Flask
    
    app = Flask(__name__)
    import secrets as _sec
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or _sec.token_hex(32)
    
    register_monetization_routes(app, apply_middleware=False)
    
    print("\n🧪 Running in test mode...")
    print("   Try: GET http://localhost:5002/api/v2/plans")
    
    app.run(host='0.0.0.0', port=5002, debug=True)
