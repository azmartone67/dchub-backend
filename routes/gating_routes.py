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


def _api_key_is_known(api_key):
    """True only if `api_key` is an ACTIVE row in a key table.

    Fails CLOSED: any error, missing DSN or unreadable table returns False.
    Checks both storage conventions, because the two live resolvers disagree —
    mcp_upgrade_gate.validate_key_tier matches the RAW api_key column, while
    util.tier_gate.resolve_tier / _detect_caller_tier match sha256(key) and
    (for raw-stored partner/owner keys) the raw string in api_keys.key_hash.
    A key found by either is real; a key found by neither is not.
    """
    if not api_key:
        return False
    try:
        import os
        import hashlib
        dsn = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not dsn:
            return False
        mod = None
        for modname in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(modname)
                break
            except Exception:
                continue
        if mod is None:
            return False
        key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
        conn = mod.connect(dsn)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM mcp_dev_keys "
                "WHERE api_key = %s AND COALESCE(status, 'active') = 'active' "
                "LIMIT 1",
                (api_key,))
            if cur.fetchone():
                return True
            # api_keys.is_active is an INTEGER column — `IN (1, TRUE)` raises
            # "operator does not exist: integer = boolean" and would be
            # swallowed below, silently reporting every key unknown.
            cur.execute(
                "SELECT 1 FROM api_keys "
                "WHERE key_hash IN (%s, %s) "
                "  AND (is_active IS NULL OR is_active = 1) LIMIT 1",
                (key_hash, api_key))
            if cur.fetchone():
                return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
    return False


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
        # ★ 2026-08-31: validate_key_tier's DOCUMENTED default is "free" — it
        # returns "free" for an unknown key and for a DB error alike, so ANY
        # non-empty header resolved 'free' here and /api/v1/me/tier reported
        # tier_index 1 for `X-API-Key: not_a_real_key_at_all`. That made this
        # endpoint useless as an auth oracle: during a credential revocation it
        # reported the revoked key as still working, and only a bogus-key
        # control distinguished "key still honoured" from "never validated".
        #
        # Do NOT "fix" this by changing validate_key_tier's default. Its other
        # caller is mcp_upgrade_gate.gate_tool_call, whose paywall is
        # `if tool_name in PAID_ONLY_TOOLS and tier == "free"` — returning
        # 'anonymous' there would make the paywall stop firing and hand
        # PAID_ONLY_TOOLS to unknown keys. The ambiguity belongs to this
        # caller, so resolve it here: 'free' is the only value the default can
        # produce, so accept it only when the key demonstrably exists.
        t = (t or 'anonymous')
        if str(t).lower().strip() in ('free', 'anonymous', ''):
            return t if _api_key_is_known(api_key) else 'anonymous'
        return t
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
            # 2026-07-30: this SELECTed by key_value OR id::text — columns
            # mcp_dev_keys has NEVER had (live schema: api_key/tier/status,
            # no id at all) — so it threw UndefinedColumn on every call and
            # the fail-soft except below swallowed it: this fallback always
            # reported 'anonymous'. Same dead-query class as
            # flask_mcp_endpoints.validate_key (PR #1943). Match the
            # primary path it stands in for (mcp_upgrade_gate.
            # validate_key_tier): key column is api_key, active keys only.
            cur.execute(
                "SELECT tier FROM mcp_dev_keys "
                "WHERE api_key = %s AND status = 'active' LIMIT 1",
                (api_key,)
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
    _src = 'api_key' if str(tier).lower() not in ('anonymous', 'anon', '') else (
        'unverified_key' if api_key else 'no_credential')
    # 2026-05-30: also recognize logged-in WEB users (JWT session cookie), not
    # just API keys. Without this, /api/v1/me/tier reports 'anonymous' for a
    # logged-in paid user, so the "Upgrade to Pro" CTA wrongly shows them
    # (the original enterprise-sees-upgrade bug). Fall back to the cookie-aware
    # resolver only when no API-key tier was found — never downgrades.
    if (not tier) or str(tier).lower() in ('anonymous', 'anon'):
        try:
            from map_tier_gating import _detect_caller_tier
            def _gt_dec(_t):
                try:
                    import jwt as _j
                    from main import JWT_SECRET
                    return _j.decode(_t, JWT_SECRET, algorithms=['HS256'])
                except Exception:
                    return None
            _ct, _ = _detect_caller_tier(decode_jwt_func=_gt_dec)
            if _ct and str(_ct).lower() not in ('anonymous', 'anon'):
                tier = str(_ct).lower()
                _src = 'session_cookie'
        except Exception:
            pass
    # 2026-06-20: the owner + dashboard-authed PAYING users reach the Land &
    # Power map via a localStorage X-API-Key / Bearer (no JWT session cookie),
    # so _tier_from_api_key (mcp_dev_keys only) AND the cookie-JWT path above
    # both miss → /api/v1/me/tier reported 'anonymous' and gating.js redacted
    # CAPACITY (MW/GW) in popups for a paying — even enterprise/owner — user
    # ("shows for a microsecond then blocks"). Promote via the proven
    # cross-table resolver (util.tier_gate.resolve_tier: api_keys dual key_hash
    # match incl. raw-stored owner/admin keys + users.plan + Bearer-JWT).
    # Additive + fail-soft — only ever PROMOTES, never downgrades.
    if (not tier) or str(tier).lower() in ('anonymous', 'anon', 'free', ''):
        try:
            from util.tier_gate import resolve_tier as _rt
            _t, _ = _rt(request)
            _name = {0: 'anonymous', 1: 'free', 2: 'developer',
                     3: 'pro', 4: 'enterprise'}.get(int(_t), 'anonymous')
            if TIER_INDEX.get(_name, 0) > TIER_INDEX.get(str(tier or 'anonymous').lower(), 0):
                tier = _name
                _src = 'cross_table_resolver'
        except Exception:
            pass
    try:
        setattr(request, '_cached_tier', tier)
        setattr(request, '_cached_tier_source', _src)
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


# 2026-06-20: vocabulary bridge. get_current_tier() can return any of several
# legacy tier vocabularies — validate_key_tier() speaks 'free'/'paid'/
# 'enterprise'; resolve_tier/_PLAN_TO_TIER can surface 'identified'/'starter'/
# 'dev'/'team'/'metered'/'admin'/'ent'/'research_seed'. gating.js only knows the
# canonical TIER_ORDER (anonymous<free<developer<pro<enterprise<founding) and
# maps anything else to index 0 → it was redacting CAPACITY for a key that
# resolved 'paid' (tier_index came back 0, gating.js tierIndex('paid')=-1→0).
# Normalize to the canonical vocabulary so tier_index is correct everywhere.
_GATE_TIER_NORMALIZE = {
    'anonymous': 'anonymous', 'anon': 'anonymous', '': 'anonymous',
    'free': 'free', 'identified': 'free', 'starter': 'free',
    'dev': 'developer', 'developer': 'developer',
    'paid': 'pro', 'pro': 'pro', 'team': 'pro', 'metered': 'pro',
    'founding': 'founding',
    'enterprise': 'enterprise', 'ent': 'enterprise', 'admin': 'enterprise',
    'research_seed': 'enterprise',
}


@gating_bp.route('/api/v1/me/tier', methods=['GET'])
def my_tier():
    """Return the current user's tier as JSON. Used by gating.js."""
    raw = get_current_tier()
    tier = _GATE_TIER_NORMALIZE.get(str(raw or '').lower().strip(),
                                    str(raw or 'anonymous').lower().strip())
    session_id = (
        request.headers.get('Mcp-Session-Id')
        or request.headers.get('X-Session-Id')
        or request.cookies.get('session_id')
        or ''
    )
    # 2026-08-31: publish WHERE the tier came from. This endpoint was used to
    # diagnose a credential revocation and nearly produced a false "the revoke
    # did not work" conclusion, because an unknown key resolved 'free' exactly
    # like a real one. 'unverified_key' names that case instead of hiding it.
    resp = jsonify({
        'tier': tier,
        'tier_index': TIER_INDEX.get(tier, 0),
        'tier_source': getattr(request, '_cached_tier_source', 'unknown'),
        'session_id': session_id,
        'redeem_url_template': 'https://dchub.cloud/api/v1/redeem/{session_id}',
    })
    # Per-user — must never be edge/browser cached (a cached 'anonymous' was
    # being served to paid users whose request carried no session cookie, since
    # the CF cache key Vary'd on Cookie, not the X-API-Key header). The client
    # (gating.js) also cache-busts, but make the directive unambiguous here.
    resp.headers['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['CDN-Cache-Control'] = 'no-store'
    resp.headers['Vary'] = 'Cookie, Authorization, X-API-Key'
    return resp


def register_jinja_filter(app):
    """Call this from main.py after app creation to register the |gated filter."""
    app.jinja_env.filters['gated'] = gated
