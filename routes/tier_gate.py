"""Phase DDDD (2026-05-16) — REST endpoint tier gates.

Until this phase, all DEVELOPER + PRO upgrade pressure lived ONLY on
the MCP side via mcp_gatekeeper. The REST API was wide open — anyone
could hit /api/v1/transactions, /api/v1/dcpi/scores, /api/v1/bots/whales
without auth. That meant the only path to a paid upgrade was through
an MCP client (Claude Desktop, Cursor, etc) — terrible for the much
larger pool of users hitting the website / API directly.

This module:
  1. Central `require_tier(min_tier)` decorator usable on any Flask
     route. Resolves the caller's tier via:
       - X-API-Key header → mcp_gatekeeper.resolve_tier()
       - dchub_token cookie → user plan via api_keys / users table
       - falls back to Tier.FREE (anonymous)
  2. Conversion-friendly 402 response: structured JSON with
       - current_tier, required_tier, required_tier_price
       - preview: a small "what you would see" sample so the user
         knows what they're missing
       - upgrade_url + upgrade_options: the measured checkout of the plan
         the gate admits (rest_wall_ladder), never a cheaper plan
       - utm tagging on pricing_url so /pricing knows which gate fired

Gate points wired in this PR:
  /api/v1/transactions/export.csv     → DEVELOPER  (net-new endpoint)
  /api/v1/bots/whales                 → PRO        (was public)
  /api/v1/bots/dormant                → PRO        (was public)

(Plus MCP tier moves: compare_sites → PRO, 3 new PRO L&P tools.)
"""

from __future__ import annotations

from functools import wraps
from flask import jsonify, request


from tier_registry import price_display as _canon_price_display

# ★2026-09-21 — THE WALL SELLS THE LADDER. _gate_response led every
# DEVELOPER wall with the retired Starter plan's Stripe link and shipped a
# `stripe_alternates` map whose keys named Starter and two retired Pro
# prices: plans and prices /pricing does not sell. Its links now come from
# routes.checkout_click_tracker.rest_wall_ladder, the measured /go/c
# checkout of the cheapest plan each required tier admits, so this module
# holds no Stripe URL and no price of its own.
#
# A tier absent here gets no paid ladder: IDENTIFIED opens with a trial
# key (below), and ENTERPRISE is sold by contact.
_WALL_PLAN = {
    "DEVELOPER": "developer",
    "PRO":       "pro",
}
_ENTERPRISE_CONTACT_URL = "https://dchub.cloud/enterprise"

# Read from tier_registry, never typed. STARTER is deliberately absent: no
# wall offers it, and a row here is how a retired price reaches a response.
_TIER_PRICE = {
    "FREE":       "$0",
    "IDENTIFIED": "$0 (free with email)",
    "DEVELOPER":  _canon_price_display("developer"),
    "PRO":        _canon_price_display("pro"),
    "ENTERPRISE": "Custom",
}

# ★2026-09-20 — DERIVED. `TEAM` was missing from this table, and this table is
# what require_tier() and caller_is_privileged() compare. A missing name falls
# to `.get(name, 0)` = FREE, so every `require_tier`-decorated route answered a
# paying Team customer with a structured 402 — deals_routes, find_sites,
# brain_rag, sites_capacity, expanded_infrastructure_api, peeringdb_layer,
# public_endpoints, paywall_middleware and main.py among them.
#
# It is the THIRD instance of one defect. The r43-H notes below record FOUNDING
# missing (denied on transactions / market intel / grid data) and RESEARCH_SEED
# missing (denied the NLR institutional contract). Both were repaired by typing
# one more line, which is why there was a third. Typing a fourth is not the fix.
#
# ★ WHY THIS IS NOT tier_registry's `rank`. TIERS.rank is a different scale
# (pro=4, enterprise=5). What this table holds is the ACCESS CEILING the local
# gates compare, and it is derived through `api_tier()` — the registry's own
# answer to "what does this plan get to use" — mapped onto the local levels
# below. founding/team -> pro -> 3 and research_seed -> enterprise -> 4 exactly
# reproduce the hand-typed rows this replaces; `test_the_derived_table_still
# _contains_every_row_it_replaced` pins that, so the derivation cannot quietly
# re-rank an existing tier while it adds the missing one.
_ACCESS_LEVEL = {
    "anonymous":  0,
    "free":       0,
    "identified": 1,
    # Phase BBB-3 — STARTER shares rank with IDENTIFIED for the
    # require_tier decorator (both unlock the same routes). Daily-call
    # quota is enforced elsewhere by tier-specific rate-limit code that
    # CAN tell STARTER (500/day) from IDENTIFIED (200/day). This keeps
    # the new tier from accidentally bumping every other tier's rank
    # comparison and breaking existing gates. DELIBERATE — and preserved
    # by the derivation, because api_tier('starter') is 'starter', not 'pro'.
    "starter":    1,
    "developer":  2,
    # r43-H (2026-05-27): FOUNDING was MISSING here — require_tier('pro')
    # (rank 3) denied founding members (fell to .get default) on
    # transactions / market intel / grid data. Founding is Pro-equivalent,
    # so it shares pro's rank. Now reached via api_tier('founding') == 'pro'.
    "pro":        3,
    # r43-H (2026-05-28): research_seed (NLR custom institutional contract)
    # is enterprise-equivalent. Was missing here → require_tier denied NLR.
    # Now reached via api_tier('research_seed') == 'enterprise'.
    "enterprise": 4,
    "admin":      5,
}

# Exactly the table this replaces. Restored verbatim if the registry cannot be
# imported, so a broken import degrades to today's behaviour and never widens
# a gate: every name it omits still falls to 0.
_FALLBACK_TIER_RANK = {
    "FREE": 0, "IDENTIFIED": 1, "STARTER": 1, "DEVELOPER": 2,
    "PRO": 3, "FOUNDING": 3, "ENTERPRISE": 4, "RESEARCH_SEED": 4,
}


def _derive_tier_rank():
    """Map every plan the registry knows onto the local access-ceiling scale."""
    try:
        from tier_registry import TIERS, api_tier
    except Exception:
        return dict(_FALLBACK_TIER_RANK)
    out = {}
    for name in TIERS:
        level = _ACCESS_LEVEL.get(str(api_tier(name) or "").strip().lower())
        if level is not None:
            out[str(name).strip().upper()] = level
    # A registry that resolves nothing is not usable; keep what worked before.
    if not out or any(k not in out for k in _FALLBACK_TIER_RANK):
        return dict(_FALLBACK_TIER_RANK)
    return out


_TIER_RANK = _derive_tier_rank()


def _resolve_caller_tier() -> tuple[str, dict]:
    """Returns (tier_name, debug_info). Best-effort across multiple auth
    surfaces; defaults to FREE.

    ★2026-09-20 — this used to promise "one of FREE/IDENTIFIED/DEVELOPER/PRO/
    ENTERPRISE". IT DOES NOT. The JWT branch below appends
    `(_plan.upper(), "cookie:jwt")` straight from the signed `plan` claim, so
    the caller receives whatever `users.plan` holds, uppercased — TEAM,
    STARTER, FOUNDING and RESEARCH_SEED all reach a consumer this way. Three
    callers (routes/radar.py, routes/deal_autopsy.py,
    routes/grid_transition_radar.py) had each typed a small uppercase set off
    the old promise and teased paying customers whose plan was not in it.
    Compare against tier_registry, not against this list."""
    debug = {}
    # r-tiermax (2026-06-30): collect EVERY tier signal (api-key + logged-in
    # cookie/JWT) and return the HIGHEST, instead of short-circuiting on the
    # api-key. A paying PRO user whose key tier lags (the billing tier-gap:
    # Stripe sets users.plan=pro but the key's rate_limit_tier stays free) was
    # stranded on the FREE teaser even while logged in as PRO — the api-key
    # path returned first and the verified PRO cookie was never consulted.
    # Max is safe: the cookie is a signature-VERIFIED JWT (can't be forged up),
    # and require_tier needs only the access ceiling (per-key call quota is
    # enforced separately).
    candidates = []  # (TIER_NAME_UPPER, source)

    # 1. X-API-Key path — delegate to mcp_gatekeeper resolver
    #
    # frontend#1534 (2026-09-22): mcp_gatekeeper.resolve_tier knows dch_trial_
    # and dchub_ keys only, so every self-serve MCP key (dch_live_, dch_oauth_)
    # resolved FREE here, and a paying Pro key got the free teaser on every
    # route gated through caller_is_privileged (deal $ and MW on /api/deals,
    # /api/v1/deals and /api/v1/transactions among them). A direct REST caller's
    # MCP key now resolves through validate_api_key, the plan the key bought.
    #
    # ★2026-09-25 (live screen, Claude keyed): the MCP server's own calls
    # (valid X-Internal-Key) used to take the mcp_gatekeeper path here, which
    # knows no dch_live_/dch_oauth_ key, so every one read FREE. "Privileged by
    # signal 2 of caller_is_privileged either way" held only for routes that
    # ask caller_is_privileged; site_selection_canvas, deal_autopsy,
    # grid_transition_radar and radar compare the tier NAME, so a paying key
    # calling through MCP got the locked teaser. The MCP server's call now
    # resolves the key as well, through validate_api_key, which under the
    # internal key keeps MCP's own mapping (paid -> Pro; util/mcp_key_plan).
    #
    # ★2026-09-22 — every key the request presents, not only the first header.
    # Only X-API-Key and ?api_key= were read here, so a paid key sent as
    # `Authorization: Bearer dch_live_...` (what an HTTP client library sends)
    # resolved FREE: the cookie branch below tries a Bearer that is not a JWT
    # against api_keys only, and MCP keys live in mcp_dev_keys. A free key in
    # the header also hid a paid one in ?api_key= or the Bearer. The shapes are
    # api_tier_gating.request_api_keys, the extraction rule every
    # api_tier_gating resolver reads; each key resolves and the highest wins.
    try:
        from api_tier_gating import request_api_keys
        api_keys = request_api_keys(request)
    except Exception as e:
        debug["api_key_extract_err"] = str(e)[:80]
        api_keys = [request.headers.get("X-API-Key") or request.args.get("api_key")]
    for api_key in api_keys:
        if not api_key:
            continue
        try:
            from util.mcp_key_plan import is_mcp_key, from_mcp_server, rest_plan
            if is_mcp_key(api_key):
                _plan = rest_plan(api_key)
                candidates.append(((_plan or "free").upper(),
                                   "x-api-key:mcp_dev_keys"
                                   + (":mcp_server" if from_mcp_server() else "")))
            else:
                from mcp_gatekeeper import resolve_tier, TIER_NAME
                tier_enum = resolve_tier(api_key)
                candidates.append((TIER_NAME.get(tier_enum, "FREE").upper(), "x-api-key"))
        except Exception as e:
            debug["api_key_resolve_err"] = str(e)[:80]

    # 2. dchub_token cookie path — JWT first, then DB lookup.
    token = request.cookies.get("dchub_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        # 2a. JWT path (FIX 2026-06-06): the frontend login stores a signed
        # HS256 JWT as dchub_token whose payload carries the `plan` claim
        # (main.py jwt.encode {..., 'plan': plan, 'exp': ...}). tier_gate
        # previously only matched RAW tokens against api_keys / users
        # .session_token, so a JWT matched nothing and EVERY JWT-logged-in
        # PRO user fell through to FREE → upgrade gate on every page that
        # uses _resolve_caller_tier ("gated even though I'm pro"). Decode +
        # VERIFY signature + exp with JWT_SECRET and trust the signed plan
        # claim — the same contract dchub_me.py and paywall_middleware use.
        # Expired / forged / unsigned tokens raise and fall through to FREE.
        if token.count(".") == 2:
            try:
                import os as _os, jwt as _pyjwt
                _secret = _os.environ.get("JWT_SECRET")
                if _secret:
                    _p = _pyjwt.decode(token, _secret, algorithms=["HS256"])
                    _plan = (_p.get("plan") or _p.get("tier") or "").strip()
                    if _plan:
                        candidates.append((_plan.upper(), "cookie:jwt"))
            except Exception as e:
                debug["jwt_decode_err"] = str(e)[:80]
        # DB fallback — only when we don't already hold a PRO+ signal (keeps the
        # common logged-in path DB-free while still catching a plan that lives
        # only in api_keys.rate_limit_tier).
        _have = max([_TIER_RANK.get(t, 0) for t, _ in candidates], default=0)
        if _have < _TIER_RANK.get("PRO", 3):
            try:
                import os, psycopg2, hashlib
                db = os.environ.get("DATABASE_URL")
                if db:
                    with psycopg2.connect(db, sslmode="require", connect_timeout=3) as c:
                        with c.cursor() as cur:
                            # Token can be a session token OR a raw api key.
                            # Try api_keys table first (most common case).
                            # 2026-09-22: the whole key, sha256 or raw (partner
                            # keys are stored raw), on an active row — the
                            # X-API-Key path's match. Not its first 16 chars.
                            try:
                                cur.execute("""
                                    SELECT COALESCE(rate_limit_tier, 'free')
                                      FROM api_keys
                                     WHERE key_hash IN (%s, %s)
                                       AND (is_active = 1 OR is_active IS NULL)
                                     LIMIT 1
                                """, (hashlib.sha256(token.encode()).hexdigest(), token))
                                r = cur.fetchone()
                                if r and r[0]:
                                    candidates.append((str(r[0]).upper(), "cookie:api_keys"))
                            except Exception:
                                pass
                            # users is deliberately not consulted here. The
                            # login credential is the signed JWT handled in 2a;
                            # nothing writes a users.session_token (the column
                            # is not in db_persistence.CRITICAL_TABLES), and a
                            # users.id is an identifier, not a credential.
            except Exception as e:
                debug["cookie_resolve_err"] = str(e)[:80]

    # Highest tier across all signals wins (see r-tiermax rationale above).
    if candidates:
        _b = max(candidates, key=lambda cc: _TIER_RANK.get(cc[0], 0))
        debug["candidates"] = [f"{t}:{s}" for t, s in candidates]
        return _b[0], {"source": _b[1], **debug}
    return "FREE", {"source": "anonymous", **debug}


def caller_meets(min_tier: str) -> tuple[bool, str]:
    """(admitted, tier) for a hard wall that sells `min_tier` and up.

    ★2026-09-22 — the export walls (transactions export.csv, the Land & Power
    export.csv and export.geojson) read _resolve_caller_tier alone, and two
    paying callers still got the 402 after it read every key:
      - the MCP server's export_dataset call. It forwards the user's key next
        to its X-Internal-Key, and that call resolves FREE above by design
        (frontend#1534 keeps it for caller_is_privileged), while MCP reads
        every paid key as Pro and admits it to the tool;
      - a login whose plan changed after sign-in: the JWT branch above trusts
        the claim minted at sign-in.
    Below the wall the caller is lifted to api_tier_gating's
    request_plan_ceiling(): the highest plan any verified credential on the
    request resolves to, through validate_api_key and get_user_plan, which
    every @require_plan gate reads. It only raises, and is read only when the
    caller would otherwise be walled, so an admitted caller pays nothing for it.
    Ranks are _TIER_RANK's, so a plan a wall never named (FOUNDING, TEAM) is
    admitted by its rank rather than walled by a missing name. A `min_tier`
    the ranks do not know admits nobody: a misspelt wall stays shut."""
    need = _TIER_RANK.get(str(min_tier or "").upper())
    if need is None:
        need = max(_TIER_RANK.values(), default=0) + 1
    tier, _ = _resolve_caller_tier()
    tier = str(tier or "FREE").upper()
    if _TIER_RANK.get(tier, 0) >= need:
        return True, tier
    try:
        from api_tier_gating import request_plan_ceiling
        ceiling = str(request_plan_ceiling() or "").upper()
    except Exception:
        ceiling = ""
    if _TIER_RANK.get(ceiling, 0) > _TIER_RANK.get(tier, 0):
        tier = ceiling
    return _TIER_RANK.get(tier, 0) >= need, tier


def caller_is_privileged(min_tier: str = "IDENTIFIED") -> bool:
    """True if the caller may receive FULL data on a *teaser-gated* data
    endpoint. Unlike `require_tier` (a hard 402), this is for endpoints we
    want to keep returning 200 for everyone but show only a teaser to
    anonymous scrapers. It combines tier rank with internal/browser trust
    signals so we never teaser our own server-to-server calls, the brain
    radar, logged-in web users, or the Land & Power map.

    Trusted signals (any one → True):
      1. caller tier rank >= min_tier (paid/identified API key or cookie)
      2. internal MCP key (X-Internal-Key) or admin radar key (X-Admin-Key)
      3. internal server-to-server call (loopback remote_addr or DCHub UA)
      4. real dchub.cloud browser (r43-G session cookie or same-origin GET)

    Returns False only for an unknown anonymous external caller."""
    # 1. tier rank
    try:
        tier, _ = _resolve_caller_tier()
        if _TIER_RANK.get(tier.upper(), 0) >= _TIER_RANK.get(min_tier.upper(), 0):
            return True
    except Exception:
        pass
    # 2. internal / admin keys
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
            return True
    except Exception:
        pass
    try:
        import os as _os, hmac as _hmac
        _ac = (request.headers.get("X-Admin-Key", "") or "").split()
        _ac = _ac[0] if _ac else ""
        _ae = (_os.environ.get("DCHUB_ADMIN_KEY", "") or "").split()
        _ae = _ae[0] if _ae else ""
        if _ac and _ae and _hmac.compare_digest(_ac, _ae):
            return True
    except Exception:
        pass
    # 3. internal server-to-server call — loopback only. Public traffic
    #    arrives via the CF→Railway proxy, so remote_addr is the proxy IP,
    #    never loopback; only true self-calls (the grid-intel headroom
    #    fetch to 127.0.0.1:8080) are loopback. We do NOT trust the
    #    User-Agent ("DCHub…") — a CF client can forge any UA.
    #    '::ffff:127.0.0.1' is what a dual-stack ([::]) listener reports for
    #    an IPv4 loopback connect — same form free_tier_gate learned in #2018.
    try:
        if request.remote_addr in ("127.0.0.1", "::1", "localhost",
                                   "::ffff:127.0.0.1"):
            return True
    except Exception:
        pass
    # 4. real dchub.cloud browser: r43-G signed session cookie ONLY.
    #    Origin/Referer is intentionally NOT trusted — the CF worker injects
    #    a dchub.cloud Referer on every proxied request, so it's true for
    #    scrapers too (that injection leaked the gated datasets to anon curl).
    try:
        from routes.session_cookie import validate_cookie
        if validate_cookie():
            return True
    except Exception:
        pass
    return False


# ── r-intl-gate (2026-07-10) — shared gate for international ISO /snapshot ──
# The international grid snapshot routes (routes/iso_*_*.py, grid_snapshot.py,
# iso_snapshot.py) shipped WIDE OPEN: full per-zone/per-country fuel mix,
# generation mix, installed capacity and demand series to any anonymous caller
# — while the US path (main.py phase19b_grid_intelligence) has gated the same
# class of data since ZZZZZ-round12. Root cause: gating is opt-in per route, so
# every new snapshot blueprint defaulted to open. This one helper closes the
# whole family and is the pattern any FUTURE snapshot route must call.
#
# Privileged callers (paid/identified key, internal server-to-server, loopback,
# or a real dchub.cloud browser via the r43-G session cookie — i.e. the live
# grid dashboard) get the FULL payload unchanged. Anonymous external scrapers
# get identity + a single headline demand number, with the heavy fuel/zone
# detail redacted behind a free-dev-key CTA (same taste-preserving teaser shape
# the US grid path uses — a 200, never a hard error, so agents still convert).
_INTL_SNAPSHOT_REDACT = frozenset({
    "metrics", "zones", "generation_mix", "tsos", "installed_capacity_mw",
    "renewable_pct", "annual_generation_twh", "by_iso", "facilities",
    "dcpi", "pipeline", "per_tso", "per_tso_status", "fuel_mix",
})


def _extract_headline_demand(payload: dict):
    """Pull ONE demand number to keep as the free headline (parity with the US
    grid path, which exposes demand_mw free). Best-effort; None if not found."""
    for k in ("aggregate_demand_mw", "demand_mw", "total_demand_mw"):
        v = payload.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 1)
    m = payload.get("metrics")
    if isinstance(m, dict):
        for k, v in m.items():
            if "demand" in str(k).lower():
                val = v.get("value") if isinstance(v, dict) else v
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return round(float(val), 1)
    return None


def gate_intl_snapshot(payload, tier: str = "IDENTIFIED"):
    """Gate an international ISO /snapshot payload for anonymous callers.

    Returns (payload_out, was_gated). Privileged callers get (payload, False)
    unchanged. Anonymous callers get a redacted headline dict + free-key CTA.
    Non-dict payloads (error bodies) pass through untouched."""
    if not isinstance(payload, dict):
        return payload, False
    # Never gate an upstream error/unavailable body — let it surface as-is.
    if payload.get("error"):
        return payload, False
    if caller_is_privileged(tier):
        return payload, False
    keep = {k: v for k, v in payload.items() if k not in _INTL_SNAPSHOT_REDACT}
    demand = _extract_headline_demand(payload)
    keep["demand_mw"] = demand
    keep["gated"] = True
    keep["tier_required"] = "identified"
    keep["metrics"] = "<gated: free dev key unlocks full fuel mix + per-zone detail>"
    keep["message"] = (
        f"You got the headline for {payload.get('iso') or 'this grid'}"
        + (f" ({demand} MW demand)." if demand is not None else ".")
        + " Full generation mix, per-zone/per-country breakdown and demand"
          " series require a free dev key (email-only signup, no credit card)."
    )
    keep["agent_action"] = {
        "type": "claim_free_key", "method": "POST",
        "url": "https://dchub.cloud/api/v1/keys/claim",
        "headers": {"Content-Type": "application/json"},
        "body": {"client_name": "<your agent identifier>"},
        "then": "Retry this GET with header 'X-API-Key: <api_key>'",
    }
    keep["upgrade_url"] = ("https://dchub.cloud/signup?next=/onboarding"
                           "&utm_source=intl_grid_snapshot")
    keep["enterprise_url"] = "https://dchub.cloud/enterprise"
    return keep, True


def jsonify_gated_snapshot(payload, status: int = 200, tier: str = "IDENTIFIED"):
    """Convenience wrapper: gate + jsonify + tag. Snapshot routes call this
    instead of `jsonify(payload), status`."""
    out, was_gated = gate_intl_snapshot(payload, tier=tier)
    resp = jsonify(out)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    if was_gated:
        resp.headers["X-Tier-Gated"] = "true"
        resp.headers["Cache-Control"] = "public, max-age=60"
    return resp, status


def _gate_response(current_tier: str, required_tier: str,
                   gate_id: str, preview: dict | None = None):
    """Standardized 402 response: a preview of what's behind the wall plus
    the checkout of the plan the gate admits (upgrade_url, upgrade_options).

    Phase NN (2026-05-17) — Funnel rescue. The diagnostic showed 7,769
    paywall hits / 0 conversions on auto-trial keys because agents
    don't parse JSON bodies of 402 responses — they treat 402 as a
    hard error. So a minted trial key ALSO rides the HTTP headers
    (X-Trial-Key, X-Trial-Key-Expires, Retry-After) so any agent
    using standard HTTP middleware can detect + retry without parsing
    the body. Adds a WWW-Authenticate header pointing at the claim
    endpoint per RFC 7235 so smart clients can self-onboard.
    ★ A key is minted only where it opens the gate (IDENTIFIED); see below.
    """
    required_upper = required_tier.upper()
    pricing_url = (f"https://dchub.cloud/pricing"
                   f"?utm_source=rest_gate&utm_medium={gate_id}"
                   f"&utm_campaign={required_upper.lower()}_upgrade")

    # ★2026-09-21 — the ladder, not a price list. Only the plan this tier's
    # gate admits is offered, through its measured /go/c checkout. What this
    # replaced led with the retired Starter link and a `stripe_alternates` map
    # naming Starter and two retired Pro prices, none of which /pricing sells.
    ladder = {}
    paid_plan = _WALL_PLAN.get(required_upper)
    if paid_plan:
        try:
            from routes.checkout_click_tracker import rest_wall_ladder
            ladder = rest_wall_ladder(opens_on_rest=paid_plan) or {}
        except Exception:  # noqa: BLE001 — a wall still answers without links
            ladder = {}
    if ladder.get("upgrade_url"):
        upgrade_url = ladder["upgrade_url"]
    elif required_upper == "ENTERPRISE":
        upgrade_url = _ENTERPRISE_CONTACT_URL
    else:
        upgrade_url = pricing_url

    # A trial key resolves IDENTIFIED (mcp_gatekeeper.resolve_tier), so it
    # opens an IDENTIFIED gate and nothing above it.
    # ★2026-09-21: this also minted one for a DEVELOPER gate — every keyless
    # hit on /api/v1/transactions/export.csv got a trial key, an X-Trial-Key
    # header and the line "Retry with header X-API-Key: <key> and this call
    # will succeed". The retry came back 402: the key could not open what the
    # wall promised it would. A gate a trial key cannot open mints nothing.
    auto_trial = None
    if current_tier == "FREE" and required_upper == "IDENTIFIED":
        try:
            from routes.auto_trial import mint_trial_for_request
            t = mint_trial_for_request(request, gate_id)
            if t.get("ok"):
                auto_trial = t
        except Exception:
            pass

    payload = {
        "error":             "upgrade_required",
        "gate":              gate_id,
        "current_tier":      current_tier,
        "required_tier":     required_upper,
        "required_tier_price": _TIER_PRICE.get(required_upper, ""),
        "preview":           preview or {},
        "message": (f"This endpoint requires {required_upper} tier "
                    f"({_TIER_PRICE.get(required_upper)}). You're on "
                    f"{current_tier}. The preview field shows a sample "
                    f"of what's behind the gate."),
        "upgrade_url":       upgrade_url,
        "pricing_url":       pricing_url,
        # ★ Not offered on these walls: a key from /api/v1/keys/claim
        # (dch_live_) resolves FREE in mcp_gatekeeper.resolve_tier, the
        # resolver _resolve_caller_tier reads, so claiming one opens none of
        # them. Kept, as null, for clients that read the field.
        "claim_free_key_first": None,
    }
    if paid_plan and ladder.get("upgrade_url"):
        payload["upgrade_options"] = ladder.get("upgrade_options") or []
        # Kept for clients that read it: the same measured /go/c checkout as
        # upgrade_url, never a raw Stripe link and never a cheaper plan than
        # the gate admits.
        payload["stripe_checkout"] = ladder["upgrade_url"]
    if auto_trial:
        _calls = auto_trial.get("daily_calls")
        payload["auto_trial_key"]         = auto_trial.get("api_key")
        payload["auto_trial_expires_at"]  = auto_trial.get("expires_at")
        payload["auto_trial_daily_calls"] = _calls
        # The allowance is the minted row's own, not a typed number: this line
        # said "200 calls/day" beside an auto_trial_daily_calls of 15.
        payload["message"] = (
            f"Trial key minted: `{auto_trial.get('api_key')}`"
            + (f" ({_calls} calls/day)" if _calls else "")
            + f". Retry with header `X-API-Key: {auto_trial.get('api_key')}`."
        )

    resp = jsonify(payload)
    resp.headers["Access-Control-Allow-Origin"]    = "*"
    resp.headers["Access-Control-Expose-Headers"]  = (
        "X-Trial-Key, X-Trial-Key-Expires, Retry-After, Link, WWW-Authenticate"
    )
    # A minted key is this caller's alone; the wall without one is the same
    # for every caller.
    resp.headers["Cache-Control"] = ("private, no-store, max-age=0" if auto_trial
                                     else "private, max-age=0, must-revalidate")
    # Phase NN — HTTP-header trial-key delivery so middleware can grab
    # the key without parsing the body. Standard HTTP retry-loop
    # patterns will pick this up automatically.
    if auto_trial and auto_trial.get("api_key"):
        resp.headers["X-Trial-Key"]         = auto_trial.get("api_key")
        if auto_trial.get("expires_at"):
            resp.headers["X-Trial-Key-Expires"] = str(auto_trial.get("expires_at"))
        resp.headers["Retry-After"]         = "0"
        # RFC 8288 Link header pointing at the redemption endpoint
        resp.headers["Link"] = (
            '<https://dchub.cloud/api/v1/keys/auto-trial/redeem>; '
            'rel="api-key-redemption"; '
            'type="application/json"'
        )
    # RFC 7235 WWW-Authenticate signals an auth challenge. It named
    # /api/v1/keys/claim on every wall; a key from there opens none of them
    # (see claim_free_key_first above), so a paid gate names its checkout and
    # an IDENTIFIED gate relies on the trial key it just minted.
    resp.headers["WWW-Authenticate"] = (
        f'X-API-Key realm="dchub.cloud", '
        + (f'upgrade="{upgrade_url}", ' if paid_plan else '')
        + f'tier="{required_upper}"'
    )
    return resp, 402


# ── Phase NNNN (2026-05-16) — REST rate-limit decorator ──────────
# Per-key bucket, in-process. Crude but effective for the L+P
# endpoints' expected volume; if it becomes a hot spot we move to
# Redis/DB-backed counters in a future phase.
import time as _time
_RL_BUCKETS: dict[str, list[float]] = {}
_RL_MAX_BUCKET = 5000  # safety cap on bucket dict size

def _rl_check(key: str, per_minute: int) -> tuple[bool, int]:
    """Returns (allowed, retry_in_seconds)."""
    now = _time.time()
    window = 60.0
    bucket = _RL_BUCKETS.setdefault(key, [])
    bucket[:] = [t for t in bucket if (now - t) < window]
    if len(bucket) >= per_minute:
        oldest = bucket[0]
        retry_in = int(window - (now - oldest)) + 1
        return False, max(1, retry_in)
    bucket.append(now)
    # Crude eviction so the dict can't grow unbounded — if we exceed
    # the cap, drop the oldest bucket entirely (5000 unique keys/min
    # is a LOT of unique callers; benign collateral)
    if len(_RL_BUCKETS) > _RL_MAX_BUCKET:
        try:
            oldest_key = min(_RL_BUCKETS, key=lambda k: (_RL_BUCKETS[k][0] if _RL_BUCKETS[k] else now))
            _RL_BUCKETS.pop(oldest_key, None)
        except Exception: pass
    return True, 0


def rate_limit(per_minute: int = 60, key_fn=None):
    """Flask decorator. Returns 429 if caller exceeds per_minute calls
    within a 60s sliding window. Key derivation: by api_key (or cookie
    token) if present, else by IP. Override with key_fn(request)→str."""
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*a, **kw):
            # r58b (2026-06-01): exempt trusted internal callers from the rate
            # limiter. The brain-radar self-probes (X-Internal-Key, hitting
            # localhost:8080) were getting 429'd on /redeem/funnel-stats,
            # /reports/monthly, /freshness/radar, /ai-citations/history,
            # /brain/memory/stats — server-to-server traffic, not abuse.
            # Fail-open: any error falls through to normal rate limiting.
            try:
                from internal_auth import is_valid_internal_key
                if (is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
                        or request.remote_addr in ("127.0.0.1", "::1", "localhost",
                                                   "::ffff:127.0.0.1")):
                    return fn(*a, **kw)
            except Exception:
                pass
            # Build a stable key per caller
            if key_fn:
                try: key = str(key_fn(request))
                except Exception: key = "anon"
            else:
                key = (request.headers.get("X-API-Key")
                       or request.cookies.get("dchub_token")
                       or request.headers.get("CF-Connecting-IP")
                       or request.remote_addr or "anon")
            # Namespace by route so a per-route 60/min doesn't share
            # quota with another route on the same key
            key = f"{fn.__name__}:{key[:32]}"
            ok, retry_in = _rl_check(key, per_minute)
            if not ok:
                resp = jsonify({
                    "error":     "rate_limited",
                    "endpoint":  fn.__name__,
                    "limit":     f"{per_minute}/min",
                    "retry_in":  retry_in,
                    "message":   (f"Too many requests. Limit: {per_minute}/min. "
                                  f"Retry in {retry_in}s."),
                    "upgrade_hint": ("Higher tier = higher cap. "
                                      "See https://dchub.cloud/pricing"),
                })
                resp.headers["Retry-After"] = str(retry_in)
                resp.headers["X-RateLimit-Limit"] = str(per_minute)
                return resp, 429
            return fn(*a, **kw)
        return wrapper
    return deco


def require_tier(min_tier: str, gate_id: str | None = None,
                 preview_fn=None):
    """Flask route decorator. Returns a structured 402 if the caller's
    tier is below min_tier.

    Args:
        min_tier:  one of FREE/IDENTIFIED/DEVELOPER/PRO/ENTERPRISE
        gate_id:   short slug for analytics (defaults to view-fn name)
        preview_fn: optional callable(request) → dict — small sample
                   payload included in the 402 so the user sees what
                   they would get. Compute cheaply; runs on every
                   blocked request.
    """
    min_rank = _TIER_RANK.get(min_tier.upper(), 0)
    def deco(fn):
        slug = gate_id or fn.__name__
        @wraps(fn)
        def wrapper(*a, **kw):
            tier, _ = _resolve_caller_tier()
            tier_rank = _TIER_RANK.get(tier.upper(), 0)
            if tier_rank < min_rank:
                preview = {}
                if preview_fn:
                    try: preview = preview_fn(request) or {}
                    except Exception: pass
                return _gate_response(tier, min_tier, slug, preview)
            return fn(*a, **kw)
        return wrapper
    return deco
