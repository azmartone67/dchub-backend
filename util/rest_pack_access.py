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
