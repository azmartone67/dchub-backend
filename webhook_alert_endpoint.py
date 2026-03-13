"""
DC Hub - Stripe Webhook Alert Endpoint
=======================================
POST /api/stripe/webhook-alert

Called by dashboard.html when post-checkout polling detects that a user's
plan hasn't activated within 30 seconds of returning from Stripe checkout.
This means the Stripe webhook likely failed to fire or process.

Payload from dashboard.html:
    {
        "email": "user@example.com",
        "issue": "plan_not_activated_after_checkout",
        "attempts": 10
    }

Actions:
    1. Logs the alert to the webhook_alerts table
    2. Sends an email notification to admin (jonathan@dchub.cloud)
    3. Optionally attempts to auto-activate the plan via Stripe API lookup

Integration:
    Add to main.py near your other Stripe routes:

        from webhook_alert_endpoint import webhook_alert_bp
        app.register_blueprint(webhook_alert_bp)

    OR paste the route directly into main.py (standalone version below).
"""

# =============================================================================
# OPTION A: Blueprint version (separate file)
# =============================================================================

from flask import Blueprint, request, jsonify
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

webhook_alert_bp = Blueprint('webhook_alert', __name__)


@webhook_alert_bp.route('/api/stripe/webhook-alert', methods=['POST'])
def stripe_webhook_alert():
    """
    Handle post-checkout plan activation failure alerts from dashboard.html.
    Called when polling /auth/me doesn't show plan activation within 30s.
    """
    try:
        data = request.get_json(silent=True) or {}
        email = data.get('email', 'unknown')
        issue = data.get('issue', 'unknown')
        attempts = data.get('attempts', 0)
        timestamp = datetime.utcnow().isoformat()

        logger.warning(
            f"🚨 WEBHOOK ALERT: {issue} | email={email} | "
            f"attempts={attempts} | time={timestamp}"
        )

        # --- 1. Log to database ---
        try:
            from db_utils import get_db
            db = get_db()
            db.execute('''
                CREATE TABLE IF NOT EXISTS webhook_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    issue TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    resolved INTEGER DEFAULT 0,
                    resolution TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    resolved_at TEXT
                )
            ''')
            db.execute(
                'INSERT INTO webhook_alerts (email, issue, attempts) VALUES (?, ?, ?)',
                (email, issue, attempts)
            )
            db.commit()
            logger.info(f"📝 Webhook alert logged to DB for {email}")
        except Exception as db_err:
            logger.error(f"❌ Failed to log webhook alert to DB: {db_err}")

        # --- 2. Try auto-activation via Stripe lookup ---
        auto_activated = False
        try:
            import stripe
            stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
            if stripe.api_key and email != 'unknown':
                # Look up customer by email in Stripe
                customers = stripe.Customer.list(email=email, limit=1)
                if customers.data:
                    customer = customers.data[0]
                    # Check for active subscriptions
                    subs = stripe.Subscription.list(
                        customer=customer.id, status='active', limit=1
                    )
                    if subs.data:
                        sub = subs.data[0]
                        plan_name = _determine_plan_from_subscription(sub)
                        if plan_name and plan_name != 'free':
                            # Activate the plan in the database
                            _activate_user_plan(email, plan_name, customer.id)
                            auto_activated = True
                            logger.info(
                                f"✅ AUTO-ACTIVATED {email} to {plan_name} "
                                f"(Stripe customer {customer.id})"
                            )
                    else:
                        # Check for completed checkout sessions (Payment Links)
                        sessions = stripe.checkout.Session.list(
                            customer_details={'email': email},
                            limit=5
                        )
                        for session in sessions.auto_paging_iter():
                            if session.payment_status == 'paid':
                                plan_name = _determine_plan_from_session(session)
                                if plan_name and plan_name != 'free':
                                    cust_id = session.customer or ''
                                    _activate_user_plan(email, plan_name, cust_id)
                                    auto_activated = True
                                    logger.info(
                                        f"✅ AUTO-ACTIVATED {email} to {plan_name} "
                                        f"(from checkout session {session.id})"
                                    )
                                    break
        except Exception as stripe_err:
            logger.error(f"⚠️ Stripe auto-activation failed for {email}: {stripe_err}")

        # --- 3. Send admin notification email ---
        try:
            _send_admin_alert_email(email, issue, attempts, auto_activated, timestamp)
        except Exception as email_err:
            logger.error(f"⚠️ Failed to send admin alert email: {email_err}")

        return jsonify({
            'success': True,
            'message': 'Alert received',
            'auto_activated': auto_activated,
            'email': email
        }), 200

    except Exception as e:
        logger.error(f"❌ Webhook alert handler error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# Helper Functions
# =============================================================================

def _determine_plan_from_subscription(subscription):
    """Determine plan name from a Stripe subscription object."""
    price_pro = os.environ.get('STRIPE_PRICE_PRO_ANNUAL', '')
    price_ent = os.environ.get('STRIPE_PRICE_ENTERPRISE_MONTHLY', '')
    price_ent2 = os.environ.get('STRIPE_PRICE_ENTERPRISE', '')

    for item in subscription.get('items', {}).get('data', []):
        price_id = item.get('price', {}).get('id', '')
        if price_id == price_pro:
            return 'pro'
        if price_id in (price_ent, price_ent2):
            return 'enterprise'

    # Fallback: check amount
    amount = subscription.get('items', {}).get('data', [{}])[0].get('price', {}).get('unit_amount', 0)
    if amount:
        if amount <= 15000:   # $150 or less = pro
            return 'pro'
        else:
            return 'enterprise'

    return 'pro'  # Safe default for paid customers


def _determine_plan_from_session(session):
    """Determine plan from a Stripe checkout session."""
    amount = session.get('amount_total', 0)
    if amount and amount > 0:
        if amount <= 15000:
            return 'pro'
        else:
            return 'enterprise'
    return 'pro'


def _activate_user_plan(email, plan, stripe_customer_id=''):
    """Activate a user's plan in the database."""
    try:
        from db_utils import get_db
        db = get_db()

        # Update user plan — uses column "plan" (not "tier")
        # Note: users table has NO updated_at or api_key columns
        db.execute('''
            UPDATE users SET
                plan = ?,
                subscription_status = 'active',
                stripe_customer_id = COALESCE(NULLIF(?, ''), stripe_customer_id)
            WHERE LOWER(email) = LOWER(?)
        ''', (plan, stripe_customer_id, email))
        db.commit()

        # Also update webhook_alerts to mark as resolved
        db.execute('''
            UPDATE webhook_alerts SET
                resolved = 1,
                resolution = ?,
                resolved_at = datetime('now')
            WHERE email = ? AND resolved = 0
        ''', (f'auto_activated_to_{plan}', email))
        db.commit()

        logger.info(f"✅ Database updated: {email} → plan={plan}")
    except Exception as e:
        logger.error(f"❌ Failed to activate plan for {email}: {e}")


def _send_admin_alert_email(email, issue, attempts, auto_activated, timestamp):
    """Send notification email to admin about the webhook failure."""
    admin_email = os.environ.get('ADMIN_EMAIL', 'jonathan@dchub.cloud')

    status_emoji = '✅' if auto_activated else '🚨'
    status_text = 'AUTO-RESOLVED' if auto_activated else 'NEEDS ATTENTION'

    subject = f"{status_emoji} DC Hub Webhook Alert: {email} — {status_text}"

    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background: {'#0a2e1a' if auto_activated else '#2e0a0a'}; border: 1px solid {'#00ff88' if auto_activated else '#ff4444'}; border-radius: 8px; padding: 20px; color: white;">
            <h2 style="margin-top: 0; color: {'#00ff88' if auto_activated else '#ff4444'};">{status_emoji} Webhook Alert — {status_text}</h2>
            <table style="width: 100%; color: #ccc; font-size: 14px;">
                <tr><td style="padding: 4px 8px; color: #888;">Customer Email</td><td style="padding: 4px 8px;"><strong>{email}</strong></td></tr>
                <tr><td style="padding: 4px 8px; color: #888;">Issue</td><td style="padding: 4px 8px;">{issue}</td></tr>
                <tr><td style="padding: 4px 8px; color: #888;">Poll Attempts</td><td style="padding: 4px 8px;">{attempts}</td></tr>
                <tr><td style="padding: 4px 8px; color: #888;">Timestamp</td><td style="padding: 4px 8px;">{timestamp}</td></tr>
                <tr><td style="padding: 4px 8px; color: #888;">Auto-Activated</td><td style="padding: 4px 8px;">{'Yes ✅' if auto_activated else 'No ❌ — manual activation needed'}</td></tr>
            </table>
        </div>
        {'<p style="color: #888; font-size: 13px; margin-top: 16px;">✅ The system auto-activated this customer via Stripe API lookup. No action needed.</p>' if auto_activated else '<div style="background: #1a1a2e; border: 1px solid #444; border-radius: 8px; padding: 16px; margin-top: 16px;"><p style="color: #ff8888; margin-top: 0;"><strong>Action Required:</strong></p><ol style="color: #ccc; font-size: 14px;"><li>Check <a href="https://dashboard.stripe.com/search?query=' + email + '" style="color: #00d4ff;">Stripe Dashboard</a> for this customer</li><li>Verify payment was received</li><li>Manually activate: <code>UPDATE users SET plan = \'pro\' WHERE email = \'' + email + '\';</code></li><li>Email the customer to confirm activation</li></ol></div>'}
    </div>
    """

    text_content = (
        f"Webhook Alert — {status_text}\n\n"
        f"Customer: {email}\n"
        f"Issue: {issue}\n"
        f"Attempts: {attempts}\n"
        f"Time: {timestamp}\n"
        f"Auto-Activated: {'Yes' if auto_activated else 'No — manual activation needed'}\n"
    )

    # Try to use the existing send_email function from main.py / email_service.py
    try:
        from email_service import send_email as send_email_svc
        success, msg = send_email_svc(admin_email, subject, html_content, text_content)
        if success:
            logger.info(f"📧 Admin alert email sent via email_service: {msg}")
            return
    except ImportError:
        pass

    # Fallback: try the main.py send_email
    try:
        # This import works if the function is defined in main.py's global scope
        import sys
        main_mod = sys.modules.get('__main__') or sys.modules.get('main')
        if main_mod and hasattr(main_mod, 'send_email'):
            success, msg = main_mod.send_email(admin_email, subject, html_content, text_content)
            if success:
                logger.info(f"📧 Admin alert email sent via main.send_email: {msg}")
                return
    except Exception:
        pass

    # Last resort: direct SMTP
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_host = os.environ.get('SMTP_HOST', 'smtp.office365.com')
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
        smtp_user = os.environ.get('SMTP_USER', '')
        smtp_pass = os.environ.get('SMTP_PASSWORD', '')
        from_email = os.environ.get('SMTP_FROM_EMAIL', smtp_user)

        if smtp_user and smtp_pass:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"DC Hub <{from_email}>"
            msg['To'] = admin_email
            msg.attach(MIMEText(text_content, 'plain'))
            msg.attach(MIMEText(html_content, 'html'))

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(from_email, admin_email, msg.as_string())

            logger.info(f"📧 Admin alert email sent via direct SMTP")
            return
    except Exception as smtp_err:
        logger.error(f"⚠️ Direct SMTP failed: {smtp_err}")

    # If all email methods fail, at least it's logged to DB and console
    logger.warning(f"⚠️ Could not send admin alert email for {email} — logged to DB only")


# =============================================================================
# OPTION B: Standalone route (paste directly into main.py)
# =============================================================================
# If you prefer not to use a separate file, copy everything above into main.py
# and replace the blueprint route decorator with:
#
#     @app.route('/api/stripe/webhook-alert', methods=['POST'])
#     def stripe_webhook_alert():
#         ...
#
# And remove the Blueprint import/creation lines.
# =============================================================================
