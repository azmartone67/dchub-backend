"""audience_keys_export.py — every KEYED address, every tier, one call (2026-09-20).

WHY THIS EXISTS
===============
Three admin exports already touch the audience and none of them answers
"give me the keyed users for a CRM import":

  `audience_export`      reads `users` — the WEB signup population, and it
                         excludes paid with a 3-name denylist.
  `warm_key_cohort`      reads `mcp_dev_keys` but EXCLUDES paid by design; it
                         is an outreach cohort, not an export, and it drops the
                         per-key fields a CRM wants.
  `crm_reverse_etl`      reads the outbound event queue — lead events, not key
                         rows, and only for addresses that tripped a capture
                         hook.

So a HubSpot import had to be stitched from all three by hand, and the paid
keyed customers could not be reached at all. This module is the one read that
covers the key table end to end.

  GET /api/v1/admin/audience/crm-keys.csv   -> the import file
  GET /api/v1/admin/audience/crm-keys       -> counts + a small sample

★ EVERY TIER, LABELLED — not filtered. `key_tier` is carried through verbatim
and `payment_status` is derived with `warm_key_cohort.is_non_paid_tier`, a
POSITIVE allowlist: a tier this codebase has never seen answers `unknown`,
never `non_paying`. That direction is deliberate. `audience_export` decides the
same question with `_PAID = ("pro","founding","enterprise")` and live
`users.plan` values include `developer`, `starter` and `research_seed`, so its
"free" export ships paying customers. A denylist re-breaks every time a plan is
added; this file must not acquire one.

★ THE NUMBER NOBODY COULD MEASURE. `skipped_no_email` (and its per-tier
breakdown) is published, because the interesting figure here is how much of the
key table has no address on it at all. Measured 2026-09-20 through the sibling
endpoint: 34 non-paid keys carry a parseable email while `warm_key_cohort`'s own
docstring claims 653 — the docstring counts key ROWS unfiltered. This endpoint
publishes both sides of that so the gap can never be silent again.

★ NEVER RETURNS api_key. NEVER WRITES. NEVER SETS CONSENT — `marketing_opt_in`
is echoed from the stored flag, and a bind is not consent. Internal/operator
addresses and the suppression list are removed and each removal is counted
separately, so a reader sees what every filter took.

Admin-gated by calling `audience_export._admin_ok`, not by restating it.
"""
from __future__ import annotations

import csv
import io
import logging
import re

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
audience_keys_export_bp = Blueprint("audience_keys_export", __name__)

# Most-entitled first. A person holding several keys is exported once, at the
# best tier they hold — pitching an enterprise customer off their old free key
# is the mistake this ordering exists to prevent.
_TIER_RANK = ("enterprise", "paid", "identified", "free")

# `metadata->>'client_name'` is CALLER-SUPPLIED and unvalidated: claim_key takes
# it off the request body and stores it (one caller minted keys as `pentest`,
# `pentest2` … `pentest12`). It is a hint, never an identity. Session ids also
# leak into client identity upstream — flask_mcp_endpoints drops UUID-shaped
# values from its platform column for exactly this reason, so do the same here
# rather than shipping a UUID into a CRM field called "platform".
_UUIDISH = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_CSV_FIELDS = [
    # The six a CRM import asks for, in order.
    "email", "key_tier", "mcp_platform", "dchub_session_ref",
    "created_at", "last_seen_at",
    # Everything else is context for segmenting, never for sending.
    "payment_status", "keys_held", "email_domain", "domain_kind",
    "marketing_opt_in", "mcp_platform_source",
]


def _admin_ok() -> bool:
    """The SAME gate audience_export applies, called not copied."""
    try:
        from routes.audience_export import _admin_ok as _gate
        return bool(_gate())
    except Exception:  # noqa: BLE001
        # Fail CLOSED. An import error must not open an endpoint that returns
        # every address we hold.
        logger.warning("[crm_keys] admin gate unavailable — refusing")
        return False


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception as e:  # noqa: BLE001
        logger.warning("[crm_keys] no DB: %s", e)
        return None


def _release(c, error=False):
    try:
        from main import return_pg_connection
        return_pg_connection(c, error=error)
    except Exception:  # noqa: BLE001
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _helpers():
    """Borrow warm_key_cohort's classifiers so there is one copy of each rule.

    Returns (is_internal, domain, domain_kind, is_non_paid_tier). If the import
    fails the caller must abort: guessing at `is_internal` would put our own
    probe addresses into a CRM, and guessing at `is_non_paid_tier` is the
    denylist bug this module's docstring is about.
    """
    from routes.warm_key_cohort import (  # noqa: PLC0415
        _domain, _domain_kind, _is_internal, is_non_paid_tier)
    return _is_internal, _domain, _domain_kind, is_non_paid_tier


def _platform(client_name):
    """(value, source) for the mcp_platform column.

    A UUID-shaped client_name is a leaked session id, not a platform.
    """
    v = (client_name or "").strip()
    if not v:
        return "", "none_recorded"
    if _UUIDISH.match(v):
        return "", "dropped_uuid_shaped"
    return v[:80], "metadata.client_name (caller-supplied, unvalidated)"


def _iso(v):
    if not v:
        return ""
    try:
        return v.isoformat(timespec="seconds")
    except (AttributeError, TypeError):
        return str(v)[:19]


def _payment_status(tier, is_non_paid) -> str:
    t = (tier or "").strip().lower()
    if t in ("paid", "enterprise"):
        return "paying"
    if is_non_paid(t):
        return "non_paying"
    return "unknown"          # never seen this tier — do not guess downward


def _rank(tier) -> int:
    t = (tier or "").strip().lower()
    return _TIER_RANK.index(t) if t in _TIER_RANK else len(_TIER_RANK)


def _gather() -> tuple:
    """Return (rows, meta). Read-only. Raises nothing the caller must catch."""
    try:
        is_internal, domain, domain_kind, is_non_paid = _helpers()
    except Exception as e:  # noqa: BLE001
        return [], {"error": f"classifiers unavailable: {type(e).__name__}"}

    c = _conn()
    if c is None:
        return [], {"error": "no_db"}

    errors, suppressed = {}, set()
    keys, no_email_total, no_email_by_tier = [], None, {}
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT lower(email) FROM email_suppression "
                            "WHERE email IS NOT NULL")
                suppressed = {r[0] for r in (cur.fetchall() or []) if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["suppression"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # ★ How much of the key table has NO address. Published, not assumed.
            try:
                cur.execute(
                    """SELECT lower(COALESCE(tier,'')) AS tier, COUNT(*) AS n
                         FROM mcp_dev_keys
                        WHERE status = 'active'
                          AND (email IS NULL OR email = ''
                               OR position('@' in email) <= 1)
                        GROUP BY 1 ORDER BY 2 DESC""")
                no_email_by_tier = {r[0] or "(none)": int(r[1] or 0)
                                    for r in (cur.fetchall() or [])}
                no_email_total = sum(no_email_by_tier.values())
            except Exception as e:  # noqa: BLE001
                errors["no_email"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # THE EXPORT: one row per active key that carries a real address,
            # EVERY tier. Newest key first so the per-address fields below are
            # taken from the most recent mint.
            try:
                cur.execute(
                    """SELECT lower(trim(email))              AS email,
                              lower(COALESCE(tier,''))        AS tier,
                              created_at,
                              last_used_at,
                              metadata->>'client_name'        AS client_name,
                              metadata->>'session_id'         AS session_id,
                              metadata->>'marketing_opt_in'   AS opt_in
                         FROM mcp_dev_keys
                        WHERE status = 'active'
                          AND email IS NOT NULL AND email <> ''
                          AND position('@' in email) > 1
                        ORDER BY created_at DESC NULLS LAST""")
                keys = list(cur.fetchall() or [])
            except Exception as e:  # noqa: BLE001
                _release(c, error=True)
                return [], {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    except Exception as e:  # noqa: BLE001
        _release(c, error=True)
        return [], {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    _release(c)

    by_tier, skipped_internal, skipped_suppressed, merged = {}, 0, 0, {}
    for email, tier, created, last_used, client_name, session_id, opt_in in keys:
        email = (email or "").strip()
        if not email:
            continue
        by_tier[tier or "(none)"] = by_tier.get(tier or "(none)", 0) + 1
        if is_internal(email):
            skipped_internal += 1
            continue
        if email in suppressed:
            skipped_suppressed += 1
            continue
        r = merged.get(email)
        if r is None:
            plat, plat_src = _platform(client_name)
            r = {
                "email": email, "key_tier": tier or "free",
                "mcp_platform": plat, "mcp_platform_source": plat_src,
                "dchub_session_ref": (session_id or "").strip()[:120],
                "created_at": _iso(created), "last_seen_at": _iso(last_used),
                "keys_held": 1, "email_domain": domain(email),
                "domain_kind": domain_kind(email),
                "marketing_opt_in": str(
                    str(opt_in or "").strip().lower() == "true").lower(),
            }
            merged[email] = r
            continue
        # Same address, another key. Best tier, widest window, most context.
        r["keys_held"] += 1
        if _rank(tier) < _rank(r["key_tier"]):
            r["key_tier"] = tier or "free"
        ca = _iso(created)
        if ca and (not r["created_at"] or ca < r["created_at"]):
            r["created_at"] = ca            # first time we ever saw them
        lu = _iso(last_used)
        if lu > r["last_seen_at"]:
            r["last_seen_at"] = lu          # most recent activity on any key
        if not r["mcp_platform"]:
            r["mcp_platform"], r["mcp_platform_source"] = _platform(client_name)
        if not r["dchub_session_ref"]:
            r["dchub_session_ref"] = (session_id or "").strip()[:120]
        if str(opt_in or "").strip().lower() == "true":
            r["marketing_opt_in"] = "true"

    rows, status_counts = [], {}
    for r in merged.values():
        r["payment_status"] = _payment_status(r["key_tier"], is_non_paid)
        status_counts[r["payment_status"]] = status_counts.get(
            r["payment_status"], 0) + 1
        rows.append(r)
    rows.sort(key=lambda x: (x["created_at"] or ""), reverse=True)

    meta = {
        "basis": (
            "mcp_dev_keys WHERE status='active' AND a parseable email, EVERY "
            "tier, deduped by lower(email) at the most-entitled tier held. "
            "key_tier is carried through verbatim; payment_status is derived "
            "with a POSITIVE non-paid allowlist, so an unrecognised tier reads "
            "'unknown' rather than 'non_paying'."),
        "keys_with_email": len(keys),
        "keys_with_email_by_tier": by_tier,
        "rows": len(rows),
        "by_payment_status": status_counts,
        "with_mcp_platform": sum(1 for r in rows if r["mcp_platform"]),
        "with_session_ref": sum(1 for r in rows if r["dchub_session_ref"]),
        "with_last_seen_at": sum(1 for r in rows if r["last_seen_at"]),
        "marketing_opt_in_true": sum(
            1 for r in rows if r["marketing_opt_in"] == "true"),
        "skipped_no_email": no_email_total,
        "skipped_no_email_by_tier": no_email_by_tier or None,
        "skipped_internal_or_operator": skipped_internal,
        "skipped_suppressed": skipped_suppressed,
        "consent_note": (
            "marketing_opt_in is ECHOED from the stored flag. A key bind is not "
            "marketing consent. This file is a CRM import, not a send list."),
    }
    if errors:
        meta["errors"] = errors
    return rows, meta


@audience_keys_export_bp.route("/api/v1/admin/audience/crm-keys",
                               methods=["GET"])
def crm_keys_json():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, error=meta["error"]), 200
    out = {"ok": True, "metric": "crm_keyed_contacts", **meta}
    # Addresses are the payload of the .csv route; the JSON route is the shape
    # and the counts, with just enough rows to sanity-check a column.
    if request.args.get("rows") == "all":
        out["rows_data"] = rows
    else:
        out["sample"] = rows[:5]
        out["sample_note"] = ("5 newest rows. Pass rows=all for every row, or "
                              "use the .csv route.")
    return jsonify(out), 200


@audience_keys_export_bp.route("/api/v1/admin/audience/crm-keys.csv",
                               methods=["GET"])
def crm_keys_csv():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, error=meta["error"]), 200
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    only = (request.args.get("payment_status") or "").strip().lower()
    for r in rows:
        # ★ Default is EVERY tier, including paying customers, because the
        # caller asked for a CRM import and a CRM wants its customers. A send
        # list is a different question: ?payment_status=non_paying narrows it,
        # and consent still has to be checked per row.
        if only and r["payment_status"] != only:
            continue
        w.writerow(r)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="dchub-crm-keys.csv"',
                 "X-DCHub-Rows": str(meta.get("rows") or 0),
                 "X-DCHub-Skipped-No-Email": str(meta.get("skipped_no_email")),
                 "Cache-Control": "no-store"})


def register_audience_keys_export(app) -> None:
    app.register_blueprint(audience_keys_export_bp)
