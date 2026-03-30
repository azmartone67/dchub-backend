"""
DC Hub Ecosystem - Dynamic Partner Directory with AI Enrichment
Companies can self-register and get AI-enhanced profiles
"""

import json
import os
import re
import hashlib
from datetime import datetime
from flask import Blueprint, request, jsonify
from db_utils import get_db

ecosystem_bp = Blueprint('ecosystem', __name__)

DB_PATH = 'dc_nexus.db'

COMPANY_CATEGORIES = [
    'Data Center Operator',
    'Colocation Provider', 
    'Cloud Provider',
    'Hyperscaler',
    'Edge Provider',
    'Connectivity Provider',
    'Power/Energy',
    'Cooling/HVAC',
    'Construction/Development',
    'Real Estate/Investment',
    'Consulting/Advisory',
    'Technology Vendor',
    'Security/Compliance',
    'Managed Services',
    'Other'
]

def init_ecosystem_tables():
    """Initialize ecosystem database tables"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ecosystem_companies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                category TEXT,
                subcategory TEXT,
                website TEXT,
                logo_url TEXT,
                headquarters TEXT,
                markets TEXT,
                services TEXT,
                contact_email TEXT,
                linkedin_url TEXT,
                twitter_url TEXT,
                founded_year INTEGER,
                employee_count TEXT,
                facility_count INTEGER,
                total_mw REAL,
                verified INTEGER DEFAULT 0,
                featured INTEGER DEFAULT 0,
                ai_enriched INTEGER DEFAULT 0,
                ai_summary TEXT,
                ai_keywords TEXT,
                submitted_by TEXT,
                submitted_at TEXT,
                approved_at TEXT,
                updated_at TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ecosystem_submissions (
                id SERIAL PRIMARY KEY,
                company_id TEXT,
                submitted_at TEXT,
                submitter_email TEXT,
                submitter_name TEXT,
                ip_address TEXT,
                status TEXT DEFAULT 'pending',
                notes TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ecosystem_integrations (
                id TEXT PRIMARY KEY,
                company_id TEXT,
                company_name TEXT,
                api_key TEXT UNIQUE,
                api_endpoint TEXT,
                webhook_url TEXT,
                data_types TEXT,
                sync_direction TEXT DEFAULT 'pull',
                sync_frequency TEXT DEFAULT 'daily',
                last_sync TEXT,
                sync_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT,
                contact_email TEXT,
                documentation_url TEXT,
                FOREIGN KEY (company_id) REFERENCES ecosystem_companies(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS integration_logs (
                id SERIAL PRIMARY KEY,
                integration_id TEXT,
                action TEXT,
                direction TEXT,
                records_count INTEGER,
                status TEXT,
                error_message TEXT,
                timestamp TEXT,
                FOREIGN KEY (integration_id) REFERENCES ecosystem_integrations(id)
            )
        ''')

        conn.commit()
    finally:
        conn.close()
    print("✅ Ecosystem tables initialized")

def generate_company_id(name):
    """Generate unique company ID from name"""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    hash_suffix = hashlib.md5(name.encode()).hexdigest()[:6]
    return f"{slug}-{hash_suffix}"

def ai_enrich_company(company_data):
    """Use AI to enrich company profile"""
    try:
        import anthropic
        client = anthropic.Anthropic()
        
        prompt = f"""Analyze this data center industry company and provide enrichment:

Company: {company_data.get('name')}
Description: {company_data.get('description', 'N/A')}
Website: {company_data.get('website', 'N/A')}
Category: {company_data.get('category', 'Unknown')}

Provide a JSON response with:
1. "summary": A professional 2-sentence summary of this company's role in the data center ecosystem
2. "category": Best category from: {', '.join(COMPANY_CATEGORIES)}
3. "subcategory": A more specific subcategory
4. "keywords": Array of 5-8 relevant keywords for search
5. "markets": Array of key geographic markets they likely serve
6. "services": Array of 3-5 main services/products

Return ONLY valid JSON, no markdown."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        
        result = json.loads(response.content[0].text)
        return {
            'ai_summary': result.get('summary', ''),
            'category': result.get('category', company_data.get('category')),
            'subcategory': result.get('subcategory', ''),
            'ai_keywords': json.dumps(result.get('keywords', [])),
            'markets': json.dumps(result.get('markets', [])),
            'services': json.dumps(result.get('services', [])),
            'ai_enriched': 1
        }
    except Exception as e:
        print(f"AI enrichment error: {e}")
        return {'ai_enriched': 0}

@ecosystem_bp.route('/api/ecosystem/categories', methods=['GET'])
def get_categories():
    """Get available company categories"""
    return jsonify({
        'categories': COMPANY_CATEGORIES,
        'success': True
    })

@ecosystem_bp.route('/api/ecosystem', methods=['GET'])
def list_companies():
    """List all ecosystem companies with filtering"""
    category = request.args.get('category')
    search = request.args.get('search', '').lower()
    status = request.args.get('status', 'approved')
    featured = request.args.get('featured')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        query = "SELECT * FROM ecosystem_companies WHERE 1=1"
        params = []

        if status:
            query += " AND status = %s"
            params.append(status)

        if category:
            query += " AND category = %s"
            params.append(category)

        if featured:
            query += " AND featured = 1"

        if search:
            query += " AND (LOWER(name) LIKE %s OR LOWER(description) LIKE %s OR LOWER(ai_keywords) LIKE %s)"
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        count_query = query.replace("SELECT *", "SELECT COUNT(*)")
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]

        query += " ORDER BY featured DESC, verified DESC, name ASC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()

        companies = []
        for row in rows:
            company = dict(row)
            for field in ['markets', 'services', 'ai_keywords']:
                if company.get(field):
                    try:
                        company[field] = json.loads(company[field])
                    except:
                        pass
            companies.append(company)

    finally:
        conn.close()
    
    return jsonify({
        'companies': companies,
        'total': total,
        'limit': limit,
        'offset': offset,
        'success': True
    })

@ecosystem_bp.route('/api/ecosystem/<company_id>', methods=['GET'])
def get_company(company_id):
    """Get single company details"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM ecosystem_companies WHERE id = %s", (company_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    
    if not row:
        return jsonify({'error': 'Company not found', 'success': False}), 404
    
    company = dict(row)
    for field in ['markets', 'services', 'ai_keywords']:
        if company.get(field):
            try:
                company[field] = json.loads(company[field])
            except:
                pass
    
    return jsonify({'company': company, 'success': True})

@ecosystem_bp.route('/api/ecosystem', methods=['POST'])
def submit_company():
    """Submit a new company to the ecosystem"""
    data = request.get_json()
    
    if not data.get('name'):
        return jsonify({'error': 'Company name is required', 'success': False}), 400
    
    if not data.get('contact_email'):
        return jsonify({'error': 'Contact email is required', 'success': False}), 400
    
    company_id = generate_company_id(data['name'])
    now = datetime.utcnow().isoformat()
    
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM ecosystem_companies WHERE id = %s", (company_id,))
        if cursor.fetchone():
    finally:
        conn.close()
        return jsonify({'error': 'A company with this name already exists', 'success': False}), 409
    
    enrichment = {}
    if os.environ.get('ANTHROPIC_API_KEY'):
        enrichment = ai_enrich_company(data)
    
    category = enrichment.get('category', data.get('category', 'Other'))
    
    cursor.execute('''
        INSERT INTO ecosystem_companies (
            id, name, description, category, subcategory, website, logo_url,
            headquarters, markets, services, contact_email, linkedin_url,
            twitter_url, founded_year, employee_count, facility_count, total_mw,
            ai_enriched, ai_summary, ai_keywords, submitted_by, submitted_at,
            status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (
        company_id,
        data['name'],
        data.get('description', ''),
        category,
        enrichment.get('subcategory', data.get('subcategory', '')),
        data.get('website', ''),
        data.get('logo_url', ''),
        data.get('headquarters', ''),
        enrichment.get('markets', json.dumps(data.get('markets', []))),
        enrichment.get('services', json.dumps(data.get('services', []))),
        data['contact_email'],
        data.get('linkedin_url', ''),
        data.get('twitter_url', ''),
        data.get('founded_year'),
        data.get('employee_count', ''),
        data.get('facility_count'),
        data.get('total_mw'),
        enrichment.get('ai_enriched', 0),
        enrichment.get('ai_summary', ''),
        enrichment.get('ai_keywords', '[]'),
        data.get('submitted_by', data['contact_email']),
        now,
        'pending'
    ))
    
    cursor.execute('''
        INSERT INTO ecosystem_submissions (
            company_id, submitted_at, submitter_email, submitter_name, status
        ) VALUES (%s, %s, %s, %s, %s)
    ''', (
        company_id,
        now,
        data['contact_email'],
        data.get('submitted_by', ''),
        'pending'
    ))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'company_id': company_id,
        'message': 'Company submitted successfully! It will appear after approval.',
        'ai_enriched': enrichment.get('ai_enriched', 0) == 1
    }), 201

@ecosystem_bp.route('/api/ecosystem/<company_id>/approve', methods=['POST'])
def approve_company(company_id):
    """Approve a company submission (admin only)"""
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    admin_key = os.environ.get('ADMIN_API_KEY', 'dc-hub-admin-2024')
    
    if api_key != admin_key:
        return jsonify({'error': 'Admin access required', 'success': False}), 403
    
    conn = get_db()
    try:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()
        cursor.execute('''
            UPDATE ecosystem_companies
            SET status = 'approved', approved_at = %s, updated_at = %s
            WHERE id = %s
        ''', (now, now, company_id))

        if cursor.rowcount == 0:
    finally:
        conn.close()
        return jsonify({'error': 'Company not found', 'success': False}), 404
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Company approved'})

@ecosystem_bp.route('/api/ecosystem/<company_id>/feature', methods=['POST'])
def feature_company(company_id):
    """Toggle featured status (admin only)"""
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    admin_key = os.environ.get('ADMIN_API_KEY', 'dc-hub-admin-2024')
    
    if api_key != admin_key:
        return jsonify({'error': 'Admin access required', 'success': False}), 403
    
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT featured FROM ecosystem_companies WHERE id = %s", (company_id,))
        row = cursor.fetchone()
        if not row:
    finally:
        conn.close()
        return jsonify({'error': 'Company not found', 'success': False}), 404
    
    new_status = 0 if row[0] else 1
    cursor.execute("UPDATE ecosystem_companies SET featured = %s WHERE id = %s", (new_status, company_id))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'featured': new_status == 1})

@ecosystem_bp.route('/api/ecosystem/stats', methods=['GET'])
def ecosystem_stats():
    """Get ecosystem statistics"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'approved'")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE status = 'pending'")
        pending = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE featured = 1 AND status = 'approved'")
        featured = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies WHERE ai_enriched = 1")
        ai_enriched = cursor.fetchone()[0]

        cursor.execute('''
            SELECT category, COUNT(*) as count
            FROM ecosystem_companies
            WHERE status = 'approved'
            GROUP BY category
            ORDER BY count DESC
        ''')
        by_category = {row[0]: row[1] for row in cursor.fetchall()}

    finally:
        conn.close()
    
    return jsonify({
        'total_companies': total,
        'pending_submissions': pending,
        'featured_companies': featured,
        'ai_enriched': ai_enriched,
        'by_category': by_category,
        'success': True
    })

@ecosystem_bp.route('/api/ecosystem/search', methods=['GET'])
def search_companies():
    """AI-powered search across ecosystem"""
    query = request.args.get('q', '').lower()
    if not query:
        return jsonify({'error': 'Search query required', 'success': False}), 400
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM ecosystem_companies
            WHERE status = 'approved' AND (
                LOWER(name) LIKE ? OR
                LOWER(description) LIKE ? OR
                LOWER(category) LIKE ? OR
                LOWER(ai_summary) LIKE ? OR
                LOWER(ai_keywords) LIKE ? OR
                LOWER(services) LIKE ? OR
                LOWER(markets) LIKE ?
            )
            ORDER BY featured DESC, verified DESC, name ASC
            LIMIT 50
        ''', tuple([f"%{query}%"] * 7))

        rows = cursor.fetchall()
    finally:
        conn.close()
    
    companies = []
    for row in rows:
        company = dict(row)
        for field in ['markets', 'services', 'ai_keywords']:
            if company.get(field):
                try:
                    company[field] = json.loads(company[field])
                except:
                    pass
        companies.append(company)
    
    return jsonify({
        'results': companies,
        'count': len(companies),
        'query': query,
        'success': True
    })

def seed_ecosystem_data():
    """Seed initial ecosystem data"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM ecosystem_companies")
        if cursor.fetchone()[0] > 0:
    finally:
        conn.close()
        return
    
    companies = [
        {
            'name': 'Equinix',
            'description': 'Global digital infrastructure company with 260+ data centers in 71+ metros',
            'category': 'Colocation Provider',
            'website': 'https://equinix.com',
            'headquarters': 'Redwood City, CA',
            'facility_count': 260,
            'verified': 1,
            'featured': 1,
            'status': 'approved'
        },
        {
            'name': 'Digital Realty',
            'description': 'Leading global provider of data center, colocation and interconnection solutions',
            'category': 'Data Center Operator',
            'website': 'https://digitalrealty.com',
            'headquarters': 'Austin, TX',
            'facility_count': 300,
            'verified': 1,
            'featured': 1,
            'status': 'approved'
        },
        {
            'name': 'QTS Data Centers',
            'description': 'Leading provider of hybrid colocation and mega data center solutions',
            'category': 'Colocation Provider',
            'website': 'https://qtsdatacenters.com',
            'headquarters': 'Overland Park, KS',
            'facility_count': 30,
            'verified': 1,
            'featured': 1,
            'status': 'approved'
        },
        {
            'name': 'Vantage Data Centers',
            'description': 'Hyperscale data center campuses across North America, EMEA and APAC',
            'category': 'Hyperscaler',
            'website': 'https://vantage-dc.com',
            'headquarters': 'Denver, CO',
            'facility_count': 25,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'CoreSite',
            'description': 'Premier provider of secure, reliable colocation and interconnection solutions',
            'category': 'Colocation Provider',
            'website': 'https://coresite.com',
            'headquarters': 'Denver, CO',
            'facility_count': 27,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'Aligned Data Centers',
            'description': 'Adaptive colocation data centers with patented cooling technology',
            'category': 'Data Center Operator',
            'website': 'https://aligneddc.com',
            'headquarters': 'Plano, TX',
            'facility_count': 10,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'EdgeCore Internet Real Estate',
            'description': 'Developer and operator of hyperscale data centers',
            'category': 'Data Center Operator',
            'website': 'https://edgecore.com',
            'headquarters': 'Denver, CO',
            'facility_count': 8,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'Flexential',
            'description': 'Hybrid IT solutions provider with colocation, cloud, and connectivity',
            'category': 'Colocation Provider',
            'website': 'https://flexential.com',
            'headquarters': 'Charlotte, NC',
            'facility_count': 40,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'DataBank',
            'description': 'Enterprise-class data center solutions for edge, cloud and core',
            'category': 'Colocation Provider',
            'website': 'https://databank.com',
            'headquarters': 'Dallas, TX',
            'facility_count': 65,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'Compass Datacenters',
            'description': 'Build-to-suit data centers for hyperscale and enterprise customers',
            'category': 'Construction/Development',
            'website': 'https://compassdatacenters.com',
            'headquarters': 'Dallas, TX',
            'facility_count': 15,
            'verified': 1,
            'status': 'approved'
        },
        {
            'name': 'Schneider Electric',
            'description': 'Global leader in energy management and data center infrastructure',
            'category': 'Technology Vendor',
            'website': 'https://se.com',
            'headquarters': 'Paris, France',
            'verified': 1,
            'featured': 1,
            'status': 'approved'
        },
        {
            'name': 'Vertiv',
            'description': 'Critical infrastructure solutions for data centers and communication networks',
            'category': 'Technology Vendor',
            'website': 'https://vertiv.com',
            'headquarters': 'Columbus, OH',
            'verified': 1,
            'status': 'approved'
        }
    ]
    
    now = datetime.utcnow().isoformat()
    for company in companies:
        company_id = generate_company_id(company['name'])
        cursor.execute('''
            INSERT INTO ecosystem_companies (
                id, name, description, category, website, headquarters,
                facility_count, verified, featured, status, submitted_at, approved_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            company_id,
            company['name'],
            company.get('description', ''),
            company.get('category', 'Other'),
            company.get('website', ''),
            company.get('headquarters', ''),
            company.get('facility_count'),
            company.get('verified', 0),
            company.get('featured', 0),
            company.get('status', 'pending'),
            now,
            now if company.get('status') == 'approved' else None
        ))
    
    conn.commit()
    conn.close()
    print(f"✅ Seeded {len(companies)} ecosystem companies")

def generate_api_key():
    """Generate secure API key for integrations"""
    import secrets
    return f"dchub_int_{secrets.token_urlsafe(32)}"

def generate_integration_id(company_name):
    """Generate unique integration ID"""
    slug = re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')
    hash_suffix = hashlib.md5(f"{company_name}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
    return f"int-{slug[:20]}-{hash_suffix}"

@ecosystem_bp.route('/api/ecosystem/integrations', methods=['GET'])
def list_integrations():
    """List all active API integrations"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, company_id, company_name, api_endpoint, data_types,
                   sync_direction, sync_frequency, last_sync, sync_count,
                   status, created_at, documentation_url
            FROM ecosystem_integrations
            WHERE status = 'active'
            ORDER BY sync_count DESC
        ''')

        integrations = [dict(row) for row in cursor.fetchall()]

        cursor.execute('SELECT COUNT(*) FROM ecosystem_integrations WHERE status = %s', ('active',))
        active_count = cursor.fetchone()[0]

        cursor.execute('SELECT SUM(sync_count) FROM ecosystem_integrations')
        total_syncs = cursor.fetchone()[0] or 0

    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'integrations': integrations,
        'total_active': active_count,
        'total_syncs': total_syncs
    })

@ecosystem_bp.route('/api/ecosystem/integrations', methods=['POST'])
def register_integration():
    """Register a new API integration"""
    data = request.get_json()
    
    required = ['company_name', 'contact_email']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'success': False, 'error': f'Missing required fields: {", ".join(missing)}'}), 400
    
    integration_id = generate_integration_id(data['company_name'])
    api_key = generate_api_key()
    now = datetime.now().isoformat()
    
    conn = get_db()
    try:
        cursor = conn.cursor()

        data_types = json.dumps(data.get('data_types', ['facilities', 'news']))

        cursor.execute('''
            INSERT INTO ecosystem_integrations
            (id, company_id, company_name, api_key, api_endpoint, webhook_url,
             data_types, sync_direction, sync_frequency, status, created_at,
             updated_at, contact_email, documentation_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            integration_id,
            data.get('company_id'),
            data['company_name'],
            api_key,
            data.get('api_endpoint'),
            data.get('webhook_url'),
            data_types,
            data.get('sync_direction', 'pull'),
            data.get('sync_frequency', 'daily'),
            'pending',
            now,
            now,
            data['contact_email'],
            data.get('documentation_url')
        ))

        conn.commit()
    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'integration_id': integration_id,
        'api_key': api_key,
        'message': 'Integration registered. Your API key has been generated. Save it securely - it will not be shown again.',
        'endpoints': {
            'pull_facilities': '/api/v1/facilities',
            'pull_news': '/api/v1/news',
            'push_data': '/api/ecosystem/integrations/push',
            'webhook_register': '/api/ecosystem/integrations/webhook',
            'documentation': '/api/ecosystem/integrations/docs'
        }
    })

@ecosystem_bp.route('/api/ecosystem/integrations/<integration_id>', methods=['GET'])
def get_integration(integration_id):
    """Get integration details"""
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, company_id, company_name, api_endpoint, data_types,
                   sync_direction, sync_frequency, last_sync, sync_count,
                   status, created_at, updated_at, documentation_url
            FROM ecosystem_integrations WHERE id = %s
        ''', (integration_id,))

        row = cursor.fetchone()

        if not row:
    finally:
        conn.close()
        return jsonify({'success': False, 'error': 'Integration not found'}), 404
    
    cursor.execute('''
        SELECT action, direction, records_count, status, timestamp
        FROM integration_logs
        WHERE integration_id = %s
        ORDER BY timestamp DESC
        LIMIT 10
    ''', (integration_id,))
    
    logs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    integration = dict(row)
    if integration.get('data_types'):
        try:
            integration['data_types'] = json.loads(integration['data_types'])
        except:
            pass
    
    return jsonify({
        'success': True,
        'integration': integration,
        'recent_logs': logs
    })

@ecosystem_bp.route('/api/ecosystem/integrations/push', methods=['POST'])
def receive_push_data():
    """Receive data pushed from external integrations"""
    api_key = request.headers.get('X-API-Key') or request.headers.get('Authorization', '').replace('Bearer ', '')
    
    if not api_key:
        return jsonify({'success': False, 'error': 'API key required'}), 401
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM ecosystem_integrations WHERE api_key = %s AND status = %s', (api_key, 'active'))
        integration = cursor.fetchone()

        if not integration:
    finally:
        conn.close()
        return jsonify({'success': False, 'error': 'Invalid or inactive API key'}), 403
    
    data = request.get_json()
    data_type = data.get('type', 'facilities')
    records = data.get('records', [])
    
    now = datetime.now().isoformat()
    cursor.execute('''
        INSERT INTO integration_logs (integration_id, action, direction, records_count, status, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (integration['id'], f'push_{data_type}', 'inbound', len(records), 'received', now))
    
    cursor.execute('''
        UPDATE ecosystem_integrations 
        SET last_sync = %s, sync_count = sync_count + 1, updated_at = %s
        WHERE id = %s
    ''', (now, now, integration['id']))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': f'Received {len(records)} {data_type} records',
        'integration_id': integration['id'],
        'timestamp': now
    })

@ecosystem_bp.route('/api/ecosystem/integrations/docs', methods=['GET'])
def integration_docs():
    """Return API integration documentation"""
    return jsonify({
        'success': True,
        'api_version': 'v1',
        'base_url': 'https://dc-hub-nexus.replit.app',
        'authentication': {
            'type': 'API Key',
            'header': 'X-API-Key',
            'alternative': 'Authorization: Bearer <api_key>'
        },
        'endpoints': {
            'pull': {
                'facilities': {
                    'method': 'GET',
                    'path': '/api/v1/facilities',
                    'description': 'Get list of data center facilities',
                    'parameters': {
                        'limit': 'Number of results (default: 50)',
                        'offset': 'Pagination offset',
                        'country': 'Filter by country code',
                        'provider': 'Filter by provider name'
                    }
                },
                'news': {
                    'method': 'GET',
                    'path': '/api/v1/news',
                    'description': 'Get latest data center news',
                    'parameters': {
                        'limit': 'Number of results (default: 20)',
                        'category': 'Filter by category'
                    }
                },
                'ecosystem': {
                    'method': 'GET',
                    'path': '/api/ecosystem',
                    'description': 'Get ecosystem companies',
                    'parameters': {
                        'category': 'Filter by category',
                        'status': 'Filter by status (approved/pending)'
                    }
                }
            },
            'push': {
                'data': {
                    'method': 'POST',
                    'path': '/api/ecosystem/integrations/push',
                    'description': 'Push data to DC Hub',
                    'body': {
                        'type': 'Data type (facilities, news, companies)',
                        'records': 'Array of records to push'
                    }
                }
            },
            'webhooks': {
                'register': {
                    'method': 'POST',
                    'path': '/api/ecosystem/integrations/webhook',
                    'description': 'Register webhook for real-time updates',
                    'body': {
                        'url': 'Your webhook endpoint URL',
                        'events': 'Array of events to subscribe to'
                    }
                }
            }
        },
        'data_types': [
            'facilities',
            'news', 
            'companies',
            'transactions',
            'infrastructure'
        ],
        'sync_directions': [
            'pull - Fetch data from DC Hub',
            'push - Send data to DC Hub',
            'bidirectional - Both pull and push'
        ],
        'rate_limits': {
            'free': '100 requests/day',
            'pro': '10,000 requests/day',
            'enterprise': '100,000 requests/day'
        },
        'webhooks': {
            'events': [
                'facility.created',
                'facility.updated',
                'news.published',
                'company.added',
                'transaction.announced'
            ]
        }
    })

@ecosystem_bp.route('/api/ecosystem/integrations/webhook', methods=['POST'])
def register_webhook():
    """Register a webhook for real-time updates"""
    api_key = request.headers.get('X-API-Key') or request.headers.get('Authorization', '').replace('Bearer ', '')
    
    if not api_key:
        return jsonify({'success': False, 'error': 'API key required'}), 401
    
    conn = get_db()
    try:
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM ecosystem_integrations WHERE api_key = %s', (api_key,))
        integration = cursor.fetchone()

        if not integration:
    finally:
        conn.close()
        return jsonify({'success': False, 'error': 'Invalid API key'}), 403
    
    data = request.get_json()
    webhook_url = data.get('url')
    
    if not webhook_url:
        conn.close()
        return jsonify({'success': False, 'error': 'Webhook URL required'}), 400
    
    now = datetime.now().isoformat()
    cursor.execute('''
        UPDATE ecosystem_integrations 
        SET webhook_url = %s, updated_at = %s
        WHERE id = %s
    ''', (webhook_url, now, integration['id']))
    
    cursor.execute('''
        INSERT INTO integration_logs (integration_id, action, direction, records_count, status, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (integration['id'], 'webhook_registered', 'config', 0, 'success', now))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Webhook registered successfully',
        'webhook_url': webhook_url,
        'events': data.get('events', ['all'])
    })

@ecosystem_bp.route('/api/ecosystem/integrations/stats', methods=['GET'])
def integration_stats():
    """Get integration statistics"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM ecosystem_integrations WHERE status = %s', ('active',))
        active = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM ecosystem_integrations WHERE status = %s', ('pending',))
        pending = cursor.fetchone()[0]

        cursor.execute('SELECT SUM(sync_count) FROM ecosystem_integrations')
        total_syncs = cursor.fetchone()[0] or 0

        cursor.execute('''
            SELECT sync_direction, COUNT(*) as count
            FROM ecosystem_integrations
            WHERE status = 'active'
            GROUP BY sync_direction
        ''')
        by_direction = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute('''
            SELECT COUNT(*) FROM integration_logs
            WHERE timestamp > datetime('now', '-24 hours')
        ''')
        syncs_24h = cursor.fetchone()[0]

    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'active_integrations': active,
        'pending_integrations': pending,
        'total_syncs': total_syncs,
        'syncs_last_24h': syncs_24h,
        'by_direction': by_direction
    })

def register_ecosystem_routes(app):
    """Register ecosystem blueprint with app"""
    init_ecosystem_tables()
    seed_ecosystem_data()
    app.register_blueprint(ecosystem_bp)
    print("🏢 Ecosystem API registered:")
    print("   GET  /api/ecosystem - List companies")
    print("   POST /api/ecosystem - Submit company")
    print("   GET  /api/ecosystem/<id> - Get company details")
    print("   GET  /api/ecosystem/stats - Ecosystem statistics")
    print("   GET  /api/ecosystem/search - Search companies")
    print("   GET  /api/ecosystem/categories - List categories")
    print("   🔌 API Integrations:")
    print("   GET  /api/ecosystem/integrations - List integrations")
    print("   POST /api/ecosystem/integrations - Register new integration")
    print("   GET  /api/ecosystem/integrations/docs - API documentation")
    print("   POST /api/ecosystem/integrations/push - Receive push data")
    print("   POST /api/ecosystem/integrations/webhook - Register webhook")
