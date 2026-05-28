"""
DC Hub Global Data Center Index API (index_api.py)
Flask Blueprint — mounts at /api/index
v6.1 — Scoring fixes: shared-state dedup, power plant locality, expanded pipeline statuses, broader deal matching
"""

import os
import time
import logging
from datetime import datetime, timezone
from collections import defaultdict

from flask import Blueprint, jsonify, request
from db_utils import get_db

logger = logging.getLogger(__name__)

index_bp = Blueprint('index', __name__, url_prefix='/api/index')

_config_cache = {}
_config_ts    = 0
_bulk_cache   = None
_bulk_ts      = 0
CONFIG_TTL    = 600
BULK_TTL      = 3600

DEFAULT_CONFIG = {
    'fac_table':              'facilities',
    'txn_table':              'deals',
    'mi_table':               'market_intelligence',
    'power_table':            'discovered_power_plants',
    'sub_table':              'substations',
    'fac_status_operational': 'active,Operational',
    # FIX 3: Expanded pipeline status list to catch more variations
    'fac_status_pipeline':    'Under Construction,Construction,Planned,Planning,Announced,announced,planned,under construction,Pre-Leased,pre-leased,Proposed,proposed,In Development,in development,Approved,approved,Permitted,permitted,Pipeline,pipeline',
    'mi_enabled':    'true',
    'power_enabled': 'true',
    'sub_enabled':   'true',
    'power_city_col': 'market',
    'power_state_col': 'state',
    'w_dhci': '30', 'w_dhri': '25', 'w_dhpi': '20', 'w_dhdi': '15', 'w_dhpw': '10',
    'admin_key': '',
}

MARKETS = [
    # US
    {'id':'nova','name':'Northern Virginia, US',     'region':'us',   'country_codes':['US'],'city_kw':['ashburn','loudoun','northern virginia','sterling','leesburg','manassas','bristow','prince william','chantilly','herndon','reston','dulles','haymarket','gainesville','warrenton','south hill','stafford'],'state_kw':['va','virginia']},
    {'id':'dal', 'name':'Dallas/Fort Worth, US',     'region':'us',   'country_codes':['US'],'city_kw':['dallas','fort worth','dfw','irving','plano','arlington','addison','garland'],'state_kw':['tx','texas']},
    {'id':'phx', 'name':'Phoenix, US',               'region':'us',   'country_codes':['US'],'city_kw':['phoenix','chandler','mesa','tempe','scottsdale','gilbert'],'state_kw':['az','arizona']},
    {'id':'chi', 'name':'Chicago, US',               'region':'us',   'country_codes':['US'],'city_kw':['chicago','elk grove','aurora','naperville','lisle'],'state_kw':['il','illinois']},
    {'id':'nyc', 'name':'New York/New Jersey, US',   'region':'us',   'country_codes':['US'],'city_kw':['new york','new jersey','secaucus','newark','jersey city','manhattan','brooklyn'],'state_kw':['nj','ny']},
    {'id':'sea', 'name':'Seattle, US',               'region':'us',   'country_codes':['US'],'city_kw':['seattle','quincy','wenatchee','bellevue','redmond','renton'],'state_kw':['wa','washington']},
    {'id':'sfo', 'name':'San Francisco Bay, US',     'region':'us',   'country_codes':['US'],'city_kw':['san jose','santa clara','fremont','milpitas','sunnyvale','palo alto','san francisco','oakland','hayward','silicon valley'],'state_kw':[]},
    {'id':'lax', 'name':'Los Angeles, US',           'region':'us',   'country_codes':['US'],'city_kw':['los angeles','el segundo','torrance','hawthorne','long beach','compton'],'state_kw':[]},
    {'id':'atl', 'name':'Atlanta, US',               'region':'us',   'country_codes':['US'],'city_kw':['atlanta','norcross','lithia springs','douglasville','alpharetta'],'state_kw':['ga','georgia']},
    {'id':'bos', 'name':'Boston, US',                'region':'us',   'country_codes':['US'],'city_kw':['boston','somerville','cambridge','waltham','quincy'],'state_kw':['ma','massachusetts']},
    {'id':'den', 'name':'Denver, US',                'region':'us',   'country_codes':['US'],'city_kw':['denver','englewood','littleton','aurora','centennial'],'state_kw':['co','colorado']},
    {'id':'mia', 'name':'Miami, US',                 'region':'us',   'country_codes':['US'],'city_kw':['miami','doral','boca raton','fort lauderdale','miramar'],'state_kw':['fl','florida']},
    {'id':'iah', 'name':'Houston, US',               'region':'us',   'country_codes':['US'],'city_kw':['houston','katy','sugar land','webster','baytown','pasadena','the woodlands','cypress'],'state_kw':['tx','texas']},
    {'id':'msp', 'name':'Minneapolis, US',           'region':'us',   'country_codes':['US'],'city_kw':['minneapolis','saint paul','eden prairie','bloomington','st paul'],'state_kw':['mn','minnesota']},
    {'id':'slc', 'name':'Salt Lake City, US',        'region':'us',   'country_codes':['US'],'city_kw':['salt lake city','west jordan','draper','sandy','south jordan'],'state_kw':['ut','utah']},
    # Canada
    {'id':'yyz', 'name':'Toronto, Canada',           'region':'us',   'country_codes':['CA'],'city_kw':['toronto','mississauga','markham','vaughan','brampton'],'state_kw':['on','ontario']},
    {'id':'yvr', 'name':'Vancouver, Canada',         'region':'us',   'country_codes':['CA'],'city_kw':['vancouver','burnaby','richmond','surrey'],'state_kw':['bc','british columbia']},
    # EMEA Europe
    {'id':'lhr', 'name':'London, UK',                'region':'emea', 'country_codes':['GB','UK'],'city_kw':['london','slough','reading','uxbridge','hayes'],'state_kw':[]},
    {'id':'fra', 'name':'Frankfurt, Germany',         'region':'emea', 'country_codes':['DE'],'city_kw':['frankfurt','eschborn'],'state_kw':[]},
    {'id':'ams', 'name':'Amsterdam, Netherlands',    'region':'emea', 'country_codes':['NL'],'city_kw':['amsterdam','amsterdam-southeast'],'state_kw':[]},
    {'id':'par', 'name':'Paris, France',             'region':'emea', 'country_codes':['FR'],'city_kw':['paris','saint-denis','vitry','aubervilliers'],'state_kw':[]},
    {'id':'dub', 'name':'Dublin, Ireland',           'region':'emea', 'country_codes':['IE'],'city_kw':['dublin'],'state_kw':[]},
    {'id':'zrh', 'name':'Zurich, Switzerland',       'region':'emea', 'country_codes':['CH'],'city_kw':['zurich','geneva','bern'],'state_kw':[]},
    {'id':'sto', 'name':'Stockholm, Sweden',         'region':'emea', 'country_codes':['SE'],'city_kw':['stockholm'],'state_kw':[]},
    {'id':'mad', 'name':'Madrid, Spain',             'region':'emea', 'country_codes':['ES'],'city_kw':['madrid','barcelona'],'state_kw':[]},
    {'id':'mil', 'name':'Milan, Italy',              'region':'emea', 'country_codes':['IT'],'city_kw':['milan','milano','rome'],'state_kw':[]},
    {'id':'war', 'name':'Warsaw, Poland',            'region':'emea', 'country_codes':['PL'],'city_kw':['warsaw','wroclaw','krakow'],'state_kw':[]},
    {'id':'vie', 'name':'Vienna, Austria',           'region':'emea', 'country_codes':['AT'],'city_kw':['vienna','wien'],'state_kw':[]},
    {'id':'cop', 'name':'Copenhagen, Denmark',       'region':'emea', 'country_codes':['DK'],'city_kw':['copenhagen'],'state_kw':[]},
    {'id':'hel', 'name':'Helsinki, Finland',         'region':'emea', 'country_codes':['FI'],'city_kw':['helsinki'],'state_kw':[]},
    {'id':'osl', 'name':'Oslo, Norway',              'region':'emea', 'country_codes':['NO'],'city_kw':['oslo'],'state_kw':[]},
    {'id':'msc', 'name':'Moscow, Russia',            'region':'emea', 'country_codes':['RU'],'city_kw':['moscow'],'state_kw':[]},
    {'id':'ist', 'name':'Istanbul, Turkey',          'region':'emea', 'country_codes':['TR'],'city_kw':['istanbul','ankara'],'state_kw':[]},
    # Middle East & Africa
    {'id':'dxb', 'name':'Dubai, UAE',                'region':'emea', 'country_codes':['AE'],'city_kw':['dubai','abu dhabi'],'state_kw':[]},
    {'id':'ruh', 'name':'Riyadh, Saudi Arabia',      'region':'emea', 'country_codes':['SA'],'city_kw':['riyadh','jeddah'],'state_kw':[]},
    {'id':'jnb', 'name':'Johannesburg, South Africa','region':'emea', 'country_codes':['ZA'],'city_kw':['johannesburg','cape town','pretoria'],'state_kw':[]},
    {'id':'nbo', 'name':'Nairobi, Kenya',            'region':'emea', 'country_codes':['KE'],'city_kw':['nairobi'],'state_kw':[]},
    {'id':'lag', 'name':'Lagos, Nigeria',            'region':'emea', 'country_codes':['NG'],'city_kw':['lagos','abuja'],'state_kw':[]},
    {'id':'cai', 'name':'Cairo, Egypt',              'region':'emea', 'country_codes':['EG'],'city_kw':['cairo'],'state_kw':[]},
    # APAC
    {'id':'sin', 'name':'Singapore',                 'region':'apac', 'country_codes':['SG'],'city_kw':['singapore'],'state_kw':[]},
    {'id':'tyo', 'name':'Tokyo, Japan',              'region':'apac', 'country_codes':['JP'],'city_kw':['tokyo','yokohama','kawasaki'],'state_kw':[]},
    {'id':'syd', 'name':'Sydney, Australia',         'region':'apac', 'country_codes':['AU'],'city_kw':['sydney'],'state_kw':['nsw','new south wales']},
    {'id':'hkg', 'name':'Hong Kong',                 'region':'apac', 'country_codes':['HK'],'city_kw':['hong kong'],'state_kw':[]},
    {'id':'sha', 'name':'Shanghai, China',           'region':'apac', 'country_codes':['CN'],'city_kw':['shanghai'],'state_kw':[]},
    {'id':'pek', 'name':'Beijing, China',            'region':'apac', 'country_codes':['CN'],'city_kw':['beijing'],'state_kw':[]},
    {'id':'bom', 'name':'Mumbai, India',             'region':'apac', 'country_codes':['IN'],'city_kw':['mumbai','pune','navi mumbai'],'state_kw':[]},
    {'id':'del', 'name':'Delhi, India',              'region':'apac', 'country_codes':['IN'],'city_kw':['delhi','noida','gurgaon','gurugram','faridabad'],'state_kw':[]},
    {'id':'sel', 'name':'Seoul, South Korea',        'region':'apac', 'country_codes':['KR'],'city_kw':['seoul','busan','incheon'],'state_kw':[]},
    {'id':'kul', 'name':'Kuala Lumpur, Malaysia',    'region':'apac', 'country_codes':['MY'],'city_kw':['kuala lumpur','cyberjaya','petaling jaya'],'state_kw':[]},
    {'id':'mel', 'name':'Melbourne, Australia',      'region':'apac', 'country_codes':['AU'],'city_kw':['melbourne'],'state_kw':['vic','victoria']},
    {'id':'jak', 'name':'Jakarta, Indonesia',        'region':'apac', 'country_codes':['ID'],'city_kw':['jakarta'],'state_kw':[]},
    {'id':'bkk', 'name':'Bangkok, Thailand',         'region':'apac', 'country_codes':['TH'],'city_kw':['bangkok'],'state_kw':[]},
    {'id':'mnl', 'name':'Manila, Philippines',       'region':'apac', 'country_codes':['PH'],'city_kw':['manila','pasig','makati'],'state_kw':[]},
    {'id':'osk', 'name':'Osaka, Japan',              'region':'apac', 'country_codes':['JP'],'city_kw':['osaka','kobe','kyoto'],'state_kw':[]},
    # LATAM
    {'id':'gru', 'name':'São Paulo, Brazil',         'region':'latam','country_codes':['BR'],'city_kw':['sao paulo','são paulo','campinas'],'state_kw':[]},
    {'id':'mex', 'name':'Mexico City, Mexico',       'region':'latam','country_codes':['MX'],'city_kw':['mexico city','queretaro','monterrey','ciudad de mexico'],'state_kw':[]},
    {'id':'bog', 'name':'Bogotá, Colombia',          'region':'latam','country_codes':['CO'],'city_kw':['bogota','bogotá','medellin'],'state_kw':[]},
    {'id':'scl', 'name':'Santiago, Chile',           'region':'latam','country_codes':['CL'],'city_kw':['santiago'],'state_kw':[]},
    {'id':'bue', 'name':'Buenos Aires, Argentina',   'region':'latam','country_codes':['AR'],'city_kw':['buenos aires'],'state_kw':[]},
    {'id':'lim', 'name':'Lima, Peru',                'region':'latam','country_codes':['PE'],'city_kw':['lima'],'state_kw':[]},
]
MARKET_BY_ID = {m['id']: m for m in MARKETS}

# Direct lookup: market ID -> discovered_power_plants.market column values
POWER_MARKET_MAP = {
    'nova': ['northern_virginia'],
    'dal':  ['dallas'],
    'phx':  ['phoenix'],
    'chi':  ['chicago'],
    'nyc':  ['new_york_nj'],
    'sea':  ['seattle_quincy'],
    'sfo':  ['silicon_valley'],
    'lax':  ['los_angeles'],
    'atl':  ['atlanta'],
    'bos':  ['boston'],
    'den':  ['denver'],
    'mia':  ['miami'],
    'iah':  ['houston'],
    'msp':  ['minneapolis'],
    'slc':  ['salt_lake'],
    'yyz':  ['toronto'],
    'yvr':  ['vancouver'],
    'lhr':  ['london'],
    'fra':  ['frankfurt'],
    'ams':  ['amsterdam'],
    'par':  ['paris'],
    'dub':  ['dublin'],
    'zrh':  ['zurich'],
    'sto':  ['stockholm'],
    'mad':  ['madrid'],
    'mil':  ['milan'],
    'sin':  ['singapore'],
    'tyo':  ['tokyo'],
    'syd':  ['sydney'],
    'hkg':  ['hong_kong'],
    'sha':  ['shanghai'],
    'pek':  ['beijing'],
    'bom':  ['mumbai'],
    'del':  ['delhi'],
    'sel':  ['seoul'],
    'kul':  ['kuala_lumpur'],
    'mel':  ['melbourne'],
    'dxb':  ['dubai'],
    'gru':  ['sao_paulo'],
    'mex':  ['mexico_city'],
}

# FIX 1: Markets that share a state — only city-level matching allowed
SHARED_STATES = {
    'tx': ['dal', 'iah'],
    'texas': ['dal', 'iah'],
    'ca': ['sfo', 'lax'],
    'california': ['sfo', 'lax'],
    'nj': ['nyc'],
    'ny': ['nyc'],
}


def _get_config():
    global _config_cache, _config_ts
    now = time.time()
    if now - _config_ts < CONFIG_TTL and _config_cache:
        return _config_cache
    cfg = dict(DEFAULT_CONFIG)
    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT key, value FROM gdci_config")
        for r in cur.fetchall():
            cfg[r[0]] = r[1]
        cur.close()
    except Exception as e:
        logger.warning("Config load failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _config_cache = cfg
    _config_ts    = now
    return cfg

def _bool(cfg, key):
    return str(cfg.get(key, 'false')).lower() in ('true', '1', 'yes')

def _score_color(s):
    if s is None: return 'gray'
    if s >= 75:   return 'red'
    if s >= 60:   return 'purple'
    if s >= 40:   return 'amber'
    return 'green'

def _score_label(s):
    if s is None: return 'No Data'
    if s >= 75:   return 'Critical'
    if s >= 60:   return 'Constrained'
    if s >= 40:   return 'Balanced'
    return "Buyer's Market"

def _safe_q(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:
        logger.debug("Query error: %s | %s", e, sql[:80])
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return []


def _run_query(sql, params=()):
    """Run a query on a guaranteed-fresh psycopg2 connection, bypassing the shared pool."""
    import psycopg2
    db_url = (os.environ.get('DATABASE_URL') or
              os.environ.get('POSTGRES_URL') or
              os.environ.get('DB_URL') or
              os.environ.get('NEON_DATABASE_URL'))
    if db_url:
        fresh = None
        try:
            fresh = psycopg2.connect(db_url)
            fresh.autocommit = True
            cur = fresh.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows
        except Exception as e:
            logger.error("GDCI fresh query failed: %s | %s", e, sql[:80])
            return []
        finally:
            if fresh:
                try: fresh.close()
                except: pass
    # fallback
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as e:
        logger.error("GDCI fallback query failed: %s | %s", e, sql[:80])
        return []


def _load_bulk(cfg, _conn=None):
    fac  = cfg.get('fac_table', 'facilities')
    txn  = cfg.get('txn_table', 'deals')
    mi   = cfg.get('mi_table',  'market_intelligence')
    pw   = cfg.get('power_table','discovered_power_plants')
    sub  = cfg.get('sub_table', 'substations')
    pcol = cfg.get('power_city_col', 'market')
    scol = cfg.get('power_state_col', 'state')

    op_st = [s.strip() for s in cfg.get('fac_status_operational','active,Operational').split(',')]
    pi_st = [s.strip() for s in cfg.get('fac_status_pipeline','Under Construction,Construction,Planned,Planning,Announced,announced,planned').split(',')]
    op_ph = ','.join(['%s']*len(op_st))
    pi_ph = ','.join(['%s']*len(pi_st))

    # Run small/secondary tables FIRST before large facility queries corrupt the connection
    pw_rows = []
    if _bool(cfg, 'power_enabled'):
        pw_rows = _run_query(f"SELECT LOWER(COALESCE({pcol},'')), LOWER(COALESCE({scol},'')), COUNT(*), COALESCE(SUM(capacity_mw),0) FROM {pw} GROUP BY LOWER(COALESCE({pcol},'')), LOWER(COALESCE({scol},''))")
        logger.info("GDCI: loaded %d power plant rows", len(pw_rows))

    sub_rows = []
    if _bool(cfg, 'sub_enabled'):
        sub_rows = _run_query(f"SELECT LOWER(COALESCE(city,'')), LOWER(COALESCE(country,'')), COALESCE(SUM(capacity_mva),0), COALESCE(SUM(available_mva),0) FROM {sub} GROUP BY LOWER(COALESCE(city,'')), LOWER(COALESCE(country,''))")
        logger.info("GDCI: loaded %d substation rows", len(sub_rows))

    mi_rows = []
    if _bool(cfg, 'mi_enabled'):
        mi_rows = _run_query(f"SELECT LOWER(COALESCE(market,'')), avg_rate_per_kw FROM {mi} ORDER BY last_updated DESC")

    deal_rows = _run_query(f"SELECT LOWER(COALESCE(market,'')), COUNT(*), COALESCE(SUM(mw),0) FROM {txn} WHERE date::timestamp >= NOW() - INTERVAL '90 days' GROUP BY LOWER(COALESCE(market,''))")

    # Large facility queries last
    op_rows = _run_query(f"SELECT UPPER(COALESCE(country,'')), LOWER(COALESCE(state,'')), LOWER(COALESCE(city,'')), COALESCE(power_mw,0) FROM {fac} WHERE status IN ({op_ph})", op_st)
    pi_rows = _run_query(f"SELECT UPPER(COALESCE(country,'')), LOWER(COALESCE(state,'')), LOWER(COALESCE(city,'')), COALESCE(power_mw,0) FROM {fac} WHERE status IN ({pi_ph})", pi_st)

    logger.info("GDCI bulk: op=%d pi=%d pw=%d sub=%d", len(op_rows), len(pi_rows), len(pw_rows), len(sub_rows))
    return {'op': op_rows, 'pi': pi_rows, 'deal': deal_rows, 'pw': pw_rows, 'sub': sub_rows, 'mi': mi_rows}


# FIX 1: Prevent state-level double-counting for shared states
def _match_facilities(rows, market):
    """
    Match facility rows to a market. City keywords always take priority.
    State-level matching is ONLY used when this market is the sole occupant
    of that state. For shared states (e.g. Texas has Dallas + Houston),
    ONLY city keyword matches count — no state-level fallback.
    """
    codes    = [c.upper() for c in market['country_codes']]
    city_kw  = market.get('city_kw', [])
    state_kw = market.get('state_kw', [])
    total_cnt = 0
    total_mw  = 0.0

    # Check if this market shares its state with another market
    shares_state = False
    for sk in state_kw:
        shared = SHARED_STATES.get(sk.lower(), [])
        if len(shared) > 1:
            shares_state = True
            break

    for (country, state, city, mw) in rows:
        if country not in codes:
            continue
        city_match = any(kw in city for kw in city_kw) if city_kw else False

        if not city_kw and not state_kw:
            # Country-only markets (e.g., Singapore, Hong Kong)
            total_cnt += 1; total_mw += float(mw)
        elif city_match:
            # City keyword match — always counts
            total_cnt += 1; total_mw += float(mw)
        elif not shares_state and state_kw:
            # State match ONLY if this market doesn't share the state
            state_match = any(kw in state for kw in state_kw) if state else False
            if state_match:
                total_cnt += 1; total_mw += float(mw)
    return total_cnt, total_mw


def _score_market_from_bulk(market, bulk, cfg):
    mid     = market['id']
    weights = {
        'dhci': float(cfg.get('w_dhci', 30)) / 100,
        'dhri': float(cfg.get('w_dhri', 25)) / 100,
        'dhpi': float(cfg.get('w_dhpi', 20)) / 100,
        'dhdi': float(cfg.get('w_dhdi', 15)) / 100,
        'dhpw': float(cfg.get('w_dhpw', 10)) / 100,
    }

    op_cnt, op_mw = _match_facilities(bulk['op'], market)
    pi_cnt, pi_mw = _match_facilities(bulk['pi'], market)
    tot_mw = op_mw + pi_mw

    dhci_val = dhci_d = None
    dhpi_val = dhpi_d = None

    if op_mw > 0:
        pi_ratio = pi_mw / max(op_mw, 1)
        # MW-density tiers — capped so DHCI alone maxes at ~88, pipeline adds up to +12
        if op_mw >= 5000:
            density_score = 80 + min(8, (op_mw - 5000) / 5000 * 8)
        elif op_mw >= 2000:
            density_score = 70 + (op_mw - 2000) / 3000 * 10
        elif op_mw >= 500:
            density_score = 50 + (op_mw - 500) / 1500 * 20
        elif op_mw >= 100:
            density_score = 25 + (op_mw - 100) / 400 * 25
        else:
            density_score = op_mw / 100 * 25
        pipeline_bonus = min(12, pi_ratio * 40)
        dhci_val = round(min(100, density_score + pipeline_bonus), 1)
        vac_pct  = max(2, min(15, 15 - pi_ratio * 5))
        dhci_d   = {'operational_count': op_cnt, 'operational_mw': round(op_mw,1), 'pipeline_mw': round(pi_mw,1), 'total_count': op_cnt+pi_cnt, 'total_mw': round(tot_mw,1), 'vacancy_pct': round(vac_pct,2)}
        dhpi_val = round(min(100, pi_ratio * 50), 1)
        dhpi_d   = {'pipeline_mw': round(pi_mw,1), 'operational_mw': round(op_mw,1), 'pipeline_ratio_pct': round(pi_ratio*100,2)}

    # FIX 4: Broader deal matching — also match on market name and market ID
    dhdi_val = dhdi_d = None
    city_kw  = market.get('city_kw', [])
    codes    = [c.lower() for c in market['country_codes']]
    deal_cnt = deal_mw = 0
    market_name_lower = market['name'].lower()
    for (mkt, cnt, mw) in bulk['deal']:
        city_hit = any(kw in mkt for kw in city_kw) if city_kw else False
        code_hit = any(c in mkt for c in codes) if not city_kw else False
        name_hit = mkt in market_name_lower or market_name_lower.split(',')[0].strip().lower().replace(' ','_') in mkt
        id_hit   = mid in mkt or mkt in mid
        if city_hit or code_hit or name_hit or id_hit:
            deal_cnt += int(cnt); deal_mw += float(mw)
    if deal_cnt > 0 or deal_mw > 0:
        dhdi_val = round(min(100, (deal_mw/2000)*100 + deal_cnt*5), 1)
        dhdi_d   = {'deal_count_90d': deal_cnt, 'absorbed_mw_90d': round(deal_mw,1)}

    dhpw_val = None
    dhpw_d   = {'source': 'none'}

    if _bool(cfg, 'sub_enabled'):
        s_total = s_avail = 0.0
        for (city, country, tmva, amva) in bulk['sub']:
            city_hit    = any(kw in city for kw in city_kw) if city_kw else False
            country_hit = country.upper() in [c.upper() for c in market['country_codes']]
            if city_hit or (not city_kw and country_hit):
                s_total += float(tmva); s_avail += float(amva)
        if s_total > 0:
            headroom = (s_avail / s_total) * 100
            dhpw_val = round(min(100, max(0, (1 - headroom/100)*100)), 1)
            dhpw_d   = {'source':'substations','total_mva':round(s_total,1),'avail_mva':round(s_avail,1),'headroom_pct':round(headroom,1)}

    # FIX 2: Power plant matching — NO state fallback, only direct map + city keywords
    if dhpw_val is None and _bool(cfg, 'power_enabled'):
        pw_cnt = pw_mw = 0.0
        direct_keys = POWER_MARKET_MAP.get(mid, [])
        for (pw_city, pw_state, cnt, mw) in bulk['pw']:
            direct_hit = pw_city in direct_keys
            city_hit   = any(kw.replace(' ','_') in pw_city or kw in pw_city for kw in city_kw) if city_kw else False
            # NO state_hit — prevents matching entire state's power grid
            if direct_hit or city_hit:
                pw_cnt += int(cnt); pw_mw += float(mw)
        if pw_cnt > 0:
            # Tiered scoring for regional grid capacity (data ranges 20-200 GW per market)
            # Higher MW = more grid capacity = better energy readiness
            pw_gw = pw_mw / 1000.0
            if pw_gw >= 150:
                dhpw_val = 90 + min(10, (pw_gw - 150) / 50 * 10)   # 150+ GW: 90-100
            elif pw_gw >= 80:
                dhpw_val = 70 + (pw_gw - 80) / 70 * 20              # 80-150 GW: 70-90
            elif pw_gw >= 40:
                dhpw_val = 50 + (pw_gw - 40) / 40 * 20              # 40-80 GW: 50-70
            elif pw_gw >= 20:
                dhpw_val = 30 + (pw_gw - 20) / 20 * 20              # 20-40 GW: 30-50
            elif pw_gw >= 5:
                dhpw_val = 10 + (pw_gw - 5) / 15 * 20               # 5-20 GW: 10-30
            else:
                dhpw_val = pw_gw / 5 * 10                            # 0-5 GW: 0-10
            dhpw_val = round(min(100, max(0, dhpw_val)), 1)
            dhpw_d   = {'source':'discovered_power_plants','plant_count':int(pw_cnt),'total_mw':round(pw_mw,1)}

    dhri_val = None
    dhri_d   = {}
    if _bool(cfg, 'mi_enabled'):
        seen = set()
        for (mkt, rate) in bulk['mi']:
            if mkt in seen or rate is None: continue
            if any(kw in mkt for kw in city_kw):
                seen.add(mkt)
                ratio    = float(rate) / 120.0
                dhri_val = round(min(100, max(0, (ratio-0.5)*133)), 1)
                dhri_d   = {'rate_per_kw': round(float(rate),2), 'index_value': round(ratio,3)}
                break

    active    = [(v,w) for v,w in [(dhci_val,weights['dhci']),(dhri_val,weights['dhri']),(dhpi_val,weights['dhpi']),(dhdi_val,weights['dhdi']),(dhpw_val,weights['dhpw'])] if v is not None]
    composite = round(sum(v*w for v,w in active)/sum(w for _,w in active), 1) if active else None

    return {
        'market_id':       mid,
        'market_name':     market['name'],
        'region':          market['region'],
        'country':         market['country_codes'][0],
        'composite_score': composite,
        'composite_label': _score_label(composite),
        'composite_color': _score_color(composite),
        'computed_at':     datetime.now(timezone.utc).isoformat(),
        'dhci': {'value': dhci_val, **(dhci_d or {})},
        'dhri': {'value': dhri_val, **(dhri_d or {})},
        'dhpi': {'value': dhpi_val, **(dhpi_d or {})},
        'dhdi': {'value': dhdi_val, **(dhdi_d or {})},
        'dhpw': {'value': dhpw_val, **dhpw_d},
        'connectivity': {},
    }


def _get_all_markets_scored():
    global _bulk_cache, _bulk_ts
    now = time.time()
    if _bulk_cache and now - _bulk_ts < BULK_TTL:
        return _bulk_cache
    cfg  = _get_config()
    bulk = _load_bulk(cfg)
    results = []
    for market in MARKETS:
        try:
            results.append(_score_market_from_bulk(market, bulk, cfg))
        except Exception as e:
            logger.error("Score failed %s: %s", market['id'], e)
    results.sort(key=lambda x: x.get('composite_score') or 0, reverse=True)
    _bulk_cache = results
    _bulk_ts    = now
    return results


def init_config_table():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS gdci_config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.commit()
        for k, v in DEFAULT_CONFIG.items():
            cur.execute("INSERT INTO gdci_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING", (k,str(v)))
        conn.commit()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        for tbl, flag in [('substations','sub_enabled'),('discovered_power_plants','power_enabled'),('market_intelligence','mi_enabled')]:
            if tbl in existing:
                cur.execute("INSERT INTO gdci_config (key,value) VALUES (%s,'true') ON CONFLICT (key) DO UPDATE SET value='true'", (flag,))
        conn.commit()
        cur.close()
        logger.info("GDCI: initialized, %d markets", len(MARKETS))
    except Exception as e:
        logger.error("GDCI: Config init failed: %s", e)


# AUTO-REPAIR: duplicate route '/health' also in main.py:3708 — review and remove one
@index_bp.route('/health')
def health():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM gdci_config")
        cnt = cur.fetchone()[0]
        cur.close()
        return jsonify({'status':'ok','db':'connected','config_keys':cnt,'markets_defined':len(MARKETS),'ts':datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'status':'error','error':str(e)}), 500

# AUTO-REPAIR: duplicate route '/markets' also in main.py:14717 — review and remove one

@index_bp.route('/markets')
def markets_list():
    try:
        region  = request.args.get('region')
        results = _get_all_markets_scored()
        if region:
            results = [m for m in results if m.get('region') == region]
        return jsonify({'markets':results,'count':len(results),'scored':sum(1 for m in results if m.get('composite_score') is not None),'generated_at':datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'error':str(e)}), 500


@index_bp.route('/composite')
def composite():
    try:
        results = _get_all_markets_scored()
        scored  = [m for m in results if m.get('composite_score') is not None]
        if not scored:
            return jsonify({'error':'No market data'}), 503
        avg    = sum(m['composite_score'] for m in scored) / len(scored)
        tot_mw = sum(m.get('dhci',{}).get('total_mw',0) or 0 for m in scored)
        pi_mw  = sum(m.get('dhpi',{}).get('pipeline_mw',0) or 0 for m in scored)
        op_mw  = sum(m.get('dhci',{}).get('operational_mw',0) or 0 for m in scored)
        vac    = round(sum((m.get('dhci',{}).get('vacancy_pct') or 0)*(m.get('dhci',{}).get('operational_mw') or 0) for m in scored)/max(op_mw,1),2) if op_mw>0 else None
        score  = round(avg, 1)
        month  = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({
            'issue':              f"{month} Issue",
            'composite_score':    score,
            'composite_label':    _score_label(score),
            'composite_color':    _score_color(score),
            'global_vacancy_pct': vac,
            'total_tracked_mw':   round(tot_mw,1),
            'total_pipeline_mw':  round(pi_mw,1),
            'markets_covered':    len(scored),
            'markets_with_data':  len(scored),
            'citation':           f"According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {score} in {month}, indicating a {_score_label(score).lower()} environment across {len(scored)} tracked markets. Source: DC Hub GDCI, dchub.cloud/index",
            'generated_at':       datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({'error':str(e)}), 500


@index_bp.route('/market/<market_id>')
def market_detail(market_id):
    try:
        results = _get_all_markets_scored()
        m = next((x for x in results if x['market_id'] == market_id), None)
        if not m:
            return jsonify({'error':f'Unknown market: {market_id}'}), 404
        return jsonify(m)
    except Exception as e:
        return jsonify({'error':str(e)}), 500


@index_bp.route('/regions')
def regions():
    try:
        results = _get_all_markets_scored()
        rmap = defaultdict(lambda: {'scores':[],'mw':0,'pi':0,'markets':[]})
        for m in results:
            r = m.get('region','other')
            rmap[r]['markets'].append(m['market_name'])
            if m.get('composite_score') is not None:
                rmap[r]['scores'].append(m['composite_score'])
            rmap[r]['mw'] += m.get('dhci',{}).get('total_mw',0) or 0
            rmap[r]['pi'] += m.get('dhpi',{}).get('pipeline_mw',0) or 0
        out = []
        for r, d in rmap.items():
            avg = round(sum(d['scores'])/len(d['scores']),1) if d['scores'] else None
            out.append({'region':r,'market_count':len(d['markets']),'composite_score':avg,'composite_label':_score_label(avg),'composite_color':_score_color(avg),'total_mw':round(d['mw'],1),'pipeline_mw':round(d['pi'],1)})
        out.sort(key=lambda x: x.get('composite_score') or 0, reverse=True)
        return jsonify({'regions':out,'generated_at':datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'error':str(e)}), 500


@index_bp.route('/citation/<market_id>')
def citation(market_id):
    try:
        results = _get_all_markets_scored()
        m = next((x for x in results if x['market_id']==market_id), None)
        if not m:
            return jsonify({'error':'Unknown market'}), 404
        month = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({'market_id':market_id,'market_name':m['market_name'],'score':m['composite_score'],
            'citation':f"According to the DC Hub Global Data Center Index (GDCI), {m['market_name']} scored {m['composite_score']} ({m['composite_label']}) in {month}. Source: DC Hub GDCI, dchub.cloud/index"})
    except Exception as e:
        return jsonify({'error':str(e)}), 500


def _require_admin(cfg):
    key      = cfg.get('admin_key','')
    provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if key and provided != key:
        return jsonify({'error':'Unauthorized'}), 401
    return None

@index_bp.route('/admin/config', methods=['GET'])
def admin_config_get():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err: return err
# AUTO-REPAIR: duplicate route '/admin/config' also in index_api.py:627 — review and remove one
    return jsonify({'config':cfg,'count':len(cfg)})

@index_bp.route('/admin/config', methods=['POST'])
def admin_config_set():
    cfg  = _get_config()
    err  = _require_admin(cfg)
    if err: return err
    data = request.get_json(silent=True) or {}
    if not data: return jsonify({'error':'No data'}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        for k,v in data.items():
            cur.execute("INSERT INTO gdci_config (key,value,updated_at) VALUES (%s,%s,NOW() ON CONFLICT DO NOTHING) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()", (k,str(v)))
        conn.commit(); cur.close()
        global _config_cache,_config_ts,_bulk_cache,_bulk_ts
        _config_cache={}; _config_ts=0; _bulk_cache=None; _bulk_ts=0
        return jsonify({'updated':list(data.keys()),'count':len(data),'note':'Cache cleared.'})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@index_bp.route('/admin/refresh', methods=['POST'])
def admin_refresh():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err: return err
    global _config_cache,_config_ts,_bulk_cache,_bulk_ts
    _config_cache={}; _config_ts=0; _bulk_cache=None; _bulk_ts=0
    return jsonify({'cleared':True,'at':datetime.now(timezone.utc).isoformat()})

@index_bp.route('/admin/sources')
def admin_sources():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err: return err
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        cur.close()
        def src(name,tbl,flag):
            return {'name':name,'table':tbl,'enabled':_bool(cfg,flag) if flag else True,'exists':tbl in existing}
        return jsonify({'sources':[
            src('Facilities (DHCI+DHPI)',cfg.get('fac_table','facilities'),None),
            src('Transactions (DHDI)',   cfg.get('txn_table','deals'),None),
            src('Market Intelligence',  cfg.get('mi_table','market_intelligence'),'mi_enabled'),
            src('Power Plants',         cfg.get('power_table','discovered_power_plants'),'power_enabled'),
            src('Substations',          cfg.get('sub_table','substations'),'sub_enabled'),
        ],'markets_defined':len(MARKETS)})
    except Exception as e:
        return jsonify({'error':str(e)}), 500
