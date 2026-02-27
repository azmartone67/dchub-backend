"""
DC Hub Global Data Center Index API (index_api.py)
Flask Blueprint — mounts at /api/index
v6.0 — Percentile-based scoring: markets ranked against each other
"""

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
    'fac_status_pipeline':    'Under Construction,Construction,Planned,Planning,Announced,announced,planned',
    'mi_enabled':    'true',
    'power_enabled': 'true',
    'sub_enabled':   'true',
    'power_city_col': 'market',
    'power_state_col': 'state',
    'w_dhci': '30', 'w_dhri': '25', 'w_dhpi': '20', 'w_dhdi': '15', 'w_dhpw': '10',
    'admin_key': '',
}

MARKETS = [
    {'id':'nova','name':'Northern Virginia, US',    'region':'us',   'cc':['US'],'ck':['ashburn','loudoun','sterling','leesburg','manassas','northern virginia'],'sk':['VA']},
    {'id':'dal', 'name':'Dallas/Fort Worth, US',    'region':'us',   'cc':['US'],'ck':['dallas','fort worth','irving','plano','arlington','addison','garland','richardson','lewisville','allen'],'sk':['TX'],'sk_cities':True},
    {'id':'phx', 'name':'Phoenix, US',              'region':'us',   'cc':['US'],'ck':['phoenix','chandler','mesa','tempe','scottsdale','gilbert','goodyear','peoria'],'sk':['AZ']},
    {'id':'chi', 'name':'Chicago, US',              'region':'us',   'cc':['US'],'ck':['chicago','elk grove','aurora','naperville','lisle','itasca','franklin park'],'sk':['IL'],'sk_cities':True},
    {'id':'nyc', 'name':'New York/New Jersey, US',  'region':'us',   'cc':['US'],'ck':['new york','secaucus','newark','jersey city','manhattan','brooklyn','bronx','weehawken','parsippany'],'sk':['NJ','NY'],'sk_cities':True},
    {'id':'sea', 'name':'Seattle, US',              'region':'us',   'cc':['US'],'ck':['seattle','quincy','wenatchee','bellevue','redmond','renton','tacoma'],'sk':['WA'],'sk_cities':True},
    {'id':'sfo', 'name':'San Francisco Bay, US',    'region':'us',   'cc':['US'],'ck':['san jose','santa clara','fremont','milpitas','sunnyvale','palo alto','san francisco','oakland','hayward','menlo park','mountain view'],'sk':[]},
    {'id':'lax', 'name':'Los Angeles, US',          'region':'us',   'cc':['US'],'ck':['los angeles','el segundo','torrance','hawthorne','long beach','compton','culver city'],'sk':[]},
    {'id':'atl', 'name':'Atlanta, US',              'region':'us',   'cc':['US'],'ck':['atlanta','norcross','lithia springs','douglasville','alpharetta','marietta','lawrenceville'],'sk':['GA']},
    {'id':'bos', 'name':'Boston, US',               'region':'us',   'cc':['US'],'ck':['boston','somerville','cambridge','waltham','quincy','woburn','norwood'],'sk':['MA'],'sk_cities':True},
    {'id':'den', 'name':'Denver, US',               'region':'us',   'cc':['US'],'ck':['denver','englewood','littleton','aurora','centennial','highlands ranch','broomfield'],'sk':['CO']},
    {'id':'mia', 'name':'Miami, US',                'region':'us',   'cc':['US'],'ck':['miami','doral','boca raton','fort lauderdale','miramar','pompano beach','hollywood'],'sk':['FL'],'sk_cities':True},
    {'id':'iah', 'name':'Houston, US',              'region':'us',   'cc':['US'],'ck':['houston','katy','sugar land','stafford','webster','humble','pearland'],'sk':[]},
    {'id':'msp', 'name':'Minneapolis, US',          'region':'us',   'cc':['US'],'ck':['minneapolis','saint paul','eden prairie','bloomington','st paul','edina','plymouth'],'sk':['MN']},
    {'id':'slc', 'name':'Salt Lake City, US',       'region':'us',   'cc':['US'],'ck':['salt lake city','west jordan','draper','sandy','south jordan','orem','provo'],'sk':['UT']},
    {'id':'yyz', 'name':'Toronto, Canada',          'region':'us',   'cc':['CA'],'ck':['toronto','mississauga','markham','vaughan','brampton'],'sk':['ON']},
    {'id':'yvr', 'name':'Vancouver, Canada',        'region':'us',   'cc':['CA'],'ck':['vancouver','burnaby','richmond','surrey'],'sk':['BC']},
    {'id':'lhr', 'name':'London, UK',               'region':'emea', 'cc':['GB','UK'],'ck':['london','slough','reading','uxbridge','hayes'],'sk':[]},
    {'id':'fra', 'name':'Frankfurt, Germany',       'region':'emea', 'cc':['DE'],'ck':['frankfurt','eschborn'],'sk':[]},
    {'id':'ams', 'name':'Amsterdam, Netherlands',   'region':'emea', 'cc':['NL'],'ck':['amsterdam'],'sk':[]},
    {'id':'par', 'name':'Paris, France',            'region':'emea', 'cc':['FR'],'ck':['paris','saint-denis','vitry'],'sk':[]},
    {'id':'dub', 'name':'Dublin, Ireland',          'region':'emea', 'cc':['IE'],'ck':['dublin'],'sk':[]},
    {'id':'zrh', 'name':'Zurich, Switzerland',      'region':'emea', 'cc':['CH'],'ck':['zurich','geneva'],'sk':[]},
    {'id':'sto', 'name':'Stockholm, Sweden',        'region':'emea', 'cc':['SE'],'ck':['stockholm'],'sk':[]},
    {'id':'mad', 'name':'Madrid, Spain',            'region':'emea', 'cc':['ES'],'ck':['madrid','barcelona'],'sk':[]},
    {'id':'mil', 'name':'Milan, Italy',             'region':'emea', 'cc':['IT'],'ck':['milan','milano'],'sk':[]},
    {'id':'war', 'name':'Warsaw, Poland',           'region':'emea', 'cc':['PL'],'ck':['warsaw','wroclaw'],'sk':[]},
    {'id':'vie', 'name':'Vienna, Austria',          'region':'emea', 'cc':['AT'],'ck':['vienna','wien'],'sk':[]},
    {'id':'cop', 'name':'Copenhagen, Denmark',      'region':'emea', 'cc':['DK'],'ck':['copenhagen'],'sk':[]},
    {'id':'hel', 'name':'Helsinki, Finland',        'region':'emea', 'cc':['FI'],'ck':['helsinki'],'sk':[]},
    {'id':'osl', 'name':'Oslo, Norway',             'region':'emea', 'cc':['NO'],'ck':['oslo'],'sk':[]},
    {'id':'msc', 'name':'Moscow, Russia',           'region':'emea', 'cc':['RU'],'ck':['moscow'],'sk':[]},
    {'id':'ist', 'name':'Istanbul, Turkey',         'region':'emea', 'cc':['TR'],'ck':['istanbul'],'sk':[]},
    {'id':'dxb', 'name':'Dubai, UAE',               'region':'emea', 'cc':['AE'],'ck':['dubai','abu dhabi'],'sk':[]},
    {'id':'ruh', 'name':'Riyadh, Saudi Arabia',     'region':'emea', 'cc':['SA'],'ck':['riyadh','jeddah'],'sk':[]},
    {'id':'jnb', 'name':'Johannesburg, South Africa','region':'emea','cc':['ZA'],'ck':['johannesburg','cape town'],'sk':[]},
    {'id':'nbo', 'name':'Nairobi, Kenya',           'region':'emea', 'cc':['KE'],'ck':['nairobi'],'sk':[]},
    {'id':'lag', 'name':'Lagos, Nigeria',           'region':'emea', 'cc':['NG'],'ck':['lagos'],'sk':[]},
    {'id':'cai', 'name':'Cairo, Egypt',             'region':'emea', 'cc':['EG'],'ck':['cairo'],'sk':[]},
    {'id':'sin', 'name':'Singapore',                'region':'apac', 'cc':['SG'],'ck':['singapore'],'sk':[]},
    {'id':'tyo', 'name':'Tokyo, Japan',             'region':'apac', 'cc':['JP'],'ck':['tokyo','yokohama','kawasaki'],'sk':[]},
    {'id':'syd', 'name':'Sydney, Australia',        'region':'apac', 'cc':['AU'],'ck':['sydney'],'sk':['NSW','New South Wales']},
    {'id':'hkg', 'name':'Hong Kong',                'region':'apac', 'cc':['HK'],'ck':['hong kong'],'sk':[]},
    {'id':'sha', 'name':'Shanghai, China',          'region':'apac', 'cc':['CN'],'ck':['shanghai'],'sk':[]},
    {'id':'pek', 'name':'Beijing, China',           'region':'apac', 'cc':['CN'],'ck':['beijing'],'sk':[]},
    {'id':'bom', 'name':'Mumbai, India',            'region':'apac', 'cc':['IN'],'ck':['mumbai','pune','navi mumbai'],'sk':[]},
    {'id':'del', 'name':'Delhi, India',             'region':'apac', 'cc':['IN'],'ck':['delhi','noida','gurgaon','gurugram'],'sk':[]},
    {'id':'sel', 'name':'Seoul, South Korea',       'region':'apac', 'cc':['KR'],'ck':['seoul'],'sk':[]},
    {'id':'kul', 'name':'Kuala Lumpur, Malaysia',   'region':'apac', 'cc':['MY'],'ck':['kuala lumpur','cyberjaya'],'sk':[]},
    {'id':'mel', 'name':'Melbourne, Australia',     'region':'apac', 'cc':['AU'],'ck':['melbourne'],'sk':['VIC','Victoria']},
    {'id':'jak', 'name':'Jakarta, Indonesia',       'region':'apac', 'cc':['ID'],'ck':['jakarta'],'sk':[]},
    {'id':'bkk', 'name':'Bangkok, Thailand',        'region':'apac', 'cc':['TH'],'ck':['bangkok'],'sk':[]},
    {'id':'mnl', 'name':'Manila, Philippines',      'region':'apac', 'cc':['PH'],'ck':['manila','pasig','makati'],'sk':[]},
    {'id':'osk', 'name':'Osaka, Japan',             'region':'apac', 'cc':['JP'],'ck':['osaka'],'sk':[]},
    {'id':'gru', 'name':'São Paulo, Brazil',        'region':'latam','cc':['BR'],'ck':['sao paulo','são paulo','campinas'],'sk':[]},
    {'id':'mex', 'name':'Mexico City, Mexico',      'region':'latam','cc':['MX'],'ck':['mexico city','queretaro','monterrey'],'sk':[]},
    {'id':'bog', 'name':'Bogotá, Colombia',         'region':'latam','cc':['CO'],'ck':['bogota','bogotá'],'sk':[]},
    {'id':'scl', 'name':'Santiago, Chile',          'region':'latam','cc':['CL'],'ck':['santiago'],'sk':[]},
    {'id':'bue', 'name':'Buenos Aires, Argentina',  'region':'latam','cc':['AR'],'ck':['buenos aires'],'sk':[]},
    {'id':'lim', 'name':'Lima, Peru',               'region':'latam','cc':['PE'],'ck':['lima'],'sk':[]},
]
MARKET_BY_ID = {m['id']: m for m in MARKETS}


def _get_config():
    global _config_cache, _config_ts
    now = time.time()
    if now - _config_ts < CONFIG_TTL and _config_cache:
        return _config_cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT key, value FROM gdci_config")
        for r in cur.fetchall():
            cfg[r[0]] = r[1]
        cur.close()
    except Exception as e:
        logger.warning("Config load: %s", e)
    _config_cache = cfg
    _config_ts    = now
    return cfg

def _bool(cfg, key):
    return str(cfg.get(key,'false')).lower() in ('true','1','yes')

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
        logger.debug("Query err: %s | %s", e, sql[:60])
        try: cur.connection.rollback()
        except: pass
        return []

def _percentile_score(value, all_values):
    """Convert a raw value to a 0-100 percentile score vs all markets"""
    if value is None or not all_values:
        return None
    valid = [v for v in all_values if v is not None and v > 0]
    if not valid:
        return None
    rank = sum(1 for v in valid if v <= value)
    return round((rank / len(valid)) * 100, 1)


def _match_fac(rows, market):
    """Match (country, state, city, mw) rows to a market"""
    codes = set(market['cc'])
    ck    = market.get('ck', [])
    sk    = [s.upper() for s in market.get('sk', [])]
    # sk_cities=True means state alone matches (large states like TX, IL)
    # sk_cities=False (default) means we require city match too for state-based markets
    use_state_alone = not market.get('sk_cities', False) and bool(sk) and not ck

    cnt = 0
    mw  = 0.0
    for (country, state, city, power) in rows:
        if country not in codes:
            continue
        city_hit  = any(kw in city  for kw in ck) if ck else False
        state_hit = (state.upper() in sk) if sk else False

        # International: no ck, no sk → country match alone
        if not ck and not sk:
            cnt += 1; mw += float(power); continue

        # Pure city match
        if city_hit:
            cnt += 1; mw += float(power); continue

        # State match: only use if city also roughly in the metro
        # For very specific metros (nova, sfo, lax) we only use city keywords
        # For broad state metros (dal, chi, nyc) we allow state as fallback
        if state_hit and market.get('sk_cities', False):
            cnt += 1; mw += float(power); continue

    return cnt, mw


def _load_bulk(cfg, conn):
    cur  = conn.cursor()
    fac  = cfg.get('fac_table', 'facilities')
    txn  = cfg.get('txn_table', 'deals')
    pw   = cfg.get('power_table', 'discovered_power_plants')
    sub  = cfg.get('sub_table', 'substations')
    pcol = cfg.get('power_city_col', 'market')
    scol = cfg.get('power_state_col', 'state')
    mi   = cfg.get('mi_table', 'market_intelligence')

    op_st = [s.strip() for s in cfg.get('fac_status_operational','active,Operational').split(',')]
    pi_st = [s.strip() for s in cfg.get('fac_status_pipeline','Under Construction,Construction,Planned,Planning,Announced,announced,planned').split(',')]
    op_ph = ','.join(['%s']*len(op_st))
    pi_ph = ','.join(['%s']*len(pi_st))

    # (country_upper, state_upper, city_lower, mw)
    op_rows = _safe_q(cur, f"SELECT UPPER(COALESCE(country,'')), UPPER(COALESCE(state,'')), LOWER(COALESCE(city,'')), COALESCE(power_mw,0) FROM {fac} WHERE status IN ({op_ph})", op_st)
    pi_rows = _safe_q(cur, f"SELECT UPPER(COALESCE(country,'')), UPPER(COALESCE(state,'')), LOWER(COALESCE(city,'')), COALESCE(power_mw,0) FROM {fac} WHERE status IN ({pi_ph})", pi_st)

    # Deals: (market_lower, count, mw)
    deal_rows = _safe_q(cur, f"SELECT LOWER(COALESCE(market,'')), COUNT(*), COALESCE(SUM(mw),0) FROM {txn} WHERE date >= NOW() - INTERVAL '90 days' GROUP BY LOWER(market)")

    # Power plants: (city_lower, state_lower, count, mw)
    pw_rows = []
    if _bool(cfg,'power_enabled'):
        pw_rows = _safe_q(cur, f"SELECT LOWER(COALESCE({pcol},'')), UPPER(COALESCE({scol},'')), COUNT(*), COALESCE(SUM(capacity_mw),0) FROM {pw} GROUP BY LOWER({pcol}), UPPER({scol})")

    # Substations: (city_lower, country_upper, total_mva, avail_mva)
    sub_rows = []
    if _bool(cfg,'sub_enabled'):
        sub_rows = _safe_q(cur, f"SELECT LOWER(COALESCE(city,'')), UPPER(COALESCE(country,'')), COALESCE(SUM(capacity_mva),0), COALESCE(SUM(available_mva),0) FROM {sub} GROUP BY LOWER(city), UPPER(country)")

    # MI: (market_lower, rate)
    mi_rows = []
    if _bool(cfg,'mi_enabled'):
        mi_rows = _safe_q(cur, f"SELECT LOWER(COALESCE(market,'')), avg_rate_per_kw FROM {mi} ORDER BY recorded_at DESC")

    cur.close()
    return {'op': op_rows, 'pi': pi_rows, 'deal': deal_rows, 'pw': pw_rows, 'sub': sub_rows, 'mi': mi_rows}


def _raw_metrics(market, bulk, cfg):
    """Get raw metrics for a market — no scoring yet"""
    ck = market.get('ck', [])
    sk = [s.upper() for s in market.get('sk', [])]

    op_cnt, op_mw = _match_fac(bulk['op'], market)
    pi_cnt, pi_mw = _match_fac(bulk['pi'], market)

    # Deals
    deal_cnt = deal_mw = 0
    for (mkt, cnt, mw) in bulk['deal']:
        if any(kw in mkt for kw in ck) if ck else any(c.lower() in mkt for c in market['cc']):
            deal_cnt += int(cnt); deal_mw += float(mw)

    # Power — substations first
    sub_total = sub_avail = 0.0
    if _bool(cfg,'sub_enabled'):
        for (city, country, tmva, amva) in bulk['sub']:
            if (any(kw in city for kw in ck) if ck else country in set(market['cc'])):
                sub_total += float(tmva); sub_avail += float(amva)

    pw_cnt = pw_mw = 0.0
    if _bool(cfg,'power_enabled'):
        for (pw_city, pw_state, cnt, mw) in bulk['pw']:
            city_hit  = any(kw in pw_city for kw in ck) if ck else False
            state_hit = pw_state in sk if sk else False
            if city_hit or state_hit:
                pw_cnt += int(cnt); pw_mw += float(mw)

    # MI rate
    rate = None
    if _bool(cfg,'mi_enabled'):
        for (mkt, r) in bulk['mi']:
            if r and (any(kw in mkt for kw in ck) if ck else False):
                rate = float(r); break

    return {
        'op_cnt': op_cnt, 'op_mw': op_mw,
        'pi_cnt': pi_cnt, 'pi_mw': pi_mw,
        'deal_cnt': deal_cnt, 'deal_mw': deal_mw,
        'sub_total': sub_total, 'sub_avail': sub_avail,
        'pw_cnt': int(pw_cnt), 'pw_mw': pw_mw,
        'rate': rate,
    }


def _score_from_percentiles(market, raw, all_raw, cfg):
    """Score market using percentile rank vs all markets"""
    mid = market['id']
    weights = {
        'dhci': float(cfg.get('w_dhci',30))/100,
        'dhri': float(cfg.get('w_dhri',25))/100,
        'dhpi': float(cfg.get('w_dhpi',20))/100,
        'dhdi': float(cfg.get('w_dhdi',15))/100,
        'dhpw': float(cfg.get('w_dhpw',10))/100,
    }

    # Percentile helpers
    def pct(val, key):
        vals = [r[key] for r in all_raw.values() if r[key] is not None and r[key] > 0]
        return _percentile_score(val, vals)

    op_mw   = raw['op_mw']
    pi_mw   = raw['pi_mw']
    deal_mw = raw['deal_mw']
    pw_mw   = raw['pw_mw']
    sub_total = raw['sub_total']
    rate    = raw['rate']

    # DHCI: percentile of operational MW
    dhci_val = pct(op_mw, 'op_mw') if op_mw > 0 else None
    dhci_d   = {
        'operational_count': raw['op_cnt'],
        'operational_mw':    round(op_mw, 1),
        'pipeline_mw':       round(pi_mw, 1),
        'total_count':       raw['op_cnt'] + raw['pi_cnt'],
        'total_mw':          round(op_mw + pi_mw, 1),
        'vacancy_pct':       None,
    } if op_mw > 0 else {}

    # DHPI: percentile of pipeline MW (absolute, not ratio)
    dhpi_val = pct(pi_mw, 'pi_mw') if pi_mw > 0 else None
    dhpi_d   = {
        'pipeline_mw':    round(pi_mw, 1),
        'operational_mw': round(op_mw, 1),
        'pipeline_ratio_pct': round(pi_mw/op_mw*100, 2) if op_mw > 0 else None,
    } if pi_mw > 0 else {}

    # DHDI: percentile of deal MW absorbed
    dhdi_val = pct(deal_mw, 'deal_mw') if deal_mw > 0 else None
    dhdi_d   = {'deal_count_90d': raw['deal_cnt'], 'absorbed_mw_90d': round(deal_mw,1)} if deal_mw > 0 else {}

    # DHPW: substations or power plants, percentile
    dhpw_val = None
    dhpw_d   = {'source': 'none'}
    if sub_total > 0:
        dhpw_val = pct(sub_total, 'sub_total')
        dhpw_d   = {'source':'substations','total_mva':round(sub_total,1),'avail_mva':round(raw['sub_avail'],1)}
    elif pw_mw > 0:
        dhpw_val = pct(pw_mw, 'pw_mw')
        dhpw_d   = {'source':'discovered_power_plants','plant_count':raw['pw_cnt'],'total_mw':round(pw_mw,1)}

    # DHRI: rate index
    dhri_val = None
    dhri_d   = {}
    if rate:
        baseline = 120.0
        ratio    = rate / baseline
        dhri_val = round(min(100, max(0, (ratio-0.5)*133)), 1)
        dhri_d   = {'rate_per_kw': round(rate,2), 'index_value': round(ratio,3)}

    # Composite
    active = [(v,w) for v,w in [(dhci_val,weights['dhci']),(dhri_val,weights['dhri']),
              (dhpi_val,weights['dhpi']),(dhdi_val,weights['dhdi']),(dhpw_val,weights['dhpw'])] if v is not None]
    composite = round(sum(v*w for v,w in active)/sum(w for _,w in active),1) if active else None

    return {
        'market_id':       mid,
        'market_name':     market['name'],
        'region':          market['region'],
        'country':         market['cc'][0],
        'composite_score': composite,
        'composite_label': _score_label(composite),
        'composite_color': _score_color(composite),
        'computed_at':     datetime.now(timezone.utc).isoformat(),
        'dhci': {'value': dhci_val, **dhci_d},
        'dhri': {'value': dhri_val, **dhri_d},
        'dhpi': {'value': dhpi_val, **dhpi_d},
        'dhdi': {'value': dhdi_val, **dhdi_d},
        'dhpw': {'value': dhpw_val, **dhpw_d},
        'connectivity': {},
    }


def _get_all_markets_scored():
    global _bulk_cache, _bulk_ts
    now = time.time()
    if _bulk_cache and now - _bulk_ts < BULK_TTL:
        return _bulk_cache

    cfg  = _get_config()
    conn = get_db()
    bulk = _load_bulk(cfg, conn)

    # Pass 1: collect raw metrics for all markets
    all_raw = {}
    for m in MARKETS:
        try:
            all_raw[m['id']] = _raw_metrics(m, bulk, cfg)
        except Exception as e:
            logger.error("Raw metrics failed %s: %s", m['id'], e)
            all_raw[m['id']] = {'op_mw':0,'pi_mw':0,'deal_mw':0,'sub_total':0,'pw_mw':0,'op_cnt':0,'pi_cnt':0,'deal_cnt':0,'sub_avail':0,'pw_cnt':0,'rate':None}

    # Pass 2: score using percentiles
    results = []
    for m in MARKETS:
        try:
            raw = all_raw[m['id']]
            r   = _score_from_percentiles(m, raw, all_raw, cfg)
            results.append(r)
        except Exception as e:
            logger.error("Scoring failed %s: %s", m['id'], e)

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
        for k,v in DEFAULT_CONFIG.items():
            cur.execute("INSERT INTO gdci_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING", (k,str(v)))
        conn.commit()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        for tbl,flag in [('substations','sub_enabled'),('discovered_power_plants','power_enabled'),('market_intelligence','mi_enabled')]:
            if tbl in existing:
                cur.execute("INSERT INTO gdci_config (key,value) VALUES (%s,'true') ON CONFLICT (key) DO UPDATE SET value='true'", (flag,))
        conn.commit()
        cur.close()
        logger.info("GDCI: ✅ Config initialized, %d markets", len(MARKETS))
    except Exception as e:
        logger.error("GDCI init: %s", e)


# ─── Routes ──────────────────────────────────────────────────────────────────

@index_bp.route('/health')
def health():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM gdci_config")
        cnt = cur.fetchone()[0]; cur.close()
        return jsonify({'status':'ok','db':'connected','config_keys':cnt,'markets_defined':len(MARKETS),'ts':datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({'status':'error','error':str(e)}), 500

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
        if not scored: return jsonify({'error':'No data'}), 503
        avg    = sum(m['composite_score'] for m in scored)/len(scored)
        tot_mw = sum(m.get('dhci',{}).get('total_mw',0) or 0 for m in scored)
        pi_mw  = sum(m.get('dhpi',{}).get('pipeline_mw',0) or 0 for m in scored)
        score  = round(avg,1)
        month  = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({
            'issue':f"{month} Issue",'composite_score':score,'composite_label':_score_label(score),
            'composite_color':_score_color(score),'global_vacancy_pct':None,
            'total_tracked_mw':round(tot_mw,1),'total_pipeline_mw':round(pi_mw,1),
            'markets_covered':len(scored),'markets_with_data':len(scored),
            'citation':f"According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {score} in {month}, indicating a {_score_label(score).lower()} environment across {len(scored)} tracked markets. Source: DC Hub GDCI, dchub.cloud/index",
            'generated_at':datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@index_bp.route('/market/<market_id>')
def market_detail(market_id):
    try:
        results = _get_all_markets_scored()
        m = next((x for x in results if x['market_id']==market_id), None)
        if not m: return jsonify({'error':f'Unknown: {market_id}'}), 404
        return jsonify(m)
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@index_bp.route('/regions')
def regions():
    try:
        results = _get_all_markets_scored()
        rmap = defaultdict(lambda:{'scores':[],'mw':0,'pi':0,'markets':[]})
        for m in results:
            r = m.get('region','other')
            rmap[r]['markets'].append(m['market_name'])
            if m.get('composite_score') is not None: rmap[r]['scores'].append(m['composite_score'])
            rmap[r]['mw'] += m.get('dhci',{}).get('total_mw',0) or 0
            rmap[r]['pi'] += m.get('dhpi',{}).get('pipeline_mw',0) or 0
        out = []
        for r,d in rmap.items():
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
        if not m: return jsonify({'error':'Unknown'}), 404
        month = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({'market_id':market_id,'market_name':m['market_name'],'score':m['composite_score'],
            'citation':f"According to the DC Hub Global Data Center Index (GDCI), {m['market_name']} scored {m['composite_score']} ({m['composite_label']}) in {month}. Source: DC Hub GDCI, dchub.cloud/index"})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

def _require_admin(cfg):
    key = cfg.get('admin_key','')
    provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if key and provided != key: return jsonify({'error':'Unauthorized'}), 401
    return None

@index_bp.route('/admin/config', methods=['GET'])
def admin_config_get():
    cfg=_get_config(); err=_require_admin(cfg)
    if err: return err
    return jsonify({'config':cfg,'count':len(cfg)})

@index_bp.route('/admin/config', methods=['POST'])
def admin_config_set():
    cfg=_get_config(); err=_require_admin(cfg)
    if err: return err
    data=request.get_json(silent=True) or {}
    if not data: return jsonify({'error':'No data'}),400
    try:
        conn=get_db(); cur=conn.cursor()
        for k,v in data.items():
            cur.execute("INSERT INTO gdci_config (key,value,updated_at) VALUES (%s,%s,NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()",(k,str(v)))
        conn.commit(); cur.close()
        global _config_cache,_config_ts,_bulk_cache,_bulk_ts
        _config_cache={}; _config_ts=0; _bulk_cache=None; _bulk_ts=0
        return jsonify({'updated':list(data.keys()),'count':len(data),'note':'Cache cleared.'})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@index_bp.route('/admin/refresh', methods=['POST'])
def admin_refresh():
    cfg=_get_config(); err=_require_admin(cfg)
    if err: return err
    global _config_cache,_config_ts,_bulk_cache,_bulk_ts
    _config_cache={}; _config_ts=0; _bulk_cache=None; _bulk_ts=0
    return jsonify({'cleared':True,'at':datetime.now(timezone.utc).isoformat()})

@index_bp.route('/admin/sources')
def admin_sources():
    cfg=_get_config(); err=_require_admin(cfg)
    if err: return err
    try:
        conn=get_db(); cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing={r[0] for r in cur.fetchall()}; cur.close()
        def src(n,t,f): return {'name':n,'table':t,'enabled':_bool(cfg,f) if f else True,'exists':t in existing}
        return jsonify({'sources':[
            src('Facilities',cfg.get('fac_table','facilities'),None),
            src('Deals',cfg.get('txn_table','deals'),None),
            src('Market Intelligence',cfg.get('mi_table','market_intelligence'),'mi_enabled'),
            src('Power Plants',cfg.get('power_table','discovered_power_plants'),'power_enabled'),
            src('Substations',cfg.get('sub_table','substations'),'sub_enabled'),
        ],'markets_defined':len(MARKETS),'scoring':'percentile_v6'})
    except Exception as e:
        return jsonify({'error':str(e)}),500
