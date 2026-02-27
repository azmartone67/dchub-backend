"""
DC Hub Global Data Center Index API (index_api.py)
Flask Blueprint — mounts at /api/index
v4.0 — Bulk query architecture: 6 queries total for all 60 markets
"""

import time
import logging
from datetime import datetime, timezone
from collections import defaultdict

from flask import Blueprint, jsonify, request
from db_utils import get_db

logger = logging.getLogger(__name__)

index_bp = Blueprint('index', __name__, url_prefix='/api/index')

# ─── Caches ───────────────────────────────────────────────────────────────────
_config_cache  = {}
_config_ts     = 0
_bulk_cache    = None
_bulk_ts       = 0
CONFIG_TTL     = 600
BULK_TTL       = 3600

# ─── Default config ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    'fac_table':              'facilities',
    'txn_table':              'deals',
    'mi_table':               'market_intelligence',
    'power_table':            'discovered_power_plants',
    'sub_table':              'substations',
    'tx_table':               'discovered_transmission_lines',
    'fiber_table':            'fiber_routes',
    'fac_status_operational': 'active,Operational',
    'fac_status_pipeline':    'Under Construction,Construction,Planned,Planning,Announced,announced,planned',
    'mi_enabled':             'true',
    'power_enabled':          'true',
    'sub_enabled':            'true',
    'tx_enabled':             'true',
    'fiber_enabled':          'true',
    'power_city_col':         'market',
    'power_state_col':        'state',
    'w_dhci': '30', 'w_dhri': '25', 'w_dhpi': '20', 'w_dhdi': '15', 'w_dhpw': '10',
    'admin_key': '',
}

# ─── 60 Global Markets ────────────────────────────────────────────────────────
MARKETS = [
    # US
    {'id':'nova','name':'Northern Virginia, US','region':'us','country':'US','state':'VA','keywords':['ashburn','loudoun','northern virginia','nova']},
    {'id':'dal', 'name':'Dallas/Fort Worth, US','region':'us','country':'US','state':'TX','keywords':['dallas','fort worth','dfw','irving','plano']},
    {'id':'phx', 'name':'Phoenix, US',          'region':'us','country':'US','state':'AZ','keywords':['phoenix','chandler','mesa','tempe','scottsdale']},
    {'id':'chi', 'name':'Chicago, US',          'region':'us','country':'US','state':'IL','keywords':['chicago','elk grove','aurora']},
    {'id':'nyc', 'name':'New York/New Jersey, US','region':'us','country':'US','state':'NJ','keywords':['new york','new jersey','secaucus','newark','manhattan','nyc']},
    {'id':'sea', 'name':'Seattle, US',          'region':'us','country':'US','state':'WA','keywords':['seattle','quincy','wenatchee','bellevue']},
    {'id':'sfo', 'name':'San Francisco Bay, US','region':'us','country':'US','state':'CA','keywords':['san jose','santa clara','silicon valley','fremont','oakland']},
    {'id':'lax', 'name':'Los Angeles, US',      'region':'us','country':'US','state':'CA','keywords':['los angeles','el segundo','torrance']},
    {'id':'atl', 'name':'Atlanta, US',          'region':'us','country':'US','state':'GA','keywords':['atlanta','norcross','lithia springs']},
    {'id':'bos', 'name':'Boston, US',           'region':'us','country':'US','state':'MA','keywords':['boston','somerville','cambridge']},
    {'id':'den', 'name':'Denver, US',           'region':'us','country':'US','state':'CO','keywords':['denver','englewood','littleton']},
    {'id':'mia', 'name':'Miami, US',            'region':'us','country':'US','state':'FL','keywords':['miami','doral','boca raton','fort lauderdale']},
    {'id':'iah', 'name':'Houston, US',          'region':'us','country':'US','state':'TX','keywords':['houston','katy','sugar land']},
    {'id':'msp', 'name':'Minneapolis, US',      'region':'us','country':'US','state':'MN','keywords':['minneapolis','saint paul','eden prairie']},
    {'id':'slc', 'name':'Salt Lake City, US',   'region':'us','country':'US','state':'UT','keywords':['salt lake city','west jordan','draper']},
    # Canada
    {'id':'yyz', 'name':'Toronto, Canada',      'region':'us','country':'CA','state':'ON','keywords':['toronto','mississauga','markham']},
    {'id':'yvr', 'name':'Vancouver, Canada',    'region':'us','country':'CA','state':'BC','keywords':['vancouver','burnaby','richmond']},
    # EMEA - Europe
    {'id':'lhr', 'name':'London, UK',           'region':'emea','country':'GB','state':None,'keywords':['london','slough','reading','uxbridge']},
    {'id':'fra', 'name':'Frankfurt, Germany',   'region':'emea','country':'DE','state':None,'keywords':['frankfurt','eschborn']},
    {'id':'ams', 'name':'Amsterdam, Netherlands','region':'emea','country':'NL','state':None,'keywords':['amsterdam','amsterdam-southeast','ams']},
    {'id':'par', 'name':'Paris, France',        'region':'emea','country':'FR','state':None,'keywords':['paris','saint-denis','vitry']},
    {'id':'dub', 'name':'Dublin, Ireland',      'region':'emea','country':'IE','state':None,'keywords':['dublin']},
    {'id':'zrh', 'name':'Zurich, Switzerland',  'region':'emea','country':'CH','state':None,'keywords':['zurich','geneva']},
    {'id':'sto', 'name':'Stockholm, Sweden',    'region':'emea','country':'SE','state':None,'keywords':['stockholm']},
    {'id':'mad', 'name':'Madrid, Spain',        'region':'emea','country':'ES','state':None,'keywords':['madrid']},
    {'id':'mil', 'name':'Milan, Italy',         'region':'emea','country':'IT','state':None,'keywords':['milan','milano']},
    {'id':'war', 'name':'Warsaw, Poland',       'region':'emea','country':'PL','state':None,'keywords':['warsaw','wroclaw']},
    {'id':'vie', 'name':'Vienna, Austria',      'region':'emea','country':'AT','state':None,'keywords':['vienna','wien']},
    {'id':'cop', 'name':'Copenhagen, Denmark',  'region':'emea','country':'DK','state':None,'keywords':['copenhagen']},
    {'id':'hel', 'name':'Helsinki, Finland',    'region':'emea','country':'FI','state':None,'keywords':['helsinki']},
    {'id':'osl', 'name':'Oslo, Norway',         'region':'emea','country':'NO','state':None,'keywords':['oslo']},
    {'id':'msc', 'name':'Moscow, Russia',       'region':'emea','country':'RU','state':None,'keywords':['moscow']},
    {'id':'ist', 'name':'Istanbul, Turkey',     'region':'emea','country':'TR','state':None,'keywords':['istanbul']},
    # Middle East & Africa
    {'id':'dxb', 'name':'Dubai, UAE',           'region':'emea','country':'AE','state':None,'keywords':['dubai']},
    {'id':'ruh', 'name':'Riyadh, Saudi Arabia', 'region':'emea','country':'SA','state':None,'keywords':['riyadh','jeddah']},
    {'id':'jnb', 'name':'Johannesburg, South Africa','region':'emea','country':'ZA','state':None,'keywords':['johannesburg','cape town']},
    {'id':'nbo', 'name':'Nairobi, Kenya',       'region':'emea','country':'KE','state':None,'keywords':['nairobi']},
    {'id':'lag', 'name':'Lagos, Nigeria',       'region':'emea','country':'NG','state':None,'keywords':['lagos']},
    {'id':'cai', 'name':'Cairo, Egypt',         'region':'emea','country':'EG','state':None,'keywords':['cairo']},
    # APAC
    {'id':'sin', 'name':'Singapore',            'region':'apac','country':'SG','state':None,'keywords':['singapore']},
    {'id':'tyo', 'name':'Tokyo, Japan',         'region':'apac','country':'JP','state':None,'keywords':['tokyo','osaka']},
    {'id':'syd', 'name':'Sydney, Australia',    'region':'apac','country':'AU','state':None,'keywords':['sydney']},
    {'id':'hkg', 'name':'Hong Kong',            'region':'apac','country':'HK','state':None,'keywords':['hong kong']},
    {'id':'sha', 'name':'Shanghai, China',      'region':'apac','country':'CN','state':None,'keywords':['shanghai']},
    {'id':'pek', 'name':'Beijing, China',       'region':'apac','country':'CN','state':None,'keywords':['beijing']},
    {'id':'bom', 'name':'Mumbai, India',        'region':'apac','country':'IN','state':None,'keywords':['mumbai','pune']},
    {'id':'del', 'name':'Delhi, India',         'region':'apac','country':'IN','state':None,'keywords':['delhi','noida','gurgaon']},
    {'id':'sel', 'name':'Seoul, South Korea',   'region':'apac','country':'KR','state':None,'keywords':['seoul']},
    {'id':'kul', 'name':'Kuala Lumpur, Malaysia','region':'apac','country':'MY','state':None,'keywords':['kuala lumpur','cyberjaya']},
    {'id':'mel', 'name':'Melbourne, Australia', 'region':'apac','country':'AU','state':None,'keywords':['melbourne']},
    {'id':'jak', 'name':'Jakarta, Indonesia',   'region':'apac','country':'ID','state':None,'keywords':['jakarta']},
    {'id':'bkk', 'name':'Bangkok, Thailand',    'region':'apac','country':'TH','state':None,'keywords':['bangkok']},
    {'id':'mnl', 'name':'Manila, Philippines',  'region':'apac','country':'PH','state':None,'keywords':['manila']},
    {'id':'osk', 'name':'Osaka, Japan',         'region':'apac','country':'JP','state':None,'keywords':['osaka']},
    # LATAM
    {'id':'gru', 'name':'São Paulo, Brazil',    'region':'latam','country':'BR','state':None,'keywords':['sao paulo','são paulo','campinas']},
    {'id':'mex', 'name':'Mexico City, Mexico',  'region':'latam','country':'MX','state':None,'keywords':['mexico city','queretaro','monterrey']},
    {'id':'bog', 'name':'Bogotá, Colombia',     'region':'latam','country':'CO','state':None,'keywords':['bogota','bogotá']},
    {'id':'scl', 'name':'Santiago, Chile',      'region':'latam','country':'CL','state':None,'keywords':['santiago']},
    {'id':'bue', 'name':'Buenos Aires, Argentina','region':'latam','country':'AR','state':None,'keywords':['buenos aires']},
    {'id':'lim', 'name':'Lima, Peru',           'region':'latam','country':'PE','state':None,'keywords':['lima']},
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
        cur  = conn.cursor()
        cur.execute("SELECT key, value FROM gdci_config")
        for row in cur.fetchall():
            cfg[row[0]] = row[1]
        cur.close()
    except Exception as e:
        logger.warning("Config load failed: %s", e)
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
        logger.debug("Query error: %s | %s", e, sql[:60])
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return []


# ─── Bulk data loader — 6 queries, all markets ───────────────────────────────

def _load_bulk_data(cfg, conn):
    """
    Load all data needed for all 60 markets in 6 queries.
    Returns dict of {market_id: {dhci, dhpi, dhdi, dhpw, dhri}} raw data.
    """
    cur = conn.cursor()

    op_statuses = [s.strip() for s in cfg.get('fac_status_operational','active,Operational').split(',')]
    pi_statuses = [s.strip() for s in cfg.get('fac_status_pipeline','Under Construction,Construction,Planned,Planning,Announced,announced,planned').split(',')]
    op_ph = ','.join(['%s'] * len(op_statuses))
    pi_ph = ','.join(['%s'] * len(pi_statuses))
    fac   = cfg.get('fac_table', 'facilities')
    txn   = cfg.get('txn_table', 'deals')
    mi    = cfg.get('mi_table',  'market_intelligence')
    pw    = cfg.get('power_table','discovered_power_plants')
    sub   = cfg.get('sub_table', 'substations')
    pcol  = cfg.get('power_city_col', 'market')
    scol  = cfg.get('power_state_col', 'state')

    # ── Q1: Operational facilities by country+state ──
    op_rows = _safe_q(cur, f"""
        SELECT country, state, city, COUNT(*), COALESCE(SUM(power_mw),0)
        FROM {fac} WHERE status IN ({op_ph})
        GROUP BY country, state, city
    """, op_statuses)

    # ── Q2: Pipeline facilities by country+state ──
    pi_rows = _safe_q(cur, f"""
        SELECT country, state, city, COUNT(*), COALESCE(SUM(power_mw),0)
        FROM {fac} WHERE status IN ({pi_ph})
        GROUP BY country, state, city
    """, pi_statuses)

    # ── Q3: Deals last 90 days ──
    deal_rows = _safe_q(cur, f"""
        SELECT LOWER(market), COUNT(*), COALESCE(SUM(mw),0)
        FROM {txn}
        WHERE date >= NOW() - INTERVAL '90 days'
        GROUP BY LOWER(market)
    """)

    # ── Q4: Power plants ──
    pw_rows = []
    if _bool(cfg, 'power_enabled'):
        pw_rows = _safe_q(cur, f"""
            SELECT LOWER({pcol}), LOWER({scol}), COUNT(*), COALESCE(SUM(capacity_mw),0)
            FROM {pw}
            GROUP BY LOWER({pcol}), LOWER({scol})
        """)

    # ── Q5: Substations ──
    sub_rows = []
    if _bool(cfg, 'sub_enabled'):
        sub_rows = _safe_q(cur, f"""
            SELECT LOWER(city), LOWER(country), COUNT(*),
                   COALESCE(SUM(capacity_mva),0), COALESCE(SUM(available_mva),0)
            FROM {sub}
            GROUP BY LOWER(city), LOWER(country)
        """)

    # ── Q6: Market intelligence ──
    mi_rows = []
    if _bool(cfg, 'mi_enabled'):
        mi_rows = _safe_q(cur, f"""
            SELECT LOWER(market), avg_rate_per_kw, recorded_at
            FROM {mi}
            ORDER BY recorded_at DESC
        """)

    cur.close()

    # Index into lookup structures
    op_by_key  = defaultdict(lambda: [0, 0.0])  # (country,state,city) -> [count, mw]
    pi_by_key  = defaultdict(lambda: [0, 0.0])
    deal_by_kw = defaultdict(lambda: [0, 0.0])  # keyword -> [count, mw]
    pw_by_kw   = defaultdict(lambda: [0, 0.0])
    sub_by_kw  = defaultdict(lambda: [0, 0.0, 0.0])  # [count, total_mva, avail_mva]
    mi_by_kw   = {}

    for country, state, city, cnt, mw in op_rows:
        op_by_key[(str(country).upper(), str(state or '').upper(), str(city or '').lower())] = [int(cnt), float(mw)]

    for country, state, city, cnt, mw in pi_rows:
        pi_by_key[(str(country).upper(), str(state or '').upper(), str(city or '').lower())] = [int(cnt), float(mw)]

    for mkt, cnt, mw in deal_rows:
        deal_by_kw[str(mkt or '')] = [int(cnt), float(mw)]

    for city_kw, state_kw, cnt, mw in pw_rows:
        for kw in [str(city_kw or ''), str(state_kw or '')]:
            if kw:
                pw_by_kw[kw][0] += int(cnt)
                pw_by_kw[kw][1] += float(mw)

    for city_kw, country_kw, cnt, tmva, amva in sub_rows:
        kw = str(city_kw or '')
        sub_by_kw[kw][0] += int(cnt)
        sub_by_kw[kw][1] += float(tmva)
        sub_by_kw[kw][2] += float(amva)

    seen_mi = set()
    for mkt, rate, ts in mi_rows:
        if mkt not in seen_mi:
            mi_by_kw[str(mkt or '')] = float(rate) if rate else None
            seen_mi.add(mkt)

    return {
        'op':  op_by_key,
        'pi':  pi_by_key,
        'deal':deal_by_kw,
        'pw':  pw_by_kw,
        'sub': sub_by_kw,
        'mi':  mi_by_kw,
    }


def _match(data_dict, market_keywords):
    """Sum values from data_dict for any key containing a market keyword"""
    matched = defaultdict(float)
    matched_int = defaultdict(int)
    for key, val in data_dict.items():
        for kw in market_keywords:
            if kw and kw in key:
                if isinstance(val, list):
                    for i, v in enumerate(val):
                        if isinstance(v, int):
                            matched_int[i] += v
                        else:
                            matched[i] += v
                break
    result = []
    keys = sorted(set(list(matched.keys()) + list(matched_int.keys())))
    for k in keys:
        result.append(matched_int.get(k, 0) + matched.get(k, 0.0))
    return result


def _match_fac(fac_dict, market):
    """Match facilities using country+state+city keywords"""
    country = market['country'].upper()
    state   = (market.get('state') or '').upper()
    keywords = market['keywords']

    total_cnt = 0
    total_mw  = 0.0
    for (c, s, city), (cnt, mw) in fac_dict.items():
        if c != country:
            continue
        if state and s and s != state:
            continue
        city_str = city.lower()
        if any(kw in city_str for kw in keywords):
            total_cnt += cnt
            total_mw  += mw
    return total_cnt, total_mw


def _score_market_from_bulk(market, bulk):
    """Score a single market from pre-fetched bulk data"""
    mid      = market['id']
    keywords = market['keywords']
    cfg      = _get_config()

    weights = {
        'dhci': float(cfg.get('w_dhci', 30)) / 100,
        'dhri': float(cfg.get('w_dhri', 25)) / 100,
        'dhpi': float(cfg.get('w_dhpi', 20)) / 100,
        'dhdi': float(cfg.get('w_dhdi', 15)) / 100,
        'dhpw': float(cfg.get('w_dhpw', 10)) / 100,
    }

    # DHCI
    op_cnt, op_mw = _match_fac(bulk['op'], market)
    pi_cnt, pi_mw = _match_fac(bulk['pi'], market)
    tot_cnt = op_cnt + pi_cnt
    tot_mw  = op_mw + pi_mw

    if op_mw > 0:
        vac_pct = max(0, min(15, 15 - (pi_mw / max(op_mw, 1)) * 5))
        dhci_val = round(min(100, (1 - vac_pct / 15) * 100), 1)
        dhci_d = {
            'operational_count': op_cnt,
            'operational_mw':    round(op_mw, 1),
            'pipeline_mw':       round(pi_mw, 1),
            'total_count':       tot_cnt,
            'total_mw':          round(tot_mw, 1),
            'vacancy_pct':       round(vac_pct, 2),
        }
    else:
        dhci_val, dhci_d = None, {}

    # DHPI
    if op_mw > 0 and pi_mw >= 0:
        ratio    = pi_mw / max(op_mw, 1)
        dhpi_val = round(min(100, ratio * 50), 1)
        dhpi_d   = {'pipeline_mw': round(pi_mw,1), 'operational_mw': round(op_mw,1), 'pipeline_ratio_pct': round(ratio*100,2)}
    else:
        dhpi_val, dhpi_d = None, {}

    # DHDI — match deals
    deal_cnt = deal_mw = 0
    for key, (cnt, mw) in bulk['deal'].items():
        if any(kw in key for kw in keywords):
            deal_cnt += cnt
            deal_mw  += mw
    if deal_cnt > 0 or deal_mw > 0:
        dhdi_val = round(min(100, (deal_mw / 2000) * 100 + deal_cnt * 5), 1)
        dhdi_d   = {'deal_count_90d': deal_cnt, 'absorbed_mw_90d': round(deal_mw,1)}
    else:
        dhdi_val, dhdi_d = None, {}

    # DHPW — substations first
    dhpw_val, dhpw_d = None, {'source':'none'}
    if _bool(cfg, 'sub_enabled'):
        s_cnt = s_total = s_avail = 0.0
        for key, vals in bulk['sub'].items():
            if any(kw in key for kw in keywords):
                s_cnt   += vals[0]
                s_total += vals[1]
                s_avail += vals[2]
        if s_total > 0:
            headroom = (s_avail / s_total) * 100
            dhpw_val = round(min(100, max(0, (1 - headroom/100)*100)), 1)
            dhpw_d   = {'source':'substations','total_mva':round(s_total,1),'avail_mva':round(s_avail,1),'headroom_pct':round(headroom,1)}

    if dhpw_val is None and _bool(cfg, 'power_enabled'):
        pw_cnt = pw_mw = 0.0
        for key, vals in bulk['pw'].items():
            if any(kw in key for kw in keywords):
                pw_cnt += vals[0]
                pw_mw  += vals[1]
        if pw_mw > 0:
            dhpw_val = round(min(100, (pw_mw / 5000) * 100 + pw_cnt * 0.5), 1)
            dhpw_d   = {'source':'discovered_power_plants','plant_count':int(pw_cnt),'total_mw':round(pw_mw,1)}

    # DHRI — market intelligence
    dhri_val, dhri_d = None, {}
    if _bool(cfg, 'mi_enabled'):
        rate = None
        for key, r in bulk['mi'].items():
            if any(kw in key for kw in keywords):
                rate = r
                break
        if rate is not None:
            baseline = 120.0
            ratio    = rate / baseline
            dhri_val = round(min(100, max(0, (ratio - 0.5) * 133)), 1)
            dhri_d   = {'rate_per_kw': round(rate,2), 'index_value': round(ratio,3)}

    # Composite
    subs = [(dhci_val, weights['dhci']), (dhri_val, weights['dhri']),
            (dhpi_val, weights['dhpi']), (dhdi_val, weights['dhdi']),
            (dhpw_val, weights['dhpw'])]
    active = [(v, w) for v, w in subs if v is not None]
    if active:
        total_w   = sum(w for _, w in active)
        composite = round(sum(v*w for v,w in active) / total_w, 1)
    else:
        composite = None

    return {
        'market_id':       mid,
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
        'connectivity': {},
    }


def _get_all_markets_scored():
    """Load bulk data once, score all 60 markets in memory"""
    global _bulk_cache, _bulk_ts
    now = time.time()

    # Check cache
    if _bulk_cache and now - _bulk_ts < BULK_TTL:
        return _bulk_cache

    cfg  = _get_config()
    conn = get_db()
    bulk = _load_bulk_data(cfg, conn)

    results = []
    for market in MARKETS:
        try:
            r = _score_market_from_bulk(market, bulk)
            results.append(r)
        except Exception as e:
            logger.error("Scoring failed for %s: %s", market['id'], e)

    results.sort(key=lambda x: x.get('composite_score') or 0, reverse=True)
    _bulk_cache = results
    _bulk_ts    = now
    return results


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def init_config_table():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gdci_config (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        for k, v in DEFAULT_CONFIG.items():
            cur.execute(
                "INSERT INTO gdci_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING",
                (k, str(v))
            )
        conn.commit()
        # Auto-enable existing tables
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        for tbl, flag in [('substations','sub_enabled'),('discovered_power_plants','power_enabled'),
                          ('market_intelligence','mi_enabled'),('discovered_transmission_lines','tx_enabled'),
                          ('fiber_routes','fiber_enabled')]:
            if tbl in existing:
                cur.execute("INSERT INTO gdci_config (key,value) VALUES (%s,'true') ON CONFLICT (key) DO UPDATE SET value='true'", (flag,))
        conn.commit()
        cur.close()
        logger.info("GDCI: ✅ Config initialized, markets=%d", len(MARKETS))
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
        logger.error("Markets error: %s", e)
        return jsonify({'error':str(e)}), 500


@index_bp.route('/composite')
def composite():
    try:
        results = _get_all_markets_scored()
        scored  = [m for m in results if m.get('composite_score') is not None]
        if not scored:
            return jsonify({'error':'No market data'}), 503

        avg     = sum(m['composite_score'] for m in scored) / len(scored)
        tot_mw  = sum(m.get('dhci',{}).get('total_mw',0) or 0 for m in scored)
        pi_mw   = sum(m.get('dhpi',{}).get('pipeline_mw',0) or 0 for m in scored)
        op_mw   = sum(m.get('dhci',{}).get('operational_mw',0) or 0 for m in scored)
        vac_pct = None
        if op_mw > 0:
            vac_pct = round(sum((m.get('dhci',{}).get('vacancy_pct') or 0) * (m.get('dhci',{}).get('operational_mw') or 0) for m in scored) / op_mw, 2)

        score = round(avg, 1)
        month = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({
            'issue':              f"{month} Issue",
            'composite_score':    score,
            'composite_label':    _score_label(score),
            'composite_color':    _score_color(score),
            'global_vacancy_pct': vac_pct,
            'total_tracked_mw':   round(tot_mw,1),
            'total_pipeline_mw':  round(pi_mw,1),
            'markets_covered':    len(scored),
            'markets_with_data':  len(scored),
            'citation':           f"According to the DC Hub Global Data Center Index (GDCI), the global composite score reached {score} in {month}, indicating a {_score_label(score).lower()} environment across {len(scored)} tracked markets. Source: DC Hub GDCI, dchub.cloud/index",
            'generated_at':       datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error("Composite error: %s", e)
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
        m = next((x for x in results if x['market_id'] == market_id), None)
        if not m:
            return jsonify({'error':'Unknown market'}), 404
        month = datetime.now(timezone.utc).strftime('%B %Y')
        return jsonify({'market_id':market_id,'market_name':m['market_name'],'score':m['composite_score'],'citation':f"According to the DC Hub Global Data Center Index (GDCI), {m['market_name']} scored {m['composite_score']} ({m['composite_label']}) in {month}. Source: DC Hub GDCI, dchub.cloud/index"})
    except Exception as e:
        return jsonify({'error':str(e)}), 500


# ─── Admin ────────────────────────────────────────────────────────────────────

def _require_admin(cfg):
    key = cfg.get('admin_key','')
    provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
    if key and provided != key:
        return jsonify({'error':'Unauthorized'}), 401
    return None

@index_bp.route('/admin/config', methods=['GET'])
def admin_config_get():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err: return err
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
        for k, v in data.items():
            cur.execute("INSERT INTO gdci_config (key,value,updated_at) VALUES (%s,%s,NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()", (k,str(v)))
        conn.commit()
        cur.close()
        global _config_cache, _config_ts, _bulk_cache, _bulk_ts
        _config_cache = {}; _config_ts = 0; _bulk_cache = None; _bulk_ts = 0
        return jsonify({'updated':list(data.keys()),'count':len(data),'note':'Cache cleared — new config active immediately.'})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@index_bp.route('/admin/refresh', methods=['POST'])
def admin_refresh():
    cfg = _get_config()
    err = _require_admin(cfg)
    if err: return err
    global _config_cache, _config_ts, _bulk_cache, _bulk_ts
    _config_cache = {}; _config_ts = 0; _bulk_cache = None; _bulk_ts = 0
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
        sources = [
            {'name':'Facilities (DHCI+DHPI)','table':cfg.get('fac_table','facilities'),'enabled':True,'exists':cfg.get('fac_table','facilities') in existing,'status':'always on'},
            {'name':'Transactions (DHDI)',    'table':cfg.get('txn_table','deals'),     'enabled':True,'exists':cfg.get('txn_table','deals') in existing,     'status':'always on'},
            {'name':'Market Intelligence (DHRI)','table':cfg.get('mi_table','market_intelligence'),'enabled':_bool(cfg,'mi_enabled'),'exists':cfg.get('mi_table','market_intelligence') in existing},
            {'name':'Power Plants (DHPW)',    'table':cfg.get('power_table','discovered_power_plants'),'enabled':_bool(cfg,'power_enabled'),'exists':cfg.get('power_table','discovered_power_plants') in existing},
            {'name':'Substations (DHPW priority)','table':cfg.get('sub_table','substations'),'enabled':_bool(cfg,'sub_enabled'),'exists':cfg.get('sub_table','substations') in existing},
            {'name':'Transmission Lines',     'table':cfg.get('tx_table','discovered_transmission_lines'),'enabled':_bool(cfg,'tx_enabled'),'exists':cfg.get('tx_table','discovered_transmission_lines') in existing},
            {'name':'Fiber Routes',           'table':cfg.get('fiber_table','fiber_routes'),'enabled':_bool(cfg,'fiber_enabled'),'exists':cfg.get('fiber_table','fiber_routes') in existing},
        ]
        return jsonify({'sources':sources,'markets_defined':len(MARKETS)})
    except Exception as e:
        return jsonify({'error':str(e)}), 500
