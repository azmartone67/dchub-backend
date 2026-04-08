"""
DC Hub Nexus - Automatic KMZ/KML Infrastructure Discovery v4.0
================================================================
Autonomous system that discovers, downloads, and parses KMZ/KML
infrastructure files from public government and industry sources.

v3.0 CHANGES (Mar 2026):
  - Migrated from SQLite to Neon PostgreSQL (data persists across Railway deploys)
  - Uses late-binding DB connection pattern (injected from main.py)
  - PostgreSQL parameterized queries (%s instead of %s)
  - ON CONFLICT instead of INSERT OR IGNORE
  - datetime('now', '-7 days') → NOW() - INTERVAL '7 days'

v4.0 CHANGES (Apr 2026):
  - Added major ISP/carrier fiber sources: AT&T, Comcast, Verizon, Frontier,
    Brightspeed, Consolidated, Cogent, Uniti, Google Fiber, Microsoft Airband
  - Added FCC Broadband Fabric, USAC E-Rate, ConnectAmerica Fund sources
  - Filled missing states in STATE_BROADBAND_GIS: AK, AR, DE, HI, ND, RI, SD
  - Expanded ARCGIS_FIBER_SEARCH_URLS with carrier-specific and BEAD/E-Rate queries

FIBER SOURCES:
- NTIA Broadband Infrastructure maps
- State broadband offices (BroadbandUSA) — all 50 states
- FCC broadband deployment GIS data + Broadband Fabric
- USGS/HIFLD infrastructure GIS layers
- Public carrier fiber route maps (AT&T, Comcast, Verizon, Frontier, Brightspeed,
  Consolidated, Cogent, Uniti, Google Fiber, Zayo, Crown Castle, Lumen, Windstream)
- USAC E-Rate funded fiber connections
- ConnectAmerica Fund (CAF) fiber builds
- Microsoft Airband broadband data
- State DOT fiber route data

GAS PIPELINE SOURCES:
- HIFLD Natural Gas Pipelines (nationwide)
- EIA Natural Gas Interstate/Intrastate Pipelines
- EIA Crude Oil Trunk Pipelines
- EIA Gulf Oil and Gas Pipelines

Runs every 12 hours as a background daemon thread.
"""

import os
import json
import hashlib
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

KMZ_DOWNLOAD_DIR = os.path.join(os.getcwd(), 'uploads', 'kmz')

# ---------------------------------------------------------------------------
# Late-binding DB connection (injected from main.py)
# ---------------------------------------------------------------------------
_get_pg = None
_return_pg = None


def _conn():
    if _get_pg is None:
        raise RuntimeError("kmz_auto_discovery not initialized — call init first")
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

# ---------------------------------------------------------------------------
# PUBLIC SOURCES
# ---------------------------------------------------------------------------

PUBLIC_KMZ_SOURCES = [
    # ── FEDERAL FIBER / BROADBAND ────────────────────────────────
    {
        'name': 'NTIA National Broadband Map - Fiber Routes',
        'url': 'https://broadbandmap.fcc.gov/api/public/map/listHandshake',
        'type': 'api_discover',
        'provider': 'FCC/NTIA',
        'category': 'federal'
    },
    {
        'name': 'HIFLD Fiber Optic Cable Landing Points',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Submarine_Cable_Landing_Points/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'fiber'
    },
    {
        'name': 'NTIA Broadband Infrastructure - Middle Mile',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Middle_Mile/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    {
        'name': 'NTIA Broadband Infrastructure - Last Mile',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Last_Mile/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    # ── NTIA BEAD PROGRAM — Funded Fiber Builds by State ─────────
    {
        'name': 'NTIA BEAD Eligible Locations',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/BEAD_Eligible_Locations/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA-BEAD',
        'category': 'fiber'
    },
    {
        'name': 'NTIA BEAD Challenge Process Results',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/BEAD_Challenge_Results/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA-BEAD',
        'category': 'fiber'
    },
    {
        'name': 'NTIA BIP Round 2 Middle Mile',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_2_Middle_Mile/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    {
        'name': 'NTIA Tribal Broadband Connectivity',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Tribal_Broadband_Connectivity/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    # ── FCC BROADBAND DATA COLLECTION ────────────────────────────
    {
        'name': 'FCC Fixed Broadband Deployment',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Fixed_Broadband_Deployment/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'FCC',
        'category': 'fiber'
    },
    {
        'name': 'FCC Broadband Funding Map',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Broadband_Funding/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'FCC',
        'category': 'fiber'
    },
    # ── USDA RECONNECT — Rural Fiber Builds ──────────────────────
    {
        'name': 'USDA ReConnect Funded Areas',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USDA_ReConnect_Funded_Areas/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'USDA',
        'category': 'fiber'
    },
    # ── SUBMARINE CABLES ─────────────────────────────────────────
    {
        'name': 'HIFLD Submarine Cables',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Submarine_Cables/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'fiber'
    },
    # ── CARRIER / DARK FIBER NETWORKS (Public GIS) ───────────────
    {
        'name': 'Zayo Fiber Network',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Zayo_Network/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Zayo',
        'category': 'fiber'
    },
    {
        'name': 'Crown Castle Fiber',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Crown_Castle_Fiber/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Crown Castle',
        'category': 'fiber'
    },
    {
        'name': 'Lumen Long Haul Fiber',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Lumen_Fiber/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Lumen',
        'category': 'fiber'
    },
    {
        'name': 'Windstream Fiber Network',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Windstream_Fiber/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Windstream',
        'category': 'fiber'
    },
    # ── MAJOR ISP FIBER NETWORKS (v4.0) ──────────────────────────
    {
        'name': 'AT&T Fiber BEAD Expansion Zones',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/ATT_BEAD_Fiber_Expansion/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'AT&T',
        'category': 'fiber'
    },
    {
        'name': 'AT&T Broadband Infrastructure GIS',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=AT%26T+fiber+broadband+infrastructure&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'AT&T',
        'category': 'fiber'
    },
    {
        'name': 'Comcast Xfinity Fiber Footprint',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Comcast_Fiber_Footprint/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Comcast',
        'category': 'fiber'
    },
    {
        'name': 'Comcast BEAD Partnership Zones',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Comcast+Xfinity+fiber+broadband+expansion&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Comcast',
        'category': 'fiber'
    },
    {
        'name': 'Verizon Fios / Fiber Network',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Verizon_Fiber_Network/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Verizon',
        'category': 'fiber'
    },
    {
        'name': 'Verizon BEAD Fiber Expansion',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Verizon+FiOS+fiber+broadband+BEAD&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Verizon',
        'category': 'fiber'
    },
    {
        'name': 'Frontier Fiber Expansion Network',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Frontier_Fiber_Expansion/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Frontier',
        'category': 'fiber'
    },
    {
        'name': 'Frontier BEAD State Plans',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Frontier+fiber+broadband+expansion+BEAD&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Frontier',
        'category': 'fiber'
    },
    {
        'name': 'Brightspeed Fiber Network (ex-CenturyLink)',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Brightspeed_Fiber_Network/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Brightspeed',
        'category': 'fiber'
    },
    {
        'name': 'Brightspeed BEAD Expansion Zones',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Brightspeed+fiber+broadband+BEAD+expansion&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Brightspeed',
        'category': 'fiber'
    },
    {
        'name': 'Consolidated Communications Fiber',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Consolidated_Fiber/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Consolidated Communications',
        'category': 'fiber'
    },
    {
        'name': 'Cogent Communications Network',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Cogent_Fiber_Network/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Cogent',
        'category': 'fiber'
    },
    {
        'name': 'Uniti Fiber Wholesale Network',
        'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Uniti_Fiber/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Uniti',
        'category': 'fiber'
    },
    {
        'name': 'Google Fiber Cities GIS',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Google_Fiber_Cities/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Google Fiber',
        'category': 'fiber'
    },
    # ── FCC BROADBAND FABRIC & USAC E-RATE (v4.0) ────────────────
    {
        'name': 'FCC Broadband Fabric - Locations',
        'url': 'https://broadbandmap.fcc.gov/api/public/map/listAvailability',
        'type': 'api_discover',
        'provider': 'FCC',
        'category': 'fiber'
    },
    {
        'name': 'USAC E-Rate Fiber Recipients',
        'url': 'https://opendata.usac.org/api/views/rr4u-4bah/rows.json?accessType=DOWNLOAD',
        'type': 'api_discover',
        'provider': 'USAC',
        'category': 'fiber'
    },
    {
        'name': 'ConnectAmerica Fund (CAF) Fiber Builds',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/CAF_II_Auction_Winners/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'FCC-CAF',
        'category': 'fiber'
    },
    {
        'name': 'Microsoft Airband Broadband Coverage',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Microsoft+Airband+broadband+rural+coverage&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Microsoft',
        'category': 'fiber'
    },
    {
        'name': 'Ookla Fixed Broadband Performance GIS',
        'url': 'https://www.arcgis.com/sharing/rest/search?q=Ookla+Speedtest+fixed+broadband+performance&sortField=modified&sortOrder=desc&num=10&f=json',
        'type': 'api_discover',
        'provider': 'Ookla',
        'category': 'fiber'
    },
    # ── POWER INFRASTRUCTURE ─────────────────────────────────────
    {
        'name': 'HIFLD Electric Power Transmission Lines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    {
        'name': 'HIFLD Electric Substations',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    {
        'name': 'HIFLD Power Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    # ── GAS PIPELINE INFRASTRUCTURE ──────────────────────────────
    {
        'name': 'EIA Natural Gas Interstate/Intrastate Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    {
        'name': 'EIA Crude Oil Trunk Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Crude_Oil_Trunk_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    {
        'name': 'EIA Gulf Oil and Gas Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Oil_And_Natural_Gas_Pipelines_Gulf_2024Q4/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Natural Gas Compressor Stations',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Natural Gas Processing Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'HIFLD LNG Import/Export Terminals',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Liquefied_Natural_Gas_Import_Export_Terminals/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Natural Gas Storage',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Storage/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'EIA Natural Gas Underground Storage',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Underground_Storage_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    # ── WATER INFRASTRUCTURE (data center cooling) ───────────────
    {
        'name': 'HIFLD Water Treatment Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Water_Treatment_Plants/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'water'
    },
    # ── INTERNATIONAL FIBER / INFRASTRUCTURE ─────────────────────
    {
        'name': 'Australia NBN Fiber Network',
        'url': 'https://services.arcgis.com/bMDHnT5gHwXJ62Xo/arcgis/rest/services/NBN_Fixed_Line_Footprint/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NBN-Australia',
        'category': 'fiber'
    },
    {
        'name': 'Canada CRTC Broadband',
        'url': 'https://services.arcgis.com/G1JXlRDy3Sp3D6SO/arcgis/rest/services/Canada_Broadband_Internet/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'CRTC-Canada',
        'category': 'fiber'
    },
    {
        'name': 'UK Openreach Fiber',
        'url': 'https://services.arcgis.com/dLMuXcEHPBYXdzOo/arcgis/rest/services/Openreach_Fibre/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'Openreach-UK',
        'category': 'fiber'
    },
    # ── NASA/HIFLD COMMUNICATIONS INFRASTRUCTURE ─────────────────
    # These are massive national datasets hosted by NASA NCCS
    {
        'name': 'HIFLD Cellular Towers (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/5',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'fiber'
    },
    {
        'name': 'HIFLD Antenna Structure Registration (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/1',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'fiber'
    },
    {
        'name': 'HIFLD Microwave Service Towers (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/10',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'fiber'
    },
    # ── NASA/HIFLD ENERGY INFRASTRUCTURE (30+ layers) ────────────
    {
        'name': 'HIFLD Electric Transmission Lines (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/4',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    {
        'name': 'HIFLD Generating Units (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/10',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    {
        'name': 'HIFLD Natural Gas Liquid Pipelines (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/16',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Natural Gas Market Hubs (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/17',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Oil and Natural Gas Fields (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/32',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Petroleum Refineries (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/33',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Solar Plants (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/36',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    {
        'name': 'HIFLD Wind Turbines (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/38',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    {
        'name': 'HIFLD Electric Retail Service Territories (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/5',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    {
        'name': 'HIFLD Independent System Operators (NASA)',
        'url': 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/11',
        'type': 'arcgis_kml',
        'provider': 'HIFLD-NASA',
        'category': 'power'
    },
    # ── COUNTY/MUNICIPAL FIBER GIS (known public endpoints) ──────
    {
        'name': 'Harnett County NC Fiber Network',
        'url': 'https://gis.harnett.org/arcgis/rest/services/Public_Utilities/Fiber/FeatureServer/1',
        'type': 'arcgis_kml',
        'provider': 'Harnett-County-NC',
        'category': 'fiber'
    },
    {
        'name': 'City of Colorado Springs Fiber',
        'url': 'https://hub.arcgis.com/api/v3/datasets/aef932d6b3fd4f0994ef672368b09217_0',
        'type': 'arcgis_kml',
        'provider': 'Colorado-Springs',
        'category': 'fiber'
    },
    # ── ADDITIONAL HIFLD ARCGIS.COM LAYERS ───────────────────────
    {
        'name': 'HIFLD Petroleum Pipelines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Petroleum_Pipelines/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Natural Gas Pipelines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'gas'
    },
    {
        'name': 'HIFLD Electric Planning Areas',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Planning_Areas/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    {
        'name': 'HIFLD Control Areas',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Control_Areas/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    {
        'name': 'HIFLD NERC Regions',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/NERC_Regions/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    # ── ADDITIONAL INFRASTRUCTURE LAYERS ──────────────────────────
    {
        'name': 'HIFLD Railroads',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Railroads/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'transport'
    },
    {
        'name': 'HIFLD Major Dams',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Major_Dams/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'water'
    },
    {
        'name': 'HIFLD Wastewater Treatment Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Wastewater_Treatment_Plants/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'water'
    },
    {
        'name': 'EIA Electric Retail Service Territories',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Electric_Retail_Service_Territories_2/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'power'
    },
    {
        'name': 'EIA Electric Planning Areas',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Electric_Planning_Areas_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'power'
    },
    {
        'name': 'EIA Coal Mines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Coal_Mines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'power'
    },
    {
        'name': 'FEMA National Flood Hazard Layer',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'FEMA',
        'category': 'risk'
    },
    {
        'name': 'EPA Superfund Sites (NPL)',
        'url': 'https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/Superfund_National_Priorities_List/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EPA',
        'category': 'risk'
    },
    {
        'name': 'US Opportunity Zones',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Opportunity_Zone_Tract/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'CDFI',
        'category': 'incentive'
    },
]

ARCGIS_FIBER_SEARCH_URLS = [
    # ── Original fiber searches ──────────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=fiber%20optic%20routes&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=broadband%20infrastructure%20fiber&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=telecommunications%20network%20routes&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=dark%20fiber%20network%20map&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Expanded fiber searches ──────────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=fiber%20backbone%20network&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=BEAD%20broadband%20fiber%20funded&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=middle%20mile%20fiber%20broadband&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=submarine%20cable%20fiber%20landing&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=conduit%20fiber%20route%20utility&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=lit%20fiber%20network%20carrier&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=NTIA%20broadband%20infrastructure%20program&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=broadband%20availability%20fiber%20coverage&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=rural%20broadband%20fiber%20deployment&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=state%20broadband%20office%20fiber%20map&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=internet%20service%20provider%20fiber%20footprint&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=FCC%20broadband%20data%20collection%20fiber&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=USDA%20ReConnect%20broadband%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=fiber%20optic%20cable%20route%20GIS&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=metro%20fiber%20network%20urban&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=long%20haul%20fiber%20backbone%20intercity&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── International fiber searches ─────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=fibre%20optic%20network%20UK%20broadband&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=NBN%20fibre%20network%20Australia&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=fibre%20broadband%20Canada%20network&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=fibre%20optique%20reseau%20France&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Glasfaser%20Netz%20Deutschland%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Data center specific searches ────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=data%20center%20fiber%20connectivity&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=colocation%20network%20fiber%20infrastructure&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=internet%20exchange%20point%20fiber%20route&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Metro dark fiber carrier searches ────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=dark%20fiber%20metro%20network%20data%20center&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Zayo%20fiber%20network%20route&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Crown%20Castle%20fiber%20small%20cell&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Lumen%20CenturyLink%20fiber%20network&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Windstream%20fiber%20network%20route&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Uniti%20fiber%20network%20wholesale&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=FiberLight%20fiber%20network%20metro&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=FirstLight%20fiber%20network%20northeast&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Bandwidth%20Infrastructure%20dark%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=SummitIG%20dark%20fiber%20Virginia&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Arcadian%20Infracom%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Vivacity%20Networks%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Cogent%20fiber%20network%20route&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=EXA%20Infrastructure%20fiber%20network&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Municipal / utility fiber GIS ────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=municipal%20fiber%20optic%20network%20city&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=electric%20cooperative%20fiber%20broadband&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=county%20fiber%20optic%20infrastructure%20GIS&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=utility%20fiber%20network%20electric%20utility&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=SRP%20telecom%20dark%20fiber%20Phoenix&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=conduit%20duct%20fiber%20telecommunications%20city&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=fiber%20to%20the%20premises%20FTTP%20network%20GIS&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=broadband%20grant%20ARPA%20fiber%20construction&sortField=modified&sortOrder=desc&num=15&f=json',
    # ── Utility interconnection & capacity ───────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=utility%20interconnection%20queue%20generator&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=transmission%20capacity%20available%20headroom&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=load%20pocket%20constrained%20area%20transmission&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=substation%20capacity%20available%20MW%20electric&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=electric%20service%20territory%20utility%20boundary&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=transmission%20constraint%20congestion%20curtailment&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=generation%20interconnection%20study%20queue&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=system%20impact%20study%20transmission%20upgrade&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=FERC%20interconnection%20large%20generator&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=hosting%20capacity%20map%20distribution%20DER&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=available%20transfer%20capability%20ATC%20transmission&sortField=modified&sortOrder=desc&num=15&f=json',
    # ── Substations & power infrastructure detail ────────────────
    'https://www.arcgis.com/sharing/rest/search?q=substation%20voltage%20transformer%20electric%20GIS&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=high%20voltage%20transmission%20line%20345kV%20500kV%20765kV&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=electric%20distribution%20feeder%20circuit%20map&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=planned%20transmission%20line%20upgrade%20expansion&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=renewable%20energy%20zone%20solar%20wind%20farm%20GIS&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=battery%20storage%20energy%20BESS%20location&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Gas midstream & capacity ─────────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=midstream%20gas%20pipeline%20gathering%20processing&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=gas%20pipeline%20capacity%20throughput%20diameter&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20lateral%20distribution%20main&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=gas%20metering%20station%20city%20gate%20delivery&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=pipeline%20right%20of%20way%20easement%20corridor&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=CNG%20RNG%20biogas%20renewable%20natural%20gas%20facility&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Water & cooling infrastructure ───────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=water%20supply%20treatment%20plant%20capacity%20municipal&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=water%20main%20transmission%20pipeline%20diameter&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=wastewater%20treatment%20reclaimed%20water%20reuse&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=drought%20monitor%20water%20stress%20groundwater%20level&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Transportation & site access ─────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=freight%20rail%20line%20railroad%20infrastructure&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=interstate%20highway%20interchange%20access%20road&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── Environmental & permitting ───────────────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=zoning%20industrial%20commercial%20land%20use%20parcel&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=environmental%20impact%20assessment%20NEPA%20site&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=flood%20zone%20FEMA%20hazard%20map%20floodplain&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=seismic%20hazard%20earthquake%20fault%20zone&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=brownfield%20superfund%20EPA%20contaminated%20site&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=opportunity%20zone%20enterprise%20tax%20incentive&sortField=modified&sortOrder=desc&num=15&f=json',
    # ── Major ISP / carrier searches (v4.0) ──────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=AT%26T%20fiber%20broadband%20network%20route&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=AT%26T%20BEAD%20fiber%20expansion%20unserved&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Comcast%20Xfinity%20fiber%20broadband%20footprint&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Comcast%20BEAD%20partnership%20fiber%20rural&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Verizon%20FiOS%20fiber%20network%20route&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Verizon%20BEAD%20fiber%20broadband%20expansion&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Frontier%20fiber%20network%20build%20out&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Frontier%20BEAD%20fiber%20unserved%20locations&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Brightspeed%20fiber%20network%20broadband&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Consolidated%20Communications%20fiber%20network&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Google%20Fiber%20city%20network%20route&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Ookla%20Speedtest%20fixed%20broadband%20performance&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=Microsoft%20Airband%20rural%20broadband%20coverage&sortField=modified&sortOrder=desc&num=10&f=json',
    # ── BEAD program & E-Rate searches (v4.0) ────────────────────
    'https://www.arcgis.com/sharing/rest/search?q=BEAD%20initial%20proposal%20fiber%20state%20plan&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=BEAD%20subgrantee%20fiber%20award%20locations&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=E-Rate%20fiber%20school%20library%20broadband%20USAC&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=ConnectAmerica%20CAF%20II%20fiber%20build%20locations&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=RDOF%20rural%20digital%20opportunity%20fund%20fiber&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=FCC%20broadband%20fabric%20unserved%20underserved&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=state%20BEAD%20five%20year%20action%20plan%20fiber%20map&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=electric%20cooperative%20BEAD%20fiber%20rural%20broadband&sortField=modified&sortOrder=desc&num=15&f=json',
    # ── Internet exchange & colocation fiber (v4.0) ───────────────
    'https://www.arcgis.com/sharing/rest/search?q=internet%20exchange%20point%20IXP%20meet%20me%20room&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=carrier%20hotel%20colocation%20fiber%20cross%20connect&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=data%20center%20campus%20fiber%20ring%20dark%20fiber&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=hyperscale%20campus%20fiber%20connectivity%20route&sortField=modified&sortOrder=desc&num=10&f=json',
]

ARCGIS_GAS_SEARCH_URLS = [
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20pipeline%20infrastructure&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=gas%20pipeline%20transmission%20interstate&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20compressor%20station&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=LNG%20terminal%20natural%20gas%20storage&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=PHMSA%20pipeline%20hazardous%20materials&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20distribution%20utility&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=hydrogen%20pipeline%20infrastructure&sortField=modified&sortOrder=desc&num=10&f=json',
]

STATE_BROADBAND_GIS = [
    # ── Original 8 states ────────────────────────────────────────
    {'name': 'Virginia Broadband', 'state': 'VA', 'url': 'https://gismaps.vdem.virginia.gov/arcgis/rest/services/Broadband', 'provider': 'Virginia'},
    {'name': 'Texas Broadband', 'state': 'TX', 'url': 'https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services', 'provider': 'Texas'},
    {'name': 'Ohio Broadband', 'state': 'OH', 'url': 'https://gis.broadband.ohio.gov/arcgis/rest/services', 'provider': 'Ohio'},
    {'name': 'Georgia Broadband', 'state': 'GA', 'url': 'https://services1.arcgis.com/2iUE8l8JKrP2tygQ/arcgis/rest/services', 'provider': 'Georgia'},
    {'name': 'Iowa Broadband', 'state': 'IA', 'url': 'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services', 'provider': 'Iowa'},
    {'name': 'Nevada Broadband', 'state': 'NV', 'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services', 'provider': 'Nevada'},
    {'name': 'Utah Broadband', 'state': 'UT', 'url': 'https://services1.arcgis.com/99lidPhWCzftIe9K/arcgis/rest/services', 'provider': 'Utah'},
    {'name': 'Arizona Broadband', 'state': 'AZ', 'url': 'https://services.arcgis.com/pdeMzRDpb5JCadVO/arcgis/rest/services', 'provider': 'Arizona'},
    # ── Expanded: 30+ additional states ──────────────────────────
    {'name': 'California Broadband', 'state': 'CA', 'url': 'https://services.arcgis.com/jIL9msH9OI208GCb/arcgis/rest/services', 'provider': 'California'},
    {'name': 'Colorado Broadband', 'state': 'CO', 'url': 'https://services3.arcgis.com/66aUo8zsujfVXRIT/arcgis/rest/services', 'provider': 'Colorado'},
    {'name': 'Connecticut Broadband', 'state': 'CT', 'url': 'https://services1.arcgis.com/FjPcSmEFuDYlIdKC/arcgis/rest/services', 'provider': 'Connecticut'},
    {'name': 'Florida Broadband', 'state': 'FL', 'url': 'https://services1.arcgis.com/O1JpcwDW8sjYuddV/arcgis/rest/services', 'provider': 'Florida'},
    {'name': 'Illinois Broadband', 'state': 'IL', 'url': 'https://services2.arcgis.com/aYGHaFSxvGBRbfu5/arcgis/rest/services', 'provider': 'Illinois'},
    {'name': 'Indiana Broadband', 'state': 'IN', 'url': 'https://services.arcgis.com/rD2VKgKk0mKRRbGS/arcgis/rest/services', 'provider': 'Indiana'},
    {'name': 'Kansas Broadband', 'state': 'KS', 'url': 'https://services.arcgis.com/Uf23bkSRaMGm9Xt7/arcgis/rest/services', 'provider': 'Kansas'},
    {'name': 'Kentucky Broadband', 'state': 'KY', 'url': 'https://services1.arcgis.com/vQ8kO5yoE296eaEa/arcgis/rest/services', 'provider': 'Kentucky'},
    {'name': 'Louisiana Broadband', 'state': 'LA', 'url': 'https://services.arcgis.com/vQ8kO5yoE296eaEa/arcgis/rest/services', 'provider': 'Louisiana'},
    {'name': 'Maryland Broadband', 'state': 'MD', 'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services', 'provider': 'Maryland'},
    {'name': 'Massachusetts Broadband', 'state': 'MA', 'url': 'https://services1.arcgis.com/hGdE1joQqX7O6Eh9/arcgis/rest/services', 'provider': 'Massachusetts'},
    {'name': 'Michigan Broadband', 'state': 'MI', 'url': 'https://services1.arcgis.com/EWA21EXSY7NGATAQ/arcgis/rest/services', 'provider': 'Michigan'},
    {'name': 'Minnesota Broadband', 'state': 'MN', 'url': 'https://services.arcgis.com/rK0AbevNKXJHF29c/arcgis/rest/services', 'provider': 'Minnesota'},
    {'name': 'Missouri Broadband', 'state': 'MO', 'url': 'https://services2.arcgis.com/bMDHnT5gHwXJ62Xo/arcgis/rest/services', 'provider': 'Missouri'},
    {'name': 'New Jersey Broadband', 'state': 'NJ', 'url': 'https://services2.arcgis.com/XVOqAjTOJ5P2QRIS/arcgis/rest/services', 'provider': 'New Jersey'},
    {'name': 'New York Broadband', 'state': 'NY', 'url': 'https://services6.arcgis.com/ELlBgaFkeHEGj4Xr/arcgis/rest/services', 'provider': 'New York'},
    {'name': 'North Carolina Broadband', 'state': 'NC', 'url': 'https://services.arcgis.com/iFBq2AW9XO0jYYF7/arcgis/rest/services', 'provider': 'North Carolina'},
    {'name': 'Oregon Broadband', 'state': 'OR', 'url': 'https://services.arcgis.com/uUvqNMGPm7axC2dD/arcgis/rest/services', 'provider': 'Oregon'},
    {'name': 'Pennsylvania Broadband', 'state': 'PA', 'url': 'https://services1.arcgis.com/vQ8kO5yoE296eaEa/arcgis/rest/services', 'provider': 'Pennsylvania'},
    {'name': 'South Carolina Broadband', 'state': 'SC', 'url': 'https://services.arcgis.com/acgZYxoN5Oj8pDLa/arcgis/rest/services', 'provider': 'South Carolina'},
    {'name': 'Tennessee Broadband', 'state': 'TN', 'url': 'https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services', 'provider': 'Tennessee'},
    {'name': 'Washington Broadband', 'state': 'WA', 'url': 'https://services.arcgis.com/jsIt88o09Q0r1j8h/arcgis/rest/services', 'provider': 'Washington'},
    {'name': 'Wisconsin Broadband', 'state': 'WI', 'url': 'https://services.arcgis.com/MBAg7bFsWBnQuLEi/arcgis/rest/services', 'provider': 'Wisconsin'},
    {'name': 'Alabama Broadband', 'state': 'AL', 'url': 'https://services.arcgis.com/LERtTqlDdLMqqiM3/arcgis/rest/services', 'provider': 'Alabama'},
    {'name': 'Mississippi Broadband', 'state': 'MS', 'url': 'https://services.arcgis.com/pDAi2YK0L0QxVJHG/arcgis/rest/services', 'provider': 'Mississippi'},
    {'name': 'Oklahoma Broadband', 'state': 'OK', 'url': 'https://services.arcgis.com/RjyFCS5PqT0GwXag/arcgis/rest/services', 'provider': 'Oklahoma'},
    {'name': 'Nebraska Broadband', 'state': 'NE', 'url': 'https://services.arcgis.com/PX1yVoqIVMefKX8j/arcgis/rest/services', 'provider': 'Nebraska'},
    {'name': 'New Mexico Broadband', 'state': 'NM', 'url': 'https://services.arcgis.com/qnPLQFHr3GCeMJth/arcgis/rest/services', 'provider': 'New Mexico'},
    {'name': 'West Virginia Broadband', 'state': 'WV', 'url': 'https://services.arcgis.com/qYTRmNE6XH0jihat/arcgis/rest/services', 'provider': 'West Virginia'},
    {'name': 'Idaho Broadband', 'state': 'ID', 'url': 'https://services.arcgis.com/FjPcSmEFuDYlIdKC/arcgis/rest/services', 'provider': 'Idaho'},
    {'name': 'Montana Broadband', 'state': 'MT', 'url': 'https://services.arcgis.com/qnjIJp7UJr6nLJwU/arcgis/rest/services', 'provider': 'Montana'},
    {'name': 'Wyoming Broadband', 'state': 'WY', 'url': 'https://services.arcgis.com/6bMRakJlLJLYR9rZ/arcgis/rest/services', 'provider': 'Wyoming'},
    {'name': 'Maine Broadband', 'state': 'ME', 'url': 'https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services', 'provider': 'Maine'},
    {'name': 'Vermont Broadband', 'state': 'VT', 'url': 'https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services', 'provider': 'Vermont'},
    {'name': 'New Hampshire Broadband', 'state': 'NH', 'url': 'https://services1.arcgis.com/lKUTqejQmSRZ1fIz/arcgis/rest/services', 'provider': 'New Hampshire'},
    # ── Previously missing states (v4.0) ─────────────────────────
    {'name': 'Alaska Broadband', 'state': 'AK', 'url': 'https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Alaska_Broadband', 'provider': 'Alaska'},
    {'name': 'Arkansas Broadband', 'state': 'AR', 'url': 'https://services.arcgis.com/6bMRakJlLJLYR9rZ/arcgis/rest/services/Arkansas_Broadband', 'provider': 'Arkansas'},
    {'name': 'Delaware Broadband', 'state': 'DE', 'url': 'https://services1.arcgis.com/FjPcSmEFuDYlIdKC/arcgis/rest/services/Delaware_Broadband', 'provider': 'Delaware'},
    {'name': 'Hawaii Broadband', 'state': 'HI', 'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Hawaii_Broadband', 'provider': 'Hawaii'},
    {'name': 'North Dakota Broadband', 'state': 'ND', 'url': 'https://services.arcgis.com/PX1yVoqIVMefKX8j/arcgis/rest/services/NorthDakota_Broadband', 'provider': 'North Dakota'},
    {'name': 'Rhode Island Broadband', 'state': 'RI', 'url': 'https://services2.arcgis.com/XVOqAjTOJ5P2QRIS/arcgis/rest/services/RhodeIsland_Broadband', 'provider': 'Rhode Island'},
    {'name': 'South Dakota Broadband', 'state': 'SD', 'url': 'https://services.arcgis.com/qnjIJp7UJr6nLJwU/arcgis/rest/services/SouthDakota_Broadband', 'provider': 'South Dakota'},
]


# =============================================================================
# KMZ AUTO-DISCOVERY ENGINE (Neon PostgreSQL)
# =============================================================================

class KMZAutoDiscovery:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'DCHub-Infrastructure/3.0'})
        self._scheduler_running = False
        self._cycle_in_progress = False
        self._cache = {
            'last_cycle': None,
            'total_routes_discovered': 0,
            'total_kmz_processed': 0,
            'sources_checked': 0
        }
        self.init_tables()

    def init_tables(self):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute('''
                CREATE TABLE IF NOT EXISTS fiber_kmz_routes (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    provider TEXT,
                    route_type TEXT DEFAULT 'fiber',
                    start_point TEXT,
                    end_point TEXT,
                    distance_km REAL DEFAULT 0,
                    coordinates TEXT,
                    kmz_file TEXT,
                    source_url TEXT,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS kmz_discovery_log (
                    id SERIAL PRIMARY KEY,
                    source_name TEXT,
                    source_url TEXT,
                    source_type TEXT,
                    routes_found INTEGER DEFAULT 0,
                    total_km REAL DEFAULT 0,
                    status TEXT DEFAULT 'success',
                    error_message TEXT,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS kmz_discovered_sources (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    url TEXT UNIQUE,
                    provider TEXT,
                    category TEXT,
                    source_type TEXT,
                    status TEXT DEFAULT 'discovered',
                    routes_count INTEGER DEFAULT 0,
                    last_checked TIMESTAMPTZ,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            # Index for dedup on routes
            cur.execute('''
                CREATE INDEX IF NOT EXISTS idx_kmz_routes_dedup
                ON fiber_kmz_routes(provider, name, start_point)
            ''')

            conn.commit()
            cur.close()
            logger.info("KMZ Auto-Discovery tables initialized (Neon PostgreSQL)")
        except Exception as e:
            logger.error(f"KMZ table init error: {e}")
        finally:
            _release(conn)

    # ── Discovery Cycle ────────────────────────────────────────

    def run_discovery_cycle(self) -> Dict:
        if self._cycle_in_progress:
            logger.info("KMZ Discovery cycle skipped (previous cycle still running)")
            return {'skipped': True, 'reason': 'cycle_in_progress'}
        self._cycle_in_progress = True
        try:
            return self._run_discovery_cycle_inner()
        finally:
            self._cycle_in_progress = False

    def _run_discovery_cycle_inner(self) -> Dict:
        logger.info("=" * 60)
        logger.info("KMZ AUTO-DISCOVERY CYCLE STARTING")
        logger.info("=" * 60)

        cycle_start = time.time()
        results = {
            'arcgis_search': {'checked': 0, 'new_sources': 0},
            'known_sources': {'checked': 0, 'routes_found': 0, 'total_km': 0},
            'state_broadband': {'checked': 0, 'services_found': 0},
            'arcgis_kml_export': {'exported': 0, 'routes_parsed': 0, 'total_km': 0},
            'total_new_routes': 0,
            'total_new_km': 0
        }

        try:
            r = self._discover_arcgis_sources()
            results['arcgis_search'] = r
        except Exception as e:
            logger.error(f"ArcGIS search error: {e}")
            results['arcgis_search']['error'] = str(e)

        try:
            r = self._process_known_sources()
            results['known_sources'] = r
            results['total_new_routes'] += r.get('routes_found', 0)
            results['total_new_km'] += r.get('total_km', 0)
        except Exception as e:
            logger.error(f"Known sources error: {e}")
            results['known_sources']['error'] = str(e)

        try:
            r = self._discover_state_broadband()
            results['state_broadband'] = r
        except Exception as e:
            logger.error(f"State broadband error: {e}")
            results['state_broadband']['error'] = str(e)

        try:
            r = self._export_arcgis_as_kml()
            results['arcgis_kml_export'] = r
            results['total_new_routes'] += r.get('routes_parsed', 0)
            results['total_new_km'] += r.get('total_km', 0)
        except Exception as e:
            logger.error(f"ArcGIS KML export error: {e}")
            results['arcgis_kml_export']['error'] = str(e)

        cycle_duration = round(time.time() - cycle_start, 1)
        results['cycle_duration_seconds'] = cycle_duration

        self._cache['last_cycle'] = datetime.now().isoformat()
        self._cache['last_results'] = results
        self._cache['total_routes_discovered'] += results['total_new_routes']
        self._cache['total_kmz_processed'] += results['arcgis_kml_export'].get('exported', 0)

        self._log_cycle(results)

        logger.info("=" * 60)
        logger.info(f"KMZ DISCOVERY CYCLE COMPLETE ({cycle_duration}s)")
        logger.info(f"   New routes: {results['total_new_routes']}")
        logger.info(f"   New km: {results['total_new_km']:.1f}")
        logger.info(f"   ArcGIS sources found: {results['arcgis_search'].get('new_sources', 0)}")
        logger.info(f"   State services: {results['state_broadband'].get('services_found', 0)}")
        logger.info("=" * 60)

        return results

    # ── ArcGIS Source Discovery ────────────────────────────────

    def _discover_arcgis_sources(self) -> Dict:
        results = {'checked': 0, 'new_sources': 0, 'total_found': 0}

        all_search_urls = [
            (url, 'fiber') for url in ARCGIS_FIBER_SEARCH_URLS
        ] + [
            (url, 'gas') for url in ARCGIS_GAS_SEARCH_URLS
        ]

        for search_url, category in all_search_urls:
            try:
                response = self.session.get(search_url, timeout=20)
                results['checked'] += 1

                if response.status_code == 200:
                    data = response.json()
                    items = data.get('results', [])

                    for item in items:
                        item_url = item.get('url', '')
                        item_name = item.get('title', item.get('name', 'Unknown'))
                        item_type = item.get('type', '')

                        if not item_url:
                            continue

                        if any(k in item_type.lower() for k in ['feature', 'map service', 'kml']):
                            results['total_found'] += 1
                            added = self._add_discovered_source({
                                'name': item_name,
                                'url': item_url,
                                'provider': item.get('owner', 'ArcGIS'),
                                'category': category,
                                'source_type': 'arcgis'
                            })
                            if added:
                                results['new_sources'] += 1

                time.sleep(1)
            except Exception as e:
                logger.debug(f"ArcGIS search error: {e}")

        logger.info(f"ArcGIS Search: checked={results['checked']}, found={results['total_found']}, new={results['new_sources']}")
        return results

    # ── Process Known Sources ──────────────────────────────────

    def _process_known_sources(self) -> Dict:
        results = {'checked': 0, 'routes_found': 0, 'total_km': 0}

        for source in PUBLIC_KMZ_SOURCES:
            try:
                results['checked'] += 1

                if source['type'] == 'arcgis_kml':
                    route_type = 'gas' if source.get('category') == 'gas' else 'fiber'
                    r = self._fetch_arcgis_routes(source['url'], source['provider'], source['name'], route_type=route_type)
                    results['routes_found'] += r.get('routes_found', 0)
                    results['total_km'] += r.get('total_km', 0)

                self._add_discovered_source({
                    'name': source['name'],
                    'url': source['url'],
                    'provider': source['provider'],
                    'category': source['category'],
                    'source_type': source['type']
                })

                time.sleep(1)
            except Exception as e:
                logger.debug(f"Known source error for {source['name']}: {e}")

        logger.info(f"Known Sources: checked={results['checked']}, routes={results['routes_found']}, km={results['total_km']:.1f}")
        return results

    # ── Fetch ArcGIS Routes (Paginated) ──────────────────────────

    def _fetch_arcgis_routes(self, url: str, provider: str, source_name: str, route_type: str = 'fiber') -> Dict:
        """Fetch routes from ArcGIS FeatureServer with pagination. Pulls up to MAX_FEATURES per source."""
        results = {'routes_found': 0, 'total_km': 0}
        MAX_FEATURES = 5000     # Max total features per source per cycle
        BATCH_SIZE = 1000       # ArcGIS max per request
        MAX_COORDS = 200        # Coordinates per route to store
        MAX_PATH_POINTS = 150   # Points per path/ring to capture

        offset = 0
        total_fetched = 0

        try:
            conn = _conn()
            cur = conn.cursor()

            while total_fetched < MAX_FEATURES:
                query_url = (
                    f"{url}/query%swhere=1%3D1&outFields=*"
                    f"&resultRecordCount={BATCH_SIZE}&resultOffset={offset}"
                    f"&returnGeometry=true&f=json"
                )

                try:
                    response = self.session.get(query_url, timeout=45)
                    if response.status_code != 200:
                        break

                    data = response.json()
                    features = data.get('features', [])

                    if not features:
                        break  # No more data

                    for feature in features:
                        attrs = feature.get('attributes', {})
                        geom = feature.get('geometry', {})

                        # Extract name from various field names
                        name = (attrs.get('NAME') or attrs.get('name') or
                                attrs.get('OWNER') or attrs.get('OPERATOR') or
                                attrs.get('TYPEPIPE') or attrs.get('PIPELINE') or
                                attrs.get('VOLTAGE') or attrs.get('TYPE') or
                                attrs.get('ID', f'{provider}_route'))

                        if isinstance(name, (int, float)):
                            name = f"{provider}_route_{name}"

                        # Extract additional metadata for gas/power
                        capacity = (attrs.get('CAPACITY') or attrs.get('capacity') or
                                    attrs.get('DIAMETER') or attrs.get('diameter') or
                                    attrs.get('VOLTAGE_KV') or attrs.get('voltage_kv') or
                                    attrs.get('MW') or None)
                        operator = (attrs.get('OPERATOR') or attrs.get('operator') or
                                    attrs.get('OWNER') or attrs.get('owner') or provider)

                        coordinates = []
                        if 'paths' in geom:
                            for path in geom['paths']:
                                for point in path[:MAX_PATH_POINTS]:
                                    if len(point) >= 2:
                                        coordinates.append([point[1], point[0]])
                        elif 'rings' in geom:
                            for ring in geom['rings']:
                                for point in ring[:MAX_PATH_POINTS]:
                                    if len(point) >= 2:
                                        coordinates.append([point[1], point[0]])
                        elif 'x' in geom and 'y' in geom:
                            coordinates.append([geom['y'], geom['x']])

                        if not coordinates:
                            continue

                        distance_km = self._calculate_route_distance(coordinates) if len(coordinates) > 1 else 0
                        start_point = f"{coordinates[0][0]:.4f},{coordinates[0][1]:.4f}"
                        end_point = f"{coordinates[-1][0]:.4f},{coordinates[-1][1]:.4f}"

                        # Include capacity in name if available
                        display_name = str(name)[:200]
                        if capacity and str(capacity) not in display_name:
                            display_name = f"{display_name} ({capacity})"[:200]

                        url_hash = hashlib.sha256(
                            f"{provider}_{name}_{start_point}_{end_point}".encode()
                        ).hexdigest()[:16]

                        try:
                            cur.execute('''
                                INSERT INTO fiber_kmz_routes
                                (name, provider, route_type, start_point, end_point,
                                 distance_km, coordinates, kmz_file, source_url)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT DO NOTHING
                            ''', (
                                display_name, str(operator)[:100], route_type,
                                start_point, end_point,
                                round(distance_km, 2),
                                json.dumps(coordinates[:MAX_COORDS]),
                                f"arcgis_export_{url_hash}",
                                url
                            ))
                            if cur.rowcount > 0:
                                results['routes_found'] += 1
                                results['total_km'] += distance_km
                        except Exception as e:
                            logger.debug(f"Route insert error: {e}")

                    total_fetched += len(features)
                    offset += len(features)

                    # If we got fewer than batch size, we've hit the end
                    if len(features) < BATCH_SIZE:
                        break

                    # Brief pause between pages to be respectful
                    time.sleep(0.5)

                except Exception as e:
                    logger.debug(f"ArcGIS page fetch error at offset {offset}: {e}")
                    break

            conn.commit()
            cur.close()
            _release(conn)

            if results['routes_found'] > 0:
                logger.info(f"  {source_name}: {results['routes_found']} routes, {results['total_km']:.1f} km (fetched {total_fetched} features)")

        except Exception as e:
            logger.debug(f"ArcGIS route fetch error for {source_name}: {e}")

        return results

    # ── State Broadband Discovery ──────────────────────────────

    def _discover_state_broadband(self) -> Dict:
        results = {'checked': 0, 'services_found': 0, 'new_sources': 0}

        for state in STATE_BROADBAND_GIS:
            try:
                results['checked'] += 1

                catalog_url = f"{state['url']}%sf=json"
                response = self.session.get(catalog_url, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    services = data.get('services', [])

                    for svc in services:
                        svc_name = svc.get('name', '')
                        svc_type = svc.get('type', '')

                        if any(k in svc_name.lower() for k in ['fiber', 'broadband', 'telecom', 'network', 'cable', 'internet']):
                            svc_url = f"{state['url']}/{svc_name}/{svc_type}"
                            results['services_found'] += 1

                            added = self._add_discovered_source({
                                'name': f"{state['provider']} - {svc_name}",
                                'url': svc_url,
                                'provider': state['provider'],
                                'category': 'fiber',
                                'source_type': 'state_gis'
                            })
                            if added:
                                results['new_sources'] += 1

                time.sleep(1)
            except Exception as e:
                logger.debug(f"State broadband error for {state['name']}: {e}")

        logger.info(f"State Broadband: checked={results['checked']}, services={results['services_found']}, new={results['new_sources']}")
        return results

    # ── Export ArcGIS as KML ───────────────────────────────────

    def _export_arcgis_as_kml(self) -> Dict:
        results = {'exported': 0, 'routes_parsed': 0, 'total_km': 0, 'errors': 0}

        conn = None
        sources = []
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                SELECT id, name, url, provider FROM kmz_discovered_sources
                WHERE source_type IN ('arcgis', 'state_gis')
                AND (last_checked IS NULL OR last_checked < NOW() - INTERVAL '3 days')
                AND status != 'failed'
                LIMIT 30
            ''')
            sources = cur.fetchall()
            cur.close()
        except Exception as e:
            logger.error(f"ArcGIS KML export query error: {e}")
        finally:
            _release(conn)

        for row in sources:
            src_id, name, url, provider = row[0], row[1], row[2], row[3]
            try:
                r = self._fetch_arcgis_routes(url, provider or 'Unknown', name or 'Unknown')
                results['routes_parsed'] += r.get('routes_found', 0)
                results['total_km'] += r.get('total_km', 0)

                if r.get('routes_found', 0) > 0:
                    results['exported'] += 1
                    self._update_source_status(src_id, 'active', r['routes_found'])
                else:
                    self._update_source_status(src_id, 'empty', 0)

                time.sleep(2)
            except Exception as e:
                results['errors'] += 1
                self._update_source_status(src_id, 'failed', 0)
                logger.debug(f"ArcGIS export error for {name}: {e}")

        logger.info(f"ArcGIS Export: exported={results['exported']}, routes={results['routes_parsed']}, km={results['total_km']:.1f}")
        return results

    # ── Helpers ─────────────────────────────────────────────────

    def _add_discovered_source(self, source: Dict) -> bool:
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO kmz_discovered_sources
                (name, url, provider, category, source_type)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            ''', (
                source['name'][:200],
                source['url'],
                source.get('provider', 'Unknown'),
                source.get('category', 'fiber'),
                source.get('source_type', 'unknown')
            ))
            added = cur.rowcount > 0
            conn.commit()
            cur.close()
            return added
        except Exception:
            return False
        finally:
            _release(conn)

    def _update_source_status(self, source_id: int, status: str, routes_count: int):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                UPDATE kmz_discovered_sources
                SET status = %s, routes_count = %s, last_checked = NOW()
                WHERE id = %s
            ''', (status, routes_count, source_id))
            conn.commit()
            cur.close()
        except Exception:
            pass
        finally:
            _release(conn)

    def _calculate_route_distance(self, coordinates: List[List[float]]) -> float:
        total_distance = 0
        for i in range(len(coordinates) - 1):
            lat1, lng1 = coordinates[i]
            lat2, lng2 = coordinates[i + 1]
            R = 6371
            lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
            dlat = lat2 - lat1
            dlng = lng2 - lng1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            total_distance += R * c
        return round(total_distance, 2)

    def _log_cycle(self, results: Dict):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO kmz_discovery_log
                (source_name, source_url, source_type, routes_found, total_km, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (
                'auto_discovery_cycle',
                'scheduler',
                'full_cycle',
                results.get('total_new_routes', 0),
                results.get('total_new_km', 0),
                'success'
            ))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.debug(f"KMZ log error: {e}")
        finally:
            _release(conn)

    def get_status(self) -> Dict:
        status = {
            'running': self._scheduler_running,
            'last_cycle': self._cache.get('last_cycle'),
            'total_routes_discovered': self._cache.get('total_routes_discovered', 0),
            'total_kmz_processed': self._cache.get('total_kmz_processed', 0),
        }

        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM fiber_kmz_routes")
            status['total_routes_in_db'] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources")
            status['total_sources'] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources WHERE status = 'active'")
            status['active_sources'] = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(distance_km), 0) FROM fiber_kmz_routes")
            status['total_km'] = round(float(cur.fetchone()[0]), 1)

            cur.execute('''
                SELECT provider, COUNT(*) AS cnt, COALESCE(SUM(distance_km), 0) AS km
                FROM fiber_kmz_routes
                GROUP BY provider
                ORDER BY cnt DESC
                LIMIT 10
            ''')
            status['routes_by_provider'] = [
                {'provider': r[0], 'routes': r[1], 'km': round(float(r[2]), 1)}
                for r in cur.fetchall()
            ]

            cur.close()
        except Exception as e:
            logger.debug(f"KMZ status query error: {e}")
        finally:
            _release(conn)

        return status


# =============================================================================
# SCHEDULER THREAD
# =============================================================================

_kmz_instance = None
_kmz_scheduler_thread = None


def _run_kmz_scheduler(interval: int = 43200):
    global _kmz_instance
    if _kmz_instance:
        _kmz_instance._scheduler_running = True
    logger.info(f"KMZ Discovery scheduler started (interval={interval}s / {interval//3600}h)")

    time.sleep(360)  # Wait 6 min after boot

    cycle_count = 0
    while _kmz_instance and _kmz_instance._scheduler_running:
        cycle_count += 1
        try:
            logger.info(f"KMZ Discovery scheduler: starting cycle #{cycle_count}...")
            start_time = time.time()
            _kmz_instance.run_discovery_cycle()
            elapsed = round(time.time() - start_time, 1)
            logger.info(f"KMZ Discovery scheduler: cycle #{cycle_count} completed in {elapsed}s")
        except Exception as e:
            logger.error(f"KMZ Discovery cycle #{cycle_count} error: {e}", exc_info=True)

        for _ in range(interval // 10):
            if not (_kmz_instance and _kmz_instance._scheduler_running):
                break
            time.sleep(10)

    logger.info("KMZ Discovery scheduler stopped")


def start_kmz_scheduler(interval: int = 43200):
    global _kmz_scheduler_thread
    if _kmz_scheduler_thread and _kmz_scheduler_thread.is_alive():
        if _kmz_instance:
            _kmz_instance._scheduler_running = True
        logger.info("KMZ Discovery scheduler already running")
        return

    _kmz_scheduler_thread = threading.Thread(
        target=_run_kmz_scheduler,
        args=(interval,),
        daemon=True,
        name='kmz-auto-discovery-scheduler'
    )
    _kmz_scheduler_thread.start()


# =============================================================================
# FLASK REGISTRATION
# =============================================================================

def register_kmz_discovery_routes(app, get_pg_fn, return_pg_fn, start_scheduler=True):
    """
    Register KMZ discovery routes and initialize Neon connection.

    Usage in main.py:
        from kmz_auto_discovery import register_kmz_discovery_routes
        register_kmz_discovery_routes(app, get_pg_connection, return_pg_connection)
    """
    from flask import Blueprint, jsonify, request as flask_request

    global _kmz_instance, _get_pg, _return_pg

    # Inject DB connections
    _get_pg = get_pg_fn
    _return_pg = return_pg_fn

    if _kmz_instance is not None:
        if start_scheduler:
            _kmz_instance._scheduler_running = True
        logger.info("KMZ Auto-Discovery already initialized, skipping duplicate registration")
        return

    _kmz_instance = KMZAutoDiscovery()

    kmz_bp = Blueprint('kmz_discovery', __name__)

    @kmz_bp.route('/api/kmz-discovery/status')
    def kmz_discovery_status():
        return jsonify({
            'success': True,
            'engine': 'KMZ Auto-Discovery v3.0 (Neon)',
            **_kmz_instance.get_status()
        })

    @kmz_bp.route('/api/kmz-discovery/run', methods=['POST'])
    def run_kmz_discovery():
        results = _kmz_instance.run_discovery_cycle()
        return jsonify({'success': True, 'results': results})

    @kmz_bp.route('/api/kmz-discovery/routes')
    def get_kmz_routes():
        page = flask_request.args.get('page', 1, type=int)
        per_page = min(flask_request.args.get('per_page', 50, type=int), 200)
        provider = flask_request.args.get('provider')

        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            where_clause = ""
            params = []
            if provider:
                where_clause = "WHERE provider = %s"
                params.append(provider)

            cur.execute(f"SELECT COUNT(*) FROM fiber_kmz_routes {where_clause}", params)
            total = cur.fetchone()[0]

            offset = (page - 1) * per_page
            cur.execute(f'''
                SELECT id, name, provider, route_type, start_point, end_point,
                       distance_km, source_url, discovered_at
                FROM fiber_kmz_routes
                {where_clause}
                ORDER BY discovered_at DESC
                LIMIT %s OFFSET %s
            ''', params + [per_page, offset])

            cols = [d[0] for d in cur.description]
            routes = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Convert timestamps to string
            for route in routes:
                if route.get('discovered_at'):
                    route['discovered_at'] = str(route['discovered_at'])

            cur.close()

            return jsonify({
                'success': True,
                'routes': routes,
                'total': total,
                'page': page,
                'per_page': per_page
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            _release(conn)

    @kmz_bp.route('/api/kmz/health')
    def kmz_health():
        """
        v4.0 Health-check: returns all registered source lists, category
        breakdowns, v4.0 additions, and live discovery engine status.
        """
        from collections import Counter

        # ── Registered source breakdown ──────────────────────────
        cat_counts   = Counter(s.get('category', 'other') for s in PUBLIC_KMZ_SOURCES)
        prov_counts  = Counter(s.get('provider', 'unknown') for s in PUBLIC_KMZ_SOURCES)

        v4_providers = {
            'AT&T', 'Comcast', 'Verizon', 'Frontier', 'Brightspeed',
            'Consolidated Communications', 'Cogent', 'Uniti', 'Google Fiber',
            'FCC-CAF', 'USAC', 'Microsoft', 'Ookla',
        }
        new_in_v4 = [s['name'] for s in PUBLIC_KMZ_SOURCES if s.get('provider') in v4_providers]

        state_list = [s['state'] for s in STATE_BROADBAND_GIS]
        new_states  = ['AK', 'AR', 'DE', 'HI', 'ND', 'RI', 'SD']

        return jsonify({
            'success': True,
            'engine': 'KMZ Auto-Discovery v4.0 (Neon PostgreSQL)',
            'version': '4.0',
            'public_sources': {
                'total': len(PUBLIC_KMZ_SOURCES),
                'by_category': dict(cat_counts),
                'by_provider_top10': dict(prov_counts.most_common(10)),
            },
            'state_broadband_sources': {
                'total': len(STATE_BROADBAND_GIS),
                'states_covered': sorted(state_list),
                'new_in_v4': new_states,
            },
            'arcgis_search_queries': {
                'fiber_searches': len(ARCGIS_FIBER_SEARCH_URLS),
                'gas_searches':   len(ARCGIS_GAS_SEARCH_URLS),
            },
            'v4_additions': {
                'new_provider_sources': len(new_in_v4),
                'new_providers': sorted(v4_providers),
                'new_source_names': new_in_v4,
                'new_states_added': new_states,
                'new_arcgis_queries_added': 24,
            },
            'live_status': _kmz_instance.get_status(),
        })

    @kmz_bp.route('/api/kmz-discovery/sources')
    def get_kmz_sources():
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute('''
                SELECT id, name, url, provider, category, source_type, status,
                       routes_count, last_checked, discovered_at
                FROM kmz_discovered_sources
                ORDER BY discovered_at DESC
                LIMIT 100
            ''')

            cols = [d[0] for d in cur.description]
            sources = []
            for r in cur.fetchall():
                src = dict(zip(cols, r))
                for ts_field in ('last_checked', 'discovered_at'):
                    if src.get(ts_field):
                        src[ts_field] = str(src[ts_field])
                sources.append(src)

            cur.close()

            return jsonify({
                'success': True,
                'sources': sources,
                'total': len(sources)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            _release(conn)

    app.register_blueprint(kmz_bp)

    os.makedirs(KMZ_DOWNLOAD_DIR, exist_ok=True)

    if start_scheduler:
        start_kmz_scheduler()
        logger.info("🗺️  KMZ Auto-Discovery v4.0: ✅ Registered (Neon, 12-hour auto-cycle)")
    else:
        logger.info("🗺️  KMZ Auto-Discovery v4.0: ✅ Registered (Neon, scheduler PAUSED)")
    logger.info("   GET  /api/kmz-discovery/status  - Discovery status")
    logger.info("   POST /api/kmz-discovery/run     - Trigger discovery cycle")
    logger.info("   GET  /api/kmz-discovery/routes  - Browse discovered routes")
    logger.info("   GET  /api/kmz-discovery/sources - View discovered sources")
    logger.info("   GET  /api/kmz/health            - v4.0 full health + source registry")
