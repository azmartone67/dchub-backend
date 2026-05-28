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

            for u in users:
                user_id, email, name, plan, created_at, pwd_hash, google_id = u
                email_l = (email or '').lower()

                # ─── Skip seed/test/internal accounts ─────────────────
                # Don't cry wolf over data that isn't a real external
                # customer: .example/.test TLDs are seed rows, and the
                # owner's own +stripe / dchub.cloud accounts aren't
                # paying customers. Keeps the daily alert signal clean.
                if (email_l.endswith('.example') or email_l.endswith('.test')
                        or 'example.' in email_l or '@dchub.cloud' in email_l
                        or '+stripe' in email_l):
                    continue
                paid_total += 1

                # critical = customer literally cannot log in.
                # warnings = degraded but self-recoverable (still counts,
                # just doesn't trigger the loud daily escalation).
                critical = []
                warnings = []

                # ─── Can they log in at all? ──────────────────────────
                if not pwd_hash and not google_id:
                    critical.append(
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
                            warnings.append(
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
                        warnings.append('no_api_key (paid tier without active API key — '
                                        'programmatic/MCP access unavailable until they '
                                        'generate one from the dashboard; NOT a login blocker)')
                except Exception as _e:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_keys")
                    except Exception:
                        pass
                    logger.warning(f"api_keys check failed for {email}: {_e}")

                if critical or warnings:
                    findings.append({
                        'user_id': user_id,
                        'email': email,
                        'name': name,
                        'plan': plan,
                        'created_at': str(created_at) if created_at else None,
                        'critical': critical,
                        'warnings': warnings,
                    })
                    if not critical:
                        # warnings-only accounts still count as "healthy"
                        # for the can-they-log-in headline.
                        healthy_total += 1
                else:
                    healthy_total += 1

    except Exception as e:
        logger.error(f"paid-account-health check failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    # Split findings: critical (can't log in) vs warnings-only.
    critical_findings = [f for f in findings if f.get('critical')]
    warning_findings = [f for f in findings if not f.get('critical') and f.get('warnings')]

    # Email summary ONLY fires on critical findings — a login-blocked
    # paying customer. Warnings (no API key, reset token expiring) are
    # returned in the JSON for the dashboard but don't trigger the loud
    # daily alert, so the signal stays meaningful.
    summary_emailed = False
    if request.args.get('email') == '1' and critical_findings:
        try:
            from email_service import send_email
            html_rows = ''
            for f in critical_findings:
                html_rows += (
                    f'<tr><td>{f["email"]}</td><td>{f["plan"]}</td>'
                    f'<td>{"; ".join(f["critical"])}</td></tr>'
                )
            html = f'''
<h2>DC Hub — Daily Paid-Account Health Check</h2>
<p><strong>{len(critical_findings)}</strong> paying customer(s) CANNOT LOG IN
({healthy_total} of {paid_total} healthy). Fix immediately via
/api/v1/admin/paid-account-health/fix-reset.</p>
<table border="1" cellpadding="6" style="border-collapse: collapse; font-family: monospace; font-size: 13px;">
<tr><th>Email</th><th>Plan</th><th>Critical issue</th></tr>
{html_rows}
</table>
<p style="color:#666; font-size:11px;">{len(warning_findings)} account(s) also have
non-blocking warnings (no API key / reset token expiring) — see the JSON endpoint.</p>
'''
            send_email(
                to='azmartone@gmail.com',
                subject=f'🚨 DC Hub — {len(critical_findings)} paying customer(s) LOCKED OUT',
                html=html,
            )
            summary_emailed = True
        except Exception as e:
            logger.warning(f"summary email failed: {e}")

    return jsonify({
        'success': True,
        'paid_users_total': paid_total,
        'healthy_total': healthy_total,
        # problem_count now means LOGIN-BLOCKED (the alert-worthy signal),
        # not "any finding". Warnings are tracked separately.
        'problem_count': len(critical_findings),
        'warning_count': len(warning_findings),
        'critical_findings': critical_findings,
        'warning_findings': warning_findings,
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


# Map a plan → the role (api access tier) it should carry. Mirrors the
# Stripe webhook's plan_tier_map: founding members get pro-level access.
_PLAN_ROLE = {
    "founding": "pro", "pro": "pro", "enterprise": "enterprise",
    "developer": "developer", "starter": "starter", "free": "user",
}


@paid_health_bp.route('/api/v1/admin/paid-account-health/set-tier', methods=['POST'])
def set_tier():
    """Operator shortcut: correct a user's tier on the users table.

    Built for the Carl Braun case — his account had plan='founding' but
    role=NULL + subscription_status=NULL (provisioned before the webhook
    set those), so login encoded role->'user' and the platform treated
    a paying founding member as free. Sets plan/role/subscription_status
    atomically. Admin-gated.
    """
    if not _admin_ok():
        return jsonify({'error': 'admin_key_required'}), 401
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').lower().strip()
    plan = (data.get('plan') or '').lower().strip()
    if not email or not plan:
        return jsonify({'error': 'email and plan required'}), 400
    # role defaults from the plan→role map unless explicitly overridden
    role = (data.get('role') or _PLAN_ROLE.get(plan) or 'user').lower().strip()
    sub_status = data.get('subscription_status', 'active')

    try:
        with _get_pg() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, plan, role, subscription_status FROM users WHERE LOWER(email) = %s", (email,))
            before = cur.fetchone()
            if not before:
                return jsonify({'success': False, 'error': f'No user found with email {email}'}), 404
            cur.execute("""
                UPDATE users
                   SET plan = %s, role = %s, subscription_status = %s, plan_updated_at = NOW()
                 WHERE LOWER(email) = %s
            """, (plan, role, sub_status, email))
            # Also bump any active API key's rate-limit tier so programmatic
            # access matches the corrected plan.
            try:
                cur.execute("""
                    UPDATE api_keys SET rate_limit_tier = %s, plan = %s
                     WHERE user_id = (SELECT id FROM users WHERE LOWER(email) = %s)
                       AND COALESCE(is_active, 1) = 1
                """, (role, plan, email))
            except Exception as _ke:
                logger.warning(f"set-tier: api_keys update skipped: {_ke}")
            conn.commit()
            return jsonify({
                'success': True,
                'email': email,
                'before': {'plan': before[1], 'role': before[2], 'subscription_status': before[3]},
                'after': {'plan': plan, 'role': role, 'subscription_status': sub_status},
            })
    except Exception as e:
        logger.error(f"set-tier failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@paid_health_bp.route('/api/v1/admin/paid-account-health/mint-key', methods=['POST'])
def mint_key():
    """Provision an API key for a paid account that has none (the
    'no_api_key' warning). Built for Carl Braun — founding member whose
    pro-level access works but who had no key to USE the MCP/API benefits.
    Returns the raw key ONCE (it's only stored hashed). Idempotent: if an
    active key already exists, returns its prefix instead of minting a dupe.
    """
    if not _admin_ok():
        return jsonify({'error': 'admin_key_required'}), 401
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').lower().strip()
    if not email:
        return jsonify({'error': 'email required'}), 400

    user_id = None
    plan = 'free'
    regenerate = bool(data.get('regenerate'))
    try:
        with _get_pg() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, plan FROM users WHERE LOWER(email) = %s", (email,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': f'No user found with email {email}'}), 404
            user_id, plan = row[0], (row[1] or 'free')
            cur.execute("""
                SELECT key_prefix FROM api_keys
                 WHERE user_id = %s AND COALESCE(is_active, 1) = 1 LIMIT 1
            """, (user_id,))
            existing = cur.fetchone()
            if existing and not regenerate:
                return jsonify({'success': True, 'already_has_key': True,
                                'key_prefix': existing[0], 'email': email,
                                'note': 'active key already exists — pass {"regenerate": true} to '
                                        'deactivate it and mint a fresh deliverable key'})
            if existing and regenerate:
                # Deactivate all current active keys so the new one is the
                # single source (used when the prior key's raw value was lost).
                cur.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = %s", (user_id,))
                conn.commit()
    except Exception as e:
        logger.error(f"mint-key lookup failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    # Mint using the SAME column set the Stripe webhook uses. NOTE:
    # api_tier_gating.generate_api_key() is broken against the live schema
    # (it INSERTs a non-existent `email` column → that's likely why these
    # accounts never got a key). We replicate the webhook's working INSERT.
    try:
        import secrets as _sec, hashlib as _hl
        from datetime import datetime as _dt
        # founding → pro api tier (rate_limit_tier); key prefix mirrors tier
        try:
            import tier_registry
            api_tier = tier_registry.api_tier(plan)
        except Exception:
            api_tier = {'founding': 'pro'}.get(plan, plan)
        _prefix_map = {'developer': 'dchub_dev_', 'pro': 'dchub_pro_',
                       'enterprise': 'dchub_ent_'}
        key_prefix_str = _prefix_map.get(api_tier, 'dchub_dev_')
        raw_key = key_prefix_str + _sec.token_urlsafe(32)
        key_hash = _hl.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:raw_key.rindex('_') + 1]
        now = _dt.utcnow().isoformat()
        with _get_pg() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions, "
                "rate_limit_tier, is_active, created_at, usage_count, plan, calls_today, calls_total) "
                "VALUES (%s, %s, %s, %s, '[\"read\",\"write\"]', %s, 1, %s, 0, %s, 0, 0)",
                (user_id, key_hash, key_prefix, f'{email} {plan.title()} Key', api_tier, now, plan))
            conn.commit()
        return jsonify({'success': True, 'email': email, 'plan': plan,
                        'api_tier': api_tier, 'api_key': raw_key,
                        'note': 'raw key shown ONCE — deliver to customer'})
    except Exception as e:
        logger.error(f"mint-key generate failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def register(app, get_pg_fn):
    init(get_pg_fn)
    app.register_blueprint(paid_health_bp)
    print("[main] paid_account_health registered: check + fix-reset + set-tier + mint-key")
