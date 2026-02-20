"""
DC HUB EMAIL SERVICE v80
========================
Email Welcome Series & Drip Campaign System
Integrates with Office 365 SMTP

Features:
- Office 365 SMTP integration
- HTML email templates
- Scheduled email queue
- Welcome series automation
- Open/click tracking (via pixels)
- Unsubscribe handling
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import json
import threading
import time
from db_utils import get_db

# =============================================================================
# CONFIGURATION
# =============================================================================

# Office 365 SMTP Settings
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.office365.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')  # Your Office 365 email
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')  # App password or OAuth token
SMTP_FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'DC Hub')
SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL', SMTP_USER)

# App Settings
APP_URL = os.environ.get('APP_URL', 'https://dchub.cloud')
TRACKING_ENABLED = os.environ.get('EMAIL_TRACKING', 'true').lower() == 'true'

DB_PATH = "dc_nexus.db"

# =============================================================================
# DATABASE SETUP
# =============================================================================

def init_email_tables():
    """Initialize email-related database tables"""
    conn = get_db()
    c = conn.cursor()
    
    # Email queue table for scheduled sends
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_queue (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            email TEXT NOT NULL,
            template_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            body_html TEXT,
            body_text TEXT,
            scheduled_at TEXT NOT NULL,
            sent_at TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            retry_count INTEGER DEFAULT 0,
            sequence_id TEXT,
            sequence_step INTEGER,
            created_at TEXT
        )
    """)
    
    # Email tracking table
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_tracking (
            id TEXT PRIMARY KEY,
            email_id TEXT,
            email TEXT,
            event_type TEXT,
            event_data TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT
        )
    """)
    
    # Welcome series tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS welcome_series (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            email TEXT UNIQUE NOT NULL,
            current_step INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            status TEXT DEFAULT 'active',
            last_email_sent TEXT
        )
    """)
    
    # Email templates table (for custom templates)
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_templates (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            subject TEXT NOT NULL,
            body_html TEXT,
            body_text TEXT,
            variables TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    print("✅ Email tables initialized")


# =============================================================================
# EMAIL TEMPLATES
# =============================================================================

def get_email_base_template():
    """Base HTML template wrapper"""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #1a1a2e;
            background-color: #f5f5f7;
        }}
        
        .email-wrapper {{
            max-width: 600px;
            margin: 0 auto;
            background: #ffffff;
        }}
        
        .email-header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 32px 40px;
            text-align: center;
        }}
        
        .logo {{
            font-size: 28px;
            font-weight: 700;
            color: #ffffff;
            letter-spacing: -0.5px;
        }}
        
        .logo span {{
            color: #00d4ff;
        }}
        
        .email-body {{
            padding: 40px;
        }}
        
        h1 {{
            font-size: 24px;
            font-weight: 700;
            color: #1a1a2e;
            margin-bottom: 16px;
        }}
        
        h2 {{
            font-size: 20px;
            font-weight: 600;
            color: #1a1a2e;
            margin: 24px 0 12px;
        }}
        
        p {{
            font-size: 16px;
            color: #4a4a5a;
            margin-bottom: 16px;
        }}
        
        .cta-button {{
            display: inline-block;
            background: linear-gradient(135deg, #00d4ff 0%, #0099cc 100%);
            color: #ffffff !important;
            padding: 14px 32px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 16px;
            margin: 20px 0;
            transition: transform 0.2s;
        }}
        
        .cta-button:hover {{
            transform: translateY(-2px);
        }}
        
        .feature-box {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 20px;
            margin: 20px 0;
            border-left: 4px solid #00d4ff;
        }}
        
        .feature-box h3 {{
            font-size: 16px;
            font-weight: 600;
            color: #1a1a2e;
            margin-bottom: 8px;
        }}
        
        .feature-box p {{
            font-size: 14px;
            color: #6a6a7a;
            margin: 0;
        }}
        
        .stats-row {{
            display: flex;
            justify-content: space-around;
            text-align: center;
            padding: 24px 0;
            background: #f8f9fa;
            border-radius: 12px;
            margin: 24px 0;
        }}
        
        .stat-item {{
            flex: 1;
        }}
        
        .stat-number {{
            font-size: 28px;
            font-weight: 700;
            color: #00d4ff;
        }}
        
        .stat-label {{
            font-size: 12px;
            color: #6a6a7a;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .divider {{
            height: 1px;
            background: #e5e5ea;
            margin: 32px 0;
        }}
        
        .email-footer {{
            background: #f8f9fa;
            padding: 32px 40px;
            text-align: center;
        }}
        
        .footer-links {{
            margin-bottom: 16px;
        }}
        
        .footer-links a {{
            color: #6a6a7a;
            text-decoration: none;
            margin: 0 12px;
            font-size: 14px;
        }}
        
        .footer-links a:hover {{
            color: #00d4ff;
        }}
        
        .footer-text {{
            font-size: 12px;
            color: #9a9aaa;
        }}
        
        .unsubscribe {{
            color: #9a9aaa;
            text-decoration: underline;
        }}
        
        @media (max-width: 600px) {{
            .email-body {{
                padding: 24px;
            }}
            .email-header {{
                padding: 24px;
            }}
            .stats-row {{
                flex-direction: column;
                gap: 16px;
            }}
        }}
    </style>
</head>
<body>
    <div class="email-wrapper">
        <div class="email-header">
            <div class="logo">DC<span>Hub</span></div>
        </div>
        <div class="email-body">
            {content}
        </div>
        <div class="email-footer">
            <div class="footer-links">
                <a href="{app_url}">Dashboard</a>
                <a href="{app_url}/pricing.html">Pricing</a>
                <a href="{app_url}/about.html">About</a>
            </div>
            <p class="footer-text">
                © 2025 DC Hub. All rights reserved.<br>
                <a href="{app_url}/api/email/unsubscribe?token={unsubscribe_token}" class="unsubscribe">Unsubscribe</a>
            </p>
            {tracking_pixel}
        </div>
    </div>
</body>
</html>
"""


# Welcome Series Email Templates
WELCOME_SERIES_TEMPLATES = {
    1: {
        "name": "welcome_1_intro",
        "subject": "Welcome to DC Hub – Your Data Center Intelligence Platform 🚀",
        "delay_hours": 0,  # Send immediately
        "content": """
            <h1>Welcome to DC Hub, {name}!</h1>
            <p>You've just joined <strong>20,000+ data center professionals</strong> who use DC Hub to make smarter infrastructure decisions.</p>
            
            <div class="stats-row">
                <div class="stat-item">
                    <div class="stat-number">20K+</div>
                    <div class="stat-label">Facilities</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number">140+</div>
                    <div class="stat-label">Countries</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number">50+</div>
                    <div class="stat-label">Markets</div>
                </div>
            </div>
            
            <p>Here's what you can do right now:</p>
            
            <div class="feature-box">
                <h3>🗺️ Interactive Map</h3>
                <p>Explore data centers worldwide with real-time filtering by market, provider, and power capacity.</p>
            </div>
            
            <div class="feature-box">
                <h3>📊 Market Intelligence</h3>
                <p>Compare markets side-by-side with detailed analytics on power, fiber, and growth trends.</p>
            </div>
            
            <div class="feature-box">
                <h3>🤖 AI Agents</h3>
                <p>Let our AI agents help with sales research, data enrichment, and social content.</p>
            </div>
            
            <p style="text-align: center;">
                <a href="{app_url}/dashboard.html" class="cta-button">Explore Your Dashboard →</a>
            </p>
            
            <p>Questions? Just reply to this email – I personally read every response.</p>
            
            <p>— Jonathan<br><span style="color: #6a6a7a;">Founder, DC Hub</span></p>
        """
    },
    
    2: {
        "name": "welcome_2_features",
        "subject": "3 features you should try first on DC Hub",
        "delay_hours": 48,  # 2 days later
        "content": """
            <h1>Getting the most from DC Hub</h1>
            <p>Hi {name},</p>
            <p>Now that you've had a chance to explore, here are the 3 features our power users can't live without:</p>
            
            <h2>1. Market Comparison Tool</h2>
            <p>Compare up to 3 markets side-by-side. See power availability, fiber connectivity, pricing trends, and growth projections in one view.</p>
            <p><a href="{app_url}/compare.html" style="color: #00d4ff;">Try Market Comparison →</a></p>
            
            <div class="divider"></div>
            
            <h2>2. Custom PDF Reports</h2>
            <p>Generate professional market reports you can share with your team or clients. Perfect for site selection committees and investor presentations.</p>
            <p><a href="{app_url}/dashboard.html" style="color: #00d4ff;">Generate a Report →</a></p>
            
            <div class="divider"></div>
            
            <h2>3. AI Sales Agent</h2>
            <p>Our AI researches prospects, identifies their data center needs, and drafts personalized outreach. Users report 3x higher response rates.</p>
            <p><a href="{app_url}/agents.html" style="color: #00d4ff;">Meet the AI Agents →</a></p>
            
            <p style="text-align: center; margin-top: 32px;">
                <a href="{app_url}/dashboard.html" class="cta-button">Open DC Hub →</a>
            </p>
            
            <p>Tomorrow, I'll show you how top users leverage DC Hub for competitive intelligence.</p>
        """
    },
    
    3: {
        "name": "welcome_3_usecase",
        "subject": "How Colo brokers use DC Hub to win more deals",
        "delay_hours": 120,  # 5 days
        "content": """
            <h1>Real-world use case: Site Selection</h1>
            <p>Hi {name},</p>
            <p>Last week, a colocation broker used DC Hub to close a 2MW deal in under 30 days. Here's how:</p>
            
            <div class="feature-box">
                <h3>Step 1: Market Shortlist</h3>
                <p>Used our comparison tool to narrow 50 potential markets down to 3 based on power costs, fiber density, and tax incentives.</p>
            </div>
            
            <div class="feature-box">
                <h3>Step 2: Facility Analysis</h3>
                <p>Filtered 400+ facilities to 12 candidates meeting specific power, cooling, and compliance requirements.</p>
            </div>
            
            <div class="feature-box">
                <h3>Step 3: Client Presentation</h3>
                <p>Generated 3 PDF reports with market data, facility specs, and our proprietary scoring—presented to the client within 48 hours.</p>
            </div>
            
            <p><strong>Result:</strong> Client signed a 5-year lease. The broker earned a $180K commission.</p>
            
            <div class="divider"></div>
            
            <p>Whether you're in sales, procurement, or strategy, DC Hub gives you the data edge your competitors don't have.</p>
            
            <p style="text-align: center;">
                <a href="{app_url}/pricing.html" class="cta-button">See Pro Features →</a>
            </p>
        """
    },
    
    4: {
        "name": "welcome_4_pro",
        "subject": "Unlock the full power of DC Hub",
        "delay_hours": 240,  # 10 days
        "content": """
            <h1>Ready to go Pro?</h1>
            <p>Hi {name},</p>
            <p>You've been using DC Hub for over a week now. Here's what Pro members get that free users don't:</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 24px 0;">
                <tr style="background: #f8f9fa;">
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea;"><strong>Feature</strong></td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;"><strong>Free</strong></td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;"><strong>Pro</strong></td>
                </tr>
                <tr>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea;">Facility Records</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;">100/day</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center; color: #00d4ff;"><strong>Unlimited</strong></td>
                </tr>
                <tr>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea;">PDF Reports</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;">2/month</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center; color: #00d4ff;"><strong>Unlimited</strong></td>
                </tr>
                <tr>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea;">AI Agent Credits</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;">10/month</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center; color: #00d4ff;"><strong>500/month</strong></td>
                </tr>
                <tr>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea;">API Access</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;">❌</td>
                    <td style="padding: 16px; border-bottom: 1px solid #e5e5ea; text-align: center;">✅</td>
                </tr>
                <tr>
                    <td style="padding: 16px;">Export to CSV/Excel</td>
                    <td style="padding: 16px; text-align: center;">❌</td>
                    <td style="padding: 16px; text-align: center;">✅</td>
                </tr>
            </table>
            
            <p style="text-align: center; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 24px; border-radius: 12px; color: #fff;">
                <strong style="font-size: 20px;">$299/month</strong><br>
                <span style="font-size: 14px; color: #9a9aaa;">Billed monthly. Cancel anytime.</span><br><br>
                <a href="{app_url}/pricing.html" class="cta-button" style="background: #00d4ff;">Upgrade to Pro →</a>
            </p>
            
            <p style="margin-top: 24px;">Have questions about which plan is right for you? Hit reply and let's chat.</p>
        """
    },
    
    5: {
        "name": "welcome_5_final",
        "subject": "Your DC Hub trial is ending – one last thing",
        "delay_hours": 336,  # 14 days
        "content": """
            <h1>Thanks for trying DC Hub</h1>
            <p>Hi {name},</p>
            <p>It's been two weeks since you joined DC Hub. Whether you're ready to upgrade or still exploring, I wanted to personally thank you for being part of our community.</p>
            
            <div class="divider"></div>
            
            <h2>Quick recap of what you can do:</h2>
            <ul style="margin: 16px 0 24px 24px; color: #4a4a5a;">
                <li style="margin-bottom: 8px;">Search 20,000+ data centers across 140+ countries</li>
                <li style="margin-bottom: 8px;">Compare markets with real power and fiber data</li>
                <li style="margin-bottom: 8px;">Generate professional PDF reports</li>
                <li style="margin-bottom: 8px;">Use AI agents for sales research and content</li>
                <li style="margin-bottom: 8px;">Access the industry's most comprehensive vendor directory</li>
            </ul>
            
            <div class="divider"></div>
            
            <p>If DC Hub isn't the right fit, no hard feelings. But if budget is the only thing holding you back, reply to this email – I may be able to offer something special for early adopters.</p>
            
            <p style="text-align: center;">
                <a href="{app_url}/pricing.html" class="cta-button">View Pricing Options →</a>
            </p>
            
            <p>Either way, I'd love to hear your feedback. What would make DC Hub more valuable for your work?</p>
            
            <p>Thanks again,</p>
            <p>— Jonathan<br><span style="color: #6a6a7a;">Founder, DC Hub</span></p>
            
            <p style="font-size: 14px; color: #9a9aaa; margin-top: 24px;">P.S. This is the last email in our welcome series. You'll only hear from us with product updates and occasional tips unless you upgrade.</p>
        """
    }
}


# =============================================================================
# EMAIL SENDING FUNCTIONS
# =============================================================================

def send_email(to_email: str, subject: str, html_content: str, text_content: str = None) -> dict:
    """Send a single email via Office 365 SMTP"""
    
    if not SMTP_USER or not SMTP_PASSWORD:
        return {
            'success': False,
            'error': 'SMTP credentials not configured. Set SMTP_USER and SMTP_PASSWORD environment variables.'
        }
    
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg['To'] = to_email
        
        # Plain text fallback
        if not text_content:
            # Strip HTML for plain text version
            import re
            text_content = re.sub(r'<[^>]+>', '', html_content)
            text_content = re.sub(r'\s+', ' ', text_content).strip()
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        
        msg.attach(part1)
        msg.attach(part2)
        
        # Connect and send
        context = ssl.create_default_context()
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_string())
        
        return {'success': True, 'message_id': secrets.token_hex(16)}
        
    except smtplib.SMTPAuthenticationError as e:
        return {'success': False, 'error': f'SMTP Authentication failed: {str(e)}'}
    except smtplib.SMTPException as e:
        return {'success': False, 'error': f'SMTP error: {str(e)}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def render_email_template(template_content: str, variables: dict) -> str:
    """Render email template with variables"""
    base_template = get_email_base_template()
    
    # Generate tracking pixel if enabled
    tracking_pixel = ""
    if TRACKING_ENABLED and variables.get('email_id'):
        tracking_pixel = f'<img src="{APP_URL}/api/email/track/{variables["email_id"]}/open.gif" width="1" height="1" style="display:none;" />'
    
    # Merge content into base template
    full_html = base_template.format(
        subject=variables.get('subject', ''),
        content=template_content,
        app_url=APP_URL,
        unsubscribe_token=variables.get('unsubscribe_token', ''),
        tracking_pixel=tracking_pixel
    )
    
    # Replace remaining variables
    for key, value in variables.items():
        full_html = full_html.replace('{' + key + '}', str(value))
    
    return full_html


# =============================================================================
# WELCOME SERIES FUNCTIONS
# =============================================================================

def start_welcome_series(user_id: str, email: str, name: str = "there"):
    """Start the welcome series for a new user"""
    conn = get_db()
    c = conn.cursor()
    
    # Check if already in welcome series
    c.execute("SELECT id FROM welcome_series WHERE email = ?", (email,))
    if c.fetchone():
        conn.close()
        return {'success': False, 'error': 'Already in welcome series'}
    
    # Create welcome series record
    series_id = secrets.token_hex(8)
    unsubscribe_token = secrets.token_urlsafe(32)
    
    c.execute("""
        INSERT INTO welcome_series (id, user_id, email, current_step, started_at, status)
        VALUES (?, ?, ?, 0, ?, 'active')
    """, (series_id, user_id, email, datetime.utcnow().isoformat()))
    
    # Schedule all emails in the series
    now = datetime.utcnow()
    
    for step, template in WELCOME_SERIES_TEMPLATES.items():
        email_id = secrets.token_hex(8)
        scheduled_at = now + timedelta(hours=template['delay_hours'])
        
        # Render the email content
        variables = {
            'name': name.split()[0] if name else 'there',  # First name only
            'email': email,
            'app_url': APP_URL,
            'email_id': email_id,
            'unsubscribe_token': unsubscribe_token,
            'subject': template['subject']
        }
        
        html_content = render_email_template(template['content'], variables)
        
        c.execute("""
            INSERT INTO email_queue 
            (id, user_id, email, template_name, subject, body_html, scheduled_at, status, sequence_id, sequence_step, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
        """, (
            email_id,
            user_id,
            email,
            template['name'],
            template['subject'],
            html_content,
            scheduled_at.isoformat(),
            series_id,
            step,
            datetime.utcnow().isoformat()
        ))
    
    conn.commit()
    conn.close()
    
    # Immediately send the first email
    process_email_queue()
    
    return {'success': True, 'series_id': series_id, 'emails_scheduled': len(WELCOME_SERIES_TEMPLATES)}


def stop_welcome_series(email: str):
    """Stop welcome series for a user (e.g., on unsubscribe or upgrade)"""
    conn = get_db()
    c = conn.cursor()
    
    # Mark series as completed/stopped
    c.execute("""
        UPDATE welcome_series SET status = 'stopped', completed_at = ?
        WHERE email = ? AND status = 'active'
    """, (datetime.utcnow().isoformat(), email))
    
    # Cancel pending emails
    c.execute("""
        UPDATE email_queue SET status = 'cancelled'
        WHERE email = ? AND status = 'scheduled' AND sequence_id IS NOT NULL
    """, (email,))
    
    conn.commit()
    conn.close()
    
    return {'success': True}


# =============================================================================
# EMAIL QUEUE PROCESSOR
# =============================================================================

def process_email_queue():
    """Process pending emails in the queue"""
    conn = get_db()
    c = conn.cursor()
    
    # Get emails that are due to be sent
    now = datetime.utcnow().isoformat()
    
    c.execute("""
        SELECT id, email, subject, body_html, body_text, retry_count, sequence_step
        FROM email_queue
        WHERE status = 'scheduled' AND scheduled_at <= ?
        ORDER BY scheduled_at ASC
        LIMIT 10
    """, (now,))
    
    emails = c.fetchall()
    results = []
    
    for email_row in emails:
        email_id, to_email, subject, html_content, text_content, retry_count, step = email_row
        
        # Check if user has unsubscribed
        c.execute("SELECT subscribed FROM leads WHERE email = ?", (to_email,))
        lead = c.fetchone()
        if lead and not lead[0]:
            c.execute("UPDATE email_queue SET status = 'cancelled', error = 'User unsubscribed' WHERE id = ?", (email_id,))
            conn.commit()
            continue
        
        # Send the email
        result = send_email(to_email, subject, html_content, text_content)
        
        if result['success']:
            c.execute("""
                UPDATE email_queue SET status = 'sent', sent_at = ? WHERE id = ?
            """, (datetime.utcnow().isoformat(), email_id))
            
            # Update welcome series progress
            if step:
                c.execute("""
                    UPDATE welcome_series 
                    SET current_step = ?, last_email_sent = ?
                    WHERE email = ?
                """, (step, datetime.utcnow().isoformat(), to_email))
            
            results.append({'email_id': email_id, 'success': True})
        else:
            # Handle failure with retry logic
            if retry_count < 3:
                c.execute("""
                    UPDATE email_queue 
                    SET retry_count = retry_count + 1, 
                        scheduled_at = ?,
                        error = ?
                    WHERE id = ?
                """, (
                    (datetime.utcnow() + timedelta(hours=1)).isoformat(),
                    result['error'],
                    email_id
                ))
            else:
                c.execute("""
                    UPDATE email_queue SET status = 'failed', error = ? WHERE id = ?
                """, (result['error'], email_id))
            
            results.append({'email_id': email_id, 'success': False, 'error': result['error']})
        
        conn.commit()
    
    conn.close()
    return results


def get_email_stats():
    """Get email sending statistics"""
    conn = get_db()
    c = conn.cursor()
    
    stats = {}
    
    # Queue status counts
    c.execute("""
        SELECT status, COUNT(*) FROM email_queue GROUP BY status
    """)
    stats['queue'] = {row[0]: row[1] for row in c.fetchall()}
    
    # Welcome series stats
    c.execute("""
        SELECT status, COUNT(*) FROM welcome_series GROUP BY status
    """)
    stats['welcome_series'] = {row[0]: row[1] for row in c.fetchall()}
    
    # Recent sends (last 24h)
    yesterday = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    c.execute("""
        SELECT COUNT(*) FROM email_queue WHERE sent_at >= ?
    """, (yesterday,))
    stats['sent_24h'] = c.fetchone()[0]
    
    # Open tracking (if implemented)
    c.execute("""
        SELECT event_type, COUNT(*) FROM email_tracking 
        WHERE created_at >= ? GROUP BY event_type
    """, (yesterday,))
    stats['tracking_24h'] = {row[0]: row[1] for row in c.fetchall()}
    
    conn.close()
    return stats


# =============================================================================
# BACKGROUND WORKER
# =============================================================================

class EmailWorker:
    """Background worker to process email queue"""
    
    def __init__(self, interval_seconds: int = 60):
        self.interval = interval_seconds
        self.running = False
        self.thread = None
    
    def start(self):
        """Start the background worker"""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print(f"📧 Email worker started (checking every {self.interval}s)")
    
    def stop(self):
        """Stop the background worker"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
    
    def _run(self):
        """Main worker loop"""
        while self.running:
            try:
                results = process_email_queue()
                if results:
                    sent = sum(1 for r in results if r.get('success'))
                    failed = len(results) - sent
                    if sent or failed:
                        print(f"📧 Processed {len(results)} emails: {sent} sent, {failed} failed")
            except Exception as e:
                print(f"❌ Email worker error: {e}")
            
            time.sleep(self.interval)


# Global worker instance
email_worker = EmailWorker(interval_seconds=60)


# =============================================================================
# API HELPER FUNCTIONS (to be used by main server)
# =============================================================================

def handle_new_signup(user_id: str, email: str, name: str, source: str = 'registration'):
    """Handle new user signup - trigger welcome series"""
    
    # Initialize tables if needed
    init_email_tables()
    
    # Start welcome series
    result = start_welcome_series(user_id, email, name)
    
    return result


def handle_unsubscribe(token: str) -> dict:
    """Handle email unsubscribe"""
    conn = get_db()
    c = conn.cursor()
    
    # Find user by unsubscribe token in email queue
    # In production, you'd want a separate unsubscribe_tokens table
    c.execute("""
        UPDATE leads SET subscribed = 0 WHERE email IN (
            SELECT DISTINCT email FROM email_queue WHERE body_html LIKE ?
        )
    """, (f'%{token}%',))
    
    # Stop welcome series
    c.execute("""
        SELECT DISTINCT email FROM email_queue WHERE body_html LIKE ?
    """, (f'%{token}%',))
    
    result = c.fetchone()
    if result:
        stop_welcome_series(result[0])
    
    conn.commit()
    conn.close()
    
    return {'success': True}


def record_email_event(email_id: str, event_type: str, ip: str = None, user_agent: str = None):
    """Record email tracking event (open, click, etc.)"""
    conn = get_db()
    c = conn.cursor()
    
    # Get email info
    c.execute("SELECT email FROM email_queue WHERE id = ?", (email_id,))
    row = c.fetchone()
    email = row[0] if row else None
    
    c.execute("""
        INSERT INTO email_tracking (id, email_id, email, event_type, ip_address, user_agent, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        secrets.token_hex(8),
        email_id,
        email,
        event_type,
        ip,
        user_agent,
        datetime.utcnow().isoformat()
    ))
    
    conn.commit()
    conn.close()


# Initialize tables when module loads
init_email_tables()
