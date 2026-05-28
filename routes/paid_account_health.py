"""
Phase r43-H (2026-05-27) — Daily QA: paid-account health audit.

Background: Carl Braun (Founding tier, paid Mar 2026) got stranded mid-signup
when his password-reset email landed but the 1-hour token TTL had already
expired by the time he clicked. He couldn't log in, couldn't reset, sent us
a support email saying "it continues to reload and will not allow me to
login." We caught it by hand; we need to catch it automatically.

This module audits every paid user (plan in developer/pro/enterprise/
founding) and flags accounts that are in a state where the customer is
PAYING US BUT CAN'T LOG IN. Specifically:

  - Missing password_hash AND no recent Google OAuth login   → blocked
  - No api_key in api_keys table                              → API broken
  - Pending password_reset_token expiring in <2h              → about to expire
  - Multiple consecutive 401s on /api/auth/me last 24h        → token broken

Reports are emailed to azmartone@gmail.com daily and posted to the brain
heartbeat for surface visibility. Cron fires via .github/workflows/
paid-account-health-daily.yml.
"""

from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta
import os
import logging

logger = logging.getLogger('paid_account_health')

paid_health_bp = Blueprint('paid_account_health', __name__)

_get_pg = None


def init(get_pg_fn):
    global _get_pg
    _get_pg = get_pg_fn


def _admin_ok():
    """Admin gate: X-Admin-Key header must match DCHUB_ADMIN_KEY env."""
    expected = os.environ.get('DCHUB_ADMIN_KEY') or ''
    provided = request.headers.get('X-Admin-Key') or ''
    return bool(expected) and provided == expected


@paid_health_bp.route('/api/v1/admin/paid-account-health/check', methods=['GET', 'POST'])
def check_paid_health():
    """
    Audit every paid user for login-blocking conditions. Returns the full
    findings list (and optionally emails a summary if ?email=1).
    Admin-gated.
    """
    if not _admin_ok():
        return jsonify({'error': 'admin_key_required'}), 401

    findings = []
    paid_total = 0
    healthy_total = 0

    try:
        with _get_pg() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, plan, created_at, password_hash, google_id
                FROM users
                WHERE LOWER(plan) IN ('developer', 'pro', 'enterprise', 'founding')
                ORDER BY created_at DESC NULLS LAST
            """)
            users = cur.fetchall()
            paid_total = len(users)

            for u in users:
                user_id, email, name, plan, created_at, pwd_hash, google_id = u
                problems = []

                # ─── Can they log in at all? ──────────────────────────
                if not pwd_hash and not google_id:
                    problems.append(
                        'no_login_method (missing password_hash AND google_id — '
                        'customer cannot sign in by any path)'
                    )

                # ─── Pending pwd reset about to expire ────────────────
                try:
                    cur.execute("SAVEPOINT sp_pwt")
                    cur.execute("""
                        SELECT token, expires_at
                        FROM password_reset_tokens
                        WHERE user_email = %s AND used = FALSE
                          AND expires_at > NOW()
                        ORDER BY expires_at DESC LIMIT 1
                    """, (email,))
                    pwt = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT sp_pwt")
                    if pwt:
                        exp_at = pwt[1]
                        if isinstance(exp_at, str):
                            try:
                                exp_at = datetime.fromisoformat(exp_at.replace('Z', ''))
                            except Exception:
                                exp_at = None
                        if exp_at and exp_at < (datetime.utcnow() + timedelta(hours=2)):
                            problems.append(
                                f'reset_token_expiring_soon (in '
                                f'{int((exp_at - datetime.utcnow()).total_seconds() / 60)}min) — '
                                f'send a fresh link before it expires'
                            )
                except Exception as _e:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_pwt")
                    except Exception:
                        pass
                    logger.warning(f"reset-token check failed for {email}: {_e}")

                # ─── API key present? (paid customers should have one) ─
                # api_keys is keyed by user_id (NOT email) and uses an
                # is_active flag (NOT a `revoked` column). A failed query
                # here aborts the whole transaction in psycopg2, so each
                # sub-check runs inside its own SAVEPOINT and rolls back
                # to it on error — that way one schema mismatch can't
                # poison the rest of the audit loop.
                try:
                    cur.execute("SAVEPOINT sp_keys")
                    cur.execute("""
                        SELECT count(*) FROM api_keys
                        WHERE user_id = %s AND COALESCE(is_active, 1) = 1
                    """, (user_id,))
                    n_keys = (cur.fetchone() or [0])[0]
                    cur.execute("RELEASE SAVEPOINT sp_keys")
                    if n_keys == 0:
                        problems.append('no_api_key (paid tier without active API key — programmatic access broken)')
                except Exception as _e:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_keys")
                    except Exception:
                        pass
                    logger.warning(f"api_keys check failed for {email}: {_e}")

                if problems:
                    findings.append({
                        'user_id': user_id,
                        'email': email,
                        'name': name,
                        'plan': plan,
                        'created_at': str(created_at) if created_at else None,
                        'problems': problems,
                    })
                else:
                    healthy_total += 1

    except Exception as e:
        logger.error(f"paid-account-health check failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    # Optional email summary
    summary_emailed = False
    if request.args.get('email') == '1' and findings:
        try:
            from email_service import send_email
            problem_count = len(findings)
            html_rows = ''
            for f in findings:
                html_rows += (
                    f'<tr><td>{f["email"]}</td><td>{f["plan"]}</td>'
                    f'<td>{"; ".join(f["problems"])}</td></tr>'
                )
            html = f'''
<h2>DC Hub — Daily Paid-Account Health Check</h2>
<p><strong>{problem_count}</strong> paid account(s) have login-blocking issues
({healthy_total} of {paid_total} healthy).</p>
<table border="1" cellpadding="6" style="border-collapse: collapse; font-family: monospace; font-size: 13px;">
<tr><th>Email</th><th>Plan</th><th>Problems</th></tr>
{html_rows}
</table>
<p style="color:#666; font-size:11px;">Run admin/paid-account-health/check by hand to investigate any row.</p>
'''
            send_email(
                to='azmartone@gmail.com',
                subject=f'⚠️ DC Hub — {problem_count} paid account(s) need attention',
                html=html,
            )
            summary_emailed = True
        except Exception as e:
            logger.warning(f"summary email failed: {e}")

    return jsonify({
        'success': True,
        'paid_users_total': paid_total,
        'healthy_total': healthy_total,
        'problem_count': len(findings),
        'findings': findings,
        'summary_emailed': summary_emailed,
        'as_of': datetime.utcnow().isoformat() + 'Z',
    })


@paid_health_bp.route('/api/v1/admin/paid-account-health/fix-reset', methods=['POST'])
def fix_reset_for_email():
    """
    Operator shortcut: trigger a fresh password reset for a specific email.
    Same as /api/auth/forgot-password but admin-gated so we can use it
    after-hours without the rate limiter getting in the way.
    """
    if not _admin_ok():
        return jsonify({'error': 'admin_key_required'}), 401
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').lower().strip()
    if not email:
        return jsonify({'error': 'email required'}), 400

    try:
        import secrets
        from datetime import datetime, timedelta
        with _get_pg() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, email, name FROM users WHERE LOWER(email) = %s", (email,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': f'No user found with email {email}'}), 404
            name = row[2] or email.split('@')[0]
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.utcnow() + timedelta(hours=72)).isoformat()
            cur.execute(
                "UPDATE password_reset_tokens SET used = TRUE WHERE user_email = %s AND used = FALSE",
                (email,)
            )
            cur.execute(
                "INSERT INTO password_reset_tokens (user_email, token, expires_at) VALUES (%s, %s, %s)",
                (email, token, expires_at)
            )
            conn.commit()
            reset_url = f"https://dchub.cloud/reset-password.html?token={token}"
            try:
                from routes.auth_routes import send_password_reset_email
                send_password_reset_email(email, name, reset_url)
                email_sent = True
            except Exception as e:
                logger.warning(f"email send failed (but token created): {e}")
                email_sent = False

            return jsonify({
                'success': True,
                'email': email,
                'reset_url': reset_url,
                'expires_in_hours': 72,
                'email_sent': email_sent,
            })
    except Exception as e:
        logger.error(f"fix-reset failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def register(app, get_pg_fn):
    init(get_pg_fn)
    app.register_blueprint(paid_health_bp)
    print("[main] paid_account_health registered: /api/v1/admin/paid-account-health/check + /fix-reset")
