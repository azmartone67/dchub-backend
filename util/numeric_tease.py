"""One gate for paid numerics on public routes (free/anon tighten, 2026-09-21).

Routes that publish DC Hub's paid numbers on a public URL (DCPI scores,
time-to-power, MW, cents per kWh) answer through serve_full_or_tease(full,
tease). Who gets `full` is decided here, once, the same way on every route
that uses it:

  * X-Internal-Key (the MCP server's calls to this backend). This is
    require_plan's own first step and is unchanged: the MCP server applies
    its own masks.
  * X-Admin-Key or ?admin_key= matching DCHUB_ADMIN_KEY (falling back to
    DCHUB_INTERNAL_KEY): the admin and QA bypass these routes already had.
  * Developer and above, resolved by api_tier_gating.require_plan: the
    website session cookie, a JWT Bearer or an API key. A paying website
    user therefore resolves to the same plan as their key.
  * A valid key below Developer that holds unexpired $10-pack credits. One
    credit is burned per DELIVERED 200 (util/rest_pack_access.serve_below_plan).

Everyone else gets `tease`, with HTTP 200: keyless callers, free and
identified keys, keys without pack credits, an unknown key, and any request
the gate could not decide. An undecided gate fails closed to the tease, never
to the full answer.

The full answer is marked private/no-store. The tease must not depend on the
caller (tease_envelope() does not), because it is the one body a shared cache
may hold.
"""
from __future__ import annotations

import hmac
import numbers
import os
from decimal import Decimal

from flask import make_response, request

TEASE_ROWS = 3
FULL_MIN_PLAN = "developer"
COORD_DP = 2
GATING_MATRIX_URL = "https://dchub.cloud/api/v1/gating-matrix"
TEASE_CACHE_CONTROL = "public, max-age=300"
NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
            "Surrogate-Control": "no-store", "Pragma": "no-cache"}

# require_plan's answers that mean "not entitled", as opposed to an answer the
# route itself produced: its two plan walls, an unknown key, and its own
# fail-closed 503.
_GATE_REFUSALS = {
    403: ("plan_required", "plan_upgrade_required"),
    401: ("invalid_api_key",),
    503: ("Authentication service unavailable",),
}


def _carries_credential() -> bool:
    """Anything require_plan (or the admin bypass) could resolve a caller from.

    Presence only. Without any of these, require_plan can only refuse, so the
    tease is served without building (and discarding) its wall. The cookies
    are the ones require_plan decodes as a login JWT, in its order.
    """
    # Literal reads, one per channel, so scripts/check_credential_channel_coverage.py
    # can enumerate them (a .get(<variable>) is invisible to it).
    return bool(request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.headers.get("X-API-Key")
                or request.headers.get("Authorization")
                or request.args.get("api_key")
                or request.args.get("admin_key")
                or request.cookies.get("session_token")
                or request.cookies.get("dchub_token")
                or request.cookies.get("token"))


def admin_key_ok() -> bool:
    """X-Admin-Key or ?admin_key= equal to DCHUB_ADMIN_KEY (else DCHUB_INTERNAL_KEY)."""
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").split()
    expected = expected[0] if expected else ""
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").split()
    sent = sent[0] if sent else ""
    return bool(expected and sent) and hmac.compare_digest(sent, expected)


def _status_and_error(resp):
    status, body_resp = None, resp
    if isinstance(resp, tuple):
        body_resp = resp[0]
        if len(resp) > 1 and isinstance(resp[1], int):
            status = resp[1]
    if status is None:
        status = getattr(body_resp, "status_code", 200)
    try:
        body = body_resp.get_json(silent=True)
    except Exception:  # noqa: BLE001 - a non-JSON body is not a gate refusal
        body = None
    return status, (body.get("error") if isinstance(body, dict) else None)


def is_gate_refusal(resp) -> bool:
    status, error = _status_and_error(resp)
    return error is not None and error in _GATE_REFUSALS.get(status, ())


def _full(resp):
    out = make_response(resp)
    out.headers.update(NO_STORE)
    return out


def _tease(resp):
    out = make_response(resp)
    try:
        # A keyless caller from a declared partner egress gets ladder links
        # carrying a ref minted for that one request; that body is not shared.
        from util.rest_tease import tease_headers
        out = tease_headers(out)
    except Exception:  # noqa: BLE001
        pass
    if out.status_code == 200 and "Cache-Control" not in out.headers:
        out.headers["Cache-Control"] = TEASE_CACHE_CONTROL
    out.headers["X-DCHub-Preview"] = "1"
    return out


def serve_full_or_tease(serve_full, serve_tease, min_plan: str = FULL_MIN_PLAN):
    """`serve_full` and `serve_tease` are zero-argument callables returning a
    Flask response. Exactly one of them answers the request (both run only
    when a pack credit could not be burned after the full answer was built)."""
    if not _carries_credential():
        return _tease(serve_tease())
    if admin_key_ok():
        return _full(serve_full())
    try:
        from api_tier_gating import require_plan
        resp = require_plan(min_plan, pack_opens=True)(serve_full)()
    except Exception:  # noqa: BLE001 - an undecided gate serves the tease
        return _tease(serve_tease())
    if is_gate_refusal(resp):
        return _tease(serve_tease())
    return _full(resp)


def tease_envelope(total_available, locked_fields) -> dict:
    """The keys every tease carries: util/rest_tease.envelope's, so these
    routes answer with the same envelope as the other gated REST reads
    (_gated, _preview_only, _locked_fields, _total_available, and the ladder
    util/rest_pack_access.plan_or_pack_wall builds: the $10 pack, then
    Developer, both of which open these routes; measured /go/c links, never
    bare /pricing). gating_matrix is kept from the DCPI list's own preview.
    """
    try:
        from util.rest_tease import envelope
        env = envelope(FULL_MIN_PLAN, locked_fields, total_available)
    except Exception:  # noqa: BLE001 - the tease still answers without a ladder
        env = {"_gated": True, "_preview_only": True,
               "_locked_fields": sorted(set(locked_fields)),
               "_total_available": int(total_available or 0)}
    env["gating_matrix"] = GATING_MATRIX_URL
    return env


def unlock_url() -> str:
    """The ladder's lead link (the pack's /go/c checkout), for an HTML tease."""
    try:
        from routes.checkout_click_tracker import rest_wall_ladder
        return rest_wall_ladder(opens_on_rest="pack").get("upgrade_url") or ""
    except Exception:  # noqa: BLE001
        return ""


def is_numeric(v) -> bool:
    return isinstance(v, (numbers.Number, Decimal)) and not isinstance(v, bool)


def null_fields(row: dict, fields) -> dict:
    """A copy of `row` with each of `fields` that it carries set to None."""
    out = dict(row)
    for k in fields:
        if k in out:
            out[k] = None
    return out


def null_numerics(row: dict, keep=(), containers: bool = False) -> tuple[dict, list]:
    """A copy of `row` with every numeric value set to None, except `keep`.
    With containers=True, every list and dict value is nulled as well (JSON
    columns such as risk lists or a score history carry the same numbers).

    For rows read with SELECT *: a column added to the table later is locked
    by default instead of published by default. Returns (copy, locked keys).
    """
    out, locked = dict(row), []
    for k, v in row.items():
        if k in keep:
            continue
        if is_numeric(v) or (containers and isinstance(v, (list, dict, tuple))):
            out[k] = None
            locked.append(k)
    return out, locked


def round_coords(row: dict, keys=("latitude", "longitude", "lat", "lng", "lon"),
                 dp: int = COORD_DP) -> dict:
    """A copy of `row` with its coordinates rounded to `dp` decimal places."""
    out = dict(row)
    for k in keys:
        v = out.get(k)
        if is_numeric(v):
            out[k] = round(float(v), dp)
    return out
