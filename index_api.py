"""
DC Hub Global Data Center Index API (index_api.py)
Flask Blueprint — mounts at /api/index
v3.0 — Parallel market scoring + 60 global markets
"""

import time
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

from flask import Blueprint, jsonify, request, g
from db_utils import get_db

logger = logging.getLogger(__name__)

index_bp = Blueprint('index', __name__, url_prefix='/api/index')

# ─── Config cache ────────────────────────────────────────────────────────────
_config_cache = {}
_config_ts    = 0
CONFIG_TTL    = 600  # 10 min

# ─── Market result cache ──────────────────────────────────────────────────────
_market_cache = {}
_market_ts    = {}
MARKET_TTL    = 3600  # 1 hour

# ─── Default config ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Table names
    'fac_table':            'facilities',
    'txn_table':            'deals',
    'mi_table':             'market_intelligence',
    'power_table':          'discovered_power_plants',
    'sub_table':            'substations',
    'tx_table':             'discovered_transmission_lines',
    'fiber_table':          'fiber_routes',
    # Facility status buckets
    'fac_status_operational': 'active,Operational',
    'fac_status_pipeline':    'Under Construction,Construction,Planned,Planning,Announced,announced,planned',
    # Source toggles
    'mi_enabled':    'true',
    'power_enabled': 'true',
    'sub_enabled':   'true',
    'tx_enabled':    'true',
    'fiber_enabled': 'true',
    # Power table column names
    'power_city_col':  'market',
    'power_state_col': 'state',
    # Sub-index weights (must sum to 100)
    'w_dhci': '30',
    'w_dhri': '25',
    'w_dhpi': '20',
    'w_dhdi': '15',
    'w_dhpw': '10',
    # Admin
    'admin_key': '',
    # Thread pool size for parallel market scoring
    'market_threads': '12',
}

# ─── 60 Global Markets ────────────────────────────────────────────────────────
MARKETS = [
    # North America - US
    {'id':'nova',    'name':'Northern Virginia, US',    'region':'us',    'country':'US', 'city':'Ashburn',      'state':'VA', 'aliases':['nova','northern virginia','ashburn','loudoun']},
    {'id':'dal',     'name':'Dallas/Fort Worth, US',    'region':'us',    'country':'US', 'city':'Dallas',       'state':'TX', 'aliases':['dal','dallas','fort worth','dfw']},
    {'id':'phx',     'name':'Phoenix, US',              'region':'us',    'country':'US', 'city':'Phoenix',      'state':'AZ', 'aliases':['phx','phoenix','chandler','mesa']},
    {'id':'chi',     'name':'Chicago, US',              'region':'us',    'country':'US', 'city':'Chicago',      'state':'IL', 'aliases':['chi','chicago']},
    {'id':'nyc',     'name':'New York/New Jersey, US',  'region':'us',    'country':'US', 'city':'Secaucus',     'state':'NJ', 'aliases':['nyc','new york','new jersey','secaucus']},
    {'id':'sea',     'name':'Seattle, US',              'region':'us',    'country':'US', 'city':'Seattle',      'state':'WA', 'aliases':['sea','seattle','quincy']},
    {'id':'sfo',     'name':'San Francisco Bay, US',    'region':'us',    'country':'US', 'city':'San Jose',     'state':'CA', 'aliases':['sfo','san jose','silicon valley','santa clara']},
    {'id':'lax',     'name':'Los Angeles, US',          'region':'us',    'country':'US', 'city':'Los Angeles',  'state':'CA', 'aliases':['lax','los angeles']},
    {'id':'atl',     'name':'Atlanta, US',              'region':'us',    'country':'US', 'city':'Atlanta',      'state':'GA', 'aliases':['atl','atlanta']},
    {'id':'bos',     'name':'Boston, US',               'region':'us',    'country':'US', 'city':'Boston',       'state':'MA', 'aliases':['bos','boston']},
    {'id':'den',     'name':'Denver, US',               'region':'us',    'country':'US', 'city':'Denver',       'state':'CO', 'aliases':['den','denver']},
    {'id':'mia',     'name':'Miami, US',                'region':'us',    'country':'US', 'city':'Miami',        'state':'FL', 'aliases':['mia','miami']},
    {'id':'iah',     'name':'Houston, US',              'region':'us',    'country':'US', 'city':'Houston',      'state':'TX', 'aliases':['iah','houston']},
    {'id':'msp',     'name':'Minneapolis, US',          'region':'us',    'country':'US', 'city':'Minneapolis',  'state':'MN', 'aliases':['msp','minneapolis']},
    {'id':'slc',     'name':'Salt Lake City, US',       'region':'us',    'country':'US', 'city':'Salt Lake City','state':'UT', 'aliases':['slc','salt lake city']},
    # North America - Canada
    {'id':'yyz',     'name':'Toronto, Canada',          'region':'us',    'country':'CA', 'city':'Toronto',      'state':'ON', 'aliases':['yyz','toronto']},
    {'id':'yvr',     'name':'Vancouver, Canada',        'region':'us',    'country':'CA', 'city':'Vancouver',    'state':'BC', 'aliases':['yvr','vancouver']},
    # EMEA - Western Europe
    {'id':'lhr',     'name':'London, UK',               'region':'emea',  'country':'GB', 'city':'London',       'state':None, 'aliases':['lhr','london','slough']},
    {'id':'fra',     'name':'Frankfurt, Germany',       'region':'emea',  'country':'DE', 'city':'Frankfurt',    'state':None, 'aliases':['fra','frankfurt']},
    {'id':'ams',     'name':'Amsterdam, Netherlands',   'region':'emea',  'country':'NL', 'city':'Amsterdam',    'state':None, 'aliases':['ams','amsterdam']},
    {'id':'par',     'name':'Paris, France',            'region':'emea',  'country':'FR', 'city':'Paris',        'state':None, 'aliases':['par','paris']},
    {'id':'dub',     'name':'Dublin, Ireland',          'region':'emea',  'country':'IE', 'city':'Dublin',       'state':None, 'aliases':['dub','dublin']},
    {'id':'zrh',     'name':'Zurich, Switzerland',      'region':'emea',  'country':'CH', 'city':'Zurich',       'state':None, 'aliases':['zrh','zurich']},
    {'id':'sto',     'name':'Stockholm, Sweden',        'region':'emea',  'country':'SE', 'city':'Stockholm',    'state':None, 'aliases':['sto','stockholm']},
    {'id':'mad',     'name':'Madrid, Spain',            'region':'emea',  'country':'ES', 'city':'Madrid',       'state':None, 'aliases':['mad','madrid']},
    {'id':'mil',     'name':'Milan, Italy',             'region':'emea',  'country':'IT', 'city':'Milan',        'state':None, 'aliases':['mil','milan']},
    {'id':'war',     'name':'Warsaw, Poland',           'region':'emea',  'country':'PL', 'city':'Warsaw',       'state':None, 'aliases':['war','warsaw']},
    {'id':'vie',     'name':'Vienna, Austria',          'region':'emea',  'country':'AT', 'city':'Vienna',       'state':None, 'aliases':['vie','vienna']},
    {'id':'cop',     'name':'Copenhagen, Denmark',      'region':'emea',  'country':'DK', 'city':'Copenhagen',   'state':None, 'aliases':['cop','copenhagen']},
    {'id':'hel',     'name':'Helsinki, Finland',        'region':'emea',  'country':'FI', 'city':'Helsinki',     'state':None, 'aliases':['hel','helsinki']},
    {'id':'osl',     'name':'Oslo, Norway',             'region':'emea',  'country':'NO', 'city':'Oslo',         'state':None, 'aliases':['osl','oslo']},
    {'id':'msc',     'name':'Moscow, Russia',           'region':'emea',  'country':'RU', 'city':'Moscow',       'state':None, 'aliases':['msc','moscow']},
    {'id':'ist',     'name':'Istanbul, Turkey',         'region':'emea',  'country':'TR', 'city':'Istanbul',     'state':None, 'aliases':['ist','istanbul']},
    # EMEA - Middle East & Africa
    {'id':'dxb',     'name':'Dubai, UAE',               'region':'emea',  'country':'AE', 'city':'Dubai',        'state':None, 'aliases':['dxb','dubai']},
    {'id':'ruh',     'name':'Riyadh, Saudi Arabia',     'region':'emea',  'country':'SA', 'city':'Riyadh',       'state':None, 'aliases':['ruh','riyadh']},
    {'id':'jnb',     'name':'Johannesburg, South Africa','region':'emea', 'country':'ZA', 'city':'Johannesburg', 'state':None, 'aliases':['jnb','johannesburg']},
    {'id':'nbo',     'name':'Nairobi, Kenya',           'region':'emea',  'country':'KE', 'city':'Nairobi',      'state':None, 'aliases':['nbo','nairobi']},
    {'id':'lag',     'name':'Lagos, Nigeria',           'region':'emea',  'country':'NG', 'city':'Lagos',        'state':None, 'aliases':['lag','lagos']},
    {'id':'cai',     'name':'Cairo, Egypt',             'region':'emea',  'country':'EG', 'city':'Cairo',        'state':None, 'aliases':['cai','cairo']},
    # APAC
    {'id':'sin',     'name':'Singapore',                'region':'apac',  'country':'SG', 'city':'Singapore',    'state':None, 'aliases':['sin','singapore']},
    {'id':'tyo',     'name':'Tokyo, Japan',             'region':'apac',  'country':'JP', 'city':'Tokyo',        'state':None, 'aliases':['tyo','tokyo']},
    {'id':'syd',     'name':'Sydney, Australia',        'region':'apac',  'country':'AU', 'city':'Sydney',       'state':None, 'aliases':['syd','sydney']},
    {'id':'hkg',     'name':'Hong Kong',                'region':'apac',  'country':'HK', 'city':'Hong Kong',    'state':None, 'aliases':['hkg','hong kong']},
    {'id':'sha',     'name':'Shanghai, China',          'region':'apac',  'country':'CN', 'city':'Shanghai',     'state':None, 'aliases':['sha','shanghai']},
    {'id':'pek',     'name':'Beijing, China',           'region':'apac',  'country':'CN', 'city':'Beijing',      'state':None, 'aliases':['pek','beijing']},
    {'id':'bom',     'name':'Mumbai, India',            'region':'apac',  'country':'IN', 'city':'Mumbai',       'state':None, 'aliases':['bom','mumbai']},
    {'id':'del',     'name':'Delhi, India',             'region':'apac',  'country':'IN', 'city':'Delhi',        'state':None, 'aliases':['del','delhi']},
    {'id':'sel',     'name':'Seoul, South Korea',       'region':'apac',  'country':'KR', 'city':'Seoul',        'state':None, 'aliases':['sel','seoul']},
    {'id':'kul',     'name':'Kuala Lumpur, Malaysia',   'region':'apac',  'country':'MY', 'city':'Kuala Lumpur', 'state':None, 'aliases':['kul','kuala lumpur']},
    {'id':'mel',     'name':'Melbourne, Australia',     'region':'apac',  'country':'AU', 'city':'Melbourne',    'state':None, 'aliases':['mel','melbourne']},
    {'id':'jak',     'name':'Jakarta, Indonesia',       'region':'apac',  'country':'ID', 'city':'Jakarta',      'state':None, 'aliases':['jak','jakarta']},
    {'id':'bkk',     'name':'Bangkok, Thailand',        'region':'apac',  'country':'TH', 'city':'Bangkok',      'state':None, 'aliases':['bkk','bangkok']},
    {'id':'mnl',     'name':'Manila, Philippines',      'region':'apac',  'country':'PH', 'city':'Manila',       'state':None, 'aliases':['mnl','manila']},
    {'id':'nrt',     'name':'Osaka, Japan',             'region':'apac',  'country':'JP', 'city':'Osaka',        'state':None, 'aliases':['nrt','osaka']},
    # LATAM
    {'id':'gru',     'name':'São Paulo, Brazil',        'region':'latam', 'country':'BR', 'city':'São Paulo',    'state':None, 'aliases':['gru','sao paulo']},
    {'id':'mex',     'name':'Mexico City, Mexico',      'region':'latam', 'country':'MX', 'city':'Mexico City',  'state':None, 'aliases':['mex','mexico city']},
    {'id':'bog',     'name':'Bogotá, Colombia',         'region':'latam', 'country':'CO', 'city':'Bogotá',       'state':None, 'aliases':['bog','bogota']},
    {'id':'scl',     'name':'Santiago, Chile',          'region':'latam', 'country':'CL', 'city':'Santiago',     'state':None, 'aliases':['scl','santiago']},
    {'id':'bue',     'name':'Buenos Aires, Argentina',  'region':'latam', 'country':'AR', 'city':'Buenos Aires', 'state':None, 'aliases':['bue','buenos aires']},
    {'id':'lim',     'name':'Lima, Peru',               'region':'latam', 'country':'PE', 'city':'Lima',         'state':None, 'aliases':['lim','lima']},
]

MARKET_BY_ID = {m['id']: m for m in MARKETS}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_config():
    global _config_cache, _config_ts
    now = time.time()
    if now - _config_ts < CONFIG_TTL and _config_cache:
        return _config_cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM gdci_config")
        for row in cur.fetchall():
            cfg[row[0]] = row[1]
        cur.close()
    except Exception as e:
        logger.warning("Config load failed, using defaults: %s", e)
    _config_cache = cfg
    _config_ts = now
    return cfg


def _bool(cfg, key):
    return str(cfg.get(key, 'false')).lower() in ('true', '1', 'yes')


def _score_color(score):
    if score is None:
        return 'gray'
    if score >= 75:
        return 'red'
    if score >= 60:
        return 'purple'
    if score >= 40:
        return 'amber'
    return 'green'


def _score_label(score):
    if score is None:
        return 'No Data'
    if score >= 75:
        return 'Critical'
    if score >= 60:
        return 'Constrained'
    if score >= 40:
        return 'Balanced'
    return "Buyer's Market"


def _safe_query(conn, sql, params=()):
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row
    except Exception as e:
        logger.debug("Query failed: %s — %s", sql[:80], e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


# ─── Sub-index calculators ────────────────────────────────────────────────────

def _calc_dhci(conn, cfg, market):
    """Capacity Index — based on facilities table"""
    fac    = cfg.get('fac_table', 'facilities')
    op_st  = cfg.get('fac_status_operational', 'active,Operational')
    pi_st  = cfg.get('fac_status_pipeline', 'Under Construction,Construction,Planned,Planning,Announced')
    op_list = tuple(s.strip() for s in op_st.split(','))
    pi_list = tuple(s.strip() for s in pi_st.split(','))

    city    = market['city']
    country = market['country']
    state   = market.get('state')
    aliases = market.get('aliases', [])

    # Build city ILIKE conditions
    city_conds = " OR ".join(["LOWER(city) LIKE %s"] * len(aliases))
    city_params = [f'%{a.lower()}%' for a in aliases]

    # Country/state filter
    if state:
        loc_filter = f"AND (state = %s OR country = %s)"
        loc_params = [state, country]
    else:
        loc_filter = f"AND country = %s"
        loc_params = [country]

    def count_q(statuses):
        placeholders = ','.join(['%s'] * len(statuses))
        sql = f"""
            SELECT COUNT(*), COALESCE(SUM(power_mw), 0)
            FROM {fac}
            WHERE status IN ({placeholders})
            AND ({city_conds})
            {loc_filter}
        """
        return _safe_query(conn, sql, list(statuses) + city_params + loc_params)

    op_row = count_q(op_list)
    pi_row = count_q(pi_list)

    op_count = int(op_row[0]) if op_row else 0
    op_mw    = float(op_row[1]) if op_row else 0.0
    pi_mw    = float(pi_row[1]) if pi_row else 0.0
    tot_row  = _safe_query(conn,
        f"SELECT COUNT(*), COALESCE(SUM(power_mw),0) FROM {fac} WHERE ({city_conds}) {loc_filter}",
        city_params + loc_params)
    tot_count = int(tot_row[0]) if tot_row else 0
    tot_mw    = float(tot_row[1]) if tot_row else 0.0

    if op_mw <= 0:
        return None, {}

    # Vacancy proxy: assume 2x pipeline = near-zero vacancy
    vac_pct = max(0, min(100, 15 - (pi_mw / max(op_mw, 1)) * 5))
    # Score: low vacancy = high score
    score = min(100, max(0, (1 - vac_pct / 15) * 100))

    return round(score, 1), {
        'operational_count': op_count,
        'operational_mw':    round(op_mw, 1),
        'pipeline_mw':       round(pi_mw, 1),
        'total_count':       tot_count,
        'total_mw':          round(tot_mw, 1),
        'vacancy_pct':       round(vac_pct, 2),
    }


def _calc_dhri(conn, cfg, market):
    """Rate Index — market_intelligence table"""
    if not _bool(cfg, 'mi_enabled'):
        return None, {}
    mi = cfg.get('mi_table', 'market_intelligence')
    mid = market['id']
    mname = market['name'].split(',')[0].lower()

    row = _safe_query(conn,
        f"SELECT avg_rate_per_kw, recorded_at FROM {mi} WHERE LOWER(market) LIKE %s ORDER BY recorded_at DESC LIMIT 1",
        (f'%{mname}%',))
    if not row or not row[0]:
        return None, {}

    current = float(row[0])
    # Baseline Jan 2025: ~$120/kW for top markets
    baseline = 120.0
    ratio = current / baseline
    score = min(100, max(0, (ratio - 0.5) * 133))

    return round(score, 1), {
        'rate_per_kw':   round(current, 2),
        'index_value':   round(ratio, 3),
        'baseline_rate': baseline,
    }


def _calc_dhpi(conn, cfg, market):
    """Pipeline Index — ratio of pipeline to operational"""
    fac    = cfg.get('fac_table', 'facilities')
    op_st  = cfg.get('fac_status_operational', 'active,Operational')
    pi_st  = cfg.get('fac_status_pipeline', 'Under Construction,Construction,Planned,Planning,Announced')
    op_list = tuple(s.strip() for s in op_st.split(','))
    pi_list = tuple(s.strip() for s in pi_st.split(','))

    aliases = market.get('aliases', [])
    city_conds = " OR ".join(["LOWER(city) LIKE %s"] * len(aliases))
    city_params = [f'%{a.lower()}%' for a in aliases]
    state   = market.get('state')
    country = market['country']
    if state:
        loc_filter = "AND (state = %s OR country = %s)"
        loc_params = [state, country]
    else:
        loc_filter = "AND country = %s"
        loc_params = [country]

    def mw_q(statuses):
        placeholders = ','.join(['%s'] * len(statuses))
        row = _safe_query(conn,
            f"SELECT COALESCE(SUM(power_mw),0) FROM {fac} WHERE status IN ({placeholders}) AND ({city_conds}) {loc_filter}",
            list(statuses) + city_params + loc_params)
        return float(row[0]) if row else 0.0

    op_mw = mw_q(op_list)
    pi_mw = mw_q(pi_list)

    if op_mw <= 0:
        return None, {}

    ratio = pi_mw / op_mw
    score = min(100, ratio * 50)  # 200% pipeline = 100 score

    return round(score, 1), {
        'pipeline_mw':        round(pi_mw, 1),
        'operational_mw':     round(op_mw, 1),
        'pipeline_ratio_pct': round(ratio * 100, 2),
    }


def _calc_dhdi(conn, cfg, market):
    """Demand Index — deal absorption in trailing 90 days"""
    txn = cfg.get('txn_table', 'deals')
    mid = market['id']
    mname = market['name'].split(',')[0].lower()
    aliases = market.get('aliases', [])

    conds = " OR ".join(["LOWER(market) LIKE %s"] * len(aliases))
    params = [f'%{a.lower()}%' for a in aliases]

    row = _safe_query(conn,
        f"SELECT COUNT(*), COALESCE(SUM(mw),0) FROM {txn} WHERE ({conds}) AND date >= NOW() - INTERVAL '90 days'",
        params)
    if not row:
        return None, {}

    deal_count = int(row[0])
    abs_mw     = float(row[1])

    if deal_count == 0 and abs_mw == 0:
        return None, {}

    score = min(100, (abs_mw / 2000) * 100 + deal_count * 5)

    return round(score, 1), {
        'deal_count_90d':   deal_count,
        'absorbed_mw_90d':  round(abs_mw, 1),
    }


def _calc_dhpw(conn, cfg, market):
    """Power Index — substations first, then power plants"""
    city    = market['city']
    country = market['country']
    state   = market.get('state')
    aliases = market.get('aliases', [])

    city_conds = " OR ".join(["LOWER(city) LIKE %s"] * len(aliases))
    city_params = [f'%{a.lower()}%' for a in aliases]

    # Try substations first
    if _bool(cfg, 'sub_enabled'):
        sub = cfg.get('sub_table', 'substations')
        row = _safe_query(conn,
            f"SELECT COUNT(*), COALESCE(SUM(capacity_mva),0), COALESCE(SUM(available_mva),0) FROM {sub} WHERE ({city_conds})",
            city_params)
        if row and row[0] and int(row[0]) > 0:
            total = float(row[1])
            avail = float(row[2])
            if total > 0:
                headroom_pct = (avail / total) * 100
                score = min(100, max(0, (1 - headroom_pct / 100) * 100))
                return round(score, 1), {
                    'source':      'substations',
                    'total_mva':   round(total, 1),
                    'avail_mva':   round(avail, 1),
                    'headroom_pct':round(headroom_pct, 1),
                }

    # Fall back to power plants
    if _bool(cfg, 'power_enabled'):
        pw  = cfg.get('power_table', 'discovered_power_plants')
        pcol = cfg.get('power_city_col', 'market')
        scol = cfg.get('power_state_col', 'state')

        conds = " OR ".join([f"LOWER({pcol}) LIKE %s"] * len(aliases))
        params = [f'%{a.lower()}%' for a in aliases]
        if state:
            conds += f" OR LOWER({scol}) LIKE %s"
            params.append(f'%{state.lower()}%')

        row = _safe_query(conn,
            f"SELECT COUNT(*), COALESCE(SUM(capacity_mw),0) FROM {pw} WHERE ({conds})",
            params)
        if row and row[0] and int(row[0]) > 0:
            cnt = int(row[0])
            mw  = float(row[1])
            score = min(100, (mw / 5000) * 100 + cnt * 0.5)
            return round(score, 1), {
                'source':      'discovered_power_plants',
                'plant_count': cnt,
                'total_mw':    round(mw, 1),
            }

    return None, {'source': 'none', 'note': 'No power data found'}


# ─── Core: score one market ───────────────────────────────────────────────────

def _score_market(market_id):
    """Score a single market — runs in thread pool"""
    now = time.time()
    if market_id in _market_cache and now - _market_ts.get(market_id, 0) < MARKET_TTL:
        return _market_cache[market_id]

    market = MARKET_BY_ID.get(market_id)
    if not market:
        return None

    cfg = _get_config()
    weights = {
        'dhci': float(cfg.get('w_dhci', 30)) / 100,
        'dhri': float(cfg.get('w_dhri', 25)) / 100,
        'dhpi': float(cfg.get('w_dhpi', 20)) / 100,
        'dhdi': float(cfg.get('w_dhdi', 15)) / 100,
        'dhpw': float(cfg.get('w_dhpw', 10)) / 100,
    }

    try:
        conn = get_db()

        dhci_val, dhci_d = _calc_dhci(conn, cfg, market)
        dhri_val, dhri_d = _calc_dhri(conn, cfg, market)
        dhpi_val, dhpi_d = _calc_dhpi(conn, cfg, market)
        dhdi_val, dhdi_d = _calc_dhdi(conn, cfg, market)
        dhpw_val, dhpw_d = _calc_dhpw(conn, cfg, market)

        # Connectivity
        fiber_count = tx_count = 0
        if _bool(cfg, 'fiber_enabled'):
            aliases = market.get('aliases', [])
            conds = " OR ".join(["LOWER(city) LIKE %s"] * len(aliases))
            params = [f'%{a.lower()}%' for a in aliases]
            row = _safe_query(conn, f"SELECT COUNT(*) FROM {cfg.get('fiber_table','fiber_routes')} WHERE ({conds})", params)
            fiber_count = int(row[0]) if row else 0
        if _bool(cfg, 'tx_enabled'):
            aliases = market.get('aliases', [])
            conds = " OR ".join(["LOWER(city) LIKE %s"] * len(aliases))
            params = [f'%{a.lower()}%' for a in aliases]
            row = _safe_query(conn, f"SELECT COUNT(*) FROM {cfg.get('tx_table','discovered_transmission_lines')} WHERE ({conds})", params)
            tx_count = int(row[0]) if row else 0

        # Composite
        active = [(v, w) for v, w in [
            (dhci_val, weights['dhci']),
            (dhri_val, weights['dhri']),
            (dhpi_val, weights['dhpi']),
            (dhdi_val, weights['dhdi']),
            (dhpw_val, weights['dhpw']),
        ] if v is not None]

        if active:
            total_w = sum(w for _, w in active)
            composite = sum(v * w for v, w in active) / total_w
            composite = round(composite, 1)
        else:
            composite = None

        result = {
            'market_id':       market_id,
            'market_name':     market['name'],
            'region':          market['region'],
            'country':         market['country'],
            'composite_score': composite,
            'composite_label': _score_label(composite),
            'composite_color': _score_color(composite),
            'computed_at':     datetime.now(timezone.utc).isoformat(),
            'dhci': {'value': dhci_val, **dhci_d},
            'dhri': {'value': dhri_val, **dhri_d},
            'dhpi': {'value': dhpi_val, **dhpi_d},
            'dhdi': {'value': dhdi_val, **dhdi_d},
            'dhpw': {'value': dhpw_val, **dhpw_d},
            'connectivity': {
                'fiber_routes':      fiber_count,
                'transmission_lines': tx_count,
            },
        }

        _market_cache[market_id] = result
        _market_ts[market_id] = now
        return result

    except Exception as e:
        logger.error("Market scoring failed for %s: %s", market_id, e)
        return {
            'market_id':       market_id,
            'market_name':     market['name'],
            'region':          market['region'],
            'country':         market['country'],
            'composite_score': None,
            'composite_label': 'Error',
            'composite_color': 'gray',
            'error':           str(e)[:200],
        }


def _score_all_markets(market_ids=None):
    """Score all markets in parallel using thread pool"""
    cfg = _get_config()
    n_threads = int(cfg.get('market_threads', 12))
    ids = market_ids or [m['id'] for m in MARKETS]

    results = []
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {pool.submit(_score_market, mid): mid for mid in ids}
        for future in as_completed(futures):
            try:
                r = future.result(timeout=30)
                if r:
                    results.append(r)
            except Exception as e:
                logger.error("Market future failed: %s", e)

    # Sort by composite score descending
    results.sort(key=lambda x: x.get('composite_score') or 0, reverse=True)
    return results


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def init_config_table():
    """Create gdci_config table and seed defaults — runs on startup"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gdci_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        for k, v in DEFAULT_CONFIG.items():
            cur.execute(
                "INSERT INTO gdci_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, str(v))
            )
        conn.commit()
        cur.close()

        # Auto-detect which tables exist and enable them
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        existing = {r[0] for r in cur.fetchall()}
        cur.close()

        toggles = {
            'substations':                   ('sub_enabled',   'sub_table'),
            'discovered_power_plants':        ('power_enabled', 'power_table'),
            'market_intelligence':            ('mi_enabled',    'mi_table'),
            'discovered_transmission_lines':  ('tx_enabled',    'tx_table'),
            'fiber_routes':                   ('fiber_enabled', 'fiber_table'),
        }
        cur = conn.cursor()
        for tbl, (flag, tbl_key) in toggles.items():
            if tbl in existing:
                cur.execute(
                    "INSERT INTO gdci_config (key, value) VALUES (%s, 'true') ON CONFLICT (key) DO UPDATE SET value='true'",
                    (flag,)
                )
        conn.commit()
        cur.close()
        logger.info("GDCI: ✅ Config table initialized, %d tables auto-detected", len(existing))
    except Exception as e:
        logger.error("GDCI: Config init failed: %s", e)


# ─── Routes ──────────────────────────────────────────────────────────────────

@index_bp.route('/health')
def health():
    try:
        conn = get_db()
        cfg  = _get_config()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM gdci_config")
        cnt = cur.fetchone()[0]
        cur.close()
        return jsonify({'status': 'ok', 'db': 'connected', 'config_keys': cnt, 'markets_defined': len(MARKETS), 'ts': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


@index_bp.route('/composite')
def composite():
    try:
        markets = _score_all_markets()
        scored  = [m for m in markets if m.get('composite_score') is not None]

        if not scored:
            return jsonify({'error': 'No market data available'}), 503

        avg = sum(m['composite_score'] for m in scored) / len(scored)

        # Global vacancy
        tot_mw = sum(m.get('dhci', {}).get('total_mw', 0) or 0 for m in scored)
        op_mw  = sum(m.get('dhci', {}).get('operational_mw', 0) or 0 for m in scored)
        pi_mw  = sum(m.get('dhpi', {}).get('pipeline_mw', 0) or 0 for m in scored)

        vac_pct = None
        if tot_mw > 0:
            # Weighted average vacancy
            vac_pct = round(
                sum((m.get('dhci', {}).get('vacancy_pct') or 0) * (m.get('dhci', {}).get('operational_mw') or 0)
                    for m in scored) / max(op_mw, 1), 2)

        composite_score = round(avg, 1)

        cfg = _get_config()
        month = datetime.now(timezone.utc).strftime('%B %Y')
        citation = (
            f"According to the DC Hub Global Data Center Index (GDCI), the global composite "
            f"score reached {composite_score} in {month}, indicating a {_score_label(composite_score).lower()} "
            f"environment across {len(scored)} tracked markets. "
            f"Source: DC Hub GDCI, dchub.cloud/index"
        )

        return jsonify({
            'issue':               f"{month} Issue",
            'composite_score':     composite_score,
            'composite_label':     _score_label(composite_score),
            'composite_color':     _score_color(composite_score),
            'global_vacancy_pct':  vac_pct,
            'total_tracked_mw':    round(tot_mw, 1),
            'total_pipeline_mw':   round(pi_mw, 1),
            'markets_covered':     len(scored),
            'markets_with_data':   len(scored),
            'citation':            citation,
            'generated_at':        datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error("Composite error: %s", e)
        return jsonify({'error': str(e)}), 500


@index_bp.route('/markets')
def markets_list():
    try:
        region = request.args.get('region')
        ids = [m['id'] for m in MARKETS if not region or m['region'] == region]
        results = _score_all_markets(ids)
        return jsonify({
            'markets':       results,
            'count':         len(results),
            'scored':        sum(1 for m in results if m.get('composite_score') is not None),
            'generated_at':  datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error("Markets list error: %s", e)
        return jsonify({'error': str(e)}), 500


@index_bp.route('/market/<market_id>')
def market_detail(market_id):
    try:
        result = _score_market(market_id)
        if not result:
            return jsonify({'error': f'Unknown market: {market_id}'}), 404
        return jsonify(result)
    except Exception as e:
        logger.error("Market detail error %s: %s", market_id, e)
        return jsonify({'error': str(e)}), 500


@index_bp.route('/regions')
def regions():
    try:
        results = _score_all_markets()
        region_map = {}
        for m in results:
            r = m.get('region', 'other')
            if r not in region_map:
                region_map[r] = {'region': r, 'markets': [], 'scores': [], 'total_mw': 0, 'pipeline_mw': 0}
            region_map[r]['markets'].append(m['market_name'])
            if m.get('composite_score') is not None:
                region_map[r]['scores'].append(m['composite_score'])
            region_map[r]['total_mw']    += m.get('dhci', {}).get('total_mw', 0) or 0
            region_map[r]['pipeline_mw'] += m.get('dhpi', {}).get('pipeline_mw', 0) or 0

        out = []
        for r, d in region_map.items():
            avg = round(sum(d['scores']) / len(d['scores']), 1) if d['scores'] else None
            out.append({
                'region':          r,
                'market_count':    len(d['markets']),
                'composite_score': avg,
                'composite_label': _score_label(avg),
                'composite_color': _score_color(avg),
                'total_mw':        round(d['total_mw'], 1),
                'pipeline_mw':     round(d['pipeline_mw'], 1),
            })
        out.sort(key=lambda x: x.get('composite_score') or 0, reverse=True)
        return jsonify({'regions': out, 'generated_at': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@index_bp.route('/citation/<market_id>')
def citation(market_id):
    try:
        m = _score_market(market_id)
        if not m:
            return jsonify({'error': 'Unknown market'}), 404
        month = datetime.now(timezone.utc).strftime('%B %Y')
        text = (
            f"According to the DC Hub Global Data Center Index (GDCI), {m['market_name']} "
            f"scored {m['composite_score']} ({m['composite_label']}) in {month}. "
            f"Source: DC Hub GDCI, dchub.cloud/index"
        )
        return jsonify({'market_id': market_id, 'market_name': m['market_name'], 'citation': text, 'score': m['composite_score']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Admin routes ─────────────────────────────────────────────────────────────

def _require_admin(cfg):
    key = cfg.get('admin_key', '')
    provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if key and provided != key:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


@index_bp.route('/admin/config', methods=['GET'])
def admin_config_get():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err:
        return err
    return jsonify({'config': cfg, 'count': len(cfg)})


@index_bp.route('/admin/config', methods=['POST'])
def admin_config_set():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        updated = []
        for k, v in data.items():
            cur.execute(
                "INSERT INTO gdci_config (key, value, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (k, str(v))
            )
            updated.append(k)
        conn.commit()
        cur.close()
        # Clear caches
        global _config_cache, _config_ts, _market_cache, _market_ts
        _config_cache = {}
        _config_ts    = 0
        _market_cache = {}
        _market_ts    = {}
        return jsonify({'updated': updated, 'count': len(updated), 'note': 'Cache cleared — new config active immediately.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@index_bp.route('/admin/refresh', methods=['POST'])
def admin_refresh():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err:
        return err
    global _config_cache, _config_ts, _market_cache, _market_ts
    _config_cache = {}
    _config_ts    = 0
    _market_cache = {}
    _market_ts    = {}
    return jsonify({'cleared': True, 'at': datetime.now(timezone.utc).isoformat()})


@index_bp.route('/admin/sources')
def admin_sources():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err:
        return err
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        cur.close()
        sources = [
            {'name': 'Facilities (DHCI+DHPI)', 'table': cfg.get('fac_table','facilities'), 'enabled': True, 'exists': cfg.get('fac_table','facilities') in existing, 'status': 'always on'},
            {'name': 'Transactions (DHDI)',     'table': cfg.get('txn_table','deals'),      'enabled': True, 'exists': cfg.get('txn_table','deals') in existing,      'status': 'always on'},
            {'name': 'Market Intelligence (DHRI)', 'table': cfg.get('mi_table','market_intelligence'),    'enabled': _bool(cfg,'mi_enabled'),    'exists': cfg.get('mi_table','market_intelligence') in existing,   'activate': 'POST /api/index/admin/config {"mi_enabled":"true"}'},
            {'name': 'Power Plants (DHPW)',     'table': cfg.get('power_table','discovered_power_plants'),'enabled': _bool(cfg,'power_enabled'), 'exists': cfg.get('power_table','discovered_power_plants') in existing, 'activate': 'POST /api/index/admin/config {"power_enabled":"true"}'},
            {'name': 'Substations (DHPW priority)', 'table': cfg.get('sub_table','substations'),         'enabled': _bool(cfg,'sub_enabled'),   'exists': cfg.get('sub_table','substations') in existing,          'activate': 'POST /api/index/admin/config {"sub_enabled":"true","sub_table":"your_table"}'},
            {'name': 'Transmission Lines',      'table': cfg.get('tx_table','discovered_transmission_lines'), 'enabled': _bool(cfg,'tx_enabled'), 'exists': cfg.get('tx_table','discovered_transmission_lines') in existing, 'activate': 'POST /api/index/admin/config {"tx_enabled":"true"}'},
            {'name': 'Fiber Routes',            'table': cfg.get('fiber_table','fiber_routes'),           'enabled': _bool(cfg,'fiber_enabled'), 'exists': cfg.get('fiber_table','fiber_routes') in existing,       'activate': 'POST /api/index/admin/config {"fiber_enabled":"true"}'},
        ]
        return jsonify({'sources': sources, 'markets_defined': len(MARKETS)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@index_bp.route('/admin/markets', methods=['POST'])
def admin_add_market():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err:
        return err
    return jsonify({'note': 'Markets are defined in MARKETS list in index_api.py. Use /admin/config to adjust weights and toggles.', 'market_count': len(MARKETS)})
