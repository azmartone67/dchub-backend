"""
Competitor Intelligence Engine
- Monitor DCByte, DCHawk, DataCenters.com, DC Dynamics, DC Knowledge
- Track their coverage gaps
- Identify unique data opportunities
- Competitive positioning
"""

import requests
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
import json
import hashlib
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

competitor_bp = Blueprint('competitor_intel', __name__)

DB_PATH = os.environ.get('DC_NEXUS_DB', 'dc_nexus.db')


def init_competitor_db():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS competitors (
            id SERIAL PRIMARY KEY,
            competitor_id TEXT UNIQUE,
            name TEXT,
            website TEXT,
            category TEXT,
            estimated_facilities INTEGER,
            geographic_coverage TEXT,
            data_freshness TEXT,
            api_available INTEGER DEFAULT 0,
            pricing_model TEXT,
            strengths TEXT,
            weaknesses TEXT,
            last_analyzed TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS coverage_gaps (
            id SERIAL PRIMARY KEY,
            gap_id TEXT UNIQUE,
            competitor TEXT,
            gap_type TEXT,
            description TEXT,
            dc_hub_advantage TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
    finally:
        conn.close()
    logger.info("✅ Competitor Intelligence tables initialized")

init_competitor_db()


class CompetitorAnalysis:
    """Analyze competitor platforms"""
    
    COMPETITORS = {
        'dcbyte': {
            'name': 'DCByte',
            'website': 'https://dcbyte.com',
            'category': 'Market Intelligence',
            'estimated_facilities': 8000,
            'geographic_coverage': 'Global (focus on major markets)',
            'data_freshness': 'Monthly updates',
            'api_available': True,
            'pricing': 'Enterprise subscription ($25k-100k/year)',
            'strengths': [
                'Strong market analytics',
                'Power pricing data',
                'Investment intelligence',
                'Clean UI/UX'
            ],
            'weaknesses': [
                'Limited facility count',
                'No real-time power data',
                'No fiber network data',
                'No AI platform integration',
                'High pricing barrier'
            ]
        },
        'dchawk': {
            'name': 'DatacenterHawk',
            'website': 'https://www.datacenterhawk.com',
            'category': 'Facility Database',
            'estimated_facilities': 6500,
            'geographic_coverage': 'North America (primary), Some EMEA',
            'data_freshness': 'Quarterly updates',
            'api_available': False,
            'pricing': 'Enterprise subscription',
            'strengths': [
                'Good North America coverage',
                'Colocation focus',
                'Market reports'
            ],
            'weaknesses': [
                'No API access',
                'Limited global coverage',
                'Slow update frequency',
                'No power grid data',
                'No climate/risk data'
            ]
        },
        'datacenters_com': {
            'name': 'DataCenters.com',
            'website': 'https://www.datacenters.com',
            'category': 'Directory/Marketplace',
            'estimated_facilities': 12000,
            'geographic_coverage': 'Global (listing-based)',
            'data_freshness': 'User-submitted (varies)',
            'api_available': False,
            'pricing': 'Free listings, premium placement',
            'strengths': [
                'Large facility count',
                'Free access',
                'Good SEO presence',
                'RFQ system'
            ],
            'weaknesses': [
                'User-submitted data (quality varies)',
                'No market intelligence',
                'No power/infrastructure data',
                'No API',
                'Advertising-driven'
            ]
        },
        'dc_dynamics': {
            'name': 'Data Center Dynamics (DCD)',
            'website': 'https://www.datacenterdynamics.com',
            'category': 'News/Media',
            'estimated_facilities': 0,
            'geographic_coverage': 'Global news coverage',
            'data_freshness': 'Real-time news',
            'api_available': False,
            'pricing': 'Free news, paid conferences',
            'strengths': [
                'Industry news leader',
                'Global coverage',
                'Conference/events',
                'Editorial quality'
            ],
            'weaknesses': [
                'No facility database',
                'No API',
                'No market data',
                'No infrastructure data',
                'Media focus only'
            ]
        },
        'dc_knowledge': {
            'name': 'Data Center Knowledge',
            'website': 'https://www.datacenterknowledge.com',
            'category': 'News/Media',
            'estimated_facilities': 0,
            'geographic_coverage': 'Global news coverage',
            'data_freshness': 'Real-time news',
            'api_available': False,
            'pricing': 'Free (Informa Tech)',
            'strengths': [
                'Industry news',
                'Technical deep-dives',
                'Long history',
                'Editorial credibility'
            ],
            'weaknesses': [
                'No facility database',
                'No API',
                'No market intelligence',
                'No data products',
                'Declining update frequency'
            ]
        },
        'cloudscene': {
            'name': 'Cloudscene',
            'website': 'https://cloudscene.com',
            'category': 'Directory/Rankings',
            'estimated_facilities': 9000,
            'geographic_coverage': 'Global',
            'data_freshness': 'User-submitted + scraped',
            'api_available': False,
            'pricing': 'Freemium',
            'strengths': [
                'Ecosystem rankings',
                'Provider comparisons',
                'Cloud provider data'
            ],
            'weaknesses': [
                'Mixed data quality',
                'No API',
                'Limited intelligence',
                'No power data'
            ]
        },
    }
    
    DC_HUB_ADVANTAGES = {
        'facility_count': '21,000+ facilities vs competitors\' 6-12k',
        'api_access': 'Free public API + tiered commercial',
        'real_time_power': 'Live power grid data (67+ zones)',
        'fiber_network': 'Major carrier routes + lit buildings',
        'ai_integration': '14 AI platforms tracked + citations',
        'climate_data': 'Cooling scores, weather risk',
        'government_data': '40+ infrastructure layers',
        'global_coverage': '170+ countries tracked',
        'update_frequency': 'Real-time news, daily discovery',
        'pricing': 'Free tier available'
    }
    
    COVERAGE_GAPS = [
        {'competitor': 'DCByte', 'gap': 'No real-time power grid data', 'dc_hub_advantage': 'Live carbon intensity, grid demand across 67+ zones'},
        {'competitor': 'DCByte', 'gap': 'No fiber network mapping', 'dc_hub_advantage': '8 major carriers, routes, carrier hotels'},
        {'competitor': 'DCHawk', 'gap': 'No API access', 'dc_hub_advantage': 'Full REST API with 100+ endpoints'},
        {'competitor': 'DCHawk', 'gap': 'Limited to North America', 'dc_hub_advantage': '170+ countries, 21,000+ facilities'},
        {'competitor': 'DataCenters.com', 'gap': 'No market intelligence', 'dc_hub_advantage': 'SEC filings, expansion signals, M&A tracking'},
        {'competitor': 'DataCenters.com', 'gap': 'User-submitted data quality', 'dc_hub_advantage': 'Verified from 15+ authoritative sources'},
        {'competitor': 'All', 'gap': 'No AI platform integration', 'dc_hub_advantage': 'ChatGPT, Gemini, Claude, Perplexity citations'},
        {'competitor': 'All', 'gap': 'No climate/risk data', 'dc_hub_advantage': 'NOAA, FEMA, drought, cooling scores'},
        {'competitor': 'All', 'gap': 'No government infrastructure', 'dc_hub_advantage': '40+ HIFLD layers: substations, pipelines, transmission'},
    ]
    
    @classmethod
    def get_competitors(cls):
        """Get all competitor profiles"""
        competitors = [{'id': k, **v} for k, v in cls.COMPETITORS.items()]
        return {
            'competitors': competitors,
            'count': len(competitors)
        }
    
    @classmethod
    def get_competitor(cls, competitor_id):
        """Get competitor details"""
        competitor = cls.COMPETITORS.get(competitor_id)
        if competitor:
            gaps = [g for g in cls.COVERAGE_GAPS if g['competitor'].lower() == competitor_id.lower() or g['competitor'] == 'All']
            return {
                'id': competitor_id,
                **competitor,
                'coverage_gaps': gaps
            }
        return {'error': f'Competitor not found: {competitor_id}'}
    
    @classmethod
    def get_coverage_gaps(cls, competitor=None):
        """Get coverage gaps by competitor"""
        gaps = cls.COVERAGE_GAPS
        if competitor:
            gaps = [g for g in gaps if g['competitor'].lower() == competitor.lower() or g['competitor'] == 'All']
        return {
            'gaps': gaps,
            'count': len(gaps)
        }
    
    @classmethod
    def get_competitive_position(cls):
        """Get DC Hub's competitive position"""
        return {
            'dc_hub_advantages': cls.DC_HUB_ADVANTAGES,
            'unique_features': [
                'Real-time global power grid integration',
                'AI platform citation tracking',
                'Government infrastructure data',
                'Fiber network discovery',
                'SEC filing analysis',
                'Climate/risk assessment',
                'Free API tier',
                '140+ country coverage'
            ],
            'vs_competitors': {
                'facility_coverage': '20,000+ vs average 8,000',
                'api_availability': 'Open API vs none/enterprise-only',
                'data_sources': '15+ vs 2-3',
                'update_frequency': 'Real-time vs monthly/quarterly',
                'pricing': 'Free tier + commercial vs enterprise-only'
            },
            'market_position': 'Most comprehensive DC intelligence platform'
        }
    
    @classmethod
    def get_comparison_matrix(cls):
        """Get feature comparison matrix"""
        features = [
            'Facility Database',
            'Public API',
            'Real-time Power Data',
            'Fiber Network Data',
            'AI Platform Integration',
            'Climate/Risk Data',
            'Government Infrastructure',
            'SEC Filing Tracking',
            'Free Tier Available',
            'Global Coverage'
        ]
        
        matrix = {'DC Hub': {f: True for f in features}}
        
        for comp_id, comp in cls.COMPETITORS.items():
            matrix[comp['name']] = {
                'Facility Database': comp['estimated_facilities'] > 0,
                'Public API': comp['api_available'],
                'Real-time Power Data': False,
                'Fiber Network Data': False,
                'AI Platform Integration': False,
                'Climate/Risk Data': False,
                'Government Infrastructure': False,
                'SEC Filing Tracking': comp_id == 'dcbyte',
                'Free Tier Available': comp_id in ['datacenters_com', 'dc_dynamics', 'dc_knowledge'],
                'Global Coverage': 'global' in comp['geographic_coverage'].lower()
            }
        
        return {
            'features': features,
            'matrix': matrix,
            'dc_hub_score': len(features),
            'competitor_avg_score': round(sum(
                sum(1 for f in features if matrix[comp['name']].get(f, False))
                for comp in cls.COMPETITORS.values()
            ) / len(cls.COMPETITORS), 1)
        }


@competitor_bp.route('/api/competitors')
def get_competitors():
    """Get all competitors"""
    return jsonify({
        'success': True,
        **CompetitorAnalysis.get_competitors()
    })

@competitor_bp.route('/api/competitors/<competitor_id>')
def get_competitor(competitor_id):
    """Get competitor details"""
    return jsonify({
        'success': True,
        **CompetitorAnalysis.get_competitor(competitor_id)
    })

@competitor_bp.route('/api/competitors/gaps')
def get_coverage_gaps():
    """Get coverage gaps"""
    competitor = request.args.get('competitor')
    return jsonify({
        'success': True,
        **CompetitorAnalysis.get_coverage_gaps(competitor)
    })

@competitor_bp.route('/api/competitors/position')
def get_competitive_position():
    """Get DC Hub's competitive position"""
    return jsonify({
        'success': True,
        **CompetitorAnalysis.get_competitive_position()
    })

@competitor_bp.route('/api/competitors/matrix')
def get_comparison_matrix():
    """Get feature comparison matrix"""
    return jsonify({
        'success': True,
        **CompetitorAnalysis.get_comparison_matrix()
    })

@competitor_bp.route('/api/competitors/summary')
def get_competitor_summary():
    """Get competitor intelligence summary"""
    position = CompetitorAnalysis.get_competitive_position()
    matrix = CompetitorAnalysis.get_comparison_matrix()
    
    return jsonify({
        'success': True,
        'tracked_competitors': len(CompetitorAnalysis.COMPETITORS),
        'dc_hub_unique_features': len(position['unique_features']),
        'coverage_gaps_identified': len(CompetitorAnalysis.COVERAGE_GAPS),
        'feature_score': {
            'dc_hub': matrix['dc_hub_score'],
            'competitor_avg': matrix['competitor_avg_score']
        },
        'endpoints': [
            '/api/competitors',
            '/api/competitors/<id>',
            '/api/competitors/gaps',
            '/api/competitors/position',
            '/api/competitors/matrix'
        ],
        'timestamp': datetime.now().isoformat()
    })


def register_competitor_intel(app):
    """Register competitor intelligence routes"""
    app.register_blueprint(competitor_bp)
    logger.info("✅ Competitor Intelligence registered")
    print("🕵️ Competitor Intelligence: ✅ Registered")
    print("   📊 Tracking: DCByte, DCHawk, DataCenters.com, DCD, DCK")
    print("   🎯 Gaps: /api/competitors/gaps")
    print("   📈 Position: /api/competitors/position")
    print("   📉 Matrix: /api/competitors/matrix")
