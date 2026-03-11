"""
DC Hub Autopilot/Evolution/Brain Routes Blueprint
===================================================
Extracted from main.py during Phase 2 modularization (Extract 2).
Contains 27 routes:
  - Autopilot API (16 routes): status, stats, pending, approve, config,
    self-learning, deep-learning, transactions, capacity-pipeline, SEO, social
  - Evolution Engine API (6 routes): status, run, teach, suggestions, knowledge, quality
  - DC Expert Brain API (5 routes): status, learn, ask, market, operator

Dependencies injected from main.py via init_autopilot_routes():
  - require_plan, require_auth, require_api_key, protect_data: Decorators
  - get_db: Database connection function
  - PIPELINE_DATA: Fallback pipeline data list
  - AUTOPILOT_AVAILABLE, discovery_engine, autopilot_scheduler: Runtime state
"""

import logging
from datetime import datetime
from flask import Blueprint, request, jsonify

# Create Blueprint
autopilot_bp = Blueprint('autopilot', __name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Late-binding decorator/dependency injection
# ---------------------------------------------------------------------------
_require_plan = None
_require_auth = None
_require_api_key = None
_protect_data = None
_get_db = None
_PIPELINE_DATA = None
AUTOPILOT_AVAILABLE = False
discovery_engine = None
autopilot_scheduler = None


def init_autopilot_routes(require_plan_fn, require_auth_fn, require_api_key_fn,
                          protect_data_fn, get_db_fn, pipeline_data,
                          autopilot_available=False, disc_engine=None, ap_scheduler=None):
    """Called by main.py after import to inject app-level dependencies."""
    global _require_plan, _require_auth, _require_api_key, _protect_data, _get_db, _PIPELINE_DATA
    global AUTOPILOT_AVAILABLE, discovery_engine, autopilot_scheduler
    _require_plan = require_plan_fn
    _require_auth = require_auth_fn
    _require_api_key = require_api_key_fn
    _protect_data = protect_data_fn
    _get_db = get_db_fn
    _PIPELINE_DATA = pipeline_data
    AUTOPILOT_AVAILABLE = autopilot_available
    discovery_engine = disc_engine
    autopilot_scheduler = ap_scheduler


def get_db():
    """Late-binding wrapper for database connection."""
    if _get_db is not None:
        return _get_db()
    raise RuntimeError("get_db not initialized — call init_autopilot_routes first")


def require_plan(min_plan='pro'):
    """Late-binding wrapper: captures min_plan now, applies gating at request time."""
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if _require_plan is not None:
                return _require_plan(min_plan)(f)(*args, **kwargs)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_auth(f):
    """Late-binding wrapper for auth decorator."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _require_auth is not None:
            return _require_auth(f)(*args, **kwargs)
        return f(*args, **kwargs)
    return wrapper


def require_api_key(f):
    """Late-binding wrapper for API key decorator."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _require_api_key is not None:
            return _require_api_key(f)(*args, **kwargs)
        return f(*args, **kwargs)
    return wrapper


def protect_data(f):
    """Late-binding wrapper for data protection decorator."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _protect_data is not None:
            return _protect_data(f)(*args, **kwargs)
        return f(*args, **kwargs)
    return wrapper

# =============================================================================
# AUTO-PILOT API ROUTES
# =============================================================================

@autopilot_bp.route('/api/autopilot/status')
def autopilot_status():
    """Get auto-pilot system status"""
    # Get dynamic feed count (static + discovered)
    try:
        from auto_pilot import get_feed_stats, get_all_rss_feeds
        feed_stats = get_feed_stats()
        total_sources = feed_stats['static_feeds'] + feed_stats.get('active_feeds', 0)
    except:
        total_sources = 36  # Fallback to static count
    
    return jsonify({
        'status': 'active' if AUTOPILOT_AVAILABLE and autopilot_scheduler else 'inactive',
        'version': '1.0',
        'features': {
            'news_sync': {'interval': '1 min', 'sources': total_sources},
            'deal_discovery': {'interval': '1 hour', 'enabled': True},
            'facility_discovery': {'interval': '5 min', 'enabled': True},
            'power_updates': {'interval': '24 hours', 'enabled': True}
        },
        'stats': discovery_engine.get_stats() if discovery_engine else {}
    })

@autopilot_bp.route('/api/autopilot/stats')
@require_plan('enterprise')
def autopilot_stats():
    """Get auto-discovery statistics"""
    if not discovery_engine:
        return jsonify({'error': 'Auto-pilot not initialized'}), 503
    return jsonify(discovery_engine.get_stats())

@autopilot_bp.route('/api/autopilot/pending')
@require_plan('enterprise')
def autopilot_pending():
    """Get pending auto-discovered items"""
    if not discovery_engine:
        return jsonify({'error': 'Auto-pilot not initialized'}), 503
    return jsonify({
        'pending_deals': list(discovery_engine.seen_deals)[-20:] if hasattr(discovery_engine, 'seen_deals') else [],
        'pending_facilities': list(discovery_engine.seen_facilities)[-20:] if hasattr(discovery_engine, 'seen_facilities') else [],
    })

@autopilot_bp.route('/api/autopilot/approve/<item_type>/<item_id>', methods=['POST'])
@require_plan('enterprise')
@require_auth
def autopilot_approve(item_type, item_id):
    """Approve an auto-discovered item"""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    return jsonify({'status': 'approved', 'type': item_type, 'id': item_id})

@autopilot_bp.route('/api/autopilot/config', methods=['GET', 'POST'])
@require_plan('enterprise')
def autopilot_config():
    """Get or update auto-pilot configuration"""
    if request.method == 'POST':
        data = request.get_json()
        return jsonify({'status': 'updated', 'config': data})
    return jsonify({
        'news_interval': 300,  # Every 5 minutes (was 1 min)
        'deals_interval': 3600,
        'facility_interval': 300,
        'power_interval': 86400,
        'self_learning_interval': 1800,
        'outreach_interval': 600,  # Every 10 minutes (was 5)
        'ecosystem_interval': 900,  # Every 15 minutes (was 5)
        'ai_extraction': True,
        'auto_approve_threshold': 80
    })

@autopilot_bp.route('/api/autopilot/self-learning/status')
def self_learning_status():
    """Get self-learning discovery status"""
    try:
        from self_learning_discovery import get_discovery_stats
        stats = get_discovery_stats()
        return jsonify({
            'enabled': True,
            'interval': '30 min',
            'stats': stats
        })
    except Exception as e:
        return jsonify({'error': str(e), 'enabled': False}), 500

@autopilot_bp.route('/api/autopilot/self-learning/run', methods=['POST'])
@require_plan('enterprise')
@require_auth
def self_learning_run():
    """Manually trigger self-learning discovery"""
    try:
        from self_learning_discovery import run_self_learning_discovery
        result = run_self_learning_discovery()
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/autopilot/deep-learning/status')
def deep_learning_status():
    """Get deep learning engine status"""
    try:
        from deep_learning_engine import get_deep_learning_stats
        stats = get_deep_learning_stats()
        return jsonify({
            'enabled': True,
            'interval': '15 min',
            'stats': stats
        })
    except Exception as e:
        return jsonify({'error': str(e), 'enabled': False}), 500

@autopilot_bp.route('/api/autopilot/deep-learning/run', methods=['POST'])
@require_plan('enterprise')
@require_auth
def deep_learning_run():
    """Manually trigger deep learning cycle"""
    try:
        from deep_learning_engine import run_deep_learning_cycle
        result = run_deep_learning_cycle()
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def extract_company_from_title(title, role):
    """Extract company names from deal headlines"""
    if not title:
        return ''
    
    title_lower = title.lower()
    
    # Common acquisition patterns
    acquire_patterns = ['acquires', 'buys', 'purchases', 'to acquire', 'acquiring', 'acquisition of', 'takes over']
    invest_patterns = ['invests in', 'investment in', 'raises', 'funding', 'backs']
    
    # Known companies to look for
    companies = [
        'Blackstone', 'KKR', 'DigitalBridge', 'Brookfield', 'GIC', 'CDPQ', 'TPG',
        'Equinix', 'Digital Realty', 'QTS', 'CyrusOne', 'CoreWeave', 'AirTrunk',
        'Microsoft', 'Google', 'Amazon', 'Meta', 'Oracle', 'Apple',
        'Vantage', 'EdgeCore', 'Stack', 'DataBank', 'CloudHQ', 'Switch',
        'Data4', 'Cyxtera', 'Zayo', 'Lumen', 'NTT', 'Colt'
    ]
    
    found_companies = []
    for company in companies:
        if company.lower() in title_lower:
            idx = title_lower.index(company.lower())
            found_companies.append((company, idx))
    
    if not found_companies:
        return ''
    
    # Sort by position in title
    found_companies.sort(key=lambda x: x[1])
    
    # Find acquisition verb position
    verb_pos = len(title)
    for pattern in acquire_patterns + invest_patterns:
        if pattern in title_lower:
            verb_pos = min(verb_pos, title_lower.index(pattern))
            break
    
    # Buyer is usually before the verb, seller after
    if role == 'buyer':
        for company, pos in found_companies:
            if pos < verb_pos:
                return company
        return found_companies[0][0] if found_companies else ''
    else:  # seller
        for company, pos in found_companies:
            if pos > verb_pos:
                return company
        return found_companies[-1][0] if len(found_companies) > 1 else ''


def extract_value_from_title(title):
    """Extract deal value from title"""
    if not title:
        return 'Undisclosed'
    
    import re
    
    # Look for $XXB, $XX billion, $XXM, $XX million patterns
    patterns = [
        (r'\$([\d.]+)\s*[Bb](?:illion)?', 'B'),
        (r'\$([\d.]+)\s*[Mm](?:illion)?', 'M'),
        (r'([\d.]+)\s*[Bb]illion', 'B'),
        (r'([\d.]+)\s*[Mm]illion', 'M'),
    ]
    
    for pattern, suffix in patterns:
        match = re.search(pattern, title)
        if match:
            value = float(match.group(1))
            return f"${value}{suffix}"
    
    return 'Undisclosed'


def parse_deal_value(value_str):
    """Parse deal value string to number"""
    if not value_str or value_str == 'Undisclosed':
        return 0
    
    import re
    match = re.search(r'([\d.]+)', str(value_str))
    if match:
        num = float(match.group(1))
        if 'B' in str(value_str).upper():
            return num * 1e9
        elif 'M' in str(value_str).upper():
            return num * 1e6
    return 0


def classify_deal_type(title, buyer, seller):
    """Classify deal type based on context"""
    title_lower = (title or '').lower()
    
    if any(w in title_lower for w in ['acquires', 'acquisition', 'buys', 'purchases', 'take-private']):
        return 'ACQUISITION'
    elif any(w in title_lower for w in ['joint venture', 'jv', 'partnership']):
        return 'JOINT_VENTURE'
    elif any(w in title_lower for w in ['invests', 'investment', 'raises', 'funding', 'round']):
        return 'INVESTMENT'
    elif any(w in title_lower for w in ['lease', 'leases', 'leasing']):
        return 'LEASE'
    elif any(w in title_lower for w in ['expand', 'expansion', 'build', 'construction']):
        return 'CAPEX'
    else:
        return 'ACQUISITION'


def get_fallback_detected_deals():
    """Curated fallback deals - real industry transactions"""
    return [
        {'deal': 'Blackstone Acquires AirTrunk', 'buyer': 'Blackstone', 'seller': 'AirTrunk', 'value': '$24B', 'type': 'ACQUISITION', 'market': 'APAC', 'confidence': 0.95, 'date': 'Dec 2024'},
        {'deal': 'KKR CyrusOne Take-Private', 'buyer': 'KKR', 'seller': 'CyrusOne', 'value': '$15B', 'type': 'ACQUISITION', 'market': 'Global', 'confidence': 0.92, 'date': '2024'},
        {'deal': 'DigitalBridge Switch Acquisition', 'buyer': 'DigitalBridge', 'seller': 'Switch', 'value': '$11B', 'type': 'ACQUISITION', 'market': 'North America', 'confidence': 0.90, 'date': '2024'},
        {'deal': 'Blackstone QTS Take-Private', 'buyer': 'Blackstone', 'seller': 'QTS', 'value': '$10B', 'type': 'ACQUISITION', 'market': 'North America', 'confidence': 0.95, 'date': '2024'},
        {'deal': 'CoreWeave AI Infrastructure Raise', 'buyer': 'CoreWeave', 'seller': 'Various Investors', 'value': '$7.5B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.88, 'date': '2024'},
        {'deal': 'GIC Equinix Joint Venture', 'buyer': 'GIC', 'seller': 'Equinix', 'value': '$6.9B', 'type': 'JOINT_VENTURE', 'market': 'APAC', 'confidence': 0.91, 'date': '2024'},
        {'deal': 'Brookfield Data4 Acquisition', 'buyer': 'Brookfield', 'seller': 'Data4', 'value': '$5B', 'type': 'ACQUISITION', 'market': 'EMEA', 'confidence': 0.87, 'date': '2024'},
        {'deal': 'Micron Singapore Fab Investment', 'buyer': 'Micron', 'seller': 'Singapore EDB', 'value': '$7B', 'type': 'INVESTMENT', 'market': 'APAC', 'confidence': 0.93, 'date': 'Jan 2025'},
        {'deal': 'Aware Super Vantage APAC Stake', 'buyer': 'Aware Super', 'seller': 'Vantage APAC', 'value': '$300M', 'type': 'INVESTMENT', 'market': 'APAC', 'confidence': 0.85, 'date': 'Jan 2025'},
        {'deal': 'SoftBank Stargate Data Center', 'buyer': 'SoftBank', 'seller': 'OpenAI JV', 'value': '$50B', 'type': 'JOINT_VENTURE', 'market': 'North America', 'confidence': 0.78, 'date': 'Jan 2025'},
        {'deal': 'DigitalBridge Vantage Stake', 'buyer': 'DigitalBridge', 'seller': 'Vantage', 'value': '$4B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.85, 'date': '2024'},
        {'deal': 'TPG EdgeCore Investment', 'buyer': 'TPG', 'seller': 'EdgeCore', 'value': '$2.5B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.82, 'date': '2024'},
    ]


def is_valid_company_name(name):
    """Check if a string looks like a valid company name (not a news snippet)"""
    if not name or not isinstance(name, str):
        return False
    
    name = name.strip()
    
    # Too short or too long
    if len(name) < 2 or len(name) > 50:
        return False
    
    # Contains sentence fragments (multiple spaces, common words)
    garbage_indicators = [
        ' the ', ' a ', ' an ', ' is ', ' are ', ' was ', ' were ',
        ' to ', ' for ', ' with ', ' from ', ' that ', ' this ',
        ' will ', ' would ', ' could ', ' should ', ' has ', ' have ',
        ' been ', ' being ', ' their ', ' they ', ' which ', ' what ',
        '...', ' and ', ' or ', ' but ', ' also ', ' just ', ' very ',
        'http', 'www.', '.com', '.org',
        ' says ', ' said ', ' claims ', ' reported ', ' announced ',
        ' today ', ' yesterday ', ' following ', ' according ',
    ]
    
    name_lower = name.lower()
    for indicator in garbage_indicators:
        if indicator in name_lower:
            return False
    
    # Has too many words (likely a sentence fragment)
    if len(name.split()) > 5:
        return False
    
    # Starts with lowercase (likely mid-sentence)
    if name[0].islower():
        return False
    
    return True


def parse_deal_value_to_display(value_millions):
    """Convert value in millions to display string"""
    if not value_millions:
        return 'Undisclosed'
    
    try:
        val = float(value_millions)
        if val >= 1000:
            return f"${val/1000:.1f}B"
        else:
            return f"${val:.0f}M"
    except:
        return 'Undisclosed'


def parse_deal_value_to_number(value_str):
    """Parse deal value string to number for calculations"""
    if not value_str or value_str == 'Undisclosed':
        return 0
    
    import re
    match = re.search(r'([\d.]+)', str(value_str))
    if match:
        num = float(match.group(1))
        if 'B' in str(value_str).upper():
            return num * 1e9
        elif 'M' in str(value_str).upper():
            return num * 1e6
    return 0

@autopilot_bp.route('/api/autopilot/transactions')
@require_plan('pro')
def autopilot_detected_transactions():
    """Return AI-detected transactions with field aliases for frontend compatibility"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                id,
                COALESCE(buyer, '') as buyer,
                COALESCE(seller, '') as seller,
                COALESCE(value, 0) as value_millions,
                COALESCE(type, 'acquisition') as deal_type,
                0.85 as confidence,
                date as discovered_at
            FROM deals
            WHERE buyer IS NOT NULL AND seller IS NOT NULL
            AND buyer NOT IN ('TBD', 'Unknown', 'N/A', '')
            AND seller NOT IN ('TBD', 'Unknown', 'N/A', '')
            ORDER BY date DESC
            LIMIT 30
        """)
        
        rows = cursor.fetchall()
        
        valid_deals = []
        
        if rows:
            for row in rows:
                deal_id, buyer, seller, value_millions, deal_type, confidence, discovered_at = row
                
                buyer_valid = is_valid_company_name(buyer)
                seller_valid = is_valid_company_name(seller)
                
                if buyer_valid or seller_valid:
                    clean_buyer = buyer.strip() if buyer_valid else 'Undisclosed'
                    clean_seller = seller.strip() if seller_valid else 'Undisclosed'
                    
                    try:
                        conf = float(confidence) if confidence else 0.75
                        if conf < 0.1:
                            conf = 0.5 + (conf * 4)
                        conf = max(0.5, min(0.98, conf))
                    except:
                        conf = 0.75
                    
                    val_millions = 0
                    if value_millions:
                        try:
                            val_millions = float(value_millions)
                        except:
                            pass
                    
                    valid_deals.append({
                        'deal': f"{clean_buyer} - {clean_seller}",
                        'buyer': clean_buyer,
                        'seller': clean_seller,
                        'value': parse_deal_value_to_display(val_millions),
                        'type': (deal_type or 'acquisition').upper(),
                        'market': 'Global',
                        'confidence': round(conf, 2),
                        'date': discovered_at.strftime('%b %Y') if hasattr(discovered_at, 'strftime') else 'Recent',
                        'target': clean_seller,
                        'value_millions': val_millions,
                        'deal_type': (deal_type or 'acquisition').lower(),
                        'discovered_at': discovered_at.isoformat() if hasattr(discovered_at, 'isoformat') else str(discovered_at)
                    })
        
        if len(valid_deals) < 5:
            print(f"Only {len(valid_deals)} valid deals found, using fallback data")
            deals = get_fallback_detected_deals()
        else:
            deals = valid_deals[:15]
        
        total_volume = sum(d.get('value_millions', 0) or 0 for d in deals)
        avg_confidence = sum(d.get('confidence', 0) for d in deals) / max(len(deals), 1)
        
        return jsonify({
            'success': True,
            'deals': deals,
            'transactions': deals,
            'stats': {
                'total_volume': f"${total_volume/1000:.1f}B" if total_volume >= 1000 else f"${total_volume:.0f}M",
                'deal_count': len(deals),
                'avg_confidence': round(avg_confidence * 100, 1),
                'last_scan': datetime.now().strftime('%I:%M %p'),
                'source': 'curated' if len(valid_deals) < 5 else 'detected'
            }
        })
        
    except Exception as e:
        print(f"Detected transactions error: {e}")
        fallback = get_fallback_detected_deals()
        total_volume = sum(d.get('value_millions', 0) or 0 for d in fallback)
        
        return jsonify({
            'success': True,
            'deals': fallback,
            'transactions': fallback,
            'stats': {
                'total_volume': f"${total_volume/1000:.1f}B",
                'deal_count': len(fallback),
                'avg_confidence': 88.0,
                'last_scan': datetime.now().strftime('%I:%M %p'),
                'source': 'curated'
            }
        })
    finally:
        if conn:
            conn.close()


def get_fallback_detected_deals():
    """Curated fallback deals with all field aliases"""
    deals = [
        {'deal': 'Blackstone Acquires AirTrunk', 'buyer': 'Blackstone', 'seller': 'AirTrunk', 'value': '$24B', 'type': 'ACQUISITION', 'market': 'APAC', 'confidence': 0.95, 'date': 'Dec 2024', 'value_millions': 24000},
        {'deal': 'KKR CyrusOne Take-Private', 'buyer': 'KKR', 'seller': 'CyrusOne', 'value': '$15B', 'type': 'ACQUISITION', 'market': 'Global', 'confidence': 0.92, 'date': '2024', 'value_millions': 15000},
        {'deal': 'DigitalBridge Switch Acquisition', 'buyer': 'DigitalBridge', 'seller': 'Switch', 'value': '$11B', 'type': 'ACQUISITION', 'market': 'North America', 'confidence': 0.90, 'date': '2024', 'value_millions': 11000},
        {'deal': 'Blackstone QTS Take-Private', 'buyer': 'Blackstone', 'seller': 'QTS', 'value': '$10B', 'type': 'ACQUISITION', 'market': 'North America', 'confidence': 0.95, 'date': '2024', 'value_millions': 10000},
        {'deal': 'CoreWeave AI Infrastructure Raise', 'buyer': 'CoreWeave', 'seller': 'Various Investors', 'value': '$7.5B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.88, 'date': '2024', 'value_millions': 7500},
        {'deal': 'GIC Equinix Joint Venture', 'buyer': 'GIC', 'seller': 'Equinix', 'value': '$6.9B', 'type': 'JOINT_VENTURE', 'market': 'APAC', 'confidence': 0.91, 'date': '2024', 'value_millions': 6900},
        {'deal': 'Brookfield Data4 Acquisition', 'buyer': 'Brookfield', 'seller': 'Data4', 'value': '$5B', 'type': 'ACQUISITION', 'market': 'EMEA', 'confidence': 0.87, 'date': '2024', 'value_millions': 5000},
        {'deal': 'Micron Singapore Fab Investment', 'buyer': 'Micron', 'seller': 'Singapore EDB', 'value': '$7B', 'type': 'INVESTMENT', 'market': 'APAC', 'confidence': 0.93, 'date': 'Jan 2025', 'value_millions': 7000},
        {'deal': 'Aware Super Vantage APAC Stake', 'buyer': 'Aware Super', 'seller': 'Vantage APAC', 'value': '$300M', 'type': 'INVESTMENT', 'market': 'APAC', 'confidence': 0.85, 'date': 'Jan 2025', 'value_millions': 300},
        {'deal': 'SoftBank Stargate Data Center', 'buyer': 'SoftBank', 'seller': 'OpenAI JV', 'value': '$50B', 'type': 'JOINT_VENTURE', 'market': 'North America', 'confidence': 0.78, 'date': 'Jan 2025', 'value_millions': 50000},
        {'deal': 'DigitalBridge Vantage Stake', 'buyer': 'DigitalBridge', 'seller': 'Vantage', 'value': '$4B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.85, 'date': '2024', 'value_millions': 4000},
        {'deal': 'TPG EdgeCore Investment', 'buyer': 'TPG', 'seller': 'EdgeCore', 'value': '$2.5B', 'type': 'INVESTMENT', 'market': 'North America', 'confidence': 0.82, 'date': '2024', 'value_millions': 2500},
    ]
    # Add aliases for frontend
    for d in deals:
        d['target'] = d['seller']
        d['deal_type'] = d['type'].lower()
        d['discovered_at'] = '2024-12-15'
    return deals


@autopilot_bp.route('/api/autopilot/capacity-pipeline')
@require_plan('pro')
def autopilot_capacity_pipeline():
    """Return capacity pipeline data - merges DB with fallback if < 20 projects or < 5 GW"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT operator, market, capacity_mw, phase, status, 
                   completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE operator IS NOT NULL AND operator != 'Unknown' AND capacity_mw > 0
            ORDER BY capacity_mw DESC
            LIMIT 200
        """)
        
        rows = cursor.fetchall()
        db_projects = []
        
        for row in rows:
            operator = row[0]
            market = row[1] or 'Multiple Markets'
            capacity = row[2] or 0
            phase = (row[3] or '').lower()
            status_raw = (row[4] or '').lower()
            delivery = row[5] or 'TBD'
            notes = row[6] or f"{operator} Expansion"
            
            if 'construct' in phase or 'construct' in status_raw or 'under' in phase:
                status_normalized = 'construction'
            elif 'operational' in phase or 'complete' in phase:
                status_normalized = 'operational'
            else:
                status_normalized = 'announced'
            
            db_projects.append({
                'operator': operator,
                'project': notes,
                'capacity_mw': capacity,
                'location': market,
                'status': status_normalized,
                'delivery': delivery,
                'preleased': 50,
                'confidence': 0.80 if row[7] == 'medium' else 0.90 if row[7] == 'high' else 0.60
            })
        
        fallback = get_fallback_pipeline_projects()
        seen_operators_markets = set()
        for p in db_projects:
            key = (p['operator'].lower().split('/')[0].strip(), (p.get('location') or '').lower())
            seen_operators_markets.add(key)
        for fp in fallback:
            key = (fp['operator'].lower().split('/')[0].strip(), fp.get('location', fp.get('market', '')).lower())
            if key not in seen_operators_markets:
                db_projects.append(fp)
                seen_operators_markets.add(key)
        
        projects = sorted(db_projects, key=lambda x: x.get('capacity_mw', 0) or 0, reverse=True)
        
        total_mw = sum(p.get('capacity_mw', 0) or 0 for p in projects)
        construction = len([p for p in projects if 'construction' in (p.get('status') or '')])
        announced = len([p for p in projects if p.get('status') == 'announced'])
        avg_preleased = sum(p.get('preleased', 50) for p in projects) / len(projects) if projects else 50
        
        return jsonify({
            'success': True,
            'pipeline': projects,
            'stats': {
                'total_gw': round(total_mw / 1000, 1),
                'total_mw': total_mw,
                'project_count': len(projects),
                'under_construction': construction,
                'announced': announced,
                'pre_leased_pct': round(avg_preleased)
            }
        })
        
    except Exception as e:
        print(f"Pipeline error: {e}")
        fallback_projects = get_fallback_pipeline_projects()
        total_mw = sum(p.get('capacity_mw', 0) for p in fallback_projects)
        construction = len([p for p in fallback_projects if 'construction' in (p.get('status') or '')])
        
        return jsonify({
            'success': True,
            'pipeline': fallback_projects,
            'stats': {
                'total_gw': round(total_mw / 1000, 1),
                'total_mw': total_mw,
                'project_count': len(fallback_projects),
                'under_construction': construction,
                'announced': len(fallback_projects) - construction,
                'pre_leased_pct': 73
            }
        })
    finally:
        if conn:
            conn.close()


def get_fallback_pipeline_projects():
    """Current pipeline data derived from PIPELINE_DATA (Feb 2026)"""
    projects = []
    for p in _PIPELINE_DATA:
        projects.append({
            'company': p['company'],
            'operator': p['company'],
            'name': p['project'],
            'project': p['project'],
            'capacity_mw': p['capacity'],
            'market': p['market'],
            'location': p['market'],
            'status': p['status'],
            'delivery': p['delivery'],
            'preleased': 90 if p.get('preleased') else 40,
            'confidence': 0.95 if p['status'] == 'operational' else 0.90 if p['status'] == 'construction' else 0.75,
        })
    return projects


@autopilot_bp.route('/api/autopilot/seo/status')
def seo_status():
    """Get SEO promotion status"""
    try:
        from seo_promotion_engine import get_seo_stats
        stats = get_seo_stats()
        return jsonify({
            'enabled': True,
            'interval': '6 hours',
            'stats': stats
        })
    except Exception as e:
        return jsonify({'error': str(e), 'enabled': False}), 500

@autopilot_bp.route('/api/autopilot/seo/run', methods=['POST'])
@require_plan('enterprise')
@require_auth
def seo_run():
    """Manually trigger SEO promotion cycle"""
    try:
        from seo_promotion_engine import run_seo_promotion
        result = run_seo_promotion()
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/autopilot/seo/sitemap')
def seo_sitemap():
    """Generate and return sitemap"""
    try:
        from seo_promotion_engine import get_seo_engine
        engine = get_seo_engine()
        sitemap = engine.generate_sitemap()
        return sitemap, 200, {'Content-Type': 'application/xml'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/autopilot/seo/press-release', methods=['POST'])
@require_plan('enterprise')
@require_auth
def seo_press_release():
    """Generate a press release"""
    try:
        from seo_promotion_engine import generate_press_release
        data = request.get_json() or {}
        topic = data.get('topic', 'platform_update')
        result = generate_press_release(topic)
        return jsonify({'success': True, 'press_release': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/autopilot/social/test', methods=['POST', 'GET'])
@require_plan('enterprise')
def social_test():
    """Test social posting to X and LinkedIn"""
    if not AUTOPILOT_AVAILABLE or not discovery_engine:
        return jsonify({'error': 'Auto-pilot not available'}), 503
    
    data = request.get_json() if request.method == 'POST' else {}
    platform = data.get('platform', 'both') if data else 'both'
    custom_message = data.get('message', '') if data else ''
    
    # Default test message
    test_message = custom_message or """🚀 DC Hub is live!

Track 20,000+ data centers across 140+ countries.
Real-time market intelligence for hyperscale infrastructure.

Explore now: https://dchub.cloud

#DataCenter #Infrastructure #CloudComputing"""
    
    results = {}
    
    if platform in ['twitter', 'both']:
        result = discovery_engine.social_poster.post_to_twitter(test_message)
        results['twitter'] = result
        print(f"🐦 Twitter test: {'✅ Success' if result.get('success') else '❌ ' + str(result.get('error', 'Failed'))}")
    
    if platform in ['linkedin', 'both']:
        result = discovery_engine.social_poster.post_to_linkedin(test_message)
        results['linkedin'] = result
        print(f"💼 LinkedIn test: {'✅ Success' if result.get('success') else '❌ ' + str(result.get('error', 'Failed'))}")
    
    return jsonify({
        'success': any(r.get('success') for r in results.values()),
        'results': results,
        'message': test_message
    })

# =============================================================================
# EVOLUTION ENGINE API - Continuous Self-Improvement System
# =============================================================================

try:
    from evolution_engine import get_evolution_engine, run_evolution_cycle, get_learning_status, teach_topic
    EVOLUTION_AVAILABLE = True
except Exception:
    EVOLUTION_AVAILABLE = False

@autopilot_bp.route('/api/evolution/status')
def evolution_status():
    """Get current Evolution Engine status and learning statistics"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available', 'available': False}), 503
    
    try:
        status = get_learning_status()
        return jsonify({
            'available': True,
            **status
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/evolution/run', methods=['POST'])
@require_api_key
def evolution_run():
    """Manually trigger an evolution cycle (requires API key)"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available'}), 503
    
    try:
        result = run_evolution_cycle()
        return jsonify({
            'success': True,
            'result': result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@autopilot_bp.route('/api/evolution/teach', methods=['POST'])
@require_api_key
def evolution_teach():
    """Teach the system about a specific topic using AI (requires API key)"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available'}), 503
    
    data = request.get_json() or {}
    topic = data.get('topic', '')
    
    if not topic:
        return jsonify({'error': 'Topic is required'}), 400
    
    try:
        result = teach_topic(topic)
        return jsonify({
            'success': True,
            'topic': topic,
            'knowledge': result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@autopilot_bp.route('/api/evolution/suggestions')
def evolution_suggestions():
    """Get suggested improvements for the platform"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available'}), 503
    
    try:
        engine = get_evolution_engine()
        suggestions = engine.suggest_next_improvements()
        return jsonify({
            'suggestions': suggestions
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/evolution/knowledge')
def evolution_knowledge():
    """Get the Evolution Engine's current knowledge base summary"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available'}), 503
    
    try:
        engine = get_evolution_engine()
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT category, COUNT(*) as count 
            FROM knowledge_items 
            GROUP BY category
        ''')
        categories = dict(cursor.fetchall())
        
        cursor.execute('''
            SELECT term, definition FROM industry_glossary 
            ORDER BY term LIMIT 50
        ''')
        glossary = [{'term': r[0], 'definition': r[1]} for r in cursor.fetchall()]
        
        cursor.execute('''
            SELECT action_type, description, timestamp 
            FROM evolution_log 
            ORDER BY timestamp DESC LIMIT 20
        ''')
        recent_actions = [{'type': r[0], 'description': r[1], 'time': r[2]} for r in cursor.fetchall()]
        
        conn.close()
        
        return jsonify({
            'knowledge_categories': categories,
            'glossary_sample': glossary,
            'recent_actions': recent_actions,
            'total_knowledge_items': sum(categories.values()) if categories else 0
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/evolution/quality-issues')
def evolution_quality_issues():
    """Get open quality issues detected by the Evolution Engine"""
    if not EVOLUTION_AVAILABLE:
        return jsonify({'error': 'Evolution Engine not available'}), 503
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, issue_type, severity, entity_type, description, auto_fixable, discovered_at
            FROM quality_issues
            WHERE fixed = 0
            ORDER BY 
                CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                discovered_at DESC
            LIMIT 50
        ''')
        
        issues = [{
            'id': r[0], 'type': r[1], 'severity': r[2], 
            'entity_type': r[3], 'description': r[4], 
            'auto_fixable': bool(r[5]), 'discovered_at': r[6]
        } for r in cursor.fetchall()]
        
        conn.close()
        
        return jsonify({
            'issues': issues,
            'total': len(issues)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# DC EXPERT BRAIN API (v89)
# =============================================================================

try:
    from dc_expert_brain import get_expert_brain, run_learning_cycle, start_auto_learning
    BRAIN_AVAILABLE = True
except Exception:
    BRAIN_AVAILABLE = False

@autopilot_bp.route('/api/brain/status')
def brain_status():
    """Get DC Expert Brain learning status"""
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'DC Expert Brain not available'}), 503
    
    try:
        brain = get_expert_brain()
        status = brain.get_learning_status()
        return jsonify({
            'success': True,
            **status
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/brain/learn', methods=['POST'])
@require_plan('enterprise')
def brain_learn():
    """Trigger a learning cycle"""
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'DC Expert Brain not available'}), 503
    
    try:
        results = run_learning_cycle()
        return jsonify({
            'success': True,
            'results': results
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/brain/ask')
@require_plan('enterprise')
@protect_data
def brain_ask():
    """Ask the expert brain a question"""
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'DC Expert Brain not available'}), 503
    
    question = request.args.get('q', '')
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    
    try:
        brain = get_expert_brain()
        answer = brain.answer_question(question)
        trends = brain.get_current_trends()[:5]
        
        return jsonify({
            'success': True,
            'question': question,
            'answer': answer,
            'trends': trends
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/brain/market/<market>')
@require_plan('enterprise')
@protect_data
def brain_market(market):
    """Get market intelligence"""
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'DC Expert Brain not available'}), 503
    
    try:
        brain = get_expert_brain()
        insight = brain.get_market_insight(market)
        return jsonify({
            'success': True,
            **insight
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@autopilot_bp.route('/api/brain/operator/<operator>')
@require_plan('enterprise')
@protect_data
def brain_operator(operator):
    """Get operator intelligence"""
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'DC Expert Brain not available'}), 503
    
    try:
        brain = get_expert_brain()
        insight = brain.get_operator_insight(operator)
        return jsonify({
            'success': True,
            **insight
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

print("🧠 DC Expert Brain routes registered")

print("🧬 Evolution Engine routes registered:")
print("   GET  /api/evolution/status - Learning status")
print("   POST /api/evolution/run - Trigger evolution cycle")
print("   POST /api/evolution/teach - Teach about a topic")
print("   GET  /api/evolution/suggestions - Get improvement suggestions")
print("   GET  /api/evolution/knowledge - View knowledge base")
print("   GET  /api/evolution/quality-issues - View quality issues")

