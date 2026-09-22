"""What a VALID REST key below a list's plan gets (frontend#1534, 2026-09-21).

/pricing sells the $10 pack as "1,000 full-depth API calls" and Developer as
full result sets. Over REST neither opened /api/v1/facilities: every key below
Pro got a 403 with no rows, while a keyless caller got the preview. The
before_request hook that injects a trial key for AI-agent user agents
(auto_issue_key_for_ai_agents) turned those agents' keyless calls into the
same 403.

For a key that require_plan() refused as `plan_upgrade_required`:

  * unexpired pack credits open the full answer. Exactly one credit is burned
    per DELIVERED 200: never for a 4xx, 429 or 5xx, and never before the answer
    exists. If the burn fails (a concurrent call took the last credit, or the
    ledger is down), the caller gets the preview instead: no credit, no data.
  * every other valid key gets exactly what a keyless caller gets.

A 401 (unknown key) or 503 (gate down) is not a plan refusal. Those answer
something about the credential, and the route passes them through unchanged.

A pack is not a tier: pack keys resolve to 'free' everywhere. So the pack path
runs protect_data as tier 'pack', whose caps api_data_protection pins beside
Developer's.

Routes gated by api_tier_gating.require_plan(min_plan, pack_opens=True) get
the same treatment through serve_below_plan, and their refusals answer with
plan_or_pack_wall (2026-09-21: /api/v1/pipeline, /api/grid/fuel-mix,
/api/energy/prices/<state>, the keyed operations of the public spec).
"""
from flask import g, make_response

PACK_TIER = "pack"
PLAN_REFUSAL = "plan_upgrade_required"


def _status_and_body(resp):
    status, body_resp = None, resp
    if isinstance(resp, tuple):
        body_resp = resp[0]
        if len(resp) > 1 and isinstance(resp[1], int):
            status = resp[1]
    if status is None:
        status = getattr(body_resp, "status_code", 200)
    try:
        body = body_resp.get_json(silent=True)
    except Exception:  # noqa: BLE001 — a non-JSON body is simply not a refusal
        body = None
    return status, body


def is_plan_upgrade_refusal(resp) -> bool:
    """require_plan()'s 403 for a valid key whose plan is below the route's."""
    status, body = _status_and_body(resp)
    return status == 403 and isinstance(body, dict) and body.get("error") == PLAN_REFUSAL


def serve_below_plan(api_key, serve_full, serve_preview):
    """`serve_full` is the route's full handler, already wrapped in protect_data.
    `serve_preview` is exactly the handler a keyless caller gets."""
    from util.location_meter import pack_active
    if not api_key or not pack_active(api_key=api_key):
        return serve_preview()
    g.user_tier = PACK_TIER
    resp = make_response(serve_full())
    if resp.status_code != 200:
        return resp
    from routes.mcp_conversion_plays import consume_credits
    burn = consume_credits(api_key, None, 1) or {}
    if not burn.get("ok"):
        return serve_preview()
    resp.headers["X-DCHub-Access"] = PACK_TIER
    resp.headers["X-DCHub-Credits-Remaining"] = str(int(burn.get("remaining") or 0))
    return resp


NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
            "Surrogate-Control": "no-store", "Pragma": "no-cache"}
PRICING_URL = "https://dchub.cloud/pricing"


def plan_or_pack_wall(min_plan, current_plan, error_code):
    """The 403 of a route gated by require_plan(min_plan, pack_opens=True).

    It offers only what opens the route over REST: the $10 pack (one credit per
    full answer) and Developer, each marked `opens: "rest"`
    (checkout_click_tracker.rest_wall_ladder in its pack mode). The generic
    paywall it replaces offered Starter and the free key, neither of which
    opens these routes, and Developer on routes Developer did not open.

    Facts only: the partner catalogues that relay this body verbatim reach
    their customers' agents. A keyless caller from a declared partner egress
    gets links carrying a ref recorded to the partner.
    """
    from flask import jsonify, request
    from routes.checkout_click_tracker import rest_wall_ladder
    try:
        from routes.partner_attribution import offer_ref_for_request
        ref = offer_ref_for_request(request.path)
    except Exception:  # noqa: BLE001 — attribution never costs the wall
        ref = ""
    try:
        import tier_registry as _tr
        plan_label = _tr.label(min_plan) or min_plan.title()
    except Exception:  # noqa: BLE001
        plan_label = min_plan.title()
    body = {
        "success": False,
        "error": error_code,
        "message": ("A key on the {} plan or above opens this endpoint over REST, and "
                    "so does any valid key holding pack credits, at one credit per "
                    "full answer.".format(plan_label)),
        "required_plan": min_plan,
        "current_plan": current_plan or "free",
        "pricing_url": PRICING_URL,
    }
    body.update(rest_wall_ladder(opens_on_rest=PACK_TIER, ref=ref))
    return jsonify(body), 403, NO_STORE
