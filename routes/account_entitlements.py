"""routes/account_entitlements.py — entitlement self-check + repair (digest #2).

WHY: the #1 (and only) open customer ask is a PAYING founder-tier customer
whose license "is not working" — triaged 2026-07-09, still open. With 34
paid keys + 3 enterprise total, one broken paid entitlement is ~3% of the
paid base. Separately, all 7 honest paid conversions in 30d are
unattributable — the same users.stripe_customer_id → key/tier linkage this
endpoint makes visible is the join the attribution bridge starves on.

GET /api/v1/account/entitlements
  Auth: the caller's own X-API-Key (or ?api_key=). Shows exactly what the
  system believes: which key store matched (api_keys vs mcp_dev_keys), the
  key's plan/active state, the linked users row (plan, stripe_customer_id
  presence — the id itself is redacted to last4), the tier the resolver
  would grant, and a mismatch[] list when the stores disagree (the founder
  bug's shape: paid plan on users, key resolving lower or inactive).
  ★util.tier_gate.Tier is an IntEnum — compare/emit .name, never .value
  (the silent never-promotes trap, 2026-07-26).

POST /api/v1/admin/entitlements/repair   (X-Admin-Key)
  Body {email, plan} — re-aligns users.plan AND the user's active api_keys
  rows to `plan`, writing an audit row to entitlement_repairs (append-only,
  created on first write). Deliberately narrow: it aligns EXISTING rows to
  a stated plan; it never creates keys, never touches Stripe.

Fail-soft reads; the only writes are the admin repair + its audit trail.
"""

from __future__ import annotations

import hashlib
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

account_entitlements_bp = Blueprint("account_entitlements", __name__)

_DDL_DONE = [False]

_PLAN_TOOL_SUMMARY = {
    "free":       "free-tier depth on all tools · 50 calls/day once email-bound",
    "starter":    "starter depth · 200 calls/day",
    "developer":  "full analytics depth (coords ~11km) · 500 calls/day",
    "pro":        "full site-grade depth · 2,000 calls/day",
    "founding":   "pro-grade depth · founding-member terms",
    "enterprise": "full depth · custom limits",
}


def _conn():
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.debug("[entitlements] conn failed: %s", e)
        return None


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@account_entitlements_bp.route("/api/v1/account/entitlements", methods=["GET"])
def my_entitlements():
    key = (request.headers.get("X-API-Key")
           or request.args.get("api_key") or "").strip()
    if not key:
        return _no_store(jsonify(
            ok=False, error="pass your API key as X-API-Key",
            hint="this endpoint shows what tier the system believes YOUR "
                 "key has and why")), 401
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    out = {"ok": True, "key_prefix": key[:12] + "…", "sources": {},
           "resolved": {}, "mismatches": []}
    c = _conn()
    if c is None:
        return _no_store(jsonify(ok=False, error="store unavailable")), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT k.plan, k.is_active, k.rate_limit_tier, u.email,"
                "       u.plan, u.stripe_customer_id"
                "  FROM api_keys k LEFT JOIN users u ON u.id = k.user_id"
                " WHERE k.key_hash = %s ORDER BY k.created_at DESC LIMIT 1",
                (key_hash,))
            row = cur.fetchone()
            if row:
                kplan, active, rlt, email, uplan, stripe = row
                out["sources"]["api_keys"] = {
                    "plan": kplan, "is_active": bool(active),
                    "rate_limit_tier": rlt,
                    "user_email": (email or "")[:2] + "…" if email else None,
                    "user_plan": uplan,
                    "stripe_linked": bool(stripe),
                    "stripe_customer_last4": (str(stripe)[-4:]
                                              if stripe else None),
                }
                if uplan and kplan and uplan != kplan:
                    out["mismatches"].append(
                        "users.plan=%s but api_keys.plan=%s — the founder-bug "
                        "shape; admin repair re-aligns" % (uplan, kplan))
                if not active:
                    out["mismatches"].append(
                        "key is INACTIVE in api_keys (rotation?) — "
                        "resolves to anonymous regardless of plan")
                if uplan in ("pro", "founding", "enterprise") and not stripe:
                    out["mismatches"].append(
                        "paid users.plan with NO stripe_customer_id — paid "
                        "conversions from this account are unattributable")
            else:
                cur.execute(
                    "SELECT status, metadata->>'email',"
                    "       created_at::date"
                    "  FROM mcp_dev_keys WHERE api_key = %s"
                    " ORDER BY created_at DESC LIMIT 1", (key,))
                mk = cur.fetchone()
                if mk:
                    out["sources"]["mcp_dev_keys"] = {
                        "status": mk[0],
                        "email_bound": bool(mk[1]),
                        "minted": str(mk[2]),
                    }
                    if mk[0] != "active":
                        out["mismatches"].append(
                            "claim-flow key status=%s — resolves anonymous"
                            % mk[0])
                else:
                    out["sources"]["none"] = True
                    out["mismatches"].append(
                        "key not found in api_keys OR mcp_dev_keys — "
                        "revoked or mistyped; call recover_my_key with your "
                        "bound email, or claim_free_key for a fresh one")
    except Exception as e:  # noqa: BLE001
        return _no_store(jsonify(ok=False, error=str(e)[:120])), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    # The tier the live resolver would grant (authoritative; .name never
    # .value — the IntEnum silent-never-promotes trap). resolve_tier reads
    # this request's own X-API-Key/?api_key= — exactly the key under test.
    try:
        from util.tier_gate import resolve_tier
        t, _tctx = resolve_tier()
        tier_name = getattr(t, "name", str(t)).lower()
        out["resolved"] = {
            "tier": tier_name,
            "unlocks": _PLAN_TOOL_SUMMARY.get(
                tier_name, _PLAN_TOOL_SUMMARY["free"]),
        }
    except Exception as e:  # noqa: BLE001
        out["resolved"] = {"tier": None, "note": "resolver unavailable: %s"
                           % str(e)[:80]}
    out["healthy"] = not out["mismatches"]
    return _no_store(jsonify(out))


@account_entitlements_bp.route("/api/v1/admin/entitlements/repair",
                               methods=["POST"])
def repair():
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    if not sent or sent != expected:
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "").strip().lower()
    if not email or plan not in _PLAN_TOOL_SUMMARY:
        return jsonify(ok=False, error="need email + plan in %s"
                       % sorted(_PLAN_TOOL_SUMMARY)), 400
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="store unavailable"), 503
    try:
        with c.cursor() as cur:
            if not _DDL_DONE[0]:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS entitlement_repairs ("
                    " id BIGSERIAL PRIMARY KEY,"
                    " ts TIMESTAMPTZ DEFAULT NOW(),"
                    " email TEXT, new_plan TEXT,"
                    " prev_user_plan TEXT, keys_updated INT)")
                _DDL_DONE[0] = True
            cur.execute("SELECT id, plan FROM users WHERE lower(email)=%s"
                        " LIMIT 1", (email,))
            u = cur.fetchone()
            if not u:
                return jsonify(ok=False, error="no users row for email"), 404
            uid, prev_plan = u
            cur.execute("UPDATE users SET plan=%s WHERE id=%s", (plan, uid))
            cur.execute(
                "UPDATE api_keys SET plan=%s, rate_limit_tier=%s"
                " WHERE user_id=%s AND is_active IS TRUE", (plan, plan, uid))
            keys_updated = cur.rowcount
            cur.execute(
                "INSERT INTO entitlement_repairs"
                " (email, new_plan, prev_user_plan, keys_updated)"
                " VALUES (%s,%s,%s,%s)",
                (email, plan, prev_plan, keys_updated))
        c.commit()
        return jsonify(ok=True, email=email, plan=plan,
                       prev_user_plan=prev_plan, keys_updated=keys_updated)
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:
            pass
        return jsonify(ok=False, error=str(e)[:140]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
