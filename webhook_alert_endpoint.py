"""
DC Hub — Stripe Webhook Alert Endpoint
=======================================
Called by dashboard.html when polling detects a user's plan didn't activate
after successful Stripe payment. Logs the alert, attempts auto-activation
via Stripe API, and emails the admin.

Blueprint: webhook_alert_bp
Route:     POST /api/stripe/webhook-alert
"""

import os
import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

webhook_alert_bp = Blueprint('webhook_alert', __name__)


def _get_stripe():
    """Lazy import stripe to avoid ImportError if not installed."""
    try:
        import stripe
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
        if not stripe.api_key:
            return None
        return stripe
    except ImportError:
        return None


def _get_db():
    """Get database connection via db_utils."""
    try:
        from db_utils import get_db
        return get_db()
    except Exception as e:
        logger.error("webhook_alert: DB connection failed: %s" % e)
        return None


def _send_admin_email(subject, body):
    """Send alert email to admin. Uses email_service if available."""
    try:
        from email_service import send_email
        admin_email = os.environ.get('ADMIN_EMAIL', 'api@dchub.cloud')
        send_email(admin_email, subject, body)
        logger.info("webhook_alert: Admin email sent to %s" % admin_email)
        return True
    except Exception as e:
        logger.warning("webhook_alert: Email send failed: %s" % e)
        return False


@webhook_alert_bp.route('/api/stripe/webhook-alert', methods=['POST'])
def stripe_webhook_alert():
    """
    POST /api/stripe/webhook-alert

    Called by dashboard.html when a user completes Stripe checkout but their
    plan doesn't activate within the polling window (typically 30-60 seconds).

    Body JSON:
        email (str): User's email
        plan (str): Expected plan (pro_monthly, pro_annual, enterprise_monthly, etc.)
        session_id (str, optional): Stripe checkout session ID
        customer_id (str, optional): Stripe customer ID
        error (str, optional): Client-side error description

    Response:
        200: Alert logged, auto-activation attempted
        500: Server error
    """
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    expected_plan = data.get('plan', 'pro')
    session_id = data.get('session_id', '')
    customer_id = data.get('customer_id', '')
    client_error = data.get('error', 'Plan not activated after payment')
    now = datetime.now(timezone.utc).isoformat()

    # Phase FF+7-meta (2026-05-19): silently drop test-email noise so the
    # admin email + log spam stops. Real customers don't match these.
    _SUPPRESSED_EMAILS = {"qa-test@dchub.cloud", "test@dchub.cloud",
                           "qa@dchub.cloud", "stripe-test@dchub.cloud"}
    if email in _SUPPRESSED_EMAILS or email.endswith("@example.com"):
        logger.info(
            "STRIPE ALERT suppressed (test email): email=%s plan=%s",
            email, expected_plan)
        return jsonify({"ok": True, "suppressed": True,
                         "reason": "test_email"}), 200

    logger.warning(
        "STRIPE ALERT: Plan not activated — email=%s plan=%s session=%s error=%s"
        % (email, expected_plan, session_id, client_error)
    )

    # --- Step 1: Log the alert to DB ---
    conn = _get_db()
    alert_logged = False
    if conn:
        try:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS webhook_alerts (
                    id SERIAL PRIMARY KEY,
                    email TEXT,
                    expected_plan TEXT,
                    session_id TEXT,
                    customer_id TEXT,
                    error TEXT,
                    auto_fix_attempted BOOLEAN DEFAULT FALSE,
                    auto_fix_result TEXT,
                    created_at TEXT
                )
            """)
            c.execute("""
                INSERT INTO webhook_alerts (email, expected_plan, session_id, customer_id, error, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (email, expected_plan, session_id, customer_id, client_error, now))
            conn.commit()
            alert_logged = True
        except Exception as e:
            logger.error("webhook_alert: DB log failed: %s" % e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # --- Step 2: Attempt auto-activation via Stripe API ---
    auto_fix_result = None
    stripe = _get_stripe()

    if stripe and (session_id or customer_id or email):
        try:
            # Try to find the subscription via session ID first
            resolved_customer_id = customer_id
            resolved_plan = None

            if session_id:
                try:
                    session = stripe.checkout.Session.retrieve(session_id)
                    resolved_customer_id = session.get('customer', customer_id)
                    metadata = session.get('metadata', {})
                    resolved_plan = metadata.get('plan', expected_plan)
                    logger.info("webhook_alert: Session %s -> customer %s, plan %s"
                                % (session_id, resolved_customer_id, resolved_plan))
                except Exception as e:
                    logger.warning("webhook_alert: Session lookup failed: %s" % e)

            # Look up customer by email if we still don't have a customer ID
            if not resolved_customer_id and email:
                try:
                    customers = stripe.Customer.list(email=email, limit=1)
                    if customers.data:
                        resolved_customer_id = customers.data[0].id
                        logger.info("webhook_alert: Found customer %s by email %s"
                                    % (resolved_customer_id, email))
                except Exception as e:
                    logger.warning("webhook_alert: Customer lookup failed: %s" % e)

            # Check if customer has an active subscription
            if resolved_customer_id:
                try:
                    subs = stripe.Subscription.list(customer=resolved_customer_id, status='active', limit=1)
                    if subs.data:
                        sub = subs.data[0]
                        # Map the Stripe price to a plan tier
                        plan_tier = _resolve_plan_tier(sub, resolved_plan or expected_plan)

                        # Update the user's plan in the database
                        fix_conn = _get_db()
                        if fix_conn and email:
                            try:
                                fc = fix_conn.cursor()
                                fc.execute("""
                                    UPDATE users SET plan = %s, stripe_customer_id = %s,
                                        subscription_status = 'active'
                                    WHERE LOWER(email) = %s
                                """, (plan_tier, resolved_customer_id, email))
                                rows_updated = fc.rowcount
                                fix_conn.commit()

                                if rows_updated > 0:
                                    auto_fix_result = "SUCCESS: Activated %s plan for %s" % (plan_tier, email)
                                    logger.info("webhook_alert: AUTO-FIX %s" % auto_fix_result)
                                else:
                                    auto_fix_result = "NO_MATCH: No user found with email %s" % email
                                    logger.warning("webhook_alert: %s" % auto_fix_result)
                            except Exception as e:
                                auto_fix_result = "DB_ERROR: %s" % str(e)
                                logger.error("webhook_alert: Auto-fix DB error: %s" % e)
                                try:
                                    fix_conn.rollback()
                                except Exception:
                                    pass
                            finally:
                                try:
                                    fix_conn.close()
                                except Exception:
                                    pass
                    else:
                        auto_fix_result = "NO_SUB: Customer %s has no active subscription" % resolved_customer_id
                        logger.warning("webhook_alert: %s" % auto_fix_result)
                except Exception as e:
                    auto_fix_result = "STRIPE_ERROR: %s" % str(e)
                    logger.error("webhook_alert: Subscription lookup failed: %s" % e)
            else:
                auto_fix_result = "NO_CUSTOMER: Could not resolve Stripe customer"
                logger.warning("webhook_alert: %s" % auto_fix_result)

        except Exception as e:
            auto_fix_result = "ERROR: %s" % str(e)
            logger.error("webhook_alert: Auto-activation failed: %s" % e)
    else:
        auto_fix_result = "SKIP: Stripe not configured or no identifiers provided"

    # --- Step 3: Update alert record with fix result ---
    if alert_logged and auto_fix_result:
        upd_conn = _get_db()
        if upd_conn:
            try:
                uc = upd_conn.cursor()
                uc.execute("""
                    UPDATE webhook_alerts SET auto_fix_attempted = TRUE, auto_fix_result = %s
                    WHERE email = %s AND created_at = %s
                """, (auto_fix_result, email, now))
                upd_conn.commit()
            except Exception:
                pass
            finally:
                try:
                    upd_conn.close()
                except Exception:
                    pass

    # --- Step 4: Email admin ---
    email_body = (
        "Stripe Webhook Alert\n"
        "====================\n"
        "Time: %s\n"
        "Email: %s\n"
        "Expected Plan: %s\n"
        "Session ID: %s\n"
        "Customer ID: %s\n"
        "Client Error: %s\n"
        "\nAuto-Fix Result: %s\n"
    ) % (now, email, expected_plan, session_id, customer_id, client_error, auto_fix_result or 'Not attempted')

    _send_admin_email(
        "DC Hub: Stripe payment activation alert — %s" % email,
        email_body
    )

    return jsonify({
        'success': True,
        'alert_logged': alert_logged,
        'auto_fix_attempted': auto_fix_result is not None,
        'auto_fix_result': auto_fix_result,
        'message': 'Alert received and processed',
    })


def _resolve_plan_tier(subscription, fallback_plan):
    """Map a Stripe subscription to a DC Hub plan tier."""
    # Check subscription metadata first
    metadata = subscription.get('metadata', {})
    if metadata.get('plan'):
        plan_key = metadata['plan']
        mapping = {
            'pro_monthly': 'pro', 'pro_annual': 'pro',
            'enterprise_monthly': 'enterprise', 'enterprise_annual': 'enterprise',
            'founding': 'founding',
        }
        return mapping.get(plan_key, 'pro')

    # Fall back to price-based detection
    try:
        items = subscription.get('items', {}).get('data', [])
        if items:
            price_id = items[0].get('price', {}).get('id', '')
            enterprise_prices = [
                os.environ.get('STRIPE_PRICE_ENTERPRISE_MONTHLY', ''),
                os.environ.get('STRIPE_PRICE_ENTERPRISE_ANNUAL', ''),
            ]
            if price_id in enterprise_prices:
                return 'enterprise'
            founding_price = os.environ.get('STRIPE_PRICE_FOUNDING', '')
            if price_id == founding_price:
                return 'founding'
    except Exception:
        pass

    # Last resort: use the fallback
    if 'enterprise' in fallback_plan:
        return 'enterprise'
    if 'founding' in fallback_plan:
        return 'founding'
    return 'pro'
