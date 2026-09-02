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
from ai_surface_canon import canon_text

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
        'facility_count': canon_text('{canon_facilities} distinct facilities vs competitors\' 6-12k'),
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
        {'competitor': 'DCHawk', 'gap': 'Limited to North America', 'dc_hub_advantage': canon_text('170+ countries, {canon_facilities} distinct facilities')},
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
                '170+ country coverage'
            ],
            'vs_competitors': {
                'facility_coverage': canon_text('{canon_facilities} vs average 8,000'),
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


def seed_competitor_tables():
    """Seed competitors + strategic coverage_gaps rows from the static
    profiles above. Until 2026-07-02 these tables were created empty and
    NOTHING ever inserted — the API served the Python constants while SQL
    consumers (brain, dashboards) saw zero rows. Idempotent upserts keyed
    on competitor_id / gap_id. Facility-level gap rows come from
    routes/competitor_gap_crawler.persist_coverage_gaps, not from here."""
    conn = get_db()
    try:
        c = conn.cursor()
        for cid, comp in CompetitorAnalysis.COMPETITORS.items():
            c.execute('''INSERT INTO competitors
                (competitor_id, name, website, category,
                 estimated_facilities, geographic_coverage, data_freshness,
                 api_available, pricing_model, strengths, weaknesses,
                 last_analyzed)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        CURRENT_TIMESTAMP::text)
                ON CONFLICT (competitor_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  website = EXCLUDED.website,
                  category = EXCLUDED.category,
                  estimated_facilities = EXCLUDED.estimated_facilities,
                  geographic_coverage = EXCLUDED.geographic_coverage,
                  data_freshness = EXCLUDED.data_freshness,
                  api_available = EXCLUDED.api_available,
                  pricing_model = EXCLUDED.pricing_model,
                  strengths = EXCLUDED.strengths,
                  weaknesses = EXCLUDED.weaknesses,
                  last_analyzed = CURRENT_TIMESTAMP::text''',
                (cid, comp['name'], comp['website'], comp['category'],
                 comp['estimated_facilities'], comp['geographic_coverage'],
                 comp['data_freshness'], 1 if comp['api_available'] else 0,
                 comp['pricing'], json.dumps(comp['strengths']),
                 json.dumps(comp['weaknesses'])))
        for g in CompetitorAnalysis.COVERAGE_GAPS:
            gap_id = ('strategic:' + hashlib.md5(
                (g['competitor'] + '|' + g['gap']).encode()
            ).hexdigest()[:16])
            c.execute('''INSERT INTO coverage_gaps
                (gap_id, competitor, gap_type, description, dc_hub_advantage)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (gap_id) DO UPDATE SET
                  description = EXCLUDED.description,
                  dc_hub_advantage = EXCLUDED.dc_hub_advantage''',
                (gap_id, g['competitor'], 'strategic', g['gap'],
                 g['dc_hub_advantage']))
        conn.commit()
        logger.info("✅ Competitor tables seeded (%d competitors, %d "
                    "strategic gaps)", len(CompetitorAnalysis.COMPETITORS),
                    len(CompetitorAnalysis.COVERAGE_GAPS))
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("competitor table seed failed: %s", e)
    finally:
        conn.close()


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

def _crawled_coverage_gaps(competitor=None, limit=50):
    """Live rows from the coverage_gaps table the daily competitor-gap
    crawler writes. brain-ascension #28 (2026-07-25): this endpoint served
    only the hand-written COVERAGE_GAPS constant while the crawler's real
    evidence sat unread. Fail-soft [] so the static gaps always render.
    created_at is TEXT in this table — ordered lexically (ISO), never cast."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            if competitor:
                cur.execute(
                    """SELECT competitor, gap_type, description,
                              dc_hub_advantage, created_at
                         FROM coverage_gaps
                        WHERE LOWER(competitor) = LOWER(%s)
                        ORDER BY created_at DESC LIMIT %s""",
                    (competitor, limit))
            else:
                cur.execute(
                    """SELECT competitor, gap_type, description,
                              dc_hub_advantage, created_at
                         FROM coverage_gaps
                        ORDER BY created_at DESC LIMIT %s""", (limit,))
            return [{'competitor': r[0], 'gap_type': r[1],
                     'gap': (r[2] or '')[:400], 'dc_hub_advantage': r[3],
                     'observed_at': r[4]} for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.debug("crawled coverage_gaps read failed: %s", e)
        return []


@competitor_bp.route('/api/competitors/gaps')
def get_coverage_gaps():
    """Coverage gaps: strategic (curated) + crawled (live daily evidence)"""
    competitor = request.args.get('competitor')
    static = CompetitorAnalysis.get_coverage_gaps(competitor)
    crawled = _crawled_coverage_gaps(competitor)
    return jsonify({
        'success': True,
        **static,
        'crawled_gaps': crawled,
        'crawled_count': len(crawled),
        'crawled_source': 'coverage_gaps (competitor_gap_crawler, daily)'
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
    try:
        seed_competitor_tables()
    except Exception as e:
        logger.warning("competitor seed at register failed: %s", e)
    logger.info("✅ Competitor Intelligence registered")
    print("🕵️ Competitor Intelligence: ✅ Registered")
    print("   📊 Tracking: DCByte, DCHawk, DataCenters.com, DCD, DCK")
    print("   🎯 Gaps: /api/competitors/gaps")
    print("   📈 Position: /api/competitors/position")
    print("   📉 Matrix: /api/competitors/matrix")
