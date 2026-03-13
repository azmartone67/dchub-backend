"""
DC Hub Fiber & Connectivity Provider Routes Blueprint
======================================================
Phase 5b: Connectivity Intelligence

Tracks dark fiber and connectivity providers — the carriers that connect
data centers but don't operate facilities themselves.

Tables:
  - fiber_providers: Provider company profiles
  - fiber_provider_markets: Which markets each provider serves
  - fiber_provider_facilities: Which facilities each provider connects to

Endpoints:
  - GET  /api/v1/connectivity/providers — list/search fiber providers
  - GET  /api/v1/connectivity/providers/<name> — provider detail + markets + facilities
  - GET  /api/v1/connectivity/market/<market> — all providers serving a market
  - GET  /api/v1/connectivity/facility/<id> — all providers at a facility
  - POST /api/v1/connectivity/seed — seed the initial provider dataset

Usage in main.py:
    from routes.connectivity_routes import connectivity_bp, init_connectivity_routes
    app.register_blueprint(connectivity_bp)
    init_connectivity_routes(get_pg_connection_fn, return_pg_connection_fn)
"""

import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger('connectivity')

connectivity_bp = Blueprint('connectivity', __name__)

_get_pg = None
_return_pg = None


def init_connectivity_routes(get_pg_fn, return_pg_fn):
    global _get_pg, _return_pg
    _get_pg = get_pg_fn
    _return_pg = return_pg_fn
    _init_tables()
    logger.info("Connectivity intelligence routes initialized")


def _conn():
    if _get_pg is None:
        raise RuntimeError("connectivity_routes not initialized")
    return _get_pg()


def _release(conn):
    if _return_pg and conn:
        try:
            _return_pg(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def _init_tables():
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS fiber_providers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT,
                provider_type TEXT DEFAULT 'dark_fiber',
                headquarters TEXT,
                website TEXT,
                route_miles INTEGER DEFAULT 0,
                markets_served INTEGER DEFAULT 0,
                on_net_buildings INTEGER DEFAULT 0,
                lit_services BOOLEAN DEFAULT FALSE,
                dark_fiber BOOLEAN DEFAULT TRUE,
                wavelength BOOLEAN DEFAULT FALSE,
                ethernet BOOLEAN DEFAULT FALSE,
                ip_transit BOOLEAN DEFAULT FALSE,
                description TEXT,
                founded_year INTEGER,
                key_markets TEXT,
                coverage_region TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS fiber_provider_markets (
                id SERIAL PRIMARY KEY,
                provider_id INTEGER REFERENCES fiber_providers(id),
                market_name TEXT NOT NULL,
                state TEXT,
                country TEXT DEFAULT 'US',
                route_miles INTEGER,
                on_net_buildings INTEGER,
                metro_fiber BOOLEAN DEFAULT TRUE,
                long_haul BOOLEAN DEFAULT FALSE,
                notes TEXT,
                UNIQUE(provider_id, market_name)
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS fiber_provider_facilities (
                id SERIAL PRIMARY KEY,
                provider_id INTEGER REFERENCES fiber_providers(id),
                facility_id INTEGER,
                facility_name TEXT,
                connection_type TEXT DEFAULT 'on-net',
                fiber_count INTEGER,
                notes TEXT,
                UNIQUE(provider_id, facility_id)
            )
        ''')

        cur.execute('CREATE INDEX IF NOT EXISTS idx_fpm_market ON fiber_provider_markets(market_name)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_fpf_facility ON fiber_provider_facilities(facility_id)')

        conn.commit()
        cur.close()
        logger.info("Fiber provider tables initialized")
    except Exception as e:
        logger.error(f"Fiber provider table init error: {e}")
    finally:
        _release(conn)


# =============================================================================
# SEED DATA — Major US dark fiber and connectivity providers
# =============================================================================

SEED_PROVIDERS = [
    {
        'name': 'summitig',
        'display_name': 'SummitIG',
        'provider_type': 'dark_fiber',
        'headquarters': 'Dulles, VA',
        'website': 'https://summitig.com',
        'route_miles': 1200,
        'dark_fiber': True, 'lit_services': False, 'wavelength': False,
        'description': 'Dense metro dark fiber in key data center markets. 100% underground, high-count fiber cables.',
        'founded_year': 2007,
        'key_markets': 'Northern Virginia, Columbus OH, Chicago, Salt Lake City, Phoenix',
        'coverage_region': 'US East, Midwest',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 750, 'metro_fiber': True},
            {'market_name': 'Columbus', 'state': 'OH', 'route_miles': 150, 'metro_fiber': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 100, 'metro_fiber': True},
            {'market_name': 'Salt Lake City', 'state': 'UT', 'route_miles': 50, 'metro_fiber': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 50, 'metro_fiber': True},
            {'market_name': 'Richmond', 'state': 'VA', 'route_miles': 175, 'long_haul': True},
        ]
    },
    {
        'name': 'bandwidth_ig',
        'display_name': 'Bandwidth IG',
        'provider_type': 'dark_fiber',
        'headquarters': 'Houston, TX',
        'website': 'https://bandwidthig.com',
        'route_miles': 2500,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True,
        'description': 'Metro and long-haul dark fiber provider serving major Texas and Southeast markets.',
        'founded_year': 2014,
        'key_markets': 'Houston, Dallas, San Antonio, Austin',
        'coverage_region': 'US South, Texas',
        'markets': [
            {'market_name': 'Houston', 'state': 'TX', 'route_miles': 800, 'metro_fiber': True},
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 600, 'metro_fiber': True},
            {'market_name': 'San Antonio', 'state': 'TX', 'route_miles': 400, 'metro_fiber': True},
            {'market_name': 'Austin', 'state': 'TX', 'route_miles': 300, 'metro_fiber': True},
        ]
    },
    {
        'name': 'fiberlight',
        'display_name': 'FiberLight',
        'provider_type': 'dark_fiber',
        'headquarters': 'Plano, TX',
        'website': 'https://fiberlight.com',
        'route_miles': 14000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True,
        'description': 'Fiber infrastructure provider with 14,000+ route miles. Custom dark fiber and lit services for enterprises and data centers.',
        'founded_year': 2002,
        'key_markets': 'Dallas, Northern Virginia, Atlanta, Baltimore, Austin, Phoenix, Denver',
        'coverage_region': 'US National',
        'markets': [
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 2500, 'metro_fiber': True},
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 1500, 'metro_fiber': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 1200, 'metro_fiber': True},
            {'market_name': 'Baltimore', 'state': 'MD', 'route_miles': 800, 'metro_fiber': True},
            {'market_name': 'Austin', 'state': 'TX', 'route_miles': 700, 'metro_fiber': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 600, 'metro_fiber': True},
            {'market_name': 'Denver', 'state': 'CO', 'route_miles': 500, 'metro_fiber': True},
            {'market_name': 'San Antonio', 'state': 'TX', 'route_miles': 500, 'metro_fiber': True},
            {'market_name': 'Houston', 'state': 'TX', 'route_miles': 400, 'metro_fiber': True},
        ]
    },
    {
        'name': 'firstlight',
        'display_name': 'FirstLight Fiber',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Albany, NY',
        'website': 'https://firstlight.net',
        'route_miles': 30000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True, 'ip_transit': True,
        'description': 'Fiber-optic and data services provider across the Northeast US with 30,000+ route miles. Operates ColoSpace data centers.',
        'founded_year': 2014,
        'key_markets': 'Boston, New York, Albany, Portland ME, Burlington VT',
        'coverage_region': 'US Northeast, New England',
        'markets': [
            {'market_name': 'Boston', 'state': 'MA', 'route_miles': 3000, 'metro_fiber': True},
            {'market_name': 'New York', 'state': 'NY', 'route_miles': 2500, 'metro_fiber': True},
            {'market_name': 'Albany', 'state': 'NY', 'route_miles': 2000, 'metro_fiber': True},
            {'market_name': 'Portland', 'state': 'ME', 'route_miles': 1500, 'metro_fiber': True},
            {'market_name': 'Burlington', 'state': 'VT', 'route_miles': 800, 'metro_fiber': True},
            {'market_name': 'Hartford', 'state': 'CT', 'route_miles': 1000, 'metro_fiber': True},
        ]
    },
    {
        'name': 'arcadian',
        'display_name': 'Arcadian Infracom',
        'provider_type': 'dark_fiber',
        'headquarters': 'Denver, CO',
        'website': 'https://arcadianinfracom.com',
        'route_miles': 3000,
        'dark_fiber': True, 'lit_services': False, 'wavelength': False,
        'description': 'Long-haul and metro dark fiber focused on connecting data centers and hyperscale campuses. New builds in high-growth corridors.',
        'founded_year': 2020,
        'key_markets': 'Denver, Phoenix, Salt Lake City, Las Vegas',
        'coverage_region': 'US West, Mountain',
        'markets': [
            {'market_name': 'Denver', 'state': 'CO', 'route_miles': 600, 'metro_fiber': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 500, 'metro_fiber': True},
            {'market_name': 'Salt Lake City', 'state': 'UT', 'route_miles': 400, 'metro_fiber': True},
            {'market_name': 'Las Vegas', 'state': 'NV', 'route_miles': 300, 'metro_fiber': True},
        ]
    },
    {
        'name': 'vivacity',
        'display_name': 'Vivacity Networks',
        'provider_type': 'dark_fiber',
        'headquarters': 'Ashburn, VA',
        'website': 'https://vivacitynetworks.com',
        'route_miles': 500,
        'dark_fiber': True, 'lit_services': False, 'wavelength': False,
        'description': 'Metro dark fiber provider in Northern Virginia data center corridor.',
        'founded_year': 2018,
        'key_markets': 'Northern Virginia',
        'coverage_region': 'Northern Virginia',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 500, 'metro_fiber': True},
        ]
    },
    {
        'name': 'uniti_fiber',
        'display_name': 'Uniti Fiber (Wholesale)',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Little Rock, AR',
        'website': 'https://unitiwholesale.com',
        'route_miles': 140000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True,
        'description': 'One of the largest US fiber networks with 140,000+ route miles. Beach Route Alliance, Heartland Express, Southeast Express, CanAm2 corridors.',
        'founded_year': 2015,
        'key_markets': 'Southeast US, Dallas, Jacksonville, Miami, Memphis, Tulsa, Oklahoma City',
        'coverage_region': 'US National (Southeast focus)',
        'markets': [
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 5000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Jacksonville', 'state': 'FL', 'route_miles': 2000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Miami', 'state': 'FL', 'route_miles': 1500, 'metro_fiber': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 2500, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Memphis', 'state': 'TN', 'route_miles': 1000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 1500, 'long_haul': True},
            {'market_name': 'New York', 'state': 'NY', 'route_miles': 1000, 'long_haul': True},
            {'market_name': 'New Orleans', 'state': 'LA', 'route_miles': 800, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Tampa', 'state': 'FL', 'route_miles': 1200, 'metro_fiber': True, 'long_haul': True},
        ]
    },
    {
        'name': 'windstream',
        'display_name': 'Windstream Wholesale',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Little Rock, AR',
        'website': 'https://windstreamwholesale.com',
        'route_miles': 170000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True, 'ip_transit': True,
        'description': 'Nationwide fiber network with 170,000+ route miles. Enterprise and wholesale dark fiber, wavelength, and Ethernet services.',
        'founded_year': 2006,
        'key_markets': 'Nationwide US — 48 states',
        'coverage_region': 'US National',
        'markets': [
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 8000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 5000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 4000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 3000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 2000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Denver', 'state': 'CO', 'route_miles': 2500, 'metro_fiber': True, 'long_haul': True},
        ]
    },
    {
        'name': 'zayo',
        'display_name': 'Zayo Group',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Boulder, CO',
        'website': 'https://zayo.com',
        'route_miles': 141000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True, 'ip_transit': True,
        'description': 'Tier-1 fiber infrastructure with 141,000+ route miles across North America and Europe. Dark fiber, wavelength, Ethernet, IP.',
        'founded_year': 2007,
        'key_markets': 'Nationwide US + Europe — all major metros',
        'coverage_region': 'US National, Europe',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 5000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'New York', 'state': 'NY', 'route_miles': 4000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 4000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 3500, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Los Angeles', 'state': 'CA', 'route_miles': 3000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Denver', 'state': 'CO', 'route_miles': 3000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 2000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 2500, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Silicon Valley', 'state': 'CA', 'route_miles': 2500, 'metro_fiber': True},
            {'market_name': 'Seattle', 'state': 'WA', 'route_miles': 2000, 'metro_fiber': True, 'long_haul': True},
        ]
    },
    {
        'name': 'crown_castle',
        'display_name': 'Crown Castle Fiber',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Houston, TX',
        'website': 'https://crowncastle.com/fiber',
        'route_miles': 85000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True,
        'description': 'Metro and enterprise fiber with 85,000+ route miles across top US markets. Small cell and edge connectivity.',
        'founded_year': 1994,
        'key_markets': 'Top 30 US metros',
        'coverage_region': 'US National (metro focus)',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 4000, 'metro_fiber': True},
            {'market_name': 'New York', 'state': 'NY', 'route_miles': 5000, 'metro_fiber': True},
            {'market_name': 'Los Angeles', 'state': 'CA', 'route_miles': 4000, 'metro_fiber': True},
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 3000, 'metro_fiber': True},
            {'market_name': 'Houston', 'state': 'TX', 'route_miles': 3000, 'metro_fiber': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 3000, 'metro_fiber': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 2000, 'metro_fiber': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 2000, 'metro_fiber': True},
        ]
    },
    {
        'name': 'lumen',
        'display_name': 'Lumen Technologies',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Monroe, LA',
        'website': 'https://lumen.com',
        'route_miles': 450000,
        'dark_fiber': True, 'lit_services': True, 'wavelength': True, 'ethernet': True, 'ip_transit': True,
        'description': 'One of the largest global fiber networks with 450,000+ route miles. Enterprise, wholesale, and hyperscale connectivity.',
        'founded_year': 1930,
        'key_markets': 'Global — all major US and international metros',
        'coverage_region': 'Global',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 8000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 6000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 5000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 4000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Denver', 'state': 'CO', 'route_miles': 4000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'Atlanta', 'state': 'GA', 'route_miles': 3500, 'metro_fiber': True, 'long_haul': True},
        ]
    },
    {
        'name': 'cogent',
        'display_name': 'Cogent Communications',
        'provider_type': 'fiber_carrier',
        'headquarters': 'Washington, DC',
        'website': 'https://cogentco.com',
        'route_miles': 113000,
        'dark_fiber': True, 'lit_services': True, 'ip_transit': True, 'ethernet': True,
        'description': 'Global internet services provider with 113,000+ route miles. Transit, colocation, and dark fiber services.',
        'founded_year': 1999,
        'key_markets': 'Global — 54 countries, 227 markets',
        'coverage_region': 'Global',
        'markets': [
            {'market_name': 'Northern Virginia', 'state': 'VA', 'route_miles': 3000, 'metro_fiber': True, 'long_haul': True},
            {'market_name': 'New York', 'state': 'NY', 'route_miles': 2500, 'metro_fiber': True},
            {'market_name': 'Chicago', 'state': 'IL', 'route_miles': 2000, 'metro_fiber': True},
            {'market_name': 'Dallas', 'state': 'TX', 'route_miles': 2000, 'metro_fiber': True},
        ]
    },
    {
        'name': 'srp_telecom',
        'display_name': 'SRP Telecom',
        'provider_type': 'dark_fiber',
        'headquarters': 'Phoenix, AZ',
        'website': 'https://srptelecom.com',
        'route_miles': 1800,
        'dark_fiber': True, 'lit_services': False,
        'description': 'Dark fiber routed along SRP high-voltage electric system in Greater Phoenix. 1,800 route miles across 15 cities. 90% of network is distinct from competition via private rights-of-way.',
        'founded_year': 2000,
        'key_markets': 'Phoenix metro',
        'coverage_region': 'Greater Phoenix, AZ',
        'markets': [
            {'market_name': 'Phoenix', 'state': 'AZ', 'route_miles': 1800, 'metro_fiber': True, 'notes': 'Routed along SRP high-voltage electric system, private rights-of-way'},
        ]
    },
]


# =============================================================================
# ROUTES
# =============================================================================

@connectivity_bp.route('/api/v1/connectivity/providers', methods=['GET'])
def list_providers():
    """List all fiber/connectivity providers. Filter by type, market, or service."""
    provider_type = request.args.get('type', '').strip()
    market = request.args.get('market', '').strip()
    service = request.args.get('service', '').strip()  # dark_fiber, lit, wavelength, ethernet
    limit = min(request.args.get('limit', 50, type=int), 200)

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        conditions = []
        params = []

        if provider_type:
            conditions.append("provider_type = %s")
            params.append(provider_type)
        if service == 'dark_fiber':
            conditions.append("dark_fiber = TRUE")
        elif service == 'lit':
            conditions.append("lit_services = TRUE")
        elif service == 'wavelength':
            conditions.append("wavelength = TRUE")

        if market:
            conditions.append("id IN (SELECT provider_id FROM fiber_provider_markets WHERE market_name ILIKE %s)")
            params.append(f"%{market}%")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        cur.execute(f"""
            SELECT id, name, display_name, provider_type, headquarters, website,
                   route_miles, markets_served, on_net_buildings,
                   dark_fiber, lit_services, wavelength, ethernet, ip_transit,
                   description, key_markets, coverage_region
            FROM fiber_providers
            {where}
            ORDER BY route_miles DESC NULLS LAST
            LIMIT %s
        """, params)

        cols = [d[0] for d in cur.description]
        providers = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()

        return jsonify({
            'success': True,
            'count': len(providers),
            'data': providers,
        })
    except Exception as e:
        logger.error(f"List providers error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


@connectivity_bp.route('/api/v1/connectivity/providers/<path:provider_name>', methods=['GET'])
def provider_detail(provider_name):
    """Full provider profile with markets served and connected facilities."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT * FROM fiber_providers WHERE name = %s OR display_name ILIKE %s
        """, (provider_name, f"%{provider_name}%"))
        row = cur.fetchone()
        if not row:
            cur.close()
            return jsonify({'success': False, 'error': f'Provider "{provider_name}" not found'}), 404

        cols = [d[0] for d in cur.description]
        provider = dict(zip(cols, row))
        provider_id = provider['id']

        # Timestamps to string
        for ts in ('created_at', 'updated_at'):
            if provider.get(ts):
                provider[ts] = str(provider[ts])

        # Markets
        cur.execute("""
            SELECT market_name, state, country, route_miles, on_net_buildings,
                   metro_fiber, long_haul, notes
            FROM fiber_provider_markets
            WHERE provider_id = %s
            ORDER BY route_miles DESC NULLS LAST
        """, (provider_id,))
        mcols = [d[0] for d in cur.description]
        provider['markets'] = [dict(zip(mcols, r)) for r in cur.fetchall()]

        # Connected facilities
        cur.execute("""
            SELECT fpf.facility_name, fpf.connection_type, fpf.fiber_count, fpf.notes,
                   df.city, df.state, df.market, df.power_mw, df.provider AS facility_operator
            FROM fiber_provider_facilities fpf
            LEFT JOIN discovered_facilities df ON fpf.facility_id = df.id
            WHERE fpf.provider_id = %s
            ORDER BY fpf.facility_name
        """, (provider_id,))
        fcols = [d[0] for d in cur.description]
        facilities = []
        for r in cur.fetchall():
            f = dict(zip(fcols, r))
            if f.get('power_mw'):
                f['power_mw'] = float(f['power_mw'])
            facilities.append(f)
        provider['connected_facilities'] = facilities

        cur.close()

        return jsonify({'success': True, 'data': provider})
    except Exception as e:
        logger.error(f"Provider detail error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


@connectivity_bp.route('/api/v1/connectivity/market/<path:market_name>', methods=['GET'])
def market_connectivity(market_name):
    """All fiber/connectivity providers serving a specific market."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT fp.display_name, fp.provider_type, fp.website, fp.route_miles AS total_route_miles,
                   fp.dark_fiber, fp.lit_services, fp.wavelength, fp.ethernet, fp.ip_transit,
                   fpm.route_miles AS market_route_miles, fpm.on_net_buildings,
                   fpm.metro_fiber, fpm.long_haul, fpm.notes
            FROM fiber_provider_markets fpm
            JOIN fiber_providers fp ON fpm.provider_id = fp.id
            WHERE fpm.market_name ILIKE %s
            ORDER BY fpm.route_miles DESC NULLS LAST
        """, (f"%{market_name}%",))

        cols = [d[0] for d in cur.description]
        providers = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()

        return jsonify({
            'success': True,
            'market': market_name,
            'provider_count': len(providers),
            'data': providers,
        })
    except Exception as e:
        logger.error(f"Market connectivity error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


@connectivity_bp.route('/api/v1/connectivity/facility/<int:facility_id>', methods=['GET'])
def facility_connectivity(facility_id):
    """All fiber/connectivity providers connected to a specific facility."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT fp.display_name, fp.provider_type, fp.website,
                   fp.dark_fiber, fp.lit_services, fp.wavelength, fp.ethernet,
                   fpf.connection_type, fpf.fiber_count, fpf.notes
            FROM fiber_provider_facilities fpf
            JOIN fiber_providers fp ON fpf.provider_id = fp.id
            WHERE fpf.facility_id = %s
            ORDER BY fp.display_name
        """, (facility_id,))

        cols = [d[0] for d in cur.description]
        providers = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()

        return jsonify({
            'success': True,
            'facility_id': facility_id,
            'provider_count': len(providers),
            'data': providers,
        })
    except Exception as e:
        logger.error(f"Facility connectivity error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


@connectivity_bp.route('/api/v1/connectivity/seed', methods=['POST'])
def seed_providers():
    """Seed the fiber provider database with known providers and market data."""
    internal_key = request.headers.get('X-Internal-Key')
    if internal_key not in ('dchub-internal-2024', 'dchub-internal-sync-2026'):
        return jsonify({'error': 'authentication_required'}), 401

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        providers_added = 0
        markets_added = 0

        for p in SEED_PROVIDERS:
            markets = p.pop('markets', [])

            try:
                cur.execute("""
                    INSERT INTO fiber_providers
                    (name, display_name, provider_type, headquarters, website,
                     route_miles, dark_fiber, lit_services, wavelength, ethernet, ip_transit,
                     description, founded_year, key_markets, coverage_region, markets_served)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        route_miles = EXCLUDED.route_miles,
                        markets_served = EXCLUDED.markets_served,
                        updated_at = NOW()
                    RETURNING id
                """, (
                    p['name'], p['display_name'], p.get('provider_type', 'dark_fiber'),
                    p.get('headquarters'), p.get('website'),
                    p.get('route_miles', 0),
                    p.get('dark_fiber', False), p.get('lit_services', False),
                    p.get('wavelength', False), p.get('ethernet', False), p.get('ip_transit', False),
                    p.get('description'), p.get('founded_year'),
                    p.get('key_markets'), p.get('coverage_region'),
                    len(markets)
                ))
                provider_id = cur.fetchone()[0]
                providers_added += 1

                for m in markets:
                    cur.execute("""
                        INSERT INTO fiber_provider_markets
                        (provider_id, market_name, state, route_miles, metro_fiber, long_haul, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (provider_id, market_name) DO UPDATE SET
                            route_miles = EXCLUDED.route_miles
                    """, (
                        provider_id, m['market_name'], m.get('state'),
                        m.get('route_miles', 0), m.get('metro_fiber', True),
                        m.get('long_haul', False), m.get('notes')
                    ))
                    markets_added += 1

            except Exception as e:
                logger.error(f"Seed error for {p.get('name')}: {e}")

        conn.commit()
        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'providers_added': providers_added,
                'markets_added': markets_added,
            }
        })
    except Exception as e:
        logger.error(f"Seed error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# ---------------------------------------------------------------------------
print("   🔌 Connectivity intelligence routes loaded:")
print("      /api/v1/connectivity/providers")
print("      /api/v1/connectivity/providers/<name>")
print("      /api/v1/connectivity/market/<market>")
print("      /api/v1/connectivity/facility/<id>")
print("      /api/v1/connectivity/seed (POST)")
