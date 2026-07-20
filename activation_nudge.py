"""
activation_nudge.py — 48h post-purchase activation nudge (2026-07-16).

The real gap behind "stranded" paying customers (e.g. Founding members Myke
Miller, Youssef Fahmy) was NOT delivery or provisioning — welcome_email_log
shows their key emails delivered via Resend and their keys are is_active=1.
They simply never made a first API call. This job closes that behavioral gap:
it finds paid + zero-call accounts that are past the first 48h but still recent,
that we have NOT already nudged, and sends ONE warm "make your first query"
email (Resend-first, reply-to the founder so a human catches the reply).

SAFETY (this touches real paying customers' inboxes):
  * DRY-RUN by DEFAULT. Sends only when explicitly armed via ?confirm=1 or
    ACTIVATION_NUDGE_ARM=1. Un-armed calls return exactly who WOULD be nudged.
  * Idempotent: email_drip_log UNIQUE(user_email, email_key='activation_nudge')
    + a NOT EXISTS filter — a customer is nudged at most once, ever.
  * Capped per run (default 25, hard max 200).
  * Skips internal/test addresses.
Register with setup_activation_nudge_routes(app). Trigger:
  POST /api/v1/admin/activation-nudge/run           (X-Admin-Key)  -> DRY-RUN preview
  POST /api/v1/admin/activation-nudge/run?confirm=1 (X-Admin-Key)  -> actually sends
"""
import os
import logging

logger = logging.getLogger("activation_nudge")

# r-starter-included (2026-07-20): 'starter' ($9/mo) was MISSING here, so every
# Starter customer was invisible to the activation nudge regardless of window —
# a paying customer (marvinvitcu@gmail.com, subscribed 2026-06-18, 0 calls in a
# month+) sat stranded because their users.plan label 'starter' wasn't a "paid"
# plan to this job. Include it and its variants.
PAID_PLANS = ('starter', 'developer', 'pro', 'paid', 'enterprise', 'founding')
EMAIL_KEY = 'activation_nudge'


def _get_conn():
    """Direct psycopg2 connection to Neon (autocommit)."""
    import psycopg2
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not url:
        raise Exception("No NEON_DATABASE_URL or DATABASE_URL set")
    conn = psycopg2.connect(url, connect_timeout=8)
    conn.autocommit = True
    return conn


def _find_candidates(min_age_hours=48, max_age_days=45, limit=25):
    """Paid + zero-call accounts in the activation window that we haven't nudged.

    'Zero calls' sums calls_total + usage_count across ALL of the user's keys, so
    a customer who made a single call on any key is excluded. NOT EXISTS against
    email_drip_log guarantees one-nudge-ever.

    NOTE: users.created_at is stored as TEXT (inconsistent formats), so the age
    window is filtered in Python — mirroring _admin_welcome_sequence — rather than
    in SQL, so one un-parseable row can't break the query.
    """
    sql = """
        SELECT u.email, u.name, u.plan, u.created_at,
               COALESCE(SUM(COALESCE(k.calls_total, 0) + COALESCE(k.usage_count, 0)), 0) AS calls
        FROM users u
        LEFT JOIN api_keys k ON k.user_id::text = u.id::text
        WHERE u.plan IN %s
          AND u.email IS NOT NULL AND u.email <> ''
          AND NOT EXISTS (
              SELECT 1 FROM email_drip_log d
              WHERE lower(d.user_email) = lower(u.email) AND d.email_key = %s
          )
        GROUP BY u.email, u.name, u.plan, u.created_at
        HAVING COALESCE(SUM(COALESCE(k.calls_total, 0) + COALESCE(k.usage_count, 0)), 0) = 0
        ORDER BY u.created_at DESC
        LIMIT 500
    """
    from datetime import datetime as DT, timedelta, timezone
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, (PAID_PLANS, EMAIL_KEY))
        rows = cur.fetchall()
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    now = DT.now(timezone.utc)
    must_be_older_than = now - timedelta(hours=min_age_hours)  # created BEFORE this
    must_be_newer_than = now - timedelta(days=max_age_days)    # created AFTER this
    out = []
    for r in rows:
        created_raw = r[3]
        try:
            cdt = created_raw if hasattr(created_raw, 'year') else \
                DT.fromisoformat(str(created_raw).replace('Z', '').strip())
            if cdt.tzinfo is None:
                cdt = cdt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if not (must_be_newer_than < cdt < must_be_older_than):
            continue
        out.append({'email': r[0], 'name': r[1], 'plan': r[2], 'created_at': cdt.isoformat()})
        if len(out) >= limit:
            break
    return out


def _log_sent(email):
    """Reserve/record the nudge. UNIQUE(user_email, email_key) makes it a no-op
    if already present — safe against overlap."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO email_drip_log (user_email, email_key, status) "
            "VALUES (%s, %s, 'sent') ON CONFLICT (user_email, email_key) DO NOTHING",
            (email, EMAIL_KEY))
        cur.close()
    except Exception as e:
        logger.warning("activation_nudge: failed to log send for %s: %s", email, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _nudge_html(first, plan):
    plan_disp = (plan or '').replace('_', ' ').title() or 'DC Hub'
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a2e;line-height:1.6;font-size:16px;">
<p>Hi {first},</p>
<p>You're all set up on DC Hub {plan_disp} — but I noticed you haven't run your first query yet, so I wanted to check in personally.</p>
<p>The fastest way to see value in about 30 seconds, no setup: <a href="https://dchub.cloud/playground">the live playground</a> — try the grid scoreboard, or drop a site into the Land &amp; Power map for real-time power, fiber, gas, and environmental layers.</p>
<p>Want it wired into Claude, Cursor, or your own agent? Just reply "setup" and I'll get you connected in about two minutes — happy to screen-share.</p>
<p>What are you working on? Tell me and I'll point you at the right tools.</p>
<p>— Jonathan Martone<br>DC Hub · Martone Advisors<br>jonathan@dchub.cloud</p>
</body></html>"""


def run_activation_nudge(armed=False, limit=25, min_age_hours=48, max_age_days=45):
    """Find and (if armed) nudge paid + zero-call customers. Returns a report."""
    try:
        from dchub_outreach import is_internal_email
    except Exception:
        def is_internal_email(e):
            return (not e) or '@dchub.cloud' in (e or '').lower()

    try:
        cands = _find_candidates(min_age_hours, max_age_days, limit * 2)
    except Exception as e:
        logger.warning("activation_nudge: candidate query failed: %s", e)
        return {'ok': False, 'error': str(e)[:200]}

    cands = [c for c in cands if not is_internal_email(c.get('email'))][:limit]
    armed = bool(armed) or os.environ.get('ACTIVATION_NUDGE_ARM') == '1'
    result = {'ok': True, 'armed': armed, 'window': f'{min_age_hours}h..{max_age_days}d',
              'candidates': len(cands), 'sent': 0, 'errors': 0, 'preview': cands}

    if not armed:
        result['note'] = ('DRY-RUN — no emails sent. These are the customers who WOULD be '
                          'nudged. Arm with ?confirm=1 or ACTIVATION_NUDGE_ARM=1.')
        return result

    from email_fallback import send_email_resilient
    subject = "Your DC Hub access is live — want a hand with your first query?"
    for c in cands:
        email = c['email']
        first = ((c.get('name') or '').strip().split(' ') or ['there'])[0] or 'there'
        try:
            ok = send_email_resilient(
                email, subject,
                html_content=_nudge_html(first, c.get('plan')),
                from_email='jonathan@dchub.cloud', from_name='Jonathan Martone')
            if ok:
                _log_sent(email)
                result['sent'] += 1
            else:
                result['errors'] += 1
        except Exception as e:
            logger.warning("activation_nudge: send failed for %s: %s", email, e)
            result['errors'] += 1
    return result


def setup_activation_nudge_routes(app):
    """Register the admin trigger route."""
    from flask import request, jsonify

    @app.route('/api/v1/admin/activation-nudge/run', methods=['POST', 'GET'])
    def _admin_activation_nudge():
        provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
        if provided != os.environ.get('DCHUB_ADMIN_KEY', ''):
            return jsonify({'error': 'unauthorized', 'hint': 'X-Admin-Key required'}), 401
        armed = request.args.get('confirm') == '1'
        try:
            limit = max(1, min(int(request.args.get('limit', 25)), 200))
        except Exception:
            limit = 25
        try:
            min_age_hours = max(1, min(int(request.args.get('min_age_hours', 48)), 720))
        except Exception:
            min_age_hours = 48
        try:
            max_age_days = max(1, min(int(request.args.get('max_age_days', 45)), 120))
        except Exception:
            max_age_days = 21
        return jsonify(run_activation_nudge(
            armed=armed, limit=limit,
            min_age_hours=min_age_hours, max_age_days=max_age_days))
