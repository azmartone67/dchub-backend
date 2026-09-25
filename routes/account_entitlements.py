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
            # ★two-form lookup (2026-07-27, found live): legacy api_keys rows
            # store the RAW key in key_hash (e.g. the QA canary, id 92) while
            # newer rows store sha256 — a sha256-only lookup reports a valid
            # legacy key as 'not found' while the resolver still grants its
            # tier (confusing mismatch). Check both forms.
            cur.execute(
                "SELECT k.plan, k.is_active, k.rate_limit_tier, u.email,"
                "       u.plan, u.stripe_customer_id"
                "  FROM api_keys k LEFT JOIN users u ON u.id = k.user_id"
                " WHERE k.key_hash = %s OR k.key_hash = %s"
                " ORDER BY k.created_at DESC LIMIT 1",
                (key_hash, key))
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
                    # r-trial-copy (d, 2026-09-24): the bound address is the
                    # `email` COLUMN (what the resolver and the checkout
                    # upgrade read). metadata->>'email' is empty on bound keys,
                    # so email_bound read false for a key /api/v1/me showed
                    # with its address.
                    "SELECT status, COALESCE(NULLIF(email, ''), metadata->>'email'),"
                    "       created_at::date, tier"
                    "  FROM mcp_dev_keys WHERE api_key = %s"
                    " ORDER BY created_at DESC LIMIT 1", (key,))
                mk = cur.fetchone()
                if mk:
                    out["sources"]["mcp_dev_keys"] = {
                        "status": mk[0],
                        "email_bound": bool(mk[1]),
                        "minted": str(mk[2]),
                        "tier": mk[3],
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
        gate = getattr(t, "name", str(t)).lower()
        # r-trial-copy (d): util.tier_gate has no FREE level; a free key
        # resolves to the ANONYMOUS gate level, and this printed "anonymous"
        # for a known, bound free key. Name the plan; keep the gate level.
        known = (_tctx or {}).get("source") not in (None, "anonymous")
        plan = ((_tctx or {}).get("plan") or "").lower()
        tier_name = ("free" if gate == "anonymous" and known else gate)
        unlocks = _PLAN_TOOL_SUMMARY.get(tier_name, _PLAN_TOOL_SUMMARY["free"])
        # 2026-09-24 (live gate run 3): a bound free key read "50 calls/day
        # once email-bound" beside email_bound=true. Say what it HAS.
        src = out["sources"]
        bound = bool((src.get("mcp_dev_keys") or {}).get("email_bound")
                     or (src.get("api_keys") or {}).get("user_email"))
        if tier_name == "free" and bound:
            try:
                from tier_registry import calls_per_day
                unlocks = ("free plan, email-bound · free-tier depth on all "
                           "tools · %s calls/day" % f"{calls_per_day('identified'):,}")
            except Exception:  # noqa: BLE001
                unlocks = "free plan, email-bound · free-tier depth on all tools"
        out["resolved"] = {
            "tier": tier_name,
            "gate_level": gate,
            "plan": plan or None,
            "email_bound": bound,
            "unlocks": unlocks,
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
            # ★2026-09-20 — CLEAR THE DUNNING MARKER, and only that.
            #
            # This wrote `plan` alone, and api_tier_gating.resolve_effective_plan
            # reads plan AND subscription_status AND demoted_at. So a repair
            # could set plan='pro' and leave the account resolving to 'free',
            # which is a repair that silently did not repair: after #4877/#4903
            # both self-serve restore paths follow the authority, so they stayed
            # shut for an account an admin had just "fixed".
            #
            # demoted_at is the dunning guard's own marker and clearing it is
            # exactly what a repair means. subscription_status is deliberately
            # NOT touched: it is BILLING state owned by the Stripe webhooks, and
            # writing 'active' over a 'canceled' account here would be this
            # service asserting a payment it has no knowledge of. An admin who
            # means to grant access to a canceled account should see that it did
            # not take effect — which is what `effective_plan` below reports —
            # rather than have a lie written under them.
            cur.execute(
                "UPDATE users SET plan=%s, demoted_at=NULL WHERE id=%s",
                (plan, uid))
            cur.execute(
                "UPDATE api_keys SET plan=%s, rate_limit_tier=%s"
                " WHERE user_id=%s AND is_active IS TRUE", (plan, plan, uid))
            keys_updated = cur.rowcount
            cur.execute(
                "INSERT INTO entitlement_repairs"
                " (email, new_plan, prev_user_plan, keys_updated)"
                " VALUES (%s,%s,%s,%s)",
                (email, plan, prev_plan, keys_updated))
            # ★ REPORT WHAT THE REPAIR ACTUALLY ACHIEVED, read back from the
            # row rather than assumed from the write. `plan` is what we asked
            # for; `effective_plan` is what the entitlement authority now says,
            # and the two disagreeing is the only visible sign that a repair was
            # a no-op.
            effective_plan, sub_status = None, None
            try:
                cur.execute("SELECT plan, subscription_status, role, demoted_at"
                            " FROM users WHERE id=%s LIMIT 1", (uid,))
                _r = cur.fetchone()
                if _r:
                    sub_status = _r[1]
                    from api_tier_gating import resolve_effective_plan
                    effective_plan = resolve_effective_plan(_r[0], _r[1], _r[2], _r[3])
            except Exception:
                effective_plan = None      # unknown, never guessed
        c.commit()
        took_effect = (effective_plan == plan) if effective_plan else None
        out = dict(ok=True, email=email, plan=plan,
                   prev_user_plan=prev_plan, keys_updated=keys_updated,
                   effective_plan=effective_plan, took_effect=took_effect)
        if took_effect is False:
            out["warning"] = (
                "plan was written but the account still resolves to '%s'"
                % effective_plan
                + (" because subscription_status is '%s'" % sub_status if sub_status else "")
                + ". Self-serve key recovery and binding confirmation follow the"
                  " resolved plan, so they remain closed for this account. This"
                  " endpoint does not write billing state; fix it at the source.")
        return jsonify(**out)
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
