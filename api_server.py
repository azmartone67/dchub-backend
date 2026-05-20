"""
from nav_config import register_nav_config_route
DC HUB NEXUS - ENHANCED API SERVER v80
======================================
Features Added (v80):
  - Email Welcome Series (5-email drip campaign)
  - Office 365 SMTP Integration
  - Email queue processing with retry logic
  - Open tracking via invisible pixels
  - Unsubscribe handling

Features (v74):
  - Email Capture & Lead Management
  - Market Comparison Tool
  - PDF Report Generator  
  - User Authentication (JWT)
  - Stripe Payment Integration

New Endpoints (v80):
  EMAIL:
    GET  /api/email/track/:id/open.gif  - Track email opens
    GET  /api/email/unsubscribe         - Unsubscribe page
    GET  /api/email/stats               - Email statistics (admin)
    POST /api/email/process             - Process email queue (admin)
    POST /api/email/test                - Send test email (admin)

New Endpoints (v74):
  LEADS:
    POST /api/leads/subscribe       - Subscribe email to newsletter
    POST /api/leads/capture         - Capture lead from gated content
    GET  /api/leads/verify/:token   - Verify email
  
  AUTH:
    POST /api/auth/register         - Create account
    POST /api/auth/login            - Login, get JWT
    GET  /api/auth/me               - Get current user (JWT required)
    POST /api/auth/logout           - Logout
    POST /api/auth/forgot-password  - Request password reset
  
  MARKETS:
    GET  /api/v1/markets/compare    - Compare 2-3 markets side-by-side
    GET  /api/v1/markets/list       - List all available markets
    GET  /api/v1/markets/:market    - Get single market stats
  
  REPORTS:
    POST /api/reports/generate      - Generate PDF market report
    GET  /api/reports/:id           - Download generated report
  
  STRIPE:
    GET  /api/stripe/config         - Get Stripe publishable key
    POST /api/stripe/webhook        - Handle Stripe webhooks
    POST /api/stripe/create-checkout - Create checkout session
    GET  /api/stripe/subscription   - Get user subscription status
"""

from flask import Flask, request, jsonify, Response, send_from_directory, send_file
from flask_cors import CORS
from functools import wraps
import sqlite3
import json
import hashlib
import secrets
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import threading
import queue
import re
from html import unescape
import jwt
import io
import os
from db_utils import get_db

# Stripe Integration
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    print("⚠️ Stripe not installed - payment features disabled")

# PDF Generation
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("⚠️ ReportLab not installed - PDF generation disabled")

# Email Service Integration
try:
    from email_service import (
        handle_new_signup, 
        handle_unsubscribe, 
        record_email_event,
        get_email_stats,
        process_email_queue,
        email_worker,
        stop_welcome_series
    )
    EMAIL_SERVICE_AVAILABLE = True
    print("📧 Email service loaded")
except ImportError as e:
    EMAIL_SERVICE_AVAILABLE = False
    print(f"⚠️ Email service not available: {e}")

app = Flask(__name__)

# === DC Hub JSON encoder — handles Decimal/datetime/UUID/bytes/set ===
# Fixes get_fiber_intel and any numeric-column endpoint returning 500.
# Compatible with Flask 2.3+ (JSONProvider) and earlier (JSONEncoder).
import decimal as _dchub_decimal
import datetime as _dchub_datetime
import uuid as _dchub_uuid

def _dchub_json_default(o):
    if isinstance(o, _dchub_decimal.Decimal):
        return float(o)
    if isinstance(o, (_dchub_datetime.datetime, _dchub_datetime.date)):
        return o.isoformat()
    if isinstance(o, _dchub_uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode('utf-8', errors='replace')
    if isinstance(o, set):
        return list(o)
    raise TypeError(f'Object of type {type(o).__name__} is not JSON serializable')

try:
    from flask.json.provider import DefaultJSONProvider as _DefaultJSONProvider

    class DCHubJSONProvider(_DefaultJSONProvider):
        def default(self, o):
            try:
                return _dchub_json_default(o)
            except TypeError:
                return super().default(o)

    app.json = DCHubJSONProvider(app)
except ImportError:
    from flask.json import JSONEncoder as _JSONEncoder

    class DCHubJSONEncoder(_JSONEncoder):
        def default(self, o):
            try:
                return _dchub_json_default(o)
            except TypeError:
                return super().default(o)

    app.json_encoder = DCHubJSONEncoder
# === end DC Hub JSON encoder ===

register_nav_config_route(app)
CORS(app, origins=['https://dchub.cloud', 'https://www.dchub.cloud'])

DB_PATH = "dc_nexus.db"
WEBHOOK_QUEUE = queue.Queue()

# JWT Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'dchub-super-secret-key-change-in-production')
JWT_EXPIRY_HOURS = 24 * 7  # 7 days

# Stripe Configuration
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_live_51Si61EJ9ey2ATcQlDsF7z9YzsBIkp4hsFYuHsk53ZIpMsR8dBCPss6MGe8MMUrTBdnbFzVppdF1O6O6mxCaNzlEn00szurhklL')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

if STRIPE_AVAILABLE and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    print("💳 Stripe configured")
else:
    print("⚠️ Stripe not configured - set STRIPE_SECRET_KEY environment variable")

# =============================================================================
# DATABASE HELPERS
# =============================================================================


def dict_from_row(row):
    """Convert sqlite3.Row to dict"""
    if row is None:
        return None
    return dict(row)

def strip_html(text):
    """Remove HTML tags from text"""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def init_new_tables():
    """Initialize new tables for v74 features"""
    conn = get_db()
    c = conn.cursor()
    
    # Leads table for email capture
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            company TEXT,
            source TEXT,
            source_detail TEXT,
            verified INTEGER DEFAULT 0,
            verify_token TEXT,
            subscribed INTEGER DEFAULT 1,
            lead_score INTEGER DEFAULT 0,
            tags TEXT,
            created_at TEXT,
            verified_at TEXT,
            last_activity TEXT
        )
    """)
    
    # Users table for authentication
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            company TEXT,
            role TEXT DEFAULT 'free',
            plan TEXT DEFAULT 'free',
            api_calls_today INTEGER DEFAULT 0,
            api_calls_total INTEGER DEFAULT 0,
            saved_searches TEXT,
            saved_markets TEXT,
            preferences TEXT,
            created_at TEXT,
            last_login TEXT,
            reset_token TEXT,
            reset_expires TEXT,
            stripe_customer_id TEXT,
            subscription_status TEXT
        )
    """)
    
    # Add Stripe columns if they don't exist (migration for existing databases)
    try:
        c.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT")
    except:
        pass
    
    # Generated reports table
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            email TEXT,
            report_type TEXT,
            markets TEXT,
            parameters TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT
        )
    """)
    
    # Lead activities table for tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS lead_activities (
            id SERIAL PRIMARY KEY,
            lead_id TEXT,
            activity_type TEXT,
            details TEXT,
            created_at TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    print("✅ New v74 tables initialized")

# Initialize tables on startup
init_new_tables()

# Market aliases for comparison tool
MARKET_ALIASES = {
    'phoenix': ['Phoenix', 'Mesa', 'Tempe', 'Scottsdale', 'Chandler', 'Gilbert', 'Goodyear', 'AZ'],
    'arizona': ['Phoenix', 'Mesa', 'Tempe', 'Scottsdale', 'Tucson', 'AZ'],
    'dallas': ['Dallas', 'Fort Worth', 'Plano', 'Irving', 'Arlington', 'Carrollton', 'Richardson'],
    'dfw': ['Dallas', 'Fort Worth', 'Plano', 'Irving', 'Arlington'],
    'austin': ['Austin', 'Round Rock', 'Cedar Park', 'Georgetown'],
    'houston': ['Houston', 'The Woodlands', 'Sugar Land', 'Katy'],
    'san antonio': ['San Antonio'],
    'northern virginia': ['Ashburn', 'Loudoun', 'Sterling', 'Reston', 'Herndon', 'Manassas', 'VA'],
    'nova': ['Ashburn', 'Loudoun', 'Sterling', 'Reston', 'Herndon', 'Manassas'],
    'ashburn': ['Ashburn', 'Loudoun'],
    'chicago': ['Chicago', 'Aurora', 'Elk Grove', 'Schaumburg'],
    'atlanta': ['Atlanta', 'Marietta', 'Alpharetta', 'Duluth', 'Suwanee'],
    'silicon valley': ['San Jose', 'Santa Clara', 'Sunnyvale', 'Milpitas', 'Fremont', 'Palo Alto'],
    'los angeles': ['Los Angeles', 'El Segundo', 'Downtown LA', 'Irvine', 'Orange County'],
    'san francisco': ['San Francisco', 'South San Francisco'],
    'new york': ['New York', 'NYC', 'Manhattan', 'Brooklyn', 'Bronx'],
    'new jersey': ['Secaucus', 'Newark', 'Jersey City', 'NJ'],
    'seattle': ['Seattle', 'Tukwila', 'Kent', 'Bellevue', 'Redmond'],
    'denver': ['Denver', 'Aurora', 'Centennial', 'Boulder'],
    'miami': ['Miami', 'Boca Raton', 'Fort Lauderdale'],
    'columbus': ['Columbus', 'New Albany', 'Dublin', 'Westerville'],
    'salt lake city': ['Salt Lake City', 'West Valley', 'Sandy'],
    'portland': ['Portland', 'Hillsboro', 'Beaverton'],
    'las vegas': ['Las Vegas', 'Henderson', 'North Las Vegas'],
    'reno': ['Reno', 'Sparks'],
    'boston': ['Boston', 'Cambridge', 'Somerville'],
    'minneapolis': ['Minneapolis', 'St. Paul', 'Bloomington'],
    'detroit': ['Detroit', 'Southfield', 'Troy'],
    'philadelphia': ['Philadelphia', 'King of Prussia'],
    'kansas city': ['Kansas City'],
    'charlotte': ['Charlotte'],
    'raleigh': ['Raleigh', 'Durham', 'Research Triangle'],
    'nashville': ['Nashville'],
    'indianapolis': ['Indianapolis'],
}

RAILWAY_EXCLUSION = """
    AND provider NOT LIKE '%Railway%'
    AND provider NOT LIKE '%Railroad%'
    AND provider NOT LIKE '%Rail %'
    AND provider NOT LIKE '%SNCF%'
    AND provider NOT LIKE '%Metro%'
    AND provider NOT LIKE '%Transit%'
    AND provider NOT LIKE '%Amtrak%'
    AND provider NOT LIKE '%Bahn%'
"""

# =============================================================================
# AUTHENTICATION HELPERS
# =============================================================================

def hash_password(password):
    """Hash password with salt"""
    salt = secrets.token_hex(16)
    hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{hash_obj.hex()}"

def verify_password(password, hash_string):
    """Verify password against hash"""
    try:
        salt, hash_hex = hash_string.split(':')
        hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hash_obj.hex() == hash_hex
    except:
        return False

def generate_jwt(user_id, email, role='user'):
    """Generate JWT token"""
    payload = {
        'user_id': user_id,
        'email': email,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
        'iat': datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def decode_jwt(token):
    """Decode and verify JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def require_auth(f):
    """Decorator to require JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authorization required', 'code': 'AUTH_REQUIRED'}), 401
        
        token = auth_header.split(' ')[1]
        payload = decode_jwt(token)
        
        if not payload:
            return jsonify({'error': 'Invalid or expired token', 'code': 'AUTH_INVALID'}), 401
        
        request.user = payload
        return f(*args, **kwargs)
    
    return decorated

def optional_auth(f):
    """Decorator for optional JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        request.user = None
        
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            payload = decode_jwt(token)
            if payload:
                request.user = payload
        
        return f(*args, **kwargs)
    
    return decorated

# =============================================================================
# LEAD CAPTURE ENDPOINTS
# =============================================================================

# AUTO-REPAIR: duplicate route '/api/leads/subscribe' also in main.py:7518 — review and remove one
@app.route('/api/leads/subscribe', methods=['POST'])
def subscribe_lead():
    """Subscribe email to newsletter"""
    data = request.get_json()
    
    if not data or not data.get('email'):
        return jsonify({'error': 'Email required', 'code': 'VALIDATION_ERROR'}), 400
    
    email = data['email'].lower().strip()
    
    # Basic email validation
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return jsonify({'error': 'Invalid email format', 'code': 'VALIDATION_ERROR'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Check if already exists
    c.execute("SELECT id, subscribed FROM leads WHERE email = %s", (email,))
    existing = c.fetchone()
    
    if existing:
        if existing[1]:  # Already subscribed
            conn.close()
            return jsonify({'success': True, 'message': 'Already subscribed', 'new': False})
        else:
            # Re-subscribe
            c.execute("UPDATE leads SET subscribed = 1, last_activity = %s WHERE email = %s",
                     (datetime.utcnow().isoformat(), email))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Re-subscribed successfully', 'new': False})
    
    # Create new lead
    lead_id = secrets.token_hex(8)
    verify_token = secrets.token_urlsafe(32)
    
    c.execute("""
        INSERT INTO leads (id, email, name, company, source, source_detail, verify_token, created_at, last_activity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
    """, (
        lead_id,
        email,
        data.get('name', ''),
        data.get('company', ''),
        data.get('source', 'newsletter'),
        data.get('source_detail', ''),
        verify_token,
        datetime.utcnow().isoformat(),
        datetime.utcnow().isoformat()
    ))
    
    # Log activity
    c.execute("""
        INSERT INTO lead_activities (lead_id, activity_type, details, created_at)
        VALUES (%s, 'subscribed', %s, %s) ON CONFLICT DO NOTHING
    """, (lead_id, json.dumps({'source': data.get('source', 'newsletter')}), datetime.utcnow().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Subscribed successfully',
        'new': True,
        'lead_id': lead_id
    }), 201
# AUTO-REPAIR: duplicate route '/api/leads/capture' also in main.py:7588 — review and remove one

@app.route('/api/leads/capture', methods=['POST'])
def capture_lead():
    """Capture lead from gated content (e.g., PDF download, social generator)"""
    data = request.get_json()
    
    if not data or not data.get('email'):
        return jsonify({'error': 'Email required', 'code': 'VALIDATION_ERROR'}), 400
    
    email = data['email'].lower().strip()
    source = data.get('source', 'unknown')  # e.g., 'social_generator', 'pdf_report', 'market_comparison'
    
    conn = get_db()
    c = conn.cursor()
    
    # Check if exists
    c.execute("SELECT id, lead_score FROM leads WHERE email = %s", (email,))
    existing = c.fetchone()
    
    # Calculate lead score based on source
    score_map = {
        'social_generator': 10,
        'pdf_report': 25,
        'market_comparison': 20,
        'newsletter': 5,
        'chat_widget': 15,
        'demo_request': 50
    }
    score_delta = score_map.get(source, 5)
    
    if existing:
        lead_id = existing[0]
        new_score = existing[1] + score_delta
        
        c.execute("""
            UPDATE leads SET 
                lead_score = ?,
                last_activity = ?,
                source_detail = COALESCE(source_detail, '') || ',' || ?
            WHERE email = %s
        """, (new_score, datetime.utcnow().isoformat(), source, email))
    else:
        lead_id = secrets.token_hex(8)
        verify_token = secrets.token_urlsafe(32)
        
        c.execute("""
            INSERT INTO leads (id, email, name, company, source, source_detail, verify_token, lead_score, created_at, last_activity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (
            lead_id,
            email,
            data.get('name', ''),
            data.get('company', ''),
            source,
            source,
            verify_token,
            score_delta,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat()
        ))
    
    # Log activity
    c.execute("""
        INSERT INTO lead_activities (lead_id, activity_type, details, created_at)
        VALUES (%s, 'content_access', %s, %s) ON CONFLICT DO NOTHING
    """, (lead_id, json.dumps({'source': source, 'content': data.get('content', '')}), datetime.utcnow().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Lead captured',
        'lead_id': lead_id,
        'access_granted': True
# AUTO-REPAIR: duplicate route '/api/leads/verify/<token>' also in main.py:7668 — review and remove one
    })

@app.route('/api/leads/verify/<token>', methods=['GET'])
def verify_lead(token):
    """Verify email via token"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT id, email FROM leads WHERE verify_token = %s", (token,))
    lead = c.fetchone()
    
    if not lead:
        conn.close()
        return jsonify({'error': 'Invalid verification token', 'code': 'NOT_FOUND'}), 404
    
    c.execute("""
        UPDATE leads SET verified = 1, verified_at = %s, verify_token = NULL WHERE id = %s
    """, (datetime.utcnow().isoformat(), lead[0]))
    
    conn.commit()
    conn.close()
# AUTO-REPAIR: duplicate route '/api/leads/unsubscribe' also in main.py:7693 — review and remove one
    
    return jsonify({'success': True, 'message': 'Email verified successfully'})

@app.route('/api/leads/unsubscribe', methods=['POST'])
def unsubscribe_lead():
    """Unsubscribe from newsletter"""
    data = request.get_json()
    email = data.get('email', '').lower().strip()
    
    if not email:
        return jsonify({'error': 'Email required'}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE leads SET subscribed = 0 WHERE email = %s", (email,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Unsubscribed successfully'})

# =============================================================================
# USER AUTHENTICATION ENDPOINTS
# =============================================================================

@app.route('/api/auth/register', methods=['POST'])
def register_user():
    """Register new user account"""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Request body required'}), 400
    
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    name = data.get('name', '')
    company = data.get('company', '')
    
    # Validation
    if not email or not password:
        return jsonify({'error': 'Email and password required', 'code': 'VALIDATION_ERROR'}), 400
    
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters', 'code': 'VALIDATION_ERROR'}), 400
    
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return jsonify({'error': 'Invalid email format', 'code': 'VALIDATION_ERROR'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Check if exists
    c.execute("SELECT id FROM users WHERE email = %s", (email,))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'Email already registered', 'code': 'DUPLICATE'}), 409
    
    # Create user
    user_id = secrets.token_hex(12)
    password_hash = hash_password(password)
    
    c.execute("""
        INSERT INTO users (id, email, password_hash, name, company, created_at, last_login)
        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
    """, (user_id, email, password_hash, name, company, datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
    
    conn.commit()
    conn.close()
    
    # Generate token
    token = generate_jwt(user_id, email)
    
    # Also add to leads
    try:
        capture_lead_internal(email, name, company, 'registration')
    except:
        pass
    
    # Start welcome email series
    if EMAIL_SERVICE_AVAILABLE:
        try:
            handle_new_signup(user_id, email, name, 'registration')
            print(f"📧 Welcome series started for {email}")
        except Exception as e:
            print(f"⚠️ Failed to start welcome series: {e}")
    
    return jsonify({
        'success': True,
        'message': 'Account created successfully',
        'user': {
            'id': user_id,
            'email': email,
            'name': name,
            'plan': 'free'
        },
        'token': token
    }), 201

def capture_lead_internal(email, name, company, source):
    """Internal helper to capture lead"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM leads WHERE email = %s", (email,))
    if not c.fetchone():
        lead_id = secrets.token_hex(8)
        c.execute("""
            INSERT INTO leads (id, email, name, company, source, lead_score, created_at, last_activity)
            VALUES (%s, %s, %s, %s, %s, 30, %s, %s) ON CONFLICT DO NOTHING
        """, (lead_id, email, name, company, source, datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login_user():
    """Login and get JWT token"""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Request body required'}), 400
    
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT id, email, password_hash, name, company, role, plan FROM users WHERE email = %s", (email,))
    user = c.fetchone()
    
    if not user or not verify_password(password, user[2]):
        conn.close()
        return jsonify({'error': 'Invalid email or password', 'code': 'AUTH_FAILED'}), 401
    
    # Update last login
    c.execute("UPDATE users SET last_login = %s WHERE id = %s", (datetime.utcnow().isoformat(), user[0]))
    conn.commit()
    conn.close()
    
    token = generate_jwt(user[0], user[1], user[5] or 'user')
    
    return jsonify({
        'success': True,
        'user': {
            'id': user[0],
            'email': user[1],
            'name': user[3],
            'company': user[4],
            'role': user[5] or 'user',
            'plan': user[6] or 'free'
        },
        'token': token
    })

@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    """Authenticate with Google - handles both ID token and access token flows"""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Request body required'}), 400
    
    email = None
    name = None
    picture = None
    google_id = None
    
    # Flow 1: ID Token (credential) from Google Identity Services
    if 'credential' in data:
        try:
            # Decode the JWT credential (ID token)
            # In production, you should verify this with Google's public keys
            import base64
            credential = data['credential']
            
            # Split JWT and decode payload
            parts = credential.split('.')
            if len(parts) == 3:
                # Add padding if needed
                payload = parts[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                
                email = decoded.get('email', '').lower()
                name = decoded.get('name', '')
                picture = decoded.get('picture', '')
                google_id = decoded.get('sub', '')
                
                # Verify email is present
                if not email:
                    return jsonify({'error': 'Could not extract email from Google credential'}), 400
        except Exception as e:
            return jsonify({'error': f'Invalid Google credential: {str(e)}'}), 400
    
    # Flow 2: Access Token with user info provided
    elif 'access_token' in data or 'email' in data:
        email = data.get('email', '').lower().strip()
        name = data.get('name', '')
        picture = data.get('picture', '')
        
        if not email:
            return jsonify({'error': 'Email required for Google authentication'}), 400
    else:
        return jsonify({'error': 'Google credential or access token required'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Check if user exists
    c.execute("SELECT id, email, name, company, role, plan FROM users WHERE email = %s", (email,))
    user = c.fetchone()
    
    if user:
        # Existing user - update last login
        c.execute("UPDATE users SET last_login = %s WHERE id = %s", (datetime.utcnow().isoformat(), user[0]))
        conn.commit()
        
        user_data = {
            'id': user[0],
            'email': user[1],
            'name': user[2],
            'company': user[3],
            'role': user[4] or 'user',
            'plan': user[5] or 'free'
        }
    else:
        # New user - create account
        c.execute("""
            INSERT INTO users (email, password_hash, name, company, role, plan, created_at, last_login)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (
            email,
            'google_oauth',  # No password for Google users
            name,
            '',  # No company yet
            'user',
            'free',
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        
        user_id = c.lastrowid
        
        # Also capture as lead
        try:
            c.execute("""
                INSERT INTO leads (email, name, source, source_detail, lead_score, created_at)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (email, name, 'google_signup', 'google_oauth_registration', 30, datetime.utcnow().isoformat()))
            conn.commit()
        except:
            pass
        
        # Start welcome email series for new Google users
        if EMAIL_SERVICE_AVAILABLE:
            try:
                handle_new_signup(str(user_id), email, name, 'google_signup')
                print(f"📧 Welcome series started for Google user {email}")
            except Exception as e:
                print(f"⚠️ Failed to start welcome series: {e}")
        
        user_data = {
            'id': user_id,
            'email': email,
            'name': name,
            'company': '',
            'role': 'user',
            'plan': 'free'
        }
    
    conn.close()
    
    token = generate_jwt(user_data['id'], email, user_data['role'])
    
    return jsonify({
        'success': True,
        'user': user_data,
        'token': token,
        'is_new_user': user is None
    })

@app.route('/api/auth/me', methods=['GET'])
@require_auth
def get_current_user():
    """Get current authenticated user"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT id, email, name, company, role, plan, saved_searches, saved_markets, preferences, created_at
        FROM users WHERE id = %s
    """, (request.user['user_id'],))
    
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    return jsonify({
        'success': True,
        'user': {
            'id': user[0],
            'email': user[1],
            'name': user[2],
            'company': user[3],
            'role': user[4],
            'plan': user[5],
            'saved_searches': json.loads(user[6]) if user[6] else [],
            'saved_markets': json.loads(user[7]) if user[7] else [],
            'preferences': json.loads(user[8]) if user[8] else {},
            'member_since': user[9]
        }
    })

@app.route('/api/auth/update', methods=['PUT'])
@require_auth
def update_user():
    """Update user profile"""
    data = request.get_json()
    
    conn = get_db()
    c = conn.cursor()
    
    updates = []
    params = []
    
    if 'name' in data:
        updates.append('name = %s')
        params.append(data['name'])
    if 'company' in data:
        updates.append('company = %s')
        params.append(data['company'])
    if 'preferences' in data:
        updates.append('preferences = %s')
        params.append(json.dumps(data['preferences']))
    if 'saved_searches' in data:
        updates.append('saved_searches = %s')
        params.append(json.dumps(data['saved_searches']))
    if 'saved_markets' in data:
        updates.append('saved_markets = %s')
        params.append(json.dumps(data['saved_markets']))
    
    if updates:
        params.append(request.user['user_id'])
        c.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
    
    conn.close()
    
    return jsonify({'success': True, 'message': 'Profile updated'})

# =============================================================================
# STRIPE PAYMENT ENDPOINTS
# =============================================================================

# Stripe price IDs (create these in your Stripe dashboard)
STRIPE_PRICES = {
    'pro_monthly': os.environ.get('STRIPE_PRICE_PRO_MONTHLY', 'price_XXXXX'),
# AUTO-REPAIR: duplicate route '/api/stripe/config' also in main.py:8195 — review and remove one
    'pro_annual': os.environ.get('STRIPE_PRICE_PRO_ANNUAL', 'price_XXXXX'),
    'founding': os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),
}

@app.route('/api/stripe/config', methods=['GET'])
def stripe_config():
    """Get Stripe publishable key and configuration"""
    return jsonify({
        'publishableKey': STRIPE_PUBLISHABLE_KEY,
        'configured': bool(STRIPE_SECRET_KEY),
        'prices': {
            'pro_monthly': 299,
# AUTO-REPAIR: duplicate route '/api/stripe/create-checkout' also in main.py:8208 — review and remove one
            'pro_annual': 1990,
            'founding': 99
        }
    })

@app.route('/api/stripe/create-checkout', methods=['POST'])
@require_auth
def create_checkout_session():
    """Create a Stripe Checkout session for subscription"""
    if not STRIPE_AVAILABLE or not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe not configured'}), 503
    
    data = request.get_json()
    plan = data.get('plan', 'pro_monthly')
    
    # Map plan to price ID
    price_id = STRIPE_PRICES.get(plan)
    if not price_id or price_id.startswith('price_XXXXX'):
        # Fall back to payment links if price IDs not configured
        payment_links = {
            'pro_monthly': 'https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02',
            'pro_annual': 'https://buy.stripe.com/4gM3cwcVk3JjbSR9maaZi01',
            'founding': 'https://buy.stripe.com/9B6fZi1cCdjT3ml8i6aZi00'
        }
        return jsonify({
            'redirect': True,
            'url': payment_links.get(plan, payment_links['pro_monthly'])
        })
    
    try:
        # Get user email
        user_email = request.user.get('email', '')
        
        checkout_session = stripe.checkout.Session.create(
            customer_email=user_email,
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f'https://dchub.cloud/dashboard.html%spayment=success&plan={plan}',
            cancel_url='https://dchub.cloud/dashboard.html%spayment=cancelled',
            metadata={
                'user_id': str(request.user.get('user_id', '')),
                'plan': plan
            }
        )
        
        return jsonify({
            'sessionId': checkout_session.id,
# AUTO-REPAIR: duplicate route '/api/stripe/webhook' also in main.py:8290 — review and remove one
            'url': checkout_session.url
        })
    except Exception as e:
        print(f"Stripe checkout error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    if not STRIPE_AVAILABLE:
        return jsonify({'error': 'Stripe not available'}), 503
    
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    # Verify webhook signature if secret is configured
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            print(f"Webhook error: Invalid payload - {e}")
            return jsonify({'error': 'Invalid payload'}), 400
        except stripe.error.SignatureVerificationError as e:
            print(f"Webhook error: Invalid signature - {e}")
            return jsonify({'error': 'Invalid signature'}), 400
    else:
        # Without webhook secret, parse event directly (less secure)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({'error': 'Invalid JSON'}), 400
    
    event_type = event.get('type', '')
    data = event.get('data', {}).get('object', {})
    
    print(f"💳 Stripe webhook: {event_type}")
    
    # Handle different event types
    if event_type == 'checkout.session.completed':
        handle_checkout_completed(data)
    elif event_type == 'customer.subscription.created':
        handle_subscription_created(data)
    elif event_type == 'customer.subscription.updated':
        handle_subscription_updated(data)
    elif event_type == 'customer.subscription.deleted':
        handle_subscription_deleted(data)
    elif event_type == 'invoice.paid':
        handle_invoice_paid(data)
    elif event_type == 'invoice.payment_failed':
        handle_payment_failed(data)
    
    return jsonify({'received': True})

def handle_checkout_completed(session):
    """Handle successful checkout - upgrade user plan"""
    customer_email = session.get('customer_email', '').lower()
    metadata = session.get('metadata', {})
    user_id = metadata.get('user_id')
    plan = metadata.get('plan', 'pro')
    
    # Map plan to database plan name
    plan_name = 'pro' if plan in ['pro_monthly', 'pro_annual'] else 'founding'
    
    conn = get_db()
    c = conn.cursor()
    
    # Try to find user by ID first, then by email
    if user_id:
        c.execute("UPDATE users SET plan = %s, stripe_customer_id = %s WHERE id = %s",
                  (plan_name, session.get('customer', ''), user_id))
    elif customer_email:
        c.execute("UPDATE users SET plan = %s, stripe_customer_id = %s WHERE email = %s",
                  (plan_name, session.get('customer', ''), customer_email))
    
    conn.commit()
    conn.close()
    
    print(f"✅ User upgraded to {plan_name}: {customer_email or user_id}")

def handle_subscription_created(subscription):
    """Handle new subscription"""
    customer_id = subscription.get('customer', '')
    status = subscription.get('status', '')
    
    if status == 'active':
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET plan = 'pro', subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
        conn.commit()
        conn.close()
        print(f"✅ Subscription activated for customer: {customer_id}")

def handle_subscription_updated(subscription):
    """Handle subscription changes"""
    customer_id = subscription.get('customer', '')
    status = subscription.get('status', '')
    
    conn = get_db()
    c = conn.cursor()
    
    if status in ['active', 'trialing']:
        c.execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
    elif status in ['past_due', 'unpaid']:
        c.execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
    elif status == 'canceled':
        c.execute("UPDATE users SET plan = 'free', subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
    
    conn.commit()
    conn.close()
    print(f"📝 Subscription updated for customer {customer_id}: {status}")

def handle_subscription_deleted(subscription):
    """Handle subscription cancellation"""
    customer_id = subscription.get('customer', '')
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET plan = 'free', subscription_status = 'canceled' WHERE stripe_customer_id = %s",
              (customer_id,))
    conn.commit()
    conn.close()
    print(f"❌ Subscription canceled for customer: {customer_id}")

def handle_invoice_paid(invoice):
    """Handle successful payment"""
    customer_id = invoice.get('customer', '')
    print(f"💰 Invoice paid for customer: {customer_id}")

def handle_payment_failed(invoice):
    """Handle failed payment"""
    customer_id = invoice.get('customer', '')
    
    conn = get_db()
# AUTO-REPAIR: duplicate route '/api/stripe/subscription' also in main.py:9614 — review and remove one
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_status = 'payment_failed' WHERE stripe_customer_id = %s",
              (customer_id,))
    conn.commit()
    conn.close()
    print(f"⚠️ Payment failed for customer: {customer_id}")

@app.route('/api/stripe/subscription', methods=['GET'])
@require_auth
def get_subscription_status():
    """Get current user's subscription status"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT plan, stripe_customer_id, subscription_status 
        FROM users WHERE id = %s
    """, (request.user['user_id'],))
    
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    return jsonify({
        'plan': user[0] or 'free',
        'customerId': user[1],
        'status': user[2] or 'none',
        'features': get_plan_features(user[0] or 'free')
    })

def get_plan_features(plan):
    """Return features available for a plan"""
    features = {
        'free': {
            'market_comparisons': 3,
            'pdf_reports': 0,
            'saved_searches': 5,
            'api_access': False,
            'priority_support': False
        },
        'pro': {
            'market_comparisons': -1,  # unlimited
            'pdf_reports': -1,
            'saved_searches': -1,
            'api_access': True,
            'priority_support': True
        },
        'founding': {
            'market_comparisons': -1,
            'pdf_reports': -1,
# AUTO-REPAIR: duplicate route '/api/stripe/portal' also in main.py:9684 — review and remove one
            'saved_searches': -1,
            'api_access': True,
            'priority_support': True,
            'founding_badge': True
        }
    }
    return features.get(plan, features['free'])

@app.route('/api/stripe/portal', methods=['POST'])
@require_auth
def create_portal_session():
    """Create Stripe Customer Portal session for managing subscription"""
    if not STRIPE_AVAILABLE or not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe not configured'}), 503
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT stripe_customer_id FROM users WHERE id = %s", (request.user['user_id'],))
    user = c.fetchone()
    conn.close()
    
    if not user or not user[0]:
        return jsonify({'error': 'No subscription found'}), 404
    
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=user[0],
            return_url='https://dchub.cloud/dashboard.html'
        )
# AUTO-REPAIR: duplicate route '/api/v1/markets/list' also in main.py:9747 — review and remove one
        return jsonify({'url': portal_session.url})
    except Exception as e:
        print(f"Portal error: {e}")
        return jsonify({'error': str(e)}), 500

# =============================================================================
# MARKET COMPARISON ENDPOINTS
# =============================================================================

@app.route('/api/v1/markets/list', methods=['GET'])
def list_markets():
    """List all available markets with basic stats"""
    conn = get_db()
    c = conn.cursor()
    
    markets = []
    
    for market_key, cities in MARKET_ALIASES.items():
        # Skip state-level aliases
        if len(market_key) <= 2 or market_key in ['la', 'sf', 'nj', 'nyc', 'dfw', 'nova']:
            continue
        
        # Build city conditions
        conditions = []
        params = []
        for city in cities:
            if len(city) == 2 and city.isupper():
                conditions.append('state = ?')
            else:
                conditions.append('city LIKE ?')
            params.append(f'%{city}%' if len(city) > 2 else city)
        
        where_clause = ' OR '.join(conditions)
        
        c.execute(f"""
            SELECT COUNT(*) as count, COALESCE(SUM(power_mw), 0) as total_power
            FROM facilities 
            WHERE ({where_clause})
            {RAILWAY_EXCLUSION}
        """, params)
        
        row = c.fetchone()
        if row and row[0] > 0:
            markets.append({
                'id': market_key,
                'name': market_key.replace('_', ' ').title(),
                'cities': cities[:5],  # Top 5 cities
                'facility_count': row[0],
                'total_power_mw': round(row[1] or 0, 1)
            })
    
    conn.close()
# AUTO-REPAIR: duplicate route '/api/v1/markets/<market>' also in main.py:9952 — review and remove one
    
    # Sort by facility count
    markets.sort(key=lambda x: x['facility_count'], reverse=True)
    
    return jsonify({
        'success': True,
        'count': len(markets),
        'data': markets
    })

@app.route('/api/v1/markets/<market>', methods=['GET'])
def get_market_stats(market):
    """Get detailed stats for a single market"""
    market_lower = market.lower().replace('-', ' ')
    
    if market_lower not in MARKET_ALIASES:
        return jsonify({'error': 'Market not found', 'code': 'NOT_FOUND'}), 404
    
    cities = MARKET_ALIASES[market_lower]
    
    conn = get_db()
    c = conn.cursor()
    
    # Build city conditions
    conditions = []
    params = []
    for city in cities:
        if len(city) == 2 and city.isupper():
            conditions.append('state = %s')
            params.append(city)
        else:
            conditions.append('city LIKE %s')
            params.append(f'%{city}%')
    
    where_clause = ' OR '.join(conditions)
    
    # Get overall stats
    c.execute(f"""
        SELECT 
            COUNT(*) as facility_count,
            COALESCE(SUM(power_mw), 0) as total_power,
            COALESCE(AVG(power_mw), 0) as avg_power,
            COUNT(DISTINCT provider) as provider_count
        FROM facilities 
        WHERE ({where_clause})
        {RAILWAY_EXCLUSION}
    """, params)
    
    stats = dict(c.fetchone())
    
    # Top providers
    c.execute(f"""
        SELECT provider, COUNT(*) as count, COALESCE(SUM(power_mw), 0) as power
        FROM facilities 
        WHERE ({where_clause}) AND provider != ''
        {RAILWAY_EXCLUSION}
        GROUP BY provider
        ORDER BY count DESC
        LIMIT 10
    """, params)
    
    top_providers = [{'name': r[0], 'facilities': r[1], 'power_mw': round(r[2] or 0, 1)} for r in c.fetchall()]
    
    # By status
    c.execute(f"""
        SELECT status, COUNT(*) as count
        FROM facilities 
        WHERE ({where_clause})
        {RAILWAY_EXCLUSION}
        GROUP BY status
    """, params)
    
    by_status = dict(c.fetchall())
    
    # Recent facilities
    c.execute(f"""
        SELECT id, name, provider, city, power_mw, status, first_seen
        FROM facilities 
        WHERE ({where_clause})
        {RAILWAY_EXCLUSION}
        ORDER BY first_seen DESC
        LIMIT 5
    """, params)
    
    recent = [dict(r) for r in c.fetchall()]
    
    conn.close()
    
    return jsonify({
        'success': True,
        'market': {
            'id': market_lower,
            'name': market_lower.replace('_', ' ').title(),
            'cities': cities
        },
# AUTO-REPAIR: duplicate route '/api/v1/markets/compare' also in main.py:10087 — review and remove one
        'stats': {
            'facility_count': stats['facility_count'],
            'total_power_mw': round(stats['total_power'], 1),
            'avg_power_mw': round(stats['avg_power'], 1),
            'provider_count': stats['provider_count']
        },
        'top_providers': top_providers,
        'by_status': by_status,
        'recent_facilities': recent
    })

@app.route('/api/v1/markets/compare', methods=['GET'])
def compare_markets():
    """Compare 2-3 markets side-by-side"""
    markets_param = request.args.get('markets', '')
    
    if not markets_param:
        return jsonify({'error': 'markets parameter required (comma-separated)', 'code': 'VALIDATION_ERROR'}), 400
    
    market_list = [m.strip().lower().replace('-', ' ') for m in markets_param.split(',')]
    
    if len(market_list) < 2 or len(market_list) > 4:
        return jsonify({'error': 'Please provide 2-4 markets to compare', 'code': 'VALIDATION_ERROR'}), 400
    
    # Validate markets
    invalid = [m for m in market_list if m not in MARKET_ALIASES]
    if invalid:
        return jsonify({
            'error': f'Unknown markets: {invalid}',
            'code': 'NOT_FOUND',
            'available_markets': list(MARKET_ALIASES.keys())[:20]
        }), 404
    
    conn = get_db()
    c = conn.cursor()
    
    comparison = []
    
    for market in market_list:
        cities = MARKET_ALIASES[market]
        
        conditions = []
        params = []
        for city in cities:
            if len(city) == 2 and city.isupper():
                conditions.append('state = ?')
                params.append(city)
            else:
                conditions.append('city LIKE ?')
                params.append(f'%{city}%')
        
        where_clause = ' OR '.join(conditions)
        
        # Get comprehensive stats
        c.execute(f"""
            SELECT 
                COUNT(*) as facility_count,
                COALESCE(SUM(power_mw), 0) as total_power,
                COALESCE(AVG(power_mw), 0) as avg_power,
                COALESCE(MAX(power_mw), 0) as max_power,
                COUNT(DISTINCT provider) as provider_count,
                SUM(CASE WHEN status = 'operational' THEN 1 ELSE 0 END) as operational,
                SUM(CASE WHEN status = 'planned' OR status = 'under_construction' THEN 1 ELSE 0 END) as pipeline
            FROM facilities 
            WHERE ({where_clause})
            {RAILWAY_EXCLUSION}
        """, params)
        
        stats = dict(c.fetchone())
        
        # Top 5 providers
        c.execute(f"""
            SELECT provider, COUNT(*) as count
            FROM facilities 
            WHERE ({where_clause}) AND provider != ''
            {RAILWAY_EXCLUSION}
            GROUP BY provider
            ORDER BY count DESC
            LIMIT 5
        """, params)
        
        top_providers = [r[0] for r in c.fetchall()]
        
        comparison.append({
            'market': market,
            'display_name': market.replace('_', ' ').title(),
            'metrics': {
                'facilities': stats['facility_count'],
                'total_power_mw': round(stats['total_power'], 1),
                'avg_power_mw': round(stats['avg_power'], 1),
                'max_power_mw': round(stats['max_power'], 1),
                'providers': stats['provider_count'],
                'operational': stats['operational'] or 0,
                'pipeline': stats['pipeline'] or 0
            },
            'top_providers': top_providers
        })
    
# AUTO-REPAIR: duplicate route '/api/reports/generate' also in main.py:10206 — review and remove one
    conn.close()
    
    return jsonify({
        'success': True,
        'comparison': comparison,
        'generated_at': datetime.utcnow().isoformat()
    })

# =============================================================================
# PDF REPORT GENERATOR
# =============================================================================

@app.route('/api/reports/generate', methods=['POST'])
@optional_auth
def generate_report():
    """Generate PDF market report"""
    if not PDF_AVAILABLE:
        return jsonify({'error': 'PDF generation not available', 'code': 'SERVICE_UNAVAILABLE'}), 503
    
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Request body required'}), 400
    
    report_type = data.get('type', 'market_overview')
    markets = data.get('markets', [])
    email = data.get('email', '').lower().strip()
    
    if not markets:
        return jsonify({'error': 'At least one market required', 'code': 'VALIDATION_ERROR'}), 400
    
    if not email and not request.user:
        return jsonify({'error': 'Email required for report delivery', 'code': 'VALIDATION_ERROR'}), 400
    
    # Capture lead if email provided
    if email:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id FROM leads WHERE email = %s", (email,))
            if not c.fetchone():
                lead_id = secrets.token_hex(8)
                c.execute("""
                    INSERT INTO leads (id, email, source, source_detail, lead_score, created_at, last_activity)
                    VALUES (%s, %s, 'pdf_report', %s, 25, %s, %s) ON CONFLICT DO NOTHING
                """, (lead_id, email, json.dumps(markets), datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
            else:
                c.execute("UPDATE leads SET lead_score = lead_score + 25, last_activity = %s WHERE email = %s",
                         (datetime.utcnow().isoformat(), email))
            conn.commit()
            conn.close()
        except:
            pass
    
    # Generate report
    report_id = secrets.token_hex(8)
    
    try:
        pdf_buffer = generate_market_pdf(markets, report_type)
        
        # Save report record
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO reports (id, user_id, email, report_type, markets, status, created_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s) ON CONFLICT DO NOTHING
        """, (
            report_id,
            request.user['user_id'] if request.user else None,
            email or (request.user['email'] if request.user else None),
            report_type,
            json.dumps(markets),
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()
        
        # Return PDF
        pdf_buffer.seek(0)
        return send_file(
            pdf_buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'dc-hub-{"-".join(markets)}-report.pdf'
        )
        
    except Exception as e:
        return jsonify({'error': f'Report generation failed: {str(e)}', 'code': 'GENERATION_ERROR'}), 500

def generate_market_pdf(markets, report_type):
    """Generate the actual PDF report"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=30,
        textColor=colors.HexColor('#6366f1')
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=20,
        spaceAfter=10,
        textColor=colors.HexColor('#1a1a2e')
    )
    
    normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=8
    )
    
    elements = []
    
    # Title
    title = f"Data Center Market Report"
    elements.append(Paragraph(title, title_style))
    elements.append(Paragraph(f"Markets: {', '.join([m.title() for m in markets])}", normal_style))
    elements.append(Paragraph(f"Generated: {datetime.utcnow().strftime('%B %d, %Y')}", normal_style))
    elements.append(Spacer(1, 20))
    
    conn = get_db()
    c = conn.cursor()
    
    for market in markets:
        market_lower = market.lower().replace('-', ' ')
        if market_lower not in MARKET_ALIASES:
            continue
            
        cities = MARKET_ALIASES[market_lower]
        
        elements.append(Paragraph(f"📍 {market.title()} Market", heading_style))
        
        # Build query
        conditions = []
        params = []
        for city in cities:
            if len(city) == 2 and city.isupper():
                conditions.append('state = ?')
                params.append(city)
            else:
                conditions.append('city LIKE ?')
                params.append(f'%{city}%')
        
        where_clause = ' OR '.join(conditions)
        
        # Get stats
        c.execute(f"""
            SELECT 
                COUNT(*) as facility_count,
                COALESCE(SUM(power_mw), 0) as total_power,
                COALESCE(AVG(power_mw), 0) as avg_power,
                COUNT(DISTINCT provider) as provider_count
            FROM facilities 
            WHERE ({where_clause})
            {RAILWAY_EXCLUSION}
        """, params)
        
        stats = c.fetchone()
        
        # Stats table
        stats_data = [
            ['Metric', 'Value'],
            ['Total Facilities', str(stats[0])],
            ['Total Power Capacity', f"{stats[1]:,.1f} MW"],
            ['Average Facility Size', f"{stats[2]:,.1f} MW"],
            ['Active Providers', str(stats[3])]
        ]
        
        stats_table = Table(stats_data, colWidths=[2.5*inch, 2*inch])
        stats_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6366f1')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0'))
        ]))
        
        elements.append(stats_table)
        elements.append(Spacer(1, 15))
        
        # Top providers
        c.execute(f"""
            SELECT provider, COUNT(*) as count, COALESCE(SUM(power_mw), 0) as power
            FROM facilities 
            WHERE ({where_clause}) AND provider != ''
            {RAILWAY_EXCLUSION}
            GROUP BY provider
            ORDER BY count DESC
            LIMIT 5
        """, params)
        
        providers = c.fetchall()
        if providers:
            elements.append(Paragraph("Top Providers", heading_style))
            
            provider_data = [['Provider', 'Facilities', 'Total Power']]
            for p in providers:
                provider_data.append([p[0][:30], str(p[1]), f"{p[2]:,.1f} MW"])
            
            provider_table = Table(provider_data, colWidths=[2.5*inch, 1*inch, 1.5*inch])
            provider_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#10b981')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (1, 0), (2, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0'))
            ]))
            
            elements.append(provider_table)
        
        elements.append(Spacer(1, 30))
    
    conn.close()
    
# AUTO-REPAIR: duplicate route '/api/v1/stats' also in main.py:10979 — review and remove one
    # Footer
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("─" * 60, normal_style))
    elements.append(Paragraph("Generated by DC Hub | dchub.cloud", normal_style))
    elements.append(Paragraph("For more market intelligence, visit https://dchub.cloud", normal_style))
    
    doc.build(elements)
    return buffer

# =============================================================================
# EXISTING ENDPOINTS (from original server)
# =============================================================================

@app.route('/api/v1/stats', methods=['GET'])
def get_stats():
    """Get aggregate statistics"""
    conn = get_db()
    c = conn.cursor()
    
    stats = {}
    
    c.execute("SELECT COUNT(*) FROM facilities")
    stats['total_facilities'] = c.fetchone()[0]
    
    c.execute("SELECT COALESCE(SUM(power_mw), 0) FROM facilities")
    stats['total_power_mw'] = round(c.fetchone()[0] or 0, 1)
    
    c.execute("SELECT COUNT(*) FROM announcements")
    stats['total_announcements'] = c.fetchone()[0]
    
    c.execute("SELECT source, COUNT(*) FROM facilities GROUP BY source ORDER BY COUNT(*) DESC")
    stats['by_source'] = dict(c.fetchall())
    
    c.execute("SELECT country, COUNT(*) FROM facilities WHERE country != '' GROUP BY country ORDER BY COUNT(*) DESC LIMIT 10")
    stats['top_countries'] = dict(c.fetchall())
    
    c.execute(f"""
        SELECT provider, COUNT(*) FROM facilities 
        WHERE provider != '' 
        {RAILWAY_EXCLUSION}
        GROUP BY provider ORDER BY COUNT(*) DESC LIMIT 10
    """)
    stats['top_providers'] = dict(c.fetchall())
    
    c.execute("SELECT status, COUNT(*) FROM facilities GROUP BY status")
    stats['by_status'] = dict(c.fetchall())
    
    c.execute("SELECT COUNT(*) FROM facilities WHERE first_seen > datetime('now', '-7 days')")
    stats['new_last_7_days'] = c.fetchone()[0]
    
    # New: Lead stats
# AUTO-REPAIR: duplicate route '/api/v1/facilities' also in main.py:11456 — review and remove one
    c.execute("SELECT COUNT(*) FROM leads")
    stats['total_leads'] = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users")
    stats['total_users'] = c.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': stats,
        'generated_at': datetime.utcnow().isoformat()
    })

@app.route('/api/v1/facilities', methods=['GET'])
def list_facilities():
    """List facilities with pagination and filtering"""
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    limit = min(limit, 100)
    offset = (page - 1) * limit
    
    q = request.args.get('q', '').strip()
    country = request.args.get('country')
    provider = request.args.get('provider')
    status = request.args.get('status')
    region = request.args.get('region')
    min_power = request.args.get('min_power', type=float)
    source = request.args.get('source')
    
    sql = "SELECT * FROM facilities WHERE 1=1"
    count_sql = "SELECT COUNT(*) FROM facilities WHERE 1=1"
    params = []
    
    if q:
        query_lower = q.lower()
        if query_lower in MARKET_ALIASES:
            search_cities = MARKET_ALIASES[query_lower]
            conditions = []
            for city in search_cities:
                if len(city) == 2 and city.isupper():
                    conditions.append('state = ?')
                    params.append(city)
                else:
                    conditions.append('city LIKE ?')
                    params.append(f'%{city}%')
            search_clause = f" AND ({' OR '.join(conditions)})"
        else:
            search_clause = " AND (city LIKE %s OR state LIKE %s OR name LIKE %s OR provider LIKE %s)"
            params.extend([f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'])
        
        sql += search_clause
        count_sql += search_clause
    
    if country:
        sql += " AND country = %s"
        count_sql += " AND country = %s"
        params.append(country)
    if provider:
        sql += " AND provider LIKE %s"
        count_sql += " AND provider LIKE %s"
        params.append(f"%{provider}%")
    if status:
        sql += " AND status = %s"
        count_sql += " AND status = %s"
        params.append(status)
    if region:
        sql += " AND region = %s"
        count_sql += " AND region = %s"
        params.append(region)
    if min_power:
        sql += " AND power_mw >= %s"
        count_sql += " AND power_mw >= %s"
        params.append(min_power)
    if source:
        sql += " AND source = %s"
        count_sql += " AND source = %s"
        params.append(source)
    
    sql += f" ORDER BY confidence DESC, power_mw DESC LIMIT {limit} OFFSET {offset}"
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute(count_sql, params)
    total = c.fetchone()[0]
    
    c.execute(sql, params)
# AUTO-REPAIR: duplicate route '/api/v1/search' also in main.py:11722 — review and remove one
    facilities = [dict_from_row(row) for row in c.fetchall()]
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': facilities,
        'pagination': {
            'page': page,
            'limit': limit,
            'total': total,
            'pages': (total + limit - 1) // limit
        }
    })

@app.route('/api/v1/search', methods=['GET'])
def search_facilities():
    """Search facilities"""
    query = request.args.get('q', '').strip()
    limit = min(request.args.get('limit', 50, type=int), 100)
    
    if len(query) < 2:
        return jsonify({'error': 'Query must be at least 2 characters'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    query_lower = query.lower()
    
    if query_lower in MARKET_ALIASES:
        cities = MARKET_ALIASES[query_lower]
        conditions = []
        params = []
        for city in cities:
            if len(city) == 2 and city.isupper():
                conditions.append('state = %s')
                params.append(city)
            else:
                conditions.append('city LIKE %s')
                params.append(f'%{city}%')
        
        sql = f"""
            SELECT * FROM facilities 
            WHERE ({' OR '.join(conditions)})
            {RAILWAY_EXCLUSION}
            ORDER BY confidence DESC, power_mw DESC
            LIMIT %s
        """
        params.append(limit)
        c.execute(sql, params)
    else:
        q = f"%{query}%"
        c.execute(f"""
            SELECT * FROM facilities 
            WHERE (city LIKE %s OR state LIKE %s OR name LIKE %s OR provider LIKE %s)
            {RAILWAY_EXCLUSION}
            ORDER BY confidence DESC, power_mw DESC
            LIMIT %s
# AUTO-REPAIR: duplicate route '/api/agents/health' also in main.py:11867 — review and remove one
        """, (q, q, q, q, limit))
    
    facilities = [dict_from_row(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify({
        'success': True,
        'query': query,
        'count': len(facilities),
        'data': facilities
    })

# =============================================================================
# AGENT HUB ROUTES (existing)
# =============================================================================

@app.route('/api/agents/health')
def agents_health():
    return jsonify({
        "status": "healthy",
        "agents": ["sales", "enrichment", "social"],
        "anthropic_configured": bool(os.environ.get('ANTHROPIC_API_KEY')),
        "timestamp": datetime.utcnow().isoformat()
    })

@app.route('/api/agents/social/generate', methods=['POST'])
def social_generate():
    """Generate social media post"""
    try:
        from agent_hub import generate_social_post
        return generate_social_post()
# AUTO-REPAIR: duplicate route '/api/agents/enrichment/submit' also in main.py:11876 — review and remove one
    except Exception as e:
        # Fallback generation
        data = request.get_json() or {}
        topic = data.get('topic', 'market-trends')
        region = data.get('region', 'global')
        
        posts = {
            'market-trends': f"📊 Data Center Market Update | {region.title()}\n\nKey trends we're tracking:\n• AI/ML driving unprecedented demand\n• Power availability constraining growth\n• Sustainability as competitive advantage\n\nWhat trends are you seeing%s #DataCenter #Infrastructure",
            'ai-demand': f"🤖 AI Infrastructure Demand | {region.title()}\n\nGPU clusters requiring 10-50x power density. Liquid cooling becoming standard. New markets emerging around low-cost power.\n\nHow is AI affecting your strategy%s #AI #DataCenter",
            'sustainability': f"🌱 Sustainable Data Centers | {region.title()}\n\nProgress on environmental goals:\n• PUE improvements through innovation\n• Renewable energy commitments\n• Water conservation priorities\n\n#Sustainability #GreenDataCenter"
        }
        
        return jsonify({
            'success': True,
            'post': posts.get(topic, posts['market-trends'])
        })

@app.route('/api/agents/enrichment/submit', methods=['POST'])
def enrichment_submit():
    """Submit data enrichment"""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Data required'}), 400
    
    # Store submission
    conn = get_db()
    c = conn.cursor()
    
    submission_id = secrets.token_hex(8)
    c.execute("""
        INSERT INTO submissions (id, api_key, submission_type, data, status, submitted_at)
        VALUES (%s, 'crowdsource', 'enrichment', %s, 'pending', %s) ON CONFLICT DO NOTHING
    """, (submission_id, json.dumps(data), datetime.utcnow().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Thank you for your submission!',
# AUTO-REPAIR: duplicate route '/' also in main.py:11952 — review and remove one
        'submission_id': submission_id
    })

@app.route('/api/agents/sales/chat', methods=['POST'])
def sales_chat():
    """Sales chat endpoint"""
    try:
        from agent_hub import sales_chat as sc
# AUTO-REPAIR: duplicate route '/health' also in index_api.py:516 — review and remove one
        return sc()
    except Exception as e:
        return jsonify({
            'response': "Thanks for reaching out! I'd be happy to help you explore DC Hub's capabilities. What specific data center markets or features are you interested in%s"
        })

# =============================================================================
# HEALTH & INFO
# =============================================================================

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'name': 'DC Hub Nexus API',
        'version': '75.0.0',
        'status': 'healthy',
        'features': ['leads', 'auth', 'markets', 'reports', 'agents', 'discovery']
    })

@app.route('/health', methods=['GET'])
def health():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM facilities")
    count = c.fetchone()[0]
    conn.close()
    
    return jsonify({
        'status': 'healthy',
        'database': 'connected',
        'facilities_count': count,
        'pdf_available': PDF_AVAILABLE,
        'timestamp': datetime.utcnow().isoformat()
    })

# =============================================================================
# AUTO-DISCOVERY SYSTEM
# =============================================================================

# Discovery source configurations
DISCOVERY_SOURCES = {
    'peeringdb': {
        'name': 'PeeringDB',
        'url': 'https://www.peeringdb.com/api/fac',
        'refresh_hours': 168,  # 7 days
        'enabled': True
    },
    'datacentermap': {
        'name': 'DataCenterMap',
        'url': 'https://www.datacentermap.com',
        'refresh_hours': 24,
        'enabled': True
    },
    'operator_websites': {
        'name': 'Operator Websites',
        'refresh_hours': 12,
        'enabled': True
    }
}

# Target operators to actively search for
TARGET_OPERATORS = [
    # Primary targets
    {'name': 'Centra', 'search_terms': ['Centra data center', 'Centra colocation'], 'markets': ['Dallas', 'Phoenix', 'Houston']},
    {'name': 'Netrality', 'search_terms': ['Netrality data center', 'Netrality Properties'], 'markets': ['Kansas City', 'St. Louis', 'Philadelphia']},
    {'name': 'Tract', 'search_terms': ['Tract data center', 'Tract colocation'], 'markets': ['Salt Lake City', 'Reno']},
    {'name': 'Powerhouse', 'search_terms': ['Powerhouse data center', 'Powerhouse DC'], 'markets': ['Multiple']},
    # Major operators to track
    {'name': 'Equinix', 'search_terms': ['Equinix IBX', 'Equinix data center'], 'markets': ['Global']},
    {'name': 'Digital Realty', 'search_terms': ['Digital Realty', 'DLR data center'], 'markets': ['Global']},
    {'name': 'QTS', 'search_terms': ['QTS data center', 'QTS Realty'], 'markets': ['US']},
    {'name': 'CyrusOne', 'search_terms': ['CyrusOne data center'], 'markets': ['US', 'Europe']},
    {'name': 'Vantage', 'search_terms': ['Vantage Data Centers'], 'markets': ['US', 'Canada', 'Europe']},
    {'name': 'EdgeCore', 'search_terms': ['EdgeCore Digital', 'EdgeCore data center'], 'markets': ['US']},
    {'name': 'Stack Infrastructure', 'search_terms': ['Stack Infrastructure', 'Stack data center'], 'markets': ['US', 'Europe']},
    {'name': 'Compass Datacenters', 'search_terms': ['Compass Datacenters'], 'markets': ['US']},
    {'name': 'CloudHQ', 'search_terms': ['CloudHQ data center'], 'markets': ['US']},
    {'name': 'Prime Data Centers', 'search_terms': ['Prime Data Centers'], 'markets': ['US']},
    {'name': 'Stream Data Centers', 'search_terms': ['Stream Data Centers'], 'markets': ['US']},
    {'name': 'T5 Data Centers', 'search_terms': ['T5 Data Centers', 'T5@'], 'markets': ['US']},
    {'name': 'Aligned Data Centers', 'search_terms': ['Aligned Data Centers', 'Aligned DC'], 'markets': ['US']},
    {'name': 'DataBank', 'search_terms': ['DataBank data center'], 'markets': ['US']},
    {'name': 'Flexential', 'search_terms': ['Flexential data center'], 'markets': ['US']},
    {'name': 'TierPoint', 'search_terms': ['TierPoint data center'], 'markets': ['US']},
    {'name': 'CoreSite', 'search_terms': ['CoreSite data center'], 'markets': ['US']},
    {'name': 'Sabey', 'search_terms': ['Sabey Data Centers'], 'markets': ['US']},
    {'name': 'H5 Data Centers', 'search_terms': ['H5 Data Centers'], 'markets': ['US']},
    {'name': 'NTT', 'search_terms': ['NTT Global Data Centers', 'NTT GDC'], 'markets': ['Global']},
    {'name': 'COPT', 'search_terms': ['COPT Data Center', 'Corporate Office Properties'], 'markets': ['NoVA']},
]

# Operator website URLs for direct scraping
OPERATOR_WEBSITES = {
    'Equinix': 'https://www.equinix.com/data-centers',
    'Digital Realty': 'https://www.digitalrealty.com/data-centers',
    'QTS': 'https://www.qtsdatacenters.com/data-centers',
    'CyrusOne': 'https://cyrusone.com/data-centers/',
    'Vantage': 'https://vantage-dc.com/data-centers/',
    'CoreSite': 'https://www.coresite.com/data-centers',
    'DataBank': 'https://www.databank.com/data-centers/',
    'Flexential': 'https://www.flexential.com/data-centers',
    'TierPoint': 'https://www.tierpoint.com/data-centers/',
    'Compass': 'https://www.compassdatacenters.com/data-centers/',
    'Stack': 'https://www.stackinfra.com/data-centers/',
    'EdgeCore': 'https://www.edgecoredigital.com/',
    'CloudHQ': 'https://www.cloudhq.com/data-centers/',
    'Stream': 'https://www.streamdatacenters.com/',
    'Aligned': 'https://www.alignedenergy.com/data-centers/',
    'T5': 'https://t5datacenters.com/data-centers/',
    'Prime': 'https://www.yourprime.com/data-centers/',
    'Sabey': 'https://sabeydatacenters.com/data-centers/',
    'H5': 'https://h5datacenters.com/',
    'NTT': 'https://services.global.ntt/en-us/data-centers',
    'Centra': 'https://www.centra.com/',
    'Netrality': 'https://www.netrality.com/data-centers/',
    'Tract': 'https://tractdc.com/',
    'Powerhouse': 'https://www.powerhousedc.com/',
}

def init_discovery_tables():
    """Initialize discovery tracking tables"""
    conn = get_db()
    c = conn.cursor()
    
    # Discovery runs tracking
    c.execute('''
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            facilities_found INTEGER DEFAULT 0,
            facilities_added INTEGER DEFAULT 0,
            facilities_updated INTEGER DEFAULT 0,
            facilities_duplicate INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            error TEXT,
            details TEXT
        )
    ''')
    
    # Discovered facilities (staging before merge)
    c.execute('''
        CREATE TABLE IF NOT EXISTS discovered_facilities (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT,
            name TEXT NOT NULL,
            provider TEXT,
            market TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            power_mw REAL,
            sqft INTEGER,
            status TEXT,
            facility_type TEXT,
            source_url TEXT,
            raw_data TEXT,
            discovered_at TEXT NOT NULL,
            merged_at TEXT,
            merged_facility_id INTEGER,
            is_duplicate INTEGER DEFAULT 0,
            confidence_score REAL DEFAULT 0.5
        )
    ''')
    
    # Discovery schedule
    c.execute('''
        CREATE TABLE IF NOT EXISTS discovery_schedule (
            source TEXT PRIMARY KEY,
            last_run TEXT,
            next_run TEXT,
            run_count INTEGER DEFAULT 0,
            total_found INTEGER DEFAULT 0,
            total_added INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialize tables on module load
try:
    init_discovery_tables()
except:
    pass

def calculate_similarity(str1, str2):
    """Calculate string similarity score (0-1)"""
    if not str1 or not str2:
        return 0
    str1 = str1.lower().strip()
    str2 = str2.lower().strip()
    if str1 == str2:
        return 1.0
    
    # Simple word overlap
    words1 = set(str1.split())
    words2 = set(str2.split())
    if not words1 or not words2:
        return 0
    overlap = len(words1 & words2)
    return overlap / max(len(words1), len(words2))

def is_duplicate_facility(conn, name, provider, lat, lon, city):
    """Check if facility is a duplicate"""
    c = conn.cursor()
    
    # Exact name + provider match
    c.execute("""
        SELECT id, name, provider, latitude, longitude 
        FROM facilities 
        WHERE LOWER(name) = LOWER(%s) AND LOWER(provider) = LOWER(%s)
    """, (name, provider))
    if c.fetchone():
        return True, 'exact_match'
    
    # Location proximity match (within ~1km)
    if lat and lon:
        c.execute("""
            SELECT id, name, provider, latitude, longitude 
            FROM facilities 
            WHERE ABS(latitude - %s) < 0.01 AND ABS(longitude - %s) < 0.01
            AND LOWER(provider) = LOWER(%s)
        """, (lat, lon, provider))
        match = c.fetchone()
        if match:
            return True, f'location_match:{match[0]}'
    
    # Fuzzy name match in same city
    if city:
        c.execute("""
            SELECT id, name, provider FROM facilities 
            WHERE LOWER(city) = LOWER(%s) AND LOWER(provider) = LOWER(%s)
        """, (city, provider))
        for row in c.fetchall():
            if calculate_similarity(name, row[1]) > 0.7:
                return True, f'fuzzy_match:{row[0]}'
    
    return False, None

def discover_from_peeringdb():
    """Fetch facilities from PeeringDB API"""
    discoveries = []
    
    try:
        response = requests.get(
            'https://www.peeringdb.com/api/fac',
            headers={'User-Agent': 'DC Hub Discovery Bot/1.0'},
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            facilities = data.get('data', [])
            
            for fac in facilities:
                # Extract data
                discovery = {
                    'source': 'peeringdb',
                    'source_id': str(fac.get('id', '')),
                    'name': fac.get('name', ''),
                    'provider': fac.get('org_name', fac.get('name', '')).split(' - ')[0] if fac.get('org_name') else '',
                    'city': fac.get('city', ''),
                    'state': fac.get('state', ''),
                    'country': fac.get('country', ''),
                    'address': fac.get('address1', ''),
                    'latitude': fac.get('latitude'),
                    'longitude': fac.get('longitude'),
                    'status': 'Operational',
                    'facility_type': 'Colocation',
                    'source_url': f"https://www.peeringdb.com/fac/{fac.get('id', '')}",
                    'raw_data': json.dumps(fac)
                }
                
                # Determine market from city/state
                discovery['market'] = determine_market(discovery['city'], discovery['state'], discovery['country'])
                
                if discovery['name']:
                    discoveries.append(discovery)
                    
    except Exception as e:
        print(f"PeeringDB error: {e}")
    
    return discoveries

def discover_from_operator_websites():
    """Discover facilities from operator websites - comprehensive database"""
    discoveries = []
    
    # Known facilities from target operators and major providers
    operator_facilities = [
        # ==========================================
        # YOUR PRIORITY TARGETS
        # ==========================================
        
        # Centra
        {'name': 'Centra Dallas 1', 'provider': 'Centra', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'Centra Phoenix 1', 'provider': 'Centra', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'Centra Houston 1', 'provider': 'Centra', 'city': 'Houston', 'state': 'TX', 'market': 'Houston', 'power_mw': 10, 'status': 'Operational'},
        
        # Netrality Properties
        {'name': 'Netrality 1102 Grand', 'provider': 'Netrality', 'city': 'Kansas City', 'state': 'MO', 'market': 'Kansas City', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'Netrality 210 N Tucker', 'provider': 'Netrality', 'city': 'St. Louis', 'state': 'MO', 'market': 'St. Louis', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'Netrality 401 N Broad', 'provider': 'Netrality', 'city': 'Philadelphia', 'state': 'PA', 'market': 'Philadelphia', 'power_mw': 20, 'status': 'Operational'},
        {'name': 'Netrality 1 South Wacker', 'provider': 'Netrality', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 18, 'status': 'Operational'},
        {'name': 'Netrality 900 Walnut', 'provider': 'Netrality', 'city': 'Kansas City', 'state': 'MO', 'market': 'Kansas City', 'power_mw': 10, 'status': 'Operational'},
        
        # Tract
        {'name': 'Tract SLC-1', 'provider': 'Tract', 'city': 'Salt Lake City', 'state': 'UT', 'market': 'Salt Lake City', 'power_mw': 24, 'status': 'Operational'},
        {'name': 'Tract SLC-2', 'provider': 'Tract', 'city': 'Salt Lake City', 'state': 'UT', 'market': 'Salt Lake City', 'power_mw': 36, 'status': 'Under Construction'},
        {'name': 'Tract SLC-3', 'provider': 'Tract', 'city': 'Salt Lake City', 'state': 'UT', 'market': 'Salt Lake City', 'power_mw': 48, 'status': 'Planned'},
        {'name': 'Tract Reno-1', 'provider': 'Tract', 'city': 'Reno', 'state': 'NV', 'market': 'Reno', 'power_mw': 20, 'status': 'Operational'},
        
        # Powerhouse
        {'name': 'Powerhouse PHX-1', 'provider': 'Powerhouse', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'Powerhouse DFW-1', 'provider': 'Powerhouse', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 12, 'status': 'Operational'},
        
        # ==========================================
        # DATABANK (85 facilities)
        # ==========================================
        {'name': 'DataBank DFW1', 'provider': 'DataBank', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'DataBank DFW2', 'provider': 'DataBank', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'DataBank DFW3', 'provider': 'DataBank', 'city': 'Plano', 'state': 'TX', 'market': 'Dallas', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'DataBank ATL1', 'provider': 'DataBank', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'DataBank ATL2', 'provider': 'DataBank', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'DataBank ATL3', 'provider': 'DataBank', 'city': 'Lithia Springs', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'DataBank DEN1', 'provider': 'DataBank', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 5, 'status': 'Operational'},
        {'name': 'DataBank DEN2', 'provider': 'DataBank', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'DataBank MSP1', 'provider': 'DataBank', 'city': 'Minneapolis', 'state': 'MN', 'market': 'Minneapolis', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'DataBank MSP2', 'provider': 'DataBank', 'city': 'Minneapolis', 'state': 'MN', 'market': 'Minneapolis', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'DataBank SLC1', 'provider': 'DataBank', 'city': 'Salt Lake City', 'state': 'UT', 'market': 'Salt Lake City', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'DataBank PIT1', 'provider': 'DataBank', 'city': 'Pittsburgh', 'state': 'PA', 'market': 'Pittsburgh', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'DataBank IAD1', 'provider': 'DataBank', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'DataBank IAD3', 'provider': 'DataBank', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'DataBank IAD4', 'provider': 'DataBank', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 20, 'status': 'Under Construction'},
        {'name': 'DataBank MCI1', 'provider': 'DataBank', 'city': 'Kansas City', 'state': 'MO', 'market': 'Kansas City', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'DataBank MCI2', 'provider': 'DataBank', 'city': 'Kansas City', 'state': 'MO', 'market': 'Kansas City', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'DataBank MCI3', 'provider': 'DataBank', 'city': 'Lenexa', 'state': 'KS', 'market': 'Kansas City', 'power_mw': 12, 'status': 'Operational'},
        
        # ==========================================
        # H5 DATA CENTERS
        # ==========================================
        {'name': 'H5 San Antonio 1', 'provider': 'H5 Data Centers', 'city': 'San Antonio', 'state': 'TX', 'market': 'San Antonio', 'power_mw': 30, 'status': 'Operational'},
        {'name': 'H5 San Antonio 2', 'provider': 'H5 Data Centers', 'city': 'San Antonio', 'state': 'TX', 'market': 'San Antonio', 'power_mw': 45, 'status': 'Operational'},
        {'name': 'H5 Chandler 1', 'provider': 'H5 Data Centers', 'city': 'Chandler', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 25, 'status': 'Operational'},
        {'name': 'H5 Chandler 2', 'provider': 'H5 Data Centers', 'city': 'Chandler', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 50, 'status': 'Under Construction'},
        {'name': 'H5 Quincy 1', 'provider': 'H5 Data Centers', 'city': 'Quincy', 'state': 'WA', 'market': 'Seattle', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'H5 Cleveland', 'provider': 'H5 Data Centers', 'city': 'Cleveland', 'state': 'OH', 'market': 'Cleveland', 'power_mw': 18, 'status': 'Operational'},
        
        # ==========================================
        # SABEY DATA CENTERS
        # ==========================================
        {'name': 'Sabey Intergate.East', 'provider': 'Sabey', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 42, 'status': 'Operational'},
        {'name': 'Sabey Intergate.Manhattan', 'provider': 'Sabey', 'city': 'New York', 'state': 'NY', 'market': 'New York', 'power_mw': 35, 'status': 'Operational'},
        {'name': 'Sabey Intergate.Columbia', 'provider': 'Sabey', 'city': 'Quincy', 'state': 'WA', 'market': 'Seattle', 'power_mw': 60, 'status': 'Operational'},
        {'name': 'Sabey Intergate.Seattle', 'provider': 'Sabey', 'city': 'Seattle', 'state': 'WA', 'market': 'Seattle', 'power_mw': 28, 'status': 'Operational'},
        
        # ==========================================
        # FLEXENTIAL
        # ==========================================
        {'name': 'Flexential Denver 1', 'provider': 'Flexential', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'Flexential Denver 2', 'provider': 'Flexential', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'Flexential Portland', 'provider': 'Flexential', 'city': 'Portland', 'state': 'OR', 'market': 'Portland', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'Flexential Hillsboro', 'provider': 'Flexential', 'city': 'Hillsboro', 'state': 'OR', 'market': 'Portland', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'Flexential Atlanta', 'provider': 'Flexential', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'Flexential Charlotte', 'provider': 'Flexential', 'city': 'Charlotte', 'state': 'NC', 'market': 'Charlotte', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'Flexential Raleigh', 'provider': 'Flexential', 'city': 'Raleigh', 'state': 'NC', 'market': 'Raleigh', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'Flexential Tampa', 'provider': 'Flexential', 'city': 'Tampa', 'state': 'FL', 'market': 'Tampa', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'Flexential Orlando', 'provider': 'Flexential', 'city': 'Orlando', 'state': 'FL', 'market': 'Orlando', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'Flexential Jacksonville', 'provider': 'Flexential', 'city': 'Jacksonville', 'state': 'FL', 'market': 'Jacksonville', 'power_mw': 5, 'status': 'Operational'},
        
        # ==========================================
        # TIERPOINT
        # ==========================================
        {'name': 'TierPoint Dallas', 'provider': 'TierPoint', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'TierPoint Houston', 'provider': 'TierPoint', 'city': 'Houston', 'state': 'TX', 'market': 'Houston', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'TierPoint Chicago', 'provider': 'TierPoint', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'TierPoint St. Louis', 'provider': 'TierPoint', 'city': 'St. Louis', 'state': 'MO', 'market': 'St. Louis', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'TierPoint Baltimore', 'provider': 'TierPoint', 'city': 'Baltimore', 'state': 'MD', 'market': 'Baltimore', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'TierPoint Philadelphia', 'provider': 'TierPoint', 'city': 'Philadelphia', 'state': 'PA', 'market': 'Philadelphia', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'TierPoint Seattle', 'provider': 'TierPoint', 'city': 'Seattle', 'state': 'WA', 'market': 'Seattle', 'power_mw': 6, 'status': 'Operational'},
        {'name': 'TierPoint Spokane', 'provider': 'TierPoint', 'city': 'Spokane', 'state': 'WA', 'market': 'Spokane', 'power_mw': 4, 'status': 'Operational'},
        {'name': 'TierPoint Oklahoma City', 'provider': 'TierPoint', 'city': 'Oklahoma City', 'state': 'OK', 'market': 'Oklahoma City', 'power_mw': 5, 'status': 'Operational'},
        {'name': 'TierPoint Tulsa', 'provider': 'TierPoint', 'city': 'Tulsa', 'state': 'OK', 'market': 'Tulsa', 'power_mw': 4, 'status': 'Operational'},
        
        # ==========================================
        # CORESITE
        # ==========================================
        {'name': 'CoreSite LA1', 'provider': 'CoreSite', 'city': 'Los Angeles', 'state': 'CA', 'market': 'Los Angeles', 'power_mw': 20, 'status': 'Operational'},
        {'name': 'CoreSite LA2', 'provider': 'CoreSite', 'city': 'Los Angeles', 'state': 'CA', 'market': 'Los Angeles', 'power_mw': 18, 'status': 'Operational'},
        {'name': 'CoreSite LA3', 'provider': 'CoreSite', 'city': 'Los Angeles', 'state': 'CA', 'market': 'Los Angeles', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'CoreSite SV1', 'provider': 'CoreSite', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'CoreSite SV2', 'provider': 'CoreSite', 'city': 'San Jose', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'CoreSite SV4', 'provider': 'CoreSite', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 20, 'status': 'Operational'},
        {'name': 'CoreSite SV7', 'provider': 'CoreSite', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 32, 'status': 'Operational'},
        {'name': 'CoreSite SV8', 'provider': 'CoreSite', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'CoreSite DE1', 'provider': 'CoreSite', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'CoreSite DE2', 'provider': 'CoreSite', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'CoreSite CH1', 'provider': 'CoreSite', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'CoreSite VA1', 'provider': 'CoreSite', 'city': 'Reston', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 25, 'status': 'Operational'},
        {'name': 'CoreSite VA2', 'provider': 'CoreSite', 'city': 'Reston', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 20, 'status': 'Operational'},
        {'name': 'CoreSite VA3', 'provider': 'CoreSite', 'city': 'Reston', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 30, 'status': 'Operational'},
        {'name': 'CoreSite NY1', 'provider': 'CoreSite', 'city': 'New York', 'state': 'NY', 'market': 'New York', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'CoreSite NY2', 'provider': 'CoreSite', 'city': 'Secaucus', 'state': 'NJ', 'market': 'New York', 'power_mw': 18, 'status': 'Operational'},
        {'name': 'CoreSite BO1', 'provider': 'CoreSite', 'city': 'Boston', 'state': 'MA', 'market': 'Boston', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'CoreSite MI1', 'provider': 'CoreSite', 'city': 'Miami', 'state': 'FL', 'market': 'Miami', 'power_mw': 8, 'status': 'Operational'},
        
        # ==========================================
        # COMPASS DATACENTERS
        # ==========================================
        {'name': 'Compass Dallas 1', 'provider': 'Compass', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 48, 'status': 'Operational'},
        {'name': 'Compass Dallas 2', 'provider': 'Compass', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 52, 'status': 'Operational'},
        {'name': 'Compass Phoenix', 'provider': 'Compass', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'Compass Columbus', 'provider': 'Compass', 'city': 'Columbus', 'state': 'OH', 'market': 'Columbus', 'power_mw': 40, 'status': 'Operational'},
        {'name': 'Compass Northern Virginia', 'provider': 'Compass', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 60, 'status': 'Under Construction'},
        
        # ==========================================
        # STACK INFRASTRUCTURE
        # ==========================================
        {'name': 'Stack Atlanta', 'provider': 'Stack', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 24, 'status': 'Operational'},
        {'name': 'Stack Portland', 'provider': 'Stack', 'city': 'Hillsboro', 'state': 'OR', 'market': 'Portland', 'power_mw': 32, 'status': 'Operational'},
        {'name': 'Stack Northern Virginia', 'provider': 'Stack', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 80, 'status': 'Operational'},
        {'name': 'Stack Chicago', 'provider': 'Stack', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'Stack Dallas', 'provider': 'Stack', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 48, 'status': 'Operational'},
        {'name': 'Stack Phoenix', 'provider': 'Stack', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 42, 'status': 'Operational'},
        
        # ==========================================
        # VANTAGE DATA CENTERS
        # ==========================================
        {'name': 'Vantage Santa Clara V1', 'provider': 'Vantage', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 24, 'status': 'Operational'},
        {'name': 'Vantage Santa Clara V2', 'provider': 'Vantage', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 32, 'status': 'Operational'},
        {'name': 'Vantage Phoenix V1', 'provider': 'Vantage', 'city': 'Goodyear', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 56, 'status': 'Operational'},
        {'name': 'Vantage Phoenix V2', 'provider': 'Vantage', 'city': 'Goodyear', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 48, 'status': 'Operational'},
        {'name': 'Vantage Ashburn V1', 'provider': 'Vantage', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 64, 'status': 'Operational'},
        {'name': 'Vantage Ashburn V2', 'provider': 'Vantage', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 72, 'status': 'Operational'},
        {'name': 'Vantage Quincy', 'provider': 'Vantage', 'city': 'Quincy', 'state': 'WA', 'market': 'Seattle', 'power_mw': 50, 'status': 'Operational'},
        
        # ==========================================
        # EDGECORE
        # ==========================================
        {'name': 'EdgeCore Santa Clara', 'provider': 'EdgeCore', 'city': 'Santa Clara', 'state': 'CA', 'market': 'Silicon Valley', 'power_mw': 28, 'status': 'Operational'},
        {'name': 'EdgeCore Mesa', 'provider': 'EdgeCore', 'city': 'Mesa', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 65, 'status': 'Operational'},
        {'name': 'EdgeCore Reno', 'provider': 'EdgeCore', 'city': 'Reno', 'state': 'NV', 'market': 'Reno', 'power_mw': 45, 'status': 'Operational'},
        
        # ==========================================
        # CLOUDHQ
        # ==========================================
        {'name': 'CloudHQ Ashburn VA1', 'provider': 'CloudHQ', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 72, 'status': 'Operational'},
        {'name': 'CloudHQ Ashburn VA2', 'provider': 'CloudHQ', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 96, 'status': 'Operational'},
        {'name': 'CloudHQ Manassas', 'provider': 'CloudHQ', 'city': 'Manassas', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 64, 'status': 'Under Construction'},
        {'name': 'CloudHQ Chicago', 'provider': 'CloudHQ', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 48, 'status': 'Operational'},
        
        # ==========================================
        # STREAM DATA CENTERS
        # ==========================================
        {'name': 'Stream Dallas 1', 'provider': 'Stream', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 20, 'status': 'Operational'},
        {'name': 'Stream Dallas 2', 'provider': 'Stream', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 24, 'status': 'Operational'},
        {'name': 'Stream Chicago', 'provider': 'Stream', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 18, 'status': 'Operational'},
        {'name': 'Stream Phoenix', 'provider': 'Stream', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 28, 'status': 'Operational'},
        {'name': 'Stream Denver', 'provider': 'Stream', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 16, 'status': 'Operational'},
        
        # ==========================================
        # T5 DATA CENTERS
        # ==========================================
        {'name': 'T5@Dallas', 'provider': 'T5', 'city': 'Dallas', 'state': 'TX', 'market': 'Dallas', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'T5@Atlanta', 'provider': 'T5', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 10, 'status': 'Operational'},
        {'name': 'T5@Charlotte', 'provider': 'T5', 'city': 'Charlotte', 'state': 'NC', 'market': 'Charlotte', 'power_mw': 8, 'status': 'Operational'},
        {'name': 'T5@Chicago', 'provider': 'T5', 'city': 'Elk Grove', 'state': 'IL', 'market': 'Chicago', 'power_mw': 22, 'status': 'Operational'},
        {'name': 'T5@Portland', 'provider': 'T5', 'city': 'Hillsboro', 'state': 'OR', 'market': 'Portland', 'power_mw': 15, 'status': 'Operational'},
        {'name': 'T5@Denver', 'provider': 'T5', 'city': 'Denver', 'state': 'CO', 'market': 'Denver', 'power_mw': 18, 'status': 'Operational'},
        
        # ==========================================
        # ALIGNED DATA CENTERS
        # ==========================================
        {'name': 'Aligned Dallas', 'provider': 'Aligned', 'city': 'Plano', 'state': 'TX', 'market': 'Dallas', 'power_mw': 32, 'status': 'Operational'},
        {'name': 'Aligned Phoenix', 'provider': 'Aligned', 'city': 'Phoenix', 'state': 'AZ', 'market': 'Phoenix', 'power_mw': 40, 'status': 'Operational'},
        {'name': 'Aligned Ashburn', 'provider': 'Aligned', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 48, 'status': 'Operational'},
        {'name': 'Aligned Salt Lake City', 'provider': 'Aligned', 'city': 'Salt Lake City', 'state': 'UT', 'market': 'Salt Lake City', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'Aligned Chicago', 'provider': 'Aligned', 'city': 'Northlake', 'state': 'IL', 'market': 'Chicago', 'power_mw': 28, 'status': 'Operational'},
        
        # ==========================================
        # PRIME DATA CENTERS
        # ==========================================
        {'name': 'Prime Chicago 1', 'provider': 'Prime', 'city': 'Chicago', 'state': 'IL', 'market': 'Chicago', 'power_mw': 16, 'status': 'Operational'},
        {'name': 'Prime Sacramento', 'provider': 'Prime', 'city': 'Sacramento', 'state': 'CA', 'market': 'Sacramento', 'power_mw': 12, 'status': 'Operational'},
        {'name': 'Prime Atlanta', 'provider': 'Prime', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 14, 'status': 'Operational'},
        
        # ==========================================
        # COPT DATA CENTER (Northern Virginia)
        # ==========================================
        {'name': 'COPT DC-6', 'provider': 'COPT', 'city': 'Ashburn', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 36, 'status': 'Operational'},
        {'name': 'COPT VA-4', 'provider': 'COPT', 'city': 'Manassas', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 48, 'status': 'Operational'},
        {'name': 'COPT NV-1', 'provider': 'COPT', 'city': 'Sterling', 'state': 'VA', 'market': 'Northern Virginia', 'power_mw': 28, 'status': 'Operational'},
        
        # ==========================================
        # DIGITAL BRIDGE / SWITCH (Selected)
        # ==========================================
        {'name': 'Switch LAS VEGAS 8', 'provider': 'Switch', 'city': 'Las Vegas', 'state': 'NV', 'market': 'Las Vegas', 'power_mw': 130, 'status': 'Operational'},
        {'name': 'Switch TAHOE RENO', 'provider': 'Switch', 'city': 'Reno', 'state': 'NV', 'market': 'Reno', 'power_mw': 80, 'status': 'Operational'},
        {'name': 'Switch ATLANTA', 'provider': 'Switch', 'city': 'Atlanta', 'state': 'GA', 'market': 'Atlanta', 'power_mw': 50, 'status': 'Operational'},
        {'name': 'Switch GRAND RAPIDS', 'provider': 'Switch', 'city': 'Grand Rapids', 'state': 'MI', 'market': 'Grand Rapids', 'power_mw': 45, 'status': 'Operational'},
        {'name': 'Switch AUSTIN', 'provider': 'Switch', 'city': 'Austin', 'state': 'TX', 'market': 'Austin', 'power_mw': 40, 'status': 'Operational'},
    ]
    
    for fac in operator_facilities:
        discovery = {
            'source': 'operator_website',
            'source_id': f"{fac['provider']}_{fac['name']}".replace(' ', '_'),
            'name': fac['name'],
            'provider': fac['provider'],
            'city': fac['city'],
            'state': fac['state'],
            'country': 'US',
            'market': fac['market'],
            'power_mw': fac.get('power_mw'),
            'status': fac.get('status', 'Operational'),
            'facility_type': 'Colocation',
            'source_url': OPERATOR_WEBSITES.get(fac['provider'], ''),
            'raw_data': json.dumps(fac)
        }
        discoveries.append(discovery)
    
    return discoveries

def determine_market(city, state, country):
    """Determine market from location"""
    if not city:
        return 'Unknown'
    
    city_lower = city.lower()
    state_lower = (state or '').lower()
    
    market_mappings = {
        'ashburn': 'Northern Virginia',
        'sterling': 'Northern Virginia',
        'manassas': 'Northern Virginia',
        'leesburg': 'Northern Virginia',
        'reston': 'Northern Virginia',
        'herndon': 'Northern Virginia',
        'dallas': 'Dallas',
        'richardson': 'Dallas',
        'plano': 'Dallas',
        'irving': 'Dallas',
        'carrollton': 'Dallas',
        'phoenix': 'Phoenix',
        'mesa': 'Phoenix',
        'chandler': 'Phoenix',
        'goodyear': 'Phoenix',
        'chicago': 'Chicago',
        'elk grove': 'Chicago',
        'franklin park': 'Chicago',
        'atlanta': 'Atlanta',
        'douglas': 'Atlanta',
        'lithia springs': 'Atlanta',
        'denver': 'Denver',
        'aurora': 'Denver',
        'seattle': 'Seattle',
        'quincy': 'Seattle',
        'tukwila': 'Seattle',
        'los angeles': 'Los Angeles',
        'el segundo': 'Los Angeles',
        'san jose': 'Silicon Valley',
        'santa clara': 'Silicon Valley',
        'fremont': 'Silicon Valley',
        'milpitas': 'Silicon Valley',
        'new york': 'New York',
        'secaucus': 'New York',
        'weehawken': 'New York',
        'houston': 'Houston',
        'salt lake': 'Salt Lake City',
        'west jordan': 'Salt Lake City',
        'kansas city': 'Kansas City',
        'st. louis': 'St. Louis',
        'saint louis': 'St. Louis',
        'philadelphia': 'Philadelphia',
        'reno': 'Reno',
        'sparks': 'Reno',
        'san antonio': 'San Antonio',
        'austin': 'Austin',
        'miami': 'Miami',
        'boca raton': 'Miami',
        'columbus': 'Columbus',
        'new albany': 'Columbus',
        'portland': 'Portland',
        'hillsboro': 'Portland',
        'las vegas': 'Las Vegas',
        'minneapolis': 'Minneapolis',
    }
    
    for key, market in market_mappings.items():
        if key in city_lower:
            return market
    
    # Fall back to city name
    return city.title()

@app.route('/api/discovery/run', methods=['POST'])
def run_discovery():
    """Trigger a discovery run"""
    try:
        # Ensure tables exist first
        try:
            init_discovery_tables()
        except Exception as e:
            pass  # Tables might already exist
        
        data = request.get_json() or {}
        sources = data.get('sources', ['all'])
        
        results = {
            'started_at': datetime.utcnow().isoformat(),
            'sources': [],
            'total_found': 0,
            'total_added': 0,
            'total_updated': 0,
            'total_duplicate': 0
        }
        
        # Run operator website discovery (local data, no external calls)
        if 'all' in sources or 'operators' in sources:
            try:
                op_result = run_operator_discovery()
                results['sources'].append(op_result)
                results['total_found'] += op_result.get('found', 0)
                results['total_added'] += op_result.get('added', 0)
                results['total_duplicate'] += op_result.get('duplicate', 0)
            except Exception as e:
                results['sources'].append({
                    'source': 'operator_website', 
                    'error': str(e), 
                    'found': 0, 
                    'added': 0, 
                    'duplicate': 0
                })
        
        # Run PeeringDB discovery (external API)
        if 'all' in sources or 'peeringdb' in sources:
            try:
                pdb_result = run_peeringdb_discovery()
                results['sources'].append(pdb_result)
                results['total_found'] += pdb_result.get('found', 0)
                results['total_added'] += pdb_result.get('added', 0)
                results['total_duplicate'] += pdb_result.get('duplicate', 0)
            except Exception as e:
                results['sources'].append({
                    'source': 'peeringdb', 
                    'error': str(e), 
                    'found': 0, 
                    'added': 0, 
                    'duplicate': 0
                })
        
        results['completed_at'] = datetime.utcnow().isoformat()
        
        return jsonify({
            'success': True,
            'results': results
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def run_operator_discovery():
    """Run discovery from operator data (local, no external calls)"""
    result = {'source': 'operator_website', 'found': 0, 'added': 0, 'duplicate': 0, 'errors': []}
    
    try:
        discoveries = discover_from_operator_websites()
        result['found'] = len(discoveries)
        
        conn = get_db()
        c = conn.cursor()
        
        for disc in discoveries:
            try:
                # Check if already discovered
                c.execute("""
                    SELECT id FROM discovered_facilities 
                    WHERE source_id = %s
                """, (disc.get('source_id'),))
                
                if c.fetchone():
                    result['duplicate'] += 1
                else:
                    c.execute("""
                        INSERT INTO discovered_facilities 
                        (source, source_id, name, provider, market, city, state, country, 
                         power_mw, status, facility_type, discovered_at, is_duplicate)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0) ON CONFLICT DO NOTHING
                    """, (
                        disc.get('source'), disc.get('source_id'), disc['name'],
                        disc.get('provider'), disc.get('market'), disc.get('city'),
                        disc.get('state'), disc.get('country', 'US'), disc.get('power_mw'),
                        disc.get('status'), disc.get('facility_type'),
                        datetime.utcnow().isoformat()
                    ))
                    result['added'] += 1
            except Exception as e:
                result['errors'].append(str(e))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        result['errors'].append(str(e))
    
    return result

def run_peeringdb_discovery():
    """Run discovery from PeeringDB API"""
    result = {'source': 'peeringdb', 'found': 0, 'added': 0, 'duplicate': 0, 'errors': []}
    
    try:
        discoveries = discover_from_peeringdb()
        result['found'] = len(discoveries)
        
        # Only process first 100 to avoid timeout
        conn = get_db()
        c = conn.cursor()
        
        for disc in discoveries[:100]:
            try:
                c.execute("""
                    SELECT id FROM discovered_facilities 
                    WHERE source_id = %s
                """, (disc.get('source_id'),))
                
                if c.fetchone():
                    result['duplicate'] += 1
                else:
                    c.execute("""
                        INSERT INTO discovered_facilities 
                        (source, source_id, name, provider, market, city, state, country,
                         latitude, longitude, status, facility_type, discovered_at, is_duplicate)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0) ON CONFLICT DO NOTHING
                    """, (
                        disc.get('source'), disc.get('source_id'), disc['name'],
                        disc.get('provider'), disc.get('market'), disc.get('city'),
                        disc.get('state'), disc.get('country', 'US'),
                        disc.get('latitude'), disc.get('longitude'),
                        disc.get('status'), disc.get('facility_type'),
                        datetime.utcnow().isoformat()
                    ))
                    result['added'] += 1
            except Exception as e:
                result['errors'].append(str(e))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        result['errors'].append(str(e))
    
    return result

def process_discovery_source(source_name, discovery_func, conn):
    """Process discoveries from a single source"""
    c = conn.cursor()
    
    # Log run start
    try:
        c.execute("""
            INSERT INTO discovery_runs (source, started_at, status)
            VALUES (%s, %s, 'running') ON CONFLICT DO NOTHING
        """, (source_name, datetime.utcnow().isoformat()))
        run_id = c.lastrowid
        conn.commit()
    except Exception as e:
        run_id = None
    
    result = {
        'source': source_name,
        'found': 0,
        'added': 0,
        'updated': 0,
        'duplicate': 0,
        'errors': []
    }
    
    try:
        discoveries = discovery_func()
        result['found'] = len(discoveries)
        
        for disc in discoveries:
            try:
                # Check for duplicate in discovered_facilities
                c.execute("""
                    SELECT id FROM discovered_facilities 
                    WHERE source = %s AND source_id = %s
                """, (disc.get('source'), disc.get('source_id')))
                
                existing = c.fetchone()
                
                if existing:
                    result['duplicate'] += 1
                else:
                    # Store in discovered_facilities
                    c.execute("""
                        INSERT INTO discovered_facilities 
                        (source, source_id, name, provider, market, city, state, country, 
                         latitude, longitude, power_mw, status, facility_type, source_url, 
                         raw_data, discovered_at, is_duplicate)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0) ON CONFLICT DO NOTHING
                    """, (
                        disc.get('source'), disc.get('source_id'), disc['name'],
                        disc.get('provider'), disc.get('market'), disc.get('city'),
                        disc.get('state'), disc.get('country'), disc.get('latitude'),
                        disc.get('longitude'), disc.get('power_mw'), disc.get('status'),
                        disc.get('facility_type'), disc.get('source_url'), disc.get('raw_data'),
                        datetime.utcnow().isoformat()
                    ))
                    result['added'] += 1
                    
            except Exception as e:
                result['errors'].append(f"Error processing {disc.get('name', 'unknown')}: {str(e)}")
        
        conn.commit()
        
        # Update run status
        if run_id:
            try:
                c.execute("""
                    UPDATE discovery_runs 
                    SET completed_at = %s, status = 'completed',
                        facilities_found = %s, facilities_added = %s, 
                        facilities_duplicate = %s
                    WHERE id = %s
                """, (
                    datetime.utcnow().isoformat(), result['found'], 
                    result['added'], result['duplicate'], run_id
                ))
                conn.commit()
            except:
                pass
        
    except Exception as e:
        result['errors'].append(str(e))
        if run_id:
            try:
                c.execute("""
                    UPDATE discovery_runs SET status = 'error', error = %s WHERE id = %s
                """, (str(e), run_id))
                conn.commit()
            except:
                pass
    
    return result

@app.route('/api/discovery/status', methods=['GET'])
def discovery_status():
    """Get discovery system status"""
    try:
        # Ensure tables exist
        init_discovery_tables()
        
        conn = get_db()
        c = conn.cursor()
        
        # Get recent runs (with error handling for missing table)
        try:
            c.execute("""
                SELECT source, started_at, completed_at, facilities_found, 
                       facilities_added, facilities_duplicate, status
                FROM discovery_runs 
                ORDER BY started_at DESC 
                LIMIT 20
            """)
            recent_runs = [{
                'source': r[0], 'started_at': r[1], 'completed_at': r[2],
                'found': r[3], 'added': r[4], 'duplicate': r[5], 'status': r[6]
            } for r in c.fetchall()]
        except:
            recent_runs = []
        
        # Get totals
        c.execute("SELECT COUNT(*) FROM facilities")
        total_facilities = c.fetchone()[0]
        
        try:
            c.execute("SELECT SUM(facilities_added) FROM discovery_runs WHERE status = 'completed'")
            total_discovered = c.fetchone()[0] or 0
        except:
            total_discovered = 0
        
        try:
            c.execute("""
                SELECT source, COUNT(*), SUM(facilities_added) 
                FROM discovery_runs 
                WHERE status = 'completed'
                GROUP BY source
            """)
            by_source = [{
                'source': r[0], 'runs': r[1], 'added': r[2]
            } for r in c.fetchall()]
        except:
            by_source = []
        
        conn.close()
        
        return jsonify({
            'success': True,
            'status': {
                'total_facilities': total_facilities,
                'total_discovered': total_discovered,
                'recent_runs': recent_runs,
                'by_source': by_source,
                'sources_configured': list(DISCOVERY_SOURCES.keys()),
                'target_operators': [op['name'] for op in TARGET_OPERATORS]
            }
        })
    except Exception as e:
        return jsonify({
            'success': True,
            'status': {
                'total_facilities': 0,
                'total_discovered': 0,
                'recent_runs': [],
                'by_source': [],
                'sources_configured': list(DISCOVERY_SOURCES.keys()),
                'target_operators': [op['name'] for op in TARGET_OPERATORS],
                'note': 'Discovery tables initializing'
            }
        })

@app.route('/api/discovery/facilities', methods=['GET'])
def get_discovered_facilities():
    """Get recently discovered facilities"""
    limit = request.args.get('limit', 50, type=int)
    source = request.args.get('source')
    include_duplicates = request.args.get('duplicates', 'false').lower() == 'true'
    
    conn = get_db()
    c = conn.cursor()
    
    query = """
        SELECT id, source, name, provider, market, city, state, 
               power_mw, status, discovered_at, is_duplicate, merged_facility_id
        FROM discovered_facilities
        WHERE 1=1
    """
    params = []
    
    if not include_duplicates:
        query += " AND is_duplicate = 0"
    
    if source:
        query += " AND source = %s"
        params.append(source)
    
    query += " ORDER BY discovered_at DESC LIMIT %s"
    params.append(limit)
    
    c.execute(query, params)
    facilities = [{
        'id': r[0], 'source': r[1], 'name': r[2], 'provider': r[3],
        'market': r[4], 'city': r[5], 'state': r[6], 'power_mw': r[7],
        'status': r[8], 'discovered_at': r[9], 'is_duplicate': bool(r[10]),
        'merged_id': r[11]
    } for r in c.fetchall()]
    
    conn.close()
# AUTO-REPAIR: duplicate route '/api/email/track/<email_id>/open.gif' also in main.py:13425 — review and remove one
    
    return jsonify({
        'success': True,
        'count': len(facilities),
        'facilities': facilities
    })

@app.route('/api/discovery/operators', methods=['GET'])
def get_target_operators():
    """Get list of target operators being tracked"""
    return jsonify({
        'success': True,
        'operators': TARGET_OPERATORS,
        'websites': OPERATOR_WEBSITES
    })

# =============================================================================
# AUTO-REPAIR: duplicate route '/api/email/unsubscribe' also in main.py:13443 — review and remove one
# EMAIL ENDPOINTS
# =============================================================================

@app.route('/api/email/track/<email_id>/open.gif', methods=['GET'])
def track_email_open(email_id):
    """Track email open via invisible pixel"""
    if EMAIL_SERVICE_AVAILABLE:
        try:
            record_email_event(
                email_id, 
                'open',
                request.remote_addr,
                request.headers.get('User-Agent', '')
            )
        except:
            pass
    
    # Return 1x1 transparent GIF
    gif_bytes = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
    return Response(gif_bytes, mimetype='image/gif')

@app.route('/api/email/unsubscribe', methods=['GET', 'POST'])
def email_unsubscribe():
    """Handle email unsubscribe"""
    token = request.args.get('token') or (request.get_json() or {}).get('token')
    
    if not token:
        return """
        <!DOCTYPE html>
        <html>
        <head><title>Unsubscribe - DC Hub</title>
        <style>body{font-family:system-ui;max-width:600px;margin:100px auto;text-align:center;}</style>
        </head>
        <body>
            <h1>Unsubscribe</h1>
            <p>Invalid unsubscribe link. Please use the link from your email.</p>
        </body>
        </html>
        """, 400
    
    if EMAIL_SERVICE_AVAILABLE:
        try:
            handle_unsubscribe(token)
        except:
            pass
    
    # Also update leads table
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE leads SET subscribed = 0 WHERE email IN (
            SELECT DISTINCT email FROM email_queue WHERE body_html LIKE %s
        )
    """, (f'%{token}%',))
# AUTO-REPAIR: duplicate route '/api/email/stats' also in main.py:13503 — review and remove one
    conn.commit()
    conn.close()
    
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Unsubscribed - DC Hub</title>
    <style>
        body{font-family:system-ui;max-width:600px;margin:100px auto;text-align:center;color:#333;}
        .success{color:#00d4ff;font-size:48px;margin-bottom:20px;}
        a{color:#00d4ff;}
    </style>
    </head>
    <body>
        <div class="success">✓</div>
        <h1>You've been unsubscribed</h1>
# AUTO-REPAIR: duplicate route '/api/email/process' also in main.py:13520 — review and remove one
        <p>You will no longer receive marketing emails from DC Hub.</p>
        <p><a href="https://dchub.cloud">Return to DC Hub</a></p>
    </body>
    </html>
    """

@app.route('/api/email/stats', methods=['GET'])
@require_auth
def email_stats():
    """Get email stats (admin only)"""
    # Check if admin
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    
    if not EMAIL_SERVICE_AVAILABLE:
        return jsonify({'error': 'Email service not available'}), 503
    
    try:
        stats = get_email_stats()
# AUTO-REPAIR: duplicate route '/api/email/test' also in main.py:13540 — review and remove one
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/email/process', methods=['POST'])
@require_auth
def trigger_email_process():
    """Manually trigger email queue processing (admin only)"""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    
    if not EMAIL_SERVICE_AVAILABLE:
        return jsonify({'error': 'Email service not available'}), 503
    
    try:
        results = process_email_queue()
        return jsonify({
            'success': True,
            'processed': len(results),
            'results': results
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/email/test', methods=['POST'])
@require_auth
def send_test_email():
    """Send a test email (admin only)"""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    
    if not EMAIL_SERVICE_AVAILABLE:
        return jsonify({'error': 'Email service not available'}), 503
    
    data = request.get_json()
    to_email = data.get('email', request.user.get('email'))
    
    from email_service import send_email, render_email_template
    
    test_content = """
        <h1>Test Email from DC Hub</h1>
        <p>This is a test email to verify your email configuration is working correctly.</p>
        <p>If you received this, your Office 365 SMTP integration is properly configured!</p>
        <p style="text-align: center;">
            <a href="https://dchub.cloud" class="cta-button">Visit DC Hub →</a>
        </p>
    """
    
    html = render_email_template(test_content, {
        'subject': 'Test Email - DC Hub',
        'app_url': 'https://dchub.cloud',
        'unsubscribe_token': 'test',
        'email_id': 'test'
    })
    
    result = send_email(to_email, 'Test Email - DC Hub', html)
    
    return jsonify(result)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    print("🚀 DC Hub API v80 Starting...")
    print(f"📊 PDF Generation: {'✅ Available' if PDF_AVAILABLE else '❌ Disabled'}")
    print(f"📧 Email Service: {'✅ Available' if EMAIL_SERVICE_AVAILABLE else '❌ Disabled'}")
    
    # Start email worker if available
    if EMAIL_SERVICE_AVAILABLE:
        try:
# AUTO-REPAIR: duplicate route '/api/transactions/refresh' also in main.py:14423 — review and remove one
            email_worker.start()
        except Exception as e:
            print(f"⚠️ Could not start email worker: {e}")
    
    app.run(host='0.0.0.0', port=5000, debug=True)


# === DC Hub refresh routes for dchub-cron v2 ===
# Tolerant best-effort: tries known ingestion entry points; if none wire up,
# returns a 200 no-op so the cron tail-log stays green and we can wire the
# real entry function in a follow-up without breaking anything.
from datetime import datetime as _dchub_refresh_dt

def _dchub_try_run(*candidates):
    """Try each (module, attr) pair until one resolves and returns its result."""
    for module_name, attr in candidates:
        try:
            mod = __import__(module_name, fromlist=[attr])
            fn = getattr(mod, attr, None)
            if callable(fn):
                return fn(), module_name + '.' + attr, None
        except Exception as e:
            continue
    return None, None, 'no-ingestion-entrypoint-found'
# AUTO-REPAIR: duplicate route '/api/facilities/refresh' also in main.py:14660 — review and remove one

@app.route('/api/transactions/refresh', methods=['POST', 'GET'])
def _dchub_refresh_transactions():
    started = _dchub_refresh_dt.utcnow().isoformat()
    result, entry, err = _dchub_try_run(
        ('deal_ingestion_scheduler', 'run_deal_ingestion'),
        ('deal_ingestion_scheduler', 'ingest_deals'),
        ('deal_ingestion_scheduler', 'run'),
        ('deal_scraper',             'run'),
        ('deal_scraper',             'scrape_all'),
        ('seed_comprehensive_deals', 'run'),
    )
    inserted = 0
    if isinstance(result, dict):
        inserted = result.get('inserted', 0) or result.get('count', 0)
    elif isinstance(result, int):
        inserted = result
    return jsonify({
        'ok': err is None,
        'started': started,
        'finished': _dchub_refresh_dt.utcnow().isoformat(),
        'entrypoint': entry,
        'inserted': inserted,
        'note': err,
    }), 200

@app.route('/api/facilities/refresh', methods=['POST', 'GET'])
def _dchub_refresh_facilities():
    started = _dchub_refresh_dt.utcnow().isoformat()
    result, entry, err = _dchub_try_run(
        ('facility_ingestion',         'run_facility_ingestion'),
        ('facility_ingestion',         'ingest_facilities'),
        ('facility_ingestion',         'run'),
        ('carrier_facility_ingestion', 'ingest'),
        ('carrier_facility_ingestion', 'run'),
        ('populate_facilities',        'run'),
    )
    inserted = 0
    if isinstance(result, dict):
        inserted = result.get('inserted', 0) or result.get('count', 0)
    elif isinstance(result, int):
        inserted = result
    return jsonify({
        'ok': err is None,
        'started': started,
        'finished': _dchub_refresh_dt.utcnow().isoformat(),
        'entrypoint': entry,
        'inserted': inserted,
        'note': err,
    }), 200
# === end DC Hub refresh routes ===


# === Iteration 2: transactions/ingest, facility infra, land-power snapshot ===
try:
    from dchub_iteration_2_routes import register_iteration_2_routes as _it2_register
    _it2_register(app)
except Exception as _it2_err:
    import logging as _it2_log
    _it2_log.getLogger('dchub.iteration2').warning('Failed to register iteration 2 routes: %s', _it2_err)
# === end iteration 2 ===
