"""
DC Hub Welcome Email Sequence v1.0
===================================
3 lifecycle emails sent via SendGrid after free account creation.

Integration:
  1. Call send_welcome_email() from your signup handler (handle_google_auth or registration endpoint)
  2. Set up a daily cron job that calls check_and_send_drip_emails() to send Day 3 and Day 7 emails
  3. Requires SENDGRID_API_KEY environment variable

Tables needed (auto-created):
  - email_drip_log: tracks which emails have been sent to which users
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta
from email_fallback import send_email_resilient
from ai_surface_canon import canon_text

logger = logging.getLogger('welcome_emails')

# ─── Billing copy (canonical — never hardcode) ────────────
# Dollar price comes from tier_registry.TIER_PRICE_USD_MONTH; the checkout URL
# comes from routes/_stripe_links.py. Templates carry {pro_price}/{pro_url}
# placeholders and _render() fills them in.
#
# Until 2026-08-01 the day3 + day7 templates hardcoded "$199/month" and the
# Payment Link 9B6fZi1cCdjT3ml8i6aZi00. Pro repriced to $299 at r-reprice
# (2026-06-19) and the copy never followed, so for six weeks every drip
# recipient was quoted $199 — and that link is the *founding* link used by
# dashboard.html / api_server.py / api_tier_gating.py, not a Pro link at all,
# so the CTA sold a different plan than its own label.
#
# WELCOME_CTA_TIER picks which plan the day7 CTA sells. Default is 'founding'
# ($99/mo, SH52-109 owner call 2026-08-21) while
# /api/v1/founding-customers/count reports remaining>0 (8 of 25 on
# 2026-08-21); _billing_vars() falls back to 'pro' (the canonical list plan)
# automatically when the counter reads 0, so a perpetual drip can never point
# at a sold-out link. Env WELCOME_CTA_TIER is an explicit override. Accepted
# values = tiers with BOTH tier_registry.price()>0 AND a STRIPE_LINKS entry:
# starter, developer, pro, team, founding (anything else raises in
# _billing_vars, as before).
WELCOME_CTA_TIER = (os.environ.get('WELCOME_CTA_TIER') or 'founding').strip().lower()


def _effective_cta_tier():
    """WELCOME_CTA_TIER, demoted to 'pro' only when Founding is sold out.

    A DB blip must not demote the CTA: founding_status() already reports
    program-active on a DB failure, and any exception here keeps the
    configured tier.
    """
    tier = WELCOME_CTA_TIER
    if tier == 'founding':
        try:
            from routes.founding_customers import founding_status
            if int(founding_status().get('remaining', 1) or 0) <= 0:
                tier = 'pro'
        except Exception:
            pass
    return tier


def _billing_vars():
    """Canonical {pro_price, pro_url, cta_label} for the effective CTA tier.

    Raises on an unresolvable tier rather than falling back to a literal: this
    is a billing surface, and sending a confident wrong price is worse than
    sending nothing. The drip route surfaces the failure as a 500.
    """
    import tier_registry

    tier = _effective_cta_tier()
    price = tier_registry.price(tier)
    url = tier_registry._stripe_link(tier)
    if not price or not url:
        raise RuntimeError(
            f"welcome_emails: cannot resolve billing copy for tier "
            f"'{tier}' (price={price!r}, url={url!r}) — refusing to "
            f"send an email quoting an unverified price"
        )
    label = ('Founding Member' if tier == 'founding'
             else tier_registry.TIERS.get(tier, {}).get('label', 'Pro'))
    return {'pro_price': f"${price:,}", 'pro_url': url, 'cta_label': label}


def _render(template_str, **kwargs):
    """Fill a template, always injecting the canonical billing vars."""
    return template_str.format(**_billing_vars(), **kwargs)


# ─── Email Templates ──────────────────────────────────────

EMAILS = {
    'day0_welcome': {
        'subject': 'Welcome to DC Hub — Here\'s How to Find Your First Facility',
        'delay_days': 0,
        'html': canon_text('''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #0a0f1e; color: #e0e0e0; }}
  .container {{ max-width: 560px; margin: 0 auto; padding: 32px 24px; }}
  .logo {{ font-size: 20px; font-weight: 700; color: #00c8ff; margin-bottom: 24px; }}
  h1 {{ color: #ffffff; font-size: 22px; margin: 0 0 16px; line-height: 1.3; }}
  p {{ font-size: 15px; line-height: 1.6; color: #b0b8c8; margin: 0 0 16px; }}
  .cta {{ display: inline-block; background: linear-gradient(135deg, #00c8ff, #0080ff); color: #fff; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; margin: 8px 0 24px; }}
  .step {{ background: rgba(255,255,255,0.05); border-left: 3px solid #00c8ff; padding: 12px 16px; margin: 12px 0; border-radius: 0 6px 6px 0; }}
  .step strong {{ color: #fff; }}
  .divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 24px 0; }}
  .footer {{ font-size: 12px; color: #666; text-align: center; margin-top: 32px; }}
  .footer a {{ color: #00c8ff; text-decoration: none; }}
</style>
</head>
<body>
<div class="container">
  <div class="logo">⚡ DC Hub</div>

  <h1>Welcome to DC Hub, {name}!</h1>

  <p>You now have access to the largest data center intelligence platform — {canon_facilities} facilities across 170+ countries, updated in real-time.</p>

  <p><strong>Try your first search in 30 seconds:</strong></p>

  <div class="step">
    <strong>Step 1:</strong> Go to <a href="https://dchub.cloud" style="color:#00c8ff;">dchub.cloud</a> and sign in
  </div>
  <div class="step">
    <strong>Step 2:</strong> Search for a market you're tracking (e.g., "Phoenix" or "Dallas")
  </div>
  <div class="step">
    <strong>Step 3:</strong> Browse facilities by tier, capacity, and provider
  </div>

  <a href="https://dchub.cloud" class="cta">Start Exploring →</a>

  <p>With your free account, you can:</p>
  <p>
    ✓ &nbsp;Browse all {canon_facilities} facilities<br>
    ✓ &nbsp;Search by market, tier, and capacity<br>
    ✓ &nbsp;Access real-time news from 30+ sources<br>
    ✓ &nbsp;3 market comparisons per month<br>
    ✓ &nbsp;Save up to 5 searches
  </p>

  <hr class="divider">

  <p style="font-size:13px; color:#888;">Need more? <strong>Pro members</strong> get unlimited searches, PDF reports, full API access, and Land & Power mapping. <a href="https://dchub.cloud/pricing" style="color:#00c8ff;">See plans →</a></p>

  <div class="footer">
    <p>DC Hub — Data Center Intelligence Platform</p>
    <p>Built in Phoenix, AZ 🌵</p>
    <p><a href="https://dchub.cloud/privacy">Privacy</a> · <a href="https://dchub.cloud/terms">Terms</a></p>
  </div>
</div>
</body>
</html>
''')
    },

    'day3_value': {
        'subject': 'Did You Know? DC Hub Tracks 612+ Substations Near Data Centers',
        'delay_days': 3,
        'html': '''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #0a0f1e; color: #e0e0e0; }}
  .container {{ max-width: 560px; margin: 0 auto; padding: 32px 24px; }}
  .logo {{ font-size: 20px; font-weight: 700; color: #00c8ff; margin-bottom: 24px; }}
  h1 {{ color: #ffffff; font-size: 22px; margin: 0 0 16px; line-height: 1.3; }}
  p {{ font-size: 15px; line-height: 1.6; color: #b0b8c8; margin: 0 0 16px; }}
  .cta {{ display: inline-block; background: linear-gradient(135deg, #00c8ff, #0080ff); color: #fff; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; margin: 8px 0 24px; }}
  .stat-row {{ display: flex; gap: 16px; margin: 20px 0; }}
  .stat-box {{ flex: 1; background: rgba(0,200,255,0.06); border: 1px solid rgba(0,200,255,0.12); border-radius: 10px; padding: 16px; text-align: center; }}
  .stat-num {{ font-size: 24px; font-weight: 700; color: #00c8ff; }}
  .stat-label {{ font-size: 12px; color: #888; margin-top: 4px; }}
  .divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 24px 0; }}
  .footer {{ font-size: 12px; color: #666; text-align: center; margin-top: 32px; }}
  .footer a {{ color: #00c8ff; text-decoration: none; }}
</style>
</head>
<body>
<div class="container">
  <div class="logo">⚡ DC Hub</div>

  <h1>Beyond Facility Data — Power Infrastructure Matters</h1>

  <p>Hey {name},</p>

  <p>Most data center databases show you buildings. DC Hub shows you the <strong>power infrastructure</strong> around them — because proximity to substations, transmission lines, and fiber routes is what determines if a site is actually viable.</p>

  <div class="stat-row">
    <div class="stat-box">
      <div class="stat-num">612+</div>
      <div class="stat-label">Substations</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">3,100+</div>
      <div class="stat-label">Transmission Lines</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">6,900+</div>
      <div class="stat-label">Power Plants</div>
    </div>
  </div>

  <p>Our <strong>Land & Power Map</strong> overlays 40+ infrastructure layers on a single interactive map — substations, FEMA flood zones, fiber routes, utility territories, and more.</p>

  <a href="https://dchub.cloud/land-power" class="cta">Explore Land & Power Map →</a>

  <p style="font-size: 13px; color: #888;">Land & Power is available to paid members. <a href="https://dchub.cloud/pricing" style="color:#00c8ff;">{cta_label}s get it for {pro_price}/month.</a></p>

  <hr class="divider">

  <p style="font-size:13px; color:#888;">You're receiving this because you created a DC Hub account. <a href="https://dchub.cloud/settings" style="color:#00c8ff;">Manage preferences</a></p>

  <div class="footer">
    <p>DC Hub — Data Center Intelligence Platform</p>
    <p><a href="https://dchub.cloud/privacy">Privacy</a> · <a href="https://dchub.cloud/terms">Terms</a></p>
  </div>
</div>
</body>
</html>
'''
    },

    'day7_convert': {
        'subject': '{name}, Your 3 Free Market Comparisons Are Waiting',
        'delay_days': 7,
        'html': '''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #0a0f1e; color: #e0e0e0; }}
  .container {{ max-width: 560px; margin: 0 auto; padding: 32px 24px; }}
  .logo {{ font-size: 20px; font-weight: 700; color: #00c8ff; margin-bottom: 24px; }}
  h1 {{ color: #ffffff; font-size: 22px; margin: 0 0 16px; line-height: 1.3; }}
  p {{ font-size: 15px; line-height: 1.6; color: #b0b8c8; margin: 0 0 16px; }}
  .cta {{ display: inline-block; background: linear-gradient(135deg, #00c8ff, #0080ff); color: #fff; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; margin: 8px 0 16px; }}
  .cta-secondary {{ display: inline-block; background: transparent; color: #00c8ff; border: 1px solid rgba(0,200,255,0.3); padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; margin: 8px 0 24px; }}
  .comparison {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 20px; margin: 20px 0; }}
  .comparison h3 {{ color: #fff; font-size: 16px; margin: 0 0 12px; }}
  .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 14px; }}
  .row:last-child {{ border: none; }}
  .metric {{ color: #888; }}
  .value {{ color: #fff; font-weight: 600; }}
  .highlight {{ color: #00c8ff; }}
  .divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 24px 0; }}
  .footer {{ font-size: 12px; color: #666; text-align: center; margin-top: 32px; }}
  .footer a {{ color: #00c8ff; text-decoration: none; }}
  .badge {{ display: inline-block; background: rgba(255,180,0,0.15); color: #ffb400; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; margin-left: 8px; }}
</style>
</head>
<body>
<div class="container">
  <div class="logo">⚡ DC Hub</div>

  <h1>Compare Markets Side-by-Side</h1>

  <p>Hey {name},</p>

  <p>Your free account includes <strong>3 market comparisons per month</strong>. Here's a preview of what a comparison looks like:</p>

  <div class="comparison">
    <h3>Phoenix vs. Dallas-Fort Worth</h3>
    <div class="row"><span class="metric">Vacancy Rate</span><span class="value">3.1% vs <span class="highlight">1.8%</span></span></div>
    <div class="row"><span class="metric">Under Construction</span><span class="value">680 MW vs <span class="highlight">770 MW</span></span></div>
    <div class="row"><span class="metric">Avg Pricing</span><span class="value">$165/kW vs $180/kW</span></div>
    <div class="row"><span class="metric">Facilities</span><span class="value">45 vs 62</span></div>
  </div>

  <a href="https://dchub.cloud" class="cta">Run Your Comparison →</a>

  <hr class="divider">

  <p><strong>Ready for unlimited access?</strong></p>

  <p>{cta_label}s get everything — unlimited comparisons, PDF reports, full API, Land & Power mapping, and site scoring — for <strong>{pro_price}/month</strong>.</p>

  <a href="{pro_url}" class="cta">Become a {cta_label} — {pro_price}/mo →</a>
  <br>
  <a href="https://dchub.cloud/pricing" class="cta-secondary">Compare All Plans →</a>

  <hr class="divider">

  <p style="font-size:13px; color:#888;">You're receiving this because you created a DC Hub account on {signup_date}. <a href="https://dchub.cloud/settings" style="color:#00c8ff;">Manage preferences</a></p>

  <div class="footer">
    <p>DC Hub — Data Center Intelligence Platform</p>
    <p><a href="https://dchub.cloud/privacy">Privacy</a> · <a href="https://dchub.cloud/terms">Terms</a></p>
  </div>
</div>
</body>
</html>
'''
    }
}


# ─── Database Setup ────────────────────────────────────────

def init_drip_table(conn):
    """Create the email_drip_log table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS email_drip_log (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            email_key TEXT NOT NULL,
            sent_at TIMESTAMPTZ DEFAULT NOW(),
            status TEXT DEFAULT 'sent',
            UNIQUE(user_email, email_key)
        )
    ''')
    conn.commit()
    cur.close()
    logger.info("✅ email_drip_log table ready")


# ─── SendGrid Integration ─────────────────────────────────

def _send_email(to_email, subject, html_body, from_email='intelligence@dchub.cloud', from_name='DC Hub'):
    """Send a single email via resilient channel (SendGrid → Resend fallback)."""
    api_key = os.environ.get('SENDGRID_API_KEY')
    resend_key = os.environ.get('RESEND_API_KEY') or os.environ.get('DCHUB_RESEND_API_KEY')
    if not api_key and not resend_key:
        logger.error("No email channel configured (SendGrid/Resend) — cannot send email")
        return False

    _ok = send_email_resilient(to_email, subject, html_body, None,
                               from_email=from_email, from_name=from_name)
    if _ok:
        logger.info(f"✅ Email sent to {to_email}: {subject}")
        return True
    logger.error(f"❌ Email send failed (SendGrid + Resend): {subject}")
    return False


def _already_sent(conn, email, email_key):
    """Check if we already sent this email to this user."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM email_drip_log WHERE user_email = %s AND email_key = %s",
        (email, email_key)
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def _log_sent(conn, email, email_key):
    """Record that we sent this email."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO email_drip_log (user_email, email_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (email, email_key)
    )
    conn.commit()
    cur.close()


# ─── Public API ────────────────────────────────────────────

def send_welcome_email(conn, user_email, user_name='there'):
    """
    Call this immediately after a new user signs up.
    Sends the Day 0 welcome email.

    Args:
        conn: PostgreSQL connection (from your pool)
        user_email: the new user's email
        user_name: the new user's display name (optional)
    """
    init_drip_table(conn)

    email_key = 'day0_welcome'
    if _already_sent(conn, user_email, email_key):
        logger.info(f"Skipping {email_key} for {user_email} — already sent")
        return

    template = EMAILS[email_key]
    name = user_name.split()[0] if user_name else 'there'
    subject = template['subject'].format(name=name)
    html = _render(
        template['html'],
        name=name,
        signup_date=datetime.utcnow().strftime('%B %d, %Y')
    )

    if _send_email(user_email, subject, html):
        _log_sent(conn, user_email, email_key)


def check_and_send_drip_emails(conn):
    """
    Call this from a daily cron job (e.g., Railway cron or scheduled task).
    Checks all free users and sends Day 3 and Day 7 emails if due.

    Args:
        conn: PostgreSQL connection (from your pool)
    """
    init_drip_table(conn)

    cur = conn.cursor()

    # Get all free users with their signup dates
    cur.execute("""
        SELECT email, name, created_at::timestamptz as created_at
        FROM users
        WHERE plan = 'free'
        AND email IS NOT NULL
        ORDER BY created_at DESC
    """)
    users = cur.fetchall()
    cur.close()

    now = datetime.utcnow()
    sent_count = 0

    for user in users:
        email = user[0]
        name = (user[1] or 'there').split()[0]
        created_at = user[2]

        if not created_at:
            continue

        days_since_signup = (now - created_at.replace(tzinfo=None)).days

        for email_key, template in EMAILS.items():
            if email_key == 'day0_welcome':
                continue  # Day 0 is sent immediately at signup

            if days_since_signup >= template['delay_days']:
                if not _already_sent(conn, email, email_key):
                    subject = template['subject'].format(name=name)
                    html = _render(
                        template['html'],
                        name=name,
                        signup_date=created_at.strftime('%B %d, %Y') if created_at else 'recently'
                    )

                    if _send_email(email, subject, html):
                        _log_sent(conn, email, email_key)
                        sent_count += 1
                        # Rate limit: don't blast all at once
                        time.sleep(1)

    logger.info(f"Drip check complete: {sent_count} emails sent to {len(users)} free users")
    return sent_count


# ─── Flask Route (optional admin endpoint) ─────────────────

def setup_drip_routes(app, get_db_conn):
    """
    Register an admin endpoint to manually trigger drip check.

    Usage in main.py:
        from welcome_emails import setup_drip_routes
        setup_drip_routes(app, get_db_conn)
    """
    from flask import jsonify, request

    @app.route('/api/admin/drip-check', methods=['POST'])
    def admin_drip_check():
        # 2026-07-03: also accept the X-Admin-Key header (what the in-process
        # cron heartbeat's _hit sends) in addition to the legacy ?admin_key=
        # query param, so the re-homed drip cron authorizes without the admin
        # key ever appearing in a URL/access-log. Re-homed off the retired
        # off-repo Replit scheduler into cron_heartbeat _DISPATCH.
        expected = os.environ.get('DCHUB_ADMIN_KEY', '')
        admin_key = request.args.get('admin_key') or request.headers.get('X-Admin-Key')
        if not expected or admin_key != expected:
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_conn()
        try:
            count = check_and_send_drip_emails(conn)
            return jsonify({'status': 'ok', 'emails_sent': count})
        finally:
            conn.close()

    @app.route('/api/admin/drip-status', methods=['GET'])
    def admin_drip_status():
        admin_key = request.args.get('admin_key')
        if admin_key != os.environ.get('DCHUB_ADMIN_KEY', ''):
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT email_key, COUNT(*), MAX(sent_at)
                FROM email_drip_log
                GROUP BY email_key
                ORDER BY email_key
            """)
            rows = cur.fetchall()
            cur.close()
            return jsonify({
                'status': 'ok',
                'drip_stats': [
                    {'email': r[0], 'sent_count': r[1], 'last_sent': str(r[2])}
                    for r in rows
                ]
            })
        finally:
            conn.close()

    logger.info("✅ Drip email admin routes registered: /api/admin/drip-check, /api/admin/drip-status")
