"""
Lost-conversion outreach (Phase FF+15-outreach, 2026-05-19)
============================================================

We had 5,222 paywall signals in 7d but only 6 conversions in 30d. Two
sequential bugs were the cause:

  FF+8     redeem_pair_code UPDATE was matching sha256("") instead of
           the actual api_key_hash — paid checkouts marked the
           pair-code redeemed but never flipped api_keys.plan.
  FF+15-funnel2  utils/paywall_response.py.build_paywall_response()
           (used by ~70% of paywall paths — get_grid_intelligence,
           get_fiber_intel, get_market_intel, ...) emitted Stripe
           Payment Links with NO client_reference_id. Webhooks
           arrived without attribution.

Some unknown subset of those 5,222 signals were users who actually
WANTED to pay and may have CLICKED through and even PAID — but their
api_keys never got upgraded because of the bugs above.

This module surfaces who they are (from mcp_upgrade_signals.user_email)
and lets us reach out with a working upgrade link + apology.

Endpoints (both admin-gated via X-Internal-Key, same as
/api/admin/load-substations-live):

  GET  /api/v1/admin/lost-conversion/candidates   DRY RUN list
  POST /api/v1/admin/lost-conversion/send         actually send

The send endpoint:
  - Marks each row outreach_sent=TRUE on success so the same person
    is never emailed twice.
  - Respects a `?limit=N` query param so you can send in batches.
  - Filters test fixtures, already-converted users, anyone who has
    been emailed before, and obviously-invalid email patterns.
"""
import os
import re
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

lost_conversion_outreach_bp = Blueprint("lost_conversion_outreach", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# Test-fixture emails to never message
_SUPPRESSED = {
    "qa-test@dchub.cloud", "test@dchub.cloud", "qa@dchub.cloud",
    "stripe-test@dchub.cloud", "admin@dchub.cloud", "api@dchub.cloud",
}


def _is_send_safe_email(em: str) -> bool:
    if not em or "@" not in em:
        return False
    em = em.strip().lower()
    if em in _SUPPRESSED:
        return False
    if em.endswith("@example.com") or em.endswith("@test.com"):
        return False
    # Basic sanity — RFC-lite, not perfect
    if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", em):
        return False
    return True


def _db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _query_candidates(min_signals=2, days=30, limit=500):
    """Return list of dicts: {email, signal_count, top_tool, first_signal_at,
    last_signal_at, signal_ids[]}.

    Logic: distinct emails with >=min_signals upgrade_signals in the last
    `days`, where:
      - email is non-test, non-suppressed
      - none of their signals have converted=TRUE
      - none of their signals have outreach_sent=TRUE
    """
    conn = _db()
    if conn is None:
        return [], "no_database"
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH user_signals AS (
                    SELECT
                        LOWER(TRIM(user_email)) AS email,
                        COUNT(*) AS signal_count,
                        MIN(created_at) AS first_signal_at,
                        MAX(created_at) AS last_signal_at,
                        MODE() WITHIN GROUP (ORDER BY tool_requested)
                            AS top_tool,
                        ARRAY_AGG(id) AS signal_ids,
                        BOOL_OR(converted) AS any_converted,
                        BOOL_OR(outreach_sent) AS any_outreach_sent
                    FROM mcp_upgrade_signals
                    WHERE user_email IS NOT NULL
                      AND user_email <> ''
                      AND created_at > NOW() - INTERVAL '%s days'
                    GROUP BY LOWER(TRIM(user_email))
                )
                SELECT email, signal_count, top_tool,
                       first_signal_at, last_signal_at, signal_ids
                FROM user_signals
                WHERE any_converted IS NOT TRUE
                  AND any_outreach_sent IS NOT TRUE
                  AND signal_count >= %s
                ORDER BY signal_count DESC, last_signal_at DESC
                LIMIT %s
            """ % (int(days), int(min_signals), int(limit)))
            rows = cur.fetchall()
            cand = []
            for r in rows:
                em = r[0]
                if not _is_send_safe_email(em):
                    continue
                cand.append({
                    "email": em,
                    "signal_count": int(r[1] or 0),
                    "top_tool": r[2] or "various tools",
                    "first_signal_at": r[3].isoformat() if r[3] else None,
                    "last_signal_at":  r[4].isoformat() if r[4] else None,
                    "signal_ids": list(r[5] or []),
                })
            return cand, None
    except Exception as e:
        return [], f"query_failed: {str(e)[:200]}"
    finally:
        try: conn.close()
        except Exception: pass


def _build_email(candidate: dict) -> dict:
    """Return {subject, html, text} for a single recipient."""
    tool = (candidate.get("top_tool") or "DC Hub Pro tools").replace("get_", "").replace("_", " ")
    signal_count = candidate.get("signal_count") or 1
    subject = "We fixed the DC Hub upgrade bug that blocked your access"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1f2937;line-height:1.55;padding:24px">
  <h2 style="margin:0 0 16px">Apology + fix from DC Hub</h2>

  <p>Hi —</p>

  <p>Our logs show you tried to access <strong>{tool}</strong> on DC Hub
  {signal_count} time{'s' if signal_count != 1 else ''} in the last 30 days
  and hit our paywall. If you tried to upgrade and it didn't seem to work,
  that's not on you.</p>

  <p>We had two coupled bugs in our checkout attribution pipeline that
  meant Stripe payments completed but our system didn't flip the
  associated API key from <code>free</code> to <code>pro</code>.
  Both bugs landed yesterday (May 19) — here's the postmortem in plain
  English:</p>

  <ul>
    <li>The webhook handler used the SHA-256 of an empty string instead of
    your actual key hash when looking up which row to upgrade. We
    matched zero rows on every successful checkout.</li>
    <li>One of two paywall response builders was emitting Stripe payment
    links without the <code>client_reference_id</code> that the webhook
    needs to identify your account.</li>
  </ul>

  <p><strong>Both are fixed.</strong> Try the upgrade again — it'll work this
  time:</p>

  <p style="text-align:center;margin:24px 0">
    <a href="https://dchub.cloud/pricing?utm_source=lost_conversion_outreach&amp;utm_content={tool}"
       style="display:inline-block;padding:14px 28px;background:#10b981;color:#fff;
              text-decoration:none;border-radius:8px;font-weight:700;font-size:15px">
      Upgrade to Pro →
    </a>
  </p>

  <p>If you already paid and didn't get upgraded, reply to this email with
  your Stripe receipt and we'll fix it manually within the hour. If you've
  since moved on to another tool, we get it — and we're sorry we wasted
  your time.</p>

  <p style="color:#6b7280;font-size:13px;margin-top:32px;border-top:1px solid #e5e7eb;padding-top:16px">
    DC Hub · Data center intelligence · 21,000+ facilities · Live MCP server<br>
    <a href="https://dchub.cloud" style="color:#1e40af">dchub.cloud</a>
    &nbsp;·&nbsp;
    <a href="https://dchub.cloud/unsubscribe?email={candidate['email']}"
       style="color:#9ca3af">unsubscribe</a>
  </p>
</body></html>
"""
    text = (
        f"Hi —\n\n"
        f"Our logs show you tried to access {tool} on DC Hub "
        f"{signal_count} time{'s' if signal_count != 1 else ''} in the last "
        f"30 days and hit our paywall. If you tried to upgrade and it "
        f"didn't seem to work, that's not on you.\n\n"
        f"We had two coupled bugs in checkout attribution. Payments "
        f"completed but our system didn't flip your API key from free "
        f"to pro. Both bugs landed yesterday (May 19) and are fixed.\n\n"
        f"Try again — it'll work this time:\n"
        f"https://dchub.cloud/pricing?utm_source=lost_conversion_outreach&utm_content={tool}\n\n"
        f"If you already paid and didn't get upgraded, reply with your "
        f"Stripe receipt and we'll fix it manually within the hour.\n\n"
        f"— DC Hub team\n"
        f"unsubscribe: https://dchub.cloud/unsubscribe?email={candidate['email']}\n"
    )
    return {"subject": subject, "html": html, "text": text}


def _mark_outreach_sent(signal_ids: list, conn=None):
    if not signal_ids:
        return 0
    own_conn = conn is None
    if own_conn:
        conn = _db()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mcp_upgrade_signals "
                "SET outreach_sent = TRUE, "
                "    outreach_sent_at = NOW() "
                "WHERE id = ANY(%s)",
                (list(signal_ids),))
            n = cur.rowcount or 0
        conn.commit()
        return n
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        if own_conn:
            try: conn.close()
            except Exception: pass


@lost_conversion_outreach_bp.get("/api/v1/admin/lost-conversion/candidates")
def candidates():
    """DRY RUN: list candidates the outreach would email. No side effects."""
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403

    min_signals = int(request.args.get("min_signals", "2"))
    days        = int(request.args.get("days", "30"))
    limit       = min(int(request.args.get("limit", "500")), 1000)
    show_email_preview = request.args.get("preview", "0") == "1"

    cand, err = _query_candidates(min_signals=min_signals, days=days, limit=limit)
    if err:
        return jsonify(error=err), 500

    preview = None
    if show_email_preview and cand:
        preview = _build_email(cand[0])

    return jsonify(
        ok=True,
        dry_run=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        filters={
            "min_signals": min_signals,
            "days":        days,
            "exclude_converted":     True,
            "exclude_already_sent":  True,
            "exclude_test_emails":   True,
        },
        candidate_count=len(cand),
        sample_top_5=cand[:5],
        sample_email_preview=preview,
        full_list_url=request.url + "&full=1" if request.args.get("full") != "1" else None,
        full_list=cand if request.args.get("full") == "1" else None,
        send_url=("POST /api/v1/admin/lost-conversion/send "
                  "?confirm=true&limit=N (X-Internal-Key required)"),
    )


@lost_conversion_outreach_bp.post("/api/v1/admin/lost-conversion/send")
def send():
    """Actually send the outreach emails. Requires:
      - X-Internal-Key header (admin gate)
      - ?confirm=true query param (explicit opt-in)
      - ?limit=N optional batch size (defaults to 25 to stay safe)
    """
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    if request.args.get("confirm") != "true":
        return jsonify(
            error="confirmation_required",
            hint="Pass ?confirm=true to actually send. Use the "
                 "candidates endpoint first to dry-run.",
        ), 400

    min_signals = int(request.args.get("min_signals", "2"))
    days        = int(request.args.get("days", "30"))
    limit       = min(int(request.args.get("limit", "25")), 200)

    cand, err = _query_candidates(min_signals=min_signals, days=days, limit=limit)
    if err:
        return jsonify(error=err), 500

    # Try to import the email sender (best-effort — degrade with error)
    try:
        from email_service import send_email
    except Exception:
        return jsonify(error="email_service_unavailable",
                       candidates_found=len(cand)), 503

    sent_ok = []
    failed = []
    for c in cand:
        try:
            msg = _build_email(c)
            result = send_email(c["email"], msg["subject"], msg["html"], msg["text"])
            # send_email may return dict {ok: True} or {status: ...}
            ok = bool(result and (result.get("ok") or result.get("status") == "sent"
                                  or result.get("success")))
            if ok:
                _mark_outreach_sent(c["signal_ids"])
                sent_ok.append(c["email"])
            else:
                failed.append({"email": c["email"], "reason": str(result)[:120]})
        except Exception as e:
            failed.append({"email": c["email"], "reason": str(e)[:120]})

    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        attempted=len(cand),
        sent=len(sent_ok),
        failed=len(failed),
        sent_emails=sent_ok,
        failures=failed[:10],   # cap so response doesn't explode
        next_batch_url=(f"/api/v1/admin/lost-conversion/send"
                        f"?confirm=true&limit={limit}"
                        if len(cand) == limit else None),
    )
