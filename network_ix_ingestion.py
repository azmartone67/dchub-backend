"""
DC Hub — Network, Internet Exchange & Campus Mapper (PeeringDB)
══════════════════════════════════════════════════════════════
Ingests autonomous systems (networks), internet exchanges, and campus data
from PeeringDB API with cross-facility relationships for connectivity mapping.

Sources:
  - PeeringDB API v0: /api/net, /api/netfac, /api/ix, /api/ixfac, /api/campus

Tables created:
  - pdb_networks         (autonomous systems, ASN, routing info)
  - pdb_network_facilities (network ↔ facility cross-ref)
  - pdb_ix               (internet exchange points)
  - pdb_ix_facilities    (IX ↔ facility cross-ref)
  - pdb_campus           (campus entities)

Run: POST /api/jobs/network-sync, /api/jobs/ix-sync, /api/jobs/campus-sync
     POST /api/jobs/peeringdb-full-sync
Query: GET /api/v1/networks/summary, /api/v1/ix/summary, /api/v1/connectivity/<fac_id>

v1.0 — March 2026
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests

logger = logging.getLogger('dchub-networks')

# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────
PEERINGDB_BASE = "https://www.peeringdb.com/api"
PEERINGDB_NETWORKS = f"{PEERINGDB_BASE}/net"
PEERINGDB_NETWORK_FAC = f"{PEERINGDB_BASE}/netfac"
PEERINGDB_IX = f"{PEERINGDB_BASE}/ix"
PEERINGDB_IX_FAC = f"{PEERINGDB_BASE}/ixfac"
PEERINGDB_CAMPUS = f"{PEERINGDB_BASE}/campus"

# Rate limiting configuration
# PeeringDB: 20 req/min anonymous, higher with API key
RATE_LIMIT_DELAY = 3  # seconds between requests (20/min = 1 req per 3 sec)
RATE_LIMIT_DELAY_WITH_KEY = 1  # seconds with API key
RATE_LIMIT_RETRIES = [5, 10, 20]  # exponential backoff seconds on 429

PEERINGDB_HEADERS = {
    'User-Agent': 'DCHub-Intelligence/1.0 (dchub.cloud; data-center-research)',
    'Accept': 'application/json',
}


# ─────────────────────────────────────────────────────────────
# TABLE CREATION
# ─────────────────────────────────────────────────────────────
def init_network_ix_tables(get_db):
    """Create network and IX tables in PostgreSQL (Neon). All IDs as TEXT."""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        # 1. Networks (autonomous systems from PeeringDB)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pdb_networks (
                id TEXT PRIMARY KEY,
                org_id TEXT,
                asn INTEGER,
                name TEXT NOT NULL,
                name_long TEXT,
                aka TEXT,
                website TEXT,
                looking_glass TEXT,
                info_traffic TEXT,
                info_ratio TEXT,
                info_scope TEXT,
                info_types TEXT,
                info_prefixes4 INTEGER,
                info_prefixes6 INTEGER,
                info_ipv6 BOOLEAN,
                info_multicast BOOLEAN,
                info_unicast BOOLEAN,
                irr_as_set TEXT,
                notes TEXT,
                policy_url TEXT,
                policy_general TEXT,
                policy_locations TEXT,
                policy_ratio BOOLEAN,
                policy_contracts TEXT,
                status TEXT DEFAULT 'ok',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Network ↔ Facility cross-reference
        c.execute("""
            CREATE TABLE IF NOT EXISTS pdb_network_facilities (
                id TEXT PRIMARY KEY,
                net_id TEXT NOT NULL,
                fac_id TEXT NOT NULL,
                local_asn TEXT,
                city TEXT,
                country TEXT,
                avail_sonet BOOLEAN,
                avail_ethernet BOOLEAN,
                avail_atm BOOLEAN,
                status TEXT DEFAULT 'ok',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(net_id, fac_id)
            )
        """)

        # 3. Internet Exchanges
        c.execute("""
            CREATE TABLE IF NOT EXISTS pdb_ix (
                id TEXT PRIMARY KEY,
                org_id TEXT,
                name TEXT NOT NULL,
                name_long TEXT,
                aka TEXT,
                city TEXT,
                country TEXT,
                region_continent TEXT,
                media TEXT,
                proto_unicast TEXT,
                proto_multicast BOOLEAN,
                proto_ipv6 TEXT,
                website TEXT,
                url_stats TEXT,
                notes TEXT,
                status TEXT DEFAULT 'ok',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. IX ↔ Facility cross-reference
        c.execute("""
            CREATE TABLE IF NOT EXISTS pdb_ix_facilities (
                id TEXT PRIMARY KEY,
                ix_id TEXT NOT NULL,
                fac_id TEXT NOT NULL,
                name TEXT,
                city TEXT,
                country TEXT,
                status TEXT DEFAULT 'ok',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ix_id, fac_id)
            )
        """)

        # 5. Campus entities
        c.execute("""
            CREATE TABLE IF NOT EXISTS pdb_campus (
                id TEXT PRIMARY KEY,
                org_id TEXT,
                name TEXT NOT NULL,
                name_long TEXT,
                aka TEXT,
                website TEXT,
                notes TEXT,
                status TEXT DEFAULT 'ok',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for fast lookups
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_networks_asn ON pdb_networks(asn)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_networks_org ON pdb_networks(org_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_networks_name ON pdb_networks(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_networks_status ON pdb_networks(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_netfac_net ON pdb_network_facilities(net_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_netfac_fac ON pdb_network_facilities(fac_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_netfac_country ON pdb_network_facilities(country)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_ix_name ON pdb_ix(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_ix_org ON pdb_ix(org_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_ix_country ON pdb_ix(country)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_ixfac_ix ON pdb_ix_facilities(ix_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_ixfac_fac ON pdb_ix_facilities(fac_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_campus_org ON pdb_campus(org_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pdb_campus_name ON pdb_campus(name)")

        conn.commit()
        logger.info("✅ Network & IX tables initialized")
    except Exception as e:
        logger.warning(f"Network/IX tables init: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# PEERINGDB DATA FETCHING
# ─────────────────────────────────────────────────────────────
def _pdb_fetch_paginated(endpoint: str, timeout: int = 60) -> Optional[List[Dict]]:
    """
    Fetch from PeeringDB API with pagination, rate limiting, and retry.
    Returns list of all records from all pages, or None on failure.
    """
    try:
        # Use API key if available (higher rate limits)
        headers = dict(PEERINGDB_HEADERS)
        api_key = os.environ.get('PEERINGDB_API_KEY', '')
        has_api_key = bool(api_key)
        if api_key:
            headers['Authorization'] = f'Api-Key {api_key}'

        rate_delay = RATE_LIMIT_DELAY_WITH_KEY if has_api_key else RATE_LIMIT_DELAY
        all_records = []
        skip = 0
        limit = 250
        page = 0
        endpoint_name = endpoint.split('/')[-1]

        while True:
            params = {'limit': limit, 'skip': skip}
            retry_count = 0
            max_retries = len(RATE_LIMIT_RETRIES)

            while retry_count <= max_retries:
                try:
                    time.sleep(rate_delay)  # Rate limit: space out requests
                    resp = requests.get(
                        endpoint,
                        headers=headers,
                        params=params,
                        timeout=timeout,
                    )

                    if resp.status_code == 429:
                        if retry_count < max_retries:
                            wait_time = RATE_LIMIT_RETRIES[retry_count]
                            logger.warning(
                                f"Rate limit (429) on {endpoint_name} page {page}. "
                                f"Retrying in {wait_time}s..."
                            )
                            time.sleep(wait_time)
                            retry_count += 1
                            continue
                        else:
                            logger.error(
                                f"Rate limit (429) on {endpoint_name} — exceeded retries"
                            )
                            return None

                    resp.raise_for_status()
                    data = resp.json()

                    # PeeringDB returns { "data": [...] } or just [...]
                    records = data.get('data', data) if isinstance(data, dict) else data
                    if not records:
                        break

                    all_records.extend(records)
                    page += 1
                    logger.info(f"Fetched {len(records)} {endpoint_name} (page {page}, total so far: {len(all_records)})")

                    # If we got fewer records than limit, we're done
                    if len(records) < limit:
                        logger.info(f"✅ Complete: {len(all_records)} {endpoint_name} total")
                        return all_records

                    skip += limit
                    break

                except requests.RequestException as e:
                    if retry_count < max_retries:
                        wait_time = RATE_LIMIT_RETRIES[retry_count]
                        logger.warning(f"Request error on {endpoint_name}: {e}. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        retry_count += 1
                    else:
                        logger.error(f"Request failed on {endpoint_name} after retries: {e}")
                        return None

        return all_records

    except Exception as e:
        logger.error(f"PeeringDB pagination error ({endpoint}): {e}")
        return None


# ─────────────────────────────────────────────────────────────
# NETWORK INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_networks(get_db) -> Dict[str, Any]:
    """Fetch all networks (autonomous systems) from PeeringDB."""
    networks = _pdb_fetch_paginated(PEERINGDB_NETWORKS)
    if networks is None:
        return {'success': False, 'error': 'Failed to fetch network data from PeeringDB'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for net in networks:
            try:
                net_id = str(net.get('id', ''))
                if not net_id:
                    continue

                # Parse info_types (array to comma-separated string)
                info_types = net.get('info_types', [])
                info_types_str = ','.join(info_types) if isinstance(info_types, list) else str(info_types)

                c.execute("""
                    INSERT INTO pdb_networks
                        (id, org_id, asn, name, name_long, aka, website, looking_glass,
                         info_traffic, info_ratio, info_scope, info_types, info_prefixes4,
                         info_prefixes6, info_ipv6, info_multicast, info_unicast,
                         irr_as_set, notes, policy_url, policy_general, policy_locations,
                         policy_ratio, policy_contracts, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        org_id = EXCLUDED.org_id,
                        asn = EXCLUDED.asn,
                        name = EXCLUDED.name,
                        name_long = EXCLUDED.name_long,
                        aka = EXCLUDED.aka,
                        website = EXCLUDED.website,
                        looking_glass = EXCLUDED.looking_glass,
                        info_traffic = EXCLUDED.info_traffic,
                        info_ratio = EXCLUDED.info_ratio,
                        info_scope = EXCLUDED.info_scope,
                        info_types = EXCLUDED.info_types,
                        info_prefixes4 = EXCLUDED.info_prefixes4,
                        info_prefixes6 = EXCLUDED.info_prefixes6,
                        info_ipv6 = EXCLUDED.info_ipv6,
                        info_multicast = EXCLUDED.info_multicast,
                        info_unicast = EXCLUDED.info_unicast,
                        irr_as_set = EXCLUDED.irr_as_set,
                        notes = EXCLUDED.notes,
                        policy_url = EXCLUDED.policy_url,
                        policy_general = EXCLUDED.policy_general,
                        policy_locations = EXCLUDED.policy_locations,
                        policy_ratio = EXCLUDED.policy_ratio,
                        policy_contracts = EXCLUDED.policy_contracts,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        synced_at = CURRENT_TIMESTAMP
                """, (
                    net_id, str(net.get('org_id', '')), net.get('asn'),
                    net.get('name', ''), net.get('name_long', ''), net.get('aka', ''),
                    net.get('website', ''), net.get('looking_glass', ''),
                    net.get('info_traffic', ''), net.get('info_ratio', ''),
                    net.get('info_scope', ''), info_types_str,
                    net.get('info_prefixes4'), net.get('info_prefixes6'),
                    net.get('info_ipv6'), net.get('info_multicast'), net.get('info_unicast'),
                    net.get('irr_as_set', ''), net.get('notes', ''),
                    net.get('policy_url', ''), net.get('policy_general', ''),
                    net.get('policy_locations', ''), net.get('policy_ratio'),
                    net.get('policy_contracts', ''), net.get('status', 'ok'),
                    net.get('created'), net.get('updated'),
                ))

                upserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"Network upsert error: {e}")

        conn.commit()
        logger.info(f"✅ Networks: {upserted} upserted, {errors} errors")

    except Exception as e:
        logger.error(f"Network ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'endpoint': 'networks',
        'total_fetched': len(networks),
        'upserted': upserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# NETWORK ↔ FACILITY INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_network_facilities(get_db) -> Dict[str, Any]:
    """Fetch network-facility relationships from PeeringDB /netfac."""
    netfacs = _pdb_fetch_paginated(PEERINGDB_NETWORK_FAC)
    if netfacs is None:
        return {'success': False, 'error': 'Failed to fetch network-facility data'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for nfac in netfacs:
            try:
                nfac_id = str(nfac.get('id', ''))
                net_id = str(nfac.get('net_id', ''))
                fac_id = str(nfac.get('fac_id', ''))

                if not nfac_id or not net_id or not fac_id:
                    continue

                c.execute("""
                    INSERT INTO pdb_network_facilities
                        (id, net_id, fac_id, local_asn, city, country,
                         avail_sonet, avail_ethernet, avail_atm, status,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        net_id = EXCLUDED.net_id,
                        fac_id = EXCLUDED.fac_id,
                        local_asn = EXCLUDED.local_asn,
                        city = EXCLUDED.city,
                        country = EXCLUDED.country,
                        avail_sonet = EXCLUDED.avail_sonet,
                        avail_ethernet = EXCLUDED.avail_ethernet,
                        avail_atm = EXCLUDED.avail_atm,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        synced_at = CURRENT_TIMESTAMP
                """, (
                    nfac_id, net_id, fac_id, str(nfac.get('local_asn', '')),
                    nfac.get('city', ''), nfac.get('country', ''),
                    nfac.get('avail_sonet'), nfac.get('avail_ethernet'), nfac.get('avail_atm'),
                    nfac.get('status', 'ok'), nfac.get('created'), nfac.get('updated'),
                ))

                upserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"NetworkFac upsert error: {e}")

        conn.commit()
        logger.info(f"✅ Network-Facilities: {upserted} upserted, {errors} errors")

    except Exception as e:
        logger.error(f"Network-facility ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'endpoint': 'network_facilities',
        'total_fetched': len(netfacs),
        'upserted': upserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# INTERNET EXCHANGE INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_ix(get_db) -> Dict[str, Any]:
    """Fetch all internet exchanges from PeeringDB."""
    ixes = _pdb_fetch_paginated(PEERINGDB_IX)
    if ixes is None:
        return {'success': False, 'error': 'Failed to fetch internet exchange data'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for ix in ixes:
            try:
                ix_id = str(ix.get('id', ''))
                if not ix_id:
                    continue

                c.execute("""
                    INSERT INTO pdb_ix
                        (id, org_id, name, name_long, aka, city, country,
                         region_continent, media, proto_unicast, proto_multicast,
                         proto_ipv6, website, url_stats, notes, status,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        org_id = EXCLUDED.org_id,
                        name = EXCLUDED.name,
                        name_long = EXCLUDED.name_long,
                        aka = EXCLUDED.aka,
                        city = EXCLUDED.city,
                        country = EXCLUDED.country,
                        region_continent = EXCLUDED.region_continent,
                        media = EXCLUDED.media,
                        proto_unicast = EXCLUDED.proto_unicast,
                        proto_multicast = EXCLUDED.proto_multicast,
                        proto_ipv6 = EXCLUDED.proto_ipv6,
                        website = EXCLUDED.website,
                        url_stats = EXCLUDED.url_stats,
                        notes = EXCLUDED.notes,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        synced_at = CURRENT_TIMESTAMP
                """, (
                    ix_id, str(ix.get('org_id', '')), ix.get('name', ''),
                    ix.get('name_long', ''), ix.get('aka', ''),
                    ix.get('city', ''), ix.get('country', ''),
                    ix.get('region_continent', ''), ix.get('media', ''),
                    ix.get('proto_unicast', ''), ix.get('proto_multicast'),
                    ix.get('proto_ipv6', ''), ix.get('website', ''),
                    ix.get('url_stats', ''), ix.get('notes', ''),
                    ix.get('status', 'ok'), ix.get('created'), ix.get('updated'),
                ))

                upserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"IX upsert error: {e}")

        conn.commit()
        logger.info(f"✅ Internet Exchanges: {upserted} upserted, {errors} errors")

    except Exception as e:
        logger.error(f"IX ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'endpoint': 'internet_exchanges',
        'total_fetched': len(ixes),
        'upserted': upserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# INTERNET EXCHANGE ↔ FACILITY INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_ix_facilities(get_db) -> Dict[str, Any]:
    """Fetch IX-facility relationships from PeeringDB /ixfac."""
    ixfacs = _pdb_fetch_paginated(PEERINGDB_IX_FAC)
    if ixfacs is None:
        return {'success': False, 'error': 'Failed to fetch IX-facility data'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for ifac in ixfacs:
            try:
                ifac_id = str(ifac.get('id', ''))
                ix_id = str(ifac.get('ix_id', ''))
                fac_id = str(ifac.get('fac_id', ''))

                if not ifac_id or not ix_id or not fac_id:
                    continue

                c.execute("""
                    INSERT INTO pdb_ix_facilities
                        (id, ix_id, fac_id, name, city, country, status,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        ix_id = EXCLUDED.ix_id,
                        fac_id = EXCLUDED.fac_id,
                        name = EXCLUDED.name,
                        city = EXCLUDED.city,
                        country = EXCLUDED.country,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        synced_at = CURRENT_TIMESTAMP
                """, (
                    ifac_id, ix_id, fac_id, ifac.get('name', ''),
                    ifac.get('city', ''), ifac.get('country', ''),
                    ifac.get('status', 'ok'), ifac.get('created'), ifac.get('updated'),
                ))

                upserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"IXFac upsert error: {e}")

        conn.commit()
        logger.info(f"✅ IX-Facilities: {upserted} upserted, {errors} errors")

    except Exception as e:
        logger.error(f"IX-facility ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'endpoint': 'ix_facilities',
        'total_fetched': len(ixfacs),
        'upserted': upserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# CAMPUS INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_campus(get_db) -> Dict[str, Any]:
    """Fetch all campus entities from PeeringDB."""
    campuses = _pdb_fetch_paginated(PEERINGDB_CAMPUS)
    if campuses is None:
        return {'success': False, 'error': 'Failed to fetch campus data'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for campus in campuses:
            try:
                campus_id = str(campus.get('id', ''))
                if not campus_id:
                    continue

                c.execute("""
                    INSERT INTO pdb_campus
                        (id, org_id, name, name_long, aka, website, notes, status,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        org_id = EXCLUDED.org_id,
                        name = EXCLUDED.name,
                        name_long = EXCLUDED.name_long,
                        aka = EXCLUDED.aka,
                        website = EXCLUDED.website,
                        notes = EXCLUDED.notes,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        synced_at = CURRENT_TIMESTAMP
                """, (
                    campus_id, str(campus.get('org_id', '')),
                    campus.get('name', ''), campus.get('name_long', ''),
                    campus.get('aka', ''), campus.get('website', ''),
                    campus.get('notes', ''), campus.get('status', 'ok'),
                    campus.get('created'), campus.get('updated'),
                ))

                upserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"Campus upsert error: {e}")

        conn.commit()
        logger.info(f"✅ Campus: {upserted} upserted, {errors} errors")

    except Exception as e:
        logger.error(f"Campus ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'endpoint': 'campus',
        'total_fetched': len(campuses),
        'upserted': upserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# MAIN SYNC FUNCTIONS
# ─────────────────────────────────────────────────────────────
def run_network_sync(get_db) -> Dict[str, Any]:
    """Sync networks + network-facility relationships."""
    results = {
        'source': 'PeeringDB Network Intelligence',
        'timestamp': datetime.utcnow().isoformat(),
    }

    init_network_ix_tables(get_db)

    net_result = ingest_networks(get_db)
    results['networks'] = net_result

    netfac_result = ingest_network_facilities(get_db)
    results['network_facilities'] = netfac_result

    results['success'] = all([
        net_result.get('success', False),
        netfac_result.get('success', False),
    ])

    total = net_result.get('upserted', 0) + netfac_result.get('upserted', 0)
    results['total_records'] = total
    logger.info(f"🔗 Network sync complete: {total} total records")
    return results


def run_ix_sync(get_db) -> Dict[str, Any]:
    """Sync internet exchanges + IX-facility relationships."""
    results = {
        'source': 'PeeringDB Internet Exchange Intelligence',
        'timestamp': datetime.utcnow().isoformat(),
    }

    init_network_ix_tables(get_db)

    ix_result = ingest_ix(get_db)
    results['internet_exchanges'] = ix_result

    ixfac_result = ingest_ix_facilities(get_db)
    results['ix_facilities'] = ixfac_result

    results['success'] = all([
        ix_result.get('success', False),
        ixfac_result.get('success', False),
    ])

    total = ix_result.get('upserted', 0) + ixfac_result.get('upserted', 0)
    results['total_records'] = total
    logger.info(f"🔗 IX sync complete: {total} total records")
    return results


def run_campus_sync(get_db) -> Dict[str, Any]:
    """Sync campus entities."""
    results = {
        'source': 'PeeringDB Campus Intelligence',
        'timestamp': datetime.utcnow().isoformat(),
    }

    init_network_ix_tables(get_db)

    campus_result = ingest_campus(get_db)
    results['campus'] = campus_result

    results['success'] = campus_result.get('success', False)
    results['total_records'] = campus_result.get('upserted', 0)
    logger.info(f"🔗 Campus sync complete: {results['total_records']} records")
    return results


def run_peeringdb_full_sync(get_db) -> Dict[str, Any]:
    """Full sync: networks + IX + campus + all cross-references."""
    results = {
        'source': 'PeeringDB Full Intelligence Suite',
        'timestamp': datetime.utcnow().isoformat(),
    }

    init_network_ix_tables(get_db)

    # Networks
    net_result = ingest_networks(get_db)
    results['networks'] = net_result
    netfac_result = ingest_network_facilities(get_db)
    results['network_facilities'] = netfac_result

    # Internet Exchanges
    ix_result = ingest_ix(get_db)
    results['internet_exchanges'] = ix_result
    ixfac_result = ingest_ix_facilities(get_db)
    results['ix_facilities'] = ixfac_result

    # Campus
    campus_result = ingest_campus(get_db)
    results['campus'] = campus_result

    results['success'] = all([
        net_result.get('success', False),
        netfac_result.get('success', False),
        ix_result.get('success', False),
        ixfac_result.get('success', False),
        campus_result.get('success', False),
    ])

    total = (
        net_result.get('upserted', 0) +
        netfac_result.get('upserted', 0) +
        ix_result.get('upserted', 0) +
        ixfac_result.get('upserted', 0) +
        campus_result.get('upserted', 0)
    )
    results['total_records'] = total
    logger.info(f"🔗 Full PeeringDB sync complete: {total} total records")
    return results


# ─────────────────────────────────────────────────────────────
# API ENDPOINTS (register with Flask app)
# ─────────────────────────────────────────────────────────────
def register_network_ix_routes(app, get_db):
    """Register network, IX, and campus routes with the Flask app."""
    from flask import jsonify, request

    @app.route('/api/jobs/network-sync', methods=['POST'])
    def network_sync_job():
        """Trigger network + network-facility sync."""
        try:
            result = run_network_sync(get_db)
            return jsonify(result), 200 if result.get('success') else 500
        except Exception as e:
            logger.error(f"Network sync job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/jobs/ix-sync', methods=['POST'])
    def ix_sync_job():
        """Trigger IX + IX-facility sync."""
        try:
            result = run_ix_sync(get_db)
            return jsonify(result), 200 if result.get('success') else 500
        except Exception as e:
            logger.error(f"IX sync job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/jobs/campus-sync', methods=['POST'])
    def campus_sync_job():
        """Trigger campus sync."""
        try:
            result = run_campus_sync(get_db)
            return jsonify(result), 200 if result.get('success') else 500
        except Exception as e:
            logger.error(f"Campus sync job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/jobs/peeringdb-full-sync', methods=['POST'])
    def peeringdb_full_sync_job():
        """Trigger full PeeringDB sync (networks + IX + campus)."""
        try:
            result = run_peeringdb_full_sync(get_db)
            return jsonify(result), 200 if result.get('success') else 500
        except Exception as e:
            logger.error(f"Full PeeringDB sync job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/v1/networks/summary', methods=['GET'])
    def networks_summary():
        """Get network statistics and sample data."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # Network counts
            c.execute("SELECT COUNT(*) FROM pdb_networks")
            total_networks = c.fetchone()[0]

            c.execute("SELECT COUNT(DISTINCT asn) FROM pdb_networks WHERE asn IS NOT NULL")
            unique_asns = c.fetchone()[0]

            c.execute("SELECT COUNT(DISTINCT org_id) FROM pdb_networks WHERE org_id IS NOT NULL")
            unique_orgs = c.fetchone()[0]

            # Sample networks
            c.execute("""
                SELECT id, asn, name, name_long, status
                FROM pdb_networks
                WHERE status = 'ok'
                ORDER BY asn
                LIMIT 10
            """)

            samples = []
            for row in c.fetchall():
                samples.append({
                    'id': row[0], 'asn': row[1], 'name': row[2],
                    'name_long': row[3], 'status': row[4],
                })

            return jsonify({
                'success': True,
                'summary': {
                    'total_networks': total_networks,
                    'unique_asns': unique_asns,
                    'unique_organizations': unique_orgs,
                },
                'samples': samples,
                'source': 'PeeringDB via DC Hub Intelligence',
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/networks/facility/<fac_id>', methods=['GET'])
    def networks_at_facility(fac_id):
        """Get all networks present at a specific facility."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            c.execute("""
                SELECT nf.net_id, n.asn, n.name, n.name_long, nf.city, nf.country,
                       nf.local_asn, nf.status
                FROM pdb_network_facilities nf
                LEFT JOIN pdb_networks n ON nf.net_id = n.id
                WHERE nf.fac_id = %s
                ORDER BY n.asn
            """, (fac_id,))

            networks = []
            for row in c.fetchall():
                networks.append({
                    'net_id': row[0], 'asn': row[1], 'name': row[2],
                    'name_long': row[3], 'city': row[4], 'country': row[5],
                    'local_asn': row[6], 'status': row[7],
                })

            return jsonify({
                'success': True,
                'facility_id': fac_id,
                'networks': networks,
                'network_count': len(networks),
                'source': 'PeeringDB via DC Hub Intelligence',
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/ix/summary', methods=['GET'])
    def ix_summary():
        """Get internet exchange statistics and sample data."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # IX counts
            c.execute("SELECT COUNT(*) FROM pdb_ix")
            total_ixes = c.fetchone()[0]

            c.execute("SELECT COUNT(DISTINCT country) FROM pdb_ix WHERE country IS NOT NULL")
            countries = c.fetchone()[0]

            c.execute("SELECT COUNT(DISTINCT region_continent) FROM pdb_ix WHERE region_continent IS NOT NULL")
            continents = c.fetchone()[0]

            # Sample IXes
            c.execute("""
                SELECT id, name, name_long, city, country, proto_unicast, proto_ipv6
                FROM pdb_ix
                WHERE status = 'ok'
                ORDER BY name
                LIMIT 10
            """)

            samples = []
            for row in c.fetchall():
                samples.append({
                    'id': row[0], 'name': row[1], 'name_long': row[2],
                    'city': row[3], 'country': row[4],
                    'proto_unicast': row[5], 'proto_ipv6': row[6],
                })

            return jsonify({
                'success': True,
                'summary': {
                    'total_exchanges': total_ixes,
                    'countries': countries,
                    'continents': continents,
                },
                'samples': samples,
                'source': 'PeeringDB via DC Hub Intelligence',
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/connectivity/<fac_id>', methods=['GET'])
    def facility_connectivity(fac_id):
        """
        Get comprehensive connectivity data for a facility:
        networks, internet exchanges, and carriers all in one place.
        This is the "big value" endpoint for site selection analysis.
        """
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # Networks at this facility
            c.execute("""
                SELECT nf.net_id, n.asn, n.name, n.status, COUNT(*) OVER() as net_count
                FROM pdb_network_facilities nf
                LEFT JOIN pdb_networks n ON nf.net_id = n.id
                WHERE nf.fac_id = %s
                ORDER BY n.asn
            """, (fac_id,))

            networks = []
            net_count = 0
            for row in c.fetchall():
                networks.append({
                    'net_id': row[0], 'asn': row[1], 'name': row[2], 'status': row[3],
                })
                net_count = row[4]

            # Internet exchanges at this facility
            c.execute("""
                SELECT if.ix_id, i.name, i.name_long, i.city, i.country, i.proto_unicast, COUNT(*) OVER() as ix_count
                FROM pdb_ix_facilities if
                LEFT JOIN pdb_ix i ON if.ix_id = i.id
                WHERE if.fac_id = %s
                ORDER BY i.name
            """, (fac_id,))

            exchanges = []
            ix_count = 0
            for row in c.fetchall():
                exchanges.append({
                    'ix_id': row[0], 'name': row[1], 'name_long': row[2],
                    'city': row[3], 'country': row[4], 'proto_unicast': row[5],
                })
                ix_count = row[6]

            # Carriers at this facility (from fiber integration, if available)
            carriers = []
            carrier_count = 0
            try:
                c.execute("""
                    SELECT carrier_pdb_id, carrier_name, COUNT(*) OVER() as car_count
                    FROM carrier_facility_presence
                    WHERE dchub_facility_id = (
                        SELECT id FROM facilities WHERE id = %s LIMIT 1
                    )
                    ORDER BY carrier_name
                """, (int(fac_id),))

                carrier_count = 0
                for row in c.fetchall():
                    carriers.append({
                        'carrier_id': row[0], 'carrier_name': row[1],
                    })
                    carrier_count = row[2]
            except Exception:
                # Table may not exist or facility ID format mismatch
                pass

            # Connectivity score (0-100)
            connectivity_score = min(100, (
                net_count * 2 +
                ix_count * 5 +
                carrier_count * 3
            ))

            return jsonify({
                'success': True,
                'facility_id': fac_id,
                'connectivity': {
                    'networks': {
                        'count': net_count,
                        'sample': networks[:5],
                    },
                    'internet_exchanges': {
                        'count': ix_count,
                        'sample': exchanges[:5],
                    },
                    'carriers': {
                        'count': carrier_count,
                        'sample': carriers[:5],
                    },
                },
                'connectivity_score': connectivity_score,
                'connectivity_rating': (
                    'Excellent' if connectivity_score >= 80 else
                    'Good' if connectivity_score >= 50 else
                    'Moderate' if connectivity_score >= 25 else
                    'Limited'
                ),
                'total_connections': net_count + ix_count + carrier_count,
                'source': 'PeeringDB + Carriers via DC Hub Intelligence',
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info(
        "🔗 Network & IX routes registered: "
        "/api/jobs/network-sync, /api/jobs/ix-sync, /api/jobs/campus-sync, "
        "/api/jobs/peeringdb-full-sync, /api/v1/networks/summary, "
        "/api/v1/networks/facility/<fac_id>, /api/v1/ix/summary, "
        "/api/v1/connectivity/<fac_id>"
    )


# ─────────────────────────────────────────────────────────────
# INTEGRATION HELPER FOR fiber_integration.py
# ─────────────────────────────────────────────────────────────
def register_with_fiber_integration():
    """
    Integration guide for wiring network_ix_ingestion.py into your
    fiber_integration.py or main Flask app.

    Example:
    --------
    In your main app initialization (e.g., app.py or fiber_integration.py):

        from network_ix_ingestion import (
            register_network_ix_routes, run_peeringdb_full_sync
        )

        # Register routes
        register_network_ix_routes(app, get_db)

        # Optional: schedule full sync on app startup or via scheduler
        # run_peeringdb_full_sync(get_db)

    Endpoints available after registration:
        POST /api/jobs/network-sync          — sync networks + netfac
        POST /api/jobs/ix-sync               — sync IX + ixfac
        POST /api/jobs/campus-sync           — sync campus
        POST /api/jobs/peeringdb-full-sync   — all of above

        GET /api/v1/networks/summary         — network stats + samples
        GET /api/v1/networks/facility/<id>   — networks at facility
        GET /api/v1/ix/summary               — IX stats + samples
        GET /api/v1/connectivity/<id>        — full connectivity view (networks + IX + carriers)

    Rate limiting:
        - 3 sec delay between paginated requests (anonymous, 20 req/min)
        - 1 sec delay with PEERINGDB_API_KEY env var
        - Automatic retry with exponential backoff on HTTP 429

    Database:
        All PeeringDB ID columns are TEXT to handle hex strings and integers.
        Tables created automatically via init_network_ix_tables(get_db).
    """
    return """
    To integrate:

    1. Import in your app:
       from network_ix_ingestion import register_network_ix_routes

    2. Register routes:
       register_network_ix_routes(app, get_db)

    3. (Optional) Schedule full sync:
       from apscheduler.schedulers.background import BackgroundScheduler
       scheduler = BackgroundScheduler()
       scheduler.add_job(
           lambda: run_peeringdb_full_sync(get_db),
           'cron',
           day_of_week='0',  # Weekly on Sunday
           hour=2,
           minute=0,
       )
       scheduler.start()
    """
