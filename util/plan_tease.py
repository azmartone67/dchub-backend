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

import datetime
import hashlib
import hmac
import os

from flask import g, jsonify, make_response, request

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


def _auto_issued_key() -> str:
    """The trial key main.auto_issue_key_for_ai_agents put on THIS request.

    For an AI-agent user agent with no credential (Claude, ChatGPT, Perplexity,
    and the Claude desktop app's own browser) that hook mints or reuses a
    dch_trial_ key and writes it into the request environ as X-API-Key, so it
    reads exactly like a key the caller sent. The caller sent none.
    """
    try:
        from flask import g
        return getattr(g, "auto_issued_key", None) or ""
    except Exception:  # noqa: BLE001
        return ""


def _injected_key_only() -> bool:
    """True when the only credential on the request is the auto-issued key."""
    key = presented_key()
    if not key or key != _auto_issued_key():
        return False
    if (request.headers.get("Authorization", "") or "").startswith("Bearer "):
        return False
    return not any(request.cookies.get(c) for c in _CREDENTIAL_COOKIES)


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
    # ★ 2026-09-22. A key the server injected is not a credential the caller
    # presented. Sent through require_plan it answered 401 invalid_api_key
    # whenever the reused trial no longer validated: measured live with the
    # Claude desktop browser's user agent, with no key at all, on the canvas
    # and on quick-score. So it gets the keyless tease. The response still
    # carries that caller's X-DC-Auto-Issued-Key header, so it is never stored
    # for anyone else (private, no-store), and its links are not bound to a
    # key the caller never chose.
    if _injected_key_only():
        return _tease(serve_tease, min_plan, "", keyed=True)
    from api_tier_gating import require_plan
    resp = make_response(require_plan(min_plan, pack_opens=True)(serve_full)())
    if resp.status_code == 403:
        body = resp.get_json(silent=True) or {}
        if body.get("error") in _REFUSALS:
            return _tease(serve_tease, min_plan, presented_key(), keyed=True)
    if resp.status_code == 200:
        resp.headers["Cache-Control"] = NO_STORE
    return resp


# ── Land & Power: the details are Pro (owner, 2026-09-22) ───────────────────
# Land & Power is sold as Pro, and nothing below Pro opens its details. Every
# Land & Power route answers through lp_gate():
#
#   * X-Internal-Key (the MCP server, which masks per its own caller) and the
#     X-Admin-Key radar: `serve_unchanged`, exactly as before.
#   * no key and no session: lp_wall(). No layer, cell, grade or score: only
#     what opens the details (Pro, through a signed checkout link) and how to
#     get the preview (a free key). An AI agent whose only credential is the
#     trial key main.auto_issue_key_for_ai_agents injected counts as keyless
#     here, which reverses the 2026-09-22 AI-agent preview for these routes.
#   * a key or session below Pro (free, trial, Developer, a $10 pack): the
#     preview, the route's `serve_tease`, whose links sell Pro only. A pack
#     no longer opens these routes. The one exception is lp_grandfathered():
#     a key still spending a pack PAID before LP_PACK_CUTOVER keeps the full
#     answer at one credit each, until those credits run out.
#   * Pro and above (founding, team, enterprise, research_seed, admin): the
#     full answer, private no-store.
#
# A session whose access token lapsed still carries the 90-day refresh cookie.
# It gets the preview, not the wall, because the map renews the token when it
# sees `_gated` and asks again (frontend#1556).
LP_PLAN = "pro"
LP_PACK_CUTOVER = datetime.datetime(2026, 9, 22, 6, 0, tzinfo=datetime.timezone.utc)
LP_CLAIM_URL = "https://dchub.cloud/api/v1/keys/claim"
LP_MAP_URL = "https://dchub.cloud/land-power-map"
_LP_SESSION_COOKIES = _CREDENTIAL_COOKIES + ("dchub_refresh",)
_PACK_TIER = "pack"   # util.rest_pack_access.PACK_TIER, set on g before a pack answer


def lp_anonymous() -> bool:
    """No key the caller sent, no Bearer token and no session cookie."""
    if presented_key() and not _injected_key_only():
        return False
    if (request.headers.get("Authorization", "") or "").startswith("Bearer "):
        return False
    return not any(request.cookies.get(c) for c in _LP_SESSION_COOKIES)


def lp_grandfathered(api_key: str) -> bool:
    """A key still spending credits from a pack paid before LP_PACK_CUTOVER."""
    if not api_key:
        return False
    try:
        from routes.mcp_conversion_plays import credits_paid_before
        return credits_paid_before(api_key, None, LP_PACK_CUTOVER) > 0
    except Exception:  # noqa: BLE001 — fails closed: a ledger error opens nothing
        return False


def _lp_bindable_key(api_key: str) -> str:
    """The key a Pro checkout may be bound to. The injected trial key is not one
    the caller chose, and a dch_trial_ key has no row the webhook's k- branch
    can stamp, so neither binds."""
    if not api_key or api_key == _auto_issued_key() or api_key.startswith("dch_trial_"):
        return ""
    return api_key


def lp_ladder(api_key: str = "", ref: str = "") -> dict:
    """upgrade_url + upgrade_options for Land & Power: Pro, and nothing else.
    `ref` is used only when there is no key to bind the checkout to."""
    _, sub_ref = key_refs(_lp_bindable_key(api_key))
    rung = _plan_rung("pro", sub_ref or ref,
                      "opens every Land & Power layer, the score and the evaluation")
    out = {"key_bound": bool(sub_ref)}
    if rung:
        out["upgrade_url"] = rung["url"]
        out["upgrade_options"] = [rung]
    return out


def lp_free_key() -> dict:
    return {"url": LP_CLAIM_URL, "method": "POST",
            "opens": "the preview: three map layers, grades and verdicts, "
                     "no scores or figures"}


def lp_wall():
    """What a keyless caller gets from a Land & Power route: no data."""
    body = {
        "success": False,
        "error": "plan_required",
        "_gated": True,
        "_wall": True,
        "_required_plan": LP_PLAN,
        "required_plan": LP_PLAN,
        "message": ("Land & Power details are Pro. Without a key this endpoint "
                    "returns no layers, cells, grades or scores. A free key opens "
                    "the preview: grades and verdicts, no scores or figures."),
        "free_key": lp_free_key(),
        "map_url": LP_MAP_URL,
    }
    # A keyless caller from a declared partner egress gets a checkout ref
    # recorded to the partner (routes/partner_attribution.py), as every REST
    # wall does; everyone else gets caller-independent links.
    try:
        from routes.partner_attribution import offer_ref_for_request
        ref = offer_ref_for_request(request.path)
    except Exception:  # noqa: BLE001 — attribution never costs the wall
        ref = ""
    body.update(lp_ladder("", ref))
    resp = make_response(jsonify(body), 403)
    # An injected trial key rides this response's headers, and a partner ref is
    # this caller's own, so neither body is ever shared.
    resp.headers["Cache-Control"] = NO_STORE if (_auto_issued_key() or ref) else TEASE_CACHE
    return resp


def lp_preview(serve_tease, api_key: str = ""):
    body, locked, total = serve_tease()
    body.update({
        "_gated": True,
        "_preview_only": True,
        "_locked_fields": list(locked),
        "_total_available": int(total or 0),
        "_required_plan": LP_PLAN,
    })
    body.update(lp_ladder(api_key))
    resp = make_response(jsonify(body), 200)
    resp.headers["Cache-Control"] = NO_STORE
    return resp


def _lp_resolve(serve_full, probe=None):
    """require_plan(LP_PLAN)'s answer for a credentialed caller, with a pack
    below Pro sent to the preview unless it is grandfathered.

    pack_opens=True keeps require_plan's refusals the light plan_or_pack_wall
    (the default builds the conversion paywall, which reads and writes the
    funnel tables on every call). Its pack path hands a pack holder to
    serve_below_plan, which marks g.user_tier 'pack', calls `_entitled` and
    burns one credit only if that answers 200. So an ungrandfathered pack is
    refused here at no cost to its credits.

    `probe` (a dict) asks without answering: a pack holder is recorded in
    probe["pack"] and refused, so nothing is burned."""
    key = presented_key()

    def _refused():
        return jsonify({"success": False, "error": "plan_upgrade_required"}), 403

    def _entitled():
        tier = getattr(g, "user_tier", None)
        if tier is None:
            # require_plan let the call through without resolving a caller (the
            # allowance it keeps for the site's own map layers). That is not Pro.
            return _refused()
        if tier == _PACK_TIER:
            if probe is not None:
                probe["pack"] = True
            if probe is not None or not lp_grandfathered(key):
                return _refused()
        return serve_full()

    from api_tier_gating import require_plan
    return make_response(require_plan(LP_PLAN, pack_opens=True)(_entitled)())


def lp_early_wall():
    """lp_wall() for a keyless caller before a route does any work, else None."""
    if _is_unchanged_caller() or not lp_anonymous():
        return None
    return lp_wall()


def lp_gate(serve_full, serve_tease, serve_unchanged=None):
    """Answer a Land & Power route.

    serve_full()      the full answer, for Pro and above.
    serve_tease()     (body dict, locked field names, total rows available):
                      the preview for a key or session below Pro.
    serve_unchanged() what internal and admin callers got before; defaults to
                      serve_full.
    """
    if _is_unchanged_caller():
        return (serve_unchanged or serve_full)()
    if lp_anonymous():
        return lp_wall()
    resp = _lp_resolve(serve_full)
    if resp.status_code == 403:
        body = resp.get_json(silent=True) or {}
        if body.get("error") in _REFUSALS:
            return lp_preview(serve_tease, presented_key())
    if resp.status_code == 200:
        resp.headers["Cache-Control"] = NO_STORE
    return resp


def lp_gated_view(tease):
    """A view decorator for a Land & Power route whose handler builds the full
    answer itself: lp_early_wall() before the handler runs, then lp_gate() on
    its 200. `tease(body)` returns (preview body, locked fields, total rows)
    from the full JSON body. Any other status (400, 404, 5xx) carries no data
    and passes through as it is.

    The handler stays byte-for-byte the full answer, so the harnesses that run
    a handler's own body (and the response-contract extractor) still read it."""
    import functools

    def deco(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            wall = lp_early_wall()
            if wall is not None:
                return wall
            resp = make_response(view(*args, **kwargs))
            if resp.status_code != 200:
                return resp
            body = resp.get_json(silent=True)
            if not isinstance(body, dict):
                # Not a JSON object: nothing the preview could be built from,
                # so below Pro it is refused rather than passed through.
                body = {}
            return lp_gate(lambda: resp, lambda: tease(body))
        return wrapped
    return deco


def lp_access() -> str:
    """'full', 'preview' or 'wall': what lp_gate would give this caller, for a
    page deciding what to draw. Spends no credit."""
    if _is_unchanged_caller():
        return "full"
    if lp_anonymous():
        return "wall"
    probe = {"pack": False}
    resp = _lp_resolve(lambda: (jsonify({"ok": True}), 200), probe)
    if resp.status_code == 200:
        return "full"
    if probe["pack"] and lp_grandfathered(presented_key()):
        return "full"
    body = resp.get_json(silent=True) or {}
    if resp.status_code == 403 and body.get("error") in _REFUSALS:
        return "preview"
    return "wall"
