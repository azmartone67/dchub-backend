"""The keyless / below-plan answer for REST reads whose full answer is paid
(2026-09-21, free/anon tighten P0).

One contract for every route that uses it, so the envelopes match:

  * X-Internal-Key / X-Admin-Key callers, and keys whose principal is 'admin':
    UNCHANGED. The route answers them exactly as it did before this module
    existed (`serve_unchanged`). The MCP server applies its own masks.
  * A caller whose plan opens the route: the full answer, marked
    `Cache-Control: private, no-store`. The plan comes from
    api_tier_gating.get_request_principal(), the same credential parse
    require_plan() runs (login JWT, session cookie, API key including
    dch_live_ / dch_trial_ keys), so a logged-in website user resolves to the
    same plan as their API key.
  * A valid key below that plan holding unexpired $10-pack credits: the full
    answer for one credit, burned only on a delivered 200
    (util/rest_pack_access.serve_below_plan).
  * Everyone else, keyless and free keys: HTTP 200 with a preview. At most
    TEASE_ROWS rows, paid numerics null, coordinates rounded to TEASE_COORD_DP,
    and the envelope below: _gated, _preview_only, _locked_fields,
    _total_available, and a ladder that names only what opens the route.

The ladder never carries price copy of its own: a Developer-plan route takes
it from util/rest_pack_access.plan_or_pack_wall (the pack and Developer, both
of which open it); a Pro-plan route takes checkout_click_tracker.
rest_wall_ladder(opens_on_rest="pro") plus the pack rung, because Developer
does not open a Pro route and the pack does, once per credit.
"""
from flask import make_response, request

TEASE_ROWS = 3
TEASE_COORD_DP = 2
NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
            "CDN-Cache-Control": "no-store",
            "Surrogate-Control": "no-store",
            "Pragma": "no-cache"}

UNCHANGED, FULL, BELOW = "unchanged", "full", "below"


def _internal_or_admin() -> bool:
    try:
        from internal_auth import require_internal_or_admin
        return bool(require_internal_or_admin(request))
    except Exception:  # noqa: BLE001 — unknown means not internal
        return False


def _principal() -> dict:
    try:
        from api_tier_gating import get_request_principal
        return get_request_principal() or {}
    except Exception:  # noqa: BLE001 — fail to anonymous, never to paid
        return {}


def access(min_plan: str) -> str:
    """'unchanged' | 'full' | 'below' for this request against `min_plan`."""
    if _internal_or_admin():
        return UNCHANGED
    tier = (_principal().get("tier") or "anon").lower()
    if tier == "admin":
        return UNCHANGED
    # ★2026-09-22: several credentials -> the highest plan among them
    # (api_tier_gating.request_plan_ceiling); the principal alone names the API
    # key first, so a paying web session beside a free key got the tease.
    try:
        from api_tier_gating import (PLAN_LEVELS, request_credential_count,
                                     request_plan_ceiling)
        if request_credential_count() > 1:
            ceiling = request_plan_ceiling()
            if ceiling != "admin" and PLAN_LEVELS.get(ceiling, 0) > PLAN_LEVELS.get(tier, 0):
                tier = ceiling
    except Exception:  # noqa: BLE001 — the principal's tier stands
        pass
    try:
        import tier_registry
        if tier_registry.satisfies(tier, min_plan):
            return FULL
    except Exception:  # noqa: BLE001
        pass
    return BELOW


def private(resp):
    """The full answer never rides a shared cache."""
    out = make_response(resp)
    for k, v in NO_STORE.items():
        out.headers[k] = v
    return out


def serve(min_plan, serve_full, serve_tease, serve_unchanged=None, kind=None):
    """Route one request to the answer its caller's plan opens.

    Each answer is a zero-argument callable returning what a Flask view
    returns. Only the one that answers is called, so a route does no work for
    an answer it does not send. `kind` is access(min_plan) when the route
    already resolved it (it costs a key lookup)."""
    kind = kind or access(min_plan)
    if kind == UNCHANGED:
        return (serve_unchanged or serve_full)()
    if kind == FULL:
        return private(serve_full())

    def _tease():
        return tease_headers(serve_tease())
    try:
        from api_tier_gating import request_api_key
        key = request_api_key(request)
    except Exception:  # noqa: BLE001
        key = None
    if key:
        from util.rest_pack_access import serve_below_plan
        return serve_below_plan(key, lambda: private(serve_full()), _tease)
    return _tease()


def rounded(value, dp=TEASE_COORD_DP):
    try:
        return None if value is None else round(float(value), dp)
    except (TypeError, ValueError):
        return None


def tease_row(row, keep=(), coords=(), null=()):
    """ALLOWLIST projection of one row: `keep` copied, `coords` rounded,
    `null` present and None. Anything else the row carries is dropped, so a
    column added upstream later cannot ride into the preview."""
    out = {k: row.get(k) for k in keep if k in row}
    for k in coords:
        if k in row:
            out[k] = rounded(row.get(k))
    for k in null:
        out[k] = None
    return out


def _per_caller() -> bool:
    """A keyless caller from a declared partner egress gets /go/c links carrying
    a ref minted for that one request; such a body must not be shared."""
    try:
        from routes.partner_attribution import partner_of_request
        return bool(partner_of_request())
    except Exception:  # noqa: BLE001
        return False


def _ladder(min_plan: str) -> dict:
    if min_plan == "pro":
        from routes.checkout_click_tracker import rest_wall_ladder
        try:
            from routes.partner_attribution import offer_ref_for_request
            ref = offer_ref_for_request(request.path)
        except Exception:  # noqa: BLE001
            ref = ""
        out = rest_wall_ladder(opens_on_rest="pro", ref=ref)
        pack = [o for o in rest_wall_ladder(opens_on_rest="pack", ref=ref).get("upgrade_options", [])
                if o.get("plan") == "pack"]
        if pack:
            out["upgrade_options"] = list(out.get("upgrade_options") or []) + pack
        out["message"] = ("A key on the Pro plan or above opens the full answer over "
                          "REST, and so does any valid key holding pack credits, at one "
                          "credit per full answer.")
        out["required_plan"] = "pro"
        return out
    from util.rest_pack_access import plan_or_pack_wall
    wall = plan_or_pack_wall(min_plan, "free", "plan_required")
    body = (wall[0] if isinstance(wall, tuple) else wall).get_json(silent=True) or {}
    return {k: body[k] for k in ("message", "required_plan", "upgrade_url", "upgrade_options")
            if k in body}


def envelope(min_plan: str, locked_fields, total_available, coords=False) -> dict:
    """The preview markers plus the ladder. Caller-independent unless the caller
    is a partner egress (see _per_caller). `coords=True` states the rounding
    the preview applied to its coordinates."""
    try:
        ladder = _ladder(min_plan)
    except Exception:  # noqa: BLE001 — the preview still ships, with a plan name
        ladder = {"required_plan": min_plan}
    out = {
        "_gated": True,
        "_preview_only": True,
        "_locked_fields": sorted(set(locked_fields)),
        "_total_available": int(total_available or 0),
    }
    if coords:
        out["_coord_precision_dp"] = TEASE_COORD_DP
    msg = ladder.pop("message", None)
    if msg:
        out["_upgrade_cta"] = msg
    out.update(ladder)
    return out


def tease_headers(resp):
    """The preview is the shareable body, except when it carries a per-caller ref."""
    out = make_response(resp)
    if _per_caller():
        for k, v in NO_STORE.items():
            out.headers[k] = v
    return out
