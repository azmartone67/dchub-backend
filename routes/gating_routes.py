"""Phase 68 -- data gating foundation.

Three layers:
  1. Server-side tier detection via existing auth (api_key / session)
  2. Jinja filter |gated for templates: {{ value|gated('dev') }}
  3. /api/v1/me/tier endpoint for client-side gating.js to query

Tier hierarchy (low to high):
  anonymous < free < developer < pro < enterprise

To apply gating in a template:
  Server-side hard gate:
    {{ exact_kwh_rate|gated('dev', placeholder='~$0.09') }}

  Client-side visual gate (lighter, more flexible):
    <span data-gate="dev" data-placeholder="~530">{{ exact_count }}</span>

  When the user is below the required tier:
    - server-side returns the placeholder (or 'Pro only' default)
    - client-side replaces the element with a redeem-URL CTA
"""
from flask import Blueprint, request, jsonify, session

gating_bp = Blueprint('gating', __name__)

# Tier ordering; higher index = higher tier
TIER_ORDER = ['anonymous', 'free', 'developer', 'pro', 'enterprise', 'founding']
TIER_INDEX = {t: i for i, t in enumerate(TIER_ORDER)}


def _tier_from_api_key(api_key):
    """Best-effort tier lookup from API key.

    Tries the existing validate_key_tier() helper if available.
    Falls back to anonymous if the helper isn't importable.
    """
    if not api_key:
        return 'anonymous'
    try:
        from mcp_upgrade_gate import validate_key_tier
        t = validate_key_tier(api_key)
        return t or 'anonymous'
    except Exception:
        pass
    # Fallback: lookup in mcp_dev_keys table directly
    try:
        import os
        neon = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not neon:
            return 'anonymous'
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                conn = mod.connect(neon)
                break
            except Exception:
                continue
        else:
            return 'anonymous'
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT tier FROM mcp_dev_keys WHERE key_value = %s OR id::text = %s LIMIT 1",
                (api_key, api_key)
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        pass
    return 'anonymous'


def get_current_tier():
    """Return the current request's tier name. Cached on `request`."""
    cached = getattr(request, '_cached_tier', None)
    if cached is not None:
        return cached
    api_key = (
        request.headers.get('X-API-Key')
        or request.headers.get('Authorization', '').replace('Bearer ', '').strip()
        or request.args.get('api_key')
        or request.cookies.get('api_key')
        or (session.get('api_key') if session else None)
    )
    tier = _tier_from_api_key(api_key)
    try:
        setattr(request, '_cached_tier', tier)
    except Exception:
        pass
    return tier


def has_tier(required_tier):
    """True if current request's tier >= required_tier."""
    cur = TIER_INDEX.get(get_current_tier(), 0)
    req = TIER_INDEX.get(required_tier, 0)
    return cur >= req


def gated(value, required='developer', placeholder=None):
    """Jinja filter that returns the value if the user has the required tier,
    otherwise returns the placeholder (or a default 'Pro only' marker).

    Usage in templates:
      {{ exact_count|gated('developer', placeholder='500+') }}
      {{ deal_size|gated('pro') }}
    """
    if has_tier(required):
        return value
    if placeholder is not None:
        return placeholder
    return f'<span class="gated-pill" data-required="{required}">Pro only</span>'


@gating_bp.route('/api/v1/me/tier', methods=['GET'])
def my_tier():
    """Return the current user's tier as JSON. Used by gating.js."""
    tier = get_current_tier()
    session_id = (
        request.headers.get('Mcp-Session-Id')
        or request.headers.get('X-Session-Id')
        or request.cookies.get('session_id')
        or ''
    )
    return jsonify({
        'tier': tier,
        'tier_index': TIER_INDEX.get(tier, 0),
        'session_id': session_id,
        'redeem_url_template': 'https://dchub.cloud/api/v1/redeem/{session_id}',
    })


def register_jinja_filter(app):
    """Call this from main.py after app creation to register the |gated filter."""
    app.jinja_env.filters['gated'] = gated
