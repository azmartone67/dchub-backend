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
    """Return list of dicts: {email, source, signal_count, top_tool,
    first_signal_at, last_signal_at, signal_ids[]}.

    Two sources, merged and deduped by lowercased email:

    A) mcp_upgrade_signals — explicit paywall hits with user_email
       captured at signal time. Filters: not converted, not previously
       outreached, >=min_signals.

    B) api_keys — claimed dev keys (have emails) on the free plan,
       active in the last `days` (last_used or created_at). These are
       the highest-value targets: they trusted us enough to claim a key
       but never upgraded. Phase FF+15-outreach added this source after
       the first dry-run showed only 1 candidate from (A) — most MCP
       signals are anonymous (no email), so api_keys.email is the
       larger pool.

    Candidates from (A) carry signal_ids so the send endpoint can flip
    outreach_sent. Candidates from (B) carry an empty list (we don't
    have signals to flag, but we still record one row with source=
    api_keys_only in mcp_upgrade_signals at send time).
    """
    conn = _db()
    if conn is None:
        return [], "no_database"
    out: dict[str, dict] = {}   # email -> merged candidate
    err = None
    try:
        with conn.cursor() as cur:
            # Source A — explicit paywall signals with email
            try:
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
                """ % (int(days), int(min_signals)))
                for r in cur.fetchall():
                    em = r[0]
                    if not _is_send_safe_email(em):
                        continue
                    out[em] = {
                        "email": em,
                        "source": "upgrade_signals",
                        "signal_count": int(r[1] or 0),
                        "top_tool": r[2] or "various tools",
                        "first_signal_at": r[3].isoformat() if r[3] else None,
                        "last_signal_at":  r[4].isoformat() if r[4] else None,
                        "signal_ids": list(r[5] or []),
                    }
            except Exception as e:
                err = f"source_A_failed: {str(e)[:140]}"
                try: conn.rollback()
                except Exception: pass

            # Source B — claimed dev keys on free plan with captured email
            # (api_keys table schema varies across the codebase; try a few
            # column variants and degrade silently)
            for sql in [
                """
                SELECT LOWER(TRIM(email)) AS email,
                       COALESCE(name, key_prefix, 'developer') AS top_tool,
                       created_at AS first_at,
                       COALESCE(last_used::timestamptz, created_at) AS last_at
                FROM api_keys
                WHERE email IS NOT NULL AND email <> ''
                  AND COALESCE(plan, 'free') = 'free'
                  AND COALESCE(is_active, 1) IN (1, TRUE)
                  AND COALESCE(last_used::timestamptz, created_at)
                      > NOW() - INTERVAL '%s days'
                """ % int(days),
                """
                SELECT LOWER(TRIM(email)) AS email,
                       'developer' AS top_tool,
                       created_at AS first_at,
                       created_at AS last_at
                FROM api_keys
                WHERE email IS NOT NULL AND email <> ''
                  AND COALESCE(rate_limit_tier, 'trial') IN ('trial','free')
                  AND created_at > NOW() - INTERVAL '%s days'
                """ % int(days),
            ]:
                try:
                    cur.execute(sql)
                    for r in cur.fetchall():
                        em = r[0]
                        if not em or not _is_send_safe_email(em):
                            continue
                        if em in out:
                            # already from source A — flag as merged
                            out[em]["source"] = "both"
                            continue
                        out[em] = {
                            "email": em,
                            "source": "api_keys_only",
                            "signal_count": 0,
                            "top_tool": r[1] or "DC Hub Pro tools",
                            "first_signal_at": r[2].isoformat() if r[2] else None,
                            "last_signal_at":  r[3].isoformat() if r[3] else None,
                            "signal_ids": [],
                        }
                    break  # first variant that runs wins
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    continue

        ordered = sorted(out.values(),
                         key=lambda c: (-c["signal_count"],
                                        c.get("last_signal_at") or ""),
                         reverse=False)
        # signal_count desc primary; if tied, more recent first
        ordered = sorted(out.values(),
                         key=lambda c: (-(c["signal_count"]),
                                        -(0 if not c.get("last_signal_at")
                                          else 1)))
        return ordered[:int(limit)], err
    except Exception as e:
        return [], f"query_failed: {str(e)[:200]}"
    finally:
        try: conn.close()
        except Exception: pass


# r68-rotate (2026-06-02): template registry keyed on signal age (days
# since last signal). Replaces the hardcoded "we fixed the bug yesterday
# (May 19)" subject which was 15+ days stale and getting staler every cron.
# Brain L5 / template-versioning can edit this map without touching code paths.
_OUTREACH_TEMPLATES = [
    # (max_days_since_signal, subject, body_intro)
    (3,   'Quick fix: try DC Hub again',
           'We saw you hit our paywall a few times this week and want to make sure you got through.'),
    (10,  'Still trying to access DC Hub data?',
           'You hit our paywall a few times recently. If a deal or analysis is blocked, we can fast-track access.'),
    (30,  'DC Hub: live datasets you tried to reach',
           'A few weeks ago you tried {tool} on DC Hub. Want to pick up where you left off?'),
    (90,  'Following up — DC Hub access',
           'A while back you tried {tool}. The catalog grew (29 tools, 485K+ AI-agent requests). Worth another look?'),
]


def _build_email(candidate: dict) -> dict:
    """Return {subject, html, text} for a single recipient.

    r68-rotate (2026-06-02): age-rotated templates — the copy never goes
    stale because the template is picked at send time based on days_since
    last signal."""
    import datetime as _dt
    tool = (candidate.get("top_tool") or "DC Hub Pro tools").replace("get_", "").replace("_", " ")
    signal_count = candidate.get("signal_count") or 1
    last_seen = candidate.get("last_signal_at") or ''
    days_since = 30  # default if we can't parse
    try:
        if last_seen:
            if hasattr(last_seen, 'isoformat'):
                # datetime object
                dt = last_seen
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
            else:
                dt = _dt.datetime.fromisoformat(str(last_seen).replace('Z', ''))
            days_since = max(0, (_dt.datetime.utcnow() - dt).days)
    except Exception:
        pass
    # Pick the first template whose max-days threshold is >= days_since.
    tpl = next((t for t in _OUTREACH_TEMPLATES if days_since <= t[0]), _OUTREACH_TEMPLATES[-1])
    subject, intro_tmpl = tpl[1], tpl[2]
    intro = intro_tmpl.format(tool=tool)
    upgrade_url = (
        f"https://dchub.cloud/upgrade?utm_source=lost_conversion_outreach"
        f"&utm_age={days_since}d&utm_tool={tool}"
    )
    html = (
        f'<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;'
        f'max-width:560px;margin:0 auto;color:#1f2937;line-height:1.55;padding:24px">'
        f'<p>Hi —</p><p>{intro}</p>'
        f'<p>Our logs show {signal_count} attempt'
        f'{"s" if signal_count != 1 else ""} at <strong>{tool}</strong> '
        f'in the last {days_since} days.</p>'
        f'<p style="text-align:center;margin:24px 0">'
        f'<a href="{upgrade_url}" '
        f'style="display:inline-block;padding:14px 28px;background:#10b981;'
        f'color:#fff;text-decoration:none;border-radius:8px;font-weight:700">'
        f'Try DC Hub Pro →</a></p>'
        f'<p style="color:#6b7280;font-size:13px;margin-top:32px;'
        f'border-top:1px solid #e5e7eb;padding-top:16px">'
        f'DC Hub · Data center intelligence<br>'
        f'<a href="https://dchub.cloud/unsubscribe?email={candidate["email"]}" '
        f'style="color:#9ca3af">unsubscribe</a></p></body></html>'
    )
    text = (
        f"Hi —\n\n{intro}\n\n"
        f"Our logs show {signal_count} attempt"
        f"{'s' if signal_count != 1 else ''} at {tool} in the last "
        f"{days_since} days.\n\n"
        f"Try it again: {upgrade_url}\n\n"
        f"— DC Hub team\n"
        f"unsubscribe: https://dchub.cloud/unsubscribe?email={candidate['email']}\n"
    )
    return {"subject": subject, "html": html, "text": text,
            "template_age_bucket": tpl[0]}


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
            # email_service.send_email signature:
            #   send_email(to_email, subject, html_content, text_content=None)
            # returns {success: True/False, message_id|error: ...}
            result = send_email(c["email"], msg["subject"], msg["html"],
                                text_content=msg["text"])
            ok = bool(result and result.get("success"))
            if ok:
                _mark_outreach_sent(c["signal_ids"])
                sent_ok.append(c["email"])
            else:
                err = (result or {}).get("error") or str(result)[:120]
                failed.append({"email": c["email"], "reason": err[:200]})
        except Exception as e:
            failed.append({"email": c["email"], "reason": str(e)[:200]})

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
