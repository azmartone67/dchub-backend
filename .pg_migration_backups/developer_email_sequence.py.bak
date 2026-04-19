"""
DC Hub — Developer Welcome Email Sequence (SendGrid)
════════════════════════════════════════════════════════
3-email drip sequence for new MCP developer registrations and trials.

INTEGRATION:
  1. Add SENDGRID_API_KEY to Railway env vars (you already have SendGrid)
  2. Import and call from main.py or a scheduler:
     from developer_email_sequence import send_welcome_email, send_drip_sequence
  3. On new trial creation: send_welcome_email(email, api_key, platform)
  4. Schedule drip checks: run_drip_check(get_db) daily at 10:00 UTC

v1.0 — March 2026
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger('dchub-developer-email')

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
FROM_EMAIL = "developers@dchub.cloud"
FROM_NAME = "DC Hub Developer Relations"


# ─────────────────────────────────────────────────────────────
# EMAIL TEMPLATES
# ─────────────────────────────────────────────────────────────

def _email_day0_welcome(api_key: str, platform: str) -> dict:
    """Day 0: Welcome + quick start."""
    return {
        'subject': "Your DC Hub Developer trial is live — here's your API key",
        'html': f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="text-align: center; padding: 20px 0;">
        <h1 style="color: #1B4F72; margin: 0;">Welcome to DC Hub</h1>
        <p style="color: #666; font-size: 16px;">14-day Developer trial activated</p>
    </div>

    <div style="background: #EBF5FB; border-left: 4px solid #1B4F72; padding: 16px; margin: 20px 0; border-radius: 4px;">
        <strong>Your API Key:</strong><br>
        <code style="background: #fff; padding: 4px 8px; border-radius: 4px; font-size: 14px;">{api_key}</code>
    </div>

    <h2 style="color: #1B4F72;">Quick Start (2 minutes)</h2>

    <p><strong>For {platform.title()}:</strong></p>
    <pre style="background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 13px;">
"env": {{
  "DCHUB_API_KEY": "{api_key}"
}}</pre>

    <p>Add this to your MCP config and restart. You now have:</p>
    <ul>
        <li><strong>1,000 API calls/day</strong> (vs 10 on free)</li>
        <li><strong>Full facility data</strong> — coordinates, power capacity, connectivity</li>
        <li><strong>Complete M&A details</strong> — buyer, seller, deal value, terms</li>
        <li><strong>Pipeline projects</strong> — all 58 projects with timelines</li>
        <li><strong>Infrastructure data</strong> — substations, transmission, gas pipelines</li>
    </ul>

    <div style="text-align: center; margin: 30px 0;">
        <a href="https://dchub.cloud/developers" style="background: #1B4F72; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 16px;">View Full API Docs</a>
    </div>

    <p style="color: #888; font-size: 13px;">
        Your trial runs for 14 days. After that, continue with a Developer plan at $49/mo — or your access reverts to the free tier automatically. No credit card was charged.
    </p>
</div>
""",
    }


def _email_day3_power(api_key: str) -> dict:
    """Day 3: Highlight unique data (power infrastructure)."""
    return {
        'subject': "DC Hub tracks 70,000+ substations — try our infrastructure API",
        'html': """
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h1 style="color: #1B4F72;">Did you know?</h1>

    <p style="font-size: 16px;">DC Hub is the only AI-accessible platform with <strong>real power infrastructure data</strong>:</p>

    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
        <tr style="background: #1B4F72; color: white;">
            <td style="padding: 10px; font-weight: bold;">Layer</td>
            <td style="padding: 10px; font-weight: bold;">Records</td>
            <td style="padding: 10px; font-weight: bold;">Coverage</td>
        </tr>
        <tr style="background: #EBF5FB;">
            <td style="padding: 10px;">Substations</td>
            <td style="padding: 10px;">70,000+</td>
            <td style="padding: 10px;">All US, 69kV+</td>
        </tr>
        <tr>
            <td style="padding: 10px;">Transmission Lines</td>
            <td style="padding: 10px;">85,000+</td>
            <td style="padding: 10px;">Interstate grid</td>
        </tr>
        <tr style="background: #EBF5FB;">
            <td style="padding: 10px;">Gas Pipelines</td>
            <td style="padding: 10px;">300,000+ miles</td>
            <td style="padding: 10px;">Interstate + gathering</td>
        </tr>
        <tr>
            <td style="padding: 10px;">Power Plants</td>
            <td style="padding: 10px;">11,000+</td>
            <td style="padding: 10px;">All fuel types, capacity</td>
        </tr>
    </table>

    <p><strong>Try it:</strong> Ask your AI agent: <em>"What power infrastructure is within 50km of Ashburn, Virginia?"</em></p>

    <p>Your agent will use the <code>get_infrastructure</code> tool to find nearby substations, transmission lines, and gas pipelines — data that no other MCP server provides.</p>

    <div style="text-align: center; margin: 30px 0;">
        <a href="https://dchub.cloud/land-power" style="background: #1B4F72; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 16px;">Explore the Land & Power Map</a>
    </div>

    <p style="color: #888; font-size: 13px;">You're on day 3 of your 14-day trial. 11 days remaining.</p>
</div>
""",
    }


def _email_day7_convert(api_key: str) -> dict:
    """Day 7: Usage stats + conversion nudge."""
    return {
        'subject': "Your DC Hub trial: 7 days in — here's what you've unlocked",
        'html': f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h1 style="color: #1B4F72;">Halfway through your trial</h1>

    <p style="font-size: 16px;">Your Developer trial has been active for 7 days. Here's what you've had access to:</p>

    <div style="background: #EBF5FB; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <h3 style="color: #1B4F72; margin-top: 0;">With your Developer key, your AI agent gets:</h3>
        <ul style="line-height: 1.8;">
            <li>Full facility specs (coordinates, MW, connectivity)</li>
            <li>Complete M&A transaction details</li>
            <li>All 58 pipeline projects with investment data</li>
            <li>Infrastructure proximity data for site selection</li>
            <li>Market intelligence with provider rankings</li>
        </ul>

        <h3 style="color: #C0392B; margin-bottom: 5px;">Without it (free tier):</h3>
        <ul style="line-height: 1.8; color: #666;">
            <li>3 results per query (vs 100)</li>
            <li>Names only — no specs, no coordinates</li>
            <li>Transaction counts only — no details</li>
            <li>10 calls/day max</li>
        </ul>
    </div>

    <p><strong>7 days remaining.</strong> Lock in your Developer access before the trial ends:</p>

    <div style="text-align: center; margin: 30px 0;">
        <a href="https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c" style="background: #27AE60; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-size: 18px; font-weight: bold;">Continue for $49/mo →</a>
    </div>

    <p style="color: #888; font-size: 13px;">
        Cancel anytime. Your API key stays the same — no migration needed.<br>
        Questions? Reply to this email.
    </p>
</div>
""",
    }


# ─────────────────────────────────────────────────────────────
# SEND FUNCTIONS
# ─────────────────────────────────────────────────────────────

def _send_email(to_email: str, subject: str, html: str) -> bool:
    """Send via SendGrid API."""
    if not SENDGRID_API_KEY:
        logger.warning(f"⚠️  SENDGRID_API_KEY not set — would send '{subject}' to {to_email}")
        return False

    try:
        import requests
        resp = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={
                'Authorization': f'Bearer {SENDGRID_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'personalizations': [{'to': [{'email': to_email}]}],
                'from': {'email': FROM_EMAIL, 'name': FROM_NAME},
                'subject': subject,
                'content': [{'type': 'text/html', 'value': html}],
            },
            timeout=10,
        )
        if resp.status_code in (200, 202):
            logger.info(f"✅ Email sent: '{subject}' → {to_email}")
            return True
        else:
            logger.error(f"❌ SendGrid error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"❌ Email send error: {e}")
        return False


def send_welcome_email(email: str, api_key: str, platform: str = 'unknown') -> bool:
    """Send Day 0 welcome email immediately on trial creation."""
    template = _email_day0_welcome(api_key, platform)
    return _send_email(email, template['subject'], template['html'])


def run_drip_check(get_db) -> int:
    """
    Check all active trials and send appropriate drip emails.
    Run this daily at 10:00 UTC.

    Returns number of emails sent.
    """
    conn = None
    sent = 0

    try:
        conn = get_db()
        cur = conn.cursor()

        now = datetime.now(timezone.utc)

        # Find trials that need Day 3 email (created 3 days ago, not yet sent)
        cur.execute("""
            SELECT ak.email, ak.key, ak.created_at
            FROM api_keys ak
            WHERE ak.rate_limit_tier = 'trial'
              AND ak.is_active = true
              AND ak.created_at BETWEEN %s AND %s
              AND NOT EXISTS (
                  SELECT 1 FROM email_drip_log
                  WHERE email = ak.email AND drip_name = 'day3_power'
              )
        """, (now - timedelta(days=4), now - timedelta(days=2)))

        for row in cur.fetchall():
            email, api_key = row[0], row[1]
            template = _email_day3_power(api_key)
            if _send_email(email, template['subject'], template['html']):
                _log_drip(cur, email, 'day3_power')
                sent += 1

        # Find trials that need Day 7 email
        cur.execute("""
            SELECT ak.email, ak.key, ak.created_at
            FROM api_keys ak
            WHERE ak.rate_limit_tier = 'trial'
              AND ak.is_active = true
              AND ak.created_at BETWEEN %s AND %s
              AND NOT EXISTS (
                  SELECT 1 FROM email_drip_log
                  WHERE email = ak.email AND drip_name = 'day7_convert'
              )
        """, (now - timedelta(days=8), now - timedelta(days=6)))

        for row in cur.fetchall():
            email, api_key = row[0], row[1]
            template = _email_day7_convert(api_key)
            if _send_email(email, template['subject'], template['html']):
                _log_drip(cur, email, 'day7_convert')
                sent += 1

        conn.commit()
        logger.info(f"✅ Drip check complete: {sent} emails sent")

    except Exception as e:
        logger.error(f"❌ Drip check error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    return sent


def _log_drip(cur, email, drip_name):
    """Log that a drip email was sent."""
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_drip_log (
                id SERIAL PRIMARY KEY,
                email VARCHAR(500),
                drip_name VARCHAR(100),
                sent_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO email_drip_log (email, drip_name) VALUES (%s, %s)
        """, (email, drip_name))
    except Exception as e:
        logger.warning(f"Drip log error: {e}")


# ─────────────────────────────────────────────────────────────
# SCHEDULER INTEGRATION
# ─────────────────────────────────────────────────────────────
# Add to crawler_scheduler.py's knowledge_sync or as standalone:
#
# In _run_knowledge_sync(), add as STEP 5:
#
#     # STEP 5: Developer drip email check
#     try:
#         from developer_email_sequence import run_drip_check
#         from db_utils import get_db
#         sent = run_drip_check(get_db)
#         logger.info(f"   [5/5] Developer emails: {sent} sent")
#     except ImportError:
#         logger.warning("   [5/5] Developer email sequence not available")
#     except Exception as e:
#         logger.warning(f"   [5/5] Developer email error: {e}")
