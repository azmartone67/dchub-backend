"""
DC Hub - Automatic KMZ/KML Infrastructure Discovery v3.0
================================================================
Autonomous system that discovers, downloads, and parses KMZ/KML
infrastructure files from public government and industry sources.

v3.0 CHANGES (Mar 2026):
  - Migrated from SQLite to Neon PostgreSQL (data persists across Railway deploys)
  - Uses late-binding DB connection pattern (injected from main.py)
  - PostgreSQL parameterized queries (%s instead of ?)
  - ON CONFLICT instead of INSERT OR IGNORE
  - datetime('now', '-7 days') → NOW() - INTERVAL '7 days'

FIBER SOURCES:
- NTIA Broadband Infrastructure maps
- State broadband offices (BroadbandUSA)
- FCC broadband deployment GIS data
- USGS/HIFLD infrastructure GIS layers
- Public carrier fiber route maps
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
from psycopg2.extras import execute_values  # r-batch (2026-06-18): batched route inserts
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from math import radians, sin, cos, sqrt, atan2
from urllib.parse import quote
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)

KMZ_DOWNLOAD_DIR = os.path.join(os.getcwd(), 'uploads', 'kmz')

# ---------------------------------------------------------------------------
# Late-binding DB connection (injected from main.py)
# ---------------------------------------------------------------------------
_get_pg = None
_return_pg = None




# =============================================================================
# [fix-railway-p1] savepoint-wrapped insert
# Installed 2026-04-17 by fix-railway-p1.py.
#
# Problem: fiber_routes has TWO unique constraints — (source, source_id) and
# (name, provider). INSERT statements in this file use ON CONFLICT (source,
# source_id) DO UPDATE, which handles conflicts on that key but NOT on the
# (name, provider) constraint. When a new row's (name, provider) pair matches
# an existing row's but (source, source_id) differs, psycopg2 raises
# UniqueViolation, the PG transaction aborts, and subsequent writes in the
# same tx also fail. Previously this manifested as 3-retry loops and 62s
# connection holds in Deploy Logs.
#
# Fix: wrap every cur.execute() that touches fiber_routes in a per-row
# SAVEPOINT. If the INSERT hits a UniqueViolation we ROLLBACK only that
# savepoint and continue. No code-site edits needed.
# =============================================================================
def _install_fiber_insert_guard():
    try:
        import psycopg2
        from psycopg2 import errors as _pg_errors
    except Exception:
        return  # psycopg2 not importable here; nothing to install

    _UniqueViolation = getattr(_pg_errors, 'UniqueViolation', None)
    _IntegrityError  = getattr(psycopg2, 'IntegrityError', None)
    if _UniqueViolation is None and _IntegrityError is None:
        return

    try:
        _cursor_cls = psycopg2.extensions.cursor
    except Exception:
        return

    if getattr(_cursor_cls, '_fiber_guard_installed', False):
        return

    _orig_execute = _cursor_cls.execute

    def _guarded_execute(self, query, vars=None):
        q = query if isinstance(query, str) else (query.decode('utf-8', errors='ignore') if isinstance(query, (bytes, bytearray)) else str(query))
        is_fiber_write = 'fiber_routes' in q and ('INSERT' in q.upper() or 'UPDATE' in q.upper())
        if not is_fiber_write:
            return _orig_execute(self, query, vars)
        sp = '_fiber_sp'
        try:
            _orig_execute(self, f'SAVEPOINT {sp}')
        except Exception:
            return _orig_execute(self, query, vars)
        try:
            result = _orig_execute(self, query, vars)
            try:
                _orig_execute(self, f'RELEASE SAVEPOINT {sp}')
            except Exception:
                pass
            return result
        except Exception as e:
            cls = type(e)
            is_dup = (_UniqueViolation and isinstance(e, _UniqueViolation)) or                      (_IntegrityError  and isinstance(e, _IntegrityError))
            try:
                _orig_execute(self, f'ROLLBACK TO SAVEPOINT {sp}')
            except Exception:
                pass
            if is_dup:
                # Duplicate on one of the fiber_routes unique constraints.
                # Silently skip. Original code continues to next row.
                return None
            raise

    try:
        # __kmz_immutable_guard_v3__
        _cursor_cls.execute = _guarded_execute
        _cursor_cls._fiber_guard_installed = True
    except TypeError:
        # Modern psycopg2 makes cursor class immutable; fiber guard
        # will be applied per-cursor at call sites instead.
        pass

_install_fiber_insert_guard()

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

# ★ 2026-09-13 — 53 of the 58 sources this list held were measured dead and
# removed (`git log -p` on this file has them):
#   · 38 ArcGIS services answered HTTP 200 with {"error": {"code": 400,
#     "message": "Invalid URL"}}. None is listed in its organisation's service
#     directory, and three of those organisations do not exist.
#   · 13 named maps.nccs.nasa.gov, which has no DNS address record.
#   · 1 county service answered HTTP 200 with error 404 "Service not found".
#   · 1 ArcGIS Hub dataset page refused /query with HTTP 403.
# _fetch_arcgis_routes read every one of them as a layer with no features: zero
# routes, nothing logged above DEBUG, and a kmz_discovery_log row that said
# 'success'. An unreadable layer is now reported as a failure
# (_arcgis_feature_page), and the cycle row records it (_cycle_status).
#
# The EIA gas and crude layers below name their object-id field FID. The
# OBJECTID ordering sent from 2026-08-22 failed every page of both; pages are
# now ordered by each layer's own field (_arcgis_object_id_field).
PUBLIC_KMZ_SOURCES = [
    # ── FEDERAL FIBER / BROADBAND ────────────────────────────────
    {
        'name': 'NTIA National Broadband Map - Fiber Routes',
        'url': 'https://broadbandmap.fcc.gov/api/public/map/listHandshake',
        'type': 'api_discover',
        'provider': 'FCC/NTIA',
        'category': 'federal'
    },
    # ── POWER INFRASTRUCTURE ─────────────────────────────────────
    {
        'name': 'HIFLD Electric Power Transmission Lines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power',
        'route_type': 'transmission'
    },
    # ── GAS PIPELINE INFRASTRUCTURE ──────────────────────────────
    {
        'name': 'EIA Natural Gas Interstate/Intrastate Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas',
        'route_type': 'gas'
    },
    {
        'name': 'EIA Crude Oil Trunk Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Crude_Oil_Trunk_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas',
        'route_type': 'gas'
    },
    {
        'name': 'EIA Gulf Oil and Gas Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Oil_And_Natural_Gas_Pipelines_Gulf_2024Q4/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas',
        'route_type': 'gas'
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
]


def _arcgis_json_body(response):
    """(body, None) for an HTTP 200 JSON answer that is not an ArcGIS error,
    else (None, reason).

    ★ ArcGIS reports a failed request INSIDE an HTTP 200. A deleted service
    answers {"error": {"code": 400, "message": "Invalid URL"}} and a bad query
    parameter answers the same shape, so a 200 says nothing on its own.
    """
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    try:
        data = response.json()
    except ValueError:
        return None, "HTTP 200 with a body that is not JSON"
    if isinstance(data, dict) and 'error' in data:
        err = data['error']
        if isinstance(err, dict):
            detail = f"{err.get('code')} {err.get('message')}"
            extra = [str(d) for d in (err.get('details') or [])
                     if str(d) != str(err.get('message'))]
            if extra:
                detail += f" ({'; '.join(extra)})"
        else:
            detail = str(err)
        return None, f"HTTP 200 with an error body: {detail}"[:300]
    return data, None


def _arcgis_feature_page(response):
    """(features, None) for a readable ArcGIS /query page, else (None, reason).

    `.get('features', [])` turns a failed query into a layer with no features.
    Only a body that carries a features list is a page.
    """
    data, reason = _arcgis_json_body(response)
    if reason:
        return None, reason
    if not isinstance(data, dict) or not isinstance(data.get('features'), list):
        return None, "HTTP 200 without a features list"
    return data['features'], None


def _arcgis_object_id_field(response):
    """(field, None) naming a layer's object-id field from its ?f=json
    metadata, else (None, reason).

    ★ It is not always OBJECTID. Measured 2026-09-13: the EIA gas and crude
    pipeline layers call it FID, hosted views objectid, and the HIFLD
    transmission layer OBJECTID_1 (its OBJECTID is an ordinary integer).
    Feature services publish objectIdField. Map service layers (10.81 to 12.1)
    omit it and type the field esriFieldTypeOID in `fields`, so that is read
    when objectIdField is absent.
    """
    data, reason = _arcgis_json_body(response)
    if reason:
        return None, reason
    if not isinstance(data, dict):
        return None, "HTTP 200 with metadata that is not an object"
    field = data.get('objectIdField')
    if isinstance(field, str) and field:
        return field, None
    oid_fields = [f.get('name') for f in (data.get('fields') or [])
                  if isinstance(f, dict) and f.get('type') == 'esriFieldTypeOID']
    if len(oid_fields) == 1 and isinstance(oid_fields[0], str) and oid_fields[0]:
        return oid_fields[0], None
    return None, (f"HTTP 200 with metadata that names no object-id field "
                  f"(objectIdField {field!r}, {len(oid_fields)} esriFieldTypeOID fields)")


def _cycle_status(results: Dict) -> str:
    """The kmz_discovery_log status of a finished cycle: ok, partial or failed.

    Judged on the curated PUBLIC_KMZ_SOURCES lane: 'failed' when that stage
    raised or every layer it queried was unreadable, 'partial' when some were
    or another stage raised. A layer that declares no route_type is counted
    as unreadable.

    Not 'success': every row written before 2026-09-13 says 'success' whatever
    the cycle found, and a reader must be able to tell a measured outcome from
    that constant.
    """
    known = results.get('known_sources') or {}
    queried = known.get('queried') or 0
    failed = known.get('failed') or 0
    if 'error' in known or (failed and failed >= queried):
        return 'failed'
    other_stage_raised = any('error' in (results.get(stage) or {})
                             for stage in ('arcgis_search', 'state_broadband'))
    if failed or other_stage_raised:
        return 'partial'
    return 'ok'


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

            # 2026-08-22: the writer's ON CONFLICT names the identity
            # (source_url, kmz_file, md5(coordinates)). That UNIQUE index was
            # built out-of-band (fiber_kmz_routes_identity_uq, after the 12.3M
            # -> ~50k collapse) and is deliberately NOT created here: init runs
            # at every boot in one transaction, and CREATE UNIQUE INDEX on a
            # table with duplicates would abort the whole init. Assert instead,
            # loudly, so a restore/re-create without the index cannot turn every
            # cycle into a silent no-op.
            try:
                cur.execute('''
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'fiber_kmz_routes'
                      AND indexdef ILIKE 'CREATE UNIQUE INDEX%%'
                      AND indexdef ILIKE '%%md5(coordinates)%%'
                ''')
                if cur.fetchone() is None:
                    logger.error(
                        "fiber_kmz_routes has NO identity UNIQUE index "
                        "(source_url, kmz_file, md5(coordinates)) — the writer's "
                        "ON CONFLICT target will raise on every cycle. Create "
                        "fiber_kmz_routes_identity_uq out-of-band.")
            except Exception as e:
                logger.warning(f"identity-index check skipped: {e}")

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

        # ★ 2026-09-13: no export stage. It fetched up to 30 DISCOVERED layers a
        # cycle and wrote their features as route_type 'fiber'. Those layers are
        # ArcGIS search results, and their category names the search that found
        # them, not what they hold. Measured in production: 33,292 rows, 6 of them
        # from layers whose title names fiber, broadband, telecom, conduit or
        # cable. The rest came from oil and gas wells and pipelines, flood zones,
        # water mains, power stations, service territories and railroads. A layer
        # is written only from PUBLIC_KMZ_SOURCES, under its declared route_type.

        cycle_duration = round(time.time() - cycle_start, 1)
        results['cycle_duration_seconds'] = cycle_duration

        self._cache['last_cycle'] = datetime.now().isoformat()
        self._cache['last_results'] = results
        self._cache['total_routes_discovered'] += results['total_new_routes']

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
        # `queried` counts the layers taken up and `failed` the ones that could
        # not be read or labelled, so a lane whose layers are all gone cannot
        # read the same as one whose layers are merely unchanged.
        results = {'checked': 0, 'queried': 0, 'failed': 0, 'failed_sources': [],
                   'routes_found': 0, 'total_km': 0}

        for source in PUBLIC_KMZ_SOURCES:
            try:
                results['checked'] += 1

                if source['type'] == 'arcgis_kml':
                    results['queried'] += 1
                    # ★ 2026-09-13: each curated layer declares what its rows are. The
                    # label was 'gas' for category gas and 'fiber' for any other
                    # category, so the HIFLD transmission lines were stored as fiber
                    # (7,748 rows). A layer that declares no route_type is refused,
                    # never given a default.
                    route_type = source.get('route_type')
                    if not route_type:
                        raise ValueError("no route_type declared, so its rows cannot be labelled")
                    r = self._fetch_arcgis_routes(source['url'], source['provider'], source['name'], route_type=route_type)
                    results['routes_found'] += r.get('routes_found', 0)
                    results['total_km'] += r.get('total_km', 0)
                    if r.get('error'):
                        results['failed'] += 1
                        results['failed_sources'].append({'name': source['name'], 'error': r['error']})
                        logger.warning(f"Known source FAILED: {source['name']}: {r['error']}")

                self._add_discovered_source({
                    'name': source['name'],
                    'url': source['url'],
                    'provider': source['provider'],
                    'category': source['category'],
                    'source_type': source['type']
                })

                time.sleep(1)
            except Exception as e:
                results['failed'] += 1
                results['failed_sources'].append({'name': source.get('name'), 'error': f"{type(e).__name__}: {str(e)[:200]}"})
                logger.warning(f"Known source error for {source.get('name')}: {e}")

        logger.info(f"Known Sources: checked={results['checked']}, queried={results['queried']}, failed={results['failed']}, routes={results['routes_found']}, km={results['total_km']:.1f}")
        return results

    # ── Fetch ArcGIS Routes (Paginated) ──────────────────────────

    def _fetch_arcgis_routes(self, url: str, provider: str, source_name: str, route_type: str) -> Dict:
        """Fetch routes from ArcGIS FeatureServer with pagination. Pulls up to MAX_FEATURES per source.

        Returns routes_found and total_km, plus `error` when a page could not be
        read: a status other than 200, a body that is not a feature page, or a
        transport failure. Rows buffered from earlier pages are still written.
        Also `error`, with no page requested, when the layer's metadata does not
        name its object-id field: pages cannot be ordered without it.
        """
        results = {'routes_found': 0, 'total_km': 0}
        MAX_FEATURES = 5000     # Max total features per source per cycle
        BATCH_SIZE = 1000       # ArcGIS max per request
        MAX_COORDS = 200        # Coordinates per route to store
        MAX_PATH_POINTS = 150   # Points per path/ring to capture

        offset = 0
        total_fetched = 0

        # r47.36 (2026-05-26): the watchdog was logging FORCED RECLAIM at
        # 60-73s on this call site every cycle. Root cause: the original
        # try/except had the conn allocation INSIDE the try, but the
        # release was inside the same try AFTER the while loop — so any
        # exception in the loop bypassed the release, requiring the
        # 60s watchdog to reclaim. Restructure: conn allocation guarded
        # by an outer try/finally so release is unconditional.
        conn = None
        cur = None
        try:
            # r88-pool (2026-06-15): buffer rows during the slow ArcGIS HTTP loop
            # and write them in ONE burst after (phase 2 below). We must never hold
            # a pooled DB connection across session.get — each is up to 45s and a
            # source spans many batches, so the connection was held 60-89s and the
            # 60s watchdog FORCED-RECLAIMed it every cycle (r47.36 fixed the release
            # path but not the hold). conn is now opened only in phase 2.
            pending = []  # list of (params_tuple, distance_km)

            # 2026-09-13: order by the layer's OWN object-id field, read once per
            # source. The literal OBJECTID is not a field on every layer: the EIA
            # gas and crude layers answered every page HTTP 200 with "'OBJECTID'
            # parameter is invalid". Unordered paging overlaps, so a field that
            # cannot be read is reported, never replaced by no ordering.
            try:
                oid_field, meta_error = _arcgis_object_id_field(
                    self.session.get(f"{url}?f=json", timeout=30))
            except Exception as e:
                oid_field, meta_error = None, f"{type(e).__name__}: {str(e)[:200]}"
            if meta_error:
                results['error'] = f"layer metadata unreadable, so no page was requested: {meta_error}"
                return results
            order_by = quote(oid_field, safe='')

            while total_fetched < MAX_FEATURES:
                query_url = (
                    f"{url}/query?where=1%3D1&outFields=*"
                    f"&resultRecordCount={BATCH_SIZE}&resultOffset={offset}"
                    # r-qa (2026-06-27): outSR=4326 forces WGS84 degrees. Without it
                    # ArcGIS returns native Web Mercator METERS (EPSG:3857); meter
                    # coords fed to the haversine inflated each distance ~1,400x —
                    # the "1.68-billion-km new routes" log bug.
                    f"&returnGeometry=true&outSR=4326&f=json"
                    # 2026-08-22: stable paging. Without an ORDER BY, ArcGIS pages
                    # overlap (682 exact duplicate features per 15k-row cycle).
                    f"&orderByFields={order_by}"
                )

                try:
                    response = self.session.get(query_url, timeout=45)
                    features, page_error = _arcgis_feature_page(response)
                    if page_error:
                        # Not the end of the data: a page that could not be read.
                        results['error'] = page_error
                        break

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
                        # 2026-08-22: a feature whose endpoints are not WGS84 degrees
                        # (Web-Mercator metres from a source that ignored outSR) used
                        # to be stored with distance 0 and a metre-valued identity —
                        # ~31% of the kept identities before the collapse. Skip it.
                        if any(abs(pt[0]) > 90 or abs(pt[1]) > 180 for pt in (coordinates[0], coordinates[-1])):
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

                        pending.append((
                            (display_name, str(operator)[:100], route_type,
                             start_point, end_point,
                             round(distance_km, 2),
                             json.dumps(coordinates[:MAX_COORDS]),
                             f"arcgis_export_{url_hash}",
                             url),
                            distance_km,
                        ))

                    total_fetched += len(features)
                    offset += len(features)

                    # If we got fewer than batch size, we've hit the end
                    if len(features) < BATCH_SIZE:
                        break

                    # Brief pause between pages to be respectful
                    time.sleep(0.5)

                except Exception as e:
                    results['error'] = f"{type(e).__name__} at offset {offset}: {str(e)[:200]}"
                    break

            # Phase 2: open the pooled connection ONLY now (HTTP is done) and write
            # the buffered rows. r-batch (2026-06-18): switched from a per-row execute
            # loop to a single batched execute_values. The old loop did up to 5000 rows
            # one at a time — and with the per-cursor fiber guard each INSERT is wrapped
            # in SAVEPOINT/INSERT/RELEASE (3 round-trips) against remote Neon, so the
            # connection was held 60-77s. The 60s pool watchdog (main.py _forced_reclaim_loop)
            # then cancel()'d the in-flight query and closed the connection underneath
            # this thread EVERY cycle. Batching collapses ~15k round-trips into a handful,
            # dropping the hold to a second or two. RETURNING distance_km under
            # ON CONFLICT DO NOTHING yields only the newly-inserted rows, so the
            # routes_found / total_km tally stays exact.
            if pending:
                conn = _conn()
                cur = conn.cursor()
                try:
                    inserted = execute_values(
                        cur,
                        '''INSERT INTO fiber_kmz_routes
                            (name, provider, route_type, start_point, end_point,
                             distance_km, coordinates, kmz_file, source_url)
                           VALUES %s
                           ON CONFLICT (source_url, kmz_file, md5(coordinates)) DO NOTHING
                           RETURNING distance_km''',
                        [p[0] for p in pending],
                        page_size=500,
                        fetch=True,
                    )
                    results['routes_found'] = len(inserted)
                    results['total_km'] = sum((row[0] or 0) for row in inserted)
                    conn.commit()
                except Exception as e:
                    # 2026-08-22: WARNING + swallowed-write note. At DEBUG a failing
                    # batch INSERT was invisible and read as "0 new routes".
                    logger.warning(f"Batched route insert error ({len(pending)} rows, {source_name}): {e}")
                    note_swallowed_write("fiber_kmz_routes", where="kmz_auto_discovery._fetch_arcgis_routes")
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            if results['routes_found'] > 0:
                logger.info(f"  {source_name}: {results['routes_found']} routes, {results['total_km']:.1f} km (fetched {total_fetched} features)")

        except Exception as e:
            results['error'] = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"ArcGIS route fetch error for {source_name}: {e}")
        finally:
            # r47.36: GUARANTEED release on every exit path. Previously
            # only reached on the happy path → 60-73s watchdog reclaims
            # whenever the loop raised.
            if cur is not None:
                try: cur.close()
                except Exception: pass
            if conn is not None:
                try: _release(conn)
                except Exception: pass

        return results

    # ── State Broadband Discovery ──────────────────────────────

    def _discover_state_broadband(self) -> Dict:
        results = {'checked': 0, 'services_found': 0, 'new_sources': 0}

        for state in STATE_BROADBAND_GIS:
            try:
                results['checked'] += 1

                catalog_url = f"{state['url']}?f=json"
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
            note_swallowed_write("kmz_discovered_sources", where="kmz_auto_discovery._add_discovered_source")
            return False
        finally:
            _release(conn)

    def _calculate_route_distance(self, coordinates: List[List[float]]) -> float:
        total_distance = 0
        for i in range(len(coordinates) - 1):
            lat1, lng1 = coordinates[i]
            lat2, lng2 = coordinates[i + 1]
            # r-qa (2026-06-27): defense-in-depth — skip any point that isn't valid
            # WGS84 degrees (e.g. Web Mercator meters from a source missing
            # outSR=4326), so a non-degree coord can't re-poison the km total.
            if abs(lat1) > 90 or abs(lat2) > 90 or abs(lng1) > 180 or abs(lng2) > 180:
                continue
            R = 6371
            lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
            dlat = lat2 - lat1
            dlng = lng2 - lng1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            total_distance += R * c
        return round(total_distance, 2)

    def _log_cycle(self, results: Dict):
        # ★ 2026-09-13: the status written below was the literal 'success' for
        # every cycle, including cycles in which no known layer could be read.
        cycle_status = _cycle_status(results)
        if cycle_status != 'ok':
            known = results.get('known_sources') or {}
            logger.warning(
                f"KMZ cycle {cycle_status}: {known.get('failed', 0)} of "
                f"{known.get('queried', 0)} known ArcGIS layers unreadable: "
                + "; ".join(f"{s.get('name')}: {s.get('error')}"
                            for s in known.get('failed_sources') or [])[:1500])
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO kmz_discovery_log
                (source_name, source_url, source_type, routes_found, total_km, status)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            ''', (
                'auto_discovery_cycle',
                'scheduler',
                'full_cycle',
                results.get('total_new_routes', 0),
                results.get('total_new_km', 0),
                cycle_status
            ))
            conn.commit()
            cur.close()
        except Exception as e:
            # WARNING, not debug: this row IS the cycle watermark data-sync.yml
            # polls. A silent failure here reads as "the cycle never completes".
            logger.warning(f"KMZ log error — cycle watermark NOT written: {e}")
        finally:
            _release(conn)

    def get_status(self, full: bool = False) -> Dict:
        """Engine status. `full=False` (the default, and what data-sync.yml
        polls) answers from the watermark + small tables + a planner ESTIMATE
        of fiber_kmz_routes; `full=True` runs the exact COUNT/SUM/GROUP BY.

        ★ 2026-08-21: fiber_kmz_routes is 12.3M rows (it re-ingests ~15k routes
        every cycle — a separate defect). COUNT(*) + SUM(distance_km) +
        GROUP BY provider over it took ~30s per call, hit the statement
        timeout, and held a pooled connection the whole time ("POOL HOLD:
        conn held 30.1s by GET /api/kmz-discovery/status") — ten polls per
        data-sync run on a pool already at 75-80%. Nothing reads those three
        fields (only data-sync.yml polls this endpoint, for last_cycle_at),
        so they are opt-in now.
        """
        status = {
            'running': self._scheduler_running,
            # ★ 2026-08-19 — READ THE NEXT TWO KEYS AS A PAIR.
            #
            # `last_cycle` comes from self._cache, an attribute of the
            # _kmz_instance singleton: PROCESS-LOCAL, one copy per service
            # under `gunicorn --workers 1`. /api/kmz-discovery/run is in
            # main.py's _WORKER_PROXY_POST_PATHS, so the cycle EXECUTES ON
            # dchub-worker and advances the WORKER's cache, while this GET
            # stays local and answers from web's — which nothing writes.
            # last_cycle has therefore read null on web since delegation
            # landed, and a caller polling it to see whether a delegated
            # cycle finished would wait forever. That is exactly the trap
            # #2929 hit with brain_autonomy_loop._LAST_TICK.
            #
            # `last_cycle_at` is the DB answer to the same question, read
            # from kmz_discovery_log — the row _log_cycle() writes at the end
            # of EVERY completed cycle, whether or not it found routes. Both
            # services read the same value, so this endpoint needs no
            # worker-proxy allowlist entry. data-sync.yml polls THIS key.
            'last_cycle': self._cache.get('last_cycle'),
            'last_cycle_at': None,
            # The outcome recorded in that same newest cycle row: ok, partial or
            # failed (_cycle_status). Rows written before 2026-09-13 say
            # 'success', which measured nothing. None when it cannot be read.
            'last_cycle_status': None,
            'total_routes_discovered': self._cache.get('total_routes_discovered', 0),
        }

        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            # ★ 2026-08-21: the LIVE column is TEXT, not the TIMESTAMPTZ the
            # CREATE TABLE above declares (that DDL is IF NOT EXISTS and the
            # table predates it). MAX() over TEXT returned a str, .isoformat()
            # raised AttributeError, the except below swallowed it, and this
            # endpoint published last_cycle_at=null on EVERY read — while the
            # worker log showed "KMZ DISCOVERY CYCLE COMPLETE" every 3h.
            # data-sync.yml polls this key, so it failed every other run with
            # "no cycle has EVER been recorded". Cast in SQL (a no-op on a real
            # timestamptz column) and never call .isoformat() on a non-datetime.
            # ★ 2026-08-21 (second pass): MAX(discovered_at::timestamptz) cast
            # EVERY row of the log table and hit the statement timeout under
            # load — the live log shows "KMZ status query error: canceling
            # statement due to statement timeout" with this connection held
            # 30s (POOL HOLD), and data-sync.yml polls this endpoint 10x per
            # run. The newest cycle row is the highest id (SERIAL PK), so walk
            # the PK backwards to the first cycle row and cast that ONE value.
            cur.execute("""
                SELECT discovered_at, status FROM kmz_discovery_log
                 WHERE source_name = 'auto_discovery_cycle'
                 ORDER BY id DESC
                 LIMIT 1
            """)
            _row = cur.fetchone()
            _lc = _row[0] if _row else None
            if _row and len(_row) > 1:
                status['last_cycle_status'] = _row[1]
            if _lc is None:
                status['last_cycle_at'] = None
            elif hasattr(_lc, 'isoformat'):
                status['last_cycle_at'] = _lc.isoformat()
            else:
                # TEXT column (the live shape): normalise to ISO so
                # data-sync.yml's fromisoformat() can read it.
                from datetime import datetime as _dt
                try:
                    status['last_cycle_at'] = _dt.fromisoformat(str(_lc).strip().replace(' ', 'T', 1)).isoformat()
                except Exception:
                    status['last_cycle_at'] = str(_lc)

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources")
            status['total_sources'] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources WHERE status = 'active'")
            status['active_sources'] = cur.fetchone()[0]

            # Planner estimate: O(1), no scan. Labelled as an estimate.
            cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'fiber_kmz_routes'")
            _est = cur.fetchone()
            status['total_routes_in_db_estimate'] = int(_est[0]) if _est and _est[0] is not None else None
            status['stats_mode'] = 'estimate'

            if full:
                cur.execute("SELECT COUNT(*) FROM fiber_kmz_routes")
                status['total_routes_in_db'] = cur.fetchone()[0]

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
                status['stats_mode'] = 'full'

            cur.close()
        except Exception as e:
            # WARNING, not debug — a swallowed read here is indistinguishable
            # from "no cycle ever ran" to every consumer of last_cycle_at.
            logger.warning(f"KMZ status query error (last_cycle_at unreadable): {e}")
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
    from internal_auth import is_valid_internal_key

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
        # ?full=1 opts into the exact COUNT/SUM/GROUP BY over fiber_kmz_routes
        # (~30s on 12.3M rows); the default is the cheap read the cron polls.
        _full = (flask_request.args.get('full') or '').strip().lower() in ('1', 'true', 'yes')
        return jsonify({
            'success': True,
            'engine': 'KMZ Auto-Discovery v3.0 (Neon)',
            **_kmz_instance.get_status(full=_full)
        })

    @kmz_bp.route('/api/kmz-discovery/run', methods=['POST'])
    def run_kmz_discovery():
        if not is_valid_internal_key(flask_request.headers.get("X-Internal-Key") or flask_request.headers.get("X-Admin-Key")):
            return jsonify({'success': False, 'error': 'unauthorized'}), 401
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

    @kmz_bp.route('/api/kmz-discovery/prune', methods=['POST'])
    def prune_kmz_sources():
        # One-shot cleanup: the scheduler is shelved (#4) and these 'discovered' sources
        # never ingested (dead/random ArcGIS). Confirm-gated; low-risk even if triggered.
        if not is_valid_internal_key(flask_request.headers.get("X-Internal-Key") or flask_request.headers.get("X-Admin-Key")):
            return jsonify({'success': False, 'error': 'unauthorized'}), 401
        if flask_request.args.get('confirm') != 'prune-stale-2026':
            return jsonify({'success': False, 'error': 'pass ?confirm=prune-stale-2026'}), 400
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM kmz_discovered_sources WHERE status = 'discovered' AND last_checked IS NULL")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            return jsonify({'success': True, 'deleted': deleted})
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            _release(conn)

    app.register_blueprint(kmz_bp)

    os.makedirs(KMZ_DOWNLOAD_DIR, exist_ok=True)

    if start_scheduler:
        start_kmz_scheduler()
        logger.info("🗺️  KMZ Auto-Discovery v3.0: ✅ Registered (Neon, 12-hour auto-cycle)")
    else:
        logger.info("🗺️  KMZ Auto-Discovery v3.0: ✅ Registered (Neon, scheduler PAUSED)")
    logger.info("   GET  /api/kmz-discovery/status  - Discovery status")
    logger.info("   POST /api/kmz-discovery/run     - Trigger discovery cycle")
    logger.info("   GET  /api/kmz-discovery/routes  - Browse discovered routes")
    logger.info("   GET  /api/kmz-discovery/sources - View discovered sources")