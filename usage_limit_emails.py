"""
DC Hub Usage Limit Emails
=========================
Sends upgrade nudge emails when free/developer users approach or hit
their daily API call limits. Designed as a standalone module called
by the scheduler or triggered inline from rate limit checks.

Integration:
  In main.py:
    from usage_limit_emails import trigger_usage_email, setup_usage_email_routes
    setup_usage_email_routes(app)

  In dchub-scheduler.py (optional daily sweep):
    from usage_limit_emails import sweep_heavy_users

Tables used:
  - email_drip_log (existing) — prevents duplicate sends
  - users (existing) — email + plan lookup
  - api_keys (existing) — calls_today tracking

Environment:
  - SENDGRID_API_KEY (required)
  - SENDGRID_FROM_EMAIL (default: info@dchub.cloud)
  - NEON_DATABASE_URL or DATABASE_URL
"""

import os
import logging
import threading
from datetime import datetime, date, timezone

logger = logging.getLogger('usage_emails')

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

THRESHOLDS = {
    'free': {
        'daily_limit': 10,
        'nudge_at': 8,           # 80% — "you're almost there"
        'hit_limit': 10,         # 100% — "you've hit the wall"
        'upgrade_plan': 'Developer',
        'upgrade_price': '$49/mo',
        'upgrade_url': 'https://dchub.cloud/pricing#developer',
        'checkout_url': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
        'upgrade_calls': '1,000',
    },
    'developer': {
        'daily_limit': 1000,
        'nudge_at': 800,
        'hit_limit': 1000,
        'upgrade_plan': 'Pro',
        'upgrade_price': '$199/mo',
        'upgrade_url': 'https://dchub.cloud/pricing#pro',
        'checkout_url': 'https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02',
        'upgrade_calls': '5,000',
    },
    'founding': {
        'daily_limit': 1000,
        'nudge_at': 800,
        'hit_limit': 1000,
        'upgrade_plan': 'Pro',
        'upgrade_price': '$199/mo',
        'upgrade_url': 'https://dchub.cloud/pricing#pro',
        'checkout_url': 'https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02',
        'upgrade_calls': '5,000',
    },
}

# Email types (for dedup in email_drip_log)
EMAIL_TYPE_NUDGE = 'usage_nudge'       # 80% — approaching limit
EMAIL_TYPE_HIT = 'usage_limit_hit'     # 100% — hit the wall


# ═══════════════════════════════════════════════════════════════
#  DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_conn():
    """Direct psycopg2 connection to Neon."""
    import psycopg2
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not url:
        raise Exception("No NEON_DATABASE_URL or DATABASE_URL set")
    conn = psycopg2.connect(url, connect_timeout=5)
    conn.autocommit = True
    return conn


def _already_sent_today(email, email_type):
    """Check if we already sent this email type to this user today."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM email_drip_log
            WHERE email = %s AND email_type = %s
            AND sent_at::date = CURRENT_DATE
        """, (email, email_type))
        count = cur.fetchone()[0]
        cur.close()
        return count > 0
    except Exception as e:
        logger.warning(f"Dedup check failed for {email}: {e}")
        return True  # Fail safe: don't send if we can't check
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _log_email_sent(email, email_type, plan, calls_used):
    """Record that we sent an email (for dedup + analytics)."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_drip_log (email, email_type, context, sent_at)
            VALUES (%s, %s, %s, NOW())
        """, (email, email_type, f"plan={plan},calls={calls_used}"))
        cur.close()
    except Exception as e:
        logger.warning(f"Failed to log email send for {email}: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
#  EMAIL TEMPLATES
# ═══════════════════════════════════════════════════════════════

def _build_nudge_email(name, plan, calls_used, config):
    """80% usage — friendly nudge."""
    remaining = config['daily_limit'] - calls_used
    subject = f"You're almost at your DC Hub daily limit ({calls_used}/{config['daily_limit']} calls)"

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; background: #f5f5f7; color: #1a1a2e; }}
.wrapper {{ max-width: 600px; margin: 0 auto; background: #fff; }}
.header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 28px 40px; text-align: center; }}
.logo {{ font-size: 28px; font-weight: 700; color: #fff; }}
.logo span {{ color: #00d4ff; }}
.body {{ padding: 36px 40px; }}
h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 12px; }}
p {{ font-size: 15px; color: #4a4a5a; margin-bottom: 14px; line-height: 1.6; }}
.usage-bar-bg {{ background: #e8e8ee; border-radius: 8px; height: 24px; margin: 20px 0; overflow: hidden; }}
.usage-bar {{ background: linear-gradient(90deg, #00d4ff, #ff6b35); height: 100%; border-radius: 8px; transition: width 0.5s; }}
.stat {{ font-size: 14px; color: #6a6a7a; text-align: center; margin-bottom: 20px; }}
.cta {{ display: inline-block; background: linear-gradient(135deg, #00d4ff, #0099cc); color: #fff !important; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 16px; margin: 16px 0; }}
.plan-box {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); border-radius: 10px; padding: 24px; margin: 24px 0; text-align: center; }}
.plan-box h2 {{ color: #00d4ff; margin: 0 0 6px 0; font-size: 20px; }}
.plan-box .price {{ color: #ff6b35; font-size: 28px; font-weight: 700; margin: 8px 0; }}
.plan-box p {{ color: #ccc; margin: 4px 0; font-size: 14px; }}
.plan-cta {{ display: inline-block; background: linear-gradient(135deg, #ff6b35, #ff4500); color: #fff !important; padding: 12px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; margin-top: 16px; }}
.footer {{ background: #f8f9fa; padding: 20px 40px; text-align: center; font-size: 12px; color: #9a9aaa; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="logo">DC<span>Hub</span></div>
  </div>
  <div class="body">
    <h1>Heads up, {name} — you're close to today's limit</h1>
    <p>You've used <strong>{calls_used} of {config['daily_limit']}</strong> API calls today. Only <strong>{remaining} calls</strong> remaining before your limit resets at midnight UTC.</p>

    <div class="usage-bar-bg">
      <div class="usage-bar" style="width: {min(100, int(calls_used / config['daily_limit'] * 100))}%"></div>
    </div>
    <div class="stat">{calls_used} / {config['daily_limit']} calls used today</div>

    <p>If you're building with DC Hub data or running automated queries, the {config['upgrade_plan']} plan gives you room to grow:</p>

    <div class="plan-box">
      <h2>{config['upgrade_plan']} Plan</h2>
      <div class="price">{config['upgrade_price']}</div>
      <p>{config['upgrade_calls']} API calls per day</p>
      <p>Full facility data with coordinates &amp; power specs</p>
      <p>Complete M&amp;A deal values &amp; pipeline tracking</p>
      <p>Site scoring, grid data, fiber intelligence</p>
      <a href="{config['checkout_url']}" class="plan-cta">Upgrade Now →</a>
    </div>

    <p style="font-size: 13px; color: #8a8a9a; text-align: center;">Your limit resets at midnight UTC. No action needed if your current plan works for you.</p>
  </div>
  <div class="footer">
    DC Hub &middot; Data Center Intelligence Platform<br>
    <a href="https://dchub.cloud" style="color: #00d4ff; text-decoration: none;">dchub.cloud</a>
  </div>
</div>
</body>
</html>"""

    return subject, html


def _build_limit_hit_email(name, plan, calls_used, config):
    """100% usage — you've hit the wall."""
    subject = f"DC Hub daily limit reached — {calls_used}/{config['daily_limit']} calls used"

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; background: #f5f5f7; color: #1a1a2e; }}
.wrapper {{ max-width: 600px; margin: 0 auto; background: #fff; }}
.header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 28px 40px; text-align: center; }}
.logo {{ font-size: 28px; font-weight: 700; color: #fff; }}
.logo span {{ color: #00d4ff; }}
.body {{ padding: 36px 40px; }}
h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 12px; }}
p {{ font-size: 15px; color: #4a4a5a; margin-bottom: 14px; line-height: 1.6; }}
.alert-box {{ background: #fff3f0; border: 1px solid #ff6b35; border-radius: 8px; padding: 16px 20px; margin: 20px 0; text-align: center; }}
.alert-box h3 {{ color: #ff4500; margin: 0 0 6px 0; }}
.alert-box p {{ margin: 0; color: #666; font-size: 14px; }}
.plan-box {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); border-radius: 10px; padding: 24px; margin: 24px 0; text-align: center; }}
.plan-box h2 {{ color: #00d4ff; margin: 0 0 6px 0; font-size: 20px; }}
.plan-box .price {{ color: #ff6b35; font-size: 28px; font-weight: 700; margin: 8px 0; }}
.plan-box p {{ color: #ccc; margin: 4px 0; font-size: 14px; }}
.plan-cta {{ display: inline-block; background: linear-gradient(135deg, #ff6b35, #ff4500); color: #fff !important; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 700; font-size: 16px; margin-top: 16px; }}
.footer {{ background: #f8f9fa; padding: 20px 40px; text-align: center; font-size: 12px; color: #9a9aaa; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="logo">DC<span>Hub</span></div>
  </div>
  <div class="body">
    <h1>You've hit today's limit, {name}</h1>

    <div class="alert-box">
      <h3>{calls_used} / {config['daily_limit']} calls used</h3>
      <p>Your daily API limit has been reached. It resets at midnight UTC.</p>
    </div>

    <p>Looks like you're actively using DC Hub data — that's great. If you need more capacity, the {config['upgrade_plan']} plan is built for exactly this:</p>

    <div class="plan-box">
      <h2>{config['upgrade_plan']} Plan</h2>
      <div class="price">{config['upgrade_price']}</div>
      <p><strong>{config['upgrade_calls']}</strong> API calls per day</p>
      <p>Full facility data — coordinates, power, connectivity</p>
      <p>Complete deal values, pipeline MW, site scoring</p>
      <p>MCP access for Claude, ChatGPT, Cursor, Windsurf</p>
      <a href="{config['checkout_url']}" class="plan-cta">Upgrade to {config['upgrade_plan']} →</a>
    </div>

    <p style="font-size: 13px; color: #8a8a9a; text-align: center;">
      Questions%s Reply to this email or reach us at <a href="mailto:support@dchub.cloud" style="color: #00d4ff;">support@dchub.cloud</a>
    </p>
  </div>
  <div class="footer">
    DC Hub &middot; Data Center Intelligence Platform<br>
    <a href="https://dchub.cloud" style="color: #00d4ff; text-decoration: none;">dchub.cloud</a>
  </div>
</div>
</body>
</html>"""

    return subject, html


# ═══════════════════════════════════════════════════════════════
#  SEND EMAIL (non-blocking via thread)
# ═══════════════════════════════════════════════════════════════

def _send_email_async(to_email, subject, html_body):
    """Fire-and-forget SendGrid email in background thread."""
    def _send():
        try:
            sg_key = os.environ.get('SENDGRID_API_KEY', '')
            if not sg_key:
                logger.warning(f"SENDGRID_API_KEY not set — skipping email to {to_email}")
                return

            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail, Email, To, HtmlContent

            from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'info@dchub.cloud')
            message = Mail(
                from_email=Email(from_email, 'DC Hub'),
                to_emails=To(to_email),
                subject=subject,
                html_content=HtmlContent(html_body)
            )

            # BCC admin for monitoring initial rollout
            try:
                from sendgrid.helpers.mail import Bcc
                admin_email = os.environ.get('ADMIN_ALERT_EMAIL', 'jonathan@dchub.cloud')
                if admin_email and admin_email != to_email:
                    message.add_bcc(Bcc(admin_email))
            except Exception:
                pass

            sg = SendGridAPIClient(sg_key)
            response = sg.send(message)
            logger.info(f"📧 Usage email sent to {to_email}: {subject} (status={response.status_code})")

        except Exception as e:
            logger.error(f"📧 Usage email FAILED for {to_email}: {e}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API — Call from main.py rate limit checks
# ═══════════════════════════════════════════════════════════════

def trigger_usage_email(email, plan, calls_used):
    """
    Check if a usage email should be sent, and send it if so.
    Call this from rate limit enforcement when a registered user
    approaches or hits their daily limit.

    Args:
        email: User's email address
        plan: Current plan (free, developer, founding)
        calls_used: Number of API calls used today
    """
    if not email or '@' not in email:
        return

    plan = (plan or 'free').lower()
    config = THRESHOLDS.get(plan)
    if not config:
        return  # pro/enterprise/admin don't get nudge emails

    name = email.split('@')[0]

    # Determine which email to send (if any)
    email_type = None
    build_fn = None

    if calls_used >= config['hit_limit']:
        email_type = EMAIL_TYPE_HIT
        build_fn = _build_limit_hit_email
    elif calls_used >= config['nudge_at']:
        email_type = EMAIL_TYPE_NUDGE
        build_fn = _build_nudge_email
    else:
        return  # Not at threshold yet

    # Dedup: don't send same email type twice in one day
    if _already_sent_today(email, email_type):
        logger.debug(f"Skipping {email_type} for {email} — already sent today")
        return

    # Build and send
    subject, html = build_fn(name, plan, calls_used, config)
    _send_email_async(email, subject, html)
    _log_email_sent(email, email_type, plan, calls_used)
    logger.info(f"📧 Triggered {email_type} for {email} (plan={plan}, calls={calls_used})")


def sweep_heavy_users():
    """
    Scheduler job: find users who hit >80% of their daily limit
    and haven't been emailed yet today. Send nudge emails.

    Call from dchub-scheduler.py as a daily job (e.g. 6pm UTC).
    """
    conn = None
    sent_count = 0
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # Find free users with high API usage today
        cur.execute("""
            SELECT u.email, u.plan, COALESCE(SUM(ak.calls_today), 0) as calls_today
            FROM users u
            LEFT JOIN api_keys ak ON ak.user_id = u.id AND ak.is_active = true
            WHERE u.plan IN ('free', 'developer', 'founding')
            AND u.email IS NOT NULL
            AND u.email != ''
            GROUP BY u.id, u.email, u.plan
            HAVING COALESCE(SUM(ak.calls_today), 0) > 0
            ORDER BY calls_today DESC
            LIMIT 100
        """)

        for row in cur.fetchall():
            email, plan, calls_today = row[0], row[1], row[2]
            config = THRESHOLDS.get(plan)
            if not config:
                continue
            if calls_today >= config['nudge_at']:
                trigger_usage_email(email, plan, calls_today)
                sent_count += 1

        cur.close()
        logger.info(f"📧 Usage sweep complete: {sent_count} emails triggered")

    except Exception as e:
        logger.error(f"📧 Usage sweep failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return sent_count


# ═══════════════════════════════════════════════════════════════
#  FLASK ROUTES (admin monitoring)
# ═══════════════════════════════════════════════════════════════

def setup_usage_email_routes(app):
    """Register admin routes for monitoring usage emails."""

    @app.route('/api/admin/usage-emails/sweep', methods=['POST'])
    def admin_sweep_usage_emails():
        """Manually trigger a sweep of heavy users. Admin only."""
        from flask import request, jsonify
        admin_key = request.headers.get('X-Admin-Key', '')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY', ''):
            return jsonify({'error': 'Unauthorized'}), 401

        count = sweep_heavy_users()
        return jsonify({'success': True, 'emails_triggered': count})

    @app.route('/api/admin/usage-emails/stats', methods=['GET'])
    def admin_usage_email_stats():
        """Get usage email stats. Admin only."""
        from flask import request, jsonify
        admin_key = request.headers.get('X-Admin-Key', '')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY', ''):
            return jsonify({'error': 'Unauthorized'}), 401

        conn = None
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT email_type, COUNT(*), COUNT(DISTINCT email)
                FROM email_drip_log
                WHERE email_type IN ('usage_nudge', 'usage_limit_hit')
                AND sent_at > NOW() - INTERVAL '30 days'
                GROUP BY email_type
            """)
            stats = {row[0]: {'total_sent': row[1], 'unique_users': row[2]}
                     for row in cur.fetchall()}

            cur.execute("""
                SELECT COUNT(DISTINCT email)
                FROM email_drip_log
                WHERE email_type IN ('usage_nudge', 'usage_limit_hit')
                AND sent_at::date = CURRENT_DATE
            """)
            today_count = cur.fetchone()[0]

            cur.close()
            return jsonify({
                'success': True,
                'last_30_days': stats,
                'sent_today': today_count,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info("  ✅ Usage limit email routes registered")
