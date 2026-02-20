"""
DC Hub — Public Deals / Analytics API
=======================================
Drop this file into your Replit project alongside main.py.
Register the blueprint in main.py with:

    from deals_public_api import deals_public_bp
    app.register_blueprint(deals_public_bp)

Provides:
  GET /api/deals/public          → Deals with limited fields (no seller)
  GET /api/deals/public/stats    → Aggregate stats for KPI cards + charts

No API key required. Designed for the /analytics dashboard.

DATA QUALITY:
  - Normalizes all values to $M (millions)
  - Filters out junk records (TBD buyers, $0 values, unknown types)
  - Caps outliers at $100B to prevent bad scrapes from skewing totals
"""

from flask import Blueprint, jsonify, request
from datetime import datetime
import sqlite3
import os
from db_utils import get_db

deals_public_bp = Blueprint('deals_public', __name__)

DB_PATH = os.environ.get('DB_PATH', 'dc_nexus.db')

# ─────────────────────────────────────────────────────────────
# DATA QUALITY CONSTANTS
# ─────────────────────────────────────────────────────────────

# Max plausible single DC transaction in $M.
# Largest real deal: Aligned/CPPIB ~$40B. Use $45B as ceiling.
MAX_DEAL_VALUE_M = 45000  # $45B in millions

# Buyers to exclude from analytics (garbage from auto-discovery)
JUNK_BUYERS = {'tbd', 'undisclosed', 'unknown', 'n/a', 'na', 'none', ''}

# Types to exclude
JUNK_TYPES = {'unknown', ''}

# Minimum value in $M to include in analytics (filters out noise)
MIN_VALUE_M = 10  # $10M minimum — sub-$10M deals are noise

# Hyperscaler annual CapEx / cloud budget entries that the AI scraper
# incorrectly added as "deals". These are NOT transactions.
# Pattern: buyer contains one of these AND value > $15B AND type is CapEx/Equity
HYPERSCALER_CAPEX_BUYERS = {
    'amazon aws', 'amazon', 'microsoft azure', 'microsoft',
    'google cloud', 'google', 'meta', 'apple', 'oracle',
    'nvidia', 'openai', 'softbank', 'intel', 'anthropic', 'xai',
}
HYPERSCALER_CAPEX_THRESHOLD_M = 15000  # $15B — real DC deals by these cos are under this


def get_deals_db():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    return conn


def safe_float(val, default=0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def normalize_value_to_millions(raw_value):
    """
    Normalize a raw value to $M (millions).

    The DB has values stored inconsistently:
      - Some in millions: 2250 means $2.25B -> keep as-is
      - Some in raw dollars: 175000000000 means $175B -> divide by 1M
      - Some in thousands: might exist too

    Heuristic:
      - If value > 500,000 -> likely stored in raw $ or $K, divide to get $M
      - If value <= 500,000 -> likely already in $M
      
    Why 500,000? The largest plausible deal in $M is ~100,000 ($100B).
    Anything above 500,000 is almost certainly in wrong units.
    """
    v = safe_float(raw_value)
    if v <= 0:
        return 0

    if v > 500000:
        # Likely stored in raw dollars — convert to millions
        v = v / 1000000

    # Cap at max plausible value
    if v > MAX_DEAL_VALUE_M:
        return 0  # Discard — clearly bad data

    return round(v, 1)


def is_quality_deal(buyer, value_m, deal_type):
    """Check if a deal record meets minimum quality standards."""
    if not buyer or buyer.strip().lower() in JUNK_BUYERS:
        return False
    if value_m < MIN_VALUE_M:
        return False
    if deal_type and deal_type.strip().lower() in JUNK_TYPES:
        return False

    # Exclude hyperscaler annual CapEx/budget entries.
    # These are corporate spending announcements, not DC transactions.
    # Real DC deals by hyperscalers (e.g. Microsoft buying Activision) are
    # not in this DB. What IS here: "Amazon AWS $75B CapEx 2025" — that's
    # Amazon's total cloud spend, not a data center acquisition.
    buyer_lower = buyer.strip().lower()
    for hs in HYPERSCALER_CAPEX_BUYERS:
        if hs in buyer_lower and value_m >= HYPERSCALER_CAPEX_THRESHOLD_M:
            return False

    return True


def deduplicate_deals(deals):
    """
    Remove near-duplicate deals. The AI scraper often creates multiple
    entries for the same deal scraped from different news sources on
    different days. Dedupe by: same buyer + same value + same type.
    Keep the most recent entry.
    """
    seen = {}
    for d in deals:
        # Key: buyer + value rounded to nearest $100M + type
        key = (
            d['buyer'].lower(),
            round(d['value_m'] / 100) * 100,  # bucket to nearest $100M
            d['type'].lower(),
        )
        if key not in seen:
            seen[key] = d
        else:
            # Keep the one with the more recent date
            existing_date = seen[key].get('date', '') or ''
            new_date = d.get('date', '') or ''
            if new_date > existing_date:
                seen[key] = d

    return list(seen.values())


def clean_buyer_name(name):
    """Normalize buyer names for consistency."""
    if not name:
        return 'Undisclosed'
    name = name.strip()
    BUYER_NORMALIZE = {
        'nvidia a': 'Nvidia',
        'nvidia': 'Nvidia',
        'NVIDIA': 'Nvidia',
        'microsoft': 'Microsoft',
        'amazon': 'Amazon',
        'google': 'Google',
        'meta': 'Meta',
        'apple': 'Apple',
        'blackstone': 'Blackstone',
        'brookfield': 'Brookfield',
        'digitalbridge': 'DigitalBridge',
        'kkr': 'KKR',
        'gic': 'GIC',
        'equinix': 'Equinix',
        'digital realty': 'Digital Realty',
    }
    return BUYER_NORMALIZE.get(name.lower(), name)


def clean_deal_type(raw_type):
    """Normalize deal types."""
    if not raw_type:
        return 'Other'
    t = raw_type.strip().lower()
    TYPE_MAP = {
        'ma': 'M&A',
        'm&a': 'M&A',
        'acquisition': 'M&A',
        'merger': 'M&A',
        'equity': 'Equity',
        'investment': 'Equity',
        'jv': 'Joint Venture',
        'joint venture': 'Joint Venture',
        'joint_venture': 'Joint Venture',
        'capex': 'CapEx',
        'capital expenditure': 'CapEx',
        'debt': 'Debt',
        'debt financing': 'Debt',
        'ai_contract': 'AI Contract',
        'ai contract': 'AI Contract',
        'ai_infra': 'AI Infrastructure',
        'ai infrastructure': 'AI Infrastructure',
        'land': 'Land',
        'land/development': 'Land',
        'development': 'Development',
        'expansion': 'Expansion',
        'partnership': 'Partnership',
    }
    return TYPE_MAP.get(t, raw_type.title())


# ─────────────────────────────────────────────────────────────
# Schema Discovery
# ─────────────────────────────────────────────────────────────

DEALS_TABLES = ['deals', 'transactions', 'transaction_intelligence']

COLUMN_MAP = {
    'buyer':  ['buyer', 'acquirer'],
    'seller': ['seller', 'target', 'seller_target'],
    'value':  ['value', 'value_millions', 'value_usd', 'deal_value'],
    'type':   ['type', 'deal_type'],
    'region': ['region'],
    'market': ['market'],
    'mw':     ['mw', 'capacity_mw', 'power_mw'],
    'date':   ['date', 'announced_date', 'discovered_at', 'created_at'],
    'year':   ['year'],
    'id':     ['id', 'deal_id'],
}


def discover_deals_schema():
    conn = None
    try:
        conn = get_deals_db()
        cursor = conn.cursor()
        for table in DEALS_TABLES:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                if count == 0:
                    continue
                cursor.execute(f"PRAGMA table_info({table})")
                db_columns = [row[1] for row in cursor.fetchall()]
                col_map = {}
                for canonical, candidates in COLUMN_MAP.items():
                    for c in candidates:
                        if c in db_columns:
                            col_map[canonical] = c
                            break
                return table, col_map, count
            except Exception:
                continue
        return None, {}, 0
    except Exception:
        return None, {}, 0
    finally:
        if conn:
            conn.close()


def fetch_all_deals_clean():
    """Fetch all deals from DB, normalize, and quality-filter."""
    table, col_map, total = discover_deals_schema()
    if not table:
        return [], 0

    select_cols = []
    for canonical in ['id', 'buyer', 'seller', 'value', 'type', 'region', 'market', 'mw', 'date', 'year']:
        if canonical in col_map:
            select_cols.append(f"{col_map[canonical]} AS {canonical}")

    if not select_cols:
        return [], 0

    date_col = col_map.get('date', col_map.get('year', 'rowid'))
    conn = None
    try:
        conn = get_deals_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {', '.join(select_cols)} FROM {table} ORDER BY {date_col} DESC")
        raw_rows = [dict(row) for row in cursor.fetchall()]
    except Exception:
        return [], 0
    finally:
        if conn:
            conn.close()

    raw_total = len(raw_rows)

    # Normalize and filter
    clean_deals = []
    for row in raw_rows:
        value_m = normalize_value_to_millions(row.get('value', 0))
        buyer = clean_buyer_name(row.get('buyer', ''))
        deal_type = clean_deal_type(row.get('type', ''))

        if not is_quality_deal(buyer, value_m, deal_type):
            continue

        # Extract year
        year = row.get('year')
        if not year and row.get('date'):
            try:
                year = int(str(row['date'])[:4])
            except (ValueError, TypeError):
                year = None

        # Skip deals with no year (can't chart them)
        if not year:
            continue

        clean_deals.append({
            "date": row.get('date', ''),
            "buyer": buyer,
            "value_m": value_m,
            "type": deal_type,
            "region": row.get('region', '') or '',
            "market": row.get('market', '') or '',
            "mw": safe_float(row.get('mw', 0)) or None,
            "year": year,
        })

    # Deduplicate near-identical entries from multi-source scraping
    clean_deals = deduplicate_deals(clean_deals)

    # Sort by date descending
    clean_deals.sort(key=lambda d: d.get('date', '') or '', reverse=True)

    return clean_deals, raw_total


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@deals_public_bp.route('/api/deals/public', methods=['GET'])
def get_public_deals():
    """
    GET /api/deals/public
    Returns quality-filtered deals with normalized values in $M.
    Seller names redacted.
    """
    try:
        year_filter = request.args.get('year', '').strip()
        type_filter = request.args.get('type', '').strip()
        region_filter = request.args.get('region', '').strip().lower()
        limit = min(int(request.args.get('limit', 1000)), 1000)
        offset = int(request.args.get('offset', 0))

        all_deals, raw_total = fetch_all_deals_clean()

        filtered = all_deals
        if year_filter:
            filtered = [d for d in filtered if str(d.get('year', '')) == year_filter]
        if type_filter:
            filtered = [d for d in filtered if d['type'].lower() == type_filter.lower()]
        if region_filter:
            filtered = [d for d in filtered if region_filter in d['region'].lower()]

        total = len(filtered)
        page = filtered[offset:offset + limit]

        return jsonify({
            "success": True,
            "count": len(page),
            "total": total,
            "total_unfiltered": len(all_deals),
            "raw_db_count": raw_total,
            "transactions": page,
            "value_unit": "millions_usd",
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Database temporarily busy: {str(e)}",
            "transactions": [],
            "count": 0
        }), 503


@deals_public_bp.route('/api/deals/public/stats', methods=['GET'])
def get_public_deals_stats():
    """
    GET /api/deals/public/stats
    Aggregate stats from quality-filtered, normalized deals.
    All values in $M (millions USD).
    """
    try:
        all_deals, raw_total = fetch_all_deals_clean()

        if not all_deals:
            return jsonify({
                "success": True,
                "total_deals": 0,
                "message": "No quality deals available"
            })

        total_deals = len(all_deals)
        total_value_m = sum(d['value_m'] for d in all_deals)
        avg_deal_m = total_value_m / total_deals if total_deals > 0 else 0
        total_mw = sum(d['mw'] or 0 for d in all_deals)

        largest = max(all_deals, key=lambda d: d['value_m'])
        largest_deal = {"value_m": largest['value_m'], "buyer": largest['buyer']}

        stats_by_year = {}
        for d in all_deals:
            yr = str(d.get('year', 'Unknown'))
            if yr not in stats_by_year:
                stats_by_year[yr] = {"count": 0, "value_m": 0}
            stats_by_year[yr]["count"] += 1
            stats_by_year[yr]["value_m"] += d['value_m']
        for yr in stats_by_year:
            stats_by_year[yr]["value_m"] = round(stats_by_year[yr]["value_m"], 1)

        stats_by_type = {}
        for d in all_deals:
            t = d['type'] or 'Other'
            if t not in stats_by_type:
                stats_by_type[t] = {"count": 0, "value_m": 0}
            stats_by_type[t]["count"] += 1
            stats_by_type[t]["value_m"] += d['value_m']
        for t in stats_by_type:
            stats_by_type[t]["value_m"] = round(stats_by_type[t]["value_m"], 1)

        stats_by_region = {}
        for d in all_deals:
            r = d['region'] or 'Unknown'
            if r not in stats_by_region:
                stats_by_region[r] = {"count": 0, "value_m": 0}
            stats_by_region[r]["count"] += 1
            stats_by_region[r]["value_m"] += d['value_m']
        for r in stats_by_region:
            stats_by_region[r]["value_m"] = round(stats_by_region[r]["value_m"], 1)

        buyer_agg = {}
        for d in all_deals:
            b = d['buyer']
            if b not in buyer_agg:
                buyer_agg[b] = {"count": 0, "value_m": 0}
            buyer_agg[b]["count"] += 1
            buyer_agg[b]["value_m"] += d['value_m']

        top_buyers = sorted(
            [{"buyer": k, "deal_count": v["count"], "total_value_m": round(v["value_m"], 1)}
             for k, v in buyer_agg.items()],
            key=lambda x: x["deal_count"], reverse=True
        )[:15]

        monthly = {}
        for d in all_deals:
            if d['date']:
                month = str(d['date'])[:7]
                if len(month) == 7:
                    if month not in monthly:
                        monthly[month] = {"count": 0, "value_m": 0}
                    monthly[month]["count"] += 1
                    monthly[month]["value_m"] += d['value_m']

        monthly_activity = [
            {"month": k, "count": v["count"], "value_m": round(v["value_m"], 1)}
            for k, v in sorted(monthly.items())
            if k >= '2024-01'
        ]

        return jsonify({
            "success": True,
            "total_deals": total_deals,
            "total_value_m": round(total_value_m, 1),
            "avg_deal_size_m": round(avg_deal_m, 1),
            "total_mw": round(total_mw),
            "largest_deal": largest_deal,
            "stats_by_year": stats_by_year,
            "stats_by_type": stats_by_type,
            "stats_by_region": stats_by_region,
            "top_buyers": top_buyers,
            "monthly_activity": monthly_activity,
            "value_unit": "millions_usd",
            "data_quality": {
                "raw_db_records": raw_total,
                "quality_filtered": total_deals,
                "removed": raw_total - total_deals,
                "normalization": "All values in $M USD",
                "outlier_cap": f"${MAX_DEAL_VALUE_M/1000:.0f}B max",
                "min_threshold": f"${MIN_VALUE_M}M",
            },
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Database temporarily busy: {str(e)}",
            "total_deals": 0
        }), 503


@deals_public_bp.route('/api/deals/public/recent', methods=['GET'])
def get_public_deals_recent():
    """
    GET /api/deals/public/recent
    Returns the most recent deals, sorted by date descending.
    """
    try:
        limit = min(int(request.args.get('limit', 20)), 100)
        all_deals, raw_total = fetch_all_deals_clean()
        sorted_deals = sorted(
            all_deals,
            key=lambda d: d.get('date') or '1900-01-01',
            reverse=True
        )
        page = sorted_deals[:limit]
        return jsonify({
            "success": True,
            "count": len(page),
            "total": len(all_deals),
            "deals": page,
            "value_unit": "millions_usd",
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Database temporarily busy: {str(e)}",
            "deals": [],
            "count": 0
        }), 503


@deals_public_bp.route('/api/deals/public/search', methods=['GET'])
def search_public_deals():
    """
    GET /api/deals/public/search?q=equinix
    Search deals by buyer name, type, region, or location.
    """
    try:
        q = request.args.get('q', '').strip().lower()
        limit = min(int(request.args.get('limit', 50)), 200)

        if not q:
            return jsonify({
                "success": False,
                "error": "Missing search query parameter 'q'",
                "deals": [],
                "count": 0
            }), 400

        all_deals, _ = fetch_all_deals_clean()
        matches = [
            d for d in all_deals
            if q in (d.get('buyer') or '').lower()
            or q in (d.get('type') or '').lower()
            or q in (d.get('region') or '').lower()
            or q in (d.get('location') or '').lower()
        ]
        page = matches[:limit]
        return jsonify({
            "success": True,
            "query": q,
            "count": len(page),
            "total": len(matches),
            "deals": page,
            "value_unit": "millions_usd",
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Database temporarily busy: {str(e)}",
            "deals": [],
            "count": 0
        }), 503
