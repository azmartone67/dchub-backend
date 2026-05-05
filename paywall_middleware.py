"""
DC Hub Paywall Middleware - Server-side Tier Enforcement

Integration with main.py:
    from paywall_middleware import paywall_bp, require_auth, require_tier, check_rate_limit
    app.register_blueprint(paywall_bp)

    # Protect routes:
    @app.route('/api/facilities/<path:path>')
    @require_auth
    @require_tier('pro')
    @check_rate_limit
    def facilities_handler(path): ...

    # Auto-truncate responses for free/registered tiers:
    response = get_facilities_data(query)
    response = auto_truncate(response, 'facilities')
    return response

Requires environment variables:
    - DATABASE_URL: postgresql://... (Neon)
    - JWT_SECRET: signing key for JWT tokens
    - STRIPE_WEBHOOK_SECRET: webhook signature verification

Tier hierarchy: free < registered < developer < pro < enterprise
Features and rate limits are tier-dependent.
"""

import os
import jwt
import json
import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, Tuple, Optional, Any

import psycopg2
from psycopg2 import pool
from flask import Blueprint, request, jsonify, g, current_app
import stripe

# Initialize logging
logger = logging.getLogger(__name__)

# Tier hierarchy
TIER_HIERARCHY = {
    'free': 0,
    'registered': 1,
    'developer': 2,
    'pro': 3,
    'enterprise': 4
}

# Feature flags by tier
TIER_FEATURES = {
    'free': {
        'facilitiesSearch': True,
        'marketIntel': False,
        'pipelineTracking': False,
        'transactionData': False,
        'gridIntelligence': False,
        'exportData': False,
    },
    'registered': {
        'facilitiesSearch': True,
        'marketIntel': True,
        'pipelineTracking': False,
        'transactionData': False,
        'gridIntelligence': False,
        'exportData': False,
    },
    'developer': {
        'facilitiesSearch': True,
        'marketIntel': True,
        'pipelineTracking': True,
        'transactionData': True,
        'gridIntelligence': True,
        'exportData': True,
    },
    'pro': {
        'facilitiesSearch': True,
        'marketIntel': True,
        'pipelineTracking': True,
        'transactionData': True,
        'gridIntelligence': True,
        'exportData': True,
    },
    'enterprise': {
        'facilitiesSearch': True,
        'marketIntel': True,
        'pipelineTracking': True,
        'transactionData': True,
        'gridIntelligence': True,
        'exportData': True,
    },
}

# Rate limits (calls per day)
RATE_LIMITS = {
    'free': 10,
    'registered': 15,
    'developer': 1000,
    'pro': 10000,
    'enterprise': 100000,
}

# Truncation limits per tier
TRUNCATION_LIMITS = {
    'facilities': 5,
    'market-intel': 3,
    'transactions': 3,
    'pipeline': 5,
}

# Stripe configuration
STRIPE_CHECKOUT_URL = 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'

# Global tier cache: {user_id: (tier, timestamp)}
_tier_cache: Dict[int, Tuple[str, float]] = {}
_tier_cache_ttl = 60  # seconds

# Global rate limit tracker: {user_id: {date: call_count}}
_rate_limit_tracker: Dict[int, Dict[str, int]] = {}


class PaywallError(Exception):
    """Base exception for paywall middleware."""
    pass


class TierNotFoundError(PaywallError):
    """User tier could not be determined."""
    pass


class JWTError(PaywallError):
    """JWT token is invalid or expired."""
    pass


def _get_db_pool() -> pool.SimpleConnectionPool:
    """
    Get or create a database connection pool.

    Returns:
        psycopg2 SimpleConnectionPool

    Raises:
        PaywallError: if DATABASE_URL is not set
    """
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise PaywallError('DATABASE_URL environment variable not set')

    if not hasattr(current_app, '_db_pool'):
        current_app._db_pool = pool.SimpleConnectionPool(
            1, 5,
            database_url,
            connect_timeout=5
        )

    return current_app._db_pool


def _decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT token.

    Args:
        token: JWT token string (with or without 'Bearer ' prefix)

    Returns:
        Decoded token payload

    Raises:
        JWTError: if token is invalid or expired
    """
    jwt_secret = os.environ.get('JWT_SECRET')
    if not jwt_secret:
        raise JWTError('JWT_SECRET environment variable not set')

    # Strip 'Bearer ' prefix if present
    if token.startswith('Bearer '):
        token = token[7:]

    try:
        payload = jwt.decode(token, jwt_secret, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        raise JWTError('Token has expired')
    except jwt.InvalidTokenError as e:
        raise JWTError(f'Invalid token: {str(e)}')


def _get_user_tier(user_id: int) -> str:
    """
    Retrieve user's tier from database with caching.

    Args:
        user_id: User ID

    Returns:
        Tier string ('free', 'registered', 'developer', 'pro', 'enterprise')

    Raises:
        TierNotFoundError: if user not found
        PaywallError: if database error occurs
    """
    # Check cache
    now = datetime.utcnow().timestamp()
    if user_id in _tier_cache:
        tier, timestamp = _tier_cache[user_id]
        if now - timestamp < _tier_cache_ttl:
            return tier

    # Query database
    try:
        db_pool = _get_db_pool()
        conn = db_pool.getconn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT u.id, u.email, u.plan, u.subscription_status
                FROM users u
                WHERE u.id = %s
                """,
                (user_id,)
            )
            row = cursor.fetchone()
            cursor.close()

            if not row:
                raise TierNotFoundError(f'User {user_id} not found')

            user_id, email, plan, sub_status = row

            # Normalize plan name (founding -> pro for tier purposes)
            plan = (plan or 'free').lower()
            if plan == 'founding':
                plan = 'pro'

            # Determine tier based on plan and subscription status
            if plan == 'enterprise':
                tier = 'enterprise'
            elif plan in ('pro', 'developer'):
                # Check if subscription is active
                if sub_status in ('active', 'trialing', None, ''):
                    tier = plan
                elif sub_status in ('canceled', 'cancelled', 'past_due', 'unpaid'):
                    tier = 'free'
                else:
                    tier = plan  # default to plan if status unknown
            elif plan == 'registered':
                tier = 'registered'
            else:
                tier = 'free'

            # Update cache
            _tier_cache[user_id] = (tier, now)

            return tier
        finally:
            db_pool.putconn(conn)
    except psycopg2.Error as e:
        logger.error(f'Database error retrieving tier for user {user_id}: {str(e)}')
        raise PaywallError(f'Database error: {str(e)}')


def _bust_tier_cache(user_id: int) -> None:
    """
    Remove user's tier from cache (e.g., after subscription change).

    Args:
        user_id: User ID to invalidate
    """
    if user_id in _tier_cache:
        del _tier_cache[user_id]


def _get_today_key() -> str:
    """Get today's date as a key for rate limiting."""
    return datetime.utcnow().strftime('%Y-%m-%d')


def _check_rate_limit_internal(user_id: int, tier: str) -> bool:
    """
    Check if user has exceeded rate limit for today.

    Args:
        user_id: User ID
        tier: User's tier

    Returns:
        True if under limit, False if exceeded
    """
    today = _get_today_key()

    if user_id not in _rate_limit_tracker:
        _rate_limit_tracker[user_id] = {}

    user_limits = _rate_limit_tracker[user_id]

    # Clean old date entries
    for date_key in list(user_limits.keys()):
        if date_key != today:
            del user_limits[date_key]

    current_count = user_limits.get(today, 0)
    limit = RATE_LIMITS.get(tier, 10)

    if current_count >= limit:
        return False

    user_limits[today] = current_count + 1
    return True


def require_auth(f):
    """
    Decorator to require authentication.

    Validates JWT from Authorization Bearer header or dchub_token cookie.
    Sets g.user with user_id, email, and tier.

    Returns:
        401 JSON if authentication fails
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None

        # Check Authorization header
        auth_header = request.headers.get('Authorization')
        if auth_header:
            try:
                token = auth_header
            except Exception:
                pass

        # Check dchub_token cookie
        if not token:
            token = request.cookies.get('dchub_token')

        if not token:
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Missing authentication token'
            }), 401

        try:
            payload = _decode_token(token)
            user_id = payload.get('sub') or payload.get('user_id')
            email = payload.get('email')

            if not user_id:
                raise JWTError('Token missing user ID')

            # Get user tier
            tier = _get_user_tier(user_id)

            # Set user context
            g.user = {
                'id': user_id,
                'email': email,
                'tier': tier
            }

        except JWTError as e:
            logger.warning(f'JWT validation failed: {str(e)}')
            return jsonify({
                'error': 'Unauthorized',
                'message': str(e)
            }), 401
        except (TierNotFoundError, PaywallError) as e:
            logger.error(f'Authentication error: {str(e)}')
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Could not verify user'
            }), 401

        return f(*args, **kwargs)

    return decorated_function


def require_tier(min_tier: str):
    """
    Decorator to require a minimum tier.

    Args:
        min_tier: Minimum tier required ('free', 'registered', 'developer', 'pro', 'enterprise')

    Returns:
        403 JSON with upgrade URL if tier insufficient
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g, 'user'):
                return jsonify({
                    'error': 'Unauthorized',
                    'message': 'Authentication required'
                }), 401

            user_tier = g.user.get('tier', 'free')
            min_tier_level = TIER_HIERARCHY.get(min_tier, 999)
            user_tier_level = TIER_HIERARCHY.get(user_tier, 0)

            if user_tier_level < min_tier_level:
                return jsonify({
                    'error': 'Forbidden',
                    'message': f'This endpoint requires {min_tier} tier or higher',
                    'currentTier': user_tier,
                    'requiredTier': min_tier,
                    'upgradeUrl': STRIPE_CHECKOUT_URL
                }), 403

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def check_rate_limit(f):
    """
    Decorator to enforce rate limiting per tier per day.

    Returns:
        429 JSON with upgrade message if limit exceeded
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not hasattr(g, 'user'):
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Authentication required'
            }), 401

        user_id = g.user['id']
        tier = g.user['tier']

        if not _check_rate_limit_internal(user_id, tier):
            limit = RATE_LIMITS.get(tier, 10)
            return jsonify({
                'error': 'Too Many Requests',
                'message': f'Rate limit exceeded: {limit} calls per day for {tier} tier',
                'currentTier': tier,
                'dailyLimit': limit,
                'upgradeUrl': STRIPE_CHECKOUT_URL
            }), 429

        return f(*args, **kwargs)

    return decorated_function


def auto_truncate(data: Any, category: str) -> Dict[str, Any]:
    """
    Truncate response data for free/registered tiers.

    Args:
        data: Response data (dict or list)
        category: Data category ('facilities', 'market-intel', 'transactions', 'pipeline')

    Returns:
        Truncated response with _upgrade object if applicable
    """
    if not hasattr(g, 'user'):
        return data

    tier = g.user.get('tier', 'free')
    limit = TRUNCATION_LIMITS.get(category)

    # Only truncate for free/registered tiers
    if tier not in ('free', 'registered') or not limit:
        return data

    # Handle list response
    if isinstance(data, list):
        truncated = data[:limit]
    # Handle dict response with 'data' key
    elif isinstance(data, dict) and 'data' in data:
        if isinstance(data['data'], list):
            data['data'] = data['data'][:limit]
        truncated = data
    else:
        truncated = data

    # Add upgrade object
    if isinstance(truncated, dict):
        truncated['_upgrade'] = {
            'currentTier': tier,
            'message': f'{tier} tier limited to {limit} results. Upgrade for unlimited access.',
            'upgradeUrl': STRIPE_CHECKOUT_URL,
            'features': {
                'unlimitedResults': tier not in ('free', 'registered'),
                'prioritySupport': tier in ('pro', 'enterprise'),
                'customIntegrations': tier == 'enterprise'
            }
        }
    elif isinstance(truncated, list):
        truncated = {
            'data': truncated,
            '_upgrade': {
                'currentTier': tier,
                'message': f'{tier} tier limited to {limit} results. Upgrade for unlimited access.',
                'upgradeUrl': STRIPE_CHECKOUT_URL,
                'features': {
                    'unlimitedResults': tier not in ('free', 'registered'),
                    'prioritySupport': tier in ('pro', 'enterprise'),
                    'customIntegrations': tier == 'enterprise'
                }
            }
        }

    return truncated


# Initialize Blueprint
paywall_bp = Blueprint('paywall', __name__)


@paywall_bp.route('/api/verify-session', methods=['GET'])
@require_auth
def verify_session():
    """
    Verify user session and return tier/feature information.

    Returns:
        JSON with authenticated status, tier, features, limits, and expiration
    """
    user_id = g.user['id']
    email = g.user['email']
    tier = g.user['tier']

    # Get subscription details from users table
    expires_at = None
    try:
        db_pool = _get_db_pool()
        conn = db_pool.getconn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT subscription_status
                FROM users
                WHERE id = %s
                """,
                (user_id,)
            )
            sub_row = cursor.fetchone()
            cursor.close()
            # No expiration tracking in current schema — tier is authoritative
        finally:
            db_pool.putconn(conn)
    except Exception as e:
        logger.error(f'Error fetching subscription: {str(e)}')

    return jsonify({
        'authenticated': True,
        'userId': user_id,
        'email': email,
        'tier': tier,
        'features': TIER_FEATURES.get(tier, {}),
        'limits': {
            'dailyCalls': RATE_LIMITS.get(tier, 10),
            'maxResults': {
                'facilities': None if tier not in ('free', 'registered') else TRUNCATION_LIMITS['facilities'],
                'marketIntel': None if tier not in ('free', 'registered') else TRUNCATION_LIMITS['market-intel'],
                'transactions': None if tier not in ('free', 'registered') else TRUNCATION_LIMITS['transactions'],
                'pipeline': None if tier not in ('free', 'registered') else TRUNCATION_LIMITS['pipeline'],
            }
        },
        'expiresAt': expires_at
    }), 200


@paywall_bp.route('/api/stripe/tier-webhook', methods=['POST'])
def stripe_tier_webhook():
    """
    Handle Stripe webhook events for tier changes.

    Supports:
        - checkout.session.completed: user purchased subscription
        - customer.subscription.deleted: subscription cancelled

    Returns:
        200 JSON if processed, 400 if signature invalid, 500 if error
    """
    stripe_webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    if not stripe_webhook_secret:
        logger.error('STRIPE_WEBHOOK_SECRET not configured')
        return jsonify({'error': 'Webhook not configured'}), 500

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    if not sig_header:
        logger.warning('Stripe webhook missing signature header')
        return jsonify({'error': 'Missing signature'}), 400

    # Verify signature
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, stripe_webhook_secret
        )
    except ValueError:
        logger.warning('Stripe webhook invalid payload')
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        logger.warning('Stripe webhook invalid signature')
        return jsonify({'error': 'Invalid signature'}), 400

    try:
        event_type = event['type']
        event_data = event['data']['object']

        user_id = None

        if event_type == 'checkout.session.completed':
            # User purchased subscription
            customer_id = event_data.get('customer')
            if customer_id:
                # Query database for user with this Stripe customer ID
                try:
                    db_pool = _get_db_pool()
                    conn = db_pool.getconn()
                    try:
                        cursor = conn.cursor()
                        cursor.execute(
                            'SELECT id FROM users WHERE stripe_customer_id = %s',
                            (customer_id,)
                        )
                        row = cursor.fetchone()
                        cursor.close()
                        if row:
                            user_id = row[0]
                    finally:
                        db_pool.putconn(conn)
                except Exception as e:
                    logger.error(f'Error querying user for customer {customer_id}: {str(e)}')

        elif event_type == 'customer.subscription.deleted':
            # Subscription cancelled
            customer_id = event_data.get('customer')
            if customer_id:
                try:
                    db_pool = _get_db_pool()
                    conn = db_pool.getconn()
                    try:
                        cursor = conn.cursor()
                        cursor.execute(
                            'SELECT id FROM users WHERE stripe_customer_id = %s',
                            (customer_id,)
                        )
                        row = cursor.fetchone()
                        cursor.close()
                        if row:
                            user_id = row[0]
                    finally:
                        db_pool.putconn(conn)
                except Exception as e:
                    logger.error(f'Error querying user for customer {customer_id}: {str(e)}')

        # Bust cache for affected user
        if user_id:
            _bust_tier_cache(user_id)
            logger.info(f'Busted tier cache for user {user_id} due to {event_type}')

        return jsonify({
            'received': True,
            'eventType': event_type,
            'userUpdated': user_id is not None
        }), 200

    except Exception as e:
        logger.error(f'Error processing Stripe webhook: {str(e)}')
        return jsonify({'error': 'Webhook processing failed'}), 500


def init_app(app):
    """
    Initialize paywall middleware with Flask app.

    Args:
        app: Flask application instance
    """
    # Configure Stripe
    stripe_key = os.environ.get('STRIPE_API_KEY')
    if stripe_key:
        stripe.api_key = stripe_key

    # Register blueprint
    app.register_blueprint(paywall_bp)

    logger.info('Paywall middleware initialized')
