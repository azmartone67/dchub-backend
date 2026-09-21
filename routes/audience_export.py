"""
audience_export.py — admin export of the free-tier user audience (2026-07-01).
==============================================================================

For upsell campaigns (e.g. Nico's Pro upsell): a one-call CSV of every FREE-tier
user with an email — first name, last name, email (+ company / tier / signup) —
with paid users and the email-suppression list excluded so the send is clean.

  GET /api/v1/admin/audience/free-users.csv           → CSV download
  GET /api/v1/admin/audience/free-users?format=json   → count + sample (preview)

Admin-gated (X-Admin-Key = DCHUB_ADMIN_KEY, or X-Internal-Key = DCHUB_INTERNAL_KEY).

"Free" = has a valid email AND a `users.plan` value POSITIVELY KNOWN not to
pay. Suppressed / unsubscribed / bounced addresses (email_suppression) and the
operator's own mailboxes are removed. Deduped by lower(email), newest kept.

★ INVERTED 2026-09-20, after a live read of this endpoint shipped 20 PAYING
  plans labelled "free". The filter was a denylist — `_PAID = ("pro",
  "founding", "enterprise")` — and `users.plan` holds more names than that.
  Measured on the 168 rows the CSV returned that day:

      free 148 · developer 15 · starter 4 · research_seed 1

  All 20 non-`free` rows shipped inside a file called free-users, and
  cross-joining them against /api/v1/admin/crm/export.csv on lower(email) put
  **6 `event_type='paid_conversion'` addresses in the send** — a `developer`
  and a `starter` among them. `tier_registry.TIERS` marks all four of
  developer/starter/research_seed/team paid; the denylist named three of seven.

  A denylist guarantees recurrence: it is wrong again the next time a plan is
  added, and it is wrong silently. `NON_PAID_PLANS` is an ALLOWLIST derived
  from the registry, so a name that is not positively known free — including a
  name the registry has never heard of — is EXCLUDED and labelled `unknown`,
  never `free`. `excluded_unknown_or_paid_plan` publishes what that removed,
  per plan name, so a new name is visible instead of silently mislabelled.

  Same defect, same inversion, same week as routes/warm_key_cohort.py, whose
  first live read put 26 paying customers in `mailable`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
audience_export_bp = Blueprint("audience_export", __name__)

from routes._audience_identity import is_operator_email  # noqa: E402

# If the registry cannot be imported we fall back to this. It is deliberately
# the NARROWEST defensible answer, not a copy of today's derived set: a
# fallback that is a SUBSET can only under-mail, while one that guesses wide
# re-creates the bug it is standing in for. `test_the_fallback_is_fail_closed`
# pins it as a subset of what the registry actually derives.
_FALLBACK_NON_PAID_PLANS = frozenset({"free", ""})

# A `users.plan` value that is blank/NULL is a signup that never chose a plan.
# That IS positively known free — unlike an unrecognised *name*, which is not.
_NO_PLAN_SET = frozenset({"", "none"})


def _derive_non_paid_plans() -> frozenset:
    """Plan names `tier_registry.TIERS` positively marks NOT paid.

    DERIVED, never typed. Two things follow that a denylist cannot give:
    a plan added to the registry as paid is excluded here the day it is added,
    and a plan name that reaches this column with NO registry entry at all is
    unknown — which is also excluded. `users.plan` speaks exactly the registry's
    vocabulary (free/starter/developer/pro/founding/team/enterprise/
    research_seed), which is why deriving is right here and was wrong for
    warm_key_cohort, whose column holds a coarser one.
    """
    try:
        from tier_registry import TIERS
        free = {str(n).strip().lower() for n, spec in TIERS.items()
                if not spec.get("paid")}
        if not free:                       # a registry with no free plan at
            raise ValueError("TIERS has no unpaid plan")   # all is not usable
    except Exception as e:  # noqa: BLE001
        logger.warning("[audience_export] tier_registry unavailable (%s) — "
                       "falling back to the narrow allowlist", e)
        free = set(_FALLBACK_NON_PAID_PLANS)
    return frozenset(free | _NO_PLAN_SET)


NON_PAID_PLANS = _derive_non_paid_plans()


def is_non_paid_plan(plan) -> bool:
    """True ONLY for a plan positively known not to pay.

    An unknown value answers False and is excluded. That is the safe direction:
    a plan we have never seen might be paid, and the cost of guessing wrong is
    an upsell pitched at someone who already bought — measured six times on
    2026-09-20.
    """
    return str(plan or "").strip().lower() in NON_PAID_PLANS


def plan_label(plan) -> str:
    """The label a row may carry — `unknown` for anything not positively known
    free, NEVER `free`.

    The second layer, and not a redundant one: the SQL allowlist decides who is
    in the file, this decides what the file CALLS them, and the old code's
    `(tier or "free")` is exactly how a row with no plan set acquired the word
    `free` on the way out. If the two layers ever disagree, the row says so.
    """
    p = str(plan or "").strip().lower()
    if p not in NON_PAID_PLANS:
        return "unknown"
    return "free" if p in _NO_PLAN_SET else p


def _admin_ok() -> bool:
    import hmac
    ak = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    ik = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    got_a = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    got_i = (request.headers.get("X-Internal-Key") or "").strip()
    if ak and got_a and hmac.compare_digest(got_a, ak):
        return True
    if ik and got_i and hmac.compare_digest(got_i, ik):
        return True
    return False


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception as e:
        logger.warning("[audience_export] no DB: %s", e)
        return None


def _return(c, error=False):
    try:
        from main import return_pg_connection
        return_pg_connection(c, error=error)
    except Exception:
        try: c.close()
        except Exception: pass


def _fmt_date(v):
    if not v:
        return ""
    try:
        return v.date().isoformat()   # datetime/date object
    except AttributeError:
        return str(v)[:10]            # TEXT column → 'YYYY-MM-DD...'


def _split_name(name: str):
    n = (name or "").strip()
    if not n or "@" in n:          # skip empties / emails-as-name
        return ("", "")
    parts = n.split()
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _gather():
    """Return (rows, meta). rows = list of dicts first_name/last_name/email/...."""
    c = _conn()
    if c is None:
        return [], {"error": "no_db"}
    suppressed = set()
    users = []
    non_paid = sorted(NON_PAID_PLANS)
    excluded_plans, errors = None, {}
    try:
        with c.cursor() as cur:
            # Suppression set (best-effort — table/column may vary).
            try:
                cur.execute("SELECT lower(email) FROM email_suppression WHERE email IS NOT NULL")
                suppressed = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:
                errors["suppression"] = f"{type(e).__name__}: {str(e)[:90]}"
                try: c.rollback()
                except Exception: pass
            # ★ What the allowlist removed, BY PLAN NAME. Its own statement and
            # its own try: a plan appearing here that SHOULD be mailable is the
            # signal that NON_PAID_PLANS needs a new member, and a read that
            # fails must name itself rather than publish {} — "nothing was
            # excluded" and "we could not tell what was excluded" demand
            # opposite responses.
            try:
                cur.execute(
                    """
                    SELECT COALESCE(lower(trim(plan)), '') AS plan, COUNT(*) AS n
                      FROM users
                     WHERE email IS NOT NULL AND email <> '' AND position('@' in email) > 1
                       AND COALESCE(lower(trim(plan)), '') <> ALL(%s)
                     GROUP BY 1 ORDER BY 2 DESC
                    """, (non_paid,))
                excluded_plans = {r[0] or "(none)": int(r[1] or 0)
                                  for r in (cur.fetchall() or [])}
            except Exception as e:
                errors["excluded_plans"] = f"{type(e).__name__}: {str(e)[:90]}"
                excluded_plans = None
                try: c.rollback()
                except Exception: pass
            # Free users with a real email, newest signup first for dedup.
            # ★ `= ANY(%s)` — an ALLOWLIST. A Python predicate that fails safe
            # is no help if the SQL still enumerates what to leave out.
            cur.execute(
                """
                SELECT email, name, company, plan, created_at
                  FROM users
                 WHERE email IS NOT NULL AND email <> '' AND position('@' in email) > 1
                   AND COALESCE(lower(trim(plan)), '') = ANY(%s)
                 ORDER BY created_at DESC NULLS LAST
                """, (non_paid,))
            for email, name, company, plan, created in cur.fetchall():
                users.append((email, name, company, plan, created))
    except Exception as e:
        _return(c, error=True)
        return [], {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    _return(c)

    rows, seen, with_name = [], set(), 0
    suppressed_hits = operator_hits = not_free_hits = 0
    for email, name, company, plan, created in users:
        key = email.strip().lower()
        if not key or key in seen:
            continue
        if key in suppressed:
            suppressed_hits += 1
            continue
        # Ours. A consumer mailbox with no dchub marker in it is still ours —
        # see routes/_audience_identity for why a marker list cannot see it.
        if is_operator_email(key):
            operator_hits += 1
            continue
        # Belt and braces against the two layers drifting: the SQL allowlist
        # should already have removed this row, so a hit here is a defect, and
        # it is counted and published rather than shipped mislabelled.
        if not is_non_paid_plan(plan):
            not_free_hits += 1
            continue
        seen.add(key)
        fn, ln = _split_name(name)
        if fn:
            with_name += 1
        rows.append({
            "first_name": fn, "last_name": ln, "email": email.strip(),
            "company": company or "", "tier": plan_label(plan),
            "signed_up": _fmt_date(created),
        })
    meta = {"total": len(rows), "with_name": with_name,
            "suppressed_excluded": suppressed_hits,
            "operator_excluded": operator_hits,
            "not_free_excluded_in_python": not_free_hits,
            "excluded_unknown_or_paid_plan": excluded_plans,
            "non_paid_plans": non_paid}
    if errors:
        meta["partial_reads"] = errors
        meta["partial_note"] = (
            "a sub-read failed and is named above; the field it feeds is 0 or "
            "null for UNKNOWN reasons, not because nothing was there")
    return rows, meta


@audience_export_bp.route("/api/v1/admin/audience/free-users.csv", methods=["GET"])
def free_users_csv():
    if not _admin_ok():
        return Response("unauthorized\n", status=401, mimetype="text/plain")
    rows, meta = _gather()
    if meta.get("error"):
        return Response(f"error: {meta['error']}\n", status=503, mimetype="text/plain")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_name", "last_name", "email", "company", "tier", "signed_up"])
    for r in rows:
        w.writerow([r["first_name"], r["last_name"], r["email"],
                    r["company"], r["tier"], r["signed_up"]])
    # ★ The CSV path publishes what the allowlist removed too. A caller who
    # only ever downloads the file must still be able to see a new plan name
    # turning up in the excluded bucket — otherwise the fix is visible only to
    # whoever happens to hit the JSON preview.
    ex = meta.get("excluded_unknown_or_paid_plan")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=dchub_free_users.csv",
                             "X-Total-Rows": str(meta["total"]),
                             "X-With-Name": str(meta["with_name"]),
                             "X-Operator-Excluded": str(meta["operator_excluded"]),
                             "X-Excluded-Unknown-Or-Paid-Plan":
                                 json.dumps(ex, sort_keys=True)
                                 if ex is not None else "unread"})


@audience_export_bp.route("/api/v1/admin/audience/free-users", methods=["GET"])
def free_users_json():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, **meta), 503
    return jsonify(ok=True, **meta, sample=rows[:10]), 200
