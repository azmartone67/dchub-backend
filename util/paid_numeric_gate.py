"""Paid numerics behind one gate: who gets the full answer, and what everyone
else gets (free/anon tighten, 2026-09-21).

Several keyless REST routes returned the numerics the paid plans sell (MW,
scores, time-to-power, fiber distance, exact coordinates). This module is the
one gate they share, so their envelopes match each other and match
/api/v1/facilities.

WHO GETS WHAT
  same   A valid X-Internal-Key (the MCP server applies its own masks), the
         admin key, or an admin-role key: exactly what the route answered
         before this module, headers included.
  full   A plan at or above `min_plan` (Developer by default), resolved from
         the same credentials require_plan() reads, the login cookie, a Bearer
         JWT and the API key, through api_tier_gating.get_request_principal().
         A paid website session gets what a paid key gets.
  pack   A valid key below `min_plan` holding unexpired $10-pack credits: the
         full answer for one credit, burned only on a delivered 200
         (util/rest_pack_access.serve_below_plan).
  tease  Everyone else: no credential, a free or identified key, or a key that
         does not resolve. HTTP 200 in the route's own shape with the paid
         numerics null (or replaced by a coarse `<field>_band`), coordinates
         at 2 dp and rows capped where the route says so. The envelope adds
         _gated, _preview_only, _locked_fields, _total_available and the
         ladder of the keyed-walls gate (util/rest_pack_access.
         plan_or_pack_wall: the pack, then Developer, both opening the route
         over REST). Names, ids, states, ISOs, counts, verdicts and timestamps
         stay.

CACHING
  The tease is caller-independent, so it is the body a shared cache may hold:
  it keeps whatever the view and the app's after_request policy give it. The
  one exception is a keyless caller from a declared partner egress, whose
  checkout links carry a ref minted for that request: that tease is `private,
  no-store`, like every full answer. Cloudflare does not key on X-API-Key, so
  a keyed body in a shared cache is a keyed body served to the next anonymous
  caller.

Routes that answer nothing below Developer (a 403, not a tease) use
developer_or_pack_wall: the keyed-walls gate, require_plan(min_plan,
pack_opens=True), with the same private/no-store rule for full answers.

Fails closed: if the gate itself errors, the caller gets the tease, never the
full answer.
"""
from __future__ import annotations

import hmac
import json
import os
from functools import wraps
from urllib.parse import urlencode

SAME = "same"
FULL = "full"
BELOW = "below"
ANON = "anon"

NO_STORE = {
    "Cache-Control": "private, no-store, max-age=0",
    "Surrogate-Control": "no-store",
    "Pragma": "no-cache",
}
_VARY = ("Cookie", "Authorization", "X-API-Key")


# ── who is asking ────────────────────────────────────────────────────────────

def _admin_key_ok() -> bool:
    """The admin key main.py's require_plan stub honours (constant-time)."""
    from flask import request
    got = (request.headers.get("X-Admin-Key", "") or "").split()
    want = (os.environ.get("DCHUB_ADMIN_KEY", "") or "").split()
    return bool(got and want and hmac.compare_digest(got[0], want[0]))


def _internal_key_ok() -> bool:
    from flask import request
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(request.headers.get("X-Internal-Key", "")))
    except Exception:  # noqa: BLE001 — unknown means not internal
        return False


def access(min_plan: str = "developer"):
    """(decision, api_key) for the current request.

    decision is SAME (internal or admin: answer exactly as before), FULL,
    BELOW (a resolved credential under `min_plan`) or ANON. api_key is the raw
    key the request presents, for the pack ledger."""
    try:
        from api_tier_gating import (get_request_principal, request_api_key,
                                     request_credential_count, request_plan_ceiling,
                                     user_has_access)
        if _admin_key_ok():
            return SAME, None
        principal = get_request_principal()
        if principal.get("internal_key") or principal.get("tier") == "admin":
            return SAME, None
        api_key = request_api_key()
        if not principal.get("credential"):
            return ANON, api_key
        tier = principal.get("tier") or "free"
        # ★2026-09-22: a caller with several credentials gets the highest plan
        # among them (api_tier_gating.request_plan_ceiling). The principal names
        # the API key first, so a paying web session whose page also sent a
        # free key was served this route's preview.
        if request_credential_count() > 1:
            ceiling = request_plan_ceiling()
            if ceiling != "admin" and user_has_access(ceiling, tier) and not user_has_access(tier, ceiling):
                tier = ceiling
        if user_has_access(tier, min_plan):
            return FULL, api_key
        return BELOW, api_key
    except Exception:  # noqa: BLE001 — a broken gate serves the tease
        return ANON, None


# ── coarse values ────────────────────────────────────────────────────────────

_MW_BANDS = (
    (10, 50, "10–50 MW"),
    (50, 100, "50–100 MW"),
    (100, 500, "100–500 MW"),
    (500, 1_000, "500 MW–1 GW"),
    (1_000, 5_000, "1–5 GW"),
    (5_000, 10_000, "5–10 GW"),
    (10_000, 50_000, "10–50 GW"),
    (50_000, 100_000, "50–100 GW"),
    (100_000, 500_000, "100–500 GW"),
    (500_000, 1_000_000, "500 GW–1 TW"),
)


def mw_band(value):
    """A coarse band for a megawatt figure, e.g. 3200 -> '1–5 GW'.

    None for anything that is not a number. Lower edge inclusive. A negative
    figure (a net decline) is banded on its magnitude and marked."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    mag = abs(v)
    if mag == 0:
        label = "0 MW"
    elif mag < 10:
        label = "under 10 MW"
    elif mag >= 1_000_000:
        label = "1 TW or more"
    else:
        label = next(lab for lo, hi, lab in _MW_BANDS if lo <= mag < hi)
    return ("net decline " + label) if v < 0 else label


def band_fields(row, fields):
    """Replace each numeric field with None and add its `<field>_band`."""
    if not isinstance(row, dict):
        return row
    for f in fields:
        if f in row:
            row[f + "_band"] = mw_band(row[f])
            row[f] = None
    return row


def band_rows(list_key, fields, *, summary_key=None, summary_fields=(), null_fields=()):
    """A tease mask for a ranking-shaped payload: every row stays, each MW
    field becomes None plus its `<field>_band`, each of `null_fields` (money,
    scores) becomes None. `_total_available` is the row count."""
    def mask(payload):
        rows = payload.get(list_key)
        if isinstance(rows, list):
            for r in rows:
                band_fields(r, fields)
                if isinstance(r, dict):
                    for f in null_fields:
                        if f in r:
                            r[f] = None
        if summary_key and isinstance(payload.get(summary_key), dict):
            band_fields(payload[summary_key], summary_fields)
        return payload, (len(rows) if isinstance(rows, list) else None)
    return mask


def coarse_coord(value, dp: int = 2):
    try:
        return round(float(value), dp)
    except (TypeError, ValueError):
        return None


def _ladder():
    """(upgrade fields, per_caller). The rungs plan_or_pack_wall offers: the
    pack, then Developer, both `opens: "rest"`, with the same partner ref a
    keyless caller from a declared partner egress gets on that wall."""
    try:
        from flask import request
        from routes.checkout_click_tracker import rest_wall_ladder
        try:
            from routes.partner_attribution import offer_ref_for_request
            ref = offer_ref_for_request(request.path)
        except Exception:  # noqa: BLE001 — attribution never costs the tease
            ref = ""
        return rest_wall_ladder(opens_on_rest="pack", ref=ref), bool(ref)
    except Exception:  # noqa: BLE001
        return {}, False


# ── responses ────────────────────────────────────────────────────────────────

def _vary(resp):
    try:
        for h in _VARY:
            resp.vary.add(h)
    except Exception:  # noqa: BLE001
        pass
    return resp


def _no_store(rv):
    from flask import make_response
    resp = make_response(rv)
    for k, v in NO_STORE.items():
        resp.headers[k] = v
    return _vary(resp)


def _call(view, args, kwargs, preview_args):
    """Run the view, under the preview's query string when there is one.
    Returns (response, the caller's params the preview did not apply as sent)."""
    from flask import current_app, make_response, request
    from werkzeug.datastructures import MultiDict
    if preview_args is None:
        return make_response(view(*args, **kwargs)), []
    pairs = list(preview_args(request.args))
    applied = MultiDict(pairs)
    changed = sorted({k for k in request.args.keys()
                      if request.args.getlist(k) != applied.getlist(k)})
    with current_app.test_request_context(
            request.path, method="GET", query_string=urlencode(pairs, doseq=True)):
        return make_response(view(*args, **kwargs)), changed


def _tease(view, args, kwargs, mask, locked, preview_args):
    from flask import current_app, make_response
    try:
        resp, dropped = _call(view, args, kwargs, preview_args)
        body = resp.get_json(silent=True) if resp.is_json else None
        if resp.status_code != 200:
            # An error body can still carry a partial answer: mask it, but it is
            # not a preview and not cacheable.
            if isinstance(body, dict):
                teased, _ = mask(body)
                resp.set_data(current_app.json.dumps(teased))
            return resp
        if not isinstance(body, dict):
            raise ValueError("a 200 this gate cannot mask")
        teased, total = mask(body)
        teased.update({
            "_gated": True,
            "_preview_only": True,
            "_locked_fields": sorted(locked),
            "_total_available": total if isinstance(total, int) else None,
        })
        if dropped:
            teased["_preview_params_not_applied"] = dropped
        ladder, per_caller = _ladder()
        teased.update(ladder)
        resp.set_data(current_app.json.dumps(teased))
        if per_caller:
            for k, v in NO_STORE.items():
                resp.headers[k] = v
        return _vary(resp)
    except Exception:  # noqa: BLE001 — fail closed, never the full body
        return make_response(json.dumps({
            "ok": False, "success": False, "error": "preview_unavailable",
            "_gated": True}), 503, {"Content-Type": "application/json", **NO_STORE})


def tease_numerics(mask, *, locked, min_plan="developer", preview_args=None):
    """Decorate a Flask JSON view whose full answer carries paid numerics.

    mask(payload) -> (tease_payload, total_available). It receives the view's
    parsed JSON and may edit it in place. preview_args(request.args) -> the
    (key, value) pairs the tease is computed from, for a view whose tease must
    not depend on the caller's numeric filters.
    Place it UNDER the route decorator and ABOVE any response memo, so the
    memo keeps caching the caller-independent full answer."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            decision, api_key = access(min_plan)
            if decision == SAME:
                return view(*args, **kwargs)
            if decision == FULL:
                return _no_store(view(*args, **kwargs))

            def full():
                return _no_store(view(*args, **kwargs))

            def tease():
                return _tease(view, args, kwargs, mask, locked, preview_args)

            if decision == BELOW:
                from util.rest_pack_access import serve_below_plan
                return serve_below_plan(api_key, full, tease)
            return tease()
        return wrapped
    return decorator


def is_full_caller(min_plan: str = "developer") -> bool:
    """True when the current request may see paid numerics without a pack
    credit. For handlers that resolve something inline (a candidate id) rather
    than returning a payload this module can mask."""
    return access(min_plan)[0] in (SAME, FULL)


def developer_or_pack_wall(min_plan: str = "developer"):
    """Decorate a view that answers only callers `min_plan` admits, plus pack
    holders for one credit: api_tier_gating.require_plan(min_plan,
    pack_opens=True), the keyed-walls gate, whose refusal is
    util/rest_pack_access.plan_or_pack_wall. Resolved at call time, so it can
    decorate a view in main.py before the gate module is imported there; it is
    deliberately not main.py's require_plan stub, which carries bypasses meant
    for the website's own pages. Internal and admin callers get exactly what
    they got before; other full answers go out private/no-store."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if _admin_key_ok() or _internal_key_ok():
                return view(*args, **kwargs)
            from api_tier_gating import require_plan
            return require_plan(min_plan, pack_opens=True)(
                lambda: _no_store(view(*args, **kwargs)))()
        return wrapped
    return decorator
