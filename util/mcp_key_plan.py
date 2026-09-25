"""The plan an MCP key's 'paid' tier stands for, when the key is used on REST.

mcp_dev_keys.tier only holds free, paid or enterprise (a CHECK constraint), so
the Stripe webhook writes 'paid' for Developer, Pro and founding alike
(main.py, `_paid_mcp_tier`). Every REST resolver then read 'paid' as Pro, so a
$49 Developer key opened every Pro-only REST route, and protect_data read it
the other way, giving a $99 Pro key Developer's record caps.

Owner decision, 2026-09-22 (frontend#1534): REST serves the plan the key
actually bought, for every key. MCP keeps its behaviour. A request that carries
the MCP server's own X-Internal-Key (server.mjs callAPI forwards the user's key
next to it) resolves exactly as it did before this module existed.

The plan of record, first match wins:
  1. users.plan for the key's email, through the entitlement authority
     (api_tier_gating.resolve_effective_plan), when that is a paid plan;
  2. the newest mcp_conversions.plan_to for that email that is a paid plan;
  3. the newest key-bound subscription paid for this key (client reference
     'k-' + sha256(key), kept in mcp_checkout_payments since 2026-09-21),
     read by its amount against tier_registry's monthly prices;
  4. none of those: 'pro', the mapping every paid key had before. A payer
     whose plan cannot be found keeps what they had, rather than losing it.

Every lookup is read-only and fails soft to the next source.
"""
import hashlib
import threading
import time

MCP_KEY_PREFIXES = ('dch_live_', 'dch_oauth_')

# Step 4. Kept as a name so the tests can say which outcome is the fallback.
FALLBACK_PAID_PLAN = 'pro'


def is_mcp_key(api_key) -> bool:
    """A self-serve key stored in mcp_dev_keys (claim, checkout, OAuth sign-in)."""
    return isinstance(api_key, str) and api_key.startswith(MCP_KEY_PREFIXES)


def from_mcp_server() -> bool:
    """True when this request carries a valid X-Internal-Key, which is how the
    MCP server calls the backend. Outside a request: False."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return False
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(request.headers.get('X-Internal-Key', '')))
    except Exception:
        return False


def _paid_plan_names():
    import tier_registry
    return set(tier_registry.paid_plan_names())


def plan_by_monthly_cents(cents):
    """The paid plan whose monthly price is `cents`, as its API tier name.
    None when no plan, or plans of different API tiers, cost that much."""
    try:
        cents = int(cents)
    except (TypeError, ValueError):
        return None
    import tier_registry
    hits = set()
    for name in tier_registry.paid_plan_names():
        usd = tier_registry.price(name)
        if usd and int(round(float(usd) * 100)) == cents:
            hits.add(tier_registry.api_tier(name))
    return hits.pop() if len(hits) == 1 else None


def _rollback(cur):
    """A failed SELECT aborts a non-autocommit transaction; clear it so the next
    source can still be read on the same connection."""
    try:
        cur.connection.rollback()
    except Exception:
        pass


def plan_of_paid_key(cur, api_key, email) -> str:
    """The plan a 'paid' MCP key bought (see the module docstring for the order).

    `cur` is an open cursor on the database that holds mcp_dev_keys; `email` is
    that row's email (may be None)."""
    paid = _paid_plan_names()
    if email:
        try:
            cur.execute(
                "SELECT plan, subscription_status, role, demoted_at FROM users "
                "WHERE LOWER(email) = LOWER(%s) LIMIT 1", (email,))
            row = cur.fetchone()
            if row:
                from api_tier_gating import resolve_effective_plan
                eff = str(resolve_effective_plan(
                    row[0] or 'free', row[1] or '', row[2] or '', row[3]) or '').lower()
                if eff in paid:
                    return eff
        except Exception:
            _rollback(cur)
        try:
            cur.execute(
                "SELECT LOWER(plan_to) FROM mcp_conversions "
                "WHERE LOWER(user_email) = LOWER(%s) "
                "ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 20", (email,))
            for (plan_to,) in cur.fetchall():
                if plan_to in paid:
                    return plan_to
        except Exception:
            _rollback(cur)
    try:
        cur.execute(
            "SELECT amount_subtotal, amount_total FROM mcp_checkout_payments "
            "WHERE client_reference_id = %s AND mode = 'subscription' "
            "ORDER BY paid_at DESC LIMIT 1",
            ('k-' + hashlib.sha256(api_key.encode()).hexdigest(),))
        row = cur.fetchone()
        if row:
            plan = plan_by_monthly_cents(row[0]) or plan_by_monthly_cents(row[1])
            if plan:
                return plan
    except Exception:
        _rollback(cur)
    return FALLBACK_PAID_PLAN


# ── REST plan of an MCP key, cached briefly ──────────────────────────────────
# routes/tier_gate resolves the caller on every teaser-gated request. Before
# this, an MCP key resolved FREE there with no database read at all, so a cache
# keeps that path from opening a connection per request. 60s bounds how long a
# plan change or a revocation takes to show. Negative results are cached too.
_REST_PLAN_TTL = 60.0
_REST_PLAN_MAX = 4096
_rest_plan_cache = {}
_rest_plan_lock = threading.Lock()


def rest_plan(api_key):
    """validate_api_key's plan for an MCP key, or None for an unknown or
    inactive key. Cached for _REST_PLAN_TTL seconds.

    The answer depends on the caller: a direct REST request gets the plan the
    key bought, the MCP server's call (X-Internal-Key) gets MCP's mapping
    (paid -> Pro). The cache is keyed on that context so one never serves the
    other (the same rule api_data_protection keeps for its own cache)."""
    now = time.time()
    ck = (hashlib.sha256(api_key.encode()).hexdigest()
          + ("|mcp" if from_mcp_server() else "|rest"))
    with _rest_plan_lock:
        hit = _rest_plan_cache.get(ck)
        if hit and hit[1] > now:
            return hit[0]
    plan = None
    try:
        from api_tier_gating import validate_api_key
        info = validate_api_key(api_key)
        plan = (info or {}).get('plan') or None
    except Exception:
        plan = None
    with _rest_plan_lock:
        if len(_rest_plan_cache) >= _REST_PLAN_MAX:
            _rest_plan_cache.clear()
        _rest_plan_cache[ck] = (plan, now + _REST_PLAN_TTL)
    return plan
