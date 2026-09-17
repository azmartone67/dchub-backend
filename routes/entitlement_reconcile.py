"""
routes/entitlement_reconcile.py — did every payer actually GET what they bought?
=============================================================================

Implements the reconciler Brain L6 specced in
routes/_proposed_founder_entitlement_provisioning_repair.py (which shipped as a
501 scaffold and was never built). The complaint that motivated it — a founding
member saying "bought the founder licence and I dont think it is working" — was
triaged HIGH on 2026-07-09 and was still open a month later.

WHY THIS EXISTS (measured 2026-08-10, live)
-------------------------------------------
19 real paying customers ($1,614/mo, from mcp_conversions excluding is_test,
refunded and the founder's own address). FOURTEEN — $1,299/mo — had never made
a single MCP call. Four of those had no MCP key at all. Nobody knew, because
nothing cross-checked "paid" against "can actually call".

★ THE TRAP THIS CLASS OF CHECK FALLS INTO, AND WHY THIS ONE DOES NOT.
The obvious implementation asks "does mcp_dev_keys have a row for this payer?"
and calls a miss unprovisioned. That is WRONG, and it produced a wrong answer
about our largest research licensee on the first pass:

    NLR ($3,000/yr, 4 seats) has ZERO mcp_dev_keys rows — and 4 ACTIVE
    enterprise REST keys in api_keys, with 960 calls actually made
    (ian.christie 865, gabriel.zuckerman 71, galen.maclaurin 24).

Reported off mcp_dev_keys alone they read as "never got access". The truth is
they had REST access, used it heavily for three weeks, stopped on 2026-06-15,
and were never provisioned on the MCP surface at all. Those are three different
problems with three different owners, and only one of them is provisioning.

So every payer is classified across BOTH surfaces:

    no_access      neither an MCP key nor an active REST key  → provision
    mcp_missing    REST works, MCP never provisioned          → provision MCP
    dormant        has access, used it, then stopped          → NOT provisioning
    never_started  has access, never called                   → NOT provisioning
    healthy        has access and is calling

Only the first two are provisioning failures. The last three are activation or
relationship work and must not be "fixed" by minting another key nobody asked
for.

SAFETY
------
- Admin-gated, fails CLOSED (no configured key ⇒ nobody in).
- DRY-RUN BY DEFAULT. Writes only on ?confirm=1, and even then only for the two
  provisioning classes.
- Emailing is separately gated behind ENTITLEMENT_RECONCILE_EMAIL=1, because
  minting a key and telling a customer about it are different decisions.
- Never touches a key that already exists and works.

    GET  /api/v1/admin/entitlements/reconcile            dry-run report
    POST /api/v1/admin/entitlements/reconcile?confirm=1  provision the gaps
"""
from __future__ import annotations

import os
import secrets as _sec

from flask import Blueprint, jsonify, request

entitlement_reconcile_bp = Blueprint("entitlement_reconcile", __name__)

# Classes that represent a PROVISIONING failure — the only ones we ever write for.
_FIXABLE = ("no_access", "mcp_missing")


def _admin_ok() -> bool:
    exp = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(exp) and got == exp


def _conn():
    import psycopg2
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "")
    if not url:
        return None
    c = psycopg2.connect(url, connect_timeout=10)
    c.autocommit = False
    return c


_PAYERS_SQL = """
WITH payers AS (
  SELECT lower(user_email) AS email,
         max(created_at)   AS paid_at,
         max(plan_to)      AS plan,
         max(mrr_cents)    AS mrr_cents
    FROM mcp_conversions
   WHERE COALESCE(is_test, false) = false
     AND refunded_at IS NULL
     AND user_email IS NOT NULL AND user_email <> ''
   GROUP BY 1
)
SELECT p.email, p.plan, p.mrr_cents, p.paid_at,
       -- MCP surface
       COALESCE(m.keys, 0)        AS mcp_keys,
       m.last_used                AS mcp_last_used,
       -- REST surface (the half a naive check misses)
       COALESCE(r.active_keys, 0) AS rest_keys,
       COALESCE(r.calls, 0)       AS rest_calls,
       r.last_used                AS rest_last_used
  FROM payers p
  LEFT JOIN LATERAL (
      SELECT count(*) AS keys, max(last_used_at) AS last_used
        FROM mcp_dev_keys d
       WHERE lower(d.email) = p.email AND d.status = 'active') m ON true
  LEFT JOIN LATERAL (
      SELECT count(*) AS active_keys, sum(COALESCE(ak.usage_count, 0)) AS calls,
             max(ak.last_used_at) AS last_used
        FROM users u JOIN api_keys ak ON ak.user_id = u.id
       WHERE lower(u.email) = p.email
         AND COALESCE(ak.is_active, 1) <> 0) r ON true
 ORDER BY p.paid_at
"""


def _classify(row: dict) -> str:
    has_mcp = row["mcp_keys"] > 0
    has_rest = row["rest_keys"] > 0
    called = (row["rest_calls"] or 0) > 0 or row["mcp_last_used"] is not None
    if not has_mcp and not has_rest:
        return "no_access"
    if not has_mcp:
        # REST works. This is the NLR shape: real access, real usage, but the
        # MCP product they bought was never turned on.
        return "mcp_missing"
    if not called:
        return "never_started"
    if row["mcp_last_used"] is None and row["rest_last_used"] is not None:
        return "dormant"
    return "healthy"


def _survey():
    c = _conn()
    if c is None:
        return None, "no DATABASE_URL"
    out = []
    try:
        with c.cursor() as cur:
            cur.execute(_PAYERS_SQL)
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                row["mrr_usd"] = round((row.pop("mrr_cents") or 0) / 100.0, 2)
                for k in ("paid_at", "mcp_last_used", "rest_last_used"):
                    if row.get(k) is not None:
                        row[k] = row[k].isoformat()
                row["klass"] = _classify(row)
                out.append(row)
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return out, None


def _provision_mcp(cur, email: str, plan: str) -> str:
    """Mint the MCP key the payer never got. Mirrors the Stripe webhook's own
    column set (flask_mcp_endpoints ~4924) so a reconciled key is
    indistinguishable from a normally-provisioned one."""
    import json
    tier = "enterprise" if (plan or "").lower().startswith("enter") else "paid"
    key = "dch_live_" + _sec.token_hex(16)
    cur.execute(
        """INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status, metadata)
           VALUES (%s, %s, %s, %s, 'active', %s::jsonb) ON CONFLICT DO NOTHING""",
        (key, "dev_" + _sec.token_hex(8), email, tier,
         json.dumps({"source": "entitlement_reconcile",
                     "reason": "paid but never provisioned on the MCP surface"})))
    return key


@entitlement_reconcile_bp.route("/api/v1/admin/entitlements/reconcile",
                                methods=["GET", "POST"])
def reconcile():
    if not _admin_ok():
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key required"), 401

    rows, err = _survey()
    if rows is None:
        return jsonify(ok=False, error=err), 500

    buckets: dict = {}
    for r in rows:
        buckets.setdefault(r["klass"], []).append(r)
    fixable = [r for r in rows if r["klass"] in _FIXABLE]

    confirm = request.args.get("confirm") == "1"
    email_armed = os.environ.get("ENTITLEMENT_RECONCILE_EMAIL") == "1"
    provisioned = []

    if confirm and request.method == "POST" and fixable:
        c = _conn()
        if c is None:
            return jsonify(ok=False, error="no DATABASE_URL"), 500
        try:
            with c.cursor() as cur:
                for r in fixable:
                    try:
                        _provision_mcp(cur, r["email"], r["plan"])
                        provisioned.append({"email": r["email"],
                                            "plan": r["plan"],
                                            "klass": r["klass"],
                                            "emailed": False})
                    except Exception as e:  # noqa: BLE001
                        provisioned.append({"email": r["email"],
                                            "error": str(e)[:140]})
            c.commit()
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    return jsonify(
        ok=True,
        dry_run=not (confirm and request.method == "POST"),
        payers_total=len(rows),
        mrr_total_usd=round(sum(r["mrr_usd"] for r in rows), 2),
        by_class={k: len(v) for k, v in sorted(buckets.items())},
        mrr_at_risk_usd=round(
            sum(r["mrr_usd"] for r in rows
                if r["klass"] in ("no_access", "mcp_missing",
                                  "never_started", "dormant")), 2),
        provisioning_failures=fixable,
        provisioned=provisioned,
        email_armed=email_armed,
        note=(
            "Classified across BOTH surfaces on purpose. A payer with zero "
            "mcp_dev_keys rows is NOT necessarily unprovisioned — our largest "
            "research licensee (NLR, $3,000/yr) has none, plus four active "
            "enterprise REST keys and 960 calls made. Only no_access and "
            "mcp_missing are provisioning failures; dormant and never_started "
            "are activation work and minting another key would not touch them."),
        classes={
            "no_access": "neither MCP nor active REST key — provision",
            "mcp_missing": "REST works, MCP never provisioned — provision MCP",
            "dormant": "had access, used it, stopped — relationship, not provisioning",
            "never_started": "has access, never called — activation, not provisioning",
            "healthy": "has access and is calling",
        },
    )


def register_entitlement_reconcile(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(entitlement_reconcile_bp)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "entitlement_reconcile wiring failed: %s", e)
