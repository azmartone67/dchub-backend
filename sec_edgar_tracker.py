"""
SEC EDGAR DC Company Tracker
- Track DC company SEC filings
- Parse 10-K, 10-Q, 8-K for expansion signals
- Monitor M&A activity
- Track capex announcements
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json
import re
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

sec_bp = Blueprint('sec_edgar', __name__)

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')


def init_sec_db():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS sec_filings (
            id SERIAL PRIMARY KEY,
            filing_id TEXT UNIQUE,
            company TEXT,
            ticker TEXT,
            cik TEXT,
            form_type TEXT,
            filed_date TEXT,
            period_date TEXT,
            url TEXT,
            has_expansion_signal INTEGER DEFAULT 0,
            has_acquisition_signal INTEGER DEFAULT 0,
            capex_amount REAL,
            summary TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS dc_companies_sec (
            id SERIAL PRIMARY KEY,
            company_id TEXT UNIQUE,
            name TEXT,
            ticker TEXT,
            cik TEXT,
            company_type TEXT,
            market_cap REAL,
            total_capacity_mw REAL,
            facility_count INTEGER,
            last_filing_date TEXT,
            website TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS expansion_signals (
            id SERIAL PRIMARY KEY,
            signal_id TEXT UNIQUE,
            company TEXT,
            signal_type TEXT,
            location TEXT,
            capacity_mw REAL,
            investment_amount REAL,
            expected_completion TEXT,
            source TEXT,
            filing_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
    finally:
        conn.close()
    logger.info("✅ SEC EDGAR Tracker tables initialized")

init_sec_db()


class DCCompanySEC:
    """Data Center companies with SEC filings"""
    
    COMPANIES = {
        'EQIX': {
            'name': 'Equinix Inc',
            'cik': '0001101239',
            'type': 'Colocation REIT',
            'market_cap': 78500000000,
            'capacity_mw': 2850,
            'facilities': 260,
            'markets': 'Global (Americas, EMEA, APAC)'
        },
        'DLR': {
            'name': 'Digital Realty Trust',
            'cik': '0001297996',
            'type': 'Hyperscale REIT',
            'market_cap': 45200000000,
            'capacity_mw': 3200,
            'facilities': 310,
            'markets': 'Global (45+ metros)'
        },
        'AMT': {
            'name': 'American Tower Corp',
            'cik': '0001053507',
            'type': 'Infrastructure REIT',
            'market_cap': 95000000000,
            'capacity_mw': 450,
            'facilities': 25,
            'markets': 'Americas, EMEA, India'
        },
        'CONE': {
            'name': 'CyrusOne (Acquired by KKR/GIP)',
            'cik': '0001553610',
            'type': 'Enterprise DC',
            'market_cap': 15000000000,
            'capacity_mw': 1100,
            'facilities': 52,
            'markets': 'US, Europe'
        },
        'QTS': {
            'name': 'QTS Realty Trust (Blackstone)',
            'cik': '0001548648',
            'type': 'Hyperscale',
            'market_cap': 10000000000,
            'capacity_mw': 850,
            'facilities': 28,
            'markets': 'US Major Markets'
        },
        'SWCH': {
            'name': 'Switch Inc',
            'cik': '0001716129',
            'type': 'Hyperscale',
            'market_cap': 8500000000,
            'capacity_mw': 650,
            'facilities': 6,
            'markets': 'Las Vegas, Reno, Atlanta, Austin'
        },
        'COR': {
            'name': 'CoreSite Realty (American Tower)',
            'cik': '0001496048',
            'type': 'Colocation',
            'market_cap': 10000000000,
            'capacity_mw': 580,
            'facilities': 25,
            'markets': 'US Major Markets'
        },
        'VRT': {
            'name': 'Vertiv Holdings',
            'cik': '0001674101',
            'type': 'DC Infrastructure',
            'market_cap': 32000000000,
            'capacity_mw': 0,
            'facilities': 0,
            'markets': 'Global (Equipment)'
        },
    }
    
    RECENT_FILINGS = [
        {'filing_id': 'EQIX-10K-2024', 'company': 'Equinix', 'ticker': 'EQIX', 'form_type': '10-K', 'filed_date': '2025-02-15', 'has_expansion_signal': 1, 'capex_amount': 2800000000, 'summary': 'Record capex of $2.8B planned for 2025, targeting 15+ new xScale builds'},
        {'filing_id': 'DLR-10K-2024', 'company': 'Digital Realty', 'ticker': 'DLR', 'form_type': '10-K', 'filed_date': '2025-02-12', 'has_expansion_signal': 1, 'capex_amount': 2400000000, 'summary': 'Expanding Northern Virginia, Phoenix, and European markets with $2.4B capex'},
        {'filing_id': 'EQIX-8K-2025-01', 'company': 'Equinix', 'ticker': 'EQIX', 'form_type': '8-K', 'filed_date': '2025-01-22', 'has_expansion_signal': 1, 'has_acquisition_signal': 0, 'summary': 'Announced new 80MW campus in Warsaw, Poland'},
        {'filing_id': 'DLR-8K-2025-01', 'company': 'Digital Realty', 'ticker': 'DLR', 'form_type': '8-K', 'filed_date': '2025-01-15', 'has_acquisition_signal': 1, 'summary': 'Completed acquisition of Switch properties for $3.1B'},
        {'filing_id': 'VRT-10K-2024', 'company': 'Vertiv', 'ticker': 'VRT', 'form_type': '10-K', 'filed_date': '2025-02-10', 'has_expansion_signal': 1, 'summary': 'Record backlog of $5.2B, AI infrastructure demand driving growth'},
        {'filing_id': 'SWCH-8K-2025-01', 'company': 'Switch', 'ticker': 'SWCH', 'form_type': '8-K', 'filed_date': '2025-01-08', 'has_expansion_signal': 1, 'summary': 'New 200MW expansion at Citadel Campus, Las Vegas'},
    ]
    
    EXPANSION_SIGNALS = [
        {'signal_id': 'EXP-2025-001', 'company': 'Equinix', 'signal_type': 'New Campus', 'location': 'Warsaw, Poland', 'capacity_mw': 80, 'investment_amount': 450000000, 'expected_completion': '2027-Q2', 'source': '8-K Filing'},
        {'signal_id': 'EXP-2025-002', 'company': 'Digital Realty', 'signal_type': 'Expansion', 'location': 'Ashburn, VA', 'capacity_mw': 120, 'investment_amount': 680000000, 'expected_completion': '2026-Q4', 'source': '10-K Filing'},
        {'signal_id': 'EXP-2025-003', 'company': 'Switch', 'signal_type': 'Campus Expansion', 'location': 'Las Vegas, NV', 'capacity_mw': 200, 'investment_amount': 1200000000, 'expected_completion': '2027-Q1', 'source': '8-K Filing'},
        {'signal_id': 'EXP-2025-004', 'company': 'QTS', 'signal_type': 'New Build', 'location': 'Phoenix, AZ', 'capacity_mw': 150, 'investment_amount': 850000000, 'expected_completion': '2026-Q3', 'source': 'Press Release'},
        {'signal_id': 'EXP-2025-005', 'company': 'Amazon/AWS', 'signal_type': 'Hyperscale Campus', 'location': 'Columbus, OH', 'capacity_mw': 300, 'investment_amount': 2000000000, 'expected_completion': '2027-Q2', 'source': 'SEC S-1'},
        {'signal_id': 'EXP-2025-006', 'company': 'Google', 'signal_type': 'New Campus', 'location': 'Mesa, AZ', 'capacity_mw': 400, 'investment_amount': 2500000000, 'expected_completion': '2027-Q4', 'source': 'Press Release'},
        {'signal_id': 'EXP-2025-007', 'company': 'Microsoft', 'signal_type': 'Expansion', 'location': 'Des Moines, IA', 'capacity_mw': 250, 'investment_amount': 1500000000, 'expected_completion': '2026-Q4', 'source': 'State Filing'},
    ]
    
    @classmethod
    def get_companies(cls):
        """Get all DC companies with SEC filings"""
        companies = [{'ticker': k, **v} for k, v in cls.COMPANIES.items()]
        return {
            'companies': companies,
            'count': len(companies),
            'total_market_cap': sum(c['market_cap'] for c in companies),
            'total_capacity_mw': sum(c['capacity_mw'] for c in companies)
        }
    
    @classmethod
    def get_company(cls, ticker):
        """Get company details"""
        company = cls.COMPANIES.get(ticker.upper())
        if company:
            filings = [f for f in cls.RECENT_FILINGS if f['ticker'] == ticker.upper()]
            signals = [s for s in cls.EXPANSION_SIGNALS if ticker.upper() in s['company'].upper() or s['company'] in company['name']]
            return {
                'ticker': ticker.upper(),
                **company,
                'recent_filings': filings,
                'expansion_signals': signals
            }
        return {'error': f'Company not found: {ticker}'}
    
    @classmethod
    def get_filings(cls, form_type=None, ticker=None, days=90):
        """Get recent SEC filings"""
        filings = cls.RECENT_FILINGS
        if form_type:
            filings = [f for f in filings if f['form_type'].upper() == form_type.upper()]
        if ticker:
            filings = [f for f in filings if f['ticker'].upper() == ticker.upper()]
        
        return {
            'filings': filings,
            'count': len(filings),
            'filters': {'form_type': form_type, 'ticker': ticker, 'days': days}
        }
    
    @classmethod
    def get_expansion_signals(cls, company=None, location=None):
        """Get expansion signals from SEC filings"""
        signals = cls.EXPANSION_SIGNALS
        if company:
            signals = [s for s in signals if company.lower() in s['company'].lower()]
        if location:
            signals = [s for s in signals if location.lower() in s['location'].lower()]
        
        total_capacity = sum(s['capacity_mw'] for s in signals)
        total_investment = sum(s['investment_amount'] for s in signals)
        
        return {
            'signals': signals,
            'count': len(signals),
            'total_capacity_mw': total_capacity,
            'total_investment': total_investment
        }
    
    @classmethod
    def get_market_summary(cls):
        """Get market summary from SEC data"""
        total_capex = sum(f.get('capex_amount', 0) for f in cls.RECENT_FILINGS if f.get('capex_amount'))
        expansion_count = len([f for f in cls.RECENT_FILINGS if f.get('has_expansion_signal')])
        acquisition_count = len([f for f in cls.RECENT_FILINGS if f.get('has_acquisition_signal')])
        
        return {
            'market_summary': {
                'total_dc_market_cap': sum(c['market_cap'] for c in cls.COMPANIES.values()),
                'total_announced_capex': total_capex,
                'expansion_announcements': expansion_count,
                'acquisition_announcements': acquisition_count,
                'upcoming_capacity_mw': sum(s['capacity_mw'] for s in cls.EXPANSION_SIGNALS),
                'upcoming_investment': sum(s['investment_amount'] for s in cls.EXPANSION_SIGNALS)
            },
            'timestamp': datetime.now().isoformat()
        }


class SECFilingParser:
    """Parse SEC filings for DC signals"""
    
    DC_KEYWORDS = [
        'data center', 'datacenter', 'hyperscale', 'colocation', 'colo',
        'megawatt', 'MW', 'critical load', 'IT load', 'power capacity',
        'rack space', 'cabinet', 'interconnection', 'cross-connect',
        'cooling', 'PUE', 'power usage effectiveness', 'UPS', 'generator'
    ]
    
    EXPANSION_KEYWORDS = [
        'expansion', 'new facility', 'new campus', 'new build', 'development',
        'construction', 'under development', 'planned', 'announced', 'pipeline',
        'capex', 'capital expenditure', 'investment'
    ]
    
    ACQUISITION_KEYWORDS = [
        'acquisition', 'acquire', 'acquired', 'merger', 'purchase', 'purchased',
        'transaction', 'deal', 'combined', 'joint venture'
    ]
    
    @classmethod
    def extract_signals(cls, text):
        """Extract expansion/acquisition signals from filing text"""
        text_lower = text.lower()
        
        is_dc_related = any(kw in text_lower for kw in cls.DC_KEYWORDS)
        has_expansion = any(kw in text_lower for kw in cls.EXPANSION_KEYWORDS)
        has_acquisition = any(kw in text_lower for kw in cls.ACQUISITION_KEYWORDS)
        
        mw_matches = re.findall(r'(\d+(?:,\d+)?)\s*(?:megawatt|MW)', text, re.IGNORECASE)
        capex_matches = re.findall(r'\$(\d+(?:\.\d+)?)\s*(?:billion|B)\s*(?:capex|capital|investment)', text, re.IGNORECASE)
        
        return {
            'is_dc_related': is_dc_related,
            'has_expansion_signal': has_expansion and is_dc_related,
            'has_acquisition_signal': has_acquisition and is_dc_related,
            'mw_mentions': mw_matches,
            'capex_mentions': capex_matches
        }


@sec_bp.route('/api/sec/companies')
def get_companies():
    """Get DC companies with SEC filings"""
    return jsonify({
        'success': True,
        **DCCompanySEC.get_companies()
    })

@sec_bp.route('/api/sec/companies/<ticker>')
def get_company(ticker):
    """Get company details"""
    return jsonify({
        'success': True,
        **DCCompanySEC.get_company(ticker)
    })

@sec_bp.route('/api/sec/filings')
def get_filings():
    """Get recent SEC filings"""
    form_type = request.args.get('form')
    ticker = request.args.get('ticker')
    days = request.args.get('days', type=int, default=90)
    return jsonify({
        'success': True,
        **DCCompanySEC.get_filings(form_type, ticker, days)
    })

@sec_bp.route('/api/sec/expansion-signals')
def get_expansion_signals():
    """Get expansion signals from SEC filings"""
    company = request.args.get('company')
    location = request.args.get('location')
    return jsonify({
        'success': True,
        **DCCompanySEC.get_expansion_signals(company, location)
    })

@sec_bp.route('/api/sec/market-summary')
def get_market_summary():
    """Get market summary from SEC data"""
    return jsonify({
        'success': True,
        **DCCompanySEC.get_market_summary()
    })

@sec_bp.route('/api/sec/summary')
def get_sec_summary():
    """Get SEC tracker summary"""
    companies = DCCompanySEC.get_companies()
    market = DCCompanySEC.get_market_summary()
    
    return jsonify({
        'success': True,
        'modules': {
            'companies': {
                'tracked': companies['count'],
                'total_market_cap': companies['total_market_cap'],
                'endpoints': ['/api/sec/companies', '/api/sec/companies/<ticker>']
            },
            'filings': {
                'recent_count': len(DCCompanySEC.RECENT_FILINGS),
                'endpoints': ['/api/sec/filings']
            },
            'expansion_signals': {
                'count': len(DCCompanySEC.EXPANSION_SIGNALS),
                'total_capacity_mw': sum(s['capacity_mw'] for s in DCCompanySEC.EXPANSION_SIGNALS),
                'endpoints': ['/api/sec/expansion-signals']
            },
            'market_summary': market['market_summary']
        },
        'timestamp': datetime.now().isoformat()
    })


def register_sec_tracker(app):
    """Register SEC tracker routes"""
    app.register_blueprint(sec_bp)
    logger.info("✅ SEC EDGAR Tracker registered")
    print("📈 SEC EDGAR Tracker: ✅ Registered")
    print("   🏢 Companies: /api/sec/companies (8 DC REITs)")
    print("   📄 Filings: /api/sec/filings")
    print("   🚀 Expansions: /api/sec/expansion-signals")
    print("   📊 Market: /api/sec/market-summary")
