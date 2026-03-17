#!/usr/bin/env python3
"""
Metro Dark Fiber Data Seed for DC Hub
======================================
Run in Railway shell: python3 /tmp/metro_dark_fiber_seed.py

Creates metro_dark_fiber table and seeds it with carrier presence
across top US data center markets. Data sourced from public carrier
service pages, press releases, and FCC filings.
"""

import os, sys, json, time

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set. Run in Railway shell.")
    sys.exit(1)

import psycopg2
from psycopg2.extras import execute_values

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def create_tables(conn):
    cur = conn.cursor()
    
    # Metro dark fiber providers per market
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metro_dark_fiber (
            id SERIAL PRIMARY KEY,
            market TEXT NOT NULL,
            state TEXT,
            carrier TEXT NOT NULL,
            route_miles_approx INTEGER,
            on_net_buildings INTEGER,
            key_endpoints TEXT[],
            fiber_type TEXT DEFAULT 'metro',
            services TEXT[],
            notes TEXT,
            source TEXT,
            source_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(market, carrier)
        )
    """)
    
    # Market-level metro fiber summary
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metro_fiber_summary (
            id SERIAL PRIMARY KEY,
            market TEXT NOT NULL UNIQUE,
            state TEXT,
            total_carriers INTEGER DEFAULT 0,
            total_route_miles_approx INTEGER DEFAULT 0,
            total_on_net_buildings INTEGER DEFAULT 0,
            fiber_density_score INTEGER DEFAULT 0,
            tier TEXT,
            key_ix_points TEXT[],
            key_carrier_hotels TEXT[],
            notes TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mdf_market ON metro_dark_fiber (market)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mdf_carrier ON metro_dark_fiber (carrier)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mfs_market ON metro_fiber_summary (market)")
    
    conn.commit()
    cur.close()
    print("✅ Tables created: metro_dark_fiber, metro_fiber_summary")

# ── METRO DARK FIBER DATA ──
# Sources: Zayo.com/network, Lumen.com, CrownCastle.com, Lightpath, 
# SummitIG, DF&I, MetroOptic, Cogent, FirstLight, Segra, Uniti/Windstream,
# FiberLight, Consolidated Communications, Frontier, DatacenterFrontier articles

METRO_FIBER_DATA = [
    # ═══════════════════════════════════════════
    # NORTHERN VIRGINIA (Ashburn / Data Center Alley)
    # ═══════════════════════════════════════════
    {"market": "Northern Virginia", "state": "VA", "carrier": "Zayo",
     "route_miles_approx": 2800, "on_net_buildings": 350,
     "key_endpoints": ["Ashburn Data Center Alley", "Reston", "Manassas", "Sterling", "Bristow"],
     "services": ["dark fiber", "wavelengths", "400G", "ethernet"],
     "notes": "Largest metro fiber footprint in NoVA; long-haul connects to NYC, Atlanta, Chicago",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Northern Virginia", "state": "VA", "carrier": "Lumen",
     "route_miles_approx": 2200, "on_net_buildings": 280,
     "key_endpoints": ["Ashburn", "Reston", "Tysons Corner", "Manassas", "Herndon"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric", "SD-WAN"],
     "notes": "Private Connectivity Fabric overlays SDN control on dark fiber IRUs",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Northern Virginia", "state": "VA", "carrier": "Crown Castle",
     "route_miles_approx": 1800, "on_net_buildings": 220,
     "key_endpoints": ["Ashburn", "Sterling", "Reston", "Potomac River Crossing"],
     "services": ["dark fiber", "small cells", "wavelengths"],
     "notes": "Potomac River Crossing connects NoVA to NE US corridor; fiber assets selling to Zayo H1 2026",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/infrastructure-solutions/dark-fiber"},
    {"market": "Northern Virginia", "state": "VA", "carrier": "SummitIG",
     "route_miles_approx": 1200, "on_net_buildings": 85,
     "key_endpoints": ["Ashburn Data Center Alley", "Prince William County", "Loudoun County"],
     "services": ["dark fiber", "high-count fiber", "custom builds"],
     "notes": "Purpose-built underground dense fiber; 10+ year presence in NoVA; SierraIG JV with Neutral Networks",
     "source": "SummitIG networks", "source_url": "https://summitig.com/networks/"},
    {"market": "Northern Virginia", "state": "VA", "carrier": "DF&I",
     "route_miles_approx": 600, "on_net_buildings": 45,
     "key_endpoints": ["Ashburn Express loop", "Prince William County", "Maryland"],
     "services": ["dark fiber", "conduit", "custom network builds"],
     "notes": "Ashburn Express Connect: 6 on-net DCs on the Ashburn loop; expanding into MD and PW County",
     "source": "DatacenterFrontier interview", "source_url": "https://www.datacenterfrontier.com/"},
    {"market": "Northern Virginia", "state": "VA", "carrier": "Cogent",
     "route_miles_approx": 400, "on_net_buildings": 60,
     "key_endpoints": ["Ashburn", "Reston", "Tysons"],
     "services": ["dark fiber", "IP transit", "wavelengths"],
     "notes": "Acquired Sprint's fiber assets; strong IP backbone presence",
     "source": "Cogent network", "source_url": "https://www.cogentco.com/"},

    # ═══════════════════════════════════════════
    # DALLAS-FORT WORTH
    # ═══════════════════════════════════════════
    {"market": "Dallas-Fort Worth", "state": "TX", "carrier": "Zayo",
     "route_miles_approx": 1800, "on_net_buildings": 260,
     "key_endpoints": ["Infomart Dallas", "Richardson", "Plano", "Allen", "Garland"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "Major hub connecting to Austin, Houston, Atlanta long-haul routes",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Dallas-Fort Worth", "state": "TX", "carrier": "Lumen",
     "route_miles_approx": 1500, "on_net_buildings": 200,
     "key_endpoints": ["Infomart Dallas", "Richardson Telecom Corridor", "Fort Worth"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "Deep legacy CenturyLink/Level 3 metro fiber; Infomart anchor tenant",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Dallas-Fort Worth", "state": "TX", "carrier": "Crown Castle",
     "route_miles_approx": 1200, "on_net_buildings": 180,
     "key_endpoints": ["Dallas CBD", "Richardson", "Plano", "Irving"],
     "services": ["dark fiber", "small cells", "wavelengths"],
     "notes": "Metro-centric footprint; fiber assets transitioning to Zayo",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},
    {"market": "Dallas-Fort Worth", "state": "TX", "carrier": "FiberLight",
     "route_miles_approx": 800, "on_net_buildings": 90,
     "key_endpoints": ["Dallas", "Richardson", "Plano", "Allen"],
     "services": ["dark fiber", "lit services", "wavelengths"],
     "notes": "Texas-focused metro fiber specialist",
     "source": "FiberLight", "source_url": "https://www.fiberlight.com/"},

    # ═══════════════════════════════════════════
    # CHICAGO
    # ═══════════════════════════════════════════
    {"market": "Chicago", "state": "IL", "carrier": "Zayo",
     "route_miles_approx": 2000, "on_net_buildings": 280,
     "key_endpoints": ["350 E Cermak", "725 S Wells", "Elk Grove Village", "Franklin Park"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "350 E Cermak is one of the largest carrier hotels globally",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Chicago", "state": "IL", "carrier": "Lumen",
     "route_miles_approx": 1600, "on_net_buildings": 220,
     "key_endpoints": ["350 E Cermak", "725 S Wells", "Chicago Loop", "Schaumburg"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "Legacy Level 3 metro ring; deep downtown presence",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Chicago", "state": "IL", "carrier": "Crown Castle",
     "route_miles_approx": 1200, "on_net_buildings": 150,
     "key_endpoints": ["Chicago Loop", "Elk Grove Village", "suburbs"],
     "services": ["dark fiber", "small cells"],
     "notes": "Dense metro footprint; transitioning to Zayo",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},
    {"market": "Chicago", "state": "IL", "carrier": "SummitIG",
     "route_miles_approx": 100, "on_net_buildings": 20,
     "key_endpoints": ["Chicago metro", "data center campuses"],
     "services": ["dark fiber", "high-count fiber", "custom builds"],
     "notes": "Initial 60-mile buildout with 100 miles under development; new purpose-built underground network",
     "source": "SummitIG networks", "source_url": "https://summitig.com/networks/"},

    # ═══════════════════════════════════════════
    # NEW YORK / NEW JERSEY METRO
    # ═══════════════════════════════════════════
    {"market": "New York Metro", "state": "NY", "carrier": "Zayo",
     "route_miles_approx": 2500, "on_net_buildings": 400,
     "key_endpoints": ["60 Hudson St", "111 8th Ave", "165 Halsey St NJ", "Secaucus", "Weehawken"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "Dual state metro ring spanning Manhattan, NJ, Long Island, Westchester",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "New York Metro", "state": "NY", "carrier": "Lightpath",
     "route_miles_approx": 10800, "on_net_buildings": 600,
     "key_endpoints": ["NYC", "Long Island", "Westchester", "NJ", "Boston", "Miami", "Phoenix", "Columbus"],
     "services": ["dark fiber", "RapidPath dark fiber", "wavelengths", "ethernet"],
     "notes": "10,800+ route miles; 170+ on-net DCs; 7 cable landing stations; RapidPath 5-day turn-up",
     "source": "Lightpath fiber", "source_url": "https://lightpathfiber.com/services/dark-fiber"},
    {"market": "New York Metro", "state": "NY", "carrier": "Lumen",
     "route_miles_approx": 2000, "on_net_buildings": 300,
     "key_endpoints": ["60 Hudson St", "111 8th Ave", "32 Ave of Americas", "NJ"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "Deep Manhattan and NJ fiber presence; JUNO trans-Pacific cable backhaul",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "New York Metro", "state": "NY", "carrier": "Crown Castle",
     "route_miles_approx": 1800, "on_net_buildings": 250,
     "key_endpoints": ["Manhattan", "NJ Meadowlands", "Transcom Route to DC/VA"],
     "services": ["dark fiber", "Transcom Route", "small cells"],
     "notes": "Transcom Route: unique NYC→DC path diverse from I-95 corridor; Potomac River crossing",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},
    {"market": "New York Metro", "state": "NY", "carrier": "GIX",
     "route_miles_approx": 50, "on_net_buildings": 10,
     "key_endpoints": ["60 Hudson St Manhattan", "165 Halsey St Newark"],
     "services": ["dark fiber", "Hudson River crossing"],
     "notes": "First new Hudson River fiber crossing in 20+ years; carrier-neutral private infrastructure",
     "source": "GIX fiber", "source_url": "https://gixfiber.com/"},

    # ═══════════════════════════════════════════
    # PHOENIX / ARIZONA
    # ═══════════════════════════════════════════
    {"market": "Phoenix", "state": "AZ", "carrier": "Zayo",
     "route_miles_approx": 800, "on_net_buildings": 120,
     "key_endpoints": ["Phoenix CBD", "Tempe", "Mesa", "Chandler", "Goodyear"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Growing AZ metro presence; connects to LA and Dallas long-haul",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Phoenix", "state": "AZ", "carrier": "Lumen",
     "route_miles_approx": 600, "on_net_buildings": 80,
     "key_endpoints": ["Phoenix", "Tempe", "Scottsdale", "Mesa"],
     "services": ["dark fiber", "wavelengths", "ethernet"],
     "notes": "Legacy CenturyLink presence; expanding with AI-focused fiber construction",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Phoenix", "state": "AZ", "carrier": "Lightpath",
     "route_miles_approx": 400, "on_net_buildings": 40,
     "key_endpoints": ["Phoenix metro", "data center campuses"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Expanding to Phoenix metro; part of multi-market expansion",
     "source": "Lightpath fiber", "source_url": "https://lightpathfiber.com/"},
    {"market": "Phoenix", "state": "AZ", "carrier": "SummitIG",
     "route_miles_approx": 0, "on_net_buildings": 0,
     "key_endpoints": ["Phoenix metro (planned)"],
     "services": ["dark fiber (planned)"],
     "notes": "Expanding to Phoenix and Salt Lake City; under development",
     "source": "SummitIG networks", "source_url": "https://summitig.com/networks/"},
    {"market": "Phoenix", "state": "AZ", "carrier": "Cox Business",
     "route_miles_approx": 500, "on_net_buildings": 60,
     "key_endpoints": ["Phoenix", "Scottsdale", "Tempe", "Mesa", "Chandler"],
     "services": ["dark fiber", "ethernet", "wavelengths"],
     "notes": "Incumbent local provider with deep metro fiber in AZ market",
     "source": "Cox Business", "source_url": "https://www.cox.com/business/"},

    # ═══════════════════════════════════════════
    # SILICON VALLEY / SAN FRANCISCO BAY AREA
    # ═══════════════════════════════════════════
    {"market": "Silicon Valley", "state": "CA", "carrier": "Zayo",
     "route_miles_approx": 2200, "on_net_buildings": 350,
     "key_endpoints": ["Santa Clara", "San Jose", "Palo Alto", "Fremont", "Oakland"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "Dense Bay Area metro ring; Santa Clara is primary hyperscale hub",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Silicon Valley", "state": "CA", "carrier": "Lumen",
     "route_miles_approx": 1800, "on_net_buildings": 250,
     "key_endpoints": ["Santa Clara", "San Jose", "San Francisco", "Oakland"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "JUNO cable backhaul terminates here; key west coast connectivity hub",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Silicon Valley", "state": "CA", "carrier": "Crown Castle",
     "route_miles_approx": 1200, "on_net_buildings": 180,
     "key_endpoints": ["San Jose", "Santa Clara", "San Francisco"],
     "services": ["dark fiber", "small cells"],
     "notes": "Metro-centric footprint; transitioning to Zayo",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},

    # ═══════════════════════════════════════════
    # ATLANTA
    # ═══════════════════════════════════════════
    {"market": "Atlanta", "state": "GA", "carrier": "Zayo",
     "route_miles_approx": 1400, "on_net_buildings": 200,
     "key_endpoints": ["56 Marietta St", "Atlanta Midtown", "Alpharetta", "Suwanee"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "56 Marietta St is southeast's premier carrier hotel; connects to Dallas, Charlotte long-haul",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Atlanta", "state": "GA", "carrier": "Lumen",
     "route_miles_approx": 1200, "on_net_buildings": 160,
     "key_endpoints": ["56 Marietta St", "Atlanta Metro", "Kennesaw"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "Deep southeast presence from Level 3 legacy",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Atlanta", "state": "GA", "carrier": "Segra",
     "route_miles_approx": 600, "on_net_buildings": 80,
     "key_endpoints": ["Atlanta", "suburbs", "southeast corridor"],
     "services": ["dark fiber", "ethernet", "wavelengths"],
     "notes": "Southeast regional specialist; deep enterprise fiber",
     "source": "Segra", "source_url": "https://www.segra.com/"},

    # ═══════════════════════════════════════════
    # COLUMBUS, OHIO
    # ═══════════════════════════════════════════
    {"market": "Columbus", "state": "OH", "carrier": "Zayo",
     "route_miles_approx": 600, "on_net_buildings": 80,
     "key_endpoints": ["Columbus metro", "New Albany", "Dublin"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Expanding presence as Columbus grows as top-5 DC market",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Columbus", "state": "OH", "carrier": "Lumen",
     "route_miles_approx": 500, "on_net_buildings": 60,
     "key_endpoints": ["Columbus", "New Albany", "Westerville"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Legacy CenturyLink metro fiber; growing with market demand",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Columbus", "state": "OH", "carrier": "SummitIG",
     "route_miles_approx": 80, "on_net_buildings": 15,
     "key_endpoints": ["Columbus metro", "data center campuses"],
     "services": ["dark fiber", "high-count fiber"],
     "notes": "New purpose-built underground dense fiber; supporting DC market growth",
     "source": "SummitIG networks", "source_url": "https://summitig.com/networks/"},
    {"market": "Columbus", "state": "OH", "carrier": "Lightpath",
     "route_miles_approx": 300, "on_net_buildings": 30,
     "key_endpoints": ["Columbus metro"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Multi-market expansion includes Columbus",
     "source": "Lightpath fiber", "source_url": "https://lightpathfiber.com/"},

    # ═══════════════════════════════════════════
    # LOS ANGELES
    # ═══════════════════════════════════════════
    {"market": "Los Angeles", "state": "CA", "carrier": "Zayo",
     "route_miles_approx": 1800, "on_net_buildings": 280,
     "key_endpoints": ["One Wilshire", "El Segundo", "Downtown LA", "Irvine"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "One Wilshire is west coast's largest carrier hotel; connects to SV and Phoenix",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Los Angeles", "state": "CA", "carrier": "Lumen",
     "route_miles_approx": 1400, "on_net_buildings": 200,
     "key_endpoints": ["One Wilshire", "El Segundo", "Downtown LA"],
     "services": ["dark fiber", "wavelengths", "private connectivity fabric"],
     "notes": "JUNO cable terminates at Grover Beach with backhaul to LA PoPs",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Los Angeles", "state": "CA", "carrier": "Crown Castle",
     "route_miles_approx": 1000, "on_net_buildings": 150,
     "key_endpoints": ["LA to San Diego corridor", "624 S Grand Ave", "609 W 7th St", "530 W 6th St"],
     "services": ["dark fiber", "small cells"],
     "notes": "LA-SD dark fiber corridor; 3 on-net LA data centers",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},

    # ═══════════════════════════════════════════
    # DENVER
    # ═══════════════════════════════════════════
    {"market": "Denver", "state": "CO", "carrier": "Zayo",
     "route_miles_approx": 1600, "on_net_buildings": 200,
     "key_endpoints": ["Denver CBD", "Boulder", "Colorado Springs", "1850 Pearl St"],
     "services": ["dark fiber", "wavelengths", "400G"],
     "notes": "Zayo HQ market; long-haul routes to SLC, Chicago, Dallas from here",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Denver", "state": "CO", "carrier": "Lumen",
     "route_miles_approx": 1200, "on_net_buildings": 150,
     "key_endpoints": ["Denver", "Boulder", "Broomfield"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Lumen HQ market; deep legacy infrastructure",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # BOSTON
    # ═══════════════════════════════════════════
    {"market": "Boston", "state": "MA", "carrier": "Crown Castle",
     "route_miles_approx": 1000, "on_net_buildings": 130,
     "key_endpoints": ["Boston Downtown", "Providence", "Worcester", "New Hampshire"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Dense metro Boston network extending south to Providence, west to Worcester, north to NH",
     "source": "Crown Castle fiber", "source_url": "https://www.crowncastle.com/"},
    {"market": "Boston", "state": "MA", "carrier": "Lightpath",
     "route_miles_approx": 800, "on_net_buildings": 80,
     "key_endpoints": ["Boston metro", "data center campuses"],
     "services": ["dark fiber", "RapidPath", "wavelengths"],
     "notes": "RapidPath 5-day dark fiber turn-up available in Boston",
     "source": "Lightpath fiber", "source_url": "https://lightpathfiber.com/"},
    {"market": "Boston", "state": "MA", "carrier": "FirstLight",
     "route_miles_approx": 600, "on_net_buildings": 70,
     "key_endpoints": ["Boston", "Cambridge", "Waltham", "suburban ring"],
     "services": ["dark fiber", "wavelengths", "ethernet"],
     "notes": "Northeast regional specialist; deep enterprise presence",
     "source": "FirstLight", "source_url": "https://www.firstlight.net/"},

    # ═══════════════════════════════════════════
    # SEATTLE
    # ═══════════════════════════════════════════
    {"market": "Seattle", "state": "WA", "carrier": "Zayo",
     "route_miles_approx": 1000, "on_net_buildings": 150,
     "key_endpoints": ["Westin Building", "Sabey Intergate", "Tukwila", "Quincy corridor"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Westin Building is Pacific NW's carrier hotel; Quincy long-haul connects to hyperscale DCs",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Seattle", "state": "WA", "carrier": "Lumen",
     "route_miles_approx": 800, "on_net_buildings": 120,
     "key_endpoints": ["Seattle", "Bellevue", "Redmond", "Tukwila"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Deep Pacific NW presence; connects to Portland and Bay Area",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # MIAMI / SOUTH FLORIDA
    # ═══════════════════════════════════════════
    {"market": "Miami", "state": "FL", "carrier": "Zayo",
     "route_miles_approx": 800, "on_net_buildings": 120,
     "key_endpoints": ["NAP of the Americas", "Boca Raton", "Fort Lauderdale"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "NAP of the Americas is LatAm connectivity gateway; subsea cable landing",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Miami", "state": "FL", "carrier": "Lightpath",
     "route_miles_approx": 500, "on_net_buildings": 50,
     "key_endpoints": ["Miami", "Fort Lauderdale", "Boca Raton"],
     "services": ["dark fiber", "RapidPath", "wavelengths"],
     "notes": "RapidPath 5-day dark fiber turn-up available in Miami",
     "source": "Lightpath fiber", "source_url": "https://lightpathfiber.com/"},

    # ═══════════════════════════════════════════
    # PORTLAND
    # ═══════════════════════════════════════════
    {"market": "Portland", "state": "OR", "carrier": "Zayo",
     "route_miles_approx": 600, "on_net_buildings": 80,
     "key_endpoints": ["Pittock Block", "Hillsboro", "Beaverton"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Hillsboro is major DC campus area; connects to Seattle and Bay Area",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Portland", "state": "OR", "carrier": "Lumen",
     "route_miles_approx": 400, "on_net_buildings": 60,
     "key_endpoints": ["Portland", "Hillsboro", "Beaverton"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "PNW metro fiber connecting to Hillsboro DC campus area",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # HOUSTON
    # ═══════════════════════════════════════════
    {"market": "Houston", "state": "TX", "carrier": "Zayo",
     "route_miles_approx": 1000, "on_net_buildings": 150,
     "key_endpoints": ["Houston CBD", "Energy Corridor", "The Woodlands", "Sugar Land"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Energy sector connectivity hub; connects to Dallas long-haul",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Houston", "state": "TX", "carrier": "Lumen",
     "route_miles_approx": 800, "on_net_buildings": 120,
     "key_endpoints": ["Houston", "Energy Corridor", "Katy"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Deep Texas metro presence; energy sector focus",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},
    {"market": "Houston", "state": "TX", "carrier": "FiberLight",
     "route_miles_approx": 500, "on_net_buildings": 60,
     "key_endpoints": ["Houston", "Energy Corridor", "suburbs"],
     "services": ["dark fiber", "lit services"],
     "notes": "Texas-focused metro fiber specialist",
     "source": "FiberLight", "source_url": "https://www.fiberlight.com/"},

    # ═══════════════════════════════════════════
    # SAN ANTONIO
    # ═══════════════════════════════════════════
    {"market": "San Antonio", "state": "TX", "carrier": "Zayo",
     "route_miles_approx": 400, "on_net_buildings": 50,
     "key_endpoints": ["San Antonio", "Westover Hills"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Growing DC market; military/government presence drives fiber demand",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "San Antonio", "state": "TX", "carrier": "Lumen",
     "route_miles_approx": 300, "on_net_buildings": 40,
     "key_endpoints": ["San Antonio metro"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Legacy CenturyLink metro fiber",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # SALT LAKE CITY
    # ═══════════════════════════════════════════
    {"market": "Salt Lake City", "state": "UT", "carrier": "Zayo",
     "route_miles_approx": 500, "on_net_buildings": 60,
     "key_endpoints": ["SLC metro", "West Valley", "Lehi"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Denver-SLC long-haul dark fiber route completed; growing DC market",
     "source": "Zayo network map", "source_url": "https://www.zayo.com/network/"},
    {"market": "Salt Lake City", "state": "UT", "carrier": "Lumen",
     "route_miles_approx": 300, "on_net_buildings": 40,
     "key_endpoints": ["SLC", "Provo corridor"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Utah metro fiber from legacy CenturyLink",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # CHARLOTTE
    # ═══════════════════════════════════════════
    {"market": "Charlotte", "state": "NC", "carrier": "Segra",
     "route_miles_approx": 500, "on_net_buildings": 70,
     "key_endpoints": ["Charlotte CBD", "University area", "suburbs"],
     "services": ["dark fiber", "wavelengths", "ethernet"],
     "notes": "Southeast regional specialist; banking sector connectivity",
     "source": "Segra", "source_url": "https://www.segra.com/"},
    {"market": "Charlotte", "state": "NC", "carrier": "Lumen",
     "route_miles_approx": 400, "on_net_buildings": 50,
     "key_endpoints": ["Charlotte", "Research Triangle corridor"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Connects to Atlanta and Raleigh long-haul",
     "source": "Lumen enterprise", "source_url": "https://www.lumen.com/"},

    # ═══════════════════════════════════════════
    # RICHMOND, VIRGINIA
    # ═══════════════════════════════════════════
    {"market": "Richmond", "state": "VA", "carrier": "SummitIG",
     "route_miles_approx": 175, "on_net_buildings": 20,
     "key_endpoints": ["Richmond area", "NoVA long-haul connection"],
     "services": ["dark fiber", "long-haul"],
     "notes": "175+ miles long-haul dark fiber connecting NoVA to Richmond and beyond",
     "source": "SummitIG networks", "source_url": "https://summitig.com/networks/"},
    {"market": "Richmond", "state": "VA", "carrier": "Segra",
     "route_miles_approx": 300, "on_net_buildings": 40,
     "key_endpoints": ["Richmond", "suburbs"],
     "services": ["dark fiber", "wavelengths"],
     "notes": "Deep Virginia/Carolinas regional presence",
     "source": "Segra", "source_url": "https://www.segra.com/"},
]

# ── MARKET SUMMARIES ──

MARKET_SUMMARIES = [
    {"market": "Northern Virginia", "state": "VA", "tier": "Tier 1",
     "key_ix_points": ["Equinix Ashburn IX", "DE-CIX New York (Ashburn PoP)"],
     "key_carrier_hotels": ["Equinix DC1-DC21", "CoreSite VA1-VA3", "QTS Ashburn"],
     "notes": "World's largest data center market; 70%+ of global internet traffic touches NoVA"},
    {"market": "Dallas-Fort Worth", "state": "TX", "tier": "Tier 1",
     "key_ix_points": ["Equinix Dallas IX", "CyrusOne IX Dallas"],
     "key_carrier_hotels": ["Infomart Dallas", "Equinix DA1-DA11", "CyrusOne Carrollton"],
     "notes": "Second-largest US DC market; no state income tax; abundant power"},
    {"market": "Chicago", "state": "IL", "tier": "Tier 1",
     "key_ix_points": ["Equinix Chicago IX", "350 E Cermak IX"],
     "key_carrier_hotels": ["350 E Cermak", "725 S Wells", "CoreSite CH1"],
     "notes": "Midwest connectivity hub; 350 E Cermak is one of world's largest carrier hotels"},
    {"market": "New York Metro", "state": "NY", "tier": "Tier 1",
     "key_ix_points": ["NYIIX", "DE-CIX New York", "Equinix NY IX"],
     "key_carrier_hotels": ["60 Hudson St", "111 8th Ave", "165 Halsey St"],
     "notes": "Global financial center; highest density of carrier hotels in US"},
    {"market": "Phoenix", "state": "AZ", "tier": "Tier 2",
     "key_ix_points": ["Phoenix IX"],
     "key_carrier_hotels": ["CyrusOne Phoenix", "Stream Data Centers"],
     "notes": "Fastest-growing US DC market; abundant land and solar power; low natural disaster risk"},
    {"market": "Silicon Valley", "state": "CA", "tier": "Tier 1",
     "key_ix_points": ["Equinix SV IX", "SFBA IX"],
     "key_carrier_hotels": ["Equinix SV1-SV16", "CoreSite SV1-SV8"],
     "notes": "Tech industry epicenter; highest density of enterprise DC customers"},
    {"market": "Atlanta", "state": "GA", "tier": "Tier 1",
     "key_ix_points": ["56 Marietta St IX", "Equinix Atlanta IX"],
     "key_carrier_hotels": ["56 Marietta St", "QTS Atlanta Metro"],
     "notes": "Southeast connectivity gateway; 56 Marietta is region's premier carrier hotel"},
    {"market": "Columbus", "state": "OH", "tier": "Tier 2",
     "key_ix_points": ["OhioIX"],
     "key_carrier_hotels": ["QTS Columbus", "Cologix Columbus"],
     "notes": "Fastest-rising midwest DC market; abundant power, low cost, growing fiber density"},
    {"market": "Los Angeles", "state": "CA", "tier": "Tier 1",
     "key_ix_points": ["LAIIX", "Equinix LA IX"],
     "key_carrier_hotels": ["One Wilshire", "CoreSite LA1-LA3"],
     "notes": "West coast's largest carrier hotel (One Wilshire); subsea cable landing point"},
    {"market": "Denver", "state": "CO", "tier": "Tier 2",
     "key_ix_points": ["Denver IX"],
     "key_carrier_hotels": ["CoreSite DE1-DE2", "Flexential Denver"],
     "notes": "Zayo and Lumen HQ market; mountain west connectivity hub"},
    {"market": "Boston", "state": "MA", "tier": "Tier 2",
     "key_ix_points": ["BOSIX", "Equinix Boston IX"],
     "key_carrier_hotels": ["Markley Group Boston", "CoreSite BO1"],
     "notes": "Life sciences and financial services DC demand driver"},
    {"market": "Seattle", "state": "WA", "tier": "Tier 2",
     "key_ix_points": ["SIX (Seattle Internet Exchange)", "Equinix SE IX"],
     "key_carrier_hotels": ["Westin Building Exchange", "Sabey Intergate"],
     "notes": "Pacific NW hub; Westin Building is region's carrier hotel; subsea cable access"},
    {"market": "Miami", "state": "FL", "tier": "Tier 2",
     "key_ix_points": ["FL-IX", "NAP of Americas IX"],
     "key_carrier_hotels": ["NAP of the Americas", "Equinix MI1"],
     "notes": "Latin America connectivity gateway; major subsea cable landing point"},
    {"market": "Portland", "state": "OR", "tier": "Tier 3",
     "key_ix_points": ["NWAX"],
     "key_carrier_hotels": ["Pittock Block"],
     "notes": "Hillsboro is major campus DC area; connects to Seattle and Bay Area"},
    {"market": "Houston", "state": "TX", "tier": "Tier 2",
     "key_ix_points": ["Houston IX"],
     "key_carrier_hotels": ["CyrusOne Houston", "DataPoint Houston"],
     "notes": "Energy sector connectivity hub; growing AI/cloud demand"},
    {"market": "San Antonio", "state": "TX", "tier": "Tier 3",
     "key_ix_points": [],
     "key_carrier_hotels": ["CyrusOne San Antonio"],
     "notes": "Military/government DC demand; growing commercial market"},
    {"market": "Salt Lake City", "state": "UT", "tier": "Tier 3",
     "key_ix_points": ["SLIX"],
     "key_carrier_hotels": ["Aligned SLC", "C7 Data Centers"],
     "notes": "Emerging DC market; low energy costs; Denver-SLC fiber corridor"},
    {"market": "Charlotte", "state": "NC", "tier": "Tier 3",
     "key_ix_points": [],
     "key_carrier_hotels": ["Flexential Charlotte"],
     "notes": "Banking sector drives enterprise DC demand; Segra regional hub"},
    {"market": "Richmond", "state": "VA", "tier": "Tier 3",
     "key_ix_points": [],
     "key_carrier_hotels": ["QTS Richmond"],
     "notes": "Growing as NoVA overflow market; SummitIG long-haul to Ashburn"},
]

def insert_fiber_data(conn):
    cur = conn.cursor()
    sql = """
        INSERT INTO metro_dark_fiber (
            market, state, carrier, route_miles_approx, on_net_buildings,
            key_endpoints, services, notes, source, source_url, fiber_type
        ) VALUES %s
        ON CONFLICT (market, carrier) DO UPDATE SET
            route_miles_approx = EXCLUDED.route_miles_approx,
            on_net_buildings = EXCLUDED.on_net_buildings,
            key_endpoints = EXCLUDED.key_endpoints,
            services = EXCLUDED.services,
            notes = EXCLUDED.notes,
            source = EXCLUDED.source,
            source_url = EXCLUDED.source_url,
            updated_at = NOW()
    """
    rows = []
    for d in METRO_FIBER_DATA:
        rows.append((
            d["market"], d["state"], d["carrier"],
            d.get("route_miles_approx", 0), d.get("on_net_buildings", 0),
            d.get("key_endpoints", []), d.get("services", []),
            d.get("notes"), d.get("source"), d.get("source_url"),
            d.get("fiber_type", "metro"),
        ))
    execute_values(cur, sql, rows, page_size=50)
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count

def insert_summaries(conn):
    cur = conn.cursor()
    
    # First insert/update from our summary data
    for s in MARKET_SUMMARIES:
        cur.execute("""
            INSERT INTO metro_fiber_summary (market, state, tier, key_ix_points, key_carrier_hotels, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (market) DO UPDATE SET
                tier = EXCLUDED.tier,
                key_ix_points = EXCLUDED.key_ix_points,
                key_carrier_hotels = EXCLUDED.key_carrier_hotels,
                notes = EXCLUDED.notes,
                updated_at = NOW()
        """, (s["market"], s["state"], s["tier"], 
              s.get("key_ix_points", []), s.get("key_carrier_hotels", []), s.get("notes")))
    
    # Then compute aggregates from metro_dark_fiber
    cur.execute("""
        UPDATE metro_fiber_summary mfs SET
            total_carriers = sub.carrier_count,
            total_route_miles_approx = sub.total_miles,
            total_on_net_buildings = sub.total_buildings,
            fiber_density_score = LEAST(100, (sub.carrier_count * 15) + LEAST(50, sub.total_miles / 100)),
            updated_at = NOW()
        FROM (
            SELECT market,
                COUNT(DISTINCT carrier) as carrier_count,
                COALESCE(SUM(route_miles_approx), 0) as total_miles,
                COALESCE(SUM(on_net_buildings), 0) as total_buildings
            FROM metro_dark_fiber
            GROUP BY market
        ) sub
        WHERE mfs.market = sub.market
    """)
    
    conn.commit()
    count = cur.rowcount
    cur.close()
    return count

def main():
    print("=" * 60)
    print("  Metro Dark Fiber Data Seed — DC Hub")
    print("=" * 60)
    
    conn = get_conn()
    create_tables(conn)
    
    fiber_count = insert_fiber_data(conn)
    print(f"✅ metro_dark_fiber: {fiber_count} carrier-market records upserted")
    
    summary_count = insert_summaries(conn)
    print(f"✅ metro_fiber_summary: {summary_count} market summaries updated")
    
    # Print summary
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM metro_dark_fiber")
    total_records = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT market) FROM metro_dark_fiber")
    total_markets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT carrier) FROM metro_dark_fiber")
    total_carriers = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(route_miles_approx), 0) FROM metro_dark_fiber")
    total_miles = cur.fetchone()[0]
    
    cur.execute("""
        SELECT market, total_carriers, total_route_miles_approx, fiber_density_score, tier
        FROM metro_fiber_summary 
        ORDER BY fiber_density_score DESC
    """)
    markets = cur.fetchall()
    cur.close()
    conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"  Metro Dark Fiber Seed Complete")
    print(f"{'=' * 60}")
    print(f"  Records:   {total_records}")
    print(f"  Markets:   {total_markets}")
    print(f"  Carriers:  {total_carriers}")
    print(f"  Route mi:  {total_miles:,}")
    print(f"\n  Market Rankings (by fiber density score):")
    for m in markets:
        print(f"    {m[4]:8s} | {m[0]:22s} | {m[1]} carriers | {m[2]:,} mi | score: {m[3]}")
    print(f"{'=' * 60}")

if __name__ == '__main__':
    main()
