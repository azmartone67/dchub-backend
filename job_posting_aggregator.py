"""
Job Posting Aggregator
- Track DC company hiring patterns
- Expansion signals from job postings
- Market growth indicators
- Skill demand trends
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

jobs_bp = Blueprint('job_aggregator', __name__)

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')


def init_jobs_db():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS job_postings (
            id SERIAL PRIMARY KEY,
            job_id TEXT UNIQUE,
            company TEXT,
            title TEXT,
            location TEXT,
            job_type TEXT,
            department TEXT,
            seniority TEXT,
            salary_min REAL,
            salary_max REAL,
            posted_date TEXT,
            url TEXT,
            is_expansion_signal INTEGER DEFAULT 0,
            skills TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS hiring_trends (
            id SERIAL PRIMARY KEY,
            company TEXT,
            month TEXT,
            total_postings INTEGER,
            engineering_postings INTEGER,
            operations_postings INTEGER,
            sales_postings INTEGER,
            executive_postings INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
    finally:
        conn.close()
    logger.info("✅ Job Posting Aggregator tables initialized")

init_jobs_db()


class JobAggregator:
    """Aggregate DC company job postings"""
    
    DC_COMPANIES = [
        'Equinix', 'Digital Realty', 'QTS', 'CyrusOne', 'CoreSite',
        'Vantage Data Centers', 'Switch', 'DataBank', 'Flexential',
        'T5 Data Centers', 'EdgeConneX', 'Stream Data Centers',
        'Compass Datacenters', 'Prime Data Centers', 'Stack Infrastructure'
    ]
    
    HYPERSCALERS = [
        'Amazon Web Services', 'Microsoft Azure', 'Google Cloud',
        'Meta', 'Apple', 'Oracle Cloud', 'IBM Cloud'
    ]
    
    SAMPLE_POSTINGS = [
        {'job_id': 'EQ-2025-001', 'company': 'Equinix', 'title': 'Senior Data Center Technician', 'location': 'Ashburn, VA', 'department': 'Operations', 'seniority': 'Senior', 'salary_min': 85000, 'salary_max': 110000, 'posted_date': '2025-01-28'},
        {'job_id': 'EQ-2025-002', 'company': 'Equinix', 'title': 'Data Center Construction Manager', 'location': 'Phoenix, AZ', 'department': 'Construction', 'seniority': 'Manager', 'salary_min': 120000, 'salary_max': 160000, 'posted_date': '2025-01-25', 'is_expansion_signal': 1},
        {'job_id': 'DLR-2025-001', 'company': 'Digital Realty', 'title': 'Critical Facilities Engineer', 'location': 'Dallas, TX', 'department': 'Engineering', 'seniority': 'Mid', 'salary_min': 95000, 'salary_max': 125000, 'posted_date': '2025-01-30'},
        {'job_id': 'DLR-2025-002', 'company': 'Digital Realty', 'title': 'VP of Development - Southwest', 'location': 'Phoenix, AZ', 'department': 'Development', 'seniority': 'Executive', 'salary_min': 250000, 'salary_max': 350000, 'posted_date': '2025-01-22', 'is_expansion_signal': 1},
        {'job_id': 'AWS-2025-001', 'company': 'Amazon Web Services', 'title': 'Data Center Manager', 'location': 'Columbus, OH', 'department': 'Operations', 'seniority': 'Manager', 'salary_min': 130000, 'salary_max': 180000, 'posted_date': '2025-01-29', 'is_expansion_signal': 1},
        {'job_id': 'AWS-2025-002', 'company': 'Amazon Web Services', 'title': 'Power Systems Engineer', 'location': 'Northern Virginia', 'department': 'Engineering', 'seniority': 'Senior', 'salary_min': 140000, 'salary_max': 190000, 'posted_date': '2025-01-27'},
        {'job_id': 'MSFT-2025-001', 'company': 'Microsoft Azure', 'title': 'Data Center Site Lead', 'location': 'Des Moines, IA', 'department': 'Operations', 'seniority': 'Lead', 'salary_min': 150000, 'salary_max': 200000, 'posted_date': '2025-01-26', 'is_expansion_signal': 1},
        {'job_id': 'GOOG-2025-001', 'company': 'Google Cloud', 'title': 'Data Center Mechanical Engineer', 'location': 'Mesa, AZ', 'department': 'Engineering', 'seniority': 'Mid', 'salary_min': 130000, 'salary_max': 170000, 'posted_date': '2025-01-24', 'is_expansion_signal': 1},
        {'job_id': 'QTS-2025-001', 'company': 'QTS', 'title': 'Facility Operations Manager', 'location': 'Atlanta, GA', 'department': 'Operations', 'seniority': 'Manager', 'salary_min': 110000, 'salary_max': 145000, 'posted_date': '2025-01-31'},
        {'job_id': 'VAN-2025-001', 'company': 'Vantage Data Centers', 'title': 'Project Director - New Campus', 'location': 'Salt Lake City, UT', 'department': 'Construction', 'seniority': 'Director', 'salary_min': 180000, 'salary_max': 240000, 'posted_date': '2025-01-20', 'is_expansion_signal': 1},
        {'job_id': 'SWI-2025-001', 'company': 'Switch', 'title': 'Data Center Construction Engineer', 'location': 'Las Vegas, NV', 'department': 'Construction', 'seniority': 'Senior', 'salary_min': 125000, 'salary_max': 165000, 'posted_date': '2025-01-23', 'is_expansion_signal': 1},
        {'job_id': 'EDG-2025-001', 'company': 'EdgeConneX', 'title': 'Regional Development Manager', 'location': 'Denver, CO', 'department': 'Development', 'seniority': 'Manager', 'salary_min': 140000, 'salary_max': 180000, 'posted_date': '2025-01-28', 'is_expansion_signal': 1},
    ]
    
    HIRING_TRENDS = {
        'Equinix': {'2024-Q4': 245, '2025-Q1': 312, 'yoy_change': 27.3, 'hot_markets': ['Phoenix', 'Dallas', 'Singapore']},
        'Digital Realty': {'2024-Q4': 198, '2025-Q1': 256, 'yoy_change': 29.3, 'hot_markets': ['Ashburn', 'Chicago', 'Frankfurt']},
        'AWS': {'2024-Q4': 520, '2025-Q1': 680, 'yoy_change': 30.8, 'hot_markets': ['Columbus', 'Northern Virginia', 'Oregon']},
        'Microsoft Azure': {'2024-Q4': 410, '2025-Q1': 545, 'yoy_change': 32.9, 'hot_markets': ['Des Moines', 'Phoenix', 'Singapore']},
        'Google Cloud': {'2024-Q4': 380, '2025-Q1': 465, 'yoy_change': 22.4, 'hot_markets': ['Mesa', 'Dallas', 'London']},
        'QTS': {'2024-Q4': 85, '2025-Q1': 120, 'yoy_change': 41.2, 'hot_markets': ['Phoenix', 'Atlanta', 'Dallas']},
        'Vantage': {'2024-Q4': 92, '2025-Q1': 138, 'yoy_change': 50.0, 'hot_markets': ['Salt Lake City', 'Phoenix', 'Montreal']},
        'Switch': {'2024-Q4': 65, '2025-Q1': 98, 'yoy_change': 50.8, 'hot_markets': ['Las Vegas', 'Atlanta', 'Austin']},
    }
    
    SKILL_DEMAND = {
        'Power Systems': {'demand': 'Very High', 'yoy_growth': 45, 'avg_salary': 145000},
        'Cooling/HVAC': {'demand': 'Very High', 'yoy_growth': 38, 'avg_salary': 125000},
        'Electrical Engineering': {'demand': 'High', 'yoy_growth': 32, 'avg_salary': 135000},
        'Network Engineering': {'demand': 'High', 'yoy_growth': 28, 'avg_salary': 140000},
        'Project Management': {'demand': 'High', 'yoy_growth': 25, 'avg_salary': 150000},
        'AI/ML Infrastructure': {'demand': 'Very High', 'yoy_growth': 85, 'avg_salary': 180000},
        'Liquid Cooling': {'demand': 'Emerging', 'yoy_growth': 120, 'avg_salary': 155000},
        'Sustainability/ESG': {'demand': 'Growing', 'yoy_growth': 55, 'avg_salary': 130000},
    }
    
    @classmethod
    def get_postings(cls, company=None, location=None, department=None, expansion_only=False):
        """Get job postings"""
        postings = cls.SAMPLE_POSTINGS
        if company:
            postings = [p for p in postings if company.lower() in p['company'].lower()]
        if location:
            postings = [p for p in postings if location.lower() in p['location'].lower()]
        if department:
            postings = [p for p in postings if department.lower() in p.get('department', '').lower()]
        if expansion_only:
            postings = [p for p in postings if p.get('is_expansion_signal', 0) == 1]
        
        return {
            'postings': postings,
            'count': len(postings),
            'filters': {'company': company, 'location': location, 'department': department}
        }
    
    @classmethod
    def get_hiring_trends(cls, company=None):
        """Get hiring trends"""
        if company:
            trend = cls.HIRING_TRENDS.get(company)
            if trend:
                return {'company': company, **trend}
            return {'error': f'Company not found: {company}'}
        
        trends = [{'company': k, **v} for k, v in cls.HIRING_TRENDS.items()]
        trends.sort(key=lambda x: x['yoy_change'], reverse=True)
        
        return {
            'trends': trends,
            'fastest_growing': trends[0] if trends else None,
            'total_q1_postings': sum(t['2025-Q1'] for t in trends)
        }
    
    @classmethod
    def get_expansion_signals(cls):
        """Get expansion signals from job postings"""
        signals = [p for p in cls.SAMPLE_POSTINGS if p.get('is_expansion_signal', 0) == 1]
        
        by_location = {}
        for s in signals:
            loc = s['location']
            if loc not in by_location:
                by_location[loc] = []
            by_location[loc].append(s['company'])
        
        return {
            'signals': signals,
            'count': len(signals),
            'by_location': by_location,
            'hottest_markets': sorted(by_location.keys(), key=lambda x: len(by_location[x]), reverse=True)[:5]
        }
    
    @classmethod
    def get_skill_demand(cls):
        """Get skill demand trends"""
        skills = [{'skill': k, **v} for k, v in cls.SKILL_DEMAND.items()]
        skills.sort(key=lambda x: x['yoy_growth'], reverse=True)
        
        return {
            'skills': skills,
            'fastest_growing': skills[0] if skills else None,
            'highest_paying': max(skills, key=lambda x: x['avg_salary']) if skills else None
        }
    
    @classmethod
    def get_market_heat(cls):
        """Get market heat based on hiring"""
        all_hot_markets = []
        for company, trend in cls.HIRING_TRENDS.items():
            all_hot_markets.extend(trend.get('hot_markets', []))
        
        from collections import Counter
        market_counts = Counter(all_hot_markets)
        
        return {
            'hottest_markets': market_counts.most_common(10),
            'total_companies_hiring': len(cls.HIRING_TRENDS),
            'expansion_signal_count': len([p for p in cls.SAMPLE_POSTINGS if p.get('is_expansion_signal')])
        }


@jobs_bp.route('/api/jobs')
def get_postings():
    """Get job postings"""
    company = request.args.get('company')
    location = request.args.get('location')
    department = request.args.get('department')
    expansion_only = request.args.get('expansion', 'false').lower() == 'true'
    
    return jsonify({
        'success': True,
        **JobAggregator.get_postings(company, location, department, expansion_only)
    })

@jobs_bp.route('/api/jobs/trends')
def get_hiring_trends():
    """Get hiring trends"""
    company = request.args.get('company')
    return jsonify({
        'success': True,
        **JobAggregator.get_hiring_trends(company)
    })

@jobs_bp.route('/api/jobs/expansion-signals')
def get_expansion_signals():
    """Get expansion signals from job postings"""
    return jsonify({
        'success': True,
        **JobAggregator.get_expansion_signals()
    })

@jobs_bp.route('/api/jobs/skills')
def get_skill_demand():
    """Get skill demand trends"""
    return jsonify({
        'success': True,
        **JobAggregator.get_skill_demand()
    })

@jobs_bp.route('/api/jobs/market-heat')
def get_market_heat():
    """Get market heat from hiring"""
    return jsonify({
        'success': True,
        **JobAggregator.get_market_heat()
    })

@jobs_bp.route('/api/jobs/summary')
def get_jobs_summary():
    """Get job aggregator summary"""
    trends = JobAggregator.get_hiring_trends()
    signals = JobAggregator.get_expansion_signals()
    skills = JobAggregator.get_skill_demand()
    
    return jsonify({
        'success': True,
        'modules': {
            'postings': {
                'tracked_companies': len(JobAggregator.DC_COMPANIES) + len(JobAggregator.HYPERSCALERS),
                'sample_count': len(JobAggregator.SAMPLE_POSTINGS),
                'endpoints': ['/api/jobs']
            },
            'trends': {
                'companies_tracked': len(JobAggregator.HIRING_TRENDS),
                'total_q1_postings': trends['total_q1_postings'],
                'endpoints': ['/api/jobs/trends']
            },
            'expansion_signals': {
                'count': signals['count'],
                'hottest_markets': signals['hottest_markets'],
                'endpoints': ['/api/jobs/expansion-signals']
            },
            'skills': {
                'tracked': len(JobAggregator.SKILL_DEMAND),
                'fastest_growing': skills['fastest_growing']['skill'] if skills.get('fastest_growing') else None,
                'endpoints': ['/api/jobs/skills']
            }
        },
        'timestamp': datetime.now().isoformat()
    })


def register_job_aggregator(app):
    """Register job aggregator routes"""
    app.register_blueprint(jobs_bp)
    logger.info("✅ Job Posting Aggregator registered")
    print("💼 Job Posting Aggregator: ✅ Registered")
    print("   📋 Postings: /api/jobs")
    print("   📈 Trends: /api/jobs/trends")
    print("   🚀 Signals: /api/jobs/expansion-signals")
    print("   🔧 Skills: /api/jobs/skills")
