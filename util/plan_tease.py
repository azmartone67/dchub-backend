"""The 200 tease a keyless or free caller gets on a route whose numbers are sold.

2026-09-21. The free/anon tighten contract (frontend#1536): a keyless or free
caller gets HTTP 200 with names, slugs, states, ISOs, verdicts, bands and counts,
at most three rows, every sold number null and coordinates at two decimals.
A plan that opens the route gets the full answer.

Who gets what, in order:

  * X-Internal-Key (the MCP server, which applies its own masks) and the
    X-Admin-Key radar: the route's `serve_unchanged` handler, exactly as before.
  * a caller with no credential at all: the route's tease. Its links do not
    depend on the caller, so the body is the public cacheable one.
  * a caller with a credential: api_tier_gating.require_plan(min_plan,
    pack_opens=True), the resolver every REST gate uses, decides. A web
    session, a JWT or an API key at or above `min_plan` gets the full answer;
    a valid key below it that holds unexpired $10-pack credits gets the full
    answer for one credit, burned only on a delivered 200
    (util.rest_pack_access.serve_below_plan). Every other credentialed caller
    gets the tease, with checkout links bound to its own key. Full and keyed
    answers are Cache-Control private, no-store.
  * an unknown key: require_plan's 401, unchanged. A gate error: its 503.

The ladder is built the way checkout_click_tracker.rest_wall_ladder builds its
rungs (tier_registry labels and prices, the pack's own constants, signed /go/c
links), so no price is written here. A key-bound rung carries the key's hash,
`pk-` for the pack and `k-` for a subscription: the refs the Stripe webhook
binds a purchase to. It is never the key itself.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from flask import jsonify, make_response, request

NO_STORE = "private, no-store, max-age=0"
TEASE_CACHE = "public, max-age=60"
TEASE_ROWS = 3

# The credentials require_plan reads, in the places it reads them.
_CREDENTIAL_COOKIES = ("session_token", "dchub_token", "token")
_REFUSALS = ("plan_required", "plan_upgrade_required")


def round2(v):
    """A coordinate at two decimals, or None."""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _is_unchanged_caller() -> bool:
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        got = (request.headers.get("X-Admin-Key", "") or "").split()
        want = (os.environ.get("DCHUB_ADMIN_KEY", "") or "").split()
        if got and want and hmac.compare_digest(got[0], want[0]):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def presented_key() -> str:
    """The API key the request carries, from the places require_plan reads it."""
    key = request.headers.get("X-API-Key") or request.args.get("api_key") or ""
    if not key:
        auth = request.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer ") and auth[7:].strip().startswith("dchub_"):
            key = auth[7:].strip()
    return key.strip()


def carries_credential() -> bool:
    if presented_key():
        return True
    if (request.headers.get("Authorization", "") or "").startswith("Bearer "):
        return True
    return any(request.cookies.get(c) for c in _CREDENTIAL_COOKIES)


def _valid_key(api_key: str) -> bool:
    """True only for a key that resolves. A ref bound to a mistyped key would
    send the purchase to a key nobody holds."""
    if not api_key:
        return False
    # require_plan sets api_key_info only after the key it read (the same one
    # presented_key reads) validated.
    if isinstance(getattr(request, "api_key_info", None), dict):
        return True
    try:
        from api_tier_gating import validate_api_key
        got = validate_api_key(api_key)
        return isinstance(got, dict)
    except Exception:  # noqa: BLE001
        return False


def key_refs(api_key: str) -> tuple[str, str]:
    """(pack ref, subscription ref) for a valid key, else ('', '')."""
    if not _valid_key(api_key):
        return "", ""
    h = hashlib.sha256(api_key.encode()).hexdigest()
    return "pk-" + h, "k-" + h


def _pack_rung(ref: str, label_tail: str) -> dict | None:
    try:
        from routes.checkout_click_tracker import checkout_url
        from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
        if not (PACK10_PRICE_CENTS and PACK10_CREDITS):
            return None
        return {"plan": "pack", "opens": "rest",
                "price": "$%d" % (int(PACK10_PRICE_CENTS) // 100),
                "label": "$%d one-time = %s API credits; %s" % (
                    int(PACK10_PRICE_CENTS) // 100,
                    format(int(PACK10_CREDITS), ","), label_tail),
                "url": checkout_url("metered", ref)}
    except Exception:  # noqa: BLE001
        return None


def _plan_rung(plan: str, ref: str, label_tail: str) -> dict | None:
    try:
        import tier_registry as _tr
        from routes.checkout_click_tracker import checkout_url
        price = _tr.price_display(plan)
        if not price:
            return None
        return {"plan": plan, "opens": "rest", "price": price,
                "label": "%s %s %s" % (_tr.label(plan), price, label_tail),
                "url": checkout_url(plan, ref)}
    except Exception:  # noqa: BLE001
        return None


def ladder(min_plan: str, api_key: str = "") -> dict:
    """upgrade_url + upgrade_options: only rungs that open a `min_plan` route.

    Below Pro the pack leads, then Developer and Pro. On a Pro-only route the
    Pro checkout leads and the pack follows (one credit opens one full answer);
    Developer does not open it, so it is not offered.
    """
    pack_ref, sub_ref = key_refs(api_key)
    rungs = [_pack_rung(pack_ref, "each full answer here uses one")]
    if min_plan != "pro":
        rungs.append(_plan_rung("developer", sub_ref, "opens this endpoint"))
    rungs.append(_plan_rung("pro", sub_ref, "opens this endpoint"))
    rungs = [r for r in rungs if r]
    out = {}
    lead = next((r for r in rungs if r["plan"] == ("pro" if min_plan == "pro" else "pack")),
                rungs[0] if rungs else None)
    if lead:
        out["upgrade_url"] = lead["url"]
    if rungs:
        out["upgrade_options"] = rungs
    out["key_bound"] = bool(pack_ref)
    return out


def envelope(min_plan: str, locked_fields, total_available: int, api_key: str = "") -> dict:
    env = {
        "_gated": True,
        "_preview_only": True,
        "_locked_fields": list(locked_fields),
        "_total_available": int(total_available or 0),
        "_required_plan": min_plan,
    }
    env.update(ladder(min_plan, api_key))
    return env


def _tease(serve_tease, min_plan: str, api_key: str, keyed: bool):
    body, locked, total = serve_tease()
    body.update(envelope(min_plan, locked, total, api_key))
    resp = make_response(jsonify(body), 200)
    resp.headers["Cache-Control"] = NO_STORE if keyed else TEASE_CACHE
    return resp


def gate_or_tease(min_plan: str, serve_full, serve_tease, serve_unchanged=None):
    """Answer a GET on a sold route.

    serve_full()      the full answer for an entitled caller.
    serve_tease()     (body dict, locked field names, total rows available).
    serve_unchanged() what internal and admin callers got before; defaults to
                      serve_full.
    """
    if _is_unchanged_caller():
        return (serve_unchanged or serve_full)()
    if not carries_credential():
        return _tease(serve_tease, min_plan, "", keyed=False)
    from api_tier_gating import require_plan
    resp = make_response(require_plan(min_plan, pack_opens=True)(serve_full)())
    if resp.status_code == 403:
        body = resp.get_json(silent=True) or {}
        if body.get("error") in _REFUSALS:
            return _tease(serve_tease, min_plan, presented_key(), keyed=True)
    if resp.status_code == 200:
        resp.headers["Cache-Control"] = NO_STORE
    return resp
